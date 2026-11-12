# From fe3h-pc-recomp installer/steps.py @ 47ef99126793dbd13e4a475feb910a13ef6639b8 (MIT, Copyright (c) 2026 Jeff Aigner), renamed to mp4; MP4 changes are noted where made.
# From totk-pc-recomp installer/steps.py @ c944f9f94520874f2a669add30dd08d29ee9f7b5 (MIT, Copyright (c) 2026 Valiantsin Dzerakh), renamed to mp4; MP4 changes are noted where made.
"""The six setup steps of docs/installer.md: check, extract, lift, compile, prepare, cleanup.

Extraction runs in this process through tools/extract.py (the package's sibling tools/, which ships with it); lift,
compile and prepare run the bundled Python on recomp/gen.py, tools/build_game.py, tools/prepare_shaders.py and
tools/pack_shader_cache.py, whose output goes to <temp>/logs/<step>-<tool>.log and is read for progress. No Win32 call
here: the host check is a small C program built with the bundled clang (hostcheck.c).

Install folder: exefs/, romfs/ (romfs.bin, index.json), profile/, mp4.exe and its DLLs, romfs-index.bin,
romfs-patches/, romfs-patches-xbox/, mp4_xlate.exe, the shader state of mp4.exe --prepare-shaders, mp4.ini,
mp4-install.json.
Temp folder (<temp>/<dest name>-<hash>): check/ (ExeFS of both inputs, the host check), gen/ (C), build/ (objects),
logs/ (kept by the cleanup).
"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

from . import ini, wine
from .layout import EXE, SUPPORTED, version_tag
from .runner import InstallError, Step

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'tools'))
import extract  # noqa: E402
from extract_exefs import (common_tickets, container_entries, load_keys, nca_ctr_key, nca_inventory,  # noqa: E402
                           program_nca)

GiB = 1 << 30
DEST_EXTRA = 8 * GiB      # beside the RomFS: MP4's shader and pipeline caches (about 5.5 GB after the first area),
                          # exefs, mp4.exe
TEMP_NEED = 3 * GiB       # generated C (about 0.5 GB) and objects
HOSTCHECK = Path(__file__).resolve().parent / 'hostcheck.c'
STAMP = '.install-key'


# ---- helpers

def file_sha256(path, progress=None):
    """SHA-256 hex of a file; progress(done, total) after each block."""
    h = hashlib.sha256()
    total = path.stat().st_size
    done = 0
    with open(path, 'rb', buffering=0) as f:
        buf = bytearray(8 << 20)
        view = memoryview(buf)
        while n := f.readinto(buf):
            h.update(view[:n])
            done += n
            if progress:
                progress(done, total)
    return h.hexdigest()


def fingerprint(*parts):
    """SHA-256 over strings and the contents of files and directory trees (a missing path counts as missing)."""
    h = hashlib.sha256()
    for p in parts:
        if isinstance(p, Path):
            if p.is_dir():
                for f in sorted(p.rglob('*')):
                    if f.is_file() and '__pycache__' not in f.parts:
                        h.update(f.relative_to(p).as_posix().encode() + b'\0' + bytes.fromhex(file_sha256(f)))
            elif p.is_file():
                h.update(p.name.encode() + b'\0' + bytes.fromhex(file_sha256(p)))
            else:
                h.update(b'missing\0' + p.name.encode())
        else:
            h.update(str(p).encode())
        h.update(b'\1')
    return h.hexdigest()


def remove(path):
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    elif path.exists() or path.is_symlink():
        path.unlink()


def replace_file(src, dst):
    """Copy src over dst through a temporary name, so a cancelled copy never leaves half a file under dst's name."""
    tmp = dst.with_name(dst.name + '.tmp')
    shutil.copy2(src, tmp)
    os.replace(tmp, dst)


def kill_tree(proc):
    if proc.poll() is not None:
        return
    if os.name == 'nt':
        subprocess.run(['taskkill', '/T', '/F', '/PID', str(proc.pid)], capture_output=True)
    else:
        proc.kill()


