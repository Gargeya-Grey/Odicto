"""macOS consent: prompts, System Settings panes and the microphone probe.

Everything runs on fakes: a fake Objective-C runtime and fake system frameworks stand in for
ctypes, and ``subprocess.run`` is patched. Nothing here touches a real permission or opens a
real window, so the tests run on every platform.
"""
import ctypes
import ctypes.util
import io
import sys
import unittest
from contextlib import ExitStack
from unittest import mock

from platforms import macos, preflight
from platforms.preflight import Problem

ACCESSIBILITY = Problem("error", "macos_accessibility", "Accessibility permission is missing.")
INPUT_MONITORING = Problem("error", "macos_input_monitoring", "Input Monitoring permission is missing.")


class Fn:
    """A C function stand-in: callable, with the settable restype/argtypes of a ctypes function."""

    def __init__(self, behaviour):
        self.behaviour = behaviour
        self.calls = []

    def __call__(self, *args):
        self.calls.append(args)
        return self.behaviour(*args)


class Lib:
    def __init__(self, **functions):
        for name, behaviour in functions.items():
            setattr(self, name, Fn(behaviour if callable(behaviour) else (lambda *a, _v=behaviour: _v)))


def fake_objc(audio_status=macos.AUTH_NOT_DETERMINED, classes=("NSString", "NSNumber", "NSDictionary",
                                                              "AVCaptureDevice")):
    sent = []

    def msg(receiver, selector, *args):
        sent.append((receiver, selector, args))
        if selector == b"sel:stringWithUTF8String:":
            return ("NSString", args[0])
        if selector == b"sel:numberWithBool:":
            return ("NSNumber", args[0])
        if selector == b"sel:dictionaryWithObject:forKey:":
            return ("NSDictionary", args[0], args[1])
        if selector == b"sel:authorizationStatusForMediaType:":
            return audio_status
        return None

    lib = Lib(
        objc_getClass=lambda name: b"class:" + name if name.decode() in classes else None,
        sel_registerName=lambda name: b"sel:" + name,
        objc_msgSend=msg,
    )
    lib.sent = sent
    return lib


class World:
    """One consistent fake macOS: frameworks, a permission state and a record of what ran."""

    def __init__(self, trusted=True, hid=0, audio_status=macos.AUTH_NOT_DETERMINED, classes=None):
        self.objc = fake_objc(audio_status, **({"classes": classes} if classes is not None else {}))
        self.services = Lib(AXIsProcessTrusted=trusted, AXIsProcessTrustedWithOptions=trusted)
        self.iokit = Lib(IOHIDCheckAccess=hid, IOHIDRequestAccess=False)
        self.opened = []

    def load(self, name):
        name = str(name)
        if "objc" in name:
            return self.objc
        if "ApplicationServices" in name:
            return self.services
        if "IOKit" in name:
            return self.iokit
        return Lib()  # Foundation and AVFoundation only need to load

    def run(self, command, **kwargs):
        self.opened.append(command)
        return mock.Mock(returncode=0)

    def patches(self, stack):
        stack.enter_context(mock.patch.object(sys, "platform", "darwin"))
        stack.enter_context(mock.patch.object(ctypes.cdll, "LoadLibrary", self.load))
        stack.enter_context(mock.patch.object(ctypes.util, "find_library", lambda name: None))
        stack.enter_context(mock.patch.object(macos.subprocess, "run", self.run))
        return self

    @property
    def prompted(self):
        return len(self.services.AXIsProcessTrustedWithOptions.calls), len(self.iokit.IOHIDRequestAccess.calls)


def world(**kwargs):
    stack = ExitStack()
    result = World(**kwargs).patches(stack)
    return stack, result


PANE = "x-apple.systempreferences:com.apple.preference.security?"


