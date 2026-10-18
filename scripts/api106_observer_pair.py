"""Explicit API106 full-discovery supervision; never enabled by ordinary test discovery."""

from contextlib import contextmanager
import ctypes
from ctypes import wintypes
import hashlib
import json
import math
import os
from pathlib import Path
import stat
import struct
import subprocess
import sys
import time
from types import ModuleType
import unittest


ROOT = Path(__file__).resolve().parents[1]
TARGET = "test_money_mutation.ProcessBudgetTest.test_approved_system_shell_survives_without_inheriting_ambient_path"
WORK_SECONDS, TOTAL_SECONDS, CLEANUP_SECONDS = 48, 60, 12
REPORT = Path("build/diagnostics/api106-approved-shell-enclosure.jsonl")
FRAME = struct.Struct("<8sBdQ32s")
MAGIC = b"API106P1"
READY, ARM, ENTER, LEAVE, DONE, SUITE_OK, SUITE_FAILED, ACK = range(1, 9)
EMPTY_DIGEST = b"\0" * 32
# Retain unconfirmed OVERLAPPED buffers through failure reporting. Their later
# finalization is not a confirmed drain or evidence of bounded interpreter exit.
UNCONFIRMED_IO = []
CLEANUP_FAILURES = (OSError, ValueError, KeyboardInterrupt, SystemExit)
CELL_POLL_MILLISECONDS = 25


class Refused(ValueError):
    """Static refusal codes only, without child output or untrusted values."""


class EnclosureExpired(TimeoutError):
    pass


class DiscoveryExited(OSError):
    pass


def require(condition, code):
    if not condition:
        raise Refused(code)


def remaining(deadline):
    return max(0.0, deadline - time.monotonic())


def before(deadline):
    if time.monotonic() > deadline:
        raise EnclosureExpired("api106-enclosure-deadline")


class Window:
    def __init__(self, started):
        require(type(started) in (int, float) and math.isfinite(started) and 0 <= started <= time.monotonic(),
                "api106-invalid-absolute-epoch")
        self.started = float(started)
        self.work_deadline = self.started + WORK_SECONDS
        self.deadline = self.started + TOTAL_SECONDS


class DeadlineCell:
    """One mutex-protected shared word: zero, published epoch bits, then their negative."""

    def __init__(self, native, *, publisher=False):
        self.native, self.publisher = native, publisher
        self.handle = self.mutex = self.address = self.window = self.token = None
        self.closing = self.closed = False

    def create(self):
        require(not self.publisher and self.handle is None and self.mutex is None, "api106-cell-create-state")
        self.handle = self.native.kernel.CreateFileMappingW(wintypes.HANDLE(-1), None, 4, 0, 8, None)
        require(self.handle, "api106-cell-create")
        self.mutex = self.native.kernel.CreateMutexW(None, False, None)
        require(self.mutex, "api106-cell-mutex-create")
        self.map()
        require(self.read() == 0, "api106-cell-not-empty")

    def map(self):
        self.address = self.native.kernel.MapViewOfFile(self.handle, 6, 0, 0, 8)
        require(self.address and self.address % 8 == 0, "api106-cell-map")

    def compare_exchange(self, value, expected):
        require(self.address and self.address % 8 == 0 and self.mutex, "api106-cell-not-mapped")
        require(not self.native.uncertain, "api106-cell-ownership-unconfirmed")
        acquired, primary = False, None
        try:
            # A contended lock must not hide publication behind a blocking wait.
            # Mutex acquisition/release supplies the documented memory barriers.
            status = self.native.kernel.WaitForSingleObject(self.mutex, 0)
            acquired = type(status) is int and status in (0, 0x80)
            require(status != 0x80, "api106-cell-lock-abandoned")
            require(status != 0x102, "api106-cell-lock-unavailable")
            require(type(status) is int and status == 0, "api106-cell-lock-refused")
            word = ctypes.c_int64.from_address(self.address)
            previous = word.value
            if previous == expected:
                word.value = value
            return previous
        except BaseException as error:
            self.native.uncertain = True
            primary = error
            raise
        finally:
            if acquired:
                try:
                    require(self.native.kernel.ReleaseMutex(self.mutex), "api106-cell-lock-release")
                except CLEANUP_FAILURES as secondary:
                    self.native.uncertain = True
                    if primary is not None:
                        primary.add_note("API106 deadline-cell mutex release is also unconfirmed")
                        if secondary is not primary and primary.__cause__ is None:
                            raise primary.with_traceback(primary.__traceback__) from secondary
                        raise primary.with_traceback(primary.__traceback__)
                    raise

    def read(self):
        return self.compare_exchange(0, 0)

    def publish(self):
        require(self.publisher and not self.closed and self.window is None, "api106-cell-reused")
        started = time.monotonic()
        require(math.isfinite(started) and started > 0, "api106-cell-epoch")
        token, = struct.unpack("<q", struct.pack("<d", started))
        # Publication commits the sampled epoch before admission or any control wait.
        # A delayed publication cannot grant a newer epoch or additional work time.
        self.token, self.window = token, Window(started)
        require(self.compare_exchange(token, 0) == 0, "api106-cell-reused")
        return self.window

    def observe(self):
        value = self.read()
        require(type(value) is int and -0x7fffffffffffffff <= value <= 0x7fffffffffffffff,
                "api106-cell-shape")
        if self.closed:
            require(value == -self.token, "api106-cell-rearmed")
            return None
        if value == 0:
            require(self.window is None, "api106-cell-erased")
            return None
        token = abs(value)
        started, = struct.unpack("<d", struct.pack("<q", token))
        if self.window is None:
            self.window, self.token = Window(started), token
        require(token == self.token, "api106-cell-epoch-changed")
        require(value > 0 or self.closing, "api106-cell-closed-before-done")
        return self.window

    def begin_close(self):
        require(not self.publisher and not self.closing and self.observe() is not None,
                "api106-cell-close-state")
        before(self.window.work_deadline)
        self.closing = True

    def close_case(self):
        require(self.publisher and not self.closed and self.window is not None, "api106-cell-close-state")
        before(self.window.work_deadline)
        require(self.compare_exchange(-self.token, self.token) == self.token, "api106-cell-close-state")
        self.closed, self.window = True, None

    def accept_close(self):
        require(not self.publisher and self.closing and not self.closed and self.window is not None,
                "api106-cell-close-state")
        value = self.read()
        require(value in (self.token, -self.token), "api106-cell-close-state")
        before(self.window.work_deadline)
        if value == self.token:
            return False
        self.closed, self.window = True, None
        return True

    def close(self):
        errors = []
        address, self.address = self.address, None
        handles = self.handle, self.mutex
        self.handle = self.mutex = None
        if address is not None:
            try:
                require(self.native.kernel.UnmapViewOfFile(address), "api106-cell-unmap")
            except CLEANUP_FAILURES as error:
                self.native.uncertain = True
                errors.append(error)
        for handle in handles:
            if handle:
                try:
                    self.native.release(handle)
                except CLEANUP_FAILURES as error:
                    self.native.uncertain = True
                    errors.append(error)
        if errors:
            if len(errors) > 1:
                errors[0].add_note("Another API106 deadline-cell release is also unconfirmed")
            raise errors[0]


