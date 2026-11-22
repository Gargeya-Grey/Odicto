"""Read a complete editable field only when its native caret has no selection.

Whole-field edits address the captured control directly. They never send Select All
to the foreground application, which could select an entire web page or file list.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import sys
from typing import Callable


@dataclass(frozen=True)
class PolishField:
    control: object
    caret: int
    text: str
    select: Callable[[], bool] = field(compare=False, repr=False)


class IneligibleField(Exception):
    """The accessibility provider identified a field that must not be edited."""


def focused_polish_field(window, limit: int) -> PolishField | None:
    try:
        if sys.platform == "win32":
            native = _windows_field(window, limit)
            return native if native is not None else _windows_uia_field(window, limit)
        if sys.platform == "darwin":
            return _mac_field(window, limit)
        if sys.platform.startswith("linux"):
            return _linux_field(window, limit)
    except IneligibleField:
        raise
    except Exception:
        # Another process owns the control. It can disappear during any query.
        pass
    return None


def _windows_focus(window):
    import ctypes as ct
    from ctypes import wintypes as wt

    class GuiThreadInfo(ct.Structure):
        _fields_ = [("size", wt.DWORD), ("flags", wt.DWORD),
                    ("active", wt.HWND), ("focus", wt.HWND), ("capture", wt.HWND),
                    ("menu", wt.HWND), ("move", wt.HWND), ("caret", wt.HWND),
                    ("caret_rect", wt.RECT)]

    api = ct.WinDLL("user32", use_last_error=True)
    info = GuiThreadInfo()
    info.size = ct.sizeof(info)
    if not api.GetGUIThreadInfo(0, ct.byref(info)) or info.active != window or not info.focus:
        return None
    return info.focus


def _windows_field(window, limit):
    handle = _windows_focus(window)
    if handle:
        return _windows_edit(handle, limit, lambda: _windows_focus(window) == handle)
    return None


def _windows_edit(handle, limit, still_focused):
    import ctypes as ct
    from ctypes import wintypes as wt

    api = ct.WinDLL("user32", use_last_error=True)
    hwnd = wt.HWND(handle)
    name = ct.create_unicode_buffer(128)
    api.GetClassNameW(hwnd, name, len(name))
    if name.value.casefold() not in {
        "edit", "richedit", "richedit20a", "richedit20w", "richedit50w", "richeditd2dpt",
    }:
        return None
    # ES_PASSWORD, ES_READONLY, WS_DISABLED.
    if api.GetWindowLongW(hwnd, -16) & (0x20 | 0x800 | 0x08000000):
        raise IneligibleField

    def message(code, wparam=0, lparam=0):
        result = ct.c_size_t()
        if not api.SendMessageTimeoutW(hwnd, code, ct.c_size_t(wparam), ct.c_ssize_t(lparam),
                                       2, 150, ct.byref(result)):
            raise OSError("Edit control did not respond")
        return result.value

    if message(0x00D2):  # EM_GETPASSWORDCHAR
        raise IneligibleField
    def selection():
        first, last = wt.DWORD(), wt.DWORD()
        message(0x00B0, ct.addressof(first), ct.addressof(last))
        return first.value, last.value

    start, stop = selection()
    collapsed = start == stop
    classic = name.value.casefold() == "edit"
    if not collapsed and not classic:
        return None  # RichEdit exposes native range offsets through UI Automation.
    size = message(0x000E)  # WM_GETTEXTLENGTH
    if not size or (collapsed and limit and size > limit * 2):
        raise IneligibleField
    if not collapsed and size > 1_000_000:
        return None  # Avoid copying a large document to inspect a small selection.

    def range_text():
        value = ct.create_unicode_buffer(size + 1)
        if message(0x000E) != size or message(0x000D, len(value), ct.addressof(value)) != size:
            raise IneligibleField
        if collapsed:
            return value.value
        # Edit offsets use UTF-16 code units, including surrogate pairs.
        return ct.wstring_at(ct.addressof(value) + 2 * start, stop - start)

    if not collapsed and not (0 <= start < stop <= size):
        raise IneligibleField
    text = range_text()
    if not text or (limit and len(text) > limit):
        raise IneligibleField

    def select():
        if collapsed:
            message(0x00B1, 0, -1)  # EM_SETSEL, restricted to this HWND
            first, last = selection()
            end = size if classic else size - text.count("\r\n")
            if first != 0 or last not in (end, size):
                return False
        elif selection() != (start, stop):
            return False
        return range_text() == text and still_focused()

    return PolishField((handle, collapsed), start, text, select)


def _windows_uia_field(window, limit, expected=None):
    from platforms.windows import foreground_window_id
    if foreground_window_id() != window:
        raise IneligibleField
    import comtypes
    from comtypes.client import CreateObject, GetModule

    comtypes.CoInitialize()
    try:
        api = GetModule("UIAutomationCore.dll")
        automation = CreateObject(api.CUIAutomation, interface=api.IUIAutomation)
        control = automation.GetFocusedElement()
        if control.CurrentIsPassword or not control.CurrentIsEnabled:
            raise IneligibleField
        kind = control.CurrentControlType
        if kind not in (api.UIA_EditControlTypeId, api.UIA_DocumentControlTypeId):
            return None
        value = control.GetCurrentPattern(api.UIA_ValuePatternId)
        pattern = control.GetCurrentPattern(api.UIA_TextPatternId)
        if value and value.QueryInterface(api.IUIAutomationValuePattern).CurrentIsReadOnly:
            raise IneligibleField
        if not pattern:
            return None
        pattern = pattern.QueryInterface(api.IUIAutomationTextPattern)
        ranges = pattern.GetSelection()
        if ranges.Length != 1:
            raise IneligibleField
        span = ranges.GetElement(0)
        start, end = api.TextPatternRangeEndpoint_Start, api.TextPatternRangeEndpoint_End
        collapsed = span.CompareEndpoints(start, span, end) == 0
        if collapsed and kind == api.UIA_DocumentControlTypeId:
            raise IneligibleField  # A document can be an entire web page.
        document = pattern.DocumentRange
        chosen = document if collapsed else span
        if not value and chosen.GetAttributeValue(api.UIA_IsReadOnlyAttributeId) is not False:
            raise IneligibleField
        # UIA counts UTF-16 units. A non-BMP character can occupy two.
        text = chosen.GetText(2 * limit + 1 if limit else -1)
        if not text or (limit and len(text) > limit):
            raise IneligibleField
        prefix = document.Clone()
        prefix.MoveEndpointByRange(end, span, start)
        offset = -prefix.MoveEndpointByUnit(end, api.TextUnit_Character, -0x7fffffff)
        identity = ((tuple(control.GetRuntimeId()), collapsed), offset, text)
        snapshot = PolishField(*identity,
                              lambda: _windows_uia_field(window, limit, expected=identity))
        if not automation.CompareElements(control, automation.GetFocusedElement()):
            raise IneligibleField
        if expected is None:
            return snapshot
        if identity != expected:
            return False
        chosen.Select()
        selected = pattern.GetSelection()
        if selected.Length != 1:
            return False
        actual = selected.GetElement(0)
        return (actual.Compare(chosen) and actual.GetText(2 * len(text) + 1) == text
                and automation.CompareElements(control, automation.GetFocusedElement()))
    finally:
        comtypes.CoUninitialize()


def _mac_field(window, limit):
    import ApplicationServices as ax
    from CoreFoundation import CFRangeMake

    def attr(element, key):
        status, value = ax.AXUIElementCopyAttributeValue(element, key, None)
        if status:
            raise ValueError(key)
        return value

    control = attr(ax.AXUIElementCreateSystemWide(), "AXFocusedUIElement")
    status, pid = ax.AXUIElementGetPid(control, None)
    if status or pid != window or attr(control, "AXRole") not in ("AXTextField", "AXTextArea"):
        return None
    status, subrole = ax.AXUIElementCopyAttributeValue(control, "AXSubrole", None)
    if status not in (0, ax.kAXErrorAttributeUnsupported, ax.kAXErrorNoValue) or subrole == "AXSecureTextField":
        raise IneligibleField
    for key in ("AXSelectedText", "AXSelectedTextRange"):
        if ax.AXUIElementIsAttributeSettable(control, key, None) != (0, True):
            return None
    valid, span = ax.AXValueGetValue(attr(control, "AXSelectedTextRange"), ax.kAXValueCFRangeType, None)
    if not valid or not attr(control, "AXEnabled"):
        raise IneligibleField
    text = attr(control, "AXSelectedText" if span.length else "AXValue")
    if not isinstance(text, str) or not text or (limit and len(text) > limit):
        raise IneligibleField

    def select():
        extent = span if span.length else CFRangeMake(0, len(text.encode("utf-16-le")) // 2)
        selected = ax.AXValueCreate(ax.kAXValueCFRangeType, extent)
        if ax.AXUIElementSetAttributeValue(control, "AXSelectedTextRange", selected):
            return False
        return (attr(control, "AXSelectedText") == text
                and attr(ax.AXUIElementCreateSystemWide(), "AXFocusedUIElement") == control)

    return PolishField((control, span.length), span.location, text, select)


def _linux_field(window, limit):
    # PyGObject/AT-SPI is optional. Import only on F8.
    from gi.repository import Atspi
    from platforms import linux

    if linux.foreground_window_id() != window:
        return None
    pid = linux._x11_active_window_pid()
    desktop = Atspi.get_desktop(0)
    for index in range(desktop.get_child_count()):
        app = desktop.get_child_at_index(index)
        if app.get_process_id() != pid:
            continue
        states = Atspi.StateSet.new([Atspi.StateType.FOCUSED, Atspi.StateType.EDITABLE])
        all_states = Atspi.CollectionMatchType.ALL
        unused = Atspi.CollectionMatchType.ALL
        rule = Atspi.MatchRule.new(states, all_states, {}, unused, [], unused, [], unused, False)
        matches = app.get_matches(rule, Atspi.CollectionSortOrder.CANONICAL, 2, False)
        if len(matches) != 1:
            return None
        control = matches[0]
        if control.get_role() == Atspi.Role.PASSWORD_TEXT:
            raise IneligibleField
        if control.get_role() not in (Atspi.Role.ENTRY, Atspi.Role.TEXT) or not control.get_editable_text_iface():
            return None
        count = Atspi.Text.get_n_selections(control)
        if count > 1:
            raise IneligibleField
        if count:
            span = Atspi.Text.get_selection(control, 0)
            start, end = span.start_offset, span.end_offset
        else:
            start, end = 0, Atspi.Text.get_character_count(control)
        if end <= start or (limit and end - start > limit):
            raise IneligibleField
        text = Atspi.Text.get_text(control, start, end)
        if len(text) != end - start or linux.foreground_window_id() != window:
            raise IneligibleField

        def select():
            if count:
                ok = Atspi.Text.set_selection(control, 0, start, end)
            else:
                ok = Atspi.Text.add_selection(control, start, end)
            if not ok or Atspi.Text.get_n_selections(control) != 1:
                return False
            selected = Atspi.Text.get_selection(control, 0)
            control.clear_cache()
            return (selected.start_offset == start and selected.end_offset == end
                    and Atspi.Text.get_text(control, start, end) == text
                    and control.get_state_set().contains(Atspi.StateType.FOCUSED))

        return PolishField((control, start, count), Atspi.Text.get_caret_offset(control), text, select)
    return None
