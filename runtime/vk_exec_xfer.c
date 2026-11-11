// From fe3h-pc-recomp runtime/vk_exec_xfer.c @ 47ef99126793dbd13e4a475feb910a13ef6639b8 (MIT, Copyright (c) 2026 Jeff Aigner), copied unchanged; mirror picks pass the bytes they touch (VK_MEM_PICK); adds the MP4_OQ_LOG report log, depth-stencil read-backs (copy_ds_to_buffer) and depth-stencil texture copies.
// From totk-pc-recomp runtime/vk_exec_xfer.c @ c944f9f94520874f2a669add30dd08d29ee9f7b5 (MIT, Copyright (c) 2026 Valiantsin Dzerakh); adds SetCopyImageStride (OP_COPY_IMG_STRIDE) for FE3H.
// Transfers of the Vulkan op-stream replay: buffer and texture copies, texture
// clears, event signals, counter reports and conditional rendering. vk_exec.c calls vk_exec_op_xfer for these ops.
//
// Buffers are guest memory: vk_mem_resolve maps an address to the VkBuffer behind its pool, textures are the vk_texture.c images
// (linear texel layout, GENERAL layout; block-linear storage is only converted when a mirrored texture is uploaded). So a
// copy is one Vulkan transfer command and the pitch of a buffer side comes from the payload (OP_COPY_STRIDE or the
// tight row). Every copy ends the open pass and starts with a full barrier. Destinations: an image is marked
// GPU-written at record time; a CPU-visible buffer range is reported to vk_guest_written once the GPU is done (write
// watch does not see GPU writes, textures stored there re-upload).
//
// Events and counters are CPU-visible side effects. Their values go into guest memory on the completion thread through
// vk_completion_add, in submit order, as gfx_null.c does: event WRITE / ADD, counter {0, timestamp} for every type but
// samples passed. A timestamp report puts 0 in the counter word: TOTK gates its clears on two such words being unequal.
//
// Samples passed (NVN_COUNTER_TYPE_SAMPLES_PASSED) is a running count on the queue: RESET_COUNTER zeroes it, every
// draw after it adds the samples that passed the depth and stencil tests, REPORT_COUNTER writes the running value.
// TOTK resets and reports it 3 to 8 times a frame in the field and its CPU reads the results back (f_70010c3298); a 0
// there culls what the query tested. The replay counts with Vulkan occlusion queries ("occlusion counts" below).
//
// Conditional rendering is a CPU-side skip, not VK_EXT_conditional_rendering: the extension compares one 32-bit word
// with zero, NVN compares the counter words at addr and addr + 16. The condition is evaluated when the op is recorded
// from guest memory; a counter reported by an earlier submission that is not done yet counts as 0 if it is a timestamp,
// as unknown if it is samples passed. An unknown count renders: the report usually sits in the command buffer being
// recorded, so its value cannot be waited for, and the word in memory still holds an older query's count. Rendering
// costs at most the work the condition would have saved (MacNativeGames nvn_mtl.m decides the same); in the field
// TOTK conditions only on timestamp reports. The condition sets VkExec.render_disabled, which vk_exec.c honours for
// clears and vk_exec_draw.c for draws and dispatches. A condition that cannot be read (address outside every pool)
// renders.
// Copies and CLEAR_TEX are not conditional (as in MacNativeGames, nvn_mtl.m).
//
// The copy semantics (region and layer arithmetic, row and image strides, whole-level clears, copy / blit / resolve
// choice) follow MacNativeGames games/totk/third_party/d2r_gfx/gfx/nvn/nvn_mtl.m:10080-10368, 10618-10642, 1700-1725
// (https://github.com/ly05010419/MacNativeGames @ b75be8cfa704df3701e86130c8ced268e79d6b11, MIT, Copyright (c) 2026
// jhaizhou) and LAS Recomp runtime/nvn/vulkan/exec.c:3045-3235 (https://github.com/peiikai/LASRecomp @
// da35b83a9611d046a9a681aae668d7085954d37f, MIT, Copyright (c) 2026 Peiikai).
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "cfg.h"
#include "host.h"
#include "nvn_api.h"
#include "nvn_tex.h"
#include "tex.h"
#include "vk.h"
#include "vk_exec.h"

enum { W_FORMAT, W_REGION, W_BUFFER, W_OVERLAP, W_COPY, W_CLEAR, W_QUERY, W_COUNT };

static void warn_once(int key, const char *fmt, ...) {
    static volatile long seen[W_COUNT];
    if (__atomic_exchange_n(&seen[key], 1, __ATOMIC_RELAXED)) return;
    char msg[256];
    va_list ap;
    va_start(ap, fmt);
    vsnprintf(msg, sizeof msg, fmt, ap);
    va_end(ap);
    rt_log("vk: %s (logged once)", msg);
}

static uint32_t max1(uint32_t v) { return v ? v : 1; }

// ---- completion callbacks (completion thread)

typedef struct { uint64_t addr, size; } Range;

static void guest_written_cb(void *arg) {
    Range *r = arg;
    vk_guest_written(r->addr, r->size);
    free(r);
}

// The GPU wrote [addr, addr+size) of CPU-visible memory: tell the texture cache once the fence is reached.
static void note_gpu_write(VkExec *ex, uint64_t addr, uint64_t size) {
    vk_usnap_drop(addr, size);
    if (nvn_gpu_only(addr)) return;
    Range *r = malloc(sizeof *r);
    if (!r) rt_fatal("vk: out of memory");
    *r = (Range){addr, size};
    vk_completion_add(ex->fence, guest_written_cb, r);
}

typedef struct { volatile uint32_t *val; uint32_t mode, value; } EventWrite;

static void event_cb(void *arg) {
    EventWrite *e = arg;
    if (e->mode == NVN_EVENT_SIGNAL_MODE_ADD) __atomic_fetch_add(e->val, e->value, __ATOMIC_RELEASE);
    else __atomic_store_n(e->val, e->value, __ATOMIC_RELEASE);
    free(e);
}

