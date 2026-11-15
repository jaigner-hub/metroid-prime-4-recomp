# Adapted from totk-pc-recomp tools/prepare_shaders.py @ c944f9f94520874f2a669add30dd08d29ee9f7b5 (MIT, Copyright (c) 2026
# Valiantsin Dzerakh): the seed files and the --prepare-shaders run. The list is this project's own (shader answers met in
# play, below) instead of MacNativeGames' TSV, the programs come from tools/shader_inventory.py, and there are no default
# variants.
"""Translate the shader variants a list names, once, before play, so the first time a scene is seen does not stutter.

The list names shader variants players met: a code hash (runtime/gfx_shader.c build_code_hash) and the translator
answers it was translated for. It is a file in the layout of shader-answers.bin, which mp4.exe writes next to
pipeline-keys.bin as it plays ("answers met in play" in gfx_shader.c), with every input id zeroed (input ids depend on
the translator; mp4.exe computes them): it holds no game code, so it can ship with a release. This tool finds each
listed program in the game's own files (tools/shader_inventory.py), writes next to the build's mp4.exe

    seed-programs.bin  "TOTKSPRG": stage, length, c1 length, code hash, control section, code, c1
    seed-answers.bin   "TOTKSANS": stage, code hash, zero input id, answers, priority

and runs `mp4.exe --prepare-shaders`, which translates every record into the build's shader cache, writes
shader-prepared.bin and builds every variant's stage library once, so the GPU driver's own disk cache holds them
(vk_pipeline.c "stage library prebuild"). The seed files are deleted afterwards.

Making the list from play (the files of one or more builds that played; records of programs not in the game are
dropped only at preparation time):

    python tools/prepare_shaders.py --make-list build/shader-list.bin build/v131072/shader-answers.bin [more...]

The pipeline list is the same for pipelines: the records of pipeline-keys.bin (runtime/vk_pipeline.c "pipeline keys":
the render state of each pipeline play built, its shaders named by input id, a hash of the code hash and the answers),
again no game code. Preparing merges it into the build's pipeline-keys.bin before mp4.exe runs, and --prepare-shaders
builds every record once, so a fresh install starts with the pipelines play met already built:

    python tools/prepare_shaders.py --make-pipeline-list build/pipeline-list.bin build/v131072/pipeline-keys.bin [more...] \
        --aliases build/v131072/shader-cache/in-aliases.bin

Preparing a build (Python 3.14 for compression.zstd: .venv-py314 or the installer's embeddable Python):

    python tools/prepare_shaders.py build/v131072 --list build/shader-list.bin [--romfs DIR] [--no-run] [--keep-seed]
"""
import argparse
import struct
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONTROL = 0x880
ANS_MAGIC, PRG_MAGIC = b'TOTKSANS', b'TOTKSPRG'
ANS_REC = 4 + 32 + 32 + 147 + 1                            # gfx_shader.c ANS_REC
LIST_PRIORITY = 1                                          # shader-prepared.bin: warm-up (0 title, 2 other)
BUNDLED_LIST = ROOT / 'data' / 'shader-list.bin'           # release folder layout (tools/make_release.py)
BUNDLED_PIPELINES = ROOT / 'data' / 'pipeline-list.bin'
KEY_MAGIC, KEY_VERSION, KEY_REC = b'TOTKPKEY', 3, 424       # vk_pipeline.c KeysHeader, KEYS_VERSION, sizeof(KeyRec)


def read_records(path):
    """The records of an answers file (shader-answers.bin or a list): [bytes of ANS_REC]."""
    b = Path(path).read_bytes()
    if len(b) < 16 or b[:8] != ANS_MAGIC:
        raise SystemExit(f'{path}: not a shader answers file')
    version, size = struct.unpack_from('<II', b, 8)
    if version != 1 or size != ANS_REC:
        raise SystemExit(f'{path}: answers version {version}, record size {size}; this tool reads 1 and {ANS_REC}')
    return [b[o:o + ANS_REC] for o in range(16, len(b) - ANS_REC + 1, ANS_REC)]


def write_records(path, recs):
    with open(path, 'wb') as f:
        f.write(ANS_MAGIC + struct.pack('<II', 1, ANS_REC))
        for r in recs:
            f.write(r)