class ConsentGuideTests(unittest.TestCase):
    def guide(self, problems, **kwargs):
        lines = []
        stack, fake = world(**kwargs)
        with stack:
            acted = preflight.guide_to_consent(problems, out=lines.append)
        return acted, lines, fake

    def test_missing_accessibility_asks_for_the_prompt_and_opens_the_pane(self):
        acted, lines, fake = self.guide([ACCESSIBILITY], trusted=False)
        self.assertEqual(acted, ["macos_accessibility"])
        (options,) = fake.services.AXIsProcessTrustedWithOptions.calls[0]
        self.assertEqual(options, ("NSDictionary", ("NSNumber", 1), ("NSString", b"AXTrustedCheckOptionPrompt")))
        self.assertEqual(fake.prompted, (1, 0))
        self.assertEqual(fake.opened, [["open", PANE + "Privacy_Accessibility"]])
        self.assertEqual(len(lines), 1)
        for text in ("Accessibility", "Turn on the switch", "start Odicto again"):
            self.assertIn(text, lines[0])

    def test_missing_input_monitoring_asks_for_listen_access_and_opens_its_pane(self):
        acted, lines, fake = self.guide([INPUT_MONITORING], hid=2)
        self.assertEqual(acted, ["macos_input_monitoring"])
        self.assertEqual(fake.iokit.IOHIDRequestAccess.calls, [(1,)])
        self.assertEqual(fake.prompted, (0, 1))
        self.assertEqual(fake.opened, [["open", PANE + "Privacy_ListenEvent"]])
        self.assertIn("Input Monitoring", lines[0])

    def test_both_missing_handles_each_once(self):
        acted, lines, fake = self.guide([ACCESSIBILITY, INPUT_MONITORING], trusted=False, hid=1)
        self.assertEqual(acted, ["macos_accessibility", "macos_input_monitoring"])
        self.assertEqual(fake.prompted, (1, 1))
        self.assertEqual([c[1] for c in fake.opened],
                         [PANE + "Privacy_Accessibility", PANE + "Privacy_ListenEvent"])
        self.assertEqual(len(lines), 2)

    def test_the_message_names_the_program_to_switch_on(self):
        _, lines, _ = self.guide([ACCESSIBILITY], trusted=False)
        self.assertIn("terminal app", lines[0])
        with mock.patch.object(macos.os.path, "realpath", return_value="/x/bin/python3.12"):
            _, lines, _ = self.guide([ACCESSIBILITY], trusted=False)
        self.assertIn('"python3.12"', lines[0])

    def test_nothing_runs_when_nothing_is_missing(self):
        acted, lines, fake = self.guide([])
        self.assertEqual((acted, lines, fake.opened, fake.prompted), ([], [], [], (0, 0)))
        warning = Problem("warning", "macos_microphone", "off")
        acted, lines, fake = self.guide([warning])
        self.assertEqual((acted, lines, fake.opened, fake.prompted), ([], [], [], (0, 0)))

    def test_granted_permissions_give_no_problem_and_so_no_prompt(self):
        stack, fake = world(trusted=True, hid=0)
        with stack:
            problems = preflight.environment_problems()
            self.assertEqual(problems, [])
            preflight.guide_to_consent(problems, out=lambda line: None)
        self.assertEqual((fake.opened, fake.prompted), ([], (0, 0)))

    def test_checking_the_environment_alone_never_prompts_or_opens_a_window(self):
        stack, fake = world(trusted=False, hid=2)
        with stack:
            found = {p.code for p in preflight.environment_problems()}
        self.assertEqual(found, {"macos_accessibility", "macos_input_monitoring"})
        self.assertEqual((fake.opened, fake.prompted), ([], (0, 0)))

    def test_only_macos_is_guided(self):
        stack, fake = world(trusted=False)
        with stack, mock.patch.object(sys, "platform", "linux"):
            self.assertEqual(preflight.guide_to_consent([ACCESSIBILITY], out=lambda line: None), [])
        self.assertEqual((fake.opened, fake.prompted), ([], (0, 0)))


class NeverRaisesTests(unittest.TestCase):
    def test_every_low_level_call_survives_a_missing_framework(self):
        boom = mock.Mock(side_effect=OSError("no such framework"))
        with mock.patch.object(ctypes.cdll, "LoadLibrary", boom):
            self.assertIsNone(macos.audio_authorization_status())
            self.assertFalse(macos.request_accessibility_prompt())
            self.assertFalse(macos.request_input_monitoring_prompt())

    def test_a_failing_open_command_is_false_not_an_error(self):
        with mock.patch.object(macos.subprocess, "run", side_effect=FileNotFoundError("open")):
            self.assertFalse(macos.open_privacy_pane("accessibility"))
        with mock.patch.object(macos.subprocess, "run", return_value=mock.Mock(returncode=1)):
            self.assertFalse(macos.open_privacy_pane("accessibility"))

    def test_exceptions_in_the_prompts_still_reach_the_message(self):
        lines = []
        with mock.patch.object(sys, "platform", "darwin"), \
                mock.patch.object(macos, "request_accessibility_prompt", side_effect=RuntimeError("x")), \
                mock.patch.object(macos, "request_input_monitoring_prompt", side_effect=RuntimeError("y")), \
                mock.patch.object(macos, "open_privacy_pane", side_effect=RuntimeError("z")):
            acted = preflight.guide_to_consent([ACCESSIBILITY, INPUT_MONITORING], out=lines.append)
        self.assertEqual(acted, ["macos_accessibility", "macos_input_monitoring"])
        self.assertEqual(len(lines), 2)
        self.assertTrue(all("Open System Settings" in line for line in lines))  # the pane did not open

    def test_a_failing_output_function_does_not_raise(self):
        with mock.patch.object(sys, "platform", "darwin"), \
                mock.patch.object(macos, "request_accessibility_prompt", return_value=False), \
                mock.patch.object(macos, "open_privacy_pane", return_value=True):
            preflight.guide_to_consent([ACCESSIBILITY], out=mock.Mock(side_effect=OSError("closed")))

    def test_the_exit_message_and_code_hold_when_the_guide_raises(self):
        import main

        err = io.StringIO()
        with mock.patch.object(sys, "stderr", err), \
                mock.patch("platforms.preflight.guide_to_consent", side_effect=RuntimeError("boom")):
            with self.assertRaises(SystemExit) as caught:
                main.stop_for_startup_blockers([ACCESSIBILITY])
        self.assertEqual(caught.exception.code, 2)
        self.assertIn("Cannot start Odicto [macos_accessibility]", err.getvalue())

    def test_the_message_prints_before_the_guide_runs_and_the_guide_gets_the_blockers(self):
        import main

        order = []
        err = io.StringIO()

        def guide(blockers):
            order.append(("guide", err.getvalue()))
            return []

        with mock.patch.object(sys, "stderr", err), \
                mock.patch("platforms.preflight.guide_to_consent", side_effect=guide) as called:
            with self.assertRaises(SystemExit) as caught:
                main.stop_for_startup_blockers([ACCESSIBILITY, INPUT_MONITORING])
        self.assertEqual(caught.exception.code, 2)
        self.assertEqual(called.call_args.args[0], [ACCESSIBILITY, INPUT_MONITORING])
        self.assertIn("[macos_input_monitoring]", order[0][1])