// NVNcounterData = {counter, timestamp}; timestamps are Tegra 19.2 MHz ticks (the base of GetSystemTick).
static void write_counter(uint64_t addr, uint64_t counter) {
    uint64_t rec[2] = {counter, (uint64_t)((unsigned __int128)kern_now_ns() * 12 / 625)};
    memcpy((void *)(uintptr_t)addr, rec, sizeof rec);
}

static void counter_cb(void *arg) {
    uint64_t *addr = arg;
    write_counter(*addr, 0);
    free(addr);
}

// Reports queued by the replay that the GPU has not finished. The condition of RENDER_COND reads a timestamp one as 0
// and a samples-passed one as unknown.
#define PENDING 256
static struct { uint64_t addr, fence; int unknown; } pending[PENDING];
static unsigned pending_head;

// ---- occlusion counts (samples passed)
//
// After a SAMPLES_PASSED reset, the first draw of a pass begins an occlusion query (precise when the device has
// occlusionQueryPrecise); the query ends with the pass, or at the next report or reset of the counter, and the next
// draw begins another. A Vulkan query begun inside a render pass must end inside it. TOTK always resets, draws the
// tested proxies, reports, so counting stops at a report and restarts at the next reset: queries cover only the draws
// between the two, not every pass of the frame. Once the game reports twice without a reset in between, counting stays
// on for good (cumulative NVN semantics; that one report misses the draws since the previous one). The queries closed since the last
// report or reset form a segment, handed to the completion thread with that report or reset: in submit order it sums
// the segment's results into the running count (a reset drops the sum and zeroes the count) and writes the count of a
// report into guest memory. Slots come from one ring; the completion thread reads and host-resets them
// (hostQueryReset, Vulkan 1.2), so the replay records no reset commands. A full ring (no slot read back yet) leaves the
// draw uncounted and the segment's count at least 1, as does a query still unavailable after its fence. Without hostQueryReset the counts stay 0.
#define OQ_N 4096
#define LOG_ONCE(...) do { static volatile char once_; if (!__atomic_exchange_n(&once_, 1, __ATOMIC_RELAXED)) rt_log(__VA_ARGS__); } while (0)
static VkQueryPool oq_pool;
static int oq_state;                     // 0 not set up, 1 counting available, -1 unavailable
static VkQueryControlFlags oq_flags;
static volatile uint8_t oq_busy[OQ_N];   // from begin until the completion thread read and reset the slot
static uint32_t oq_next;                 // replay: next ring slot
static int oq_counting;                  // replay: draws are counted (a reset since the last report, or oq_keep)
static int oq_reported;                  // replay: a samples-passed report since the last reset
static int oq_keep;                      // replay: the game reported twice without a reset: count every draw
static int oq_open = -1;                 // replay: the slot whose query is active in the open pass
static uint32_t *oq_seg;                 // replay: slots closed since the last report or reset
static uint32_t oq_nseg, oq_capseg;
static int oq_lost;                      // replay: the segment missed a draw (ring full)
static uint32_t oq_draws, oq_pw, oq_ph;   // replay: the segment's counted draws, its first draw's pass size (MP4_OQ_LOG)
static uint64_t oq_prog;                 // replay: the program of the segment's first counted draw (MP4_OQ_LOG)
static uint64_t oq_count;                // completion thread: samples since the last reset

typedef struct { uint64_t addr; int reset, lost; uint32_t n, draws, pass_w, pass_h; uint64_t prog; uint32_t q[]; } OqSegment;

static int oq_ready(void) {
    if (oq_state) return oq_state > 0;
    VkCtx *v = vk_ctx();
    // vk_device.c enables every feature the device reports, so supported means enabled.
    VkPhysicalDeviceVulkan12Features f12 = {.sType = VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_VULKAN_1_2_FEATURES};
    VkPhysicalDeviceFeatures2 f2 = {.sType = VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_FEATURES_2, .pNext = &f12};
    vkGetPhysicalDeviceFeatures2(v->phys, &f2);
    oq_state = -1;
    if (!f12.hostQueryReset) {
        rt_log("vk: no hostQueryReset: samples-passed counters report 0");
        return 0;
    }
    VkQueryPoolCreateInfo qi = {.sType = VK_STRUCTURE_TYPE_QUERY_POOL_CREATE_INFO, .queryType = VK_QUERY_TYPE_OCCLUSION,
                                .queryCount = OQ_N};
    VkResult r = vkCreateQueryPool(v->device, &qi, NULL, &oq_pool);
    if (r != VK_SUCCESS) {
        rt_log("vk: occlusion query pool creation failed (%d): samples-passed counters report 0", (int)r);
        return 0;
    }
    vkResetQueryPool(v->device, oq_pool, 0, OQ_N);
    oq_flags = f2.features.occlusionQueryPrecise ? VK_QUERY_CONTROL_PRECISE_BIT : 0;
    oq_state = 1;
    return 1;
}

static void oq_close(VkExec *ex) {
    if (oq_open < 0) return;
    vkCmdEndQuery(ex->cb, oq_pool, (uint32_t)oq_open);
    ex->recorded++;
    if (oq_nseg == oq_capseg) {
        oq_capseg = oq_capseg ? oq_capseg * 2 : 64;
        oq_seg = realloc(oq_seg, sizeof *oq_seg * oq_capseg);
        if (!oq_seg) rt_fatal("vk: out of memory");
    }
    oq_seg[oq_nseg++] = (uint32_t)oq_open;
    oq_open = -1;
}

void vk_exec_oq_draw(VkExec *ex) {
    if (oq_counting && !oq_draws++) oq_prog = ex->prog ? ex->prog->serial : 0, oq_pw = ex->pass_w, oq_ph = ex->pass_h;
    if (!oq_counting || oq_open >= 0) return;
    uint32_t q = oq_next % OQ_N;
    if (__atomic_load_n(&oq_busy[q], __ATOMIC_ACQUIRE)) {
        warn_once(W_QUERY, "occlusion query ring full: draws left uncounted");
        oq_lost = 1;
        return;
    }
    oq_busy[q] = 1;
    oq_next++;
    vkCmdBeginQuery(ex->cb, oq_pool, q, oq_flags);
    ex->recorded++;
    oq_open = (int)q;
}

void vk_exec_oq_pass_end(VkExec *ex) { oq_close(ex); }

