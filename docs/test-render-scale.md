# Testing the render scale for every game resolution (issue #19)

The change: `runtime/game_patch_stubs.c` `res_init` used to switch on the resolution patch set (`native4k` in
`recomp/patches-v131072.txt`) only for `output_res = 3840x2160` with `render_scale = 100`, and every other combination
kept the stock 1600x900 scene. The patched sites read all their sizes from the patch page, so `res_init` now writes the
size for any combination there: the largest 16:9 rectangle inside `output_res` (`scene_size`), times `render_scale`, the
height rounded to a multiple of 4, at most 3840x2160. `render_scale = game` keeps the stock size. It was written on a
machine without the toolchain or a dump and has not been compiled or run yet. This file lists what is left to do on the
Windows build machine.

## 1. Build

```
.venv-win/Scripts/python.exe tools/build_game.py
```

The C did not go through a compiler before it was committed. Fix any warnings in `game_patch_stubs.c` (it now
includes `stdio.h` and `stdlib.h` for `sscanf` and `atoi`).

## 2. Play each combination

Start in docked mode (the default). Set each pair in `mp4.ini` or in the menu (both settings apply after restart). In
each run, check the log line, the performance overlay's scene line (F10, Full) and the first area.

| output_res | render_scale | Expected log line (`resolution: ...`) |
|---|---|---|
| 3840x2160 | 100 | `3840x2160 (output 3840x2160 at 100%)`, unchanged from before: compare with a 0.4.14 run |
| 1920x1080 | 100 | `1920x1080 (output 1920x1080 at 100%)`: the case the issue reports |
| 2560x1440 | 75 | `1920x1080 (output 2560x1440 at 75%)` |
| 1920x1080 | 67 | `1288x724 (output 1920x1080 at 67%)` |
| 1280x720 | 50 | `640x360 (output 1280x720 at 50%)` (360 is the lowest height) |
| 1920x1080 | 200 | `3840x2160 (output 1920x1080 at 200%)`: supersampled, the present scales it down |
| 3840x2160 | 150 | `3840x2160 (output 3840x2160 at 150%)`: clamped |
| 3440x1440 | 100 | `2560x1440 (output 3440x1440 at 100%)`: 16:9 picture, pillarboxed |
| any | game | no `resolution:` line; scene 1600x900 as before |

In each run, check:

- The picture fills the window (no image cut into a corner, no stretching), and the HUD and menus are placed
  correctly. Below 100 % the HUD is softer too: the patch ties the UI to the render target, which is expected.
- Screen effects are aligned with the scene: the visor, bloom, the meteor impact, the depth of field in the
  cinematic, the shadows.
- The scan visor (hold ZL): while it is held the scan effect's pair goes back to 1600x900 (`res_scan_input`), as it
  does at 4K. Its overlay must line up.
- Morph ball, a save and a load, the pause menu and the map.
- Handheld (`operation_mode = handheld`) with `render_scale = 100`: the log reads `render_scale needs the 1.1.0 patch
  table and docked mode` and the game keeps 1600x900.

Odd sizes (such as 1288x724 at 67 %) are the most likely to show problems: a misaligned post effect or a seam in the
half-size targets. If one breaks, round to a coarser step in `scene_size` (a multiple of 8 or 16) rather than drop the
scale.

## 3. Afterwards

- Record the result in `docs/plan.md` and in the status section of `CLAUDE.md` (which sizes were played, anything
  that broke).
- Post the reply on issue #19 and close it with the release that ships the change.
- Delete this file once the change has been tested.