def source_bytes(path, limit=4 * 1024 * 1024):
    for component in (path, *path.parents):
        require(not component.is_symlink() and not component.is_junction(), "api106-linked-input")
    prior = path.stat(follow_symlinks=False)
    require(stat.S_ISREG(prior.st_mode) and prior.st_nlink == 1 and prior.st_size <= limit,
            "api106-input-kind-or-size")
    with path.open("rb") as stream:
        opened = os.fstat(stream.fileno())
        data = stream.read(limit + 1)
        after = os.fstat(stream.fileno())
    current = path.stat(follow_symlinks=False)
    require(all(os.path.samestat(prior, item) for item in (opened, after, current))
            and len({item.st_mtime_ns for item in (prior, opened, after, current)}) == 1
            and all(item.st_size == len(data) for item in (prior, opened, after, current)),
            "api106-input-changed")
    return data


def fingerprint(data):
    return {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode("ascii")


class Admission:
    """Bind this API's fixed sources and the already selected CPython, not Infra tools."""

    def __init__(self):
        require(os.name == "nt" and sys.implementation.name == "cpython" and sys.version_info[:2] == (3, 14),
                "api106-requires-api-windows-cpython314")
        require(not any(key.upper().startswith("PYTHON") and key.upper() not in {
            "PYTHONDONTWRITEBYTECODE", "PYTHONUNBUFFERED", "PYTHONIOENCODING", "PYTHONWARNINGS",
            "PYTHONLOCATION", "PYTHON_ROOT_DIR", "PYTHON2_ROOT_DIR", "PYTHON3_ROOT_DIR",
        } for key in os.environ), "api106-python-startup-environment")
        self.python = Path(sys.executable)
        require(self.python.is_absolute() and self.python.name.lower() == "python.exe",
                "api106-python-selection")
        self.paths = tuple(sorted((ROOT / "scripts").glob("*.py"))) + tuple(
            sorted((ROOT / "scripts" / "tests").glob("test*.py")))
        required = {ROOT / "scripts" / name for name in
                    ("api106_observer_pair.py", "process_budget.py", "quality.py", "tests/test_money_mutation.py")}
        require(required <= set(self.paths), "api106-source-inventory")
        self.snapshot = self.capture()
        self.digest = hashlib.sha256(encoded(self.snapshot)).digest()

    def capture(self):
        paths = tuple(sorted((ROOT / "scripts").glob("*.py"))) + tuple(
            sorted((ROOT / "scripts" / "tests").glob("test*.py")))
        require(paths == self.paths, "api106-source-inventory-changed")
        return {
            "python": {"path": str(self.python), **fingerprint(source_bytes(self.python, 32 * 1024 * 1024))},
            "version": sys.version,
            "sources": {path.relative_to(ROOT).as_posix(): fingerprint(source_bytes(path)) for path in self.paths},
            "environment_sha256": hashlib.sha256(encoded(dict(os.environ))).hexdigest(),
        }

    def verify(self):
        require(self.capture() == self.snapshot, "api106-admission-changed")

    def budget_module(self):
        path = ROOT / "scripts" / "process_budget.py"
        data = source_bytes(path)
        require(fingerprint(data) == self.snapshot["sources"]["scripts/process_budget.py"],
                "api106-budget-source-changed")
        module = ModuleType("api106_owned_process_budget")
        module.__file__ = str(path)
        exec(compile(data, str(path), "exec"), module.__dict__)
        return module

    def command(self, handle, deadline_handle, mutex_handle):
        path = ROOT / "scripts" / "api106_observer_pair.py"
        digest = self.snapshot["sources"]["scripts/api106_observer_pair.py"]["sha256"]
        # -c retains the ordinary interpreter's startup flags. Restore -m's absolute
        # cwd entry, then execute only the fixed, byte-bound API bootstrap.
        bootstrap = (
            "import sys;sys.path[0]=" + repr(str(ROOT)) + ";"
            "import hashlib,pathlib;"
            "p=pathlib.Path(" + repr(str(path)) + ");f=p.open('rb');b=f.read(4194305);f.close();"
            "(hashlib.sha256(b).hexdigest()==" + repr(digest) + ") or sys.exit('api106-bootstrap-source-changed');"
            "n={'__name__':'__api106_discovery__','__file__':str(p)};"
            "exec(compile(b,str(p),'exec'),n);"
            "sys.exit(n['discovery'](int(sys.argv[1]),int(sys.argv[2]),int(sys.argv[3]),bytes.fromhex(sys.argv[4])))"
        )
        return [str(self.python), "-c", bootstrap, str(handle), str(deadline_handle), str(mutex_handle),
                self.digest.hex()]


def packet(kind, epoch=0.0, value=0, digest=EMPTY_DIGEST):
    require(type(kind) is int and READY <= kind <= ACK
            and type(epoch) in (int, float) and math.isfinite(epoch) and epoch >= 0
            and type(value) is int and 0 <= value <= 1000000
            and type(digest) is bytes and len(digest) == 32, "api106-control-shape")
    return FRAME.pack(MAGIC, kind, epoch, value, digest)


def unpack(data):
    require(type(data) is bytes and len(data) == FRAME.size, "api106-control-size")
    magic, kind, epoch, value, digest = FRAME.unpack(data)
    require(magic == MAGIC and packet(kind, epoch, value, digest) == data, "api106-control-shape")
    return kind, epoch, value, digest


class Protocol:
    def __init__(self, digest):
        self.digest, self.phase, self.window = digest, "discovery", None
        self.discovered = self.ran = None

    def accept(self, data):
        kind, epoch, value, digest = unpack(data)
        require(digest == self.digest, "api106-control-binding")
        if self.window is not None:
            before(self.window.work_deadline)
        if kind == READY:
            require(self.phase == "discovery" and epoch == 0 and value > 0, "api106-discovery-protocol")
            self.phase, self.discovered = "ready", value
        elif kind == ARM:
            require(self.phase == "ready" and value == 0, "api106-arm-reused")
            self.window = Window(epoch)
            before(self.window.work_deadline)
            self.phase = "armed"
        elif kind in (ENTER, LEAVE, DONE):
            require(self.window is not None and epoch == self.window.started, "api106-window-changed")
            expected = {ENTER: "armed", LEAVE: "entered", DONE: "left"}[kind]
            require(self.phase == expected and (value in (0, 1) if kind == LEAVE else value == 0),
                    "api106-case-protocol")
            self.phase = {ENTER: "entered", LEAVE: "left", DONE: "closing"}[kind]
        else:
            require(kind in (SUITE_OK, SUITE_FAILED) and self.phase == "closed" and epoch == 0
                    and (kind != SUITE_OK or value == self.discovered), "api106-incomplete-suite")
            self.phase, self.ran = "finished", value
        return kind, epoch, value

    def close_case(self):
        require(self.phase == "closing" and self.window is not None, "api106-case-not-closing")
        before(self.window.work_deadline)
        self.phase, self.window = "closed", None


class Native:
    """The fixed Windows creation/overlapped-I/O boundary; no command or budget configuration."""

    def __init__(self):
        import _winapi
        self.api = _winapi
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel.ResumeThread.argtypes = [wintypes.HANDLE]
        self.kernel.ResumeThread.restype = wintypes.DWORD
        self.kernel.QueryFullProcessImageNameW.argtypes = [
            wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD),
        ]
        self.kernel.QueryFullProcessImageNameW.restype = wintypes.BOOL
        self.kernel.GetProcessId.argtypes = [wintypes.HANDLE]
        self.kernel.GetProcessId.restype = wintypes.DWORD
        self.kernel.CreateFileMappingW.argtypes = [
            wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, wintypes.LPCWSTR,
        ]
        self.kernel.CreateFileMappingW.restype = wintypes.HANDLE
        self.kernel.MapViewOfFile.argtypes = [
            wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, ctypes.c_size_t,
        ]
        self.kernel.MapViewOfFile.restype = ctypes.c_void_p
        self.kernel.UnmapViewOfFile.argtypes = [ctypes.c_void_p]
        self.kernel.UnmapViewOfFile.restype = wintypes.BOOL
        self.kernel.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
        self.kernel.CreateMutexW.restype = wintypes.HANDLE
        self.kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        self.kernel.WaitForSingleObject.restype = wintypes.DWORD
        self.kernel.ReleaseMutex.argtypes = [wintypes.HANDLE]
        self.kernel.ReleaseMutex.restype = wintypes.BOOL
        self.pending = []
        self.uncertain = False
        self.deadline_cell = None

    def duplicate(self, handle, inheritable=True):
        own = self.api.GetCurrentProcess()
        return self.api.DuplicateHandle(own, handle, own, 0, inheritable, self.api.DUPLICATE_SAME_ACCESS)

    def release(self, handle):
        try:
            self.api.CloseHandle(handle)
        except CLEANUP_FAILURES:
            self.uncertain = True
            raise

    def create(self, admission, child, process, cell):
        duplicates = []
        failure, traceback, closing_error = None, None, None
        try:
            control = self.duplicate(child.fileno())
            duplicates.append(control)
            deadline_handle = self.duplicate(cell.handle)
            duplicates.append(deadline_handle)
            mutex_handle = self.duplicate(cell.mutex)
            duplicates.append(mutex_handle)
            standards = []
            for code in (self.api.STD_INPUT_HANDLE, self.api.STD_OUTPUT_HANDLE, self.api.STD_ERROR_HANDLE):
                handle = self.duplicate(self.api.GetStdHandle(code))
                duplicates.append(handle)
                standards.append(handle)
            startup = subprocess.STARTUPINFO()
            startup.dwFlags |= self.api.STARTF_USESTDHANDLES
            startup.hStdInput, startup.hStdOutput, startup.hStdError = standards
            startup.lpAttributeList = {"handle_list": duplicates}
            process.command = admission.command(control, deadline_handle, mutex_handle)
            admission.verify()
            process._handle, process.thread, process.pid, process.tid = self.api.CreateProcess(
                str(admission.python), subprocess.list2cmdline(process.command), None, None, True,
                0x00000004, dict(os.environ), str(ROOT), startup,  # CREATE_SUSPENDED, no breakaway
            )
        except BaseException as error:
            failure, traceback = error, error.__traceback__
        finally:
            for handle in duplicates:
                try:
                    self.release(handle)
                except CLEANUP_FAILURES as error:
                    self.uncertain = True
                    if closing_error is None:
                        closing_error = error
                    else:
                        closing_error.add_note("Another API106 inherited-handle release is unconfirmed")
        if failure is not None:
            if closing_error is failure:
                failure.add_note("API106 inherited-handle release is also unconfirmed")
                raise failure.with_traceback(traceback)
            raise failure.with_traceback(traceback) from closing_error
        if closing_error is not None:
            raise closing_error

    def resume(self, process, admission):
        admission.verify()
        size = wintypes.DWORD(32768)
        name = ctypes.create_unicode_buffer(size.value)
        require(self.kernel.QueryFullProcessImageNameW(process._handle, 0, name, ctypes.byref(size)),
                "api106-created-image-unavailable")
        require(Path(name.value).resolve() == admission.python.resolve(), "api106-created-image-mismatch")
        require(self.kernel.ResumeThread(process.thread) == 1, "api106-resume-refused")
        self.release(process.thread)
        process.thread = None

    def io(self, handle, data=None, *, deadline=None, process=None):
        require(not self.pending and not self.uncertain, "api106-prior-io-unconfirmed")
        self.io_deadline(deadline)
        operation, error = (self.api.ReadFile(handle, FRAME.size, overlapped=True) if data is None
                            else self.api.WriteFile(handle, data, overlapped=True))
        self.pending.append(operation)
        handles = [operation.event] + ([] if process is None else [process._handle])
        if error == self.api.ERROR_IO_PENDING:
            while True:
                effective = self.io_deadline(deadline)
                wait = self.api.INFINITE if effective is None else max(0, int(remaining(effective) * 1000))
                if self.deadline_cell is not None:
                    wait = min(wait, CELL_POLL_MILLISECONDS)
                result = self.api.WaitForMultipleObjects(handles, False, wait)
                self.io_deadline(deadline)
                if result == self.api.WAIT_OBJECT_0:
                    break
                if result == self.api.WAIT_TIMEOUT:
                    if self.deadline_cell is not None:
                        continue
                    raise EnclosureExpired("api106-control-or-report-deadline")
                if process is not None and result == self.api.WAIT_OBJECT_0 + 1:
                    raise DiscoveryExited("api106-discovery-exited")
                raise OSError("api106-overlapped-wait")
        count, error = operation.GetOverlappedResult(False)
        require(error == 0, "api106-overlapped-operation")
        self.pending.remove(operation)
        self.io_deadline(deadline)
        if data is None:
            content = bytes(operation.getbuffer())
            require(count == len(content) == FRAME.size, "api106-control-size")
            return content
        require(count == len(data), "api106-short-write")

    def io_deadline(self, deadline):
        if self.deadline_cell is not None:
            window = self.deadline_cell.observe()
            if window is not None:
                deadline = window.work_deadline if deadline is None else min(deadline, window.work_deadline)
        if deadline is not None:
            before(deadline)
        return deadline

    def wait_case_close(self, cell, process):
        while not cell.accept_close():
            wait = min(CELL_POLL_MILLISECONDS, max(0, int(remaining(cell.window.work_deadline) * 1000)))
            result = self.api.WaitForSingleObject(process._handle, wait)
            if result == self.api.WAIT_OBJECT_0:
                raise DiscoveryExited("api106-discovery-exited")
            require(result == self.api.WAIT_TIMEOUT, "api106-cell-close-wait")

    def drain(self, deadline):
        uncertain, interrupted = False, None
        for operation in self.pending:
            try:
                operation.cancel()
                result = self.api.WaitForMultipleObjects(
                    [operation.event], False, max(0, int(remaining(deadline) * 1000)),
                )
                if result != self.api.WAIT_OBJECT_0:
                    uncertain = True
                    UNCONFIRMED_IO.append(operation)
                else:
                    _, status = operation.GetOverlappedResult(False)
                    if status not in (0, 995):  # ERROR_OPERATION_ABORTED confirms completed cancellation.
                        uncertain = True
                        UNCONFIRMED_IO.append(operation)
            except CLEANUP_FAILURES as error:
                uncertain = True
                UNCONFIRMED_IO.append(operation)
                if isinstance(error, (KeyboardInterrupt, SystemExit)):
                    interrupted = error
        self.pending.clear()
        self.uncertain = self.uncertain or uncertain
        if interrupted is not None:
            interrupted.add_note("API106 control I/O cancellation is unconfirmed")
            raise interrupted
        require(not uncertain, "api106-io-cancellation-unconfirmed")
        before(deadline)

    def members(self, job, process):
        class Members(ctypes.Structure):
            _fields_ = [("assigned", wintypes.DWORD), ("listed", wintypes.DWORD),
                        ("pids", ctypes.c_size_t * 16)]
        value, length = Members(), wintypes.DWORD()
        require(process.poll() is None and self.kernel.GetProcessId(process._handle) == process.pid,
                "api106-discovery-not-live")
        require(job.api.QueryInformationJobObject(job.handle, 3, ctypes.byref(value), ctypes.sizeof(value),
                                                  ctypes.byref(length)),
                "api106-members-unavailable")
        require(Members.pids.offset + ctypes.sizeof(ctypes.c_size_t) <= length.value <= ctypes.sizeof(value)
                and value.assigned == value.listed == 1 and value.pids[0] == process.pid
                and job.active_processes() == 1, "api106-unexpected-live-members")
        return 1