void vk_exec_xfer_shutdown(void) {
    if (oq_state > 0) vkDestroyQueryPool(vk_ctx()->device, oq_pool, NULL);
    oq_pool = VK_NULL_HANDLE, oq_state = 0, oq_next = 0, oq_counting = oq_reported = oq_keep = 0, oq_open = -1;
    oq_nseg = 0, oq_lost = 0, oq_count = 0;
    memset((void *)oq_busy, 0, sizeof oq_busy);
}

// MP4_OQ_LOG=<n> (1: 20000): one line per samples-passed report, up to n lines, for a game's visibility tests (which
// addresses it reports to, how many queries each covers, the counts it gets back): frame, address, running count,
// this segment's sum, its query count, whether it missed draws (ring full), its counted draws and the program and pass
// size of the first (a program number of the MP4_DUMP passes.txt). Completion thread, submit order.
static void oq_log_report(const OqSegment *s, uint64_t sum) {
    static long budget = -1;
    if (budget < 0) {
        const char *e = cfg_get("MP4_OQ_LOG");
        long n = e && *e ? atol(e) : 0;
        budget = n == 1 ? 20000 : n > 0 ? n : 0;
    }
    if (budget <= 0) return;
    budget--;
    rt_log("oq: frame %llu report %#llx count %llu (segment %llu over %u queries%s, %u draws, first prog %llu in a %ux%u pass)",
           (unsigned long long)gfx_stats().frames, (unsigned long long)s->addr, (unsigned long long)oq_count,
           (unsigned long long)sum, s->n, s->lost ? ", lost" : "", s->draws, (unsigned long long)s->prog, s->pass_w,
           s->pass_h);
}

static void oq_cb(void *arg) {
    OqSegment *s = arg;
    VkDevice dev = vk_ctx()->device;
    uint64_t sum = 0, v[64 * 2];
    for (uint32_t i = 0; i < s->n;) {
        uint32_t j = i + 1;
        while (j < s->n && j - i < 64 && s->q[j] == s->q[j - 1] + 1) j++;
        // The fence has passed, so a query that is still unavailable will not become available: waiting would block the
        // completion thread, and with it every later callback, for good. It counts as visible (one sample).
        VkResult r = vkGetQueryPoolResults(dev, oq_pool, s->q[i], j - i, sizeof v, v, 2 * sizeof v[0],
                                           VK_QUERY_RESULT_64_BIT | VK_QUERY_RESULT_WITH_AVAILABILITY_BIT);
        if (r != VK_SUCCESS && r != VK_NOT_READY) {
            vk_device_fault_report(r);
            rt_fatal("vk: occlusion query results failed (%d)", (int)r);
        }
        vkResetQueryPool(dev, oq_pool, s->q[i], j - i);
        for (uint32_t k = i; k < j; k++) {
            if (v[2 * (k - i) + 1]) sum += v[2 * (k - i)];
            else {
                sum += 1;
                LOG_ONCE("vk: occlusion query slot %u not available after its fence, counted as visible", s->q[k]);
            }
            __atomic_store_n(&oq_busy[s->q[k]], 0, __ATOMIC_RELEASE);
        }
        i = j;
    }
    if (s->reset) oq_count = 0;
    else {
        oq_count += sum;
        if (s->lost && !oq_count) oq_count = 1;
        if (s->addr) write_counter(s->addr, oq_count);
        oq_log_report(s, sum);
    }
    free(s);
}

// Hands the segment to the completion thread with a report (addr; 0: no CPU-visible memory) or a reset.
static void oq_segment(VkExec *ex, uint64_t addr, int reset) {
    oq_close(ex);
    OqSegment *s = malloc(sizeof *s + sizeof *s->q * oq_nseg);
    if (!s) rt_fatal("vk: out of memory");
    s->addr = addr, s->reset = reset, s->lost = oq_lost, s->n = oq_nseg;
    s->draws = oq_draws, s->prog = oq_prog, s->pass_w = oq_pw, s->pass_h = oq_ph;
    oq_draws = 0, oq_prog = 0;
    memcpy(s->q, oq_seg, sizeof *s->q * oq_nseg);
    oq_nseg = 0, oq_lost = 0;
    vk_completion_add(ex->fence, oq_cb, s);
}

static void reset_counter(VkExec *ex, const p_reset_counter *p) {
    if (p->type != NVN_COUNTER_TYPE_SAMPLES_PASSED || !oq_ready()) return;
    oq_segment(ex, 0, 1);
    oq_counting = 1, oq_reported = 0;
}

// A samples-passed report: the segment goes with it (addr 0: no CPU-visible memory) and counting stops until the next
// reset, unless reports came without resets before.
static void oq_report(VkExec *ex, uint64_t addr) {
    oq_segment(ex, addr, 0);
    if (oq_reported && !oq_keep) {
        oq_keep = 1;
        rt_log("vk: samples passed reported twice without a reset: counting every draw from now on");
    }
    oq_reported = 1;
    if (!oq_keep) oq_counting = 0;
}

static void signal_event(VkExec *ex, const p_signal_event *p) {
    EventWrite *e = malloc(sizeof *e);
    if (!e) rt_fatal("vk: out of memory");
    *e = (EventWrite){p->val, p->mode, p->value};
    vk_completion_add(ex->fence, event_cb, e);
}

static void report_counter(VkExec *ex, const p_report_counter *p) {
    int samples = p->type == NVN_COUNTER_TYPE_SAMPLES_PASSED && oq_ready();
    if (nvn_gpu_only(p->addr)) {   // CPU-invisible pool: no host memory behind it
        if (samples) oq_report(ex, 0);
        return;
    }
    VkMemSpan m;
    if (!vk_mem_resolve(p->addr, sizeof(NVNcounterData), &m))
        rt_fatal("vk: counter report at %#llx is in no memory pool", (unsigned long long)p->addr);
    vk_mem_count(m.cls, VK_MUSE_GPU_WRITE, sizeof(NVNcounterData));
    if (samples) oq_report(ex, p->addr);
    else {
        uint64_t *a = malloc(sizeof *a);
        if (!a) rt_fatal("vk: out of memory");
        *a = p->addr;
        vk_completion_add(ex->fence, counter_cb, a);
    }
    pending[pending_head++ % PENDING] = (typeof(pending[0])){p->addr, ex->fence, samples};
}

