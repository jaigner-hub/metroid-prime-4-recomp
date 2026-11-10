// NVIDIA Nsight Aftermath GPU crash dumps (MP4_AFTERMATH=1), a diagnostic for development builds. Compiled in only when
// tools/build_game.py runs with MP4_AFTERMATH_SDK=<the unpacked SDK>: the SDK's headers come from there and its DLL is
// loaded at run time (MP4_AFTERMATH_DLL, else <SDK>\lib\x64). Nothing of the SDK is in this repository or a release:
// its license lets only the DLL be distributed, and not under an open source license. Without the define every
// function below is a no-op.
//
// On a device loss (vk_device.c vk_device_fault_report, aftermath_wait) the driver hands the crash dump to dump_cb,
// which writes aftermath-<pid>.nv-gpudmp next to the log (Nsight Graphics opens it) and decodes it in-process: the page
// fault with the resources at the faulting address (whether destroyed, size, format, the Vulkan handle) and the shaders
// running, named through the table aftermath_shader fills as modules are made (vk_pipeline.c make_module), and the
// whole decode as aftermath-<pid>.json.
#include <windows.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "cfg.h"
#include "host.h"
#include "vk.h"

#ifdef MP4_AFTERMATH_SDK
#include "GFSDK_Aftermath.h"
#include "GFSDK_Aftermath_GpuCrashDump.h"
#include "GFSDK_Aftermath_GpuCrashDumpDecoding.h"

static int on;
static PFN_GFSDK_Aftermath_EnableGpuCrashDumps f_enable;
static PFN_GFSDK_Aftermath_GetCrashDumpStatus f_status;
static PFN_GFSDK_Aftermath_GpuCrashDump_CreateDecoder f_dec_create;
static PFN_GFSDK_Aftermath_GpuCrashDump_DestroyDecoder f_dec_destroy;
static PFN_GFSDK_Aftermath_GpuCrashDump_GetPageFaultInfo f_fault;
static PFN_GFSDK_Aftermath_GpuCrashDump_GetPageFaultResourceInfo f_fault_res;
static PFN_GFSDK_Aftermath_GpuCrashDump_GetActiveShadersInfoCount f_sh_n;
static PFN_GFSDK_Aftermath_GpuCrashDump_GetActiveShadersInfo f_sh;
static PFN_GFSDK_Aftermath_GetShaderHashForShaderInfo f_sh_hash;
static PFN_GFSDK_Aftermath_GetShaderHashSpirv f_spv_hash;
static PFN_GFSDK_Aftermath_GpuCrashDump_GenerateJSON f_json_gen;
static PFN_GFSDK_Aftermath_GpuCrashDump_GetJSON f_json_get;

// Module hash -> label ("vs 1a2b3c4d"), open addressing; a full table drops new modules.
#define SH_SLOTS (1u << 17)
static struct { uint64_t hash; char label[24]; } *sh_tab;
static SRWLOCK sh_lock = SRWLOCK_INIT;
static _Thread_local char cur_label[24];
static volatile LONG dumped;

static void path_beside_log(char *o, size_t n, const char *ext) {
    const char *log = rt_log_path();
    snprintf(o, n, "%s", log ? log : "mp4.log");
    char *cut = strrchr(o, '\\');
    if (!cut) cut = strrchr(o, '/');
    snprintf(cut ? cut + 1 : o, n - (size_t)(cut ? cut + 1 - o : 0), "aftermath-%lu.%s", GetCurrentProcessId(), ext);
}

static const char *label_of(uint64_t h) {
    if (!sh_tab) return NULL;
    for (uint32_t i = (uint32_t)h & (SH_SLOTS - 1), k = 0; k < SH_SLOTS; i = (i + 1) & (SH_SLOTS - 1), k++) {
        if (!sh_tab[i].hash) return NULL;
        if (sh_tab[i].hash == h) return sh_tab[i].label;
    }
    return NULL;
}