class OwnedDiscovery:
    def __init__(self, native):
        self.native = native
        self.command = self._handle = self.thread = self.pid = self.tid = None

    def poll(self):
        api = self.native.api
        if api.WaitForSingleObject(self._handle, 0) == api.WAIT_OBJECT_0:
            return api.GetExitCodeProcess(self._handle)
        return None

    def wait(self, deadline):
        api = self.native.api
        if api.WaitForSingleObject(self._handle, max(0, int(remaining(deadline) * 1000))) != api.WAIT_OBJECT_0:
            raise EnclosureExpired("api106-discovery-reap-unconfirmed")
        before(deadline)
        return api.GetExitCodeProcess(self._handle)

    def kill(self):
        if self.poll() is None:
            self.native.api.TerminateProcess(self._handle, 1)

    def close(self):
        try:
            if self.thread is not None:
                self.native.release(self.thread)
                self.thread = None
        finally:
            if self._handle is not None:
                self.native.release(self._handle)
                self._handle = None


class Receipt:
    def __init__(self, native):
        self.native, self.count, self.size = native, 0, 0
        path = ROOT / REPORT
        for component in (path.parent, *path.parent.parents):
            require(not component.is_symlink() and not component.is_junction(), "api106-linked-report")
        path.parent.mkdir(parents=True, exist_ok=True)
        # Append-only access, no overwrite and no delete sharing. Overlapped writes
        # must consume the same absolute deadline as control/termination.
        self.handle = native.api.CreateFile(
            str(path), 0x00000004, 1, 0, 1, 0x40000000 | 0x00200000, 0,
        )  # FILE_APPEND_DATA, FILE_SHARE_READ, CREATE_NEW, OVERLAPPED | OPEN_REPARSE_POINT

    def record(self, fields, deadline):
        data = encoded({"event": "api106_approved_shell_enclosure", "v": 1, "diagnostic_only": True, **fields}) + b"\n"
        require(self.count < 8 and len(data) <= 1024 and self.size + len(data) <= 8192,
                "api106-enclosure-report-limit")
        self.native.io(self.handle, data, deadline=deadline)
        self.count += 1
        self.size += len(data)

    def close(self):
        self.native.release(self.handle)


