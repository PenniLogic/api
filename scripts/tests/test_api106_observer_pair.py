"""Mocked API106 ownership/control boundaries, never a real shell or discovery child."""

from contextlib import contextmanager
import ctypes
import hashlib
import importlib.util
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import Mock, call, patch


SOURCE = Path(__file__).resolve().parents[1] / "api106_observer_pair.py"
SPEC = importlib.util.spec_from_file_location("api106_supervisor_synthetic", SOURCE)
pair = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(pair)
DIGEST = b"a" * 32


def synthetic_cells(native=None):
    storage = [0]
    native = native if native is not None else Mock(uncertain=False)
    parent, child = pair.DeadlineCell(native), pair.DeadlineCell(native, publisher=True)

    def atomic(value, expected):
        previous = storage[0]
        if previous == expected:
            storage[0] = value
        return previous
    for cell in (parent, child):
        cell.handle, cell.mutex, cell.address = 90, 91, 8192
        cell.compare_exchange = Mock(side_effect=atomic)
    parent.create = Mock()
    return parent, child, storage


class SyntheticOnly(unittest.TestCase):
    def setUp(self):
        self.now = 100.0
        self.enterContext(patch.object(pair, "time", SimpleNamespace(monotonic=lambda: self.now)))
        self.popen = self.enterContext(patch.object(
            subprocess, "Popen", side_effect=AssertionError("No real process in API106 synthetic controls"),
        ))
        self.create_process = None
        self.win_dll = None
        if os.name == "nt":
            import _winapi
            self.create_process = self.enterContext(patch.object(
                _winapi, "CreateProcess", side_effect=AssertionError("No native process in API106 synthetic controls"),
            ))
            self.win_dll = self.enterContext(patch.object(
                ctypes, "WinDLL", side_effect=AssertionError("No native kernel boundary in API106 synthetic controls"),
            ))

    def tearDown(self):
        self.popen.assert_not_called()
        if self.create_process is not None:
            self.create_process.assert_not_called()
            self.win_dll.assert_not_called()


class ObserverPairProtocolStateTest(SyntheticOnly):
    def setUp(self):
        super().setUp()
        self.protocol = pair.Protocol(DIGEST)

    def send(self, kind, epoch=0.0, value=0):
        return self.protocol.accept(pair.packet(kind, epoch, value, DIGEST))

    def arm(self):
        self.send(pair.READY, value=3)
        self.send(pair.ARM, epoch=100)

    def test_one_absolute_epoch_covers_admission_body_report_and_case_closure(self):
        self.arm()
        self.assertEqual((self.protocol.window.started, self.protocol.window.work_deadline,
                          self.protocol.window.deadline), (100, 148, 160))
        self.now = 107
        self.send(pair.ENTER, epoch=100)
        self.now = 142
        self.send(pair.LEAVE, epoch=100, value=1)
        self.now = 147
        self.send(pair.DONE, epoch=100)
        self.assertIsNotNone(self.protocol.window)
        self.protocol.close_case()
        self.assertIsNone(self.protocol.window)
        self.send(pair.SUITE_FAILED, value=3)
        self.assertEqual(self.protocol.phase, "finished")

    def test_delayed_arm_message_does_not_restart_the_clock(self):
        self.send(pair.READY, value=1)
        self.now = 149
        with self.assertRaises(pair.EnclosureExpired):
            self.send(pair.ARM, epoch=100)
        self.assertEqual(self.protocol.window.deadline, 160)

    def test_changed_epoch_repeated_arm_and_out_of_order_messages_refuse(self):
        self.arm()
        for kind, epoch, value in ((pair.ARM, 100, 0), (pair.DONE, 100, 0), (pair.ENTER, 101, 0),
                                   (pair.SUITE_OK, 0, 3)):
            with self.subTest(kind=kind, epoch=epoch), self.assertRaises(pair.Refused):
                self.send(kind, epoch, value)
        self.send(pair.ENTER, epoch=100)
        with self.assertRaises(pair.Refused):
            self.send(pair.ENTER, epoch=100)

    def test_work_limit_applies_to_leave_and_close_not_only_arm_calls(self):
        for phase in ("leave", "close"):
            with self.subTest(phase=phase):
                self.now = 100
                self.protocol = pair.Protocol(DIGEST)
                self.arm()
                self.send(pair.ENTER, epoch=100)
                if phase == "close":
                    self.send(pair.LEAVE, epoch=100)
                    self.send(pair.DONE, epoch=100)
                self.now = 148.01
                with self.assertRaises(pair.EnclosureExpired):
                    if phase == "close":
                        self.protocol.close_case()
                    else:
                        self.send(pair.LEAVE, epoch=100)
                self.assertEqual(self.protocol.window.deadline, 160)

    def test_bad_fixed_frames_and_nonfinite_epochs_never_enter_the_state_machine(self):
        good = pair.packet(pair.READY, value=1, digest=DIGEST)
        invalid = (b"", good[:-1], good + b"x", b"x" + good[1:],
                   pair.FRAME.pack(pair.MAGIC, pair.READY, float("nan"), 1, DIGEST),
                   pair.FRAME.pack(pair.MAGIC, pair.READY, float("inf"), 1, DIGEST),
                   pair.packet(pair.READY, value=1, digest=b"b" * 32))
        for data in invalid:
            with self.subTest(size=len(data)), self.assertRaises(pair.Refused):
                self.protocol.accept(data)
        self.assertEqual(self.protocol.phase, "discovery")

    def test_success_requires_actual_discovered_count_but_failure_is_not_forged_into_success(self):
        self.arm()
        for kind in (pair.ENTER, pair.LEAVE, pair.DONE):
            self.send(kind, epoch=100)
        self.protocol.close_case()
        with self.assertRaises(pair.Refused):
            self.send(pair.SUITE_OK, value=2)
        self.send(pair.SUITE_FAILED, value=2)
        self.assertEqual(self.protocol.ran, 2)


