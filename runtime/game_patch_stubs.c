// From fe3h-pc-recomp runtime/game_patch_stubs.c @ 47ef99126793dbd13e4a475feb910a13ef6639b8 (MIT, Copyright (c) 2026 Jeff Aigner), copied; the frame step part moved to runtime/frame_rate.c.
// Host state for the docked resolution patch in recomp/patches-v131072.txt (set native4k): the scene, the UI and the
// main viewport take one size read from the patch page, so any 16:9 size works, not only 3840x2160 (issue #19).
#include <stdatomic.h>
#include <stddef.h>
#include <stdio.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>

#include "cfg.h"
#include "host.h"

enum { STOCK_W = 1600, STOCK_H = 900, OUTPUT_W = 1920, OUTPUT_H = 1080, MAX_H = 2160 };

// The first 0x20 bytes of the patch page belong to the frame step.
static struct ResolutionPage {
    uint64_t target, auxiliary;
    float width, height, negative_width;
    _Atomic uint64_t scan;
} *resolution;

_Static_assert(offsetof(struct ResolutionPage, scan) == 0x20, "resolution patch page layout");

static unsigned scene_w, scene_h;   // the patched size; 0 while the game keeps its own

static uint64_t pack_dimensions(unsigned w, unsigned h) { return ((uint64_t)h << 32) | w; }

uint64_t res_heap_extra_kib(void) { return 0; }

uint64_t res_heap_extra_bytes(void) { return 0; }

int res_heapdef_build(const void *in, size_t n, void **out, size_t *out_n) {
    (void)in, (void)n, (void)out, (void)out_n;
    return 0;   // never reached: romfs.c heapdef_overlay() returns first when res_heap_extra_kib() is 0
}

void res_heapdef_served(void) {}

// The patched size for an output of out_w x out_h at scale percent: the largest 16:9 rectangle inside the output (a
// 21:9 output gets the 16:9 picture the game lays out, pillarboxed by the present), scaled, the height a multiple of 4
// so the half-size auxiliary targets stay even, at most 3840x2160 (the largest size played; the game's heaps were not
// grown for more).
static void scene_size(unsigned out_w, unsigned out_h, unsigned scale, unsigned *w, unsigned *h) {
    double fit_h = out_h;
    if (out_w * 9.0 < out_h * 16.0) fit_h = out_w * 9.0 / 16.0;
    double sh = fit_h * scale / 100.0;
    if (sh > MAX_H) sh = MAX_H;
    unsigned hh = (unsigned)(sh / 4 + 0.5) * 4;
    if (hh < 360) hh = 360;
    *h = hh;
    *w = (unsigned)(hh * 16.0 / 9.0 / 2 + 0.5) * 2;
}

void res_init(void) {
    int set = rt_patch_set("native4k");
    rt_patch_disable(set);
    const char *scale_s = cfg_value("MP4_RENDER_SCALE");
    if (!strcmp(scale_s, "game")) return;
    unsigned out_w = 0, out_h = 0, scale = (unsigned)atoi(scale_s);
    if (sscanf(cfg_value("MP4_OUTPUT_RES"), "%ux%u", &out_w, &out_h) != 2 || !out_w || !out_h || !scale) {
        rt_log("resolution: cannot read output_res %s / render_scale %s, the game keeps 1600x900",
               cfg_value("MP4_OUTPUT_RES"), scale_s);
        return;
    }
    if (set < 0 || !strcmp(cfg_value("MP4_OPERATION_MODE"), "handheld")) {
        rt_log("resolution: render_scale needs the 1.1.0 patch table and docked mode, the game keeps 1600x900");
        return;
    }
    const char *enabled = cfg_get("MP4_NATIVE_4K");
    if (enabled && !strcmp(enabled, "0")) return;

    unsigned w, h;
    scene_size(out_w, out_h, scale, &w, &h);
    resolution = (struct ResolutionPage *)((char *)rt_patch_page() + 0x20);
    resolution->target = pack_dimensions(w, h);
    resolution->auxiliary = pack_dimensions(w / 2, h / 2);
    resolution->width = (float)w;
    resolution->height = (float)h;
    resolution->negative_width = -(float)w;
    atomic_init(&resolution->scan, resolution->target);
    scene_w = w, scene_h = h;
    rt_patch_switch(set, 1);
    rt_log("resolution: %ux%u (output %ux%u at %u%%) in docked mode", w, h, out_w, out_h, scale);
}

void res_scan_input(int held) {
    if (resolution)
        atomic_store_explicit(&resolution->scan, held ? pack_dimensions(STOCK_W, STOCK_H) : resolution->target,
                              memory_order_relaxed);
}

int res_is_16x9(unsigned w, unsigned h) {
    // Within 1% of 16:9, the test resolution.c uses.
    double r = h ? (double)w / h : 0, want = 16.0 / 9.0;
    return r > want * 0.99 && r < want * 1.01;
}

int res_wants_wide_overlay(const unsigned out[2]) {
    (void)out;
    return 0;
}

void res_wanted(unsigned out[2]) { out[0] = OUTPUT_W, out[1] = OUTPUT_H; }

void res_sizes(unsigned out[2], unsigned scene[2]) {
    // Patched, the game's image is the scene size: the UI and the viewport follow the render target.
    out[0] = resolution ? scene_w : OUTPUT_W;
    out[1] = resolution ? scene_h : OUTPUT_H;
    scene[0] = resolution ? scene_w : STOCK_W;
    scene[1] = resolution ? scene_h : STOCK_H;
}
