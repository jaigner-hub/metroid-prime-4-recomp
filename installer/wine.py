"""The setup under Wine or Proton (a Steam Deck, a Linux desktop): what to log about it, and running a tool so that
Wine's own messages reach the tool's log.

Wine writes its own messages (a failed assertion in ntdll, a missing library, a crash in its Unix half) to the Linux
stderr the whole process tree inherited from Steam or the terminal, not to the Windows stderr of the program that
caused them. A tool run with a Windows pipe for its output then dies leaving an empty log and only an exit code
(issue #11: gen.py, exit code 0x80000100 and nothing else). A Linux program started from inside Wine gets real file
descriptors, so start_tool runs the tool through /bin/sh with both streams sent to the log file. Wine starts the Linux
program but hands back no Windows handle for it (CreateProcess fails with ERROR_INVALID_HANDLE after the program has
started), so the tool tells its Windows process id through a file (PID_ENV, read by extract.RUN_SCRIPT) and Tool waits
on that process; /bin/sh records the exit status in a second file for a tool that ends before it could be opened.

Nothing here runs on Windows: info() is None there, and steps.run_tool keeps its pipe.
"""
import ctypes
import os
import subprocess
from pathlib import Path

PID_ENV = 'MP4_TOOL_PID_FILE'

# Environment variables worth logging: the Proton build (its folder is in STEAM_COMPAT_TOOL_PATHS), the Steam runtime
# container, Wine's debug and DLL settings. A fixed list, so nothing else from the environment reaches the log.
LOGGED_ENV = ('STEAM_COMPAT_TOOL_PATHS', 'SteamAppId', 'SteamGameId', 'SteamDeck', 'PRESSURE_VESSEL_RUNTIME',
              'PROTON_VERSION', 'PROTON_LOG', 'PROTON_USE_WINED3D', 'WINEDEBUG', 'WINEDLLOVERRIDES', 'WINEESYNC',
              'WINEFSYNC', 'WINE_LARGE_ADDRESS_AWARE')

SYNCHRONIZE = 0x00100000
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
WAIT_OBJECT_0 = 0
STILL_ACTIVE = 259


def _ntdll():
    if os.name != 'nt':
        return None
    try:
        nt = ctypes.WinDLL('ntdll')
        nt.wine_get_version.restype = ctypes.c_char_p
        return nt
    except (OSError, AttributeError):
        return None


def info():
    """None on Windows. Under Wine: {'version', 'build', 'host', 'memory', 'env'}, for the setup log."""
    nt = _ntdll()
    if nt is None:
        return None
    out = {'version': nt.wine_get_version().decode(errors='replace')}
    try:
        nt.wine_get_build_id.restype = ctypes.c_char_p
        out['build'] = nt.wine_get_build_id().decode(errors='replace')
    except AttributeError:
        pass
    try:
        sysname, release = ctypes.c_char_p(), ctypes.c_char_p()
        nt.wine_get_host_version.restype = None
        nt.wine_get_host_version(ctypes.byref(sysname), ctypes.byref(release))
        out['host'] = f'{sysname.value.decode(errors="replace")} {release.value.decode(errors="replace")}'
    except AttributeError:
        pass
    out['memory'] = _meminfo()
    out['env'] = {k: os.environ[k] for k in LOGGED_ENV if k in os.environ}
    return out


def _meminfo():
    """MemTotal, MemAvailable and SwapTotal of the Linux host in GiB, read through Wine's Z: drive (it maps /)."""
    path = Path(windows_path('/proc/meminfo') or 'Z:\\proc\\meminfo')
    try:
        fields = dict(line.split(':', 1) for line in path.read_text(errors='replace').splitlines() if ':' in line)
    except OSError:
        return None
    gib = lambda k: round(int(fields[k].split()[0]) / (1 << 20), 1) if k in fields else None
    return {'total_gib': gib('MemTotal'), 'available_gib': gib('MemAvailable'), 'swap_gib': gib('SwapTotal')}


def unix_path(path):
    """The Linux path of a Windows path (kernel32.wine_get_unix_file_name), or None."""
    try:
        k = ctypes.WinDLL('kernel32')
        f = k.wine_get_unix_file_name
    except (OSError, AttributeError):
        return None
    f.argtypes, f.restype = [ctypes.c_wchar_p], ctypes.c_void_p
    p = f(str(path))
    if not p:
        return None
    s = ctypes.string_at(p).decode('utf-8', errors='surrogateescape')
    k.GetProcessHeap.restype = ctypes.c_void_p
    k.HeapFree.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p]
    k.HeapFree(k.GetProcessHeap(), 0, p)
    return s


def windows_path(path):
    """The Windows path of a Linux path (kernel32.wine_get_dos_file_name), or None."""
    try:
        k = ctypes.WinDLL('kernel32')
        f = k.wine_get_dos_file_name
    except (OSError, AttributeError):
        return None
    f.argtypes, f.restype = [ctypes.c_char_p], ctypes.c_void_p
    p = f(os.fsencode(path))
    if not p:
        return None
    s = ctypes.wstring_at(p)
    k.GetProcessHeap.restype = ctypes.c_void_p
    k.HeapFree.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p]
    k.HeapFree(k.GetProcessHeap(), 0, p)
    return s


