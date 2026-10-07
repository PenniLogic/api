"""Bound only the launched process tree, never a shared service or process name."""

import ctypes
import hashlib
import math
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
            job.close()
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
            raise BudgetExceeded(stdout, stderr, streams)
        return subprocess.CompletedProcess(command, process.returncode, stdout, stderr if separate else None)
    finally:
        if captured is not None:
            captured.stop.set()
        if job is not None:
            job.close()
        if process is not None:
            terminate()
            process.wait(timeout=10)
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream is not None:
                    stream.close()
