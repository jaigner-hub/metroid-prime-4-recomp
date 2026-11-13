@AGENTS.md

# Session guide

## What this project is

A static recompilation of *Metroid Prime 4: Beyond* (Switch 1 release). The game's ARM64 code is lifted to C,
compiled for x86-64, and linked with a host runtime that stands in for the Switch OS and GPU. Players bring their own
dump and keys; nothing from the game is ever committed.

The method comes from **totk-pc-recomp**, a working port of *Tears of the Kingdom*. **fe3h-pc-recomp** is the second
port built on that code, of *Fire Emblem: Three Houses*, and the direct predecessor of this project: it already
separated the game-independent parts of the TotK port from the TotK-specific ones, and its `docs/plan.md` records
what broke on a second game and why. This is the third game. The goal is a port, and, along the way, the answer to
how much of a Switch recomp is shared code that a fourth game gets for free.

## Where to get code from

- `refs/fe3h-pc-recomp/`: the FE3H port at a pinned commit (`tools/fetch_refs.sh`). Take code from here first: it
  is the TotK code with the first round of game-specific parts removed and the first round of SDK-version differences
  handled. Cite and attribute it from here, and carry its upstream attributions along (`THIRD_PARTY_NOTICES.md`).
- `refs/totk-pc-recomp/`: the original. Read `docs/architecture.md` there first; it explains the whole pipeline.
  Go here when FE3H changed something and the reason does not apply to this game.
- `CLAUDE.local.md` (ignored) may name local working checkouts with newer changes; read those for the latest state,
  but attribute copies to the commit they came from.

What is expected to carry over, judging by what carried from TotK to FE3H:

| Part | Where in fe3h-pc-recomp | Expected here |
|---|---|---|
| ARM64 lifter, NEON lowering, code generator, NSO loader | `recomp/lifter.py`, `simd_c.py`, `gen.py`, `module.py`, `nso.py`, `flags.py`, `regs.py` | Reuse as is (unchanged from TotK through FE3H) |
| Jump table recovery | `recomp/jumptables.py`, `tests/jumptables_check.py` | Reuse; re-validate on this game |
| Module bases | `recomp/layout.py` | Adapt to this game's modules |
| Differential tests against Unicorn | `tests/diff/` | Reuse |
| Guest memory, dispatch, loader | `runtime/memory.c`, `dispatch.c`, `loader.c`, `hostfn.c` | Reuse |
| Horizon host: threads, SVCs, IPC, fs, RomFS, time, audio renderer | `runtime/kernel.c`, `svc.c`, `ipc.c`, `fs_host.c`, `romfs.c`, `audren.c`, `ov_*.c` | Reuse; SDK version differences are the usual trouble (FE3H was 6.5.0, TotK far newer; this game is newer still) |
| NVN front end and Vulkan back end | `runtime/nvn_*.c`, `vk_*.c`, `tex_*.c` | Reuse if the game uses NVN (Phase 0 decides) |
| Shader translator (Maxwell to SPIR-V) | `tools/xlate/`, `tools/vendor/ryujinx/`, `runtime/gfx_shader.c` | Reuse if shaders ship as Maxwell code (Phase 0 decides) |
| Extraction, hactool build, content guard | `tools/extract_exefs.py`, `extract_romfs.py`, `build_hactool.py`, `check_content.py` | Copied already (title id read from the dump) |
| Installer and release assembly | `installer/`, `tools/make_release.py` | Reuse later, with this game's extraction and language table |
| Boot checkpoint table, patch tables, FE3H overrides, expected-file tables | `runtime/boot.c` (version table), `recomp/patches-v*.txt`, `runtime/ov_fe3h.c`, `tools/data/expected-*.json` | FE3H only. Do not copy |