static void GFSDK_AFTERMATH_CALL dump_cb(const void *data, const uint32_t size, void *user) {
    (void)user;
    InterlockedExchange(&dumped, 1);
    char p[MAX_PATH * 2];
    path_beside_log(p, sizeof p, "nv-gpudmp");
    FILE *f = fopen(p, "wb");
    if (f) fwrite(data, 1, size, f), fclose(f);
    rt_log("aftermath: GPU crash dump of %u bytes written to %s", size, p);
    GFSDK_Aftermath_GpuCrashDump_Decoder dec = NULL;
    if (f_dec_create(GFSDK_Aftermath_Version_API, data, size, &dec) != GFSDK_Aftermath_Result_Success) {
        rt_log("aftermath: the dump cannot be decoded");
        return;
    }
    GFSDK_Aftermath_GpuCrashDump_PageFaultInfo pf;
    memset(&pf, 0, sizeof pf);
    if (f_fault(dec, &pf) == GFSDK_Aftermath_Result_Success) {
        rt_log("aftermath: page fault at GPU VA %#llx: fault type %d, access %d, engine %d, client %d, %u resources there",
               (unsigned long long)pf.faultingGpuVA, (int)pf.faultType, (int)pf.accessType, (int)pf.engine, (int)pf.client,
               pf.resourceInfoCount);
        if (pf.resourceInfoCount) {
            GFSDK_Aftermath_GpuCrashDump_ResourceInfo *ri = calloc(pf.resourceInfoCount, sizeof *ri);
            if (ri && f_fault_res(dec, pf.resourceInfoCount, ri) == GFSDK_Aftermath_Result_Success)
                for (uint32_t i = 0; i < pf.resourceInfoCount; i++)
                    rt_log("aftermath:   resource %u: VA %#llx size %#llx, %ux%ux%u, %u levels, VkFormat %u, handle %#llx \"%s\", %s",
                           i, (unsigned long long)ri[i].gpuVa, (unsigned long long)ri[i].size, ri[i].width, ri[i].height,
                           ri[i].depth, ri[i].mipLevels, ri[i].format, (unsigned long long)ri[i].apiResource, ri[i].debugName,
                           ri[i].bWasDestroyed ? "DESTROYED" : "alive");
            free(ri);
        }
    } else {
        rt_log("aftermath: no page fault information in the dump");
    }
    uint32_t n = 0;
    if (f_sh_n(dec, &n) == GFSDK_Aftermath_Result_Success && n) {
        GFSDK_Aftermath_GpuCrashDump_ShaderInfo *si = calloc(n, sizeof *si);
        if (si && f_sh(dec, n, si) == GFSDK_Aftermath_Result_Success)
            for (uint32_t i = 0; i < n && i < 64; i++) {
                GFSDK_Aftermath_ShaderBinaryHash bh = {0};
                const char *l = NULL;
                if (!si[i].isInternal && f_sh_hash(dec, &si[i], &bh) == GFSDK_Aftermath_Result_Success) {
                    AcquireSRWLockShared(&sh_lock);
                    l = label_of(bh.hash);
                    ReleaseSRWLockShared(&sh_lock);
                }
                rt_log("aftermath:   active shader %u: type %d%s, binary hash %#llx, ours: %s", i, (int)si[i].shaderType,
                       si[i].isInternal ? " (driver internal)" : "", (unsigned long long)bh.hash, l ? l : "not known");
            }
        free(si);
    }
    uint32_t js = 0;
    if (f_json_gen(dec, GFSDK_Aftermath_GpuCrashDumpDecoderFlags_ALL_INFO, GFSDK_Aftermath_GpuCrashDumpFormatterFlags_NONE,
                   NULL, NULL, NULL, NULL, &js) == GFSDK_Aftermath_Result_Success && js) {
        char *j = malloc(js);
        if (j && f_json_get(dec, js, j) == GFSDK_Aftermath_Result_Success) {
            path_beside_log(p, sizeof p, "json");
            if ((f = fopen(p, "wb"))) fwrite(j, 1, strlen(j), f), fclose(f);
            rt_log("aftermath: decoded dump written to %s", p);
        }
        free(j);
    }
    f_dec_destroy(dec);
}

