"""Windows native range probes exercise hidden controls owned by the test only."""
import sys
import unittest

if sys.platform == "win32":
    import ctypes as ct
    from ctypes import wintypes as wt
    from threading import Thread, Event
    from unittest.mock import patch
    from platforms.polish_field import IneligibleField, _windows_edit, _windows_uia_field

    class TestWindowsPolishField(unittest.TestCase):
        def setUp(self):
            self.api = ct.WinDLL("user32")
            self.api.CreateWindowExW.restype = wt.HWND
            self.ready = Event()
            self.thread = Thread(target=self.run_gui, daemon=True)
            self.thread.start()
            self.assertTrue(self.ready.wait(5))
            self.addCleanup(self.close_windows)
            self.assertTrue(self.edit and self.rich and self.password)

        def run_gui(self):
            self.rich_library = ct.WinDLL("msftedit.dll")
            def create(name, style):
                # WS_POPUP without WS_VISIBLE. No activation or input injection.
                return self.api.CreateWindowExW(0, name, "My draft", 0x80000000 | style,
                                                0, 0, 128, 48, None, None, None, None)
            self.edit = create("EDIT", 0)
            self.rich = create("RICHEDIT50W", 0)
            self.password = create("EDIT", 0x20)
            self.ready.set()
            msg = wt.MSG()
            while self.api.GetMessageW(ct.byref(msg), None, 0, 0) > 0:
                self.api.DispatchMessageW(ct.byref(msg))
            for handle in [self.edit, self.rich, self.password]:
                self.api.DestroyWindow(wt.HWND(handle))

        def close_windows(self):
            self.api.PostThreadMessageW(self.thread.native_id, 0x12, 0, 0)
            self.thread.join(5)
            self.assertFalse(self.thread.is_alive())

        def test_empty_native_caret_selects_only_owned_edit(self):
            self.api.SendMessageW(wt.HWND(self.edit), 0x00B1, 2, 2)
            snapshot = _windows_edit(self.edit, 1200, lambda: True)
            self.assertEqual((snapshot.caret, snapshot.text), (2, "My draft"))
            self.assertTrue(snapshot.select())
            self.assertEqual(self.api.SendMessageW(wt.HWND(self.edit), 0x00B0, 0, 0), 8 << 16)
            with self.assertRaises(IneligibleField):
                _windows_edit(self.password, 1200, lambda: True)

        def test_explicit_selection_in_classic_edit_uses_utf16_offsets(self):
            self.api.SetWindowTextW(wt.HWND(self.edit), "\U0001f600My draft")
            self.api.SendMessageW(wt.HWND(self.edit), 0x00B1, 2, 4)
            snapshot = _windows_edit(self.edit, 10, lambda: True)
            self.assertEqual(snapshot.text, "My")
            self.assertTrue(snapshot.select())

        def test_native_range_rejects_same_length_edit_before_injection(self):
            snapshot = _windows_edit(self.edit, 1200, lambda: True)
            self.api.SetWindowTextW(wt.HWND(self.edit), "New text")
            self.assertFalse(snapshot.select())
            value = ct.create_unicode_buffer(20)
            self.api.GetWindowTextW(wt.HWND(self.edit), value, len(value))
            self.assertEqual(value.value, "New text")

        def test_uia_range_prefers_selection_and_rechecks_caret(self):
            from comtypes.client import GetModule, CreateObject
            api = GetModule("UIAutomationCore.dll")
            ui = CreateObject(api.CUIAutomation, interface=api.IUIAutomation)
            control = ui.ElementFromHandle(self.rich)
            class OwnedFocus:
                CompareElements = staticmethod(ui.CompareElements)
                GetFocusedElement = staticmethod(lambda: control)
            with patch("platforms.windows.foreground_window_id", return_value=self.rich), patch(
                    "comtypes.client.CreateObject", return_value=OwnedFocus()):
                self.api.SendMessageW(wt.HWND(self.rich), 0x00B1, 3, 3)
                snapshot = _windows_uia_field(self.rich, 100)
                self.assertEqual((snapshot.caret, snapshot.text), (3, "My draft"))
                self.api.SendMessageW(wt.HWND(self.rich), 0x00B1, 0, 2)
                self.assertFalse(snapshot.select())
                selected = _windows_uia_field(self.rich, 100)
                self.assertEqual(selected.text, "My")
                self.assertTrue(selected.select())

                class TextOnlyEditor:
                    CurrentControlType = api.UIA_DocumentControlTypeId
                    CurrentIsPassword = False
                    CurrentIsEnabled = True
                    GetRuntimeId = staticmethod(control.GetRuntimeId)
                    def GetCurrentPattern(self, pattern):
                        return None if pattern == api.UIA_ValuePatternId else control.GetCurrentPattern(pattern)
                with patch.object(OwnedFocus, "GetFocusedElement", return_value=TextOnlyEditor()), patch.object(
                        OwnedFocus, "CompareElements", return_value=True):
                    self.assertEqual(_windows_uia_field(self.rich, 100).text, "My")
                self.api.SendMessageW(wt.HWND(self.rich), 0x00B1, 3, 3)
                self.assertTrue(snapshot.select())
                self.assertEqual(self.api.SendMessageW(wt.HWND(self.rich), 0x00B0, 0, 0), 8 << 16)
