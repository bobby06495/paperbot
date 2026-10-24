"""Process checks and keep-awake, per operating system.

pid_alive(pid)   True if the process exists. Never signals it (on Windows
                 os.kill(pid, 0) would terminate the process, so the Win32
                 API is queried instead).
keep_awake()     context manager: the computer does not go to sleep while the
                 bot runs. Windows: SetThreadExecutionState for this process
                 only (no power settings are changed); macOS: caffeinate;
                 Linux and servers: nothing to do.
"""
import contextlib
import os
import subprocess
import sys

from paperbot.core.common import log


def pid_alive(pid: int) -> bool:
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return ctypes.get_last_error() == 5  # access denied: exists, owned by someone else
        try:
            code = wintypes.DWORD()
            ok = kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
            return bool(ok) and code.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


@contextlib.contextmanager
def keep_awake():
    """Keep the computer awake while the block runs; never fails the caller."""
    caffeinate = None
    try:
        if sys.platform == "win32":
            import ctypes
            ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001
            ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)
        elif sys.platform == "darwin":
            caffeinate = subprocess.Popen(["caffeinate", "-i", "-w", str(os.getpid())])
    except Exception as e:  # noqa: BLE001 - staying awake is a convenience
        log.warning("keep-awake not available: %r", e)
    try:
        yield
    finally:
        try:
            if sys.platform == "win32":
                import ctypes
                ctypes.windll.kernel32.SetThreadExecutionState(0x80000000)  # back to normal
            elif caffeinate is not None:
                caffeinate.terminate()
        except Exception:  # noqa: BLE001
            pass