class ObserverPairDeadlineCellTest(SyntheticOnly):
    def setUp(self):
        super().setUp()
        self.parent, self.child, self.storage = synthetic_cells()

    def mutex_cell(self, *, publisher=False):
        word = ctypes.c_int64(0)
        kernel = SimpleNamespace(WaitForSingleObject=Mock(return_value=0), ReleaseMutex=Mock(return_value=1))
        native = SimpleNamespace(kernel=kernel, release=Mock(), uncertain=False)
        cell = pair.DeadlineCell(native, publisher=publisher)
        cell.address, cell.handle, cell.mutex = ctypes.addressof(word), 90, 91
        return cell, native, word

    def test_atomic_publication_and_closed_tombstone_bind_one_exact_epoch(self):
        self.assertIsNone(self.parent.observe())
        window = self.child.publish()
        self.child.compare_exchange.assert_called_once_with(self.storage[0], 0)
        self.assertEqual((window.started, window.work_deadline, window.deadline), (100, 148, 160))
        self.assertEqual(self.parent.observe().started, window.started)
        self.parent.begin_close()
        self.assertFalse(self.parent.accept_close())
        self.child.close_case()
        self.assertLess(self.storage[0], 0)
        self.assertTrue(self.parent.accept_close())
        self.assertIsNone(self.parent.window)
        self.now = 10000
        self.assertIsNone(self.parent.observe())
        with self.assertRaisesRegex(pair.Refused, "cell-reused"):
            self.child.publish()
        for value in (0, -self.storage[0]):
            self.storage[0] = value
            with self.assertRaisesRegex(pair.Refused, "cell-rearmed"):
                self.parent.observe()

    def test_publication_latency_retains_sampled_epoch_instead_of_granting_more_time(self):
        original = self.child.compare_exchange.side_effect

        def publish(value, expected):
            self.now = 120
            return original(value, expected)
        self.child.compare_exchange.side_effect = publish
        self.assertEqual(self.child.publish().started, 100)
        self.assertEqual(self.parent.observe().deadline, 160)

    def test_prior_discovery_has_no_case_deadline_and_cannot_close_an_unused_cell(self):
        self.now = 10000
        self.assertIsNone(self.parent.observe())
        for operation in (self.child.close_case, self.parent.begin_close, self.parent.accept_close):
            with self.assertRaises(pair.Refused):
                operation()
        self.assertEqual(self.storage, [0])

    def test_changed_erased_nonfinite_and_prematurely_closed_cells_refuse(self):
        self.child.publish()
        self.parent.observe()
        initial = self.storage[0]
        for value in (0, -initial, initial + 1, -9223372036854775808):
            with self.subTest(value=value):
                self.storage[0] = value
                with self.assertRaises(pair.Refused):
                    self.parent.observe()
        for value in (float("nan"), float("inf"), 101):
            parent, child, storage = synthetic_cells()
            storage[0], = pair.struct.unpack("<q", pair.struct.pack("<d", value))
            with self.subTest(value=repr(value)), self.assertRaises(pair.Refused):
                parent.observe()
        for value in ("invalid", True, 2 ** 63, -(2 ** 63)):
            parent, child, storage = synthetic_cells()
            storage[0] = value
            with self.subTest(value=repr(value)), self.assertRaises(pair.Refused):
                parent.observe()

    def test_late_closed_tombstone_cannot_disarm_work_enforcement(self):
        self.child.publish()
        self.parent.begin_close()
        self.child.close_case()
        self.now = 149
        with self.assertRaises(pair.EnclosureExpired):
            self.parent.accept_close()
        self.assertEqual(self.parent.window.deadline, 160)
        self.assertFalse(self.parent.closed)

    def test_mapping_is_unnamed_fixed_eight_bytes_and_uses_atomic_aligned_word(self):
        word = ctypes.c_int64()
        address = ctypes.addressof(word)
        kernel = SimpleNamespace(
            CreateFileMappingW=Mock(return_value=90), MapViewOfFile=Mock(return_value=address),
            CreateMutexW=Mock(return_value=91), WaitForSingleObject=Mock(return_value=0),
            ReleaseMutex=Mock(return_value=1), UnmapViewOfFile=Mock(return_value=1),
        )
        native = SimpleNamespace(kernel=kernel, release=Mock(), uncertain=False)
        cell = pair.DeadlineCell(native)
        cell.create()
        creation = kernel.CreateFileMappingW.call_args.args
        self.assertEqual(creation[0].value, pair.wintypes.HANDLE(-1).value)
        self.assertEqual(creation[1:], (None, 4, 0, 8, None))
        kernel.MapViewOfFile.assert_called_once_with(90, 6, 0, 0, 8)
        self.assertEqual(cell.address % 8, 0)
        kernel.CreateMutexW.assert_called_once_with(None, False, None)
        kernel.WaitForSingleObject.assert_called_once_with(91, 0)
        kernel.ReleaseMutex.assert_called_once_with(91)
        self.assertEqual(cell.compare_exchange(37, 0), 0)
        self.assertEqual(cell.compare_exchange(38, 0), 37)
        self.assertEqual(cell.read(), 37)
        self.assertEqual(word.value, 37)
        self.assertEqual(kernel.WaitForSingleObject.call_args_list, [call(91, 0)] * 4)
        self.assertEqual(kernel.ReleaseMutex.call_args_list, [call(91)] * 4)
        cell.close()
        kernel.UnmapViewOfFile.assert_called_once_with(address)
        self.assertEqual(native.release.call_args_list, [call(90), call(91)])

    def test_mapping_close_cancellation_attempts_handle_once_and_remains_uncertain(self):
        primary = KeyboardInterrupt()
        native = SimpleNamespace(kernel=SimpleNamespace(UnmapViewOfFile=Mock(side_effect=primary)),
                                 release=Mock(), uncertain=False)
        cell = pair.DeadlineCell(native)
        cell.address, cell.handle, cell.mutex = 8192, 90, 91
        with self.assertRaises(KeyboardInterrupt) as observed:
            cell.close()
        self.assertIs(observed.exception, primary)
        self.assertTrue(native.uncertain)
        self.assertEqual(native.release.call_args_list, [call(90), call(91)])
        self.assertIsNone(cell.handle)
        self.assertIsNone(cell.mutex)
        self.assertIsNone(cell.address)

    def test_mapping_unmap_and_handle_failures_keep_first_error_without_retry(self):
        first, second = OSError("synthetic unmap"), SystemExit(31)
        native = SimpleNamespace(kernel=SimpleNamespace(UnmapViewOfFile=Mock(side_effect=first)),
                                 release=Mock(side_effect=second), uncertain=False)
        cell = pair.DeadlineCell(native)
        cell.address, cell.handle, cell.mutex = 8192, 90, 91
        with self.assertRaises(OSError) as observed:
            cell.close()
        self.assertIs(observed.exception, first)
        self.assertTrue(native.uncertain)
        self.assertIn("also unconfirmed", first.__notes__[0])
        native.kernel.UnmapViewOfFile.assert_called_once_with(8192)
        self.assertEqual(native.release.call_args_list, [call(90), call(91)])

    def test_invalid_mapping_cannot_be_used_for_atomic_access(self):
        native = SimpleNamespace(kernel=SimpleNamespace(
            MapViewOfFile=Mock(return_value=0), WaitForSingleObject=Mock(),
        ), uncertain=False)
        cell = pair.DeadlineCell(native)
        cell.handle = 90
        with self.assertRaises(pair.Refused):
            cell.map()
        with self.assertRaises(pair.Refused):
            cell.read()
        native.kernel.WaitForSingleObject.assert_not_called()

    def test_unavailable_or_abandoned_mutex_never_consumes_or_changes_shared_word(self):
        for status in (0x80, 0x102, 0xffffffff, 3, False):
            with self.subTest(status=status):
                cell, native, word = self.mutex_cell()
                word.value = 37
                native.kernel.WaitForSingleObject.return_value = status
                with self.assertRaises(pair.Refused):
                    cell.compare_exchange(38, 37)
                self.assertEqual(word.value, 37)
                self.assertTrue(native.uncertain)
                native.kernel.WaitForSingleObject.assert_called_once_with(91, 0)
                self.assertEqual(native.kernel.ReleaseMutex.call_args_list,
                                 [call(91)] if type(status) is int and status == 0x80 else [])
                with self.assertRaisesRegex(pair.Refused, "ownership-unconfirmed"):
                    cell.read()
                native.kernel.WaitForSingleObject.assert_called_once_with(91, 0)

    def test_mutex_acquisition_cancellation_is_primary_uncertain_and_not_retried(self):
        for primary in (KeyboardInterrupt(), SystemExit(41), OSError("synthetic mutex wait")):
            with self.subTest(primary=type(primary).__name__):
                cell, native, word = self.mutex_cell()
                native.kernel.WaitForSingleObject.side_effect = primary
                with self.assertRaises(BaseException) as observed:
                    cell.read()
                self.assertIs(observed.exception, primary)
                self.assertTrue(native.uncertain)
                self.assertEqual(word.value, 0)
                native.kernel.WaitForSingleObject.assert_called_once_with(91, 0)
                native.kernel.ReleaseMutex.assert_not_called()

    def test_mutex_release_failure_retains_sampled_window_and_prohibits_retry(self):
        for failure in (0, KeyboardInterrupt(), SystemExit(43), ValueError("synthetic release"),
                        OSError("synthetic release")):
            with self.subTest(failure=type(failure).__name__):
                cell, native, word = self.mutex_cell(publisher=True)
                if failure == 0:
                    native.kernel.ReleaseMutex.return_value = 0
                else:
                    native.kernel.ReleaseMutex.side_effect = failure
                with self.assertRaises(BaseException) as observed:
                    cell.publish()
                if failure == 0:
                    self.assertIsInstance(observed.exception, pair.Refused)
                else:
                    self.assertIs(observed.exception, failure)
                self.assertTrue(native.uncertain)
                self.assertEqual((cell.window.started, cell.window.work_deadline, cell.window.deadline),
                                 (100, 148, 160))
                self.assertEqual(word.value, cell.token)
                self.now = 101
                with self.assertRaisesRegex(pair.Refused, "cell-reused"):
                    cell.publish()
                with self.assertRaisesRegex(pair.Refused, "ownership-unconfirmed"):
                    cell.read()
                native.kernel.WaitForSingleObject.assert_called_once_with(91, 0)
                native.kernel.ReleaseMutex.assert_called_once_with(91)
                self.now = 100

    def test_word_failure_keeps_original_exception_when_mutex_release_is_cancelled(self):
        for existing_cause in (None, OSError("synthetic earlier cause")):
            cell, native, _ = self.mutex_cell()
            primary, secondary = ValueError("synthetic word access"), KeyboardInterrupt()
            primary.__cause__ = existing_cause
            native.kernel.ReleaseMutex.side_effect = secondary
            with patch.object(pair.ctypes, "c_int64", SimpleNamespace(from_address=Mock(side_effect=primary))):
                with self.assertRaises(ValueError) as observed:
                    cell.read()
            self.assertIs(observed.exception, primary)
            self.assertIs(primary.__cause__, secondary if existing_cause is None else existing_cause)
            self.assertTrue(native.uncertain)
            self.assertIn("also unconfirmed", primary.__notes__[0])
            native.kernel.WaitForSingleObject.assert_called_once_with(91, 0)
            native.kernel.ReleaseMutex.assert_called_once_with(91)

    def test_partial_cell_acquisition_retains_each_successful_handle_until_owner_cleanup(self):
        for failure_at in ("mapping", "mutex", "view"):
            with self.subTest(failure_at=failure_at):
                kernel = SimpleNamespace(
                    CreateFileMappingW=Mock(return_value=None if failure_at == "mapping" else 90),
                    CreateMutexW=Mock(return_value=None if failure_at == "mutex" else 91),
                    MapViewOfFile=Mock(return_value=None), UnmapViewOfFile=Mock(),
                )
                native = SimpleNamespace(kernel=kernel, release=Mock(), drain=Mock(), uncertain=False)
                with self.assertRaises(pair.Refused):
                    with pair.DiscoveryOwner(native) as owner:
                        owner.cell = pair.DeadlineCell(native)
                        owner.cell.create()
                expected = [] if failure_at == "mapping" else [call(90)]
                if failure_at == "view":
                    expected.append(call(91))
                self.assertEqual(native.release.call_args_list, expected)
                self.assertIsNone(owner.cell.handle)
                self.assertIsNone(owner.cell.mutex)
                if failure_at == "mapping":
                    kernel.CreateMutexW.assert_not_called()
                if failure_at != "view":
                    kernel.MapViewOfFile.assert_not_called()
                kernel.UnmapViewOfFile.assert_not_called()
                native.drain.assert_called_once_with(110)

    def test_protected_epoch_is_immutable_through_done_ack_and_one_closed_transition(self):
        child, native, word = self.mutex_cell(publisher=True)
        parent = pair.DeadlineCell(native)
        parent.handle, parent.mutex, parent.address = child.handle, child.mutex, child.address
        child.publish()
        token = word.value
        self.assertEqual(parent.observe().started, 100)
        parent.begin_close()
        self.assertFalse(parent.accept_close())
        child.close_case()
        self.assertEqual(word.value, -token)
        self.assertTrue(parent.accept_close())
        self.now = 1000
        self.assertIsNone(parent.observe())
        with self.assertRaises(pair.Refused):
            child.publish()
        self.assertEqual(native.kernel.WaitForSingleObject.call_args_list, [call(91, 0)] * 7)
        self.assertEqual(native.kernel.ReleaseMutex.call_args_list, [call(91)] * 7)