// ---- conditional rendering

// 1: *v read, 0: not readable, 2: a samples-passed report not done yet (unknown)
static int counter_word(uint64_t addr, uint64_t *v) {
    uint64_t done = vk_sync_fence_done();
    for (unsigned k = 1; k <= PENDING; k++) {   // newest first: the last report to addr decides
        unsigned i = (pending_head - k) % PENDING;
        if (pending[i].addr == addr && pending[i].fence > done) return *v = 0, pending[i].unknown ? 2 : 1;
    }
    VkMemSpan m;
    if (nvn_gpu_only(addr) || !vk_mem_resolve(addr, sizeof *v, &m)) return 0;
    memcpy(v, (const void *)(uintptr_t)addr, sizeof *v);
    vk_mem_count(m.cls, VK_MUSE_CPU_READ, sizeof *v);
    return 1;
}

static int cond_enabled(const p_render_cond *c) {
    uint64_t x, y;
    int rx = counter_word(c->addr, &x), ry = counter_word(c->addr + 16, &y);
    if (!rx || !ry) {
        warn_once(W_BUFFER, "conditional render counters at %#llx are not readable: rendering", (unsigned long long)c->addr);
        return 1;
    }
    if (rx == 2 || ry == 2) return 1;   // a samples-passed count not done yet
    return c->mode == 0 ? x == y : x != y;
}

// ---- textures

typedef struct {
    const VkTexImage *img;
    VkImageAspectFlags aspect;
    uint32_t level, layer0;
    int bpb, bw, bh;
    int32_t x, y, z;           // region origin
    uint32_t w, h, nz;         // region extent; nz: slices (3D) or layers
} Sub;

// Resolves tex, its view and a copy region to image coordinates. 0 (warned) when the copy cannot be replayed.
// ds: a depth-stencil image may be resolved (both aspects, also one stored in another format than the NVN one), for
// the packed read-back of copy_ds_to_buffer.
static int sub_resolve_ds(const tex_t *t, const view_t *v, int has_v, const NVNcopyRegion *r, int defaults_to_level,
                          Sub *s, int ds) {
    memset(s, 0, sizeof *s);
    if (!t) return 0;
    TexVkFormat f;
    int ds_ok = ds && tex_vk_format(t->d.format, &f) && f.depth && f.stencil;
    if (!ds_ok && (!tex_vk_format(t->d.format, &f) || f.vk != f.ideal || f.expand_rgb || f.astc_bw)) {
        warn_once(W_FORMAT, "copy of format %#x texture (decoded or expanded on upload): skipped", t->d.format);
        return 0;
    }
    s->bpb = nvn_fmt_bpb(t->d.format, &s->bw, &s->bh);
    s->img = vk_texture_get(t);
    if (!s->img || !s->bpb) {
        warn_once(W_FORMAT, "copy of format %#x texture without an image: skipped", t->d.format);
        return 0;
    }
    const VkTexImage *img = s->img;
    if (img->aspect & VK_IMAGE_ASPECT_COLOR_BIT) s->aspect = VK_IMAGE_ASPECT_COLOR_BIT;
    else if (img->aspect == VK_IMAGE_ASPECT_DEPTH_BIT) s->aspect = VK_IMAGE_ASPECT_DEPTH_BIT;
    else if (ds_ok && img->aspect == (VK_IMAGE_ASPECT_DEPTH_BIT | VK_IMAGE_ASPECT_STENCIL_BIT)) s->aspect = img->aspect;
    else {
        warn_once(W_FORMAT, "copy of depth-stencil format %#x texture: skipped", t->d.format);
        return 0;
    }
    s->level = has_v && v->set_levels ? v->base_level : 0;
    s->layer0 = has_v && v->set_layers ? v->min_layer : 0;
    uint32_t lw = max1(img->width >> s->level), lh = max1(img->height >> s->level);
    s->x = r->xoffset, s->y = r->yoffset, s->z = r->zoffset;
    s->nz = r->depth > 0 ? (uint32_t)r->depth : 1;
    s->w = r->width > 0 ? (uint32_t)r->width : defaults_to_level ? (s->x < (int32_t)lw ? lw - (uint32_t)s->x : 0) : 0;
    s->h = r->height > 0 ? (uint32_t)r->height : defaults_to_level ? (s->y < (int32_t)lh ? lh - (uint32_t)s->y : 0) : 0;
    int is3d = img->type == VK_IMAGE_TYPE_3D;
    uint32_t layers = is3d ? 1 : (uint32_t)s->z + s->nz, first = is3d ? 0 : s->layer0;
    if (s->level >= img->levels || s->x < 0 || s->y < 0 || s->z < 0 || !s->w || !s->h ||
        (uint64_t)s->x + s->w > lw || (uint64_t)s->y + s->h > lh || first + layers > img->layers ||
        (is3d && (uint64_t)s->z + s->nz > max1(img->depth >> s->level))) {
        warn_once(W_REGION, "copy region %d,%d,%d %ux%ux%u outside level %u of a %ux%u texture: skipped", s->x, s->y,
                  s->z, s->w, s->h, s->nz, s->level, img->width, img->height);
        return 0;
    }
    return 1;
}

static int sub_resolve(const tex_t *t, const view_t *v, int has_v, const NVNcopyRegion *r, int defaults_to_level,
                       Sub *s) {
    return sub_resolve_ds(t, v, has_v, r, defaults_to_level, s, 0);
}

static VkImageSubresourceLayers sub_layers(const Sub *s) {
    int is3d = s->img->type == VK_IMAGE_TYPE_3D;
    return (VkImageSubresourceLayers){s->aspect, s->level, is3d ? 0 : s->layer0 + (uint32_t)s->z, is3d ? 1 : s->nz};
}

static VkOffset3D sub_offset(const Sub *s) {
    return (VkOffset3D){s->x, s->y, s->img->type == VK_IMAGE_TYPE_3D ? s->z : 0};
}

static VkExtent3D sub_extent(const Sub *s) {
    return (VkExtent3D){s->w, s->h, s->img->type == VK_IMAGE_TYPE_3D ? s->nz : 1};
}