def cleanup(native, job, process, channel, receipt, deadline, *, cell=None, saved_errors=()):
    errors = list(saved_errors)
    if job is not None:
        try:
            job.cleanup_deadline = deadline
            job.close()
        except CLEANUP_FAILURES as error:
            errors.append(error)
    if process is not None and process._handle is not None:
        try:
            process.kill()
        except CLEANUP_FAILURES as error:
            errors.append(error)
        try:
            process.wait(deadline)
        except CLEANUP_FAILURES as error:
            errors.append(error)
    try:
        native.drain(deadline)
    except CLEANUP_FAILURES as error:
        errors.append(error)
    if channel is not None:
        try:
            channel.close()
        except CLEANUP_FAILURES as error:
            errors.append(error)
    if process is not None:
        try:
            process.close()
        except CLEANUP_FAILURES as error:
            errors.append(error)
    if cell is not None:
        try:
            cell.close()
        except CLEANUP_FAILURES as error:
            errors.append(error)
    if receipt is not None:
        try:
            receipt.record({"phase": "cleanup", "owned_tree_and_control_released": not errors and not native.uncertain,
                            "report_close_not_attested_by_this_record": True}, deadline)
        except CLEANUP_FAILURES as error:
            errors.append(error)
            try:
                native.drain(deadline)
            except CLEANUP_FAILURES as secondary:
                errors.append(secondary)
        try:
            receipt.close()
        except CLEANUP_FAILURES as error:
            errors.append(error)
    before(deadline)
    if errors:
        if isinstance(errors[0], (KeyboardInterrupt, SystemExit)):
            errors[0].add_note("API106 cleanup was interrupted; release is unconfirmed")
            raise errors[0]
        raise Refused("api106-owned-cleanup-unconfirmed") from errors[0]
    require(not native.uncertain, "api106-owned-cleanup-unconfirmed")