def _nt_to_unix(value):
    """WINELOADER and WINECONFIGDIR hold NT paths (\\??\\Z:\\...)."""
    if not value:
        return None
    return unix_path(value[4:] if value.startswith('\\??\\') else value)


def _quote(s):
    return "'" + str(s).replace("'", "'\\''") + "'"


class Tool:
    """A tool started through /bin/sh by start_tool. Its output goes to log (written by the shell, read by the caller);
    poll() is None while it runs, then its exit code."""

    def __init__(self, log, pid_file, done_file, upid_file):
        self.log, self.pid_file, self.done_file, self.upid_file = log, pid_file, done_file, upid_file
        self.handle, self.pid, self.code = None, None, None

    def _open(self):
        if self.handle or self.pid is not None or not self.pid_file.is_file():
            return
        try:
            self.pid = int(self.pid_file.read_text().strip())
        except ValueError:
            return        # still being written
        k = ctypes.WinDLL('kernel32', use_last_error=True)
        k.OpenProcess.restype = ctypes.c_void_p
        h = k.OpenProcess(SYNCHRONIZE | PROCESS_QUERY_LIMITED_INFORMATION, False, self.pid)
        self.handle = h or None

    def started(self):
        """True once the Windows tool wrote its process id (the shell route works)."""
        self._open()
        return self.pid is not None

    def poll(self):
        if self.code is not None:
            return self.code
        self._open()
        if self.handle:
            k = ctypes.WinDLL('kernel32')
            k.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
            if k.WaitForSingleObject(self.handle, 0) == WAIT_OBJECT_0:
                code = ctypes.c_uint32()
                k.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
                k.GetExitCodeProcess(self.handle, ctypes.byref(code))
                k.CloseHandle.argtypes = [ctypes.c_void_p]
                k.CloseHandle(self.handle)
                self.handle = None
                self.code = code.value
            return self.code
        if self.done_file.is_file():
            # Ended before it could be opened, or never started: the shell's status (the low 8 bits of the exit code).
            try:
                self.code = int(self.done_file.read_text().strip())
            except ValueError:
                return None
        return self.code

    def shell_done(self):
        """True once the shell finished (it writes the status after the tool's last output reached the log)."""
        return self.done_file.is_file()

    def kill(self):
        if self.poll() is not None:
            return
        if self.pid is not None:
            subprocess.run(['taskkill', '/T', '/F', '/PID', str(self.pid)], capture_output=True)
        elif self.upid_file.is_file():
            try:
                subprocess.Popen(['/bin/sh', '-c', f'kill -9 {int(self.upid_file.read_text().strip())}'],
                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except (OSError, ValueError):
                pass

    def cleanup(self):
        for p in (self.pid_file, self.done_file, self.upid_file):
            try:
                p.unlink()
            except OSError:
                pass


def start_tool(command, log, cwd, env):
    """Run command (a Windows program and its arguments) through /bin/sh, its stdout and stderr and Wine's own
    messages appended to log. Returns a Tool, or None when the shell route is not available (the caller then uses a
    pipe)."""
    loader = _nt_to_unix(os.environ.get('WINELOADER'))
    prefix = _nt_to_unix(os.environ.get('WINECONFIGDIR'))
    program, ulog, ucwd = unix_path(command[0]), unix_path(log), unix_path(cwd)
    if not (loader and program and ulog and ucwd):
        return None
    base = Path(log)
    pid_file, done_file, upid_file = (base.with_name(base.name + s) for s in ('.pid', '.done', '.upid'))
    tool = Tool(log, pid_file, done_file, upid_file)
    tool.cleanup()
    udone, uupid = unix_path(done_file), unix_path(upid_file)
    if not (udone and uupid):
        return None
    exports = {k: env[k] for k in ('PYTHONUNBUFFERED', 'PYTHONIOENCODING') if k in env}
    exports[PID_ENV] = str(pid_file)
    if prefix:
        exports['WINEPREFIX'] = prefix
    script = (' '.join(f'export {k}={_quote(v)};' for k, v in exports.items())
              + f' cd {_quote(ucwd)} || {{ echo 127 > {_quote(udone)}; exit; }};'
              + f' {_quote(loader)} {_quote(program)} ' + ' '.join(_quote(a) for a in command[1:])
              + f' >> {_quote(ulog)} 2>&1 < /dev/null & echo $! > {_quote(uupid)}; wait $!;'
              + f' echo $? > {_quote(udone)}')
    try:
        subprocess.Popen(['/bin/sh', '-c', script], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, env=env)
    except OSError as e:
        # Wine starts a Linux program but returns no handle for it: ERROR_INVALID_HANDLE (6) means it is running.
        if getattr(e, 'winerror', None) != 6:
            return None
    return tool