// Buffer side of a texture <-> buffer copy: bytes covered, pitches, Vulkan buffer span (a VRAM mirror when the pool
// has one).
static int buffer_side(const VkExec *ex, const Sub *s, uint64_t addr, uint64_t *size, VkBufferImageCopy *c,
                       VkMemSpan *span) {
    uint32_t wb = (s->w + (uint32_t)s->bw - 1) / (uint32_t)s->bw, hb = (s->h + (uint32_t)s->bh - 1) / (uint32_t)s->bh;
    uint64_t row = ex->copy_stride > 0 ? (uint64_t)ex->copy_stride : (uint64_t)wb * (uint64_t)s->bpb;
    if (row % (uint64_t)s->bpb || row < (uint64_t)wb * (uint64_t)s->bpb) {
        warn_once(W_REGION, "buffer copy row stride %llu does not fit %u blocks of %d bytes: skipped",
                  (unsigned long long)row, wb, s->bpb);
        return 0;
    }
    // Image stride (SetCopyImageStride): the buffer pitch from one layer or slice to the next, a whole number of rows
    // of at least the copy's height, as Vulkan's bufferImageHeight expresses it; 0 = rows packed.
    uint64_t img = ex->copy_img_stride > 0 ? (uint64_t)ex->copy_img_stride : row * hb;
    if (img % row || img < row * hb) {
        warn_once(W_REGION, "buffer copy image stride %llu is not a whole number of %llu-byte rows covering %u rows: "
                  "skipped", (unsigned long long)img, (unsigned long long)row, hb);
        return 0;
    }
    *size = img * (s->nz - 1) + row * (hb - 1) + (uint64_t)wb * (uint64_t)s->bpb;
    VkMemSpan m;
    if (!addr || !vk_mem_resolve(addr, *size, &m) || m.avail < *size) {
        warn_once(W_BUFFER, "texture copy buffer %#llx (%llu bytes) is not one backed span: skipped",
                  (unsigned long long)addr, (unsigned long long)*size);
        return 0;
    }
    VK_MEM_PICK(m, *size);
    *span = m;
    *c = (VkBufferImageCopy){.bufferOffset = m.offset, .bufferRowLength = (uint32_t)(row / (uint64_t)s->bpb) * (uint32_t)s->bw,
                             .bufferImageHeight = (uint32_t)(img / row) * (uint32_t)s->bh, .imageSubresource = sub_layers(s),
                             .imageOffset = sub_offset(s), .imageExtent = sub_extent(s)};
    return 1;
}

// ---- depth-stencil read-back
// A copy of a combined depth-stencil texture into a buffer. Vulkan copies one aspect at a time, tightly packed (4-byte
// float depth for D32_SFLOAT_S8_UINT, 1-byte stencil), and DEPTH24_STENCIL8 is always stored as D32_SFLOAT_S8_UINT
// (vk_formats.c), so the bytes the game expects cannot come from a copy alone: both aspects go to a host-visible staging
// buffer, and once the GPU is done the completion thread packs them into the guest buffer with the copy's pitches, as
// the NVN layout has it: DEPTH24_STENCIL8 a 32-bit word with the stencil in bits 0-7 and the 24-bit unsigned
// normalised depth in 8-31 (OpenGL's UNSIGNED_INT_24_8 order), DEPTH32F_STENCIL8 8 bytes: the float depth, the
// stencil, three zero bytes. The DEPTH24_STENCIL8 order is the game's: the scan visor's picker (main+0x11ff9b0) takes
// a target's slot from bits 1-6 of each word, `(v & 0x7e) >> 1` minus one. Depth in bits 0-23, as this was first
// written, handed it the low bits of the depth, which change from frame to frame, and the highlight jumped between
// nearby targets (issue #10). The guest bytes are written by the CPU,
// so the pages read as CPU-written (vk_guest_written) and a mirror of the pool takes them on its next use like any
// other CPU write. Metroid Prime 4's scan visor reads 120x100 depth texels around the reticle this way every frame to
// tell which scan targets are visible (issue #10): skipped, it read whatever the buffer held.
typedef struct {
    VkBuffer buf;
    VmaAllocation alloc;
    const uint8_t *map;
    uint64_t dst, row, img, size;   // guest buffer, its row and image pitches, the bytes it covers
    uint32_t w, h, nz, bpb, stencil_off;
} DsReadback;

static void ds_readback_cb(void *arg) {   // completion thread
    DsReadback *r = arg;
    vmaInvalidateAllocation(vk_ctx()->vma, r->alloc, 0, VK_WHOLE_SIZE);
    const float *dep = (const float *)r->map;
    const uint8_t *st = r->map + r->stencil_off;
    for (uint32_t z = 0; z < r->nz; z++)
        for (uint32_t y = 0; y < r->h; y++) {
            uint8_t *o = (uint8_t *)(uintptr_t)(r->dst + z * r->img + y * r->row);
            size_t i = ((size_t)z * r->h + y) * r->w;
            for (uint32_t x = 0; x < r->w; x++, i++) {
                float d = dep[i];
                if (r->bpb == 4) {
                    d = d > 0.0f ? d < 1.0f ? d : 1.0f : 0.0f;   // also NaN to 0
                    uint32_t v = (uint32_t)(d * 16777215.0f + 0.5f) << 8 | st[i];
                    memcpy(o + 4 * x, &v, 4);
                } else {
                    uint8_t v[8] = {0};
                    memcpy(v, &d, 4), v[4] = st[i];
                    memcpy(o + 8 * x, v, 8);
                }
            }
        }
    vk_guest_written(r->dst, r->size);
    vk_defer_buffer(r->buf, r->alloc);
    free(r);
}