class DiscoveryOwner:
    """One cleanup guard, entered before the first owned resource is acquired."""

    def __init__(self, native):
        self.native = native
        self.job = self.process = self.channel = self.child = self.receipt = self.cell = self.protocol = None
        self.exit_deadline = self.cleanup_deadline = None

    def __enter__(self):
        return self

    def close_child(self):
        child, self.child = self.child, None
        if child is not None:
            try:
                child.close()
            except CLEANUP_FAILURES:
                self.native.uncertain = True
                raise

    def finish(self):
        errors = []
        if self.cell is not None and self.cell.address is not None:
            try:
                self.cell.observe()
            except CLEANUP_FAILURES as error:
                self.native.uncertain = True
                errors.append(error)
        window = self.cell.window if self.cell is not None else None
        if window is None and self.protocol is not None:
            window = self.protocol.window
        self.cleanup_deadline = (window.deadline if window is not None else
                                 self.exit_deadline if self.exit_deadline is not None else
                                 time.monotonic() if errors else time.monotonic() + 10)
        # Only finalization disables work monitoring; all cleanup shares this exact deadline.
        self.native.deadline_cell = None
        try:
            self.close_child()
        except CLEANUP_FAILURES as error:
            errors.append(error)
        cleanup(self.native, self.job, self.process, self.channel, self.receipt, self.cleanup_deadline,
                cell=self.cell, saved_errors=errors)

    def __exit__(self, error_type, error, traceback):
        try:
            self.finish()
        except CLEANUP_FAILURES as secondary:
            if error is not None:
                error.add_note("API106 outer cleanup is unconfirmed; no complete enclosure or inventory credit")
                if secondary is error:
                    raise error.with_traceback(traceback)
                if error.__cause__ is not None:
                    error.add_note("API106 additional cleanup failure: " + type(secondary).__name__)
                    raise error.with_traceback(traceback)
                raise error.with_traceback(traceback) from secondary
            raise


