"""Bound only the launched process tree, never a shared service or process name."""

import ctypes
import math
import os
import signal
import subprocess
import sys
import time
from ctypes import wintypes


# The bootstrap cannot spawn the real command until its Job Object is attached.
WINDOWS_BOOTSTRAP = (
    "import subprocess,sys; "
    "sys.exit(subprocess.call(sys.argv[1:]) if sys.stdin.buffer.read(1) == b'1' else 1)"
)


def launch_command(command):
    return [sys.executable, "-c", WINDOWS_BOOTSTRAP, *command] if os.name == "nt" else command


class BudgetExceeded(TimeoutError):
    def __init__(self, output):
        super().__init__("Owned process tree exceeded its enforced elapsed budget")
        self.output = output


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


def run(command, root, seconds, capture=False):
    if isinstance(seconds, bool) or not math.isfinite(seconds) or seconds <= 0:
        raise ValueError("Process budget must be a positive finite number")
    started = time.monotonic()
    job = WindowsJob() if os.name == "nt" else None
    process = None
    try:
        process = subprocess.Popen(
            launch_command(command), cwd=root, text=True, encoding="utf-8", errors="replace",
            stdin=subprocess.PIPE if job is not None else None,
            stdout=subprocess.PIPE if capture else None,
            stderr=subprocess.STDOUT if capture else None,
            start_new_session=os.name != "nt",
        )
        if job is not None:
            job.attach(process)
        try:
            output, _ = process.communicate(
                input="1" if job is not None else None,
                timeout=max(0, seconds - (time.monotonic() - started)),
            )
        except subprocess.TimeoutExpired:
            if job is not None:
                job.close()
            else:
                os.killpg(process.pid, signal.SIGKILL)
            output, _ = process.communicate(timeout=10)
            raise BudgetExceeded(output) from None
        if time.monotonic() - started > seconds:
            raise BudgetExceeded(output)
        return subprocess.CompletedProcess(command, process.returncode, output)
    finally:
        if job is not None:
            job.close()
        if process is not None:
            if os.name != "nt":
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            elif process.poll() is None:
                process.kill()
            process.wait(timeout=10)