def run_tool(ctx, step, script, args, parse=None, cwd=None):
    """Run a tool script with the bundled Python. Its output goes to <temp>/logs/<step>-<script>.log and, line by line,
    to parse(line). Returns (exit code, log path).

    -X faulthandler: a crash in native code (numpy, capstone) prints the Python stack to the log; without it such a
    crash leaves an empty log and only an exit code (issue #11: under Proton, gen.py died before its first line)."""
    logs = ctx.temp / 'logs'
    logs.mkdir(parents=True, exist_ok=True)
    log = logs / f'{step}-{Path(script).stem}.log'
    command = [str(ctx.layout.python), '-X', 'faulthandler', '-c', extract.RUN_SCRIPT, str(script), *map(str, args)]
    ctx.reporter.log('run ' + ' '.join(command[5:]))
    env = dict(os.environ, PYTHONUNBUFFERED='1', PYTHONIOENCODING='utf-8')
    if ctx.wine:
        code = run_tool_wine(ctx, command, log, cwd or ctx.layout.root, env, parse)
        if code is not None:
            ctx.reporter.log(f'{Path(script).name}: exit code {code}; output in {log}')
            return code, log
    with open(log, 'w', encoding='utf-8', newline='\n') as out:
        proc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, cwd=cwd or ctx.layout.root,
                                env=env, text=True, encoding='utf-8', errors='replace', bufsize=1)
        try:
            for line in proc.stdout:
                out.write(line)
                if parse:
                    parse(line.rstrip())
            code = proc.wait()
        except BaseException:
            kill_tree(proc)
            raise
    ctx.reporter.log(f'{Path(script).name}: exit code {code}; output in {log}')
    return code, log


WINE_START_SECONDS = 60   # MP4: how long the /bin/sh route may take to show a sign of life before the pipe is used


def run_tool_wine(ctx, command, log, cwd, env, parse):
    """MP4: run_tool under Wine or Proton. The tool runs through /bin/sh (wine.start_tool), so Wine's own messages go
    to its log too; the log is read as it grows for parse(). Returns the exit code, or None when the shell route does
    not work here (the caller then uses a pipe)."""
    log.write_bytes(b'')
    tool = wine.start_tool(command, log, cwd, env)
    if tool is None:
        ctx.reporter.log('wine: /bin/sh not available for tools; their output goes through a pipe')
        return None
    pos, partial = 0, b''

    def drain(final=False):
        nonlocal pos, partial
        with open(log, 'rb') as f:
            f.seek(pos)
            data = f.read()
        pos += len(data)
        lines = (partial + data).split(b'\n')
        partial = b'' if final else lines.pop()
        if parse:
            for line in lines:
                if line.strip():
                    parse(line.decode('utf-8', errors='replace').rstrip())

    t0 = time.monotonic()
    try:
        while tool.poll() is None:
            drain()
            if (not tool.started() and not tool.upid_file.exists()
                    and time.monotonic() - t0 > WINE_START_SECONDS):
                tool.kill()
                ctx.reporter.log(f'wine: the tool showed no sign of starting through /bin/sh in '
                                 f'{WINE_START_SECONDS} s; running it through a pipe')
                return None
            time.sleep(0.2)
        # The shell writes its status after the last of the tool's output (and Wine's) reached the log.
        deadline = time.monotonic() + 10
        while not tool.shell_done() and time.monotonic() < deadline:
            time.sleep(0.1)
        drain(final=True)
        code = tool.poll()
        if not tool.started() and code in (126, 127) and not log.stat().st_size:
            ctx.reporter.log(f'wine: the shell could not start the tool (status {code}); running it through a pipe')
            return None
        return code
    except BaseException:
        tool.kill()
        raise
    finally:
        tool.cleanup()


def free_bytes(path):
    """Free bytes on the disk of path (its nearest existing parent) and that parent's device."""
    p = Path(path)
    while not p.exists():
        p = p.parent
    return shutil.disk_usage(p).free, os.stat(p).st_dev


def raise_if_damaged(error, name, path):
    """InstallError check.input_damaged when a reader's ValueError says the file's data fails its hashes or ends early
    (extract.DAMAGE_TEXT)."""
    if extract.DAMAGE_TEXT.search(str(error)):
        raise InstallError('check.input_damaged', input=name, path=str(path), detail=str(error)) from None


def game_version(ctx):
    """The update this install is built from: what the check identified, else the recorded one (an install made before
    1.4.3 support records none: 1.2.1)."""
    return ctx.state.get('version', SUPPORTED[0])


def expected_update(ctx):
    return extract.load_expected()[game_version(ctx)]


class Watch(threading.Thread):
    """Polls a measure() of bytes written while a long call runs and reports it as progress."""

    def __init__(self, reporter, step):
        super().__init__(daemon=True)
        self.reporter, self.step = reporter, step
        self.phase, self.measure, self.total = None, None, None
        self.stopped = threading.Event()

    def set(self, phase, measure=None, total=None):
        self.phase, self.measure, self.total = phase, measure, total
        self.reporter.progress(self.step, phase, 0 if total else None, total, 'bytes' if total else None, force=True)

    def run(self):
        while not self.stopped.wait(1.0):
            phase, measure, total = self.phase, self.measure, self.total
            if phase and measure:
                try:
                    done = min(measure(), total)
                except OSError:
                    continue
                self.reporter.progress(self.step, phase, done, total, 'bytes')

    def stop(self):
        self.stopped.set()


def size_of(paths):
    return sum(p.stat().st_size for p in paths if p.is_file())


# ---- 1. check