totk-pc-recomp is MIT (Copyright (c) 2026 Valiantsin Dzerakh); fe3h-pc-recomp is MIT (Copyright (c) 2026 Jeff Aigner).
Every copy gets a row in `THIRD_PARTY_NOTICES.md` with the commit, and a header in the file naming the source. The
upstreams behind them (LASRecomp, MacNativeGames, d2r-mobile, nxnative, Ryujinx's shader translator) keep their
attribution too: copy the matching rows from fe3h-pc-recomp's `THIRD_PARTY_NOTICES.md` when their code comes along.

## Status

Phase 1 (`docs/plan.md`): the lifter, runtime, tests and build tools are in the tree (copied from
`refs/fe3h-pc-recomp` @ `47ef991` on 2026-11-02, each file's first line naming its source), `build/xlate/mp4_xlate.exe`
and `build/v131072/mp4.exe` build, and `--selftest` and `--probe-loader` pass. The dump is at `files/` (ignored),
`config.local.toml` points at it, the ExeFS of both versions is in `build/extracted/v0/exefs` and
`build/extracted/v131072/exefs` (1.1.0, the target), the generated C in `generated/v131072/`.

Build and run from WSL through interop: `.venv-win/Scripts/python.exe tools/build_game.py` (Windows Python, standard
library only; MSYS2 clang compiles) and `build/v131072/mp4.exe --boot`. The differential tests run with the Windows
venv too (`.venv-win`, made from `tools/requirements.txt`), as the Linux side has no clang. Survey and run logs go to
`build/logs/`.

The game boots to its title screen, menus, save and opening cinematic (text crawl, H.264 movies through openh264);
the window origin is NVN's lower-left default (render targets bottom-up, flipped at present), and the window acquire
bounds how far the CPU runs ahead of the GPU (`MP4_GPU_AHEAD`), which the game's timer-query pool needs. The
cinematic's AAC audio is decoded by the lifted library (its audio codecs are software). The first area renders
(front face mirrored for the lower-left origin, shadow-backed virtual pools for the streamed light volumes, integer
samplers, shader image stores tracked). The BC6H light volume's sparse tile shape (16x16x16 blocks, found by comparing dumps) lit the arm cannon and brightened the scene. Transform feedback is replayed (`VK_EXT_transform_feedback`, layout from the vertex shader's outputs, calls through
the recorder thread; `MP4_DUMP=xfb` dumps the capture frame). The stray blue/purple streaks after the meteor impact were
flare sprites whose shader indexes its attributes (one input array of 16 locations): every location now gets an
attribute (gfx_shader.c reflect_spirv marks every location an interface array spans). Game time follows real time at any frame rate, including above 60 with `MP4_FPS_LIMIT` (the patch set realtime in `recomp/patches-v131072.txt`: the update's step and the {1/60, 1/30} frame-time pair at main+0x13bf400; host half `runtime/frame_rate.c`;
regenerate with `gen.py ... --patches recomp/patches-v131072.txt`). The mouse can aim through the motion sensor (`MP4_MOUSE_AIM=gyro`, issue #17; the game's own "Motion controls" must be on; its own default keys). Next: whether the occasional green blob is gone, the control buffer write-back if the game turns out to read it, then
further areas. Give `MP4_PROFILE_DIR` an absolute Windows path (a relative one resolves next to mp4.exe, an empty profile that starts a new game). Test runs: `build/input/fast.txt` with
`MP4_INPUT_SCRIPT` presses A every second into gameplay (by tick 3000, about 50 s; `ball.txt` then morphs, and
`<tick> SHOT` / `<tick> DUMP` lines capture); run them muted (`MP4_VOLUME=0`) when the user plays. For glitches that last a frame, `MP4_SHOT_RING=<n>` keeps the last n presents in memory and P (or present
`MP4_SHOT_RING_AT=<n>`) writes them as PNGs to `MP4_SHOTS_DIR`. Game-bound tables rewritten so far: `runtime/boot.c`, the main stack in
`runtime/kernel.c`, `tools/build_game.py` versions, `tests/diff/lift.py` bases, `runtime/loader.c` probe table.
`runtime/ov_mp4.c` is where this game's SDK overrides go, following `refs/fe3h-pc-recomp/runtime/ov_fe3h.c`.
Shaders: `tools/shader_inventory.py` reads every program from the RomFS (the `SNVN` chunks of `MaterialArchive.arc` and
of the paks' `MTRL`/`SVGM` assets, the update's patch pak included); the release ships a list of the variants play met
(`build/shader-list.bin`, made with `tools/prepare_shaders.py --make-list` from `shader-answers.bin`, never committed),
which the installer prepares from the player's files (`docs/installer.md`, "Shader list"). Regenerate the list after
playing new areas. The pipelines play built ship the same way (`build/pipeline-list.bin`, `tools/prepare_shaders.py
--make-pipeline-list` from `pipeline-keys.bin`, built by `--prepare-shaders`; "Pipeline list" there), which took a fresh
install's skipped draws from 2,247 to 75. The save-load hitch went from 1.5 s to 0.5 s (`docs/plan.md`, "The save-load
hitch": partial light-volume uploads, one host-pointer query, write-watch queries that resume), then to about 0.2 s
(shared heap imports, light volume images made and zeroed at registration, during the load). The ASTC burst about 8 s
in (80 M texels decoded in one frame on a GPU the game keeps at low clocks, 65-120 ms) is now spread by a per-frame
decode budget, `MP4_ASTC_BUDGET`, with views clamped by `VK_EXT_image_view_min_lod` until the large levels are in
(`docs/plan.md`, "The 179-texture frame"); `MP4_ASTC_DUMP` with `--bench-astc` times the decoder on real uploads offline.

## Open questions

Resolved in Phase 0 (details in `docs/plan.md`): the update NSP is the Switch 1 code (a BKTR patch over the Switch 1
base, NVN and SDK 19.3.15 inside) although its NACP carries the "Nintendo Switch 2 Edition" name; the target is
1.1.0 = `v131072`; SDK 19.3.13 (base) and 19.3.15 (update); Retro's own engine with `RFRM`/`PACK` archives and zstd;
NVN with precompiled Maxwell shaders (plus DXBC copies of every shader in the material archive, purpose unknown);
amiibo through `nn::nfp`, no network namespaces imported by `main`. The Switch 2 build was not extracted or read.

Still open:

- Frame rate: the update's step and the {1/60, 1/30} pair at main+0x13bf400 now follow real time (60 and 120 fps play
  alike in the first area). The other 1/60 constants were read (`docs/plan.md`, 2026-11-05): conversions from frames
  and frame-sized tolerances, no further step; one same-frame test now reads half the step.
- The DXBC blobs in `MaterialArchive.arc`: reflection the engine reads, or a toolchain leftover? Decide from how
  `main` parses the archive before building the shader path.
- Video codec of the FMVs (inside the paks; `RetroFMV` on top of `nn::movie`).
- Which of the SVCs the FE3H host lacks (0x13/0x14 shared memory, 0x2F, 0x32, 0x33, 0x40 to 0x45, 0x4B, 0x50 to
  0x52, 0x70, 0x72) the game actually reaches.

## Environment notes

- The user works in WSL (Linux), where the interpreter is `python3`; `.venv` is a Python 3.12 venv made with uv from
  `tools/requirements.txt`. FE3H builds its C on Windows with Windows Python and MSYS2 UCRT64 Clang
  (`tools/build_game.py` there); expect the same here once there is C to build.
- The sibling checkout `../fe3h-pc-recomp` on this machine is the working FE3H repository; `refs/fe3h-pc-recomp` is
  the pinned copy to cite.
- Never commit without the content check passing; never bypass the hook.