def make_list(out, sources):
    """The union of the sources' variants, in the order first met, with input ids zeroed and one record per
    (stage, code hash, answers)."""
    seen, recs = set(), []
    for src in sources:
        for r in read_records(src):
            stage, h, packed = r[:4], r[4:36], r[68:68 + 147]
            if (stage, h, packed) in seen:
                continue
            seen.add((stage, h, packed))
            recs.append(stage + h + bytes(32) + packed + bytes([LIST_PRIORITY]))
    write_records(out, recs)
    print(f'shader list: {len(recs)} variants of {len({r[4:36] for r in recs})} programs from {len(sources)} file(s) '
          f'-> {out}')
    return 0


def read_keys(path):
    """The records of a pipeline keys file (pipeline-keys.bin or a pipeline list): [bytes of KEY_REC]."""
    b = Path(path).read_bytes()
    if len(b) < 16 or b[:8] != KEY_MAGIC:
        raise SystemExit(f'{path}: not a pipeline keys file')
    version, size = struct.unpack_from('<II', b, 8)
    if version != KEY_VERSION or size != KEY_REC:
        raise SystemExit(f'{path}: keys version {version}, record size {size}; this tool reads {KEY_VERSION} and {KEY_REC}')
    return [b[o:o + KEY_REC] for o in range(16, len(b) - KEY_REC + 1, KEY_REC)]


def write_keys(path, recs):
    tmp = Path(str(path) + '.tmp')
    with open(tmp, 'wb') as f:
        f.write(KEY_MAGIC + struct.pack('<II', KEY_VERSION, KEY_REC))
        for r in recs:
            f.write(r)
    tmp.replace(path)


def union_keys(sources):
    """The records of the files in order, each distinct record once (the runtime deduplicates by the same bytes)."""
    seen, recs = set(), []
    for src in sources:
        for r in read_keys(src):
            if r not in seen:
                seen.add(r)
                recs.append(r)
    return recs


ALIAS_MAGIC, ALIAS_VERSION = 0x41534b54, 1                # gfx_shader.c "input aliases"


def read_aliases(path):
    """{old input id: current input id} of a shader cache's in-aliases.bin (runtime/gfx_shader.c "input aliases")."""
    b = Path(path).read_bytes()
    if len(b) < 40 or (len(b) - 40) % 64 or struct.unpack_from('<II', b) != (ALIAS_MAGIC, ALIAS_VERSION):
        raise SystemExit(f'{path}: not an input alias file')
    return {b[o:o + 32]: b[o + 32:o + 64] for o in range(40, len(b), 64)}


def canonical(recs, aliases):
    """The records with their shaders named by the ids the current use masks give (issue #18): records recorded under
    version 2 masks name variants a fresh install never stores inputs for, so the boot prewarm would drop them. Records
    that become equal are kept once."""
    out, seen, renamed = [], set(), 0
    for r in recs:
        vs, fs = aliases.get(r[0:32], r[0:32]), r[32:64]
        if fs != bytes(32):
            fs = aliases.get(fs, fs)
        n = vs + fs + r[64:]
        renamed += n != r
        if n not in seen:
            seen.add(n)
            out.append(n)
    print(f'input aliases: {renamed} records renamed, {len(recs) - len(out)} merged into others')
    return out


def make_pipeline_list(out, sources, aliases=None):
    recs = union_keys(sources)
    if aliases:
        recs = canonical(recs, read_aliases(aliases))
    write_keys(out, recs)
    print(f'pipeline list: {len(recs)} pipelines from {len(sources)} file(s) -> {out}')
    return 0


def merge_pipelines(build, pipelines):
    """The list's records added to the build's pipeline-keys.bin; its own records (an update over a played install)
    stay first. A keys file of another version is replaced, as mp4.exe would rewrite it."""
    keys = build / 'pipeline-keys.bin'
    own = []
    if keys.is_file():
        try:
            own = read_keys(keys)
        except SystemExit as e:
            print(f'{e}; replaced by the list', flush=True)
    listed = read_keys(pipelines)
    have = set(own)
    added = [r for r in dict.fromkeys(listed) if r not in have]
    if added or not keys.is_file():
        write_keys(keys, own + added)
    print(f'{len(listed)} listed pipelines: {len(added)} added to {len(own)} in pipeline-keys.bin', flush=True)


