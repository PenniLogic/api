"""Bound only the launched process tree, never a shared service or process name."""

import ctypes
import hashlib
import json
import math
import ntpath
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time
from ctypes import wintypes


# The bootstrap cannot spawn the real command until its Job Object is attached.
WINDOWS_BOOTSTRAP = (
    "import subprocess,sys; "
    "sys.exit(subprocess.call(sys.argv[1:]) if sys.stdin.buffer.read(1) == b'1' else 1)"
)


def launch_command(command):
    return [sys.executable, "-I", "-S", "-B", "-c", WINDOWS_BOOTSTRAP, *command] if os.name == "nt" else command


MAX_CAPTURE_BYTES = 4 * 1024 * 1024
JOB_STATE_PREFIX = "process_budget_job_state="
JOB_STATE_SCHEMA = "pennilogic.process-budget-job-state/1"
JOB_STATE_UNAVAILABLE = (
    JOB_STATE_PREFIX + '{"accounting":{"status":"unavailable"},"members":{"status":"unavailable"},'
    '"schema":"' + JOB_STATE_SCHEMA + '"}'
)
MAX_JOB_MEMBERS = 16
MAX_JOB_IMAGE_CHARS = 32768
ENVIRONMENT_KEYS = {
    "HOME", "USERPROFILE", "TEMP", "TMP", "TMPDIR", "LANG", "LC_ALL",
    "JAVA_HOME", "GRADLE_USER_HOME", "CI", "GITHUB_ACTIONS",
    "SYSTEMROOT", "WINDIR", "COMSPEC", "PATH", "PENNILOGIC_PYTHON",
}


def system_paths():
    if os.name != "nt":
        return [Path("/usr/local/bin"), Path("/usr/bin"), Path("/bin")]
    api = ctypes.WinDLL("kernel32", use_last_error=True)
    api.GetSystemDirectoryW.argtypes = [wintypes.LPWSTR, wintypes.UINT]
    api.GetSystemDirectoryW.restype = wintypes.UINT
    buffer = ctypes.create_unicode_buffer(32768)
    length = api.GetSystemDirectoryW(buffer, len(buffer))
    if not 0 < length < len(buffer):
        raise OSError("Cannot resolve the approved Windows system directory")
    system = Path(buffer.value)
    return [system, system / "WindowsPowerShell" / "v1.0", system / "Wbem"]