class Check(Step):
    id = 'check'
    always = True

    def run(self, ctx):
        layout, rep, dest = ctx.layout, ctx.reporter, ctx.dest
        missing = layout.missing()
        if missing:
            raise InstallError('check.release_incomplete', paths=missing[:5])
        if dest.exists() and not dest.is_dir():
            raise InstallError('check.dest_not_folder', path=str(dest))
        if ctx.mode == 'install' and dest.is_dir() and any(dest.iterdir()):
            raise InstallError('check.dest_not_empty', path=str(dest))
        lang = ini.canonical_language(ctx.options.lang or ctx.state.get('lang'))
        if not lang:
            raise InstallError('check.lang_missing')
        if lang not in ini.LANGUAGES:
            raise InstallError('check.lang_unknown', lang=lang, known=', '.join(ini.LANGUAGES))
        if lang in ini.NEED_SHARED_FONTS:
            rep.warning('warn.lang_needs_fonts', lang=lang)
        ctx.lang = lang
        ctx.state['lang'] = lang   # MP4: recorded here, so a resume of an install that failed before the cleanup needs no --lang
        ctx.temp.mkdir(parents=True, exist_ok=True)

        rep.progress('check', 'host', force=True)
        self.host(ctx)
        rep.progress('check', 'verify', force=True)
        ctx.verify_kept('check')
        ncas = 0
        if ctx.will_run('extract'):
            rep.progress('check', 'inputs', force=True)
            ncas = self.inputs(ctx)
        self.space(ctx, ncas)
        if ctx.will_run('extract'):
            rep.progress('check', 'versions', force=True)
            self.versions(ctx)
        return {}

    def host(self, ctx):
        layout, work = ctx.layout, ctx.temp / 'check'
        work.mkdir(parents=True, exist_ok=True)
        exe = work / ('hostcheck' + EXE)
        command = [str(layout.clang), '-O1', str(HOSTCHECK), '-I', str(layout.deps / 'include'), '-o', str(exe)]
        if os.name != 'nt':
            command.append('-ldl')
        r = subprocess.run(command, capture_output=True, text=True, errors='replace')
        if r.returncode:
            ctx.reporter.log(r.stdout + r.stderr)
            raise InstallError('check.toolchain_broken', detail=(r.stderr or r.stdout).strip()[-400:])
        r = subprocess.run([str(exe)], capture_output=True, text=True, errors='replace')
        try:
            host = json.loads(r.stdout)
        except ValueError:
            raise InstallError('check.host_probe_failed', code=r.returncode) from None
        ctx.reporter.log(f'host: {r.stdout.strip()}')
        if ctx.wine:   # MP4: under Wine or Proton, which build and how much memory (issue #11)
            ctx.reporter.log(f'wine: {json.dumps(ctx.wine)}')
        if not host['x86_64_v3']:
            raise InstallError('check.cpu_no_avx2')
        if not host['loader']:
            raise InstallError('check.vulkan_missing')
        devices = host['devices']
        if not devices:
            raise InstallError('check.vulkan_no_device')
        if not any(tuple(d['api'][:2]) >= (1, 3) for d in devices):
            raise InstallError('check.vulkan_too_old',
                               devices=', '.join(f'{d["name"]} ({d["api"][0]}.{d["api"][1]})' for d in devices))
        ctx.host = host

    def inputs(self, ctx):
        """Open both files with the keys as the extraction will. Returns the bytes of both Program NCAs."""
        o = ctx.options
        for name in ('base', 'update', 'keys'):
            path = getattr(o, name)
            if not path:
                raise InstallError('check.inputs_needed')
            if not Path(path).is_file():
                raise InstallError('check.input_missing', input=name, path=str(path))
        try:
            keys = load_keys(Path(o.keys))
        except (ValueError, OSError, UnicodeDecodeError):
            raise InstallError('check.keys_unreadable') from None
        if 'header_key' not in keys:
            raise InstallError('check.keys_no_header_key')
        total = 0
        for name, is_update in (('base', False), ('update', True)):
            path = Path(getattr(o, name))
            with path.open('rb') as f:
                total += self.open_input(f, path, keys, name, is_update)
        del keys
        return total

    @staticmethod
    def open_input(f, path, keys, name, is_update):
        try:
            kind, entries = container_entries(f, path.stat().st_size)
        except ValueError as e:
            raise_if_damaged(e, name, path)
            raise InstallError('check.not_game_file', input=name) from None
        if is_update and kind != 'nsp':
            raise InstallError('check.update_not_nsp')
        try:
            tickets = common_tickets(f, entries)
        except ValueError as e:
            if 'Personalized' in str(e):
                raise InstallError('check.ticket_personalized', input=name) from None
            raise_if_damaged(e, name, path)
            raise InstallError('check.file_unreadable', input=name, detail=str(e)) from None
        try:
            records = nca_inventory(f, entries, keys)
        except ValueError as e:
            if 'NCA3' in str(e):
                raise InstallError('check.keys_wrong', input=name) from None
            raise_if_damaged(e, name, path)
            raise InstallError('check.file_unreadable', input=name, detail=str(e)) from None
        try:
            record, header = program_nca(records, update=is_update)
        except ValueError as e:
            if is_update and 'BKTR' in str(e):
                raise InstallError('check.update_is_base') from None
            raise InstallError('check.update_not_mp4' if is_update else 'check.base_not_mp4', detail=str(e)) from None
        try:
            nca_ctr_key(header, keys, tickets)
        except ValueError as e:
            if 'ticket' in str(e):
                raise InstallError('check.ticket_missing', input=name) from None
            raise InstallError('check.keys_too_old', input=name,
                               generation=max(header[0x206], header[0x220])) from None
        return record['size']

    def space(self, ctx, ncas):
        dest_need = temp_need = 0
        if ctx.will_run('extract'):
            # the update is identified after this estimate: the larger RomFS of the supported ones
            dest_need += max(extract.load_expected()[v]['romfs']['size'] for v in SUPPORTED) + ncas
            romfs = ctx.dest / 'romfs' / 'romfs.bin'
            if romfs.is_file():   # a resumed extraction replaces it
                dest_need -= romfs.stat().st_size
        if ctx.will_run('prepare') or ctx.will_run('extract'):
            dest_need += DEST_EXTRA
        if ctx.will_run('lift') or ctx.will_run('compile'):
            temp_need += TEMP_NEED
        dest_free, dest_dev = free_bytes(ctx.dest)
        temp_free, temp_dev = free_bytes(ctx.temp)
        ctx.reporter.log(f'space: install folder needs {dest_need / GiB:.1f} GiB of {dest_free / GiB:.1f} free, '
                         f'temp folder {temp_need / GiB:.1f} GiB of {temp_free / GiB:.1f}')
        if dest_dev == temp_dev:
            dest_need += temp_need
            temp_need = 0
        ctx.reporter.emit('space', dest=str(ctx.dest), need=dest_need, free=dest_free, temp=str(ctx.temp),
                          temp_need=temp_need, temp_free=temp_free, same_disk=dest_dev == temp_dev)
        for path, need, free in ((ctx.dest, dest_need, dest_free), (ctx.temp, temp_need, temp_free)):
            if need > free:
                raise InstallError('check.disk_space', path=str(path), need_gb=round(need / 1e9, 1),
                                   free_gb=round(free / 1e9, 1))

    def versions(self, ctx):
        """Extract both ExeFS into the temp folder (verifying every block) and identify the versions by hash."""
        o, work = ctx.options, ctx.temp / 'check'
        logs = ctx.temp / 'logs'
        logs.mkdir(parents=True, exist_ok=True)
        versions = extract.load_expected()
        for name, is_update in (('base', False), ('update', True)):
            out = work / name
            remove(out)
            try:
                extract.extract_exefs(Path(getattr(o, name)), Path(o.keys), out, logs, update=is_update)
            except extract.DamagedFile as e:
                raise InstallError('check.input_damaged', input=name, path=str(e.path), detail=e.detail) from None
            except ValueError:
                raise InstallError('check.exefs_failed', input=name, log=str(logs / f'extract_exefs-{name}.log')) \
                    from None
        problems = extract.exefs_problems(work / 'base' / 'exefs', versions['1.0.0'])
        if problems:
            raise InstallError('check.base_wrong_version', modules=', '.join(problems))
        try:
            found = extract.identify_update(work / 'update' / 'exefs', versions)['display_version']
        except ValueError:
            found = 'unknown'
        if found not in SUPPORTED:
            raise InstallError('check.update_wrong_version', found=found, wanted=' / '.join(SUPPORTED))
        if found != game_version(ctx):   # another update than the recorded one: every step builds again from it
            ctx.reporter.log(f'check: update {found} (recorded {game_version(ctx)})')
            ctx.state['version'] = found
            ctx.keys = {}
            ctx.plan(ctx.forced | {'extract', 'lift', 'compile', 'prepare'})
        ctx.state['version'] = found