class ObserverPairAdmissionTest(SyntheticOnly):
    def setUp(self):
        super().setUp()
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        (self.root / "scripts" / "tests").mkdir(parents=True)
        for name in ("api106_observer_pair.py", "process_budget.py", "quality.py", "tests/test_money_mutation.py"):
            (self.root / "scripts" / name).write_bytes(b"# synthetic source binding only\n")
        self.python = self.root / "python.exe"
        self.python.write_bytes(b"synthetic executable bytes, never executed")
        self.environ = {"PATH": "unchanged-test-environment", "PYTHONIOENCODING": "utf-8"}
        self.enterContext(patch.object(pair, "ROOT", self.root))
        self.enterContext(patch.object(pair, "os", SimpleNamespace(
            name="nt", environ=self.environ, fstat=os.fstat, path=os.path,
        )))
        self.enterContext(patch.object(pair, "sys", SimpleNamespace(
            executable=str(self.python), version="synthetic CPython", version_info=(3, 14),
            implementation=SimpleNamespace(name="cpython"),
        )))

    def test_sources_python_and_environment_are_rebound_not_borrowed_from_infra(self):
        admission = pair.Admission()
        admission.verify()
        self.assertEqual(set(admission.snapshot), {"python", "version", "sources", "environment_sha256"})
        self.assertNotIn("Infra", str(admission.snapshot))
        command = admission.command(91, 92, 93)
        self.assertEqual(command[:2], [str(self.python), "-c"])
        self.assertEqual(command[-4:], ["91", "92", "93", admission.digest.hex()])
        self.assertIn("int(sys.argv[3]),bytes.fromhex(sys.argv[4])", command[2])
        self.assertNotIn("-I", command)
        self.assertNotIn("-S", command)
        self.assertNotIn("-B", command)
        self.assertIn("sys.path[0]=" + repr(str(self.root)), command[2])
        self.assertIn("api106-bootstrap-source-changed", command[2])
        self.assertNotIn("assert ", command[2])
        self.assertEqual(self.environ, {"PATH": "unchanged-test-environment", "PYTHONIOENCODING": "utf-8"})

    def test_each_tool_source_inventory_and_environment_drift_refuses(self):
        for kind in ("python", "source", "inventory", "environment"):
            with self.subTest(kind=kind):
                admission = pair.Admission()
                if kind == "python":
                    self.python.write_bytes(b"changed inert executable")
                elif kind == "source":
                    (self.root / "scripts" / "quality.py").write_bytes(b"# changed inert source\n")
                elif kind == "inventory":
                    (self.root / "scripts" / "tests" / "test_extra.py").write_bytes(b"# inert\n")
                else:
                    self.environ["PATH"] = "changed-but-never-used"
                with self.assertRaises(pair.Refused):
                    admission.verify()

    def test_unapproved_python_startup_configuration_refuses_without_modifying_it(self):
        for key in ("PYTHONPATH", "PythonHome", "PYTHONSTARTUP", "PYTHONUSERBASE", "PYTHONOPTIMIZE"):
            with self.subTest(key=key):
                self.environ[key] = "synthetic startup setting"
                before = dict(self.environ)
                with self.assertRaisesRegex(pair.Refused, "startup-environment"):
                    pair.Admission()
                self.assertEqual(self.environ, before)
                del self.environ[key]

    def test_setup_python_location_metadata_is_bound_but_not_treated_as_startup_configuration(self):
        self.environ.update({key: "existing-tool-location" for key in (
            "pythonLocation", "Python_ROOT_DIR", "Python2_ROOT_DIR", "Python3_ROOT_DIR",
        )})
        unchanged = dict(self.environ)
        admission = pair.Admission()
        admission.verify()
        self.assertEqual(self.environ, unchanged)
        self.environ["pythonLocation"] = "different-tool-location"
        with self.assertRaisesRegex(pair.Refused, "admission-changed"):
            admission.verify()

    def test_non_windows_or_non_api_interpreter_refuses_without_fallback(self):
        pair.os.name = "posix"
        with self.assertRaises(pair.Refused):
            pair.Admission()
        pair.os.name = "nt"
        pair.sys.version_info = (3, 13)
        with self.assertRaises(pair.Refused):
            pair.Admission()

    def test_symlink_or_changed_read_is_not_a_source_binding(self):
        with patch.object(Path, "is_symlink", return_value=True), self.assertRaises(pair.Refused):
            pair.Admission()
        admission = pair.Admission()
        with patch.object(pair.os.path, "samestat", return_value=False), self.assertRaises(pair.Refused):
            admission.verify()


class ObserverPairDiscoveryClientTest(SyntheticOnly):
    def setUp(self):
        super().setUp()
        self.admission = SimpleNamespace(digest=DIGEST, verify=Mock())
        self.channel = Mock()
        self.channel.fileno.return_value = 19
        self.sent, self.answer, self.events = [], None, []

        def transfer(handle, data=None, **kwargs):
            self.assertEqual(handle, 19)
            if data is not None:
                kind, epoch, value, digest = pair.unpack(data)
                self.sent.append((kind, epoch, value, kwargs.get("deadline")))
                self.answer = pair.packet(pair.ACK, epoch, kind, digest)
            else:
                return self.answer
        self.native = SimpleNamespace(io=Mock(side_effect=transfer))
        self.parent_cell, self.cell, self.cell_storage = synthetic_cells()
        self.client = pair.Client(self.native, self.channel, self.admission, self.cell)
        self.module = ModuleType("test_money_mutation")
        self.module.__file__ = str(pair.ROOT / "scripts" / "tests" / "test_money_mutation.py")
        self.module.APPROVED_SHELL_PAIR_ENCLOSURE = None
        self.enterContext(patch.dict(sys.modules, {"test_money_mutation": self.module}))
        method = pair.TARGET.rsplit(".", 1)[1]
        cls = type("ProcessBudgetTest", (unittest.TestCase,), {
            "__module__": "test_money_mutation", method: lambda case: None,
        })
        self.target = cls(method)
        self.suite = unittest.TestSuite([self.target])

    def test_installs_in_exact_discovered_module_and_returns_same_suite_and_case(self):
        self.assertIs(self.client.install(self.suite), self.suite)
        self.assertIs(self.client.target, self.target)
        self.assertEqual(self.module.APPROVED_SHELL_PAIR_ENCLOSURE, self.client.enclosure)
        self.assertEqual(self.sent, [(pair.READY, 0.0, 1, None)])
        self.admission.verify.assert_called_once_with()

    def test_zero_duplicate_wrong_module_and_preexisting_seam_refuse(self):
        for condition in ("absent", "duplicate", "module", "seam"):
            with self.subTest(condition=condition):
                suite = self.suite
                self.module.__file__ = str(pair.ROOT / "scripts" / "tests" / "test_money_mutation.py")
                self.module.APPROVED_SHELL_PAIR_ENCLOSURE = None
                if condition == "absent":
                    suite = unittest.TestSuite()
                elif condition == "duplicate":
                    suite = unittest.TestSuite([self.target, self.target])
                elif condition == "module":
                    self.module.__file__ = str(pair.ROOT / "scripts" / "wrong.py")
                else:
                    self.module.APPROVED_SHELL_PAIR_ENCLOSURE = object()
                with self.assertRaises(pair.Refused):
                    self.client.install(suite)
        self.assertEqual(self.sent, [])

    def test_acknowledgment_latency_does_not_restart_the_case_epoch(self):
        self.client.install(self.suite)
        original = self.native.io.side_effect

        def latency(*args, **kwargs):
            if len(args) > 1 and pair.unpack(args[1])[0] == pair.DONE:
                self.assertIsNone(self.module.APPROVED_SHELL_PAIR_ENCLOSURE)
            value = original(*args, **kwargs)
            if len(args) == 1:
                self.now += 3
            return value
        self.native.io.side_effect = latency
        self.client.start(self.target)
        with self.client.enclosure(seconds=60, cleanup_reserve=12) as window:
            self.assertEqual((window.started, window.work_deadline, window.deadline), (100, 148, 160))
        self.client.stop(self.target)
        self.assertTrue(all(epoch == 100 and deadline == 148 for _, epoch, _, deadline in self.sent[1:]))
        self.assertIsNone(self.module.APPROVED_SHELL_PAIR_ENCLOSURE)
        self.assertIsNone(self.client.window)

    def test_cell_publication_precedes_first_arm_io_and_closed_state_follows_done_ack(self):
        self.client.install(self.suite)
        original = self.native.io.side_effect

        def transfer(handle, data=None, **kwargs):
            if data is not None:
                kind, epoch, _, _ = pair.unpack(data)
                if kind in (pair.ARM, pair.ENTER, pair.LEAVE, pair.DONE):
                    self.assertGreater(self.cell_storage[0], 0)
                    self.assertEqual(self.cell.window.started, epoch)
            if data is None:
                self.assertFalse(self.cell.closed)
            return original(handle, data, **kwargs)
        self.native.io.side_effect = transfer
        self.client.start(self.target)
        with self.client.enclosure(seconds=60, cleanup_reserve=12):
            pass
        self.client.stop(self.target)
        self.assertLess(self.cell_storage[0], 0)
        self.assertTrue(self.cell.closed)
        self.assertIsNone(self.client.window)

    def test_original_error_object_survives_control_failure_chaining(self):
        self.client.install(self.suite)
        self.client.start(self.target)
        primary = AssertionError("synthetic-required-error")
        original = self.native.io.side_effect

        def fail_leave(handle, data=None, **kwargs):
            if data is not None and pair.unpack(data)[0] == pair.LEAVE:
                raise pair.Refused("synthetic-control-refusal")
            return original(handle, data, **kwargs)
        self.native.io.side_effect = fail_leave
        with self.assertRaises(AssertionError) as observed:
            with self.client.enclosure(seconds=60, cleanup_reserve=12):
                raise primary
        self.assertIs(observed.exception, primary)
        self.assertIsInstance(primary.__cause__, pair.Refused)
        self.assertFalse(self.client.left)

    def test_missing_pair_and_second_invocation_are_not_successful_case_closure(self):
        self.client.install(self.suite)
        self.client.start(self.target)
        with self.assertRaises(pair.Refused):
            self.client.stop(self.target)
        self.assertIsNone(self.module.APPROVED_SHELL_PAIR_ENCLOSURE)
        with self.client.enclosure(seconds=60, cleanup_reserve=12):
            pass
        with self.assertRaises(pair.Refused):
            with self.client.enclosure(seconds=60, cleanup_reserve=12):
                self.fail("repeated enclosure yielded")
        with self.assertRaises(pair.Refused):
            self.client.start(self.target)

    def test_unrelated_cases_cannot_arm_or_stop_the_target_window(self):
        self.client.install(self.suite)
        other = unittest.FunctionTestCase(lambda: None)
        self.client.start(other)
        self.client.stop(other)
        self.assertEqual(len(self.sent), 1)
        self.assertIsNone(self.client.window)

    def test_normal_runner_preserves_objects_order_fixtures_failure_and_accounting(self):
        primary, seen = AssertionError("synthetic-required-failure"), []
        method = pair.TARGET.rsplit(".", 1)[1]

        def target_body(case):
            self.events.append("target")
            case.addCleanup(self.events.append, "case-cleanup")
            with self.module.APPROVED_SHELL_PAIR_ENCLOSURE(seconds=60, cleanup_reserve=12):
                raise primary

        def before_case():
            self.events.append("before")
            self.now = 130
        setattr(type(self.target), method, target_body)
        setattr(type(self.target), "setUpClass", classmethod(lambda cls: self.events.append("class-setup")))
        original = unittest.TextTestResult.addFailure

        def observe(result, test, error):
            seen.append((test, error[1]))
            return original(result, test, error)
        before_case = unittest.FunctionTestCase(before_case)
        after_case = unittest.FunctionTestCase(lambda: self.events.append("after"))
        suite = unittest.TestSuite([before_case, self.target, after_case])
        expected_objects = list(suite)
        observed_program = {}

        def program(**kwargs):
            observed_program.update(kwargs)
            discovered = kwargs["testLoader"].discover("scripts/tests")
            self.assertIs(discovered, suite)
            self.assertEqual(list(discovered), expected_objects)
            result = kwargs["testRunner"](stream=io.StringIO(), verbosity=2).run(discovered)
            self.assertEqual(result.testsRun, 3)
            self.assertEqual(len(result.failures), 1)
            self.assertEqual(result.errors, [])
            return SimpleNamespace(result=result)
        with patch.object(unittest.TestLoader, "discover", return_value=suite) as discover, patch.object(
            unittest, "TestProgram", side_effect=program,
        ), patch.object(unittest.TextTestResult, "addFailure", observe):
            self.assertEqual(pair.run_discovery(self.client), 1)
        self.assertEqual(self.events, ["before", "class-setup", "target", "case-cleanup", "after"])
        self.assertEqual(seen, [(self.target, primary)])
        self.assertEqual(observed_program["argv"],
                         ["python -m unittest", "discover", "-s", "scripts/tests", "-v"])
        self.assertIsNone(observed_program["module"])
        self.assertIs(observed_program["exit"], False)
        discover.assert_called_once_with("scripts/tests")
        self.assertEqual(self.sent[1][0:2], (pair.ARM, 130))
        self.assertEqual([row[0] for row in self.sent],
                         [pair.READY, pair.ARM, pair.ENTER, pair.LEAVE, pair.DONE, pair.SUITE_FAILED])
        self.assertIsNone(self.module.APPROVED_SHELL_PAIR_ENCLOSURE)


