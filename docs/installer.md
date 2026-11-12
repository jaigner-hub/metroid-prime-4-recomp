<!-- From fe3h-pc-recomp docs/installer.md @ 47ef99126793dbd13e4a475feb910a13ef6639b8 (MIT, Copyright (c) 2026 Jeff Aigner), renamed to mp4; MP4 changes are noted where made. -->
<!-- From totk-pc-recomp docs/installer.md @ c944f9f94520874f2a669add30dd08d29ee9f7b5 (MIT, Copyright (c) 2026 Valiantsin Dzerakh), adapted to MP4. -->
# Installer

A public release that turns the player's own game files into a playable MP4 folder. The release carries no game
data and no code derived from the game: the installer builds the game on the player's machine. The installed folder
holds only the game; the installer and its tools stay outside it.

The installer is totk-pc-recomp's (`installer/`, MIT), renamed and changed where this game differs: one supported
update line, our extraction tools, no RomFS patches, and a shader list of our own (see "Shader list").

## What the player does

1. Downloads `MP4-Setup-<version>-windows.zip` from GitHub Releases, unpacks it anywhere and starts `MP4-Setup.exe`.
2. Picks:
   - the base game (XCI or NSP) and the 1.1.0 update (NSP), the last Switch 1 update;
   - their `prod.keys`;
   - the game language and the install folder.
3. Waits for the setup, one time. The window shows each step and its progress.
4. Plays `mp4.exe` from the install folder. The installer and the source files can be deleted.

The install folder (`main.c` `portable_paths()` reads this layout; it stays portable):

```
<install folder>/
  mp4.exe, SDL3.dll, libopus-0.dll, mp4_xlate.exe
  openh264-2.7.0-win64.dll       Cisco's OpenH264 binary, downloaded from Cisco by the setup (required),
  openh264-BINARY_LICENSE.txt    with Cisco's licence for it
  mp4.ini                       language; the other keys as commented defaults (mp4.ini.example)
  exefs/, romfs/                 extracted from the player's files
  profile/                       saves
  logs/                          mp4.log of the last run, mp4.1.log to mp4.4.log of the runs before it
  shader-cache/                  the prepared translations (shader-cache.pack) and those made in play since
  shader-prepared.bin            the variants the setup prepared, which every start rebuilds into the driver's cache
  shader-answers.bin             the answers met in play
  mp4-install.json              what built this folder: release version, input hashes, finished steps
```

## Updating the port

The player runs a newer installer on the same folder. It reads `mp4-install.json`, keeps `romfs/`, `exefs/`,
`profile/` and `mp4.ini` after checking their hashes, rebuilds `mp4.exe` and reinstalls the translator. A newer
release that changes nothing game-derived skips the rebuild.

## Setup steps

1. **Check** before writing anything: the files open with the keys, the base is 1.0.0 and the update 1.1.0 (by the
   hashes of their code modules, `tools/data/expected-v*.json`), the keys are new enough for the update, enough space
   is free on the target disk (about 61 GiB at the peak of a first install, 35 GiB after it, most of it the 26 GiB RomFS), the CPU has AVX2
   (`-march=x86-64-v3`), a Vulkan 1.3 GPU exists. Each failure is one clear message.
2. **Extract** the ExeFS of the update and the RomFS (the base image with the update's BKTR patch, merged by hactool,
   which verifies every IVFC level) into the install folder and verify the hashes.