static void copy_ds_to_buffer(VkExec *ex, const p_copy_b2t *p) {
    Sub s;
    if (!sub_resolve_ds(p->tex, &p->view, p->has_view, &p->r, 1, &s, 1)) return;
    if (s.bpb != 4 && s.bpb != 8) {
        warn_once(W_FORMAT, "depth-stencil read-back of format %#x (%d bytes a texel): skipped", p->tex->d.format, s.bpb);
        return;
    }
    if (s.img->format != VK_FORMAT_D32_SFLOAT_S8_UINT) {
        warn_once(W_FORMAT, "depth-stencil read-back from VkFormat %d: skipped", (int)s.img->format);
        return;
    }
    uint64_t size;
    VkBufferImageCopy c;
    VkMemSpan m;
    if (!buffer_side(ex, &s, p->src, &size, &c, &m)) return;   // the guest buffer's pitches and span
    if (nvn_gpu_only(p->src)) {
        warn_once(W_BUFFER, "depth-stencil read-back into a CPU-invisible pool: skipped");
        return;
    }
    uint64_t texels = (uint64_t)s.w * s.h * s.nz, soff = (texels * 4 + 15) & ~15ull;
    DsReadback *r = calloc(1, sizeof *r);
    if (!r) rt_fatal("vk: out of memory");
    VkBufferCreateInfo bi = {.sType = VK_STRUCTURE_TYPE_BUFFER_CREATE_INFO, .size = soff + texels,
                             .usage = VK_BUFFER_USAGE_TRANSFER_DST_BIT};
    VmaAllocationCreateInfo ai = {.usage = VMA_MEMORY_USAGE_AUTO_PREFER_HOST,
                                  .flags = VMA_ALLOCATION_CREATE_HOST_ACCESS_RANDOM_BIT | VMA_ALLOCATION_CREATE_MAPPED_BIT};
    VmaAllocationInfo info;
    if (vmaCreateBuffer(vk_ctx()->vma, &bi, &ai, &r->buf, &r->alloc, &info) != VK_SUCCESS || !info.pMappedData) {
        if (r->buf) vmaDestroyBuffer(vk_ctx()->vma, r->buf, r->alloc);
        free(r);
        warn_once(W_BUFFER, "depth-stencil read-back: no staging buffer: skipped");
        return;
    }
    r->map = info.pMappedData;
    r->dst = p->src, r->size = size, r->w = s.w, r->h = s.h, r->nz = s.nz, r->bpb = (uint32_t)s.bpb;
    r->row = (uint64_t)c.bufferRowLength * (uint32_t)s.bpb;
    r->img = r->row * c.bufferImageHeight;
    r->stencil_off = (uint32_t)soff;
    vk_exec_pass_end(ex);
    vk_exec_barrier(ex);
    s.aspect = VK_IMAGE_ASPECT_DEPTH_BIT;
    VkBufferImageCopy cc[2] = {
        {.bufferOffset = 0, .imageSubresource = sub_layers(&s), .imageOffset = sub_offset(&s), .imageExtent = sub_extent(&s)},
        {.bufferOffset = soff, .imageOffset = sub_offset(&s), .imageExtent = sub_extent(&s)},
    };
    s.aspect = VK_IMAGE_ASPECT_STENCIL_BIT;
    cc[1].imageSubresource = sub_layers(&s);
    vkCmdCopyImageToBuffer(ex->cb, s.img->image, VK_IMAGE_LAYOUT_GENERAL, r->buf, 2, cc);
    // The copy made visible to the host. vkCmdPipelineBarrier2: the recorder thread wraps it (vk_rec.h), the first
    // version is not wrapped and would land in the command buffer out of order.
    VkMemoryBarrier2 mb = {.sType = VK_STRUCTURE_TYPE_MEMORY_BARRIER_2, .srcStageMask = VK_PIPELINE_STAGE_2_COPY_BIT,
                           .srcAccessMask = VK_ACCESS_2_TRANSFER_WRITE_BIT, .dstStageMask = VK_PIPELINE_STAGE_2_HOST_BIT,
                           .dstAccessMask = VK_ACCESS_2_HOST_READ_BIT};
    VkDependencyInfo di = {.sType = VK_STRUCTURE_TYPE_DEPENDENCY_INFO, .memoryBarrierCount = 1, .pMemoryBarriers = &mb};
    vkCmdPipelineBarrier2(ex->cb, &di);
    ex->recorded += 2;
    vk_usnap_drop(p->src, size);
    vk_completion_add(ex->fence, ds_readback_cb, r);
    LOG_ONCE("vk: depth-stencil read-back: format %#x %ux%ux%u at %d,%d into %#llx (row %llu bytes; logged once)",
             p->tex->d.format, s.w, s.h, s.nz, s.x, s.y, (unsigned long long)p->src, (unsigned long long)r->row);
}

static void copy_buffer_image(VkExec *ex, const p_copy_b2t *p, int to_image) {
    TexVkFormat tf;
    if (!to_image && p->tex && tex_vk_format(p->tex->d.format, &tf) && tf.depth && tf.stencil) {
        copy_ds_to_buffer(ex, p);
        return;
    }
    Sub s;
    if (!sub_resolve(p->tex, &p->view, p->has_view, &p->r, 1, &s)) return;
    uint64_t size;
    VkBufferImageCopy c;
    VkMemSpan m;
    if (!buffer_side(ex, &s, p->src, &size, &c, &m)) return;
    vk_mem_count(m.cls, to_image ? VK_MUSE_COPY_SRC : VK_MUSE_GPU_WRITE, size);
    vk_exec_pass_end(ex);
    vk_exec_barrier(ex);
    if (to_image) vkCmdCopyBufferToImage(ex->cb, m.buffer, s.img->image, VK_IMAGE_LAYOUT_GENERAL, 1, &c);
    else vkCmdCopyImageToBuffer(ex->cb, s.img->image, VK_IMAGE_LAYOUT_GENERAL, m.buffer, 1, &c);
    ex->recorded++;
    if (to_image) vk_texture_gpu_written(p->tex);
    else vk_mem_gpu_wrote(&m, size), note_gpu_write(ex, p->src, size);
}