# ---- 2. extract

class Extract(Step):
    id = 'extract'

    def key(self, ctx):
        # The output is whatever matches the expected hashes, whichever files and tools produced it.
        return fingerprint('extract-1', ctx.layout.expected(game_version(ctx)))

    def current(self, ctx, record):
        # Another update file than the recorded one (1.2.1 -> 1.4.3) extracts again; the check then identifies it.
        update, known = getattr(ctx.options, 'update', None), ctx.state['inputs'].get('update')
        if update and known and Path(update).is_file():
            st = Path(update).stat()
            if (st.st_size, st.st_mtime_ns) != (known.get('size'), known.get('mtime_ns')) \
                    and file_sha256(Path(update)) != known.get('sha256'):
                return False
        expected = expected_update(ctx)
        romfs = ctx.dest / 'romfs'
        if extract.exefs_problems(ctx.dest / 'exefs', expected) or not extract.romfs_verified(romfs, expected):
            return False
        st = (romfs / 'romfs.bin').stat()
        return st.st_size == record.get('romfs_size') and st.st_mtime_ns == record.get('romfs_mtime_ns')

    def verify(self, ctx, record):
        image = ctx.dest / 'romfs' / 'romfs.bin'
        rep = ctx.reporter
        digest = file_sha256(image, lambda d, t: rep.progress('check', 'verify_romfs', d, t, 'bytes'))
        return digest == expected_update(ctx)['romfs']['sha256']

    def run(self, ctx):
        o, dest, rep = ctx.options, ctx.dest, ctx.reporter
        expected = expected_update(ctx)
        base, update, keys = Path(o.base), Path(o.update), Path(o.keys)
        inputs = {}
        for name, path in (('base', base), ('update', update)):
            known = ctx.state['inputs'].get(name, {})
            st = path.stat()
            if known.get('size') == st.st_size and known.get('mtime_ns') == st.st_mtime_ns and known.get('sha256'):
                inputs[name] = known
                continue
            digest = file_sha256(path, lambda d, t, n=name: rep.progress('extract', 'hash_' + n, d, t, 'bytes'))
            inputs[name] = {'name': path.name, 'size': st.st_size, 'mtime_ns': st.st_mtime_ns, 'sha256': digest}
        ctx.state['inputs'] = inputs
        ctx.save()

        dest.mkdir(parents=True, exist_ok=True)
        for stale in dest.glob('update-merge-*'):   # a killed merge leaves its NCA copies (and key copies) behind
            remove(stale)
        rep.progress('extract', 'exefs', force=True)
        staged = ctx.temp / 'check' / 'update' / 'exefs'
        if not staged.is_dir():
            remove(ctx.temp / 'check' / 'update')
            try:
                extract.extract_exefs(update, keys, ctx.temp / 'check' / 'update', ctx.temp / 'logs', update=True)
            except extract.DamagedFile as e:
                raise InstallError('check.input_damaged', input='update', path=str(e.path), detail=e.detail) from None
            except ValueError:
                raise InstallError('check.exefs_failed', input='update',
                                   log=str(ctx.temp / 'logs' / 'extract_exefs-update.log')) from None
        remove(dest / 'exefs')
        shutil.move(staged, dest / 'exefs')
        problems = extract.exefs_problems(dest / 'exefs', expected)
        if problems:
            raise InstallError('extract.exefs_mismatch', version=game_version(ctx), modules=', '.join(problems))
        if extract.sha256(dest / 'exefs' / 'main.npdm') != expected['exefs_sha256']['main.npdm']:
            rep.warning('warn.npdm_differs', version=game_version(ctx))

        romfs = dest / 'romfs'
        if self.id in ctx.forced:
            remove(romfs)   # the full hash failed: index.json still claims the extraction's hash
        if not extract.romfs_verified(romfs, expected):
            self.merge(ctx, base, update, keys, romfs, expected)
        report = json.loads((romfs / 'index.json').read_text())
        if report['romfs_size'] != expected['romfs']['size'] or report['romfs_sha256'] != expected['romfs']['sha256']:
            raise InstallError('extract.romfs_mismatch', version=game_version(ctx), size=report['romfs_size'],
                               sha256=report['romfs_sha256'][:16])
        (dest / 'profile').mkdir(exist_ok=True)
        st = (romfs / 'romfs.bin').stat()
        return {'romfs_size': st.st_size, 'romfs_mtime_ns': st.st_mtime_ns}

    def merge(self, ctx, base, update, keys, romfs, expected):
        dest, logs = ctx.dest, ctx.temp / 'logs'
        ncas = extract.merge_space(base, update, keys)
        watch = Watch(ctx.reporter, 'extract')

        def phase(name):
            if name == 'copy':
                watch.set('copy', lambda: size_of(dest.glob('update-merge-*/*.nca')), ncas)
            elif name == 'merge':
                watch.set('merge', lambda: size_of(dest.glob('update-merge-*/result/romfs.bin')),
                          expected['romfs']['size'])
            else:
                watch.set(name)

        watch.start()
        try:
            extract.merge_update_romfs(base, update, keys, ctx.layout.hactool, romfs, logs, phase=phase)
        except extract.DamagedFile as e:
            raise InstallError('check.input_damaged', input='base' if e.path == base else 'update', path=str(e.path),
                               detail=e.detail) from None
        except ValueError as e:
            raise InstallError('extract.romfs_merge_failed', detail=str(e), log=str(logs / 'merge_romfs-'
                                                                                     f'{dest.name}.log')) from None
        finally:
            watch.stop()