def supervise(owner, admission):
    native, job, process = owner.native, owner.job, owner.process
    channel, receipt, cell = owner.channel, owner.receipt, owner.cell
    owner.protocol = protocol = Protocol(admission.digest)
    native.resume(process, admission)
    while protocol.phase != "finished":
        deadline = protocol.window.work_deadline if protocol.window is not None else None
        data = native.io(channel.fileno(), deadline=deadline, process=process)
        kind, epoch, _, _ = unpack(data)
        published = cell.observe()
        if kind == ARM:
            require(published is not None and published.started == epoch, "api106-arm-not-published")
        kind, epoch, value = protocol.accept(data)
        deadline = protocol.window.work_deadline if protocol.window is not None else None
        if kind in (READY, ARM, LEAVE, DONE):
            native.members(job, process)
        if kind in (ARM, LEAVE, DONE):
            receipt.record({"phase": {ARM: "armed", LEAVE: "body-left", DONE: "case-closed"}[kind],
                            "elapsed_ms": int((time.monotonic() - epoch) * 1000),
                            "known_live_discovery_members": 1, "suite_job_retained": True}, deadline)
        if kind == DONE:
            cell.begin_close()
        native.io(channel.fileno(), packet(ACK, epoch, kind, admission.digest), deadline=deadline, process=process)
        if kind == DONE:
            native.wait_case_close(cell, process)
            protocol.close_case()
    # This is ordinary suite-exit teardown, not a restarted case allowance.
    owner.exit_deadline = time.monotonic() + 10
    exit_code = process.wait(owner.exit_deadline)
    require(exit_code == (0 if kind == SUITE_OK else 1), "api106-suite-exit-disagreement")
    suite_error = subprocess.CalledProcessError(exit_code, process.command) if exit_code else None
    try:
        receipt.record({"phase": "suite-exit", "exit_code": exit_code, "tests_run": protocol.ran,
                        "tests_discovered": protocol.discovered,
                        "inventory_complete": protocol.ran == protocol.discovered}, owner.exit_deadline)
    except CLEANUP_FAILURES as error:
        if suite_error is not None:
            raise suite_error from error
        raise
    if suite_error is not None:
        raise suite_error