def environment(supplied=None, *, runtime_dirs=()):
    supplied = os.environ if supplied is None else supplied
    result = {key: value for key, value in supplied.items()
              if key in ENVIRONMENT_KEYS - {"PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "PENNILOGIC_PYTHON"}}
    paths = [Path(sys.executable).absolute().parent, Path(sys.base_prefix) / "Scripts", *runtime_dirs]
    if result.get("JAVA_HOME"):
        home = Path(result["JAVA_HOME"])
        if not home.is_absolute():
            raise ValueError("Approved JAVA_HOME must be absolute")
        paths.append(home / "bin")
    system = system_paths()
    paths.extend(system)
    if any(not Path(path).is_absolute() for path in paths):
        raise ValueError("Approved runtime directories must be absolute")
    result["PATH"] = os.pathsep.join(dict.fromkeys(str(path) for path in paths))
    result["PENNILOGIC_PYTHON"] = str(Path(sys.executable).absolute())
    if os.name == "nt":
        result.update(SYSTEMROOT=str(system[0].parent), WINDIR=str(system[0].parent),
                      COMSPEC=str(system[0] / "cmd.exe"))
    return result


def resolve_command(command, child_environment):
    command = [str(part) for part in command]
    if not command:
        raise ValueError("Owned command must not be empty")
    if not Path(command[0]).is_absolute():
        if Path(command[0]).name != command[0]:
            raise ValueError("Owned command path must be absolute")
        suffixes = ("", ".exe", ".cmd", ".bat") if os.name == "nt" else ("",)
        candidates = (Path(folder) / (command[0] + suffix)
                      for folder in child_environment["PATH"].split(os.pathsep) for suffix in suffixes)
        match = next((path for path in candidates if path.is_file()), None)
        if match is None:
            raise FileNotFoundError("Approved command executable is missing")
        command[0] = str(match)
    if not Path(command[0]).is_file():
        raise FileNotFoundError("Approved command executable is missing")
    return command


class BudgetExceeded(TimeoutError):
    def __init__(self, output, stderr=None, streams=None):
        super().__init__("Owned process tree exceeded its enforced elapsed budget")
        self.output = output
        self.stderr = stderr
        self.streams = streams


class OutputExceeded(BudgetExceeded):
    def __init__(self, output, stderr, streams):
        super().__init__(output, stderr, streams)
        self.args = ("Owned process tree exceeded its enforced output budget",)


def budget_observation(process, captured, started, waiting):
    schema = "pennilogic.process-budget-state/1"
    try:
        code = process.poll()
        if code is not None and (type(code) is not int or not -2147483648 <= code <= 4294967295):
            raise ValueError("Invalid process state")
        readers = [] if captured is None else captured.threads
        if type(readers) is not list or not 0 <= len(readers) <= 2:
            raise ValueError("Invalid capture state")
        alive = [reader.is_alive() for reader in readers]
        if any(type(value) is not bool for value in alive):
            raise ValueError("Invalid capture state")
        observed = time.monotonic()
        if (any(type(value) not in (int, float) or not math.isfinite(value)
                for value in (started, waiting, observed)) or not started <= waiting <= observed):
            raise ValueError("Invalid elapsed state")
        elapsed_ms, setup_ms = int((observed - started) * 1000), int((waiting - started) * 1000)
        if not 0 <= setup_ms <= elapsed_ms <= 86400000:
            raise ValueError("Invalid elapsed state")
        state = {
            "schema": schema, "status": "observed", "process_running": code is None, "returncode": code,
            "capture_readers": len(readers), "capture_readers_alive": sum(alive),
            "setup_ms": setup_ms, "wait_ms": elapsed_ms - setup_ms, "elapsed_ms": elapsed_ms,
        }
    except (OSError, RuntimeError, AttributeError, TypeError, ValueError, OverflowError):
        state = {"schema": schema, "status": "unavailable"}
    return "process_budget_state=" + json.dumps(state, sort_keys=True, separators=(",", ":"))


class Capture:
    def __init__(self, process):
        self.data = {"stdout": bytearray(), "stderr": bytearray()}
        self.counts = {"stdout": 0, "stderr": 0}
        self.hashes = {name: hashlib.sha256() for name in self.data}
        self.lock, self.changed, self.stop = threading.Lock(), threading.Event(), threading.Event()
        self.exceeded, self.failed = False, False
        self.threads = []
        for name, stream in (("stdout", process.stdout), ("stderr", process.stderr)):
            if stream is not None:
                thread = threading.Thread(target=self.read, args=(name, stream), daemon=True)
                self.threads.append(thread)
                thread.start()

    def read(self, name, stream):
        try:
            while not self.stop.is_set():
                part = stream.read(65536)
                if not part:
                    break
                with self.lock:
                    if self.stop.is_set():
                        break
                    self.counts[name] += len(part)
                    self.hashes[name].update(part)
                    available = min(MAX_CAPTURE_BYTES - len(self.data[name]),
                                    MAX_CAPTURE_BYTES - sum(map(len, self.data.values())))
                    self.data[name].extend(part[:available])
                    if self.counts[name] > MAX_CAPTURE_BYTES or sum(self.counts.values()) > MAX_CAPTURE_BYTES:
                        self.exceeded = True
                        self.stop.set()
                self.changed.set()
        except (OSError, ValueError):
            if not self.stop.is_set():
                self.failed = True
        finally:
            self.changed.set()

    def snapshot(self, separate):
        with self.lock:
            stdout, stderr = bytes(self.data["stdout"]), bytes(self.data["stderr"])
            streams = {name: {"bytes": self.counts[name], "sha256": self.hashes[name].hexdigest()}
                       for name in self.data}
        return (stdout if separate else stdout.decode("utf-8", errors="replace")), stderr, streams


class WindowsJob:
    def __init__(self):
        class BasicLimits(ctypes.Structure):
            _fields_ = [
                ("process_time", ctypes.c_int64), ("job_time", ctypes.c_int64),
                ("flags", wintypes.DWORD), ("minimum", ctypes.c_size_t),
                ("maximum", ctypes.c_size_t), ("processes", wintypes.DWORD),
                ("affinity", ctypes.c_size_t), ("priority", wintypes.DWORD),
                ("scheduling", wintypes.DWORD),
            ]

        class ExtendedLimits(ctypes.Structure):
            _fields_ = [
                ("basic", BasicLimits), ("io", ctypes.c_uint64 * 6),
                ("process_memory", ctypes.c_size_t), ("job_memory", ctypes.c_size_t),
                ("peak_process_memory", ctypes.c_size_t), ("peak_job_memory", ctypes.c_size_t),
            ]

        self.api = ctypes.WinDLL("kernel32", use_last_error=True)
        self.api.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        self.api.CreateJobObjectW.restype = wintypes.HANDLE
        self.api.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        self.api.SetInformationJobObject.restype = wintypes.BOOL
        self.api.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        self.api.AssignProcessToJobObject.restype = wintypes.BOOL
        self.api.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        self.api.OpenProcess.restype = wintypes.HANDLE
        self.api.CloseHandle.argtypes = [wintypes.HANDLE]
        self.api.CloseHandle.restype = wintypes.BOOL
        self.handle = self.api.CreateJobObjectW(None, None)
        if not self.handle:
            raise OSError("Cannot create the owned process budget job")
        limits = ExtendedLimits()
        limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not self.api.SetInformationJobObject(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            self.close()
            raise OSError("Cannot enforce the owned process budget job")

    def attach(self, process):
        handle = self.api.OpenProcess(0x0101, False, process.pid)
        if not handle:
            raise OSError("Cannot open the launched process for its budget job")
        try:
            if not self.api.AssignProcessToJobObject(self.handle, handle):
                raise OSError("Cannot attach the launched process to its budget job")
        finally:
            self.api.CloseHandle(handle)

    def observation(self, process, command, child_environment):
        errors = (OSError, RuntimeError, AttributeError, TypeError, ValueError, OverflowError, ctypes.ArgumentError)

        class Accounting(ctypes.Structure):
            _fields_ = [
                ("user_time", ctypes.c_int64), ("kernel_time", ctypes.c_int64),
                ("period_user_time", ctypes.c_int64), ("period_kernel_time", ctypes.c_int64),
                ("page_faults", ctypes.c_uint32), ("total", ctypes.c_uint32),
                ("active", ctypes.c_uint32), ("terminated", ctypes.c_uint32),
            ]

        class Members(ctypes.Structure):
            _fields_ = [
                ("assigned", ctypes.c_uint32), ("listed", ctypes.c_uint32),
                ("pids", ctypes.c_size_t * MAX_JOB_MEMBERS),
            ]

        def function(name, arguments, result, *, provider=None):
            value = getattr(self.api if provider is None else provider, name)
            value.argtypes, value.restype = arguments, result
            return value

        def read(kind, value):
            returned = ctypes.c_uint32()
            if not query(self.handle, kind, ctypes.byref(value), ctypes.sizeof(value), ctypes.byref(returned)):
                code = ctypes.get_last_error()
                status = ("oversized" if kind == 3 and code == 234 else
                          "unsupported" if code in (50, 120) else "unavailable")
                return status, None
            return "observed", returned.value

        def accounting():
            try:
                value = Accounting()
                status, length = read(1, value)
                if status != "observed":
                    return {"status": status}
                if length != ctypes.sizeof(value) or value.active > value.total:
                    return {"status": "unavailable"}
                return {"status": "observed", "total_assigned": value.total, "active": value.active}
            except errors:
                return {"status": "unavailable"}

        def image_key(value):
            if type(value) is not str or "\0" in value:
                return None
            value = value.replace("/", "\\")
            drive, tail = ntpath.splitdrive(value)
            if (len(drive) != 2 or drive[0] not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
                    or drive[1] != ":" or not tail.startswith("\\")):
                return None
            parts = tail[1:].split("\\")
            if any(not part or part in (".", "..") or part.endswith((" ", ".")) or "~" in part for part in parts):
                return None
            return ntpath.normcase(value)

        def members():
            try:
                value = Members()
                status, length = read(3, value)
                if status != "observed":
                    return {"status": status}
                if not Members.pids.offset <= length <= ctypes.sizeof(value):
                    return {"status": "unavailable"}
                if max(value.assigned, value.listed) > MAX_JOB_MEMBERS:
                    return {"status": "oversized"}
                if (value.assigned < value.listed
                        or length < Members.pids.offset + value.listed * ctypes.sizeof(ctypes.c_size_t)):
                    return {"status": "unavailable"}
                pids = list(value.pids[:value.listed])
                if len(set(pids)) != len(pids) or any(not 0 < pid <= 4294967295 for pid in pids):
                    return {"status": "unavailable"}
                counts = {"bootstrap": 0, "system_approved_shell": 0, "other": 0, "unclassified": 0}
                if pids:
                    try:
                        open_process = function("OpenProcess", [ctypes.c_uint32, ctypes.c_int32, ctypes.c_uint32],
                                                wintypes.HANDLE)
                        in_job = function("IsProcessInJob", [wintypes.HANDLE, wintypes.HANDLE,
                                                            ctypes.POINTER(ctypes.c_int32)], ctypes.c_int32)
                        comparison_api = ctypes.WinDLL(
                            "KernelBase.dll", use_last_error=True, winmode=0x800,  # LOAD_LIBRARY_SEARCH_SYSTEM32
                        )
                        same_object = function("CompareObjectHandles", [wintypes.HANDLE, wintypes.HANDLE],
                                               ctypes.c_int32, provider=comparison_api)
                        image_name = function("QueryFullProcessImageNameW", [
                            wintypes.HANDLE, ctypes.c_uint32, wintypes.LPWSTR, ctypes.POINTER(ctypes.c_uint32),
                        ], ctypes.c_int32)
                        close = function("CloseHandle", [wintypes.HANDLE], ctypes.c_int32)
                    except AttributeError:
                        return {"status": "unsupported"}
                    bootstrap = process._handle
                    requested = image_key(command[0] if command else None)
                    comspec = image_key(child_environment.get("COMSPEC"))
                    approved = None
                    if comspec is not None:
                        powershell = image_key(ntpath.join(
                            ntpath.dirname(comspec), "WindowsPowerShell", "v1.0", "powershell.exe",
                        ))
                        if requested in (comspec, powershell):
                            approved = requested
                    buffer = ctypes.create_unicode_buffer(MAX_JOB_IMAGE_CHARS)

                    def owned(handle):
                        belongs = ctypes.c_int32()
                        return bool(in_job(handle, self.handle, ctypes.byref(belongs)) and belongs.value == 1)

                    for pid in pids:
                        handle, role = None, "unclassified"
                        try:
                            handle = open_process(0x1000, False, pid)
                            if handle and owned(handle):
                                candidate = "unclassified"
                                if bootstrap and same_object(handle, bootstrap):
                                    candidate = "bootstrap"
                                elif bootstrap and requested is not None and comspec is not None:
                                    size = ctypes.c_uint32(len(buffer))
                                    buffer.value = ""
                                    if (image_name(handle, 0, buffer, ctypes.byref(size))
                                            and 0 < size.value < len(buffer) and buffer[size.value] == "\0"):
                                        image = image_key(buffer[:size.value])
                                        if image is not None:
                                            candidate = "system_approved_shell" if image == approved else "other"
                                if owned(handle):
                                    role = candidate
                        except errors:
                            role = "unclassified"
                        finally:
                            if handle:
                                try:
                                    if not close(handle):
                                        role = "unclassified"
                                except errors:
                                    role = "unclassified"
                        counts[role] += 1
                if counts["bootstrap"] > 1:
                    return {"status": "unavailable"}
                return {
                    "status": "partial" if counts["unclassified"] or value.assigned != value.listed else "observed",
                    "listed": value.listed, **counts,
                }
            except errors:
                return {"status": "unavailable"}

        try:
            if not self.handle:
                return JOB_STATE_UNAVAILABLE
            state = {"schema": JOB_STATE_SCHEMA}
            try:
                query = function("QueryInformationJobObject", [
                    wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint32),
                ], ctypes.c_int32)
            except AttributeError:
                state.update(accounting={"status": "unsupported"}, members={"status": "unsupported"})
            else:
                state.update(accounting=accounting(), members=members())
                if (state["accounting"]["status"] == "observed" and state["members"]["status"] == "observed"
                        and state["accounting"]["active"] != state["members"]["listed"]):
                    state["members"]["status"] = "partial"
            note = JOB_STATE_PREFIX + json.dumps(state, sort_keys=True, separators=(",", ":"), allow_nan=False)
            if len(note.encode("ascii")) > 512:
                return JOB_STATE_UNAVAILABLE
            return note
        except errors:
            return JOB_STATE_UNAVAILABLE

    def close(self):
        if self.handle:
            if not self.api.CloseHandle(self.handle):
                raise OSError("Cannot close the owned process budget job")
            self.handle = None


def run(command, root, seconds, capture=False, *, env=None, separate=False, inherit_tree=False):
    if isinstance(seconds, bool) or not math.isfinite(seconds) or seconds <= 0:
        raise ValueError("Process budget must be a positive finite number")
    started = time.monotonic()
    child_environment = environment(env, runtime_dirs=(
        Path(part) for part in (env or {}).get("PATH", "").split(os.pathsep) if part
    ))
    command = resolve_command(command, child_environment)
    job = WindowsJob() if os.name == "nt" else None
    process, captured = None, None

    def terminate():
        if job is not None:
            try:
                job.close()
            finally:
                if process.poll() is None:
                    process.kill()
        elif inherit_tree:
            if process.poll() is None:
                process.kill()
        else:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    try:
        process = subprocess.Popen(
            launch_command(command), cwd=root, env=child_environment, bufsize=0,
            stdin=subprocess.PIPE if job is not None else None,
            stdout=subprocess.PIPE if capture else None,
            stderr=(subprocess.PIPE if separate else subprocess.STDOUT) if capture else None,
            start_new_session=os.name != "nt" and not inherit_tree,
        )
        if job is not None:
            job.attach(process)
        captured = Capture(process) if capture else None
        if job is not None:
            process.stdin.write(b"1")
            process.stdin.close()
        waiting = time.monotonic()
        failure = None
        while process.poll() is None or (captured and any(thread.is_alive() for thread in captured.threads)):
            remaining = seconds - (time.monotonic() - started)
            if captured and (captured.exceeded or captured.failed):
                failure = "output" if captured.exceeded else "capture"
                break
            if remaining <= 0:
                failure = "elapsed"
                break
            if captured:
                captured.changed.wait(min(0.025, remaining))
                captured.changed.clear()
            else:
                try:
                    process.wait(timeout=remaining)
                except subprocess.TimeoutExpired:
                    failure = "elapsed"
                    break
        if captured and captured.exceeded:
            failure = "output"
        if captured and captured.failed:
            failure = "capture"
        if failure is None and time.monotonic() - started > seconds:
            failure = "elapsed"
        if failure is not None:
            if failure == "elapsed":
                observation = budget_observation(process, captured, started, waiting)
                job_observation = job.observation(process, command, child_environment) if job is not None else None
            terminate()
            if captured:
                if failure != "elapsed":
                    captured.stop.set()
                for thread in captured.threads:
                    thread.join(timeout=0.1 if inherit_tree and os.name != "nt" else 10)
        stdout, stderr, streams = captured.snapshot(separate) if captured else (None, None, None)
        if failure == "output":
            raise OutputExceeded(stdout, stderr, streams)
        if failure == "capture":
            raise OSError("Owned process output capture failed")
        if failure == "elapsed":
            error = BudgetExceeded(stdout, stderr, streams)
            if job_observation is not None:
                error.add_note(job_observation)
            error.add_note(observation)
            raise error
        return subprocess.CompletedProcess(command, process.returncode, stdout, stderr if separate else None)
    finally:
        if captured is not None:
            captured.stop.set()
        try:
            if process is not None:
                try:
                    terminate()
                finally:
                    process.wait(timeout=10)
            elif job is not None:
                job.close()
        finally:
            if process is not None:
                for stream in (process.stdin, process.stdout, process.stderr):
                    if stream is not None:
                        stream.close()
