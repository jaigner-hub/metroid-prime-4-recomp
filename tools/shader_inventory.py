"""Every NVN shader program the game ships, read from the merged RomFS, keyed by the code hash runtime/gfx_shader.c gives
it (build_code_hash), so a shader list that names programs by that hash can be resolved against the player's own files.

Where the programs are (found 2026-11-04, docs/plan.md "Shader preparation"):

- Every asset file is a Retro `RFRM` form. A `.pak` (and the update's `Patch/*.pak.patch`) is a `PACK` form whose
  `TOCC` form holds `ADIR`, the asset directory: a u32 count, then 60-byte entries (type fourcc, GUID, two u32, then
  u64 offset, compressed size, size, and a zero u64; offsets are from the start of the pak).
- Shaders live in `SNVN` chunks: one in `MaterialArchive.arc` (RomFS root) and one or more in each material (`MTRL`)
  and vector graphic (`SVGM`) asset. A material asset is an `RFRM` header and a zstd frame; its decompressed body holds
  the `SNVN` chunk.
- An `SNVN` chunk is the tag, a u64 size, then at +0x18 a u32 1, a u32 header length, a u32 entry count and the entries
  (u32 offset, u32 size each). Each entry is a u32 length and a zstd frame without a stored content size; the first
  frame's offset gives the base the offsets count from. A decoded entry holds one or more programs: their code blocks
  (each headed by 0x12345678) one after another, then one 0x880-byte NVN control section (0x98761234) per block, in the
  same order. Control +0x700 is the c1 offset in the code, +0x704 the code length, +0x714 the stage (0 vertex,
  1 fragment, 2 geometry).
- `NVN2` (MaterialArchive.arc) and `SDX ` (DXBC) chunks hold the same shaders for other targets; the Switch 1 build does
  not use them.

All 2,826 programs one play of the first area met were found here by their runtime hash. Needs Python 3.14
(compression.zstd).
"""
import hashlib
import json
import struct
from pathlib import Path

from compression import zstd

CONTROL = 0x880
CODE_MAGIC = b'\x78\x56\x34\x12'
CONTROL_MAGIC = b'\x34\x12\x76\x98'
ZSTD_MAGIC = b'\x28\xb5\x2f\xfd'
STAGES = {0: 0, 1: 4, 2: 3}      # control +0x714 -> runtime/gfx.h GFX_STAGE_VS, GFX_STAGE_FS, GFX_STAGE_GS
UNKNOWN_STAGE = (0, 4, 5)        # another value: every translated stage is tried (its hash names the stage)
SHADER_ASSETS = (b'MTRL', b'SVGM')


def code_hash(stage, control, code):
    """runtime/gfx_shader.c build_code_hash over a program whose c1 constants are the code's tail."""
    c1_off, length = struct.unpack_from('<II', control, 0x700)
    body = code[:length]
    return hashlib.sha256(struct.pack('<I', stage) + control + struct.pack('<Q', length) + body +
                          struct.pack('<I', length - c1_off) + body[c1_off:length]).digest()


def _find_all(b, pat):
    out, k = [], b.find(pat)
    while k >= 0:
        out.append(k)
        k = b.find(pat, k + 4)
    return out


def _frames(raw):
    """Every zstd frame in raw, decoded (a frame that does not decode is skipped)."""
    out, k = [], raw.find(ZSTD_MAGIC)
    while k >= 0:
        d = zstd.ZstdDecompressor()
        try:
            data = d.decompress(raw[k:])
        except zstd.ZstdError:
            k = raw.find(ZSTD_MAGIC, k + 4)
            continue
        out.append(data)
        k = raw.find(ZSTD_MAGIC, len(raw) - len(d.unused_data))
    return out


def snvn_programs(blob, at):
    """(control, code) of every program in the SNVN chunk at blob[at]; [] when the bytes there are not one."""
    try:
        n = struct.unpack_from('<I', blob, at + 0x20)[0]
        ents = [struct.unpack_from('<II', blob, at + 0x24 + 8 * i) for i in range(n)]
    except struct.error:
        return []
    if not ents:
        return []
    first = blob.find(ZSTD_MAGIC, at + 0x24 + 8 * n)
    if first < 0:
        return []
    base, out = first - ents[0][0], []
    for off, size in ents:
        try:
            d = zstd.ZstdDecompressor().decompress(blob[base + off:base + off + size])
        except zstd.ZstdError:
            continue
        codes, controls = _find_all(d, CODE_MAGIC), _find_all(d, CONTROL_MAGIC)
        for k, c in enumerate(controls):
            if k < len(codes) and c + CONTROL <= len(d):
                out.append((d[c:c + CONTROL], d[codes[k]:]))
    return out


class RomFS:
    """The merged RomFS as extract_romfs.py leaves it: romfs.bin and index.json (path, offset, size per file)."""

    def __init__(self, root):
        root = Path(root)
        self.files = {f['path']: f for f in json.loads((root / 'index.json').read_text(encoding='utf-8'))['files']}
        self.f = open(root / 'romfs.bin', 'rb')

    def read(self, path, off=0, n=None):
        e = self.files[path]
        n = e['size'] - off if n is None else n
        self.f.seek(e['offset'] + off)
        return self.f.read(n)

    def adir(self, path):
        """The ADIR entries of a PACK file: (type, guid, offset, compressed size, size); None when it is not one."""
        h = self.read(path, 0, 0x30)
        if h[:4] != b'RFRM' or h[0x14:0x18] != b'PACK':
            return None
        t = self.read(path, 0x20, 0x18 + struct.unpack_from('<Q', h, 0x24)[0])
        o = 0x20
        while o + 0x18 <= len(t):
            tag, size = t[o:o + 4], struct.unpack_from('<Q', t, o + 4)[0]
            if tag == b'ADIR':
                p = o + 0x18
                out = []
                for _ in range(struct.unpack_from('<I', t, p)[0]):
                    ty, guid = t[p + 4:p + 8], t[p + 8:p + 24]
                    off, csize, dsize = struct.unpack_from('<QQQ', t, p + 32)
                    out.append((ty, guid, off, csize, dsize))
                    p += 60
                return out
            o += 0x18 + size
        return []


def programs(romfs_dir):
    """Yields (code hash, stage, control, code) for every program, the archive's first, then the paks' (the update's
    patch pak last); a program met twice comes once."""
    rf = RomFS(romfs_dir)
    seen_hash, seen_asset = set(), set()

    def emit(control, code):
        s = struct.unpack_from('<I', control, 0x714)[0]
        for stage in ([STAGES[s]] if s in STAGES else UNKNOWN_STAGE):
            h = code_hash(stage, control, code)
            if h not in seen_hash:
                seen_hash.add(h)
                yield h, stage, control, code

    def chunks(blob):
        k = blob.find(b'SNVN')
        while k >= 0:
            for control, code in snvn_programs(blob, k):
                yield from emit(control, code)
            k = blob.find(b'SNVN', k + 4)

    if 'MaterialArchive.arc' in rf.files:
        yield from chunks(rf.read('MaterialArchive.arc'))
    paks = sorted((p for p in rf.files if p.endswith('.pak') or p.endswith('.pak.patch')),
                  key=lambda p: (p.endswith('.patch'), p))
    for path in paks:
        for ty, guid, off, csize, dsize in rf.adir(path) or []:
            if ty not in SHADER_ASSETS or (guid, csize) in seen_asset:
                continue
            seen_asset.add((guid, csize))
            raw = rf.read(path, off, csize)
            for blob in [raw] + _frames(raw):
                yield from chunks(blob)