# ---- 3. lift

EMITTED = re.compile(r'^emitted (\d+)/(\d+) chunks')
DISCOVERED = re.compile(r'^(\w+): \d+ functions \(.*discovery')


class Lift(Step):
    id = 'lift'
    transient = True

    def key(self, ctx):
        return fingerprint('lift-1', ctx.keys['extract'], ctx.layout.recomp, ctx.layout.patches(game_version(ctx)))

    def current(self, ctx, record):
        gen = ctx.temp / 'gen'
        stamp = gen / STAMP
        return (gen / 'layout.json').is_file() and stamp.is_file() and stamp.read_text().strip() == ctx.keys['lift']

    def run(self, ctx):
        layout, rep, gen = ctx.layout, ctx.reporter, ctx.temp / 'gen'
        remove(gen)
        rep.progress('lift', 'discover', force=True)

        def parse(line):
            m = EMITTED.match(line)
            if m:
                rep.progress('lift', 'emit', int(m.group(1)), int(m.group(2)), 'chunks')

        code, log = run_tool(ctx, 'lift', layout.recomp / 'gen.py',
                             ['--exefs', ctx.dest / 'exefs', '--out', gen, '--modules', 'main,subsdk0,sdk',
                              '--patches', layout.patches(game_version(ctx))], parse)
        if code:
            raise InstallError('lift.failed', code=code, log=str(log))
        (gen / STAMP).write_text(ctx.keys['lift'] + '\n')
        return {}


