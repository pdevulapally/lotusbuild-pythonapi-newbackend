"""OS containment for our newly spawned child, never a PID discovered by scanning."""
import ctypes
import os
import signal
import sys
import time


class WindowsJob:
    """Non-inheritable, unnamed job: closing its sole handle kills its members."""
    def __init__(self, process):
        from ctypes import wintypes as w

        class Basic(ctypes.Structure):
            _fields_ = [("user", ctypes.c_int64), ("job", ctypes.c_int64), ("flags", w.DWORD),
                        ("min_ws", ctypes.c_size_t), ("max_ws", ctypes.c_size_t),
                        ("active", w.DWORD), ("affinity", ctypes.c_size_t),
                        ("priority", w.DWORD), ("scheduling", w.DWORD)]

        class IO(ctypes.Structure):
            _fields_ = [(name, ctypes.c_uint64) for name in
                        ("read_ops", "write_ops", "other_ops", "read_bytes", "write_bytes", "other_bytes")]

        class Extended(ctypes.Structure):
            _fields_ = [("basic", Basic), ("io", IO), ("process_memory", ctypes.c_size_t),
                        ("job_memory", ctypes.c_size_t), ("peak_process", ctypes.c_size_t),
                        ("peak_job", ctypes.c_size_t)]

        class Accounting(ctypes.Structure):
            _fields_ = [(name, ctypes.c_int64) for name in ("user", "kernel", "period_user", "period_kernel")] + [
                (name, w.DWORD) for name in ("faults", "total", "active", "terminated")]

        class Members(ctypes.Structure):
            _fields_ = [("assigned", w.DWORD), ("count", w.DWORD), ("ids", ctypes.c_size_t * 1024)]

        self.api = ctypes.WinDLL("kernel32", use_last_error=True)
        self.api.CreateJobObjectW.argtypes = [ctypes.c_void_p, w.LPCWSTR]
        self.api.CreateJobObjectW.restype = w.HANDLE
        self.api.SetInformationJobObject.argtypes = [w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD]
        self.api.SetInformationJobObject.restype = w.BOOL
        self.api.AssignProcessToJobObject.argtypes = [w.HANDLE, w.HANDLE]
        self.api.AssignProcessToJobObject.restype = w.BOOL
        self.api.CloseHandle.argtypes = [w.HANDLE]
        self.api.CloseHandle.restype = w.BOOL
        self.api.TerminateJobObject.argtypes = [w.HANDLE, w.UINT]
        self.api.TerminateJobObject.restype = w.BOOL
        self.api.QueryInformationJobObject.argtypes = [w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD, ctypes.c_void_p]
        self.api.QueryInformationJobObject.restype = w.BOOL
        self.api.OpenProcess.argtypes, self.api.OpenProcess.restype = [w.DWORD, w.BOOL, w.DWORD], w.HANDLE
        self.api.IsProcessInJob.argtypes, self.api.IsProcessInJob.restype = [w.HANDLE, w.HANDLE, ctypes.POINTER(w.BOOL)], w.BOOL
        self.api.WaitForSingleObject.argtypes, self.api.WaitForSingleObject.restype = [w.HANDLE, w.DWORD], w.DWORD
        self.accounting, self.members, self.boolean = Accounting, Members, w.BOOL
        self.handle = self.api.CreateJobObjectW(None, None)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        info = Extended()
        info.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE; no breakaway.
        try:
            if not self.api.SetInformationJobObject(self.handle, 9, ctypes.byref(info), ctypes.sizeof(info)):
                raise ctypes.WinError(ctypes.get_last_error())
            # multiprocessing's Windows sentinel is the owned process handle.
            if not self.api.AssignProcessToJobObject(self.handle, process.sentinel):
                raise ctypes.WinError(ctypes.get_last_error())
        except BaseException:
            self.close()
            raise

    def close(self):
        if self.handle:
            handles = []
            try:
                members = self.members()
                if not self.api.QueryInformationJobObject(self.handle, 3, ctypes.byref(members), ctypes.sizeof(members), None):
                    raise ctypes.WinError(ctypes.get_last_error())
                if members.assigned > members.count:
                    raise RuntimeError("owned process list exceeds confirmation bound")
                for pid in members.ids[:members.count]:
                    handle = self.api.OpenProcess(0x100000 | 0x0400, False, pid)
                    if not handle:
                        if ctypes.get_last_error() == 87:  # Already exited.
                            continue
                        raise ctypes.WinError(ctypes.get_last_error())
                    belongs = self.boolean()
                    if not self.api.IsProcessInJob(handle, self.handle, ctypes.byref(belongs)):
                        self.api.CloseHandle(handle)
                        raise ctypes.WinError(ctypes.get_last_error())
                    if belongs.value:
                        handles.append(handle)  # Stable identity, no reused-PID termination.
                    else:
                        self.api.CloseHandle(handle)
                if not self.api.TerminateJobObject(self.handle, 73):
                    raise ctypes.WinError(ctypes.get_last_error())
                until = time.monotonic() + 2
                for handle in handles:
                    remaining = max(0, int((until - time.monotonic()) * 1000))
                    if self.api.WaitForSingleObject(handle, remaining) != 0:
                        raise RuntimeError("owned process termination unconfirmed")
                while True:
                    accounting = self.accounting()
                    if not self.api.QueryInformationJobObject(self.handle, 1, ctypes.byref(accounting), ctypes.sizeof(accounting), None):
                        raise ctypes.WinError(ctypes.get_last_error())
                    if accounting.active == 0:
                        break
                    if time.monotonic() >= until:
                        raise RuntimeError("owned process tree termination unconfirmed")
                    time.sleep(0.02)
            finally:
                for handle in handles:
                    self.api.CloseHandle(handle)
                # Kill-on-close still applies if confirmation/querying failed.
                if not self.api.CloseHandle(self.handle):
                    raise ctypes.WinError(ctypes.get_last_error())
                self.handle = None


def contain(process):
    if os.name == "nt":
        return WindowsJob(process)
    if sys.platform != "linux":
        raise RuntimeError("execution containment unsupported on this platform")
    return None  # Linux child installs parent-death protection before ready.


def protect_parent_death(parent_pid):
    if sys.platform == "linux":
        libc = ctypes.CDLL(None, use_errno=True)
        if libc.prctl(1, signal.SIGKILL, 0, 0, 0) != 0:  # PR_SET_PDEATHSIG
            raise OSError(ctypes.get_errno(), "parent-death protection failed")
        if os.getppid() != parent_pid:  # Parent may have died before prctl.
            os._exit(72)