class ObserverPairNativeBoundaryTest(SyntheticOnly):
    def setUp(self):
        super().setUp()
        self.native = pair.Native.__new__(pair.Native)
        self.native.pending, self.native.uncertain = [], False
        self.native.deadline_cell = None
        self.api = SimpleNamespace(
            ERROR_IO_PENDING=997, WAIT_TIMEOUT=258, WAIT_OBJECT_0=0, INFINITE=4294967295,
            STD_INPUT_HANDLE=-10, STD_OUTPUT_HANDLE=-11, STD_ERROR_HANDLE=-12,
            STARTF_USESTDHANDLES=256,
            GetStdHandle=Mock(side_effect=lambda n: abs(n)), CloseHandle=Mock(),
            CreateProcess=Mock(return_value=(1001, 1002, 1003, 1004)),
            WaitForMultipleObjects=Mock(return_value=0),
        )
        self.native.api = self.api
        self.native.kernel = SimpleNamespace(ResumeThread=Mock(return_value=1), GetProcessId=Mock(return_value=1003))
        self.process = pair.OwnedDiscovery(self.native)
        self.admission = SimpleNamespace(python=Path("C:\\synthetic\\python.exe"), verify=Mock(),
                                         command=Mock(return_value=["absolute-python", "-c", "inert", "201"]))
        self.child = Mock()
        self.child.fileno.return_value = 90
        self.cell = SimpleNamespace(handle=91, mutex=92)
        self.native.duplicate = Mock(side_effect=(201, 202, 203, 204, 205, 206))
        self.startup = SimpleNamespace(dwFlags=0)
        self.enterContext(patch.object(subprocess, "STARTUPINFO", return_value=self.startup, create=True))

    def test_binding_uses_documented_exports_without_fabricating_interlocked_intrinsic(self):
        exports = ("ResumeThread", "QueryFullProcessImageNameW", "GetProcessId", "CreateFileMappingW",
                   "MapViewOfFile", "UnmapViewOfFile", "CreateMutexW", "WaitForSingleObject", "ReleaseMutex")
        kernel = SimpleNamespace(**{name: Mock() for name in exports})
        with patch.dict(sys.modules, {"_winapi": self.api}), \
                patch.object(ctypes, "WinDLL", return_value=kernel, create=True) as loader:
            native = pair.Native()
        loader.assert_called_once_with("kernel32", use_last_error=True)
        self.assertIs(native.kernel, kernel)
        self.assertEqual((native.pending, native.uncertain, native.deadline_cell), ([], False, None))
        self.assertEqual(kernel.CreateMutexW.argtypes,
                         [ctypes.c_void_p, pair.wintypes.BOOL, pair.wintypes.LPCWSTR])
        self.assertIs(kernel.CreateMutexW.restype, pair.wintypes.HANDLE)
        self.assertEqual(kernel.WaitForSingleObject.argtypes, [pair.wintypes.HANDLE, pair.wintypes.DWORD])
        self.assertIs(kernel.WaitForSingleObject.restype, pair.wintypes.DWORD)
        self.assertEqual(kernel.ReleaseMutex.argtypes, [pair.wintypes.HANDLE])
        self.assertIs(kernel.ReleaseMutex.restype, pair.wintypes.BOOL)
        self.assertFalse(hasattr(kernel, "InterlockedCompareExchange64"))
        for name in exports:
            getattr(kernel, name).assert_not_called()

    def test_missing_documented_mutex_binding_refuses_without_allocating_or_falling_back(self):
        exports = ("ResumeThread", "QueryFullProcessImageNameW", "GetProcessId", "CreateFileMappingW",
                   "MapViewOfFile", "UnmapViewOfFile", "CreateMutexW", "WaitForSingleObject", "ReleaseMutex")
        for missing in ("CreateMutexW", "WaitForSingleObject", "ReleaseMutex"):
            with self.subTest(missing=missing):
                kernel = SimpleNamespace(**{name: Mock() for name in exports if name != missing})
                with patch.dict(sys.modules, {"_winapi": self.api}), \
                        patch.object(ctypes, "WinDLL", return_value=kernel, create=True) as loader:
                    with self.assertRaisesRegex(AttributeError, missing):
                        pair.Native()
                loader.assert_called_once_with("kernel32", use_last_error=True)
                for name in exports:
                    if name != missing:
                        getattr(kernel, name).assert_not_called()

    def test_creation_is_suspended_with_only_control_and_original_standard_handles(self):
        self.native.create(self.admission, self.child, self.process, self.cell)
        args = self.api.CreateProcess.call_args.args
        self.assertEqual(args[4:6], (True, 4))
        self.assertEqual(args[6], dict(os.environ))
        self.assertEqual(args[7], str(pair.ROOT))
        self.assertEqual(self.startup.lpAttributeList, {"handle_list": [201, 202, 203, 204, 205, 206]})
        self.assertEqual((self.startup.hStdInput, self.startup.hStdOutput, self.startup.hStdError), (204, 205, 206))
        self.assertEqual((self.process._handle, self.process.thread, self.process.pid), (1001, 1002, 1003))
        self.assertEqual(self.api.CloseHandle.call_args_list,
                         [call(201), call(202), call(203), call(204), call(205), call(206)])
        self.admission.command.assert_called_once_with(201, 202, 203)
        self.assertEqual(self.native.duplicate.call_args_list,
                         [call(90), call(91), call(92), call(10), call(11), call(12)])
        self.native.kernel.ResumeThread.assert_not_called()
        self.admission.verify.assert_called_once_with()

    def test_partial_duplication_failure_closes_already_created_handles(self):
        for prior in ([201, 202], [201, 202, 203]):
            with self.subTest(acquired=len(prior)):
                self.native.duplicate.side_effect = [*prior, OSError("synthetic duplication refused")]
                self.api.CloseHandle.reset_mock()
                with self.assertRaises(OSError):
                    self.native.create(self.admission, self.child, self.process, self.cell)
                self.api.CreateProcess.assert_not_called()
                self.assertEqual(self.api.CloseHandle.call_args_list, [call(handle) for handle in prior])

    def test_failed_creation_or_handle_close_never_loses_a_created_process_handle(self):
        self.api.CloseHandle.side_effect = OSError("synthetic close failure")
        with self.assertRaises(OSError):
            self.native.create(self.admission, self.child, self.process, self.cell)
        self.assertEqual(self.process._handle, 1001)
        self.assertEqual(self.api.CloseHandle.call_count, 6)
        self.assertTrue(self.native.uncertain)
        self.native.kernel.ResumeThread.assert_not_called()

    def test_duplicate_release_cancellation_attempts_all_and_preserves_creation_primary(self):
        for failed_creation in (False, True):
            for position in (0, 2, 3):
                for cancellation in (KeyboardInterrupt(), SystemExit(23), ValueError("synthetic close"),
                                     OSError("synthetic close")):
                    with self.subTest(failed_creation=failed_creation, position=position,
                                      cancellation=type(cancellation).__name__):
                        primary = OSError("synthetic creation refused") if failed_creation else cancellation
                        self.native.uncertain = False
                        self.process = pair.OwnedDiscovery(self.native)
                        self.native.duplicate.side_effect = (201, 202, 203, 204, 205, 206)
                        self.api.CreateProcess.side_effect = primary if failed_creation else None
                        self.api.CloseHandle.reset_mock()
                        effects = [None] * 6
                        effects[position] = cancellation
                        self.api.CloseHandle.side_effect = effects
                        with self.assertRaises(BaseException) as observed:
                            self.native.create(self.admission, self.child, self.process, self.cell)
                        self.assertIs(observed.exception, primary)
                        self.assertEqual(self.api.CloseHandle.call_args_list,
                                         [call(201), call(202), call(203), call(204), call(205), call(206)])
                        self.assertTrue(self.native.uncertain)
                        if failed_creation:
                            self.assertIs(primary.__cause__, cancellation)

    def test_live_child_without_arm_is_monitored_before_the_first_frame_arrives(self):
        self.now = 10
        window = SimpleNamespace(started=100.0, work_deadline=148.0, deadline=160.0)
        cell = SimpleNamespace(observe=Mock(side_effect=lambda: window if self.now >= 100 else None))
        self.native.deadline_cell = cell
        operation = Mock(event=300)
        self.api.ReadFile = Mock(return_value=(operation, 997))
        self.process._handle = 1001

        def wait(handles, all_handles, milliseconds):
            self.assertNotEqual(milliseconds, self.api.INFINITE, "Live pre-ARM wait has no independent deadline owner")
            self.assertEqual(handles, [300, 1001])
            self.now = 100 if self.now < 100 else 149
            return self.api.WAIT_TIMEOUT

        self.api.WaitForMultipleObjects.side_effect = wait
        with self.assertRaises(pair.EnclosureExpired):
            self.native.io(70, process=self.process)
        self.assertGreaterEqual(cell.observe.call_count, 3)
        self.assertEqual(self.native.pending, [operation])
        operation.GetOverlappedResult.assert_not_called()

    def test_never_delivered_arm_uses_published_total_deadline_in_the_single_owner(self):
        self.now = 10
        cell, publisher, _ = synthetic_cells(self.native)
        cell.close = Mock()
        self.native.deadline_cell = cell
        self.native.drain = Mock()
        self.process._handle = 1001
        self.process.kill, self.process.wait, self.process.close = Mock(), Mock(), Mock()
        job, channel, receipt = Mock(), Mock(), Mock()
        operation = Mock(event=300)
        self.api.ReadFile = Mock(return_value=(operation, 997))

        def wait(handles, all_handles, milliseconds):
            self.assertLessEqual(milliseconds, pair.CELL_POLL_MILLISECONDS)
            if self.now < 100:
                self.now = 100
                publisher.publish()
            else:
                self.now = 149
            return self.api.WAIT_TIMEOUT
        self.api.WaitForMultipleObjects.side_effect = wait
        with self.assertRaises(pair.EnclosureExpired):
            with pair.DiscoveryOwner(self.native) as owner:
                owner.job, owner.process, owner.channel, owner.receipt = job, self.process, channel, receipt
                owner.cell = cell
                self.native.io(70, process=self.process)
        self.assertIsNone(owner.protocol)
        self.assertEqual((owner.cleanup_deadline, job.cleanup_deadline), (160, 160))
        self.process.kill.assert_called_once_with()
        self.process.wait.assert_called_once_with(160)
        self.native.drain.assert_called_once_with(160)
        channel.close.assert_called_once_with()
        cell.close.assert_called_once_with()
        receipt.close.assert_called_once_with()
        operation.GetOverlappedResult.assert_not_called()

    def test_late_arm_io_is_refused_using_the_published_epoch_not_arrival_time(self):
        cell, publisher, _ = synthetic_cells(self.native)
        publisher.publish()
        self.native.deadline_cell = cell
        data = pair.packet(pair.ARM, 100, digest=DIGEST)
        operation = Mock(event=300)
        operation.GetOverlappedResult.return_value = (len(data), 0)
        operation.getbuffer.return_value = data
        self.api.ReadFile = Mock(return_value=(operation, 997))

        def wait(*args):
            self.now = 149
            return self.api.WAIT_OBJECT_0
        self.api.WaitForMultipleObjects.side_effect = wait
        with self.assertRaises(pair.EnclosureExpired):
            self.native.io(70, process=self.process)
        self.assertEqual(cell.window.deadline, 160)
        operation.GetOverlappedResult.assert_not_called()

    def test_long_preceding_suite_wait_has_no_global_sixty_second_deadline(self):
        cell, _, _ = synthetic_cells(self.native)
        self.native.deadline_cell = cell
        data = pair.packet(pair.READY, value=4, digest=DIGEST)
        operation = Mock(event=300)
        operation.GetOverlappedResult.return_value = (len(data), 0)
        operation.getbuffer.return_value = data
        self.api.ReadFile = Mock(return_value=(operation, 997))

        def wait(*args):
            self.now += 1000
            return self.api.WAIT_TIMEOUT if self.now == 1100 else self.api.WAIT_OBJECT_0
        self.api.WaitForMultipleObjects.side_effect = wait
        self.assertEqual(self.native.io(70, process=self.process), data)
        self.assertIsNone(cell.window)
        self.assertEqual(self.api.WaitForMultipleObjects.call_count, 2)

    def test_done_ack_waits_for_child_cell_close_without_resetting_the_deadline(self):
        cell, publisher, _ = synthetic_cells(self.native)
        publisher.publish()
        cell.begin_close()

        def wait(handle, milliseconds):
            self.assertEqual(milliseconds, pair.CELL_POLL_MILLISECONDS)
            self.now = 147
            publisher.close_case()
            return self.api.WAIT_TIMEOUT
        self.api.WaitForSingleObject = Mock(side_effect=wait)
        self.native.wait_case_close(cell, self.process)
        self.assertTrue(cell.closed)
        self.assertIsNone(cell.window)
        self.now = 1000
        self.native.deadline_cell = cell
        self.assertIsNone(self.native.io_deadline(None))

    def test_live_child_never_closing_the_cell_remains_subject_to_the_original_window(self):
        cell, publisher, _ = synthetic_cells(self.native)
        publisher.publish()
        cell.begin_close()

        def wait(*args):
            self.now = 149
            return self.api.WAIT_TIMEOUT
        self.api.WaitForSingleObject = Mock(side_effect=wait)
        with self.assertRaises(pair.EnclosureExpired):
            self.native.wait_case_close(cell, self.process)
        self.assertEqual(cell.window.deadline, 160)
        self.assertFalse(cell.closed)

    def test_acquisition_failure_is_primary_when_release_is_cancelled(self):
        for prior in ([201, 202], [201, 202, 203]):
            with self.subTest(acquired=len(prior)):
                primary, cancellation = OSError("synthetic acquisition refused"), SystemExit(29)
                self.native.uncertain = False
                self.native.duplicate.side_effect = [*prior, primary]
                self.api.CloseHandle.reset_mock()
                self.api.CloseHandle.side_effect = [cancellation, *([None] * (len(prior) - 1))]
                with self.assertRaises(BaseException) as observed:
                    self.native.create(self.admission, self.child, self.process, self.cell)
                self.assertIs(observed.exception, primary)
                self.assertIs(primary.__cause__, cancellation)
                self.assertEqual(self.api.CloseHandle.call_args_list, [call(handle) for handle in prior])
                self.assertTrue(self.native.uncertain)
                self.api.CreateProcess.assert_not_called()

    def test_duplicate_close_cancellation_keeps_outer_cleanup_and_never_claims_control_release(self):
        self.admission.digest, self.admission.paths = DIGEST, ("synthetic",)
        self.admission.snapshot = {"python": {"sha256": "a" * 64}}
        self.native.members, self.native.drain = Mock(), Mock()
        self.native.resume = Mock()
        for failed_creation in (False, True):
            for position in (0, 2, 3):
                with self.subTest(failed_creation=failed_creation, position=position):
                    cancellation = KeyboardInterrupt()
                    primary = OSError("synthetic create refused") if failed_creation else cancellation
                    self.native.uncertain = False
                    self.native.duplicate.side_effect = (201, 202, 203, 204, 205, 206)
                    self.native.drain.reset_mock()
                    self.process = pair.OwnedDiscovery(self.native)
                    self.process.kill, self.process.wait, self.process.close = Mock(), Mock(), Mock()
                    self.api.CreateProcess.side_effect = primary if failed_creation else None
                    self.api.CloseHandle.reset_mock()
                    effects = [None] * 6
                    effects[position] = cancellation
                    self.api.CloseHandle.side_effect = effects
                    job, channel, child, receipt = Mock(), Mock(), Mock(), Mock()
                    cell, _, _ = synthetic_cells(self.native)
                    cell.close = Mock()
                    self.admission.budget_module = Mock(return_value=SimpleNamespace(
                        WindowsJob=Mock(return_value=job),
                    ))
                    with patch.object(pair, "Admission", return_value=self.admission), \
                            patch.object(pair, "Native", return_value=self.native), \
                            patch.object(pair, "OwnedDiscovery", return_value=self.process), \
                            patch.object(pair, "DeadlineCell", return_value=cell), \
                            patch.object(pair, "Receipt", return_value=receipt), \
                            patch("multiprocessing.connection.Pipe", return_value=(channel, child)), \
                            patch.object(pair, "supervise") as supervise:
                        with self.assertRaises(BaseException) as observed:
                            pair.run_owning_suite()
                    self.assertIs(observed.exception, primary)
                    if failed_creation:
                        self.assertIs(primary.__cause__, cancellation)
                        self.assertIsInstance(primary.__context__, pair.Refused)
                    self.assertEqual(self.api.CloseHandle.call_args_list, [call(n) for n in range(201, 207)])
                    self.assertTrue(self.native.uncertain)
                    job.close.assert_called_once_with()
                    if not failed_creation:
                        self.process.kill.assert_called_once_with()
                        self.process.wait.assert_called_once_with(110)
                    self.native.drain.assert_called_once_with(110)
                    self.process.close.assert_called_once_with()
                    channel.close.assert_called_once_with()
                    child.close.assert_called_once_with()
                    cell.close.assert_called_once_with()
                    receipt.close.assert_called_once_with()
                    self.assertFalse(receipt.record.call_args.args[0]["owned_tree_and_control_released"])
                    supervise.assert_not_called()
        self.native.resume.assert_not_called()

    def test_mutex_contention_has_no_hidden_wait_or_fresh_cleanup_credit_before_or_after_arm(self):
        for known_epoch in (False, True):
            with self.subTest(known_epoch=known_epoch):
                self.native.uncertain = False
                word = ctypes.c_int64()
                cell = pair.DeadlineCell(self.native)
                cell.handle, cell.mutex, cell.address = 90, 91, ctypes.addressof(word)
                self.native.kernel = SimpleNamespace(
                    WaitForSingleObject=Mock(return_value=0x102), ReleaseMutex=Mock(),
                    UnmapViewOfFile=Mock(return_value=1),
                )
                if known_epoch:
                    cell.window = pair.Window(100)
                    cell.token, = pair.struct.unpack("<q", pair.struct.pack("<d", 100))
                    word.value = cell.token
                self.native.deadline_cell = cell
                self.native.drain = Mock()
                self.api.ReadFile = Mock()
                self.api.CloseHandle.reset_mock()
                job, channel, receipt = Mock(), Mock(), Mock()
                process = SimpleNamespace(_handle=1001, kill=Mock(), wait=Mock(), close=Mock())
                with self.assertRaisesRegex(pair.Refused, "cell-lock-unavailable"):
                    with pair.DiscoveryOwner(self.native) as owner:
                        owner.cell, owner.job, owner.process, owner.channel, owner.receipt = (
                            cell, job, process, channel, receipt,
                        )
                        self.native.io(70, process=process)
                deadline = 160 if known_epoch else 100
                self.assertEqual((owner.cleanup_deadline, job.cleanup_deadline), (deadline, deadline))
                self.native.kernel.WaitForSingleObject.assert_called_once_with(91, 0)
                self.native.kernel.ReleaseMutex.assert_not_called()
                self.api.ReadFile.assert_not_called()
                process.kill.assert_called_once_with()
                process.wait.assert_called_once_with(deadline)
                self.native.drain.assert_called_once_with(deadline)
                self.assertEqual(self.api.CloseHandle.call_args_list, [call(90), call(91)])
                self.assertFalse(receipt.record.call_args.args[0]["owned_tree_and_control_released"])

    def test_child_bootstrap_binds_new_mutex_as_noninheritable_and_closes_it_with_mapping(self):
        for failed in (False, True):
            with self.subTest(failed=failed):
                native = SimpleNamespace(drain=Mock(), uncertain=False)
                cell = Mock(window=None)
                cell.read.return_value = 0
                channel, client = Mock(), SimpleNamespace(module=None, window=None)
                primary, secondary = ValueError("synthetic child admission"), KeyboardInterrupt()
                if failed:
                    cell.map.side_effect = primary
                    cell.close.side_effect = secondary
                selected_os = SimpleNamespace(name="nt", set_handle_inheritable=Mock())
                with patch.object(pair, "os", selected_os), \
                        patch.object(pair, "Native", return_value=native), \
                        patch.object(pair, "DeadlineCell", return_value=cell), \
                        patch.object(pair, "Admission", return_value=SimpleNamespace(digest=DIGEST)), \
                        patch.object(pair, "Client", return_value=client), \
                        patch.object(pair, "run_discovery", return_value=0) as run, \
                        patch("multiprocessing.connection.PipeConnection", return_value=channel, create=True):
                    if failed:
                        with self.assertRaises(ValueError) as observed:
                            pair.discovery(70, 71, 72, DIGEST)
                        self.assertIs(observed.exception, primary)
                        self.assertIs(primary.__cause__, secondary)
                        run.assert_not_called()
                    else:
                        self.assertEqual(pair.discovery(70, 71, 72, DIGEST), 0)
                        run.assert_called_once_with(client)
                self.assertEqual((cell.handle, cell.mutex), (71, 72))
                self.assertEqual(selected_os.set_handle_inheritable.call_args_list,
                                 [call(70, False), call(71, False), call(72, False)])
                native.drain.assert_called_once_with(110)
                channel.close.assert_called_once_with()
                cell.close.assert_called_once_with()

    def test_bootstrap_rejects_invalid_or_aliased_mutex_before_resource_admission(self):
        with patch.object(pair, "os", SimpleNamespace(name="nt")), \
                patch.object(pair, "Native") as native, \
                patch("multiprocessing.connection.PipeConnection", create=True) as connection:
            for mutex in (None, True, 0, -1, 70, 71, "72", float("nan")):
                with self.subTest(mutex=repr(mutex)), self.assertRaises(pair.Refused):
                    pair.discovery(70, 71, mutex, DIGEST)
            native.assert_not_called()
            connection.assert_not_called()

    def test_resume_rechecks_binding_and_created_image_before_one_resume(self):
        self.process._handle, self.process.thread = 1001, 1002
        self.admission.python = pair.ROOT / "synthetic-python.exe"

        def image(handle, flags, buffer, size):
            self.assertEqual(handle, 1001)
            buffer.value = str(self.admission.python)
            return 1
        self.native.kernel.QueryFullProcessImageNameW = Mock(side_effect=image)
        self.native.resume(self.process, self.admission)
        self.admission.verify.assert_called_once_with()
        self.native.kernel.ResumeThread.assert_called_once_with(1002)
        self.assertIsNone(self.process.thread)
        self.api.CloseHandle.assert_called_once_with(1002)

    def test_changed_tool_binding_prevents_resume(self):
        self.admission.verify.side_effect = pair.Refused("synthetic tool drift")
        with self.assertRaises(pair.Refused):
            self.native.resume(self.process, self.admission)
        self.native.kernel.ResumeThread.assert_not_called()

    def test_created_image_mismatch_refuses_before_resume(self):
        self.process._handle, self.process.thread = 1001, 1002

        def image(handle, flags, buffer, size):
            buffer.value = str(pair.ROOT / "different-inert.exe")
            return 1
        self.native.kernel.QueryFullProcessImageNameW = Mock(side_effect=image)
        with self.assertRaisesRegex(pair.Refused, "created-image-mismatch"):
            self.native.resume(self.process, self.admission)
        self.native.kernel.ResumeThread.assert_not_called()
        self.assertEqual(self.process.thread, 1002)

    def test_thread_close_failure_still_releases_the_creation_process_handle(self):
        self.process._handle, self.process.thread = 1001, 1002
        self.api.CloseHandle.side_effect = [OSError("synthetic thread close"), None]
        with self.assertRaises(OSError):
            self.process.close()
        self.assertEqual(self.api.CloseHandle.call_args_list, [call(1002), call(1001)])
        self.assertEqual(self.process.thread, 1002)
        self.assertIsNone(self.process._handle)
        self.assertTrue(self.native.uncertain)

    def test_overlapped_control_uses_work_deadline_and_bounded_frame(self):
        data = pair.packet(pair.ARM, 100, digest=DIGEST)
        operation = Mock(event=300)
        operation.GetOverlappedResult.return_value = (len(data), 0)
        operation.getbuffer.return_value = data
        self.api.ReadFile = Mock(return_value=(operation, 997))
        self.process._handle = 1001
        self.assertEqual(self.native.io(70, deadline=148, process=self.process), data)
        self.api.ReadFile.assert_called_once_with(70, pair.FRAME.size, overlapped=True)
        self.api.WaitForMultipleObjects.assert_called_once_with([300, 1001], False, 48000)
        operation.GetOverlappedResult.assert_called_once_with(False)
        self.assertEqual(self.native.pending, [])

    def test_timeout_cancel_and_drain_share_remaining_reserve_not_a_fresh_budget(self):
        operation = Mock(event=300)
        operation.GetOverlappedResult.return_value = (0, 995)
        self.api.ReadFile = Mock(return_value=(operation, 997))

        def wait(handles, all_handles, milliseconds):
            self.now = 148
            return 258
        self.api.WaitForMultipleObjects.side_effect = wait
        with self.assertRaises(pair.EnclosureExpired):
            self.native.io(70, deadline=148)
        self.assertEqual(self.native.pending, [operation])
        self.api.WaitForMultipleObjects.side_effect = None
        self.api.WaitForMultipleObjects.return_value = 0
        self.native.drain(160)
        operation.cancel.assert_called_once_with()
        self.assertEqual(self.api.WaitForMultipleObjects.call_args, call([300], False, 12000))
        operation.GetOverlappedResult.assert_called_once_with(False)
        self.assertEqual(self.native.pending, [])

    def test_truncated_frames_short_writes_and_child_exit_are_explicit_failures(self):
        for kind in ("short-read", "short-write", "child-exit"):
            with self.subTest(kind=kind):
                self.native.pending = []
                operation = Mock(event=300)
                operation.GetOverlappedResult.return_value = (1, 0)
                operation.getbuffer.return_value = b"x"
                self.api.ReadFile = Mock(return_value=(operation, 997))
                self.api.WriteFile = Mock(return_value=(operation, 997))
                self.api.WaitForMultipleObjects.return_value = 1 if kind == "child-exit" else 0
                error = pair.DiscoveryExited if kind == "child-exit" else pair.Refused
                with self.assertRaises(error):
                    self.native.io(70, b"more" if kind == "short-write" else None,
                                   deadline=148, process=self.process)
                if kind == "child-exit":
                    self.assertEqual(self.native.pending, [operation])
                    operation.GetOverlappedResult.assert_not_called()

    def test_signaled_event_with_incomplete_io_is_not_confirmed_cancellation(self):
        operation = Mock(event=300)
        operation.GetOverlappedResult.return_value = (0, 996)
        self.native.pending.append(operation)
        with patch.object(pair, "UNCONFIRMED_IO", []) as retained:
            with self.assertRaisesRegex(pair.Refused, "cancellation-unconfirmed"):
                self.native.drain(160)
            self.assertEqual(retained, [operation])
        self.assertTrue(self.native.uncertain)

    def test_failed_cancel_drain_is_explicit_and_prevents_more_io(self):
        operation = Mock(event=300)
        self.native.pending.append(operation)
        self.api.WaitForMultipleObjects.return_value = 258
        with patch.object(pair, "UNCONFIRMED_IO", []) as retained:
            with self.assertRaisesRegex(pair.Refused, "cancellation-unconfirmed"):
                self.native.drain(160)
            self.assertEqual(retained, [operation])
            with self.assertRaisesRegex(pair.Refused, "prior-io-unconfirmed"):
                self.native.io(70, b"inert", deadline=160)
        self.assertTrue(self.native.uncertain)

    def test_interrupted_cancel_retains_pending_buffer_and_propagates_real_cancellation(self):
        error = KeyboardInterrupt()
        operation = Mock(event=300)
        operation.cancel.side_effect = error
        self.native.pending.append(operation)
        with patch.object(pair, "UNCONFIRMED_IO", []) as retained:
            with self.assertRaises(KeyboardInterrupt) as observed:
                self.native.drain(160)
            self.assertIs(observed.exception, error)
            self.assertEqual(retained, [operation])
        self.assertTrue(self.native.uncertain)
        self.assertIn("unconfirmed", error.__notes__[0])

    def test_normal_member_check_requires_the_known_live_creation_handle_not_zero(self):
        self.process._handle, self.process.pid = 1001, 1003
        self.process.poll = Mock(return_value=None)

        def query(handle, kind, value, size, length):
            value._obj.assigned = value._obj.listed = 1
            value._obj.pids[0] = 1003
            length._obj.value = size
            return 1
        job = SimpleNamespace(handle=7, api=SimpleNamespace(QueryInformationJobObject=Mock(side_effect=query)),
                              active_processes=Mock(return_value=1))
        self.assertEqual(self.native.members(job, self.process), 1)
        job.active_processes.return_value = 0
        with self.assertRaises(pair.Refused):
            self.native.members(job, self.process)
        job.active_processes.return_value = 1
        self.native.kernel.GetProcessId.return_value = 999
        with self.assertRaises(pair.Refused):
            self.native.members(job, self.process)

    def test_unknown_or_truncated_member_list_is_refused(self):
        self.process._handle, self.process.pid = 1001, 1003
        self.process.poll = Mock(return_value=None)

        def query(handle, kind, value, size, length):
            value._obj.assigned = value._obj.listed = 1
            value._obj.pids[0] = 1003
            length._obj.value = 0
            return 1
        job = SimpleNamespace(handle=7, api=SimpleNamespace(QueryInformationJobObject=Mock(side_effect=query)),
                              active_processes=Mock(return_value=1))
        with self.assertRaises(pair.Refused):
            self.native.members(job, self.process)