# ---- 4. compile

NINJA_LINE = re.compile(r'^\[(\d+)/(\d+)\] ')


class Compile(Step):
    id = 'compile'

    def key(self, ctx):
        layout = ctx.layout
        clang = subprocess.run([str(layout.clang), '--version'], capture_output=True, text=True).stdout
        tools = [layout.tools / n for n in ('build_game.py', 'romfs_index.py', 'check_content.py')]
        return fingerprint('compile-1', ctx.keys['lift'], layout.runtime, layout.runtime_lib, layout.deps, *tools,
                           clang, *[d.name for d in layout.dlls])

    def current(self, ctx, record):
        exe = ctx.dest / ('mp4' + EXE)
        if not exe.is_file() or not (ctx.dest / 'romfs-index.bin').is_file():
            return False
        if not all((ctx.dest / d.name).is_file() for d in ctx.layout.dlls):
            return False
        return file_sha256(exe) == record.get('exe_sha256')

    def run(self, ctx):
        layout, rep, dest = ctx.layout, ctx.reporter, ctx.dest
        build = ctx.temp / 'build'
        phase = ['compile']

        def parse(line):
            m = NINJA_LINE.match(line)
            if m:
                rep.progress('compile', phase[0], int(m.group(1)), int(m.group(2)), 'objects')
            elif line.startswith('compile: '):
                phase[0] = 'link'
                rep.progress('compile', 'link', force=True)

        rep.progress('compile', 'compile', force=True)
        code, log = run_tool(ctx, 'compile', layout.tools / 'build_game.py', [
            '--gen', ctx.temp / 'gen', '--out', build, '--toolchain', layout.llvm, '--deps', layout.deps,
            '--runtime', layout.runtime, '--runtime-lib', layout.runtime_lib, '--ninja', layout.ninja,
            '--version', version_tag(game_version(ctx)), '--expected', layout.expected(game_version(ctx)),
            '--exefs', dest / 'exefs', '--romfs', dest / 'romfs',
            '--profile', dest / 'profile'], parse)
        exe = build / ('mp4' + EXE)
        if code or not exe.is_file():
            raise InstallError('compile.failed', code=code, log=str(log))
        try:
            replace_file(exe, dest / exe.name)
        except PermissionError:
            raise InstallError('compile.exe_in_use', path=str(dest / exe.name)) from None
        replace_file(build / 'romfs-index.bin', dest / 'romfs-index.bin')
        for dll in layout.dlls:
            replace_file(dll, dest / dll.name)
        return {'exe_sha256': file_sha256(dest / exe.name)}


# ---- 5. prepare

# MP4: the step installs the shader translator, prepares the shaders the release's list names and fetches the video
# codec. TotK's step also built RomFS patches; MP4 has none. The list (data/shader-list.bin, the variants play met, no
# game code) is resolved against the player's own RomFS by tools/prepare_shaders.py, which runs mp4.exe
# --prepare-shaders: every listed variant is translated into shader-cache/ and built into the driver's cache, so a scene
# met for the first time does not wait for its shaders. tools/pack_shader_cache.py then folds the cache into one file.
# A dev checkout without build/shader-list.bin skips both; the shaders are then translated as they are met in play.
# The pipeline list (data/pipeline-list.bin, the pipelines play built, render state and shader input ids only) goes to
# the same tool, which merges it into pipeline-keys.bin; --prepare-shaders then builds every listed pipeline into
# pipeline-cache.bin and the driver's cache, and each boot's prewarm finds them there. Without it the pipelines are
# built as they are met in play, and their draws are skipped meanwhile (2,247 in a scripted first-area play).
TRANSLATED = re.compile(r'gfx_shader: prepare: (\d+) of (\d+) pairs')