class FakeRuntimeTests(unittest.TestCase):
    def test_the_microphone_status_is_read_with_the_audio_media_type(self):
        stack, fake = world(audio_status=macos.AUTH_DENIED)
        with stack:
            self.assertEqual(macos.audio_authorization_status(), macos.AUTH_DENIED)
        receiver, selector, args = fake.objc.sent[-1]
        self.assertEqual((receiver, selector, args),
                         (b"class:AVCaptureDevice", b"sel:authorizationStatusForMediaType:",
                          (("NSString", b"soun"),)))

    def test_a_missing_class_is_unknown(self):
        stack, _ = world(classes=("NSString",))
        with stack:
            self.assertIsNone(macos.audio_authorization_status())

    def test_the_pane_urls(self):
        self.assertEqual(macos.privacy_pane_url("microphone"), PANE + "Privacy_Microphone")
        self.assertEqual(macos.privacy_pane_url("accessibility"), PANE + "Privacy_Accessibility")
        self.assertEqual(macos.privacy_pane_url("input_monitoring"), PANE + "Privacy_ListenEvent")


class MicrophoneProbeTests(unittest.TestCase):
    def probe(self, status):
        with mock.patch.object(macos, "audio_authorization_status", return_value=status):
            return preflight._macos_microphone()

    def test_denied_warns_and_says_where_to_turn_it_on(self):
        (problem,) = self.probe(macos.AUTH_DENIED)
        self.assertEqual((problem.severity, problem.code), ("warning", "macos_microphone"))
        for text in ("Privacy & Security > Microphone", "start Odicto again"):
            self.assertIn(text, problem.message)

    def test_restricted_warns_and_names_the_system_rule(self):
        (problem,) = self.probe(macos.AUTH_RESTRICTED)
        self.assertEqual((problem.severity, problem.code), ("warning", "macos_microphone"))
        self.assertIn("system rule", problem.message)

    def test_authorized_not_determined_and_unknown_say_nothing(self):
        for status in (macos.AUTH_AUTHORIZED, macos.AUTH_NOT_DETERMINED, None, 99):
            with self.subTest(status=status):
                self.assertEqual(self.probe(status), [])

    def test_through_the_whole_preflight_a_denied_microphone_is_only_a_warning(self):
        stack, _ = world(trusted=True, hid=0, audio_status=macos.AUTH_DENIED)
        with stack:
            problems = preflight.environment_problems()
        self.assertEqual([(p.severity, p.code) for p in problems], [("warning", "macos_microphone")])

    def test_a_microphone_probe_that_raises_does_not_hide_the_others(self):
        stack, _ = world(trusted=False, hid=0)
        with stack, mock.patch.object(macos, "audio_authorization_status", side_effect=OSError("boom")):
            self.assertEqual({p.code for p in preflight.environment_problems()}, {"macos_accessibility"})

    def test_a_denied_microphone_never_blocks_the_start(self):
        stack, _ = world(trusted=True, hid=0, audio_status=macos.AUTH_DENIED)
        with stack:
            blockers = [p for p in preflight.environment_problems() if p.severity == "error"]
        self.assertEqual(blockers, [])


if __name__ == "__main__":
    unittest.main()
