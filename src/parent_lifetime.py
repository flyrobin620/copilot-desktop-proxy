"""Bind a worker to the exact Windows supervisor process handle, not a reusable PID."""
import ctypes
import os
import threading


def watch_parent(pid, server, log):
    if os.name != 'nt' or not pid:
        return None
    from ctypes import wintypes
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE; never terminate by PID.
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    stopped = threading.Event()

    def watch():
        try:
            while not stopped.is_set():
                result = kernel.WaitForSingleObject(handle, 500)
                if result == 0:
                    log.event('supervisor_lost', supervisor_pid=pid)
                    server.shutdown()
                    return
                if result != 258:  # WAIT_TIMEOUT
                    log.event('parent_monitor_failed', winerror=ctypes.get_last_error())
                    server.shutdown()
                    return
        finally:
            kernel.CloseHandle(handle)

    thread = threading.Thread(target=watch, name='supervisor-lifetime', daemon=True)
    thread.start()
    return stopped, thread