def prepare(a):
    sys.path.insert(0, str(ROOT / 'tools'))
    from shader_inventory import programs   # needs Python 3.14 (compression.zstd)
    t0 = time.monotonic()
    listed = read_records(a.list or BUNDLED_LIST)
    want = {r[4:36] for r in listed}
    progs = {}
    for h, stage, control, code in programs(a.romfs):
        if h in want and h not in progs:
            c1_off, length = struct.unpack_from('<II', control, 0x700)
            progs[h] = struct.pack('<III', stage, length, length - c1_off) + h + control + code[:length] + \
                code[c1_off:length]
    t1 = time.monotonic()
    recs = [r for r in listed if r[4:36] in progs]
    out = a.build.resolve()
    with open(out / 'seed-programs.bin', 'wb') as f:
        f.write(PRG_MAGIC + struct.pack('<II', 1, 0))
        for body in progs.values():
            f.write(body)
    write_records(out / 'seed-answers.bin', recs)
    pipelines = a.pipelines or (BUNDLED_PIPELINES if BUNDLED_PIPELINES.is_file() else None)
    if pipelines:
        merge_pipelines(out, pipelines)
    print(f'{len(listed)} listed variants of {len(want)} programs: {len(recs)} variants of {len(progs)} programs found '
          f'in the game ({len(want) - len(progs)} programs not); inventory {t1 - t0:.1f} s', flush=True)
    if a.no_run:
        return 0
    r = subprocess.run([str(out / 'mp4.exe'), '--prepare-shaders'], cwd=str(out))
    if not a.keep_seed:
        for name in ('seed-programs.bin', 'seed-answers.bin'):
            (out / name).unlink(missing_ok=True)
    print(f'mp4.exe --prepare-shaders: rc={r.returncode}; total {time.monotonic() - t0:.1f} s', flush=True)
    return r.returncode


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('build', type=Path, nargs='?', help='build directory holding mp4.exe (build\\v131072)')
    ap.add_argument('--version', default='v131072', help='build/extracted/<version> (default v131072)')
    ap.add_argument('--romfs', type=Path, help='merged RomFS directory with romfs.bin and index.json '
                                               '(default build/extracted/<version>/romfs)')
    ap.add_argument('--list', type=Path, help=f'the shader list (default {BUNDLED_LIST.relative_to(ROOT)})')
    ap.add_argument('--pipelines', type=Path, help='the pipeline list merged into pipeline-keys.bin (default '
                                                   f'{BUNDLED_PIPELINES.relative_to(ROOT)} when there is one)')
    ap.add_argument('--no-run', action='store_true', help='write the seed files only')
    ap.add_argument('--keep-seed', action='store_true', help='keep the seed files after mp4.exe ran')
    ap.add_argument('--make-list', type=Path, metavar='OUT', help='write a list from the answers files given after it')
    ap.add_argument('--make-pipeline-list', type=Path, metavar='OUT',
                    help='write a pipeline list from the pipeline-keys.bin files given after it')
    ap.add_argument('--aliases', type=Path, metavar='FILE',
                    help='with --make-pipeline-list: shader-cache/in-aliases.bin of the build that played, so the list '
                         'names shaders by their current input ids')
    ap.add_argument('sources', type=Path, nargs='*', help='with --make-list: shader-answers.bin files; with '
                                                          '--make-pipeline-list: pipeline-keys.bin files')
    a = ap.parse_args()
    if a.make_pipeline_list:
        sources = ([a.build] if a.build else []) + a.sources
        if not sources:
            ap.error('--make-pipeline-list needs at least one pipeline-keys.bin')
        return make_pipeline_list(a.make_pipeline_list, sources, a.aliases)
    if a.make_list:
        sources = ([a.build] if a.build else []) + a.sources
        if not sources:
            ap.error('--make-list needs at least one shader-answers.bin')
        return make_list(a.make_list, sources)
    if not a.build:
        ap.error('the build directory is required')
    a.romfs = a.romfs or ROOT / 'build' / 'extracted' / a.version / 'romfs'
    return prepare(a)


if __name__ == '__main__':
    sys.exit(main())