static void copy_t2t(VkExec *ex, const p_copy_t2t *p) {
    Sub s, d;
    // Depth-stencil to depth-stencil: both images hold the same Vulkan format whatever the NVN one says (DEPTH24_STENCIL8
    // is stored as D32_SFLOAT_S8_UINT, vk_formats.c), so an unscaled copy of both aspects is exact. Metroid Prime 4
    // copies its scene depth into a second texture this way every frame, before the scan visor's read-back (issue #10).
    TexVkFormat sf, df;
    int ds = p->src && p->dst && tex_vk_format(p->src->d.format, &sf) && tex_vk_format(p->dst->d.format, &df) &&
             sf.depth && sf.stencil && df.depth && df.stencil;
    if (!sub_resolve_ds(p->src, &p->sv, p->has_sv, &p->sr, 1, &s, ds)) return;
    if (!sub_resolve_ds(p->dst, &p->dv, p->has_dv, &p->dr, 1, &d, ds)) return;
    d.nz = s.nz;
    int same_size = s.w == d.w && s.h == d.h;
    int same_format = s.img->format == d.img->format;
    if (ds && (!same_size || !same_format || s.img->samples != d.img->samples)) {
        warn_once(W_COPY, "depth-stencil texture copy %ux%u -> %ux%u (VkFormat %d -> %d, %u -> %u samples): skipped", s.w,
                  s.h, d.w, d.h, (int)s.img->format, (int)d.img->format, s.img->samples, d.img->samples);
        return;
    }
    if (s.img->samples != d.img->samples && !(s.img->samples > 1 && d.img->samples == 1 && same_size && same_format)) {
        warn_once(W_COPY, "texture copy between %u and %u samples: skipped", s.img->samples, d.img->samples);
        return;
    }
    if (s.aspect != d.aspect || (s.aspect != VK_IMAGE_ASPECT_COLOR_BIT && !same_format)) {
        warn_once(W_COPY, "texture copy between formats %#x and %#x: skipped", p->src->d.format, p->dst->d.format);
        return;
    }
    VkImageSubresourceLayers sl = sub_layers(&s), dl = sub_layers(&d);
    vk_exec_pass_end(ex);
    vk_exec_barrier(ex);
    if (s.img->samples > 1 && d.img->samples == 1) {
        VkImageResolve rv = {sl, sub_offset(&s), dl, sub_offset(&d), sub_extent(&s)};
        vkCmdResolveImage(ex->cb, s.img->image, VK_IMAGE_LAYOUT_GENERAL, d.img->image, VK_IMAGE_LAYOUT_GENERAL, 1, &rv);
    } else if (same_size && same_format) {
        VkImageCopy cp = {sl, sub_offset(&s), dl, sub_offset(&d), sub_extent(&s)};
        vkCmdCopyImage(ex->cb, s.img->image, VK_IMAGE_LAYOUT_GENERAL, d.img->image, VK_IMAGE_LAYOUT_GENERAL, 1, &cp);
    } else {   // scaled or converting copy: nearest, or linear when asked for (colour only)
        int is3d = s.img->type == VK_IMAGE_TYPE_3D;
        VkImageBlit bl = {.srcSubresource = sl, .dstSubresource = dl};
        bl.srcOffsets[0] = sub_offset(&s);
        bl.srcOffsets[1] = (VkOffset3D){s.x + (int32_t)s.w, s.y + (int32_t)s.h, is3d ? s.z + (int32_t)s.nz : 1};
        bl.dstOffsets[0] = sub_offset(&d);
        bl.dstOffsets[1] = (VkOffset3D){d.x + (int32_t)d.w, d.y + (int32_t)d.h, is3d ? d.z + (int32_t)d.nz : 1};
        int linear = (p->flags & NVN_COPY_FLAGS_LINEAR_FILTER_BIT) && s.aspect == VK_IMAGE_ASPECT_COLOR_BIT;
        vkCmdBlitImage(ex->cb, s.img->image, VK_IMAGE_LAYOUT_GENERAL, d.img->image, VK_IMAGE_LAYOUT_GENERAL, 1, &bl,
                       linear ? VK_FILTER_LINEAR : VK_FILTER_NEAREST);
    }
    ex->recorded++;
    vk_texture_gpu_written(p->dst);
}

// ---- buffer to buffer

static void copy_b2b(VkExec *ex, const p_copy_b2b *p) {
    uint64_t src = p->src, dst = p->dst, left = p->size;
    int started = 0;
    while (left) {
        VkMemSpan s, d;
        if (!vk_mem_resolve(src, 1, &s) || !vk_mem_resolve(dst, 1, &d)) {
            warn_once(W_BUFFER, "buffer copy %#llx -> %#llx (%llu bytes) leaves backed memory: rest skipped",
                      (unsigned long long)p->src, (unsigned long long)p->dst, (unsigned long long)p->size);
            break;
        }
        uint64_t n = left < s.avail ? left : s.avail;
        if (n > d.avail) n = d.avail;
        VK_MEM_PICK(s, n);
        VK_MEM_PICK(d, n);
        if (s.buffer == d.buffer && s.offset < d.offset + n && d.offset < s.offset + n) {
            warn_once(W_OVERLAP, "buffer copy %#llx -> %#llx overlaps within one buffer: skipped",
                      (unsigned long long)p->src, (unsigned long long)p->dst);
            break;
        }
        if (!started) vk_exec_pass_end(ex), vk_exec_barrier(ex), started = 1;
        VkBufferCopy bc = {s.offset, d.offset, n};
        vkCmdCopyBuffer(ex->cb, s.buffer, d.buffer, 1, &bc);
        vk_mem_gpu_wrote(&d, n);
        ex->recorded++;
        vk_mem_count(s.cls, VK_MUSE_COPY_SRC, n);
        vk_mem_count(d.cls, VK_MUSE_GPU_WRITE, n);
        src += n, dst += n, left -= n;
    }
    if (started) note_gpu_write(ex, p->dst, p->size - left);
}

// ---- buffer fills (ClearBuffer, and ReportValue as a 4-byte one): vkCmdFillBuffer in GPU order after a full
// barrier, so the value lands after earlier work, as NVN's semaphore release does; CPU-visible ranges go to
// vk_guest_written at the fence, as copies do. Works in CPU-invisible pools.

