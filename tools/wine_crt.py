"""MP4: C runtime functions that older Wine builds leave unimplemented, filled in before numpy loads (issue #11).

numpy's Windows build calls the C99 complex accessors crealf and friends from ucrtbase while it imports. Wine 10.0, and
so Proton 10.0, the default Proton on a Steam Deck, has these only as stubs (dlls/ucrtbase/ucrtbase.spec there); a
stub ends the process with "unimplemented function ucrtbase.dll.crealf" and exit code 0x80000100 (EXCEPTION_WINE_STUB),
so every tool that imports numpy died before printing anything. Wine 10.1 implements them.

patch() runs under Wine only (tools/extract.py RUN_SCRIPT calls it first). For each function below whose export in
ucrtbase is still Wine's stub thunk, it writes the real function over the thunk, so every module importing it, directly
or through api-ms-win-crt-math-l1-1-0, gets the real one. A function Wine implements is left alone. Each function only
returns one half of a complex number, so the replacement is the whole function, not an approximation.
"""
import ctypes
import os
import sys

# x64 code, Microsoft calling convention: an 8-byte _Fcomplex arrives by value in rcx, a 16-byte _Dcomplex or _Lcomplex
# (long double is double there) by pointer in rcx; the result goes back in xmm0.
CODE = {
    'crealf': bytes.fromhex('660f6ec1' 'c3'),              # movd xmm0, ecx; ret
    'cimagf': bytes.fromhex('48c1e920' '660f6ec1' 'c3'),   # shr rcx, 32; movd xmm0, ecx; ret
    'creal': bytes.fromhex('f20f1001' 'c3'),               # movsd xmm0, [rcx]; ret
    'cimag': bytes.fromhex('f20f104108' 'c3'),             # movsd xmm0, [rcx+8]; ret
    'creall': bytes.fromhex('f20f1001' 'c3'),
    'cimagl': bytes.fromhex('f20f104108' 'c3'),
}

# The x86-64 stub thunk winebuild emits for a "@ stub" export (tools/winebuild/spec32.c output_stub_funcs):
# sub rsp, 28h; lea rcx, [rip+name]; lea rdx, [rip+dll]; call __wine_spec_unimplemented_stub. 24 bytes.
STUB_LENGTH = 24

LOGGED_ENV = 'MP4_WINE_CRT_LOGGED'


def is_stub(code):
    return (len(code) >= STUB_LENGTH and code[:7] == bytes.fromhex('4883ec28488d0d')
            and code[11:14] == bytes.fromhex('488d15') and code[18] == 0xe8)


def under_wine():
    if os.name != 'nt':
        return False
    try:
        return hasattr(ctypes.WinDLL('ntdll'), 'wine_get_version')
    except OSError:
        return False


def patch():
    """Fill in the stubbed functions of CODE under Wine. Returns the names patched; does nothing elsewhere."""
    if not under_wine():
        return []
    k = ctypes.WinDLL('kernel32', use_last_error=True)
    k.GetModuleHandleW.restype = ctypes.c_void_p
    k.GetProcAddress.argtypes, k.GetProcAddress.restype = [ctypes.c_void_p, ctypes.c_char_p], ctypes.c_void_p
    k.VirtualProtect.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint32)]
    k.FlushInstructionCache.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t]
    k.GetCurrentProcess.restype = ctypes.c_void_p
    ucrt = k.GetModuleHandleW('ucrtbase.dll') or ctypes.WinDLL('ucrtbase')._handle
    done = []
    for name, code in CODE.items():
        addr = k.GetProcAddress(ucrt, name.encode())
        if not addr or not is_stub(ctypes.string_at(addr, STUB_LENGTH)):
            continue
        old = ctypes.c_uint32()
        if not k.VirtualProtect(addr, STUB_LENGTH, 0x40, ctypes.byref(old)):   # PAGE_EXECUTE_READWRITE
            print(f'wine_crt: cannot write {name} (error {ctypes.get_last_error()})', file=sys.stderr)
            continue
        ctypes.memmove(addr, code, len(code))
        k.VirtualProtect(addr, STUB_LENGTH, old.value, ctypes.byref(old))
        k.FlushInstructionCache(k.GetCurrentProcess(), addr, STUB_LENGTH)
        done.append(name)
    if done and not os.environ.get(LOGGED_ENV):
        # Once per tool: its worker processes inherit the variable and patch quietly.
        os.environ[LOGGED_ENV] = '1'
        print(f'wine_crt: filled in {", ".join(done)} (stubs in this Wine)', file=sys.stderr, flush=True)
    return done