class ObserverPairSupervisorTest(SyntheticOnly):
    def setUp(self):
        super().setUp()
        self.admission = SimpleNamespace(digest=DIGEST)
        self.job = SimpleNamespace(cleanup_deadline=None, close=Mock())
        self.process = SimpleNamespace(_handle=900, command=["synthetic-discovery"], kill=Mock(),
                                       wait=Mock(return_value=0), close=Mock())
        self.channel = Mock()
        self.channel.fileno.return_value = 19
        self.receipt = Mock()
        self.native = SimpleNamespace(resume=Mock(), members=Mock(return_value=1), drain=Mock(),
                                      kernel=SimpleNamespace(UnmapViewOfFile=Mock(return_value=1)),
                                      release=Mock(), uncertain=False)
        self.cell, self.publisher, self.storage = synthetic_cells(self.native)
        self.native.deadline_cell = self.cell
        self.native.wait_case_close = Mock(side_effect=lambda cell, process: self.assertTrue(cell.accept_close()))
        self.messages = [
            pair.packet(pair.READY, value=3, digest=DIGEST),
            pair.packet(pair.ARM, 100, digest=DIGEST),
            pair.packet(pair.ENTER, 100, digest=DIGEST),
            pair.packet(pair.LEAVE, 100, digest=DIGEST),
            pair.packet(pair.DONE, 100, digest=DIGEST),
            pair.packet(pair.SUITE_OK, value=3, digest=DIGEST),
        ]
        self.received = 0

        def transfer(handle, data=None, **kwargs):
            if data is None:
                self.received += 1
                message = self.messages.pop(0)
                if pair.unpack(message)[0] == pair.ARM:
                    self.publisher.publish()
                return message
            if pair.unpack(data)[2] == pair.DONE:
                self.publisher.close_case()
        self.native.io = Mock(side_effect=transfer)

    def invoke(self):
        with pair.DiscoveryOwner(self.native) as owner:
            owner.job, owner.process, owner.channel, owner.receipt = self.job, self.process, self.channel, self.receipt
            owner.cell = self.cell
            return pair.supervise(owner, self.admission)

    def test_normal_completion_does_not_leave_case_deadline_on_retained_suite_job(self):
        original = self.native.io.side_effect

        def transfer(handle, data=None, **kwargs):
            if data is None and self.received == 5:
                self.assertIsNone(self.job.cleanup_deadline)
                self.assertIsNone(kwargs["deadline"])
                self.now = 400
            return original(handle, data, **kwargs)
        self.native.io.side_effect = transfer
        self.invoke()
        self.assertEqual(self.job.cleanup_deadline, 410)
        self.native.members.assert_called()
        self.assertEqual(self.native.members.call_count, 4)
        self.job.close.assert_called_once_with()
        self.process.wait.assert_has_calls([call(410), call(410)])
        self.channel.close.assert_called_once_with()
        self.process.close.assert_called_once_with()
        for invocation in self.receipt.record.call_args_list[:3]:
            self.assertEqual(invocation.args[0]["known_live_discovery_members"], 1)
            self.assertEqual(invocation.args[1], 148)

    def test_actual_required_suite_nonzero_is_not_replaced_by_successful_protocol(self):
        self.messages[-1] = pair.packet(pair.SUITE_FAILED, value=3, digest=DIGEST)
        self.process.wait.return_value = 1
        with self.assertRaises(subprocess.CalledProcessError) as error:
            self.invoke()
        self.assertEqual(error.exception.returncode, 1)
        self.assertEqual(error.exception.cmd, ["synthetic-discovery"])

    def test_actual_suite_failure_stays_primary_when_final_report_or_cleanup_fails(self):
        self.messages[-1] = pair.packet(pair.SUITE_FAILED, value=3, digest=DIGEST)
        self.process.wait.return_value = 1
        report_error = pair.Refused("synthetic final report refused")

        def record(fields, deadline):
            if fields["phase"] == "suite-exit":
                raise report_error
        self.receipt.record.side_effect = record
        with self.assertRaises(subprocess.CalledProcessError) as observed:
            self.invoke()
        self.assertEqual(observed.exception.returncode, 1)
        self.assertIs(observed.exception.__cause__, report_error)
        self.job.close.assert_called_once_with()

    def test_hard_expiry_uses_absolute_total_deadline_and_never_completes_inventory(self):
        original = self.native.io.side_effect

        def transfer(handle, data=None, **kwargs):
            if data is None and self.received == 3:
                self.now = 148
                raise pair.EnclosureExpired("synthetic deadline")
            return original(handle, data, **kwargs)
        self.native.io.side_effect = transfer
        with self.assertRaises(pair.EnclosureExpired):
            self.invoke()
        self.assertEqual(self.job.cleanup_deadline, 160)
        self.native.drain.assert_called_once_with(160)
        self.process.wait.assert_called_once_with(160)
        self.assertFalse(any(item.args[0]["phase"] in ("case-closed", "suite-exit")
                             for item in self.receipt.record.call_args_list))

    def test_frame_epoch_must_match_the_independently_published_epoch(self):
        self.messages[1] = pair.packet(pair.ARM, 99, digest=DIGEST)
        with self.assertRaisesRegex(pair.Refused, "arm-not-published"):
            self.invoke()
        self.assertEqual(self.job.cleanup_deadline, 160)
        self.process.kill.assert_called_once_with()
        self.assertFalse(any(item.args[0]["phase"] == "suite-exit"
                             for item in self.receipt.record.call_args_list))

    def test_reporting_failure_retains_primary_and_explicit_cleanup_uncertainty(self):
        primary = pair.Refused("synthetic parent report failure")
        self.receipt.record.side_effect = primary
        self.native.drain.side_effect = OSError("synthetic pending pipe")
        with self.assertRaises(pair.Refused) as error:
            self.invoke()
        self.assertIs(error.exception, primary)
        self.assertEqual(self.job.cleanup_deadline, 160)
        self.assertIn("unconfirmed", primary.__notes__[0])
        self.assertIsInstance(primary.__cause__, pair.Refused)
        self.receipt.close.assert_called_once_with()

    def test_discovery_exit_and_cancellation_are_fail_closed_not_fake_test_results(self):
        for failure in (pair.DiscoveryExited("synthetic abrupt exit"), KeyboardInterrupt()):
            with self.subTest(failure=type(failure).__name__):
                self.received = 0
                self.cell, self.publisher, self.storage = synthetic_cells(self.native)
                self.native.deadline_cell = self.cell
                self.messages = [pair.packet(pair.READY, value=3, digest=DIGEST),
                                 pair.packet(pair.ARM, 100, digest=DIGEST)]
                original = self.native.io.side_effect

                def transfer(handle, data=None, **kwargs):
                    if data is None and self.received == 2:
                        raise failure
                    if data is None:
                        self.received += 1
                        message = self.messages.pop(0)
                        if pair.unpack(message)[0] == pair.ARM:
                            self.publisher.publish()
                        return message
                self.native.io.side_effect = transfer
                with self.assertRaises(type(failure)) as error:
                    self.invoke()
                self.assertIs(error.exception, failure)
                self.assertEqual(self.job.cleanup_deadline, 160)
                self.native.io.side_effect = original

    def test_cancellation_during_cleanup_still_attempts_control_report_and_handle_release(self):
        interruption = KeyboardInterrupt()
        self.job.close.side_effect = interruption
        with self.assertRaises(KeyboardInterrupt) as observed:
            pair.cleanup(self.native, self.job, self.process, self.channel, self.receipt, 160)
        self.assertIs(observed.exception, interruption)
        self.process.kill.assert_called_once_with()
        self.process.wait.assert_called_once_with(160)
        self.native.drain.assert_called_once_with(160)
        self.channel.close.assert_called_once_with()
        self.process.close.assert_called_once_with()
        self.receipt.close.assert_called_once_with()
        self.assertFalse(self.receipt.record.call_args.args[0]["owned_tree_and_control_released"])

    def test_failed_termination_still_attempts_reap_and_never_claims_complete_cleanup(self):
        self.process.kill.side_effect = OSError("synthetic termination refused")
        with self.assertRaises(pair.Refused):
            pair.cleanup(self.native, self.job, self.process, self.channel, self.receipt, 160)
        self.process.wait.assert_called_once_with(160)
        self.channel.close.assert_called_once_with()
        self.assertFalse(self.receipt.record.call_args.args[0]["owned_tree_and_control_released"])


