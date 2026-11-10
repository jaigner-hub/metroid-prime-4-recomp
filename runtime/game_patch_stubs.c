// From fe3h-pc-recomp runtime/game_patch_stubs.c @ 47ef99126793dbd13e4a475feb910a13ef6639b8 (MIT, Copyright (c) 2026 Jeff Aigner), copied; the frame step part moved to runtime/frame_rate.c.
// Host state for the docked native 4K patch in recomp/patches-v131072.txt.
#include <stdatomic.h>
#include <stddef.h>
#include <stdint.h>
#include <string.h>

#include "cfg.h"
#include "host.h"

enum { STOCK_W = 1600, STOCK_H = 900, OUTPUT_W = 1920, OUTPUT_H = 1080, NATIVE_W = 3840, NATIVE_H = 2160 };

// The first 0x20 bytes of the patch page belong to the frame step.
static struct ResolutionPage {
    uint64_t target, auxiliary;
    float width, height, negative_width;
    _Atomic uint64_t scan;
} *resolution;

_Static_assert(offsetof(struct ResolutionPage, scan) == 0x20, "resolution patch page layout");

static uint64_t pack_dimensions(unsigned w, unsigned h) { return ((uint64_t)h << 32) | w; }

uint64_t res_heap_extra_kib(void) { return 0; }

uint64_t res_heap_extra_bytes(void) { return 0; }

int res_heapdef_build(const void *in, size_t n, void **out, size_t *out_n) {
    (void)in, (void)n, (void)out, (void)out_n;
    return 0;   // never reached: romfs.c heapdef_overlay() returns first when res_heap_extra_kib() is 0
}

void res_heapdef_served(void) {}

void res_init(void) {
    int set = rt_patch_set("native4k");
    rt_patch_disable(set);
    if (strcmp(cfg_value("MP4_OUTPUT_RES"), "3840x2160") || strcmp(cfg_value("MP4_RENDER_SCALE"), "100"))
        return;
    if (set < 0 || !strcmp(cfg_value("MP4_OPERATION_MODE"), "handheld")) {
        rt_log("resolution: native 4K requires the 1.1.0 patch table and docked mode");
        return;
    }
    const char *enabled = cfg_get("MP4_NATIVE_4K");
    if (enabled && !strcmp(enabled, "0")) return;

    resolution = (struct ResolutionPage *)((char *)rt_patch_page() + 0x20);
    resolution->target = pack_dimensions(NATIVE_W, NATIVE_H);
    resolution->auxiliary = pack_dimensions(NATIVE_W / 2, NATIVE_H / 2);
    resolution->width = NATIVE_W;
    resolution->height = NATIVE_H;
    resolution->negative_width = -NATIVE_W;
    atomic_init(&resolution->scan, resolution->target);
    rt_patch_switch(set, 1);
    rt_log("resolution: native %ux%u at 100%% in docked mode", NATIVE_W, NATIVE_H);
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
    out[0] = resolution ? NATIVE_W : OUTPUT_W;
    out[1] = resolution ? NATIVE_H : OUTPUT_H;
    scene[0] = resolution ? NATIVE_W : STOCK_W;
    scene[1] = resolution ? NATIVE_H : STOCK_H;
}