static void clear_buf(VkExec *ex, const p_clear_buf *p) {
    if ((p->addr | p->size) & 3) {
        warn_once(W_BUFFER, "buffer fill at %#llx of %llu bytes is not 4-byte aligned: skipped", (unsigned long long)p->addr,
                  (unsigned long long)p->size);
        return;
    }
    uint64_t dst = p->addr, left = p->size;
    int started = 0;
    while (left) {
        VkMemSpan d;
        if (!vk_mem_resolve(dst, 1, &d) || d.offset % 4) {
            warn_once(W_BUFFER, "buffer fill at %#llx (%llu bytes) leaves backed memory: rest skipped",
                      (unsigned long long)p->addr, (unsigned long long)p->size);
            break;
        }
        uint64_t n = left < d.avail ? left : d.avail & ~3ull;
        if (!n) break;
        if (!started) vk_exec_pass_end(ex), vk_exec_barrier(ex), started = 1;
        VK_MEM_PICK(d, n);
        vkCmdFillBuffer(ex->cb, d.buffer, d.offset, n, p->value);
        vk_mem_gpu_wrote(&d, n);
        ex->recorded++;
        vk_mem_count(d.cls, VK_MUSE_GPU_WRITE, n);
        dst += n, left -= n;
    }
    if (started) note_gpu_write(ex, p->addr, p->size - left);
}

// ---- texture clears

static int int_format(VkFormat f) {
    switch (f) {
    case VK_FORMAT_R8_UINT: case VK_FORMAT_R8G8_UINT: case VK_FORMAT_R8G8B8A8_UINT: case VK_FORMAT_R8_SINT:
    case VK_FORMAT_R8G8_SINT: case VK_FORMAT_R8G8B8A8_SINT: case VK_FORMAT_R16_UINT: case VK_FORMAT_R16G16_UINT:
    case VK_FORMAT_R16G16B16A16_UINT: case VK_FORMAT_R16_SINT: case VK_FORMAT_R16G16_SINT:
    case VK_FORMAT_R16G16B16A16_SINT: case VK_FORMAT_R32_UINT: case VK_FORMAT_R32G32_UINT:
    case VK_FORMAT_R32G32B32A32_UINT: case VK_FORMAT_R32_SINT: case VK_FORMAT_R32G32_SINT:
    case VK_FORMAT_R32G32B32A32_SINT: case VK_FORMAT_A2B10G10R10_UINT_PACK32:
        return 1;
    default: return 0;
    }
}

// Whole levels only (vkCmdClear*Image has no rectangle); partial regions, channel masks and a view that reinterprets
// the format are skipped (MacNativeGames does not replay them either, nvn_mtl.m).
static void clear_tex(VkExec *ex, const p_clear_tex *p) {
    const tex_t *t = p->tex;
    if (!t) return;
    Sub s;
    if (!sub_resolve(t, &p->view, p->has_view, &p->r, 1, &s)) return;
    const VkTexImage *img = s.img;
    int is3d = img->type == VK_IMAGE_TYPE_3D;
    uint32_t lw = max1(img->width >> s.level), lh = max1(img->height >> s.level);
    int reformat = p->has_view && p->view.set_format && p->view.format != t->d.format;
    if (s.x || s.y || s.w != lw || s.h != lh || (p->mask & 0xf) != 0xf || reformat ||
        (is3d && (s.z || s.nz != max1(img->depth >> s.level)))) {
        warn_once(W_CLEAR, "texture clear of region %d,%d %ux%u mask %#x (level %ux%u, view format change %d): skipped",
                  s.x, s.y, s.w, s.h, p->mask, lw, lh, reformat);
        return;
    }
    VkImageSubresourceRange rng = {img->aspect, s.level, 1, is3d ? 0 : s.layer0 + (uint32_t)s.z, is3d ? 1 : s.nz};
    if (img->aspect & VK_IMAGE_ASPECT_COLOR_BIT) {
        rng.aspectMask = VK_IMAGE_ASPECT_COLOR_BIT;
        VkClearColorValue cv;
        if (int_format(img->format) != (p->is_uint != 0)) {
            warn_once(W_CLEAR, "texture clear value kind (uint %d) does not match format %d: skipped", p->is_uint, (int)img->format);
            return;
        }
        memcpy(&cv, &p->c, sizeof cv);
        vk_exec_pass_end(ex);
        vk_exec_barrier(ex);
        vkCmdClearColorImage(ex->cb, img->image, VK_IMAGE_LAYOUT_GENERAL, &cv, 1, &rng);
    } else {
        int both = (img->aspect & VK_IMAGE_ASPECT_DEPTH_BIT) && (img->aspect & VK_IMAGE_ASPECT_STENCIL_BIT);
        VkClearDepthStencilValue dv = {0, 0};
        if (img->aspect & VK_IMAGE_ASPECT_DEPTH_BIT) dv.depth = p->c.f[0];
        if (img->aspect & VK_IMAGE_ASPECT_STENCIL_BIT) dv.stencil = both ? p->c.u[1] : p->c.u[0];
        vk_exec_pass_end(ex);
        vk_exec_barrier(ex);
        vkCmdClearDepthStencilImage(ex->cb, img->image, VK_IMAGE_LAYOUT_GENERAL, &dv, 1, &rng);
    }
    ex->recorded++;
    vk_texture_gpu_written(t);
}

void vk_exec_op_xfer(VkExec *ex, uint32_t op, const void *payload) {
    switch (op) {
    case OP_COPY_B2B: copy_b2b(ex, payload); break;
    case OP_COPY_B2T: copy_buffer_image(ex, payload, 1); break;
    case OP_COPY_T2B: copy_buffer_image(ex, payload, 0); break;   // p_copy_b2t: src holds the destination address
    case OP_COPY_T2T: copy_t2t(ex, payload); break;
    case OP_CLEAR_TEX: clear_tex(ex, payload); break;
    case OP_SIGNAL_EVENT: signal_event(ex, payload); break;
    case OP_REPORT_COUNTER: report_counter(ex, payload); break;
    case OP_RESET_COUNTER: reset_counter(ex, payload); break;
    case OP_CLEAR_BUF: clear_buf(ex, payload); break;
    case OP_RENDER_ENABLE: ex->render_disabled = !((const p_flag *)payload)->value; break;
    case OP_RENDER_COND:
        ex->render_cond = *(const p_render_cond *)payload;
        ex->has_render_cond = 1;
        ex->render_disabled = !cond_enabled(payload);
        break;
    default: rt_fatal("vk: op %s is not a transfer", nvn_op_name(op));
    }
}