# The cinematics are H.264, decoded by openh264 (runtime/ov_movie.c h264_lib, which loads OPENH264_DLL next to
# mp4.exe). Cisco's H.264 patent licence covers its own binary only when that binary is downloaded separately to the
# player's machine, so the release carries no openh264 binary: this step downloads Cisco's from Cisco, checks it against
# the pinned hash and unpacks it. The game cannot be played without it (the opening cinematic never ends: issue #2, and
# mp4.exe refuses to start, runtime/ov_movie.c movie_require_codec), so a failed download fails the step, before the
# shaders, and the next run of the setup retries. Cisco's licence text is in THIRD_PARTY_NOTICES.md.
OPENH264_DLL = 'openh264-2.7.0-win64.dll'
OPENH264_URL = 'https://ciscobinary.openh264.org/openh264-2.7.0-win64.dll.bz2'
OPENH264_BZ2_SHA256 = '7073fda55e904ac515f787d9728c4bcf9965ec7c91c9f84eae3f260ef9b9b37c'
OPENH264_SHA256 = '0ef5e7f3b38753b15c13afa0418d4d7805fa228e6adfd3e425bbf758cff1c288'   # Cisco's .signed.sha256.txt


def local_openh264(ctx, dest):
    """Take a copy the player downloaded themselves (the download failed, or they would rather fetch it in a browser):
    the .dll or Cisco's .bz2, in the game folder or the setup's own folder, accepted only with Cisco's hash. True when
    dest now holds it."""
    import bz2
    for folder in dict.fromkeys((dest.parent, ctx.layout.root)):
        for name in (OPENH264_DLL, OPENH264_DLL + '.bz2'):
            src = folder / name
            if not src.is_file() or src == dest:
                continue
            try:
                data = src.read_bytes()
                if name.endswith('.bz2'):
                    if hashlib.sha256(data).hexdigest() != OPENH264_BZ2_SHA256:
                        continue
                    data = bz2.decompress(data)
            except (OSError, ValueError, EOFError):
                continue
            if hashlib.sha256(data).hexdigest() == OPENH264_SHA256:
                tmp = dest.with_name(dest.name + '.tmp')
                tmp.write_bytes(data)
                os.replace(tmp, dest)
                ctx.reporter.log(f'openh264: taken from {src}')
                return True
    return False


def fetch_openh264(ctx, dest):
    """Download Cisco's DLL to dest; None when done, else the reason it failed."""
    import bz2
    import urllib.request
    tmp = dest.with_name(dest.name + '.tmp')
    try:
        with urllib.request.urlopen(OPENH264_URL, timeout=60) as r:
            packed = r.read()
        if hashlib.sha256(packed).hexdigest() != OPENH264_BZ2_SHA256:
            return 'the download does not match the expected hash'
        data = bz2.decompress(packed)
        if hashlib.sha256(data).hexdigest() != OPENH264_SHA256:
            return 'the unpacked file does not match the expected hash'
        tmp.write_bytes(data)
        os.replace(tmp, dest)
    except (OSError, ValueError, EOFError) as e:
        remove(tmp)
        return str(e)[:200]
    return None


def prepare_shaders(ctx, dest):
    """Translate the listed shader variants into dest's shader cache and pack it (MP4 change: TotK's list step, with
    this project's list and inventory)."""
    layout, rep = ctx.layout, ctx.reporter

    def parse(line):
        m = TRANSLATED.search(line)
        if m:
            rep.progress('prepare', 'translate', int(m.group(1)), int(m.group(2)), 'shaders')
        elif 'listed variants' in line:
            rep.progress('prepare', 'translate', force=True)
        elif 'listed pipelines' in line:
            rep.progress('prepare', 'pipelines', force=True)
        elif 'gfx_shader: prepared' in line or 'stage library prebuild' in line:
            rep.progress('prepare', 'libraries', force=True)

    rep.progress('prepare', 'seed', force=True)
    code, log = run_tool(ctx, 'prepare', layout.tools / 'prepare_shaders.py',
                         [dest, '--romfs', dest / 'romfs', '--list', layout.shader_list,
                          *(['--pipelines', layout.pipeline_list] if layout.pipeline_list else [])], parse)
    if code or not (dest / 'shader-prepared.bin').is_file():
        raise InstallError('prepare.shaders_failed', code=code, log=str(log))
    # One shader-cache.pack instead of four files per shader (about 140,000 after preparing): mp4.exe reads the pack
    # first.
    rep.progress('prepare', 'pack', force=True)
    code, log = run_tool(ctx, 'prepare', layout.tools / 'pack_shader_cache.py', [dest / 'shader-cache'])
    if code:
        raise InstallError('prepare.pack_failed', code=code, log=str(log))