def run_owning_suite():
    admission = Admission()
    native = Native()
    budget = admission.budget_module()
    with DiscoveryOwner(native) as owner:
        owner.job = budget.WindowsJob()
        from multiprocessing.connection import Pipe
        owner.channel, owner.child = Pipe(duplex=True)
        owner.cell = DeadlineCell(native)
        owner.cell.create()
        native.deadline_cell = owner.cell
        owner.receipt = Receipt(native)
        owner.receipt.record({"phase": "admitted", "binding_sha256": admission.digest.hex(),
                              "sources": len(admission.paths), "python": admission.snapshot["python"]["sha256"],
                              "work_seconds": WORK_SECONDS, "total_seconds": TOTAL_SECONDS,
                              "cleanup_reserve_seconds": CLEANUP_SECONDS}, time.monotonic() + 10)
        owner.process = OwnedDiscovery(native)
        native.create(admission, owner.child, owner.process, owner.cell)
        owner.close_child()
        owner.job.attach(owner.process)
        native.members(owner.job, owner.process)
        supervise(owner, admission)


class Client:
    def __init__(self, native, channel, admission, cell):
        self.native, self.channel, self.admission, self.cell = native, channel, admission, cell
        self.window, self.target, self.module = None, None, None
        self.entered, self.left = False, False

    def exchange(self, kind, value=0):
        epoch = self.window.started if self.window is not None else 0.0
        deadline = self.window.work_deadline if self.window is not None else None
        self.native.io(self.channel.fileno(), packet(kind, epoch, value, self.admission.digest), deadline=deadline)
        answer = self.native.io(self.channel.fileno(), deadline=deadline)
        require(answer == packet(ACK, epoch, kind, self.admission.digest), "api106-control-acknowledgment")

    def install(self, suite):
        def cases(item):
            if isinstance(item, unittest.TestSuite):
                for child in item:
                    yield from cases(child)
            else:
                yield item
        selected = [case for case in cases(suite) if case.id() == TARGET]
        require(len(selected) == 1, "api106-target-count")
        target = selected[0]
        module = sys.modules.get(target.__class__.__module__)
        require(module is not None and module.__name__ == "test_money_mutation"
                and Path(module.__file__).resolve() == ROOT / "scripts" / "tests" / "test_money_mutation.py"
                and getattr(module, "APPROVED_SHELL_PAIR_ENCLOSURE", object()) is None,
                "api106-target-module-or-seam")
        self.admission.verify()
        self.exchange(READY, suite.countTestCases())
        self.target, self.module = target, module
        module.APPROVED_SHELL_PAIR_ENCLOSURE = self.enclosure
        return suite

    def start(self, test):
        if test is self.target:
            require(self.window is None and not self.entered, "api106-target-reused")
            self.window = self.cell.publish()
            self.exchange(ARM)
            self.admission.verify()
            before(self.window.work_deadline)

    @contextmanager
    def enclosure(self, *, seconds, cleanup_reserve):
        require(type(seconds) is int and seconds == TOTAL_SECONDS and type(cleanup_reserve) is int
                and cleanup_reserve == CLEANUP_SECONDS and self.window is not None and not self.entered,
                "api106-enclosure-admission")
        self.entered = True
        self.exchange(ENTER)
        error, traceback = None, None
        try:
            yield self.window
        except BaseException as observed:
            error, traceback = observed, observed.__traceback__
        try:
            self.exchange(LEAVE, int(error is not None))
            self.left = True
        except CLEANUP_FAILURES as secondary:
            if error is not None:
                raise error.with_traceback(traceback) from secondary
            raise
        if error is not None:
            raise error.with_traceback(traceback)

    def stop(self, test):
        if test is self.target:
            try:
                require(self.entered and self.left, "api106-pair-not-completed")
            finally:
                self.module.APPROVED_SHELL_PAIR_ENCLOSURE = None
            self.exchange(DONE)
            before(self.window.work_deadline)
            self.cell.close_case()
            self.window = None


