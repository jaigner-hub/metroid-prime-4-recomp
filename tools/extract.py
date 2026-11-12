# From fe3h-pc-recomp tools/extract.py @ 47ef99126793dbd13e4a475feb910a13ef6639b8 (MIT, Copyright (c) 2026 Jeff Aigner), renamed to mp4; MP4 changes are noted where made.
"""The installer's view of the extraction (installer/steps.py): the contract of totk-pc-recomp tools/extract.py over
tools/extract_exefs.py (the ExeFS of one package) and tools/extract_romfs.py (the base RomFS merged with the update's
BKTR patch by hactool).

Interface and messages follow totk-pc-recomp tools/extract.py @ c944f9f94520874f2a669add30dd08d29ee9f7b5 (MIT,
Copyright (c) 2026 Valiantsin Dzerakh); the implementation is ours. The expected hashes of the supported dumps are
tools/data/expected-v<display version>.json (hashes only, no game data). Key values stay in the keys file the tools
read; they never appear in logs or output.
"""
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
DATA = TOOLS / 'data'
EXEFS_CODE = ('main', 'rtld', 'sdk', 'subsdk0')   # MP4: no subsdk1. The modules the lifter reads; main.npdm may differ

# A reader's message when a game file's data fails its hash checks or ends early (extract_exefs.py, hactool).
DAMAGE_TEXT = re.compile(r'hash mismatch|hash-table mismatch|FS header hash|IVFC level \d+ block \d+ failed|Truncated|'
                         r'out of bounds|Section bounds|\(FAIL\)')
DAMAGE = re.compile(rf'^\w*Error: (.*(?:{DAMAGE_TEXT.pattern}).*)$', re.M)

# Runs a tools/ script by path with its folder first on sys.path (installer/steps.py run_tool). MP4: under Wine the
# setup starts the tool through /bin/sh and gets no handle for it, so the tool writes its process id to the file
# MP4_TOOL_PID_FILE names (installer/wine.py start_tool).
RUN_SCRIPT = ('import os, runpy, sys\n'
              "f = os.environ.pop('MP4_TOOL_PID_FILE', None)\n"
              'if f:\n'
              "    with open(f + '.tmp', 'w') as o: o.write(str(os.getpid()))\n"
              "    os.replace(f + '.tmp', f)\n"
              'p = sys.argv[1]; sys.path.insert(0, os.path.dirname(p)); sys.argv = sys.argv[1:]; '
              "runpy.run_path(p, run_name='__main__')")


class DamagedFile(ValueError):
    """A game file whose data fails its hash checks or ends early. path: that file; detail: the check that failed."""

    def __init__(self, path, detail):
        self.path, self.detail = Path(path), detail
        super().__init__(f'{self.path} is damaged or incomplete ({detail}). Copy that file again from its source '
                         f'(or dump it again) and retry.')


def sha256(path):
    with Path(path).open('rb') as f:
        return hashlib.file_digest(f, 'sha256').hexdigest()


def load_expected():
    """{display version: expected} of every tools/data/expected-v*.json."""
    versions = {}
    for path in sorted(DATA.glob('expected-v*.json')):
        data = json.loads(path.read_text(encoding='utf-8'))
        versions[data['display_version']] = data
    return versions


def run_logged(command, log, source=None):
    """Run a tools/ script with its output in log. source: the game file it reads; a failed hash check or a short
    read of that file raises DamagedFile; any other failure ValueError naming the log."""
    args = [str(c) for c in command]
    env = dict(os.environ, PYTHONIOENCODING='utf-8')   # its messages may name non-ASCII paths
    log = Path(log)
    log.parent.mkdir(parents=True, exist_ok=True)
    done = subprocess.run(args, env=env, capture_output=True, encoding='utf-8', errors='replace')
    log.write_text(done.stdout + done.stderr, encoding='utf-8')
    if done.returncode:
        m = DAMAGE.search(done.stdout + done.stderr)
        if m and source:
            raise DamagedFile(source, m.group(1).strip()[:200])
        raise ValueError(f'{Path(args[1]).name if len(args) > 1 else args[0]} failed ({done.returncode}); see {log}')


def extract_exefs(game, keys, out, logs, *, update):
    """Extract the ExeFS of game into out/exefs (every block verified); the manifest goes next to it."""
    out, exefs = Path(out), Path(out) / 'exefs'
    if exefs.exists():
        import shutil
        shutil.rmtree(exefs)
    out.mkdir(parents=True, exist_ok=True)
    command = [sys.executable, '-I', TOOLS / 'extract_exefs.py', game, '--keys', keys, '--output', exefs]
    run_logged(command + (['--update'] if update else []), Path(logs) / f'extract_exefs-{out.name}.log', source=game)
    return exefs


def exefs_problems(exefs, expected):
    """The code modules that are missing or differ from expected (byte for byte). main.npdm is not compared: dumps
    that a repacking tool re-signed differ there."""
    exefs = Path(exefs)
    problems = []
    for name in EXEFS_CODE:
        path = exefs / name
        if not path.is_file() or sha256(path) != expected['exefs_sha256'][name]:
            problems.append(name)
    return problems


def identify_update(exefs, versions):
    """The expected record whose main matches the one in exefs, among the updates (version > 0) of versions."""
    digest = sha256(Path(exefs) / 'main')
    for expected in versions.values():
        if expected['version'] and expected['exefs_sha256']['main'] == digest:
            return expected
    raise ValueError(f'Update main {digest[:16]}... matches no supported version ({", ".join(versions)})')


def romfs_verified(romfs, expected):
    """Whether romfs/ holds the merged image of expected: its index.json reports that size and hash."""
    romfs = Path(romfs)
    index = romfs / 'index.json'
    if not index.is_file() or not (romfs / 'romfs.bin').is_file():
        return False
    report = json.loads(index.read_text(encoding='utf-8'))
    return report.get('romfs_sha256') == expected['romfs']['sha256'] and report.get('romfs_size') == expected['romfs']['size']


def merge_space(base, update, keys_path):
    """Bytes the merge needs free next to its output beyond the merged image: the two Program NCAs it copies."""
    sys.path.insert(0, str(TOOLS))
    import extract_romfs
    _, _, inputs = extract_romfs.program_ncas(Path(base), Path(update), Path(keys_path))
    return inputs[0][1]['size'] + inputs[1][1]['size']


def merge_update_romfs(base, update, keys_path, hactool, romfs, logs, phase=None):
    """The base RomFS with the update's BKTR patch, as romfs/romfs.bin + index.json (tools/extract_romfs.py merge).
    phase(name) is called at 'copy', 'merge' and 'index'. A failed hash check names the damaged file."""
    sys.path.insert(0, str(TOOLS))
    import extract_romfs
    try:
        extract_romfs.merge(Path(base), Path(update), Path(keys_path), Path(hactool), None, Path(logs), out=Path(romfs),
                            phase=phase)
    except ValueError as e:
        if DAMAGE_TEXT.search(str(e)):
            raise DamagedFile(update, str(e)[:200]) from None
        raise
