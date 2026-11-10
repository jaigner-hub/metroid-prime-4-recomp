"""Decode a GPU crash dump from a crash report with NVIDIA's Nsight Aftermath library.

    python tools/decode_gpu_fault.py <gpu-fault-*.bin | *.nv-gpudmp> [--dll PATH] [--out FILE.json]

On a device loss the runtime writes the driver's own crash dump beside the log (runtime/vk_device.c
vk_device_fault_report: the VK_EXT_device_fault vendor binary, gpu-fault-<date>-<time>.bin). On NVIDIA that is an
Aftermath GPU crash dump. This tool decodes it with GFSDK_Aftermath_Lib.x64.dll from the Nsight Aftermath SDK, which
the developer downloads from NVIDIA (its license keeps it out of this repository and the release): --dll, else
MP4_AFTERMATH_DLL, else toolchain/aftermath/lib/x64. It prints the page fault, the active shaders and the warps, and
writes the whole decode as JSON (default: beside the dump). A shader is named fragment_NN or vertex_NN by the dump; the
runtime's own names (the first 4 bytes of the shader's cache key) come from a run with MP4_AFTERMATH=1
(runtime/aftermath_win.c), which hashes every module as it is made.

Windows Python only (ctypes loads the DLL); standard library only.
"""
import argparse
import ctypes
import json
import os
import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VERSION_API = 0x21B           # GFSDK_Aftermath_Version_API of SDK 2026.3 (the library checks it)
DECODER_ALL_INFO = 0x3FFF     # GFSDK_Aftermath_GpuCrashDumpDecoderFlags_ALL_INFO
FORMAT_NONE = 0
SUCCESS = 0x1                 # GFSDK_Aftermath_Result_Success


def load(dll_arg):
    for p in (dll_arg, os.environ.get('MP4_AFTERMATH_DLL'), ROOT / 'toolchain' / 'aftermath' / 'lib' / 'x64' / 'GFSDK_Aftermath_Lib.x64.dll'):
        if p and Path(p).is_file():
            return ctypes.CDLL(str(p))
    sys.exit('decode_gpu_fault: GFSDK_Aftermath_Lib.x64.dll not found (--dll, MP4_AFTERMATH_DLL or toolchain/aftermath)')


def decode(lib, data):
    dec = ctypes.c_void_p()
    r = lib.GFSDK_Aftermath_GpuCrashDump_CreateDecoder(ctypes.c_uint32(VERSION_API), data, ctypes.c_uint32(len(data)),
                                                      ctypes.byref(dec))
    if r != SUCCESS:
        return None, r
    n = ctypes.c_uint32()
    r = lib.GFSDK_Aftermath_GpuCrashDump_GenerateJSON(dec, ctypes.c_uint32(DECODER_ALL_INFO), ctypes.c_uint32(FORMAT_NONE),
                                                     None, None, None, None, ctypes.byref(n))
    out = None
    if r == SUCCESS and n.value:
        buf = ctypes.create_string_buffer(n.value)
        if lib.GFSDK_Aftermath_GpuCrashDump_GetJSON(dec, ctypes.c_uint32(n.value), buf) == SUCCESS:
            out = buf.value.decode('utf-8', 'replace')
    lib.GFSDK_Aftermath_GpuCrashDump_DestroyDecoder(dec)
    return out, r


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('dump', type=Path)
    ap.add_argument('--dll')
    ap.add_argument('--out', type=Path)
    a = ap.parse_args()
    lib = load(a.dll)
    raw = a.dump.read_bytes()
    text, r = decode(lib, raw)
    if text is None and len(raw) >= 8:
        # A VK_EXT_device_fault vendor binary may start with VkDeviceFaultVendorBinaryHeaderVersionOneEXT
        # (headerSize, headerVersion, ...): the vendor's own data follows it.
        hsize, hver = struct.unpack_from('<II', raw)
        if hver == 1 and 0 < hsize < len(raw):
            text, r = decode(lib, raw[hsize:])
    if text is None:
        sys.exit(f'decode_gpu_fault: the library cannot decode {a.dump} (result {r:#x}); not an Aftermath dump?')
    out = a.out or a.dump.with_suffix('.json')
    out.write_text(text, encoding='utf-8')
    doc = json.loads(text)
    for part in doc if isinstance(doc, list) else [doc]:
        for k, v in part.items():
            if k in ('Page fault info', 'Device info'):
                print(f'{k}: {json.dumps(v)}')
            elif k == 'Shader infos':
                for s in v:
                    i = s.get('Info', s)
                    print(f'shader {i.get("Shader name")}: {i.get("Shader type")}, {i.get("Shader size")} bytes, hash {i.get("Shader hash"):#x}')
            elif k == 'Active Warps':
                print(f'active warps: {sum(w.get("Warp count", 1) for w in v)} at {len(v)} PCs, e.g. ' +
                      ', '.join(w.get('GPU PC Address', '?') for w in v[:6]))
    print(f'full decode: {out}')


if __name__ == '__main__':
    main()