3. **Lift** the ExeFS to C (`recomp/gen.py`, `recomp/patches-v<version>.txt`) into a temp folder next to the installer.
4. **Compile** the generated C, `modules.c`, `romfs.c` and `patches.c` (they bake the per-install layout, RomFS hashes
   and the game version's patch page) and link them with the prebuilt runtime library into `mp4.exe`
   (`build_game.py --runtime-lib`).
5. **Prepare**: install the shader translator (`mp4_xlate.exe`) next to the game, then prepare the shaders the
   release's list names (`tools/prepare_shaders.py`: find each in the player's RomFS, `mp4.exe --prepare-shaders`
   translates them and builds them into the driver's cache, about 2 minutes for 35,000 variants on 12 cores) and pack
   the cache into one file (`tools/pack_shader_cache.py`). Shaders the list lacks are translated as they are met in
   play. First of all, Cisco's OpenH264 binary, which the game needs (see "Release layout").
6. **Clean up** the temp folder (generated C and objects).

A cancelled or failed setup resumes from the last finished step (`mp4-install.json`).

## Command line

`installer/` (stdlib only; `installer/layout.py` maps the release folder and, in a repository, the dev toolchain):

```
python -m installer --base FILE --update FILE --keys FILE --lang CODE --dest DIR [--root DIR] [--temp DIR] [--verify]
```

- On a folder with `mp4-install.json` only `--dest` is needed; the game files only when the extraction must run again.
- stdout: one JSON event per line, types `start`, `plan`, `step` (started, done, skipped), `progress` (step, phase,
  done/total/unit), `space` (bytes needed and free on the install disk and the temp disk), `warning`, `error`, `end`
  (`installer/events.py`). Messages are ids with parameters; `installer/text/en.json` holds their English text. The
  human-readable log goes to `<temp>/logs/`.
- Exit codes: 0 done, 1 failed, 2 bad command line, 3 cancelled. The window cancels with Ctrl+Break to the
  installer's process group.
- Each step records a key over its inputs (runner.py). A step with the recorded key and intact output is skipped, so
  resume and update are one rule; output kept from another release is re-hashed first.
- Peak space on the install disk: the extraction copies both Program NCAs (about 26.3 GiB) next to the merged RomFS
  (26.2 GiB) before it deletes them; the shader and pipeline caches reach about 5.5 GiB after the first area. The check computes the exact figure.
- Window (`python -m installer.gui`, `installer/gui.py` draws, `installer/guilogic.py` decides): form, step list with
  progress, space line, collapsible log, Cancel, finish screen with Open folder. English or Russian, chosen from the
  Windows display language, switchable in the window; every sentence is an id in `installer/text/<en|ru>.json`. A
  folder with `mp4-install.json` needs only the folder. `--selftest [--shots DIR]` builds every screen in both
  languages and closes itself.
- Under Wine or Proton (`installer/wine.py`): the check logs the Wine build, the Linux kernel, the host's memory and a
  fixed list of Proton variables (`wine: {...}`). Lift, compile and prepare start their tool through `/bin/sh`, its
  output appended to `<step>-<tool>.log` by the shell, so Wine's own messages (written to the Linux stderr, which a
  Windows pipe never sees) land in that log too; the tool tells its Windows process id through a file, the setup
  waits on that process and reads the log as it grows. When `/bin/sh` or the Wine loader is not there, the tool runs
  through the pipe as on Windows. Every tool runs with `-X faulthandler`, so a crash in native code leaves its Python
  stack in the log. Issue #11: before this, `gen.py` under Proton left an empty log and only exit code 0x80000100.
- `tests/installer_test.py`: bookkeeping with fake steps.
- Dev mode (a repository checkout, no `python/python.exe`): the Python that runs the module runs the tools, so use
  one with numpy, lz4 and cryptography (`.venv-win`); `toolchain/llvm-mingw-*` and `toolchain/deps-mingw` when they
  exist, else MSYS2 UCRT64's clang, ninja, headers, libraries and DLLs (the usual build's); the runtime library from
  `build/install-rtlib` (`tools/build_game.py --make-runtime-lib --out build/install-rtlib`); `toolchain/hactool/hactool.exe`
  (`tools/build_hactool.py` on Windows); `build/xlate/mp4_xlate.exe`; the temp folder `build/install-temp`.

## Release layout

The unpacked release; `tools/make_release.py` assembles it and `--zip` packs it:

```
MP4-Setup.exe        starts python/pythonw.exe -m installer.gui (installer/launcher/launcher.c, no console)
installer/            the installer: steps, CLI, window, en/ru text catalog
python/               Python 3.14 embeddable + numpy, lz4, cryptography, cffi, pycparser (+ tkinter for the window)
recomp/, tools/       the lifter, the build, extraction and shader scripts the steps run (copied from the repo), tools/data/
data/shader-list.bin  the shader list (see "Shader list"); not in the repository
llvm/                 llvm-mingw, x86_64-only subset (Apache-2.0 with LLVM exception; mingw-w64 parts permissive)
runtime/              libmp4rt.a and the headers the generated code and modules.c/romfs.c/patches.c include
bin/                  mp4_xlate.exe, hactool.exe, SDL3.dll, libopus-0.dll, ninja.exe (no openh264: see below)
THIRD_PARTY_NOTICES.md
mp4.ini.example, VERSION  the default ini; the release string (the repository's VERSION plus the commit)
```