class ObserverPairStartupOwnershipTest(SyntheticOnly):
    def test_cell_read_cancellation_preserves_primary_without_new_cleanup_credit(self):
        for known_window in (False, True):
            with self.subTest(known_window=known_window):
                primary, secondary = AssertionError("synthetic primary"), KeyboardInterrupt()
                native = Mock(uncertain=False)
                cell = SimpleNamespace(address=8192, window=pair.Window(100) if known_window else None,
                                       observe=Mock(side_effect=secondary), close=Mock())
                job, process, channel, receipt = Mock(), Mock(_handle=900), Mock(), Mock()
                with self.assertRaises(AssertionError) as observed:
                    with pair.DiscoveryOwner(native) as owner:
                        owner.job, owner.process, owner.channel, owner.receipt = job, process, channel, receipt
                        owner.cell = cell
                        raise primary
                self.assertIs(observed.exception, primary)
                self.assertIs(primary.__cause__, secondary)
                deadline = 160 if known_window else 100
                self.assertEqual((owner.cleanup_deadline, job.cleanup_deadline), (deadline, deadline))
                native.drain.assert_called_once_with(deadline)
                job.close.assert_called_once_with()
                process.kill.assert_called_once_with()
                process.wait.assert_called_once_with(deadline)
                channel.close.assert_called_once_with()
                process.close.assert_called_once_with()
                cell.close.assert_called_once_with()
                receipt.close.assert_called_once_with()
                self.assertFalse(receipt.record.call_args.args[0]["owned_tree_and_control_released"])

    def test_protocol_construction_or_supervisory_entry_cancellation_keeps_one_cleanup_owner(self):
        for boundary in ("Protocol", "supervise"):
            with self.subTest(boundary=boundary):
                primary = KeyboardInterrupt()
                admission = Mock(digest=DIGEST, paths=("synthetic",), snapshot={"python": {"sha256": "a" * 64}})
                job = SimpleNamespace(attach=Mock(), close=Mock(), cleanup_deadline=None)
                admission.budget_module.return_value.WindowsJob.return_value = job
                process = SimpleNamespace(_handle=900, command=["inert"], kill=Mock(), wait=Mock(), close=Mock())
                native = Mock(uncertain=False)
                cell, _, _ = synthetic_cells(native)
                channels, receipt = (Mock(), Mock()), Mock()
                with patch.object(pair, "Admission", return_value=admission), \
                        patch.object(pair, "Native", return_value=native), \
                        patch.object(pair, "OwnedDiscovery", return_value=process), \
                        patch.object(pair, "DeadlineCell", return_value=cell), \
                        patch.object(pair, "Receipt", return_value=receipt), \
                        patch("multiprocessing.connection.Pipe", return_value=channels), \
                        patch.object(pair, boundary, side_effect=primary):
                    with self.assertRaises(KeyboardInterrupt) as observed:
                        pair.run_owning_suite()
                self.assertIs(observed.exception, primary)
                job.attach.assert_called_once_with(process)
                job.close.assert_called_once_with()
                self.assertEqual(job.cleanup_deadline, 110)
                process.kill.assert_called_once_with()
                process.wait.assert_called_once_with(110)
                native.drain.assert_called_once_with(110)
                channels[0].close.assert_called_once_with()
                channels[1].close.assert_called_once_with()
                process.close.assert_called_once_with()
                native.kernel.UnmapViewOfFile.assert_called_once_with(8192)
                self.assertEqual(native.release.call_args_list, [call(90), call(91)])
                receipt.close.assert_called_once_with()

    def test_attachment_precedes_resume_and_source_entry_and_has_no_fallback(self):
        events = []
        admission = Mock(digest=DIGEST, paths=("synthetic",), snapshot={"python": {"sha256": "a" * 64}})
        job = SimpleNamespace(attach=Mock(side_effect=lambda process: events.append("attach")), close=Mock())
        admission.budget_module.return_value.WindowsJob.return_value = job
        native = Mock(uncertain=False)
        cell, _, _ = synthetic_cells(native)
        native.create.side_effect = lambda admission, child, process, deadline_cell: events.append("suspended-create")
        native.members.side_effect = lambda job, process: events.append("known-member")
        channels = (Mock(), Mock())
        with patch.object(pair, "Admission", return_value=admission), patch.object(pair, "Native", return_value=native), \
                patch.object(pair, "DeadlineCell", return_value=cell), \
                patch.object(pair, "Receipt"), patch.object(pair, "supervise", side_effect=lambda *args:
                                                          events.append("supervise-and-resume")) as supervise, \
                patch("multiprocessing.connection.Pipe", return_value=channels):
            pair.run_owning_suite()
        self.assertEqual(events, ["suspended-create", "attach", "known-member", "supervise-and-resume"])
        supervise.assert_called_once()
        job.close.assert_called_once_with()
        channels[1].close.assert_called_once_with()

    def test_attach_failure_never_resumes_and_preserves_primary_through_cleanup_failure(self):
        error = OSError("synthetic attachment refused")
        admission = Mock(digest=DIGEST, paths=("synthetic",), snapshot={"python": {"sha256": "a" * 64}})
        admission.budget_module.return_value.WindowsJob.return_value.attach.side_effect = error
        native = Mock(uncertain=False)
        cell, _, _ = synthetic_cells(native)
        with patch.object(pair, "Admission", return_value=admission), patch.object(pair, "Native", return_value=native), \
                patch.object(pair, "DeadlineCell", return_value=cell), \
                patch.object(pair, "Receipt"), patch.object(pair, "supervise") as supervise, \
                patch("multiprocessing.connection.Pipe", return_value=(Mock(), Mock())), \
                patch.object(pair, "cleanup", side_effect=pair.Refused("synthetic cleanup uncertainty")):
            with self.assertRaises(OSError) as observed:
                pair.run_owning_suite()
        self.assertIs(observed.exception, error)
        self.assertIsInstance(error.__cause__, pair.Refused)
        supervise.assert_not_called()
        native.resume.assert_not_called()

    def test_failed_child_control_close_cannot_skip_owned_cleanup_or_resume(self):
        primary = OSError("synthetic child handle close")
        admission = Mock(digest=DIGEST, paths=("synthetic",), snapshot={"python": {"sha256": "a" * 64}})
        native, child = Mock(uncertain=False), Mock()
        cell, _, _ = synthetic_cells(native)
        child.close.side_effect = primary
        with patch.object(pair, "Admission", return_value=admission), patch.object(pair, "Native", return_value=native), \
                patch.object(pair, "DeadlineCell", return_value=cell), \
                patch.object(pair, "Receipt"), patch.object(pair, "supervise") as supervise, \
                patch("multiprocessing.connection.Pipe", return_value=(Mock(), child)), \
                patch.object(pair, "cleanup") as cleanup:
            with self.assertRaises(OSError) as observed:
                pair.run_owning_suite()
        self.assertIs(observed.exception, primary)
        self.assertTrue(native.uncertain)
        child.close.assert_called_once_with()
        cleanup.assert_called_once()
        supervise.assert_not_called()
        native.resume.assert_not_called()

    def test_receipt_is_exclusive_append_only_bounded_and_has_no_raw_child_fields(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        native = SimpleNamespace(api=SimpleNamespace(CreateFile=Mock(return_value=5)), io=Mock(), release=Mock())
        with patch.object(pair, "ROOT", root):
            receipt = pair.Receipt(native)
        args = native.api.CreateFile.call_args.args
        self.assertEqual(args[1:6], (4, 1, 0, 1, 0x40200000))
        for index in range(8):
            receipt.record({"phase": "synthetic"}, 148)
        with self.assertRaises(pair.Refused):
            receipt.record({"phase": "ninth"}, 148)
        self.assertEqual(native.io.call_count, 8)
        self.assertTrue(all(item.kwargs == {"deadline": 148} for item in native.io.call_args_list))
        receipt.close()
        native.release.assert_called_once_with(5)


if __name__ == "__main__":
    unittest.main()