class Prepare(Step):
    id = 'prepare'

    def key(self, ctx):
        layout = ctx.layout
        tools = [layout.tools / n for n in ('prepare_shaders.py', 'shader_inventory.py', 'pack_shader_cache.py')]
        return fingerprint('prepare-mp4-4', ctx.keys['compile'], layout.xlate, layout.xlate.with_suffix('.id'),
                           OPENH264_SHA256,
                           *([layout.shader_list] if layout.shader_list else []),
                           *([layout.pipeline_list] if layout.pipeline_list else []), *tools)

    def current(self, ctx, record):
        xlate = ctx.dest / ctx.layout.xlate.name
        codec = ctx.dest / OPENH264_DLL
        codec_ok = codec.is_file() and file_sha256(codec) == OPENH264_SHA256
        prepared = not ctx.layout.shader_list or (ctx.dest / 'shader-prepared.bin').is_file()
        return xlate.is_file() and file_sha256(xlate) == record.get('xlate_sha256') and codec_ok and prepared

    def run(self, ctx):
        layout, rep, dest = ctx.layout, ctx.reporter, ctx.dest
        codec = dest / OPENH264_DLL
        notice = dest / 'openh264-BINARY_LICENSE.txt'   # Cisco's licence, next to the binary it covers
        replace_file(Path(__file__).resolve().parent / 'text' / notice.name, notice)
        if not (codec.is_file() and file_sha256(codec) == OPENH264_SHA256) and not local_openh264(ctx, codec):
            rep.progress('prepare', 'openh264', force=True)
            problem = fetch_openh264(ctx, codec)
            if problem:
                rep.log(f'openh264: {problem}')
                raise InstallError('prepare.openh264_failed', detail=problem, url=OPENH264_URL, file=OPENH264_DLL,
                                   dest=str(dest))
        rep.progress('prepare', 'translator', force=True)
        xlate = dest / layout.xlate.name
        if xlate.is_file() and file_sha256(xlate) != file_sha256(layout.xlate):
            # Another translator: its cache keys differ, so the old translations would only take space.
            for name in ('shader-cache', 'shader-answers.bin', 'shader-programs.bin', 'shader-prepared.bin'):
                remove(dest / name)
        replace_file(layout.xlate, xlate)
        xlate_id = layout.xlate.with_suffix('.id')
        if xlate_id.is_file():
            replace_file(xlate_id, xlate.with_suffix('.id'))
        else:
            remove(xlate.with_suffix('.id'))
        if layout.shader_list:
            prepare_shaders(ctx, dest)
        else:
            rep.log('prepare: no shader list (a dev checkout without build/shader-list.bin): shaders are translated in play')
        return {'xlate_sha256': file_sha256(xlate)}


# ---- 6. clean up

class Cleanup(Step):
    id = 'cleanup'
    always = True

    def run(self, ctx):
        rep, dest = ctx.reporter, ctx.dest
        rep.progress('cleanup', 'ini', force=True)
        try:
            ini.write(dest, ctx.lang, ctx.layout.ini_example)
        except OSError as e:
            raise InstallError('cleanup.ini_failed', detail=str(e)) from None
        ctx.state['lang'] = ctx.lang
        (dest / 'profile').mkdir(exist_ok=True)
        rep.progress('cleanup', 'temp', force=True)
        for stale in dest.glob('update-merge-*'):
            remove(stale)
        for name in ('gen', 'build', 'check'):
            remove(ctx.temp / name)
        romfs_problem(dest / 'romfs', expected_update(ctx))
        return {}


def romfs_problem(romfs, expected):
    """Raise cleanup.romfs_unreadable unless romfs/ still holds the verified image. The game opens romfs.bin at start;
    an install whose romfs/ went empty and unreadable after the extraction verified it (a folder Windows was still
    deleting, #107) reported success and the game stopped at its first file read."""
    try:
        ok = extract.romfs_verified(romfs, expected) and (romfs / 'romfs.bin').stat().st_size == expected['romfs']['size']
        if ok:
            with open(romfs / 'romfs.bin', 'rb') as f:
                f.read(1)
    except (OSError, ValueError):
        ok = False
    if not ok:
        raise InstallError('cleanup.romfs_unreadable', path=str(romfs))


def all_steps():
    return [Check(), Extract(), Lift(), Compile(), Prepare(), Cleanup()]