- SDL3 comes from the upstream release, not MSYS2: MSYS2's DLL imports the LGPL libiconv.
- openh264, the cinematics' H.264 decoder: Cisco's H.264 patent licence covers Cisco's own binary only when it is
  downloaded separately to the player's machine. So the release carries only openh264's headers; the Prepare step,
  before the shaders, downloads Cisco's `openh264-2.7.0-win64.dll` from ciscobinary.openh264.org, checks it against
  Cisco's published SHA-256 and puts it, with `openh264-BINARY_LICENSE.txt`, next to `mp4.exe`. The window shows
  "OpenH264 Video Codec provided by Cisco Systems, Inc." There is no opt-out: without the codec the opening cinematic
  never ends (the game's movie player stops feeding a decoder that returns no frames and waits, a black screen with
  music, issue #2), so `mp4.exe` refuses to start without it, with a message box saying how to get it
  (`runtime/ov_movie.c` movie_require_codec). A failed download fails the step. A player who fetches the file
  themselves puts Cisco's `.bz2` or the unpacked `.dll` in the install folder (or the setup folder) and runs the setup
  again: Prepare takes a copy with Cisco's hash (`installer/steps.py` local_openh264) and the setup goes on from there.
- Every bundled third-party piece gets a row in `THIRD_PARTY_NOTICES.md`.
- The release contains nothing game-derived; `make_release.py` runs `tools/check_content.py` over the folder before it
  finishes.

## Constraints

- The installer steps contain no Win32 calls: Linux follows with its own compiler and the same steps.
- The keys file path is passed to the tools; key values never appear in the UI, logs or files.
- The window is a front end over the CLI, which prints one JSON progress event per line. Tests and CI use the CLI.
- Paths may hold any Unicode characters. Every exe the setup runs or builds reads its arguments and paths as UTF-8
  through an `activeCodePage` manifest: hactool (our patch), mp4.exe (`runtime/mp4.rc`), ninja (upstream).
- A game file whose data fails its hash checks or ends early stops the setup with `check.input_damaged`, which names
  the file and asks to copy it again (`tools/extract.py` `DamagedFile`).
- Unsigned exe: SmartScreen warns on first start.

## Later

- An icon for `mp4.exe` and the setup (`runtime/mp4.ico`; the launcher builds without one for now).
- A GitHub release workflow: a release is `make_release.py --zip` on a machine with uv and Python 3.14, a tag at the
  commit the zip names in its `VERSION`, and `gh release create` with the zip as `MP4-Setup-<version>-windows.zip`.
  NSZ and XCZ input. Linux release.

## Shader list

The game's shaders are Maxwell code in `SNVN` chunks: `MaterialArchive.arc` and every material (`MTRL`) and vector
graphic (`SVGM`) asset of the paks and the update's patch pak, about 260,000 programs (`tools/shader_inventory.py`). Each
is translated per variant (the texture types and vertex formats it is bound with), so translating every program up
front would cost hours and many gigabytes and still miss the variants the game asks for. The list names the variants
play met instead: a file in the layout of `shader-answers.bin` (stage, the runtime's code hash, the binding answers)
with the input ids zeroed. It holds no game code; `tools/prepare_shaders.py` finds each program in the player's own
RomFS by its hash. The list ships in the release (`data/shader-list.bin`) and stays out of the repository.

To make or extend it, play a build (the runtime records every variant in `shader-answers.bin` next to `mp4.exe`) and
merge the files:

```
python tools/prepare_shaders.py --make-list build/shader-list.bin build/v131072/shader-answers.bin [more files]
```

`tools/make_release.py --shader-list` takes it (default `build/shader-list.bin`). On a fresh copy of the build, the list
of one play of the first area (34,812 variants of 2,826 programs) took the translations in play from 1,883 to 14 and the
draws that waited for a shader from 24,549 to 30, and shortened the longest frame of the save load from 2.4 to 1.6 s.

## Pipeline list

A draw whose Vulkan pipeline is not built yet is skipped while a worker builds it (`runtime/vk_pipeline.c`). Each
build appends a record to `pipeline-keys.bin` (the render state, the shaders named by input id, a hash of the code hash
and the answers; no game code), and every boot builds the file's records before the guest starts. A fresh install has
no such file, so the release carries one: `data/pipeline-list.bin`, records in the same layout, which
`tools/prepare_shaders.py --pipelines` merges into the install's `pipeline-keys.bin` (the player's own records stay)
before `mp4.exe --prepare-shaders` builds every record into `pipeline-cache.bin` and the driver's cache. The release
ships it when there is one; without it the pipelines are built in play.

```
python tools/prepare_shaders.py --make-pipeline-list build/pipeline-list.bin build/v131072/pipeline-keys.bin [more files]
```

`tools/make_release.py --pipeline-list` takes it (default `build/pipeline-list.bin`). Measured 2026-11-05 on a copy
prepared from the shader list, the same scripted first-area play: without the list 1,548 pipelines were built in play
and 2,247 draws skipped while they built; with the list of 27,286 records (26,732 built at preparation in 4 s, the rest
name shaders the shader list does not hold) 53 and 75, and the boot waited 1.5 s for the prewarm. The longest frames
of that play are texture uploads at the save load, which the list does not change.