int aftermath_enable(void) {
    const char *e = cfg_get("MP4_AFTERMATH");   // a debug switch without a settings row: cfg_get, not cfg_bool
    if (!e || !*e || !strcmp(e, "0")) return 0;
    const char *dp = cfg_get("MP4_AFTERMATH_DLL");
    wchar_t w[MAX_PATH];
    HMODULE dll = NULL;
    if (dp && *dp && MultiByteToWideChar(CP_UTF8, 0, dp, -1, w, MAX_PATH)) dll = LoadLibraryW(w);
    if (!dll) dll = LoadLibraryW(L"GFSDK_Aftermath_Lib.x64.dll");
    if (!dll) {
        rt_log("aftermath: MP4_AFTERMATH=1 but GFSDK_Aftermath_Lib.x64.dll is not found (MP4_AFTERMATH_DLL names it)");
        return 0;
    }
#define GET(v, name) (v = (void *)GetProcAddress(dll, name))
    if (!GET(f_enable, "GFSDK_Aftermath_EnableGpuCrashDumps") || !GET(f_status, "GFSDK_Aftermath_GetCrashDumpStatus") ||
        !GET(f_dec_create, "GFSDK_Aftermath_GpuCrashDump_CreateDecoder") ||
        !GET(f_dec_destroy, "GFSDK_Aftermath_GpuCrashDump_DestroyDecoder") ||
        !GET(f_fault, "GFSDK_Aftermath_GpuCrashDump_GetPageFaultInfo") ||
        !GET(f_fault_res, "GFSDK_Aftermath_GpuCrashDump_GetPageFaultResourceInfo") ||
        !GET(f_sh_n, "GFSDK_Aftermath_GpuCrashDump_GetActiveShadersInfoCount") ||
        !GET(f_sh, "GFSDK_Aftermath_GpuCrashDump_GetActiveShadersInfo") ||
        !GET(f_sh_hash, "GFSDK_Aftermath_GetShaderHashForShaderInfo") || !GET(f_spv_hash, "GFSDK_Aftermath_GetShaderHashSpirv") ||
        !GET(f_json_gen, "GFSDK_Aftermath_GpuCrashDump_GenerateJSON") || !GET(f_json_get, "GFSDK_Aftermath_GpuCrashDump_GetJSON")) {
        rt_log("aftermath: the DLL lacks an entry point");
        return 0;
    }
#undef GET
    sh_tab = calloc(SH_SLOTS, sizeof *sh_tab);
    GFSDK_Aftermath_Result r = f_enable(GFSDK_Aftermath_Version_API, GFSDK_Aftermath_GpuCrashDumpWatchedApiFlags_Vulkan,
                                        GFSDK_Aftermath_GpuCrashDumpFeatureFlags_DeferDebugInfoCallbacks, dump_cb, NULL, NULL,
                                        NULL, NULL);
    if (r != GFSDK_Aftermath_Result_Success) {
        rt_log("aftermath: EnableGpuCrashDumps failed (%#x)", (unsigned)r);
        return 0;
    }
    on = 1;
    rt_log("aftermath: GPU crash dumps on (resource tracking, shader debug info, automatic checkpoints)");
    return 1;
}

void aftermath_label(const char *label) { snprintf(cur_label, sizeof cur_label, "%s", label ? label : ""); }

void aftermath_shader(const uint32_t *code, uint64_t words) {
    if (!on || !sh_tab) return;
    GFSDK_Aftermath_SpirvCode sc = {.pData = code, .size = (uint32_t)(words * 4)};
    GFSDK_Aftermath_ShaderBinaryHash h = {0};
    if (f_spv_hash(GFSDK_Aftermath_Version_API, &sc, &h) != GFSDK_Aftermath_Result_Success || !h.hash) return;
    AcquireSRWLockExclusive(&sh_lock);
    for (uint32_t i = (uint32_t)h.hash & (SH_SLOTS - 1), k = 0; k < SH_SLOTS; i = (i + 1) & (SH_SLOTS - 1), k++) {
        if (sh_tab[i].hash == h.hash) break;
        if (!sh_tab[i].hash) {
            sh_tab[i].hash = h.hash;
            snprintf(sh_tab[i].label, sizeof sh_tab[i].label, "%s", cur_label[0] ? cur_label : "unlabelled");
            break;
        }
    }
    ReleaseSRWLockExclusive(&sh_lock);
}

void aftermath_wait(void) {
    if (!on) return;
    GFSDK_Aftermath_CrashDump_Status st = GFSDK_Aftermath_CrashDump_Status_Unknown;
    for (int i = 0; i < 200; i++) {   // up to 10 s, as the SDK's sample waits
        if (f_status(&st) != GFSDK_Aftermath_Result_Success) break;
        if (st == GFSDK_Aftermath_CrashDump_Status_Finished || st == GFSDK_Aftermath_CrashDump_Status_CollectingDataFailed) break;
        Sleep(50);
    }
    rt_log("aftermath: crash dump status %d%s", (int)st, dumped ? "" : " (no dump handed over)");
}

int aftermath_on(void) { return on; }
#else
int aftermath_enable(void) { return 0; }
void aftermath_label(const char *label) { (void)label; }
void aftermath_shader(const uint32_t *code, uint64_t words) { (void)code, (void)words; }
void aftermath_wait(void) {}
int aftermath_on(void) { return 0; }
#endif
