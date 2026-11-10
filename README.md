# Metroid Prime 4 PC Recomp

Research project: static recompilation of *Metroid Prime 4: Beyond* (Switch 1 release, ARM64) into a native Windows
program, by the method of [totk-pc-recomp](https://github.com/valentderah/totk-pc-recomp) as carried to a second game
by [fe3h-pc-recomp](https://github.com/jaigner-hub/fe3h-pc-recomp). The game's code is lifted to C, compiled for
x86-64 and linked with a host runtime that stands in for the Switch OS and for NVN, its graphics API, on Vulkan.

This is the third game on that code. Besides a port, the project answers how much of a Switch recompilation is shared
code that a fourth game gets for free: the lifter, runtime and renderer came over from fe3h-pc-recomp with the game
name renamed and a handful of game-bound tables rewritten ([docs/plan.md](docs/plan.md), "Shared core").

## Status

Playable from the start: the title screen, menus, saves, the opening cinematic (H.264 movies with AAC audio) and the
first area through its first boss, with sound, a controller or keyboard and mouse, and the morph ball; 60 fps on an
RTX 4070, with the stutter described below, and higher rates on request (see "Frame rate"). Game time follows real
time, so any frame rate keeps the game's pace. Only that much of the game has been played so far: later areas will bring visual glitches, and maybe crashes,
that nobody has seen yet. The fixes so far are listed in [docs/plan.md](docs/plan.md), which also records Phase 0,
the survey of the binaries. Version 1.1.0, the final Switch 1 update, is the target.

Expect, for now:

- Objects missing for a moment, or a stutter, the first time a scene is seen, while its pipelines are built (and its
  shaders translated, for scenes the setup's shader list does not cover yet: the list holds what play has met so far,
  the first area for now). Both are cached, so it happens once.
- Rendering glitches past the first area. To report one, add to `mp4.ini`

  ```
  [capture]
  shot_ring = 8
  shots_dir = shots
  dump = when
  dump_dir = dump
  ```

  and press P while it is on screen: the last 8 frames go to `shots` as PNGs and the next frame's render passes to
  `dump` (about 1 GB, and a short hitch); both paths are relative to the folder the game was started from. The log
  is `logs\mp4.log` next to the exe. The ring copies every frame back from the GPU and lowers the frame rate a lot in
  heavy areas, so remove the `[capture]` lines again once you have the frames.

This repository contains no game content. You need your own dump of the game (XCI or NSP), its 1.1.0 update NSP, and
the keys dumped from your own console. The content check (`tools/check_content.py`, run by the pre-commit hook and
CI) keeps it that way.

## Installing (players)

The setup builds the game on your machine from your own files; nothing from the game is in the download.

1. Download `MP4-Setup-<version>-windows.zip` from the GitHub Releases page, unpack it anywhere and start
   `MP4-Setup.exe` (Windows warns about an unsigned program the first time).
2. Pick the base game (XCI or NSP), the 1.1.0 update (NSP), your `prod.keys`, the game language and an install
   folder. The setup downloads the OpenH264 video codec from Cisco, which the game needs. If that download fails, get
   https://ciscobinary.openh264.org/openh264-2.7.0-win64.dll.bz2 yourself, put it in the install folder (as it is, or
   unpacked with 7-Zip; renaming it is not enough) and run the setup again. OpenH264 Video Codec provided by Cisco
   Systems, Inc.
3. Wait: about 8 minutes on a fast SSD, 2 of them preparing the shaders. The install folder needs about 61 GB free during the setup and 35 GB after it.
4. Start `mp4.exe` in the install folder. The setup folder and your game files are no longer needed.

You need Windows 10 or later, a CPU with AVX2 and a GPU with Vulkan 1.3. To update, run a newer setup on the same
folder; it keeps your saves. Details: [docs/installer.md](docs/installer.md).

## Settings and frame rate

F1 or Esc opens the in-game menu; its changes are saved to `mp4.ini` next to `mp4.exe`. Every setting, with its
default, is in [mp4.ini.example](mp4.ini.example), and a `MP4_*` environment variable of the same name overrides the
file (`MP4_FPS_LIMIT` for `fps_limit`). F10 cycles the performance overlay: off, the frame rate, and a panel with
frame times.

The Switch runs the game at 60 fps, and that is the default limit. "Frame rate limit" in the menu (`fps_limit`) also
offers 30, 90, 120, 144 and the monitor's refresh rate (`display`). Above 60 is experimental:

- The game keeps its pace at any rate. With the same inputs at 60 and 120 fps, Samus walks and turns the same
  distance in the same time; cinematics and movies keep time with their sound. This needs "Game speed follows the
  clock" (`realtime`, on by default).
- Not everything has been checked yet. Some effects or animations may still step once per frame and run fast above
  60; please report what you see. The title screen moves on from its intro movie sooner at higher rates.
- Your hardware decides how high it goes. On an RTX 4070 the first area runs at 110 to 120 fps in most views and
  about 80 in the heaviest, where the CPU side limits it; with `display` on a 240 Hz monitor the light views reach
  200 fps.

## What you need to build from source

Windows 10 or later, a Vulkan 1.3 GPU, and the toolchain totk-pc-recomp's `docs/building.md` describes: MSYS2
UCRT64 (clang, lld, ninja, SDL3, opus, libvpx, Vulkan, vulkan-memory-allocator), the .NET 10 SDK and Visual Studio
Build Tools for the shader translator, and Python 3.12. The lifter and its tests also run under WSL with the
`.venv` of `tools/requirements.txt`.

## Building

1. Copy `config.example.toml` to `config.local.toml` and point it at your game, update and keys.
2. `python tools/build_hactool.py`, then `python tools/extract_exefs.py <game> --keys <keys> --output build/extracted/v0/exefs`
   for the base and, with `--update`, `--output build/extracted/v131072/exefs` for the update;
   `python tools/extract_romfs.py` merges the update's RomFS (needs about three times the RomFS size, 27 GB, free).
3. `python recomp/gen.py --exefs build/extracted/v131072/exefs --out generated/v131072 --modules main,subsdk0,sdk --patches recomp/patches-v131072.txt`
   (the patches make game time follow real time; without them the game runs slow below 60 fps)
4. `python tools/build_xlate.py`, then `python tools/build_game.py` (Windows Python; MSYS2 clang does the compiling).
5. Run `build\v131072\mp4.exe` to play, in the foreground: the game pauses when its window loses focus. `--seconds S`
   plays S seconds of game time and exits, `--boot` runs to the first NVN call and exits, and the `--probe-*` modes
   check the host without a window (`runtime/main.c`).


## Third-party code

Everything taken from elsewhere is listed with its source, commit and license in
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md). fe3h-pc-recomp is MIT, Copyright (c) 2026 Jeff Aigner;
totk-pc-recomp is MIT, Copyright (c) 2026 Valiantsin Dzerakh; their own upstreams (LAS Recomp, MacNativeGames,
d2r-mobile, nxnative, Ryujinx's shader translator) keep their notices.
