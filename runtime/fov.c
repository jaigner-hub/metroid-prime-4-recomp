// Field of view (issue #20): the host half of the `fov` patch set in recomp/patches-v131072.txt. The game draws
// gameplay with a 55 degree vertical field of view (0.959931 rad), which is about 86 degrees horizontally at 16:9;
// cutscenes and the title use 30. The patch multiplies the FOV the active camera hands its view each frame
// (main+0xb875c8) by a factor from the patch page when that FOV lies within a band around the gameplay value, so the
// view's projection, its frustum and the renderer's culling all follow, and cameras outside the band keep theirs.
//
// The arm cannon and the HUD are drawn by cameras of their own at the same 55 degrees, widened by the same copy; the
// patch gives them theirs back before their projections are built, so the cannon and the visor keep their size.
//
// MP4_FOV (fov in mp4.ini, live): "game", or the horizontal field of view in degrees at 16:9. The patch page holds, from
// +0x80, the factor and the band's ends (radians), then the inverse factor and the scaled band. The first 0x48 bytes
// belong to frame_rate.c and game_patch_stubs.c.
#include <math.h>
#include <stdatomic.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "cfg.h"
#include "host.h"

#define GAME_VFOV 0.959931f   // 55 degrees, the gameplay camera's (main+0xb875c8 copies it at +0x4c of the view)
// The band, relative to GAME_VFOV: the gameplay camera and the arm cannon's and HUD's (all at 55), not the cutscenes'
// 30. The patch restores the cannon's and HUD's FOV where it lies in the band times the factor; that scaled band must
// not reach into this one, or a block built twice (the cannon's) would be restored twice: factor > BAND_HI / BAND_LO,
// 1.051, which MIN_DEG keeps (90 degrees horizontal is 1.067).
#define BAND_LO 0.975f
#define BAND_HI 1.025f
#define MIN_DEG 90
#define MAX_DEG 130

static struct FovPage {
    _Atomic float factor, lo, hi;            // the cameras' FOV in [lo, hi] times factor (main+0xb87628)
    _Atomic float inverse, scaled_lo, scaled_hi;   // the cannon's and HUD's in [scaled_lo, scaled_hi] times inverse
} *page;
static int set = -2;
static char applied[16];

// Degrees horizontal at 16:9 -> the vertical FOV in radians.
static double vertical(double h_deg) { return 2 * atan(tan(h_deg * M_PI / 360) * 9.0 / 16.0); }

// At every present (nvn_api_obj.c, beside frame_step_present): applies MP4_FOV when it changed.
void fov_refresh(void) {
    if (set == -2) {
        set = rt_patch_set("fov");
        if (set >= 0) page = (struct FovPage *)((char *)rt_patch_page() + 0x80);
    }
    if (set < 0) return;
    const char *v = cfg_value("MP4_FOV");
    if (!strncmp(v, applied, sizeof applied)) return;
    snprintf(applied, sizeof applied, "%s", v);
    int deg = atoi(v);
    if (!strcmp(v, "game") || deg <= 0) {
        rt_patch_switch(set, 0);
        rt_log("fov: the game's (55 degrees vertical, about 86 horizontal at 16:9)");
        return;
    }
    if (deg < MIN_DEG) deg = MIN_DEG;
    if (deg > MAX_DEG) deg = MAX_DEG;
    double vf = vertical(deg);
    double k = vf / GAME_VFOV;
    atomic_store(&page->lo, GAME_VFOV * BAND_LO);
    atomic_store(&page->hi, GAME_VFOV * BAND_HI);
    atomic_store(&page->factor, (float)k);
    atomic_store(&page->inverse, (float)(1 / k));
    atomic_store(&page->scaled_lo, (float)(GAME_VFOV * BAND_LO * k));
    atomic_store(&page->scaled_hi, (float)(GAME_VFOV * BAND_HI * k));
    rt_patch_switch(set, 1);
    rt_log("fov: %d degrees horizontal at 16:9 (%.1f vertical, the game's 55 times %.3f)", deg, vf * 180 / M_PI,
           vf / GAME_VFOV);
}