def run_discovery(client):
    class Loader(unittest.TestLoader):
        def discover(self, *args, **kwargs):
            return client.install(super().discover(*args, **kwargs))

    class Result(unittest.TextTestResult):
        def startTest(self, test):
            client.start(test)
            super().startTest(test)

        def stopTest(self, test):
            super().stopTest(test)
            client.stop(test)

    class Runner(unittest.TextTestRunner):
        resultclass = Result

    program = unittest.TestProgram(
        module=None, argv=["python -m unittest", "discover", "-s", "scripts/tests", "-v"],
        testLoader=Loader(), testRunner=Runner, exit=False,
    )
    result = program.result
    client.exchange(SUITE_OK if result.wasSuccessful() else SUITE_FAILED, result.testsRun)
    return 0 if result.wasSuccessful() else 1


def discovery(handle, deadline_handle, mutex_handle, digest):
    require(os.name == "nt" and type(handle) is int and handle > 0
            and type(deadline_handle) is int and deadline_handle > 0
            and type(mutex_handle) is int and mutex_handle > 0 and len({handle, deadline_handle, mutex_handle}) == 3
            and type(digest) is bytes and len(digest) == 32, "api106-bootstrap-arguments")
    from multiprocessing.connection import PipeConnection
    channel, native = PipeConnection(handle), Native()
    cell = DeadlineCell(native, publisher=True)
    client = None
    error, traceback, exit_code = None, None, None
    try:
        cell.handle, cell.mutex = deadline_handle, mutex_handle
        os.set_handle_inheritable(handle, False)
        os.set_handle_inheritable(deadline_handle, False)
        os.set_handle_inheritable(mutex_handle, False)
        cell.map()
        require(cell.read() == 0, "api106-cell-not-empty")
        admission = Admission()
        require(admission.digest == digest, "api106-bootstrap-binding")
        client = Client(native, channel, admission, cell)
        exit_code = run_discovery(client)
    except BaseException as observed:
        error, traceback = observed, observed.__traceback__
    finally:
        if client is not None and client.module is not None:
            client.module.APPROVED_SHELL_PAIR_ENCLOSURE = None
        window = client.window if client is not None and client.window is not None else cell.window
        deadline = window.deadline if window is not None else time.monotonic() + 10
        secondary = None
        for close in (lambda: native.drain(deadline), channel.close, cell.close):
            try:
                close()
            except CLEANUP_FAILURES as failure:
                if secondary is None:
                    secondary = failure
                else:
                    secondary.add_note("Another API106 discovery-side release is unconfirmed")
        if secondary is not None:
            if error is not None:
                raise error.with_traceback(traceback) from secondary
            raise secondary
    if error is not None:
        raise error.with_traceback(traceback)
    return exit_code


if __name__ == "__main__":
    raise SystemExit("API106 discovery is admitted only by quality.py --api106-observer-pair")
