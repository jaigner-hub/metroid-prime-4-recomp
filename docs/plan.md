# Plan

## Phase 0: survey

Answer these from the user's dump before writing any port code. Record answers here (facts about the game only, no game data).

| Question | Answer |
|---|---|
| Release in scope, final update version and title ids | Switch 1 release. Base XCI: title id `010019A01E2F2000`, version 0, display version 1.0.0. Update NSP: Meta title id `010019A01E2F2800`, version 131072 (`v131072`, the second update), display version 1.1.0; its Program NCA is a BKTR patch over the Switch 1 base (same title id, NVN and SDK 19.3 inside), so it is the Switch 1 code even though its `control.nacp` names the application with the "Nintendo Switch 2 Edition" suffix. Both packages verified by `tools/extract_exefs.py` (FS header, hash table and every ExeFS block; signatures not checked). Extracted to `build/extracted/v0/exefs` and `build/extracted/v131072/exefs`. Target: 1.1.0 (`v131072`) |
| ExeFS modules and their sizes | `rtld`, `main`, `subsdk0`, `sdk`: one subsdk fewer than TotK, two fewer than FE3H. NSO file sizes in 1.1.0 (bytes): `main` 15,426,217; `sdk` 6,248,767; `subsdk0` 3,429,165; `rtld` 8,550. Loaded segment sizes (text / ro / data): `main` 20,508,192 / 4,067,328 / 751,600; `sdk` 6,612,672 / 5,160,960 / 345,312; `subsdk0` 3,509,104 / 1,982,464 / 199,560. The update changes every module (`sdk` and `subsdk0` keep their sizes: an SDK patch-level rebuild, 19.3.13 to 19.3.15). `main` is `GameFramework.nss` (Retro's build name, module path under `RS15`), `subsdk0` is `multimedia` (`nn::movie`), `sdk` is `nnSdk`. `main` needs `multimedia.nss` and `nnSdk.nss`. `main` exports `malloc`, `free`, `calloc`, `realloc`, `aligned_alloc` and the C++ `new`/`delete` set, so the game supplies the allocator, as TotK does. `main.npdm`: 3 cores (0 to 2), main thread priority 44, 1 MiB main stack, handle table 512, `Allow Debug` set, retail ACID |
| SDK version string in `main` | `main` carries no SDK MW tag. `sdk` is `NintendoSdk_nnSdk-19_3_13-Release` in 1.0.0 and `19_3_15` in 1.1.0, with the `NintendoSDK_NVN` and `NintendoSDK_libz` tags of the same version; `subsdk0` is `NintendoSDK_movie` of the same version. So SDK 19.3, as the listings said. The `sdk` module also holds `nn::nifm`, `nn::socket` and `nvrm_gpu` code, as every SDK build does; what matters is what `main` imports (below) |
| Graphics API: NVN, Vulkan or OpenGL imports | NVN, exactly as TotK and FE3H reach it: `main` imports `nvnBootstrapLoader`, `nv::InitializeGraphics`, `nv::FinalizeGraphics`, `nv::SetGraphicsAllocator` and `nv::SetGraphicsDevtoolsAllocator`, and takes every other NVN entry through the bootstrap loader (`nvnDevice*` builder and `nvnDeviceSetIntermediateShaderCache` names appear in `main`'s strings). No `glslc*` import in `main` (unlike FE3H): the runtime GLSL compiler is not used, only `sdk`'s own glslc allocator hooks exist. `subsdk0` imports the `NvDdkVic*`, `NvBlit*` and `NvOs*` symbols for hardware video decoding, as FE3H's `multimedia` module did |
| How the shaders ship (precompiled Maxwell code, GLSL source for glslc, or both) | Precompiled Maxwell code. The base RomFS root holds `MaterialArchive.arc` (30 MiB, `RFRM` form of type `MTRL`), `main` names the file. It is 77 KB of plain header followed by 10,518 zstd frames. The first 3,558 decoded frames are `DXBC` blobs (chunks `RDEF`, `ISGN`, `OSGN`, `SHEX`, `STAT`; built by "Microsoft (R) HLSL Shader Compiler 6.3.9"), that is Direct3D 11 bytecode with reflection, one per shader. They are followed by 1,734 shader pairs for the GPU: a block headed by a Maxwell shader program header word and a 256-byte-aligned block headed by `0x12345678` holding the SASS (3,500 Maxwell `EXIT` encodings counted across them). Pass names in the plain header (post-processing, shadows, clustered culling, indirect draw, stream-out) describe Retro's renderer. No GLSL source, SPIR-V or `DKSH` anywhere in the archive. So TotK's shader path applies: Maxwell to SPIR-V through the translator in `tools/xlate/`; the DXBC copies are reflection or a leftover of the Windows toolchain, to be confirmed in Phase 1 from how `main` reads the archive. The exact frame packaging (how the `0x12345678` block and the header block are paired, and what the 3,480 frames that `zstd -d` refuses are) is Phase 1 work too |
| Middleware (audio, video, compression, physics) | Retro Studios' own engine (`RetroBuildInfoStamp`, `RetroFMV` strings), no Havok, PhysX, Wwise, FMOD, CRI or Bink strings. Compression: zstd linked statically into `main` (the two `ZSTD_trace_*` imports are its weak trace hooks) and used for the shader archive; `LZ4` and `libz` strings also present. Audio: `nn::audio` audio renderer, 175 imported functions, including effect types newer than FE3H's SDK (compressor, light limiter, I3DL2 reverb, splitter, aux send/return, circular buffer sink) and `ParseAdpcmHeader`; no `nn::codec` (no Opus in `main`, although `hwopus` is in the service list). Video: `nn::movie` through `subsdk0` (`movie::Decoder`, `Extractor`, `MediaData`, `Buffer::SetNvnDev`), with the FMV container handled by `RetroFMV` code in `main`; codec not yet known (no movie files in the RomFS tree, they are inside the paks) |
| `nn::*` functions imported, and how many the TotK and FE3H hosts already implement | `main` imports 449 symbols: 64 are libc, libm, C++ runtime and the NVN entries above, 434 are `nn::*` and `movie::*` (`subsdk0`'s own 665 imports are `nn::*` and NVIDIA `NvOs*`/`NvDdkVic*`). By namespace: `nn::audio` 175, `nn::os` 71, `nn::hid` 42, `nn::fs` 23, `movie` 20, `nn::oe` 13, `nn::nfp` 12, `nn::account` 8, `nn::vi` 7, `nn::mem` 6, `nn::time` 4, `nn::ec` 2, `nn::err` 2, `nn::settings` 2, `nn::pl`, `nn::ro`, `nn::util`, `nn::result` 1 each. By symbol, the TotK host replaces 75 and the FE3H host 83 (`tools/survey_imports.py`); the rest run as lifted `sdk` code over SVCs and IPC, as in both ports. Of note for the host: `nn::hid` vibration player, node and mixer (HD rumble patterns), six-axis sensors, touch screen and `ShowControllerSupport`; `nn::oe` display resolution and its change event, performance mode and configuration, CPU boost; `nn::pl::IsRunningOnOunce` (Switch 2 detection, must say no); `nn::ro::ProtectRelro`; `nn::fs` save data (`EnsureSaveData`, `MountSaveData`, `GetSaveDataSize`, `Commit`), `MountRom` plus `QueryMountRomCacheSize`, and `SetAllocator`; `nn::ec::ShowShopApplicationInformationForUpgrade` and `nn::err::ShowUnacceptableApplicationVersionError` (upgrade and version applets). `sdk` is the only module that issues SVCs: 58 distinct numbers, of which the FE3H host does not handle 0x13, 0x14 (shared memory), 0x23, 0x28, 0x2A, 0x2F, 0x32, 0x33, 0x40 to 0x45, 0x4B, 0x50 to 0x52, 0x70 and 0x72; which of those the game reaches is a Phase 1 question |
| Lifter run over `main`: unknown instructions, unresolved jump tables | FE3H `recomp/gen.py` @ `47ef991` (TotK's, unchanged) over 1.1.0, nothing adapted: `main` 99,388 functions, 5,127,048 instructions, 344 MB of C in 19 s; `subsdk0` 14,152 functions, 61 MB; `sdk` 40,076 functions, 115 MB. Unsupported instructions: 0 in every module. Trap sites (`rt_udf`): 24,509 / 7,744 / 26,116, all on zero padding words, `udf #1` words or two undecodable data words per module, none on real code. Jump tables resolved: 613 / 334 / 574; all-label fallbacks: 12 / 0 / 30. `tests/jumptables_check.py` against the Unicorn oracle: `main` 500 agree, 2 disagree (the resolver lists more targets than the oracle at both), 1 table only the oracle finds, 9 computed sites unresolved; `subsdk0` 310 agree, 0 disagree; `sdk` 469 agree, 0 disagree, 2 only the oracle (1 of them an oracle error), 27 computed sites unresolved. Every hoisted table base verified, no PLT stub miscounted. The check exits 1 for the disagreements and only-oracle tables, the same class of leftover FE3H carried into its Phase 1 |
| RomFS layout (archive format, compression) | Base RomFS: 27,242,247,956 bytes, 443 files, no per-file compression at the RomFS level. Everything is Retro's `RFRM` form container: 441 `.pak` files (`PACK` form with `TOCC`, `META` and `STRG` chunks, the Metroid Prime Remastered layout), one `.arc` and one `.bin`. Root: `Kernel.pak`, `MiscData.pak`, `StartupUI.pak`, `MaterialArchive.arc`, `AreaPakManagerCacheNX.bin`. `Preload/` holds two paks, 3.7 GB, `UI/` two, `Worlds/` 434 (22 GB): one pak per room, per zone, per zone transition and per vista. Compression inside the paks: zstd frames and some LZ4 blocks seen in the sampled paks. The update's RomFS (BKTR patch) is not yet read: `tools/extract_romfs.py` with hactool does that in Phase 1 |
| Frame-rate assumptions in game logic (the game runs at 60 fps on Switch 1) | Not decidable from strings: `main`'s rodata holds both 1/60 and 1/30 as float constants (2 each), 60.0f eleven times and 30.0f six times. `nn::oe::GetPerformanceMode` and `SetPerformanceConfiguration` are imported (docked and handheld profiles). Phase 1 question, to answer from the frame loop once it runs |
| Online and amiibo services linked | amiibo: yes, `nn::nfp` (12 functions: initialize, list devices, mount, start/stop detection, model info and the three attach-event calls). Accounts: `nn::account` (open preselected user, user selector, list users). Online: none in `main`'s imports (no `nn::nifm`, `nn::socket`, `nn::friends`, `nn::bcat`, `nn::ldn`, `nn::prepo`, `nn::ssl`), although the NPDM service list grants the usual set (`nifm:u`, `bsd:u`, `ssl`, `friend:u`, `bcat:u`, `ldn:u`, `prepo:u` and others; 50 services in all). So no network stubbing beyond what the SDK's own initialization asks for; `nn::ec` shop applet and the version-error applet are the only store-facing calls |

## Phase 1: boot to the title screen

Lift all modules, link them on the host, bring up the services the game calls, and draw the first frames.

What FE3H hit between the first black frames and its title screen is in fe3h-pc-recomp `docs/plan.md`, Phase 1.
Expect the SDK-version class of problem again, in the other direction: FE3H's SDK was older than TotK's and this
game's is newer.

**Started 2026-11-02.** The lifter, runtime, tests and build tools came over from fe3h-pc-recomp @ `47ef991` in one
copy (see "Shared core"); the translator built as `build/xlate/mp4_xlate.exe`. Game-bound tables rewritten for this
game before the first build: the boot driver (`runtime/boot.c`: three modules, their init_array sizes, the 1 MiB main
stack, the first `nvnBootstrapLoader` return address per version, and SDK 19.3's three-callback `nn::init::Start`,
which is TotK's form again after FE3H's two-callback 6.5.0), the main stack in `runtime/kernel.c`, the version names in
`tools/build_game.py`, the module bases in `tests/diff/lift.py`, and the window title. `runtime/ov_mp4.c` starts
empty: no entry of FE3H's `ov_fe3h.c` applies, as `main` imports no network namespace.

**First build (2026-11-02):** `mp4.exe` compiles in 90 s and links; `--selftest` passes (153,616 functions). The
differential tests pass on this game's code: `tests/diff/lift.py` 24,000 encodings, 71,967 cases, 0 failures;
`tests/diff/func.py` 410 whole functions, 1,859 cases, 0 failures (run with the Windows venv, as the Linux side has no
clang). Host probes: `--probe-loader` passes with this game's measured tables; `--probe-kernel`, `--probe-gfx-abi`,
`--probe-astc`, `--probe-dpi` pass; `--probe-ipc` reaches the SDK client's render loop with the SDK 19.3 heap
constructor (`sdk+0x217770`) and a fall-back for `GetAudioRendererElapsedFrameCount`, which this game does not
import; its one failure is the frame period (9.8 ms against 5 ms), which the FE3H build reproduces on this machine,
whose USB audio device opens with a 480-sample period. `--probe-services` lists the overrides this game does not
import (`nn::oe` focus and volume calls, `nn::prepo`, `nn::codec` hardware Opus, `nn::swkbd`, mouse and keyboard)
and those it links but the probe does not exercise; the host's own checks there pass. `tests/excl_mt.py`'s ABA cases
fail with this machine's clang 23.1.4 on the FE3H tree too, so that is not this port's doing. A pitfall of the rename
worth recording: the settings code assumed the five-character `FE3H_` prefix in four places (`cfg.c`, `input_map.c`,
`ui/binds_page.cpp`, `ui/menu.cpp`); the length now follows the prefix string. The TotK-era `RT_V143` switch is pinned
on, as SDK 19.3 belongs to the 20.5 generation.

`--boot` runs through `nn::init::Start`, `nnosInitialize`, the rtld callback and `nninitInitializeSdkModule`, and stops
where the host wants its RomFS, whose merge (`tools/extract_romfs.py`: the 27 GB base NCA copied out of the XCI, then
hactool's BKTR merge with verification) takes most of an hour on this machine.

**Reached (2026-11-02, later):** `--boot` passes: the first `nvnBootstrapLoader` call comes from `main+0xa20f54`,
the address Phase 0 found, after 170 SVCs, 12 threads and 256 host calls, 3.4 s after the process starts. The RomFS
merge took 24 min (28.2 GB, 445 files, two more than the base). What stood between the SDK init and that call, in
the order found, each a host answer to a call the TotK and FE3H games never made:

- `SetHeapSize`: the game's start-up binary-searches the largest heap the kernel grants between 0 and 8 GiB in 2 MiB
  steps, so it expects a clean failure for a size too large. The SDK's wrapper (sdk+0x1455f0) maps OutOfResource,
  OutOfMemory and LimitReached to its own ResultOutOfMemory and aborts on any other result; the host answered
  InvalidCurrentMemory. It now answers OutOfMemory (`memory.c`). The search settles on the application pool
  (0xCD500000 less the images), the hardware's answer too.
- `nn::oe::GetPerformanceConfiguration`, lifted, read the apm session the overridden `Initialize` never opened: it now
  returns the configuration the game last requested per mode (`ov_system.c`, `ov_mp4.c`), and
  `GetDefaultDisplayResolution` and its change event are answered (1080p docked, 720p handheld; an event that never
  fires).
- `nn::fs::MountRom` and `QueryMountRomCacheSize` take an extra `int` in SDK 19.3; the same handlers serve both forms
  (`romfs.c`). `GetSaveDataSize` answers the control data's 10 MiB save and 4 MiB journal (`fs_host.c`).
- `nn::account`: the game lists the open users (`ListOpenUsers`), opens one by Uid and can show the user selector; all
  answer with the host's one local user (`ov_system.c`).
- `nn::hid`: `SetGyroscopeZeroDriftMode` opened the hid service; it, `IsSixAxisSensorAtRest`, `InitializeTouchScreen`
  and `GetTouchScreenStates<16>` are answered at the SDK's detail layer, where ov_input.c replaces the others
  (`ov_mp4.c`). The SDK's vibration player, node and mixer classes run lifted and reach the host's `SendVibrationValue`.

**Into the world (2026-11-02, night):** the game walks its RomFS, opens every area pak and the update's patch pak,
loads its save, presents frames at 58 fps and plays its intro movie. What stood in the way, in order:

- Paths. The game spells its paths with backslashes and "." components (`rom:\StartupUI.pak`, `rom:\./Worlds`),
  which Horizon accepts and the host's path code refused; it normalises them at its fs entry now (`fs_host.c`).
- File handles. The game keeps its open mode in the low three bits of a file handle and masks them off before
  closing, so handles must be 8-byte aligned like Horizon's pointers; the host's are multiples of 8 now.
- RomFS directories. The game lists the whole RomFS tree to find its paks (TotK and FE3H never listed it); listings
  are built from the index (`romfs.c`).
- The audio renderer's `SetVoiceDropParameter` command (12), which this game sends.
- Movies. The game plays H.264 in `nn::movie`'s texture mode: the decoder writes into the game's own NVN textures
  (RGBA8_SRGB, `VIDEO_DECODE`), passed to `GetOutputBuffer` as the buffer's data pointer; the SDK's decoder ran
  lifted into the NVIDIA driver and aborted. The host decodes with openh264 (BSD, linked statically from MSYS2) and
  writes the frames through the back end's texel write (`ov_movie.c`, beside TotK's VP9 buffer-mode decoder); in
  texture mode the game never releases an output index, so the slot is freed after the write. A null decoder stands
  in for any other type, taking the stream and dropping it so the player reaches its end. The conversion to RGBA is
  SSE2; a 1080p60 stream decodes in 4 ms, two at once in 15-19 ms, and a few percent of frames drop.
- Audio. The game's final mix goes through an Aux effect to the game's own DSP code and back, into a 5.1 device sink;
  the host folds it to stereo. The sound was wrong, and clean with the Aux round trip bypassed (`MP4_AUDIO_AUX=0`).
  The ring trace showed why: the Aux ring holds exactly two renderer frames (2,880 samples for six channels), so the
  game's DSP code must run between every two consecutive renders, which hardware's fixed 5 ms cadence guarantees;
  the host rendered frames in bursts whenever its output queue fell below target and overwrote the ring before the
  game read it. The render thread now keeps one frame per period even when the device asks for more (`audren.c`);
  with that the game reads and writes a whole frame between every two renders.

Diagnostics added on the way: `MP4_BREAK_AT` and `MP4_BREAK_WATCH` (a log at chosen lifted function entries with the
arguments and a watched global), the fs trace's open, directory and read lines, the movie decoder's call log. Settings
reach a Windows process started from WSL only through `WSLENV`.

**The window origin (2026-11-03):** the opening cinematic's text crawl showed a faded, vertically flipped copy of
itself. The frame dump (`MP4_DUMP`) placed it: the bloom chain's first downsample came out upside down while the
tone map from the same source, same vertex shader, same quad and same uniforms did not; and with no host change
between runs the tone map came out upside down as well, so the quads themselves map texture v=0 to the bottom of the
viewport. That is GL's render-to-texture convention, and the game keeps NVN's default window origin, LOWER_LEFT
(`nvnDeviceSetWindowOriginMode` is logged now; TotK sets UPPER_LEFT, so the host never met the default). In that mode
NDC +y lands on the last memory row of a render target and the display turns the window texture over; window y is the
memory row in both modes, so viewports, scissors, `gl_FragCoord` and the front face need no change. The host now keeps
Vulkan's own viewport orientation for the lower-left origin instead of the negative-height viewport it uses for the
upper-left one, turns the frame over in the present blit (and the upscaler's), and reverses the rows of screenshots
and dump PNGs so they read as the screen does (`nvn_origin_lower_left` in `nvn_api_core.c`; `vk_exec_draw.c`,
`gfx_window.c`, `vk_fsr.c`, `boot.c`, `vk_dump.c`). Two wrong turns on the way, both reverted: mirroring viewport
and scissor rectangles against the target height plus the translator's YNegate for `gl_FragCoord`, and mirroring the
front face; the dump showed the bloom shader reads no `gl_FragCoord` at all.

**The cinematic crash (2026-11-03):** a few seconds into the opening cinematic the game faulted in `main+0x115980`,
a pop from an empty pool of GPU timer queries (three per profiler object; the pop with a count of zero overwrites the
count with a pointer and the next pop reads wild). A breakpoint trace (`MP4_BREAK_AT` on the pop, the query's
IsReady and its fence setter, `MP4_BREAK_BRIEF=1`, `MP4_BREAK_FOLLOW=x1:0x978@0x340`) showed the bookkeeping: each
query carries the game's submit count at issue, the game's reclaim (`main+0xbb880`) polls its syncs in order with a
zero timeout and advances a completed count, and a query returns to the pool once the completed count reaches its
value. Each of two profilers pops one query per frame, so at a frame's first pop the submits of two frames ago must
have completed. Hardware's GPU pace gives that; the host's replay, stalled by a pipeline build, let the CPU run up to
five submits ahead (seen in the trace) and the pool ran dry in three of six runs. The window acquire now bounds the
distance: it waits for the GPU to finish the image's last present (as the display's release implies on hardware) and
for every submit made before the previous acquire (`MP4_GPU_AHEAD`, then default 1 frame of overlap; 0 serialises the
frames; 0 was the default in 0.4.1 only, below). Three scripted runs through the cinematic pass at 59 fps. `nvnSyncWait` logs a blocking wait that times
out and a poll that fails eight times on the same fence, which ruled out a stuck sync first. On the way the game also
asked for `nvnTextureBuilderGetSparseTileLayout`, answered with Maxwell's 64 KiB tile sizes by bytes per block.

**The cinematic's audio and picture (2026-11-03):** past the text crawl the cinematic stayed black. Its movie carries
an AAC track (`audio/mp4a-latm`, decoder type 4 in buffer mode), which the host had taken over as a null decoder;
the player waits on its audio clock, so the decoded video frames were never drawn (the frame dump showed no draw of
the movie texture). The library's audio decoders are software (FDK AAC and libopus inside subsdk0, the Android
codec framework around them) and only the video decoders need the NVIDIA driver, so the audio decoder is left to
the library and runs lifted: the cinematic plays with picture and sound at 59 fps (`MP4_MOVIE_AUDIO=null` restores
the null decoder). No host AAC decoder needed.

**Into the first area (2026-11-03):** the save loads into gameplay with the HUD over a black world. In order:

- The first gameplay frames use transform feedback (`nvnCommandBufferBindTransformFeedbackBuffer` and the begin,
  end, pause, resume and draw entries). They are accepted and dropped for now; a replay needs
  `VK_EXT_transform_feedback` and the streamout layout of the vertex program, which the control section does not
  visibly hold. The vertex shaders of that pass read attributes only, so their push descriptor set had no bytes and
  `vkCmdPushDescriptorSetWithTemplateKHR` got a NULL pointer, which faulted inside the NVIDIA driver; found with the
  Khronos validation layer (`MP4_VK_VALIDATION=1`, installed on this machine).
- The world was black because the volumetric fog composite multiplied the scene by an empty fog volume. The volume
  (RGBA16F 76x44x64) is written by a points draw under rasterizer discard whose vertex shader stores into it as a
  storage image. Two things kept it empty: the texture was made without `NVN_TEXTURE_FLAGS_IMAGE`, so the host had
  given its image no storage usage and bound a dummy (storage usage now follows format support); and the replay never
  recorded shader image stores as GPU writes, so the mirrored texture was re-uploaded from its untouched storage
  (`vk_texture_gpu_written_id`, from the draw's and dispatch's storage image bindings).
- The game's streamed light and fog volumes (one BC6H and five BC1 3D textures of 384x192x528) are sparse textures
  in virtual pools mapped page by page from a CPU_CACHED pool the game fills from its paks; TotK maps only physical
  pools, which the host backs with device memory. A virtual pool now has shadow storage of its own from its creation
  (`mem_shadow_reserve`: watched host memory below the address limit, since the game builds its textures before it
  maps their pages), each mapping copies the pages into it, and textures over it are mirrored from it. A sparse
  texture is laid out so that eight blocks make a 64 KiB tile (`nvn_tex.c sparse_tiles`); the BC1 volumes' block
  height (2 GOBs) and depth (16) were confirmed by decoding the dumped pages under every candidate and scoring the
  smoothness of neighbouring blocks (`MP4_DUMP_VPOOL` writes them). Mappings from physical pools into a shadowed pool
  are not supported.
- The BC6H volume (the HDR light volume the fog scattering and the ambient lighting read) was still laid out with the
  tile shape the aspect rule gives 16-byte blocks (32x8x16, block height one GOB), and its pages decoded as striped
  garbage: the fog scattering slices showed the stripes, the scene stayed dark, stray glows crossed the sky now and
  then, and the arm cannon rendered black (its lighting comes from that volume). Two dumps of the same frame, the
  default against `MP4_SPARSE_TILES=1,4`, settled it: the volume decodes coherently with blocks two GOBs high, so its
  tile is 16x16x16 blocks (`nvn_tex.c sparse_tiles`). The page scoring that confirmed the BC1 layout had not been
  conclusive for BC6H. With that the cannon is lit and the scene brighter; the tone mapper's exposure (the first
  float of its c3 block, 0.031 at frame 7200 with `MP4_DUMP_UBO=1`) is live, fed by the 1x1 luminance reduction,
  two vertex-shader passes over storage buffers and a 64-byte copy the CPU reads back.
- Transform feedback is replayed (VK_EXT_transform_feedback). Every ~245 frames the game binds a 1 MiB buffer,
  begins, draws four rasterizer-discard triangle lists with a vertex-only program that transforms positions
  (gl_Position gets (0, 0, 0, 1), location 0 the world-space vertex), ends, and reads the buffer back on the CPU;
  with the bracket dropped it read whatever the buffer held. The capture layout is not in the program's control
  section: the fields that differ between programs (dword 0x7f8 and the bytes at 0x804) are a vertex attribute order
  every program has, and 0x714/0x734, once taken for streamout flags, are set in nearly every program. The replay
  takes the layout from the translated vertex shader instead (`vk_pipeline.c spirv_add_xfb`: the located outputs in
  location order, packed, with the TransformFeedback capability, the Xfb execution mode and the XfbBuffer, XfbStride
  and Offset decorations), keyed by a bit in the pipeline key and the pre-raster library key. The command stream
  carries the bind, begin and end (`OP_XFB_*`); the first draw after a begin binds the buffer and begins the capture
  in the open pass, the end op or the pass end ends it, and a synchronization-2 barrier after the pass hands the
  vertices to the write-back. The calls go through the recorder thread (`vk_rec.c rec_xfb_*`): made directly they
  ran ahead of the draws the recorder had still queued, which the validation layer reported as a begin under a
  pipeline without Xfb and a barrier inside the pass. `MP4_DUMP=xfb` dumps the frame of the first begin from there
  on and marks the captured draws "xfb"; `MP4_DUMP_BUF` on the buffer showed the captured positions. The control
  buffer (32 bytes at the begin and end addresses, zero as the game fills it) is not written back yet. Whether the
  readback was behind the stray glows is for the user's next session to tell.
- Stray blue and purple streaks (from the first meteor impact on) were the game's flare sprites: an octagon drawn
  from one instance record by a vertex shader that indexes its attributes, translated with one input array
  `in vec4 a[16]`. The reflection listed only the locations read at fixed offsets, so the array's other locations had
  no Vulkan attribute, and NVIDIA's driver fetched other data into the array (found by evaluating the shader on the
  dumped inputs on the CPU and capturing the GPU's outputs with `MP4_DUMP_XFB_PROBE`). Every location 0-15 now gets an
  attribute: the shader reflection now marks every Location an interface array spans (`gfx_shader.c reflect_spirv`),
  which also gives the vertex shader outputs for a fragment shader's indexed varyings (validation had reported a
  fragment input at Location 9 with no vertex output). The scripted save went from 60-100 frames with stray blue to none.
- Game time ran slow: the frame update gets a fixed 1/60 s step (main+0x236544 loads it from the frame message, the
  update at main+0x236d78 hands it on), the frame's length at the Switch's 60 fps. At 41 fps here the game ran at about
  0.7 of real time and subtitles fell seconds behind their voices. The patch set realtime (`recomp/patches-v131072.txt`,
  `MP4_REALTIME`, on by default) loads the step from the patch page, where `runtime/frame_rate.c` writes the 5-frame
  average of display intervals (at most 1/15 s), TotK's real-time step. Found by counting GetSystemTick callers per
  frame (`MP4_BREAK_COUNT=1`) and logging the step argument (`MP4_BREAK_BRIEF` now prints s0 and s1).
- A dark box around the morph ball: the fog pass (pass 6 of the frame, after the light list) samples each listed light's
  cookie texture through an array whose handles are in user uniform block 3 (Ryujinx `fp_t2d_cb3_0[32]`, element k at
  byte 8k). The replay gave such resources slot -1 and bound element k to texture slot k - 1, so the ball's light read
  the depth buffer and darkened the fog over its 48-pixel screen tiles. `vk_exec_draw.c cbuf_handle` reads the handles
  from the bound block. Scripts can now end in `<tick> SHOT` / `<tick> DUMP`; `build/input/fast.txt` (A every second) reaches gameplay by tick 3000;
  `build/input/ball.txt` morphs at tick 3000.
  The box came back once plant shadows came ahead of the ball in that list: each record (144 bytes from c3+0x80) holds
  its shadow texture handle at +224, and the array was 32 elements long, so every record after the first clamped to
  element 31 (a dummy). Arrays from block 3 are now 1024 long (`tools/xlate/Program.cs`), the others stay 32.
  `build/input/ball2.txt` morphs and rolls forward, where it showed; `MP4_DUMP_UBO=<n>` prints n vec4 per block.
- The lighting shaders sample the light list, an R32UI texture, and Ryujinx declares every sampler as float; the
  host's per-slot texture answers never reached the translator either, because its slot lookup knew only TotK's
  separate-texture handles at c2+0x168 while this game uses combined handles at c2+0x20 (`Program.cs TexSlot`).
  Translator patch 0010 declares an integer sampled image when the host says the texture is integer
  (`QuerySamplerFormat`, answered as `_uint` / `_sint` suffixes) and bitcasts the texels; the use masks were bumped
  (`USE_VERSION 2`) so the keys relearn which slots matter. With that the tone-mapped scene shows the room lit, with
  the fire's glow.
- The magenta polygons and cyan streaks in the sky, and the meshes that showed their back faces and lost their
  fronts, were the face winding: under the lower-left origin NVN measures it in a y-up window space while the
  replay keeps Vulkan's orientation, so the front face has to be mirrored (`vk_pipeline_set_dynamic`; LAS Recomp
  does the same). The earlier attempt at this had been reverted together with the viewport mirroring; a dump with
  `MP4_FRONT_FLIP=1` settled it. With that the first area renders as it should: rocks, Samus's ship, the sky, the
  fire and the energy field, at 59 fps.
- Morphing into the ball stopped the process in `rt_dispatch`: a switch in `main+0x54e9f0` jumped to a case
  (`main+0x54ec38`) that the generated code took for an exit, since the generator had cut that function into a
  dozen fragments. The cause was in function discovery (`gen.py discover`): an ADRP+ADD pair is read as a
  materialised address even when an instruction between the two rewrites the register (`adrp x8; add x8, x8, #a;
  ...; ldr x8, [x19, #0x30]; add x0, x8, #0x34`), which made an address in the middle of the function a start,
  and the branch fixpoint split the rest. Pairs with such a rewrite are dropped now (capstone names the registers
  an instruction writes): 9,946 of them in `main`, and the discovery fixpoint went from seven rounds to two. Every
  module was regenerated.
- Frame rates above 60 (2026-11-04). The frame limit (`MP4_FPS_LIMIT`, 30 to 360 or `display`) came over with
  `runtime/frame_rate.c`, but with only the update's step patched the player still moved by the frame: the same inputs
  at the same real time (`build/input/rate_compare.txt`, ticks after the save has loaded) walked and turned Samus about
  twice as far at 120 fps as at 60. Much of the game reads the frame's length from a pair of floats in rodata,
  main+0x13bf400 = {1/60, 1/30}, picked by the frame-rate mode at main+0x1826d70 (24 sites, all
  `adrp; add #0x400; ldr s, [x, w, uxtw #2]`). An experiment that wrote the step into the pair matched 60 and 120;
  the patch set realtime now points the 24 ADRP+ADD pairs at patch page +8, where the step is kept in both elements.
  The other time constants (2026-11-05): every ADRP-based load in `main` of the 1/60 float at main+0x13be780 (60
  sites), the 1/60 double at main+0x13bf338 (23), a second 1/30 float at main+0x13bea88 (5) and the {60, 30} table at
  main+0x13bf500 (3) was read in place. None is a time step. The engine scales per-frame quantities by `dt * 60`
  ("frames"), and these constants turn frames back into seconds or test against one Switch frame:
  - Frames to seconds (most of the double and about 15 float sites): a property getter (vtable +0x28) or an integer
    times 1/60, the timeline at main+0xde6a60..0xe1f194 that counts in frames, and the 3D emitter table at
    main+0x1833118 (main+0x5bd40, main+0xbab314), whose velocity is the position delta over `frames * 1/60`.
    `MP4_BREAK_AT=main+0x5bcb0` at 120 fps showed the callers already pass `frames = dt * 60` (0.5 in 3,524 of 4,000
    calls), so that velocity is right at any rate.
  - Frame-rate-independent already: `powf(c, dt / (1/60))` damping (main+0xd36d84, main+0x120df38), `clock / (1/60)`
    as a frame number (main+0x10022ac), a 1/60 finite difference (main+0x129ad8c), a history ring sampled every 1/60 s
    of game time (main+0x120a218), clamps and thresholds (`max(x, 1/30)`, `max(x, 1/60)`).
  - The time-slot scheduler main+0x1c4968 (and its twin main+0x85b228): 18 lists of jobs booked into slots 1/60 s of
    game time apart (the clock at `[world+0x6f0268]+0xa4`, seconds of game time; at 120 fps it advances by 1/120),
    and about 25 clients that test `slot + 1/60 < clock` (missed: book again) and `clock >= slot` (run). Above 60 fps a
    slot spans two frames and the job runs in the first, as meant; below 60 a frame can step over a slot and the job
    runs one booking later. Left as is.
  - The {60, 30} table is indexed by a mode in what looks like a frame-rate governor (main+0x346c94, main+0x1256acc);
    it sets no step.
  - A same-frame test (main+0x2a2534): main+0x2a2490 keeps a copy of an object's transform unless object, version and
    time are unchanged, the time being `|stamp - clock| < 1/60`. With the real-time step that lets a frame a little
    shorter than 1/60 pass for the same frame, and above 60 fps the copy is refreshed every other frame. The patch set
    realtime now reads half the step there (patch page +16). The first area does not reach the function (no hit with
    `MP4_BREAK_AT` in a scripted walk), so its effect is not seen yet. Movies keep time at any rate (`ov_movie.c` hands frames over by timestamp); the title
  screen's attract movie is stopped by the game after about 2400 frames, so at 120 fps it moves on after 20 s instead of
  40 (`MP4_MOVIE_EOS_BT=1` logs the guest backtrace of the end-of-stream packet; the stop comes from the RetroFMV input
  thread, main+0xe7002c). In the first area at 120 the scripted walk holds 110 to 120 fps in places and about 82 in the
  heaviest view, where the render thread is busy about 12 ms a frame; at the monitor's 240 Hz the light views reach
  200 fps.
- Shader preparation (2026-11-04). Phase 0 placed the shaders in `MaterialArchive.arc`; that holds only a few hundred
  of them. Every program is in an `SNVN` chunk: the archive's, and those inside each material (`MTRL`, 1,913 assets) and
  vector graphic (`SVGM`) of the paks, the update's `Patch/Z_198_NXPatch.pak.patch` (itself a `PACK`) among them. The
  formats are in `tools/shader_inventory.py`: a pak's `TOCC`/`ADIR` lists its assets (60-byte entries); a material is an
  `RFRM` header and one zstd frame; an `SNVN` chunk is an entry table of zstd frames without content sizes, each frame
  the code blocks of one or more programs followed by their 0x880-byte control sections in the same order. The
  archive's `NVN2` chunk has a 216-byte control layout this build never uses (the Switch 2 build's, most likely) and
  `SDX ` the DXBC copies. About 260,000 programs in all, read in 12 s; all 2,826 that one play of the first area met
  are found by the runtime's own code hash (`gfx_shader.c build_code_hash`, stage from control +0x714). Too many to
  translate up front (each also has about 12 binding variants in play), so the release ships a list of the variants
  play met (`tools/prepare_shaders.py --make-list`, from `shader-answers.bin`, no game code) and the installer
  prepares those from the player's files (`docs/installer.md`, "Shader list"). A fresh copy of the build prepared from
  the list of one first-area play (34,812 variants, 2 minutes, 1.9 GB) translated 14 shaders in the same scripted play
  instead of 1,883, and draws waiting for a shader fell from 24,549 to 30. Frame pacing in gameplay was already alike
  (translation runs in the background, a draw waits or is skipped); what remains are draws skipped while their
  pipelines link (about 2,000 in both), which the list does not cover.
- Pipeline list (2026-11-05). Those skips are pipelines a fresh install has never built: the stage libraries the
  preparation warms are keyed by vertex output unions it did not know, so most draws still waited for a worker. The
  machinery for a played install already existed (`pipeline-keys.bin`, built at every boot); the release now ships its
  records as `data/pipeline-list.bin`, and `--prepare-shaders` builds them (before the libraries, so these get the
  unions) into `pipeline-cache.bin` and the driver cache. Same scripted play on a fresh prepared copy: 2,247 draws
  skipped without the list, 75 with it; 1,548 pipelines built in play, 53; boot waited 1.5 s for the prewarm. Details
  in `docs/installer.md`, "Pipeline list".
- The save-load hitch (2026-11-05). The first frame of play after the save load took 1.5 s and the next 0.5 s
  (scripted first-area play, NVIDIA GPU). `MP4_TRACE=perf` now logs uploads, image creations, mirror creations and
  pool imports over 2 ms, and `MP4_PROFILE_SLOW` (read with totk-pc-recomp `tools/prof_report.py`'s map loader) showed
  the rest. Three causes, three changes:
  - Seven streamed light and fog volumes (3D BC6H/BC1, 1408x256x1008, 173 and 346 MiB, in virtual pools) were
    deswizzled whole at first use (1.2 GB, 535 ms) and whole again the next frame when the game mapped more tiles
    (259 ms). `vk_texture.c` `upload_part` copies only the block-linear blocks over pages written since the last upload;
    on a first upload over virtual pool shadow storage the blocks nothing wrote are zero and come from a zeroed slab.
    Texture upload in that frame: 566 to 18 ms. `MP4_TEX_PARTIAL=0` restores the whole uploads; screenshots of both
    agree, and `--probe-vkmem` checks the block box reads.
  - Each pool import (`VK_EXT_external_memory_host`) cost about 50 ms on the NVIDIA driver from the import of the
    1.8 GiB asset pool on until the first frame of play, whatever its size: half in
    `vkGetMemoryHostPointerPropertiesEXT`, half in `vkAllocateMemory`; that frame imports 14 pools. The memory types
    are now asked once (`import_pool`). The allocation half remains (about 25 ms each, 3 ms later in play); a
    submission referencing the large import did not change it. Importing neighbouring pools as one allocation would.
  - The first sync of that 1.8 GiB pool's VRAM mirror took 180 ms in write-watch queries: each full answer of 1,024
    pages was followed by a query from the start of the range again. The queries now continue after the last page
    (`ww_next`): 27 ms. `MP4_MEMORY=verify` compared about 123,000 mirror pages every 60 frames through play with no
    mismatch.
  Result in the same play: worst frame 1,545 to 501 ms, the next 483 to 359 ms (the GPU copying the 1.8 GiB pool into
  its mirror), frames over 100 ms in the first minute 20 to 6, 1% low 19 to 33 fps.
- The rest of that hitch (2026-11-05, later). Upload batches now carry GPU timestamps (`MP4_TRACE=perf`: batches over
  5 ms of GPU time, with their staging bytes), which corrected two guesses: the next frame's wait was not the mirror
  copy, and the replay's own GPU work in those frames stayed under a millisecond.
  - Pool imports share one `vkAllocateMemory` per 64 MiB chunk of the guest heap (`vk_memory.c` arenas; each pool keeps
    its own buffer, bound at its offset). The first frame of play's 14 imports at ~25 ms each are gone. Chunks whose
    import fails (some hold pages the game made inaccessible) leave their pools importing alone. `MP4_IMPORT_ARENA=0`.
  - The light volumes (12 of them, 2.6 GB, 3D BC in virtual pools) get their images when the game registers them in a
    texture pool, during the load, and those in shadow storage are zeroed then from a device-local zero buffer
    (`vk_texture.c` `vk_texture_register`, `zero_image`); their first upload copies only written blocks. Zeroing them
    at the first upload had cost an upload batch 112 ms of GPU time in the first frame of play, and creating them then
    a further wait for the new allocations. `MP4_TEX_EARLY=0`.
  - Lazy mirrors (each 64 KiB chunk copied at its first binding) were built for the 1.8 GiB pool and timed alike once
    the above was in; `MP4_MEMORY=verify` found one mismatching page in ~11 million in each of two lazy runs and none
    without, cause not found, so `MP4_MIRROR_LAZY` stays 0. Allocating that pool's mirror early made no difference
    and was dropped.
  Result: the first frame of play 501 to about 207 ms (two runs: 207, 208). The cost moved into the load: one frame of
  the loading screen at about 220-280 ms (the light volumes' residency and zeroing). 1% low in the first minute 33 to
  46 fps, frames over 100 ms 6 to 4. Left: about 5 s into play the game streams 179 textures in one frame (an upload
  batch of 69 MiB and 120 ms of GPU time, a 120-180 ms frame); the zero fill under the loading screen.
- The 179-texture frame (2026-11-07). About 8 s into scripted first-area play (frame ~2470) one upload batch takes
  65-120 ms of GPU time and the frame waits on it. The batch perf line now says what a batch holds (`vk_upload_note`):
  that one is 81-83 ASTC textures (80 M texels) and 12 plain uploads (13 MiB); the save-load frame's batch is the same
  mix (46 ASTC, 65 M texels). The RTX 4070 samples no ASTC, so the compute decoder (`vk_astc.c`) does it all.
  - `MP4_ASTC_DUMP=<dir>` writes the blocks and regions of every upload the decoder takes (under `build/`, game data),
    and `--bench-astc` times the decoder on them away from the game and checks every texel against `astc_decode_ldr`.
    The burst's 111 textures (105 M texels): 16-19 ms of dispatches and 29-40 ms of batch wall time on a busy GPU,
    129 us per M texel for 5x5; in the game the same work ran at 660 us per M texel. After two idle seconds the
    first run took 182-228 ms of dispatches: the GPU's clocks. The game keeps the GPU at about 3 ms of work per frame,
    so the burst meets a GPU at low clocks. A scratch buffer kept from call to call (against fresh memory per texture)
    changed nothing and was not kept.
  - When the game registers those textures (`vk_texture_register`): 286-767 frames before their first draw, and the
    data is mostly written after the registration. But the game registers far more than it draws: 1,625 ASTC textures
    (1,012 M texels at level 0) by frame 2400, of which 200 were drawn in the run; in the 300 frames before the burst
    it wrote 974 registered ASTC textures (785 M texels) and drew 50. Decoding ahead from registration or from recent
    writes would cost gigabytes of RGBA8 for textures nothing draws.
  So the decode stays at the first draw, and a frame decodes at most a budget of it: `MP4_ASTC_BUDGET` (M texels per
  frame, default 8; 0 restores whole decodes). A mipmapped ASTC texture that does not fit decodes its small levels
  (at least those of up to 128 K texels per layer) and is sampled through views with a minimum LOD
  (`VK_EXT_image_view_min_lod`; `VkTexImage.min_lod`, part of the view key in `vk_exec_draw.c` `get_view`); its larger
  levels follow one per frame, oldest texture first (`vk_texture.c` `astc_pend_step`, run before each frame's first
  submission is flushed, so a level is in the queue before the draws that sample it). Without the extension nothing
  changes. Result in the same play: the burst's largest batch 62 ms to 9 ms of GPU time, spread over about ten frames
  of 5-9 ms batches; slow frames in frames 2400-2700 one (68 ms) to none. The burst's textures look soft for those
  ten frames and are sharp again by frame 2504 (screenshots of both runs at the burst and after it). The save-load
  frame (frame ~2216) still waits 130 ms on a fence whose batch took 7 ms of GPU time: that is the load's residency
  cost above, not the decode. The validation layer reports nothing new (the two VUIDs it lists predate this).
- The crash leaving Sol Valley (issue #7, 2026-11-10), reproduced on a reporter's save (about a minute from the load
  to the door). As the game leaves the area it finalizes six virtual pools, and the next `nvnQueueSubmitCommands`
  uploads a texture over one of them from its storage: the shadow had been freed with the pool, and `tex_bl_copy`
  faulted reading its first page (`0x15c0000000`). On the console that read meets unmapped GPU memory. A finalized
  pool's shadow is now reset to zero pages and kept for the next virtual pool that fits (`memory.c`
  `mem_shadow_release`), and `vk_texture.c` `storage_texels` logs an uncommitted storage instead of copying it.
  Crash reports now name the host function the guest called (`main.c`), and `MP4_HOSTFN_LIST=1` lists them.
- The same spot has the first transform feedback capture into two buffers (program 139, a vertex-only program; 432 x
  432 points into 84- and 64-byte records). Unlike the single-buffer programs, its control section carries the layout:
  per buffer b a u16 stride at 0x6dc + 2b, an output count at 0x6e8 + b, and per output the attribute index
  (0x20 + Location) at 0xdc + 0x80b + i and its component count at 0x4dc + 0x80b + i (buffer 0 locations 0-5 with
  1, 4, 4, 4, 4, 4 components, buffer 1 locations 6-9). `gfx_shader.c` `gfx_shader_xfb_layout` reads it from the
  shader's stored inputs and `spirv_add_xfb` follows it; the earlier inference packed all ten outputs into buffer 0.
  `MP4_DUMP=xfb2` dumps the frame of the first capture into more than one buffer, and a dump writes each program's
  control sections (`prog_<serial>_<stage>.ctl`). Not yet checked: that the area past the door draws right from them.
- Past the door (2026-11-10, the reporter's save, four runs played by hand):
  - The frame rate fell from about 110 to 66-75 fps there: the user's `shot_ring = 8` (mp4.ini). The ring waits for
    the GPU at every present, reads the frame back and filters it on the guest main thread (`boot.c` box_filter and
    `vk_read_texels`, together more samples than the lifted code on that thread). Without it both sides of the door hold
    the 120 fps cap (render thread 2.5 ms a frame before, 4.5-5 ms after; GPU 1.6 and 2 ms). Making the ring
    asynchronous is left.
  - A pixel shader there was "translator output missing or malformed" and its draws skipped for the run: two
    translations of one program ran at once, one under its final cache key and one under its provisional key whose
    masked answers came out the same, and the provisional one renamed its files onto the final key while the other read
    them. `gfx_shader.c` `translate` now serialises translations per program (code hash). One in 436 logs.
  - Without the ring the GPU lost the device 1 to 13 s after the door, each time soon after the two-buffer capture
    (40 to 800 frames): a write to an invalid address (0x4bd6000 twice, 0x5838000 under the address watch).
    `MP4_VK_ADDR_WATCH=<hex>` (VK_EXT_device_address_binding_report) logs every bind and unbind of a range holding
    the address; nothing was ever bound there, so it is no freed object but a write through a bad address or offset.
    With `MP4_GPU_AHEAD=0` (frames serialised, which the ring's per-present wait also gave) 100 s there ran clean, at
    99 instead of 120 fps. So it is a race between the CPU running a frame ahead and the GPU; the lead is the
    write-back of the two capture buffers against the game's read of them. 0.4.1 ships with 0 as the default until
    it is found. `fault-ops.txt` now starts at the oldest submission the GPU had not finished (at most 16 back).
  - With the validation layer, draws reported set 0 "disturbed by a previous call that set VkPipelineLayout ... which is
    not compatible", and with `MP4_GPL=0` (validation still on) the run past the door stayed clean. The stage
    libraries were cached by shader alone, so one built under another layout's set (the boot prebuild's, made from the
    stage alone, or another pipeline's) was linked into pipelines whose set differs. `PrKey`/`FsKey` now carry the
    built set's hash (`Layout.set_h`). The white flicker of textures there is gone with it (the user's play, overlap
    on). The device loss is not: it came again about 900 frames past the door (a write at 0x4fd6000), and the layer
    still reports the set 0 message in the first area, perhaps its INDEPENDENT_SETS compatibility check (see
    `vk_exec_draw.c` bind_resources).
  - Without GPL (`MP4_GPL=0`) and the CPU a frame ahead, validation off, 4,600 frames past the door ran clean at 120 fps;
    with GPL every one of five runs lost the device 40 to 900 frames in. So 0.4.2 turns GPL off by default on every
    driver (`vk_caps.c` vk_caps_gpl; before, only on AMD's RDNA 1/2 branch, for device losses with fast-linked
    pipelines in issue #5, likely the same bug) and puts `MP4_GPU_AHEAD` back to 1. What in the fast links makes the
    store go astray is not found yet.
  - Unattended reproduction: `MP4_INPUT_RECORD` writes `SYNC <n>` lines whenever the virtual pool creations and
    finalizations (`memory.c` mem_shadow_events) change, and times the lines after them from there; the player waits
    for the run to reach n and plays them from that moment. Timed lines had counted from the first present, so a title
    or save load of another length shifted every move after it, and a replay walked off the route. A recording of the
    reporter's save from the load through the door (`build/input/friend-route.txt`, 18 SYNC points) replays to the
    door every time (sync 24, the door's pool finalizations, at 89.1-89.2 s in eight runs). With it, CPU a frame
    ahead: 0.4.2's defaults 3 of 3 clean; GPL 3 of 3 device losses; GPL with `MP4_BIND_SKIP=0`, `MP4_PUSH_SKIP=0` or
    `MP4_PUSH_STAGE=fs` lost the device too; `MP4_GPL_FAST=0` (the independent layouts GPL needs, no stage libraries
    or fast links) 2 of 2 lost, also with the capture pipelines left without the vertex output union. So the
    trigger is the stage-independent layouts (vertex set 0, fragment set 1, a support buffer each), not the libraries.
    The layer's "set 0 disturbed" message names the pipeline's own layout on both sides (debug names on layouts and
    pipelines under validation), its INDEPENDENT_SETS check. GPU-assisted validation disabled itself after an
    internal error. `MP4_GPL_LOG=1` logs each fast link with its shaders.
  - Inside the independent layouts, three more replays lost the device as well: `MP4_PUSH_STAGE=none` (both sets
    allocated), `MP4_DS_FRESH=1` (no set reused between draws, pools destroyed instead of recycled) and
    `MP4_VS_UNION=0` (each pipeline's vertex outputs from its own fragment shader, as with shared layouts). Left: what
    differs in the shaders themselves, the fragment stage moved to set 1 (`spirv_set`) against the shared layout's
    renumbered bindings (`spirv_rebind`).
  - Also with independent layouts and no libraries, each of these lost the device: `MP4_DESC_TEMPLATE=0` (write
    structs), `MP4_STORE_SINK=all` (every store into a written storage buffer or a stored image sent to the stand-ins),
    `MP4_SKIP_OPS=XFB_BIND,XFB_BEGIN,XFB_END`, `MP4_SKIP_OPS=COPY_B2B,COPY_B2T,COPY_T2B,COPY_T2T,CLEAR_BUF` and
    `MP4_SKIP_OPS=REPORT_COUNTER,RESET_COUNTER`, and a replay with nothing touching the window (no focus message or
    Alt in the log). So the faulting write is no storage store, capture, copy, fill or counter write the replay makes;
    render targets are left. Alt-tab is not it: 0.4.2's defaults played by hand, focus lost and regained 8 times around
    the door, ran clean. The window now logs focus changes and Alt / window-menu messages with the frame.
  - Still with independent layouts: `MP4_RT_SINK=1` (every draw discards its primitives), `MP4_MEMORY=import` (no
    mirror syncs or write-backs), `MP4_ASTC=cpu` and `MP4_NO_FREE=1` (no deferred image, view or buffer destroyed, no
    pool memory freed) lost the device as well, hands off at about frame 10,500 (88 s in, just before the door). The
    address watch of the earlier run watched another address than the one that faulted, so "never bound" was not shown.
    No path of the runtime's own is left that the layouts change; next is NVIDIA Nsight Aftermath for the faulting
    shader and resource. A replay the user alt-tabbed out of lost the device 17 frames after the focus loss: focus
    changes shift the timing and set the race off sooner; 0.4.2's defaults do not have it.
  - Found with Nsight Aftermath (`runtime/aftermath_win.c`, a development build with `MP4_AFTERMATH_SDK`, run with
    `MP4_AFTERMATH=1`): the fault was a shader-core *read* that failed to translate, at an address no resource held,
    with six of our fragment shaders active (VK_EXT_device_fault had said "write invalid", so no write path mattered).
    The cause, in `vk_exec_draw.c` bind_resources: with independent layouts, a pipeline without a fragment shader
    (depth, shadow, capture passes) has an empty set 1; the loop that marks where set 1's descriptors start never
    reached it, so `d0[1]` stayed 0, set 0's size came out 0 and set 0 was neither pushed nor bound. The draw ran
    with the previous draw's set 0 under another layout and fetched through stale descriptor memory, which is recycled
    per frame, hence the CPU-ahead and focus timing. The validation layer had said so ("... fs 00000000 ... uses set 0
    but that set is not bound"); it was taken for its INDEPENDENT_SETS false positive. Fixed: 0 such messages in a
    75 s validation run, three replays with GPL on and the CPU a frame ahead clean to frame 20,200 at 120 fps. 0.4.3
    puts GPL back on by default (off on AMD RDNA 1/2 as before). `MP4_DEBUG_SET0_BUG=1` restores the bug for testing
    crash reports.
  - Crash reports: on a device loss the runtime writes the VK_EXT_device_fault vendor binary as
    `gpu-fault-<date>-<time>.bin` beside the log. On NVIDIA it is an Aftermath crash dump behind the extension's
    56-byte header; `tools/decode_gpu_fault.py` decodes it with the SDK's DLL (page fault, active shaders, warps,
    full JSON), so players enable nothing. The dump's shader hashes are Aftermath's, which an `MP4_AFTERMATH=1` run
    maps to the runtime's shader keys. The SDK stays out of the repository and releases (its license distributes only
    the DLL, not under an open source license); `make_release.py` refuses to run with `MP4_AFTERMATH_SDK` set.
  - `MP4_PROFILE_FRAMES=key`: F8 starts and stops the sampling profiler, for a stretch of play whose frames are not
    known ahead. A white flicker of textures the user saw there is not caught yet (two rings, 120 and 600 presents,
    held only the door's own fade).

## Shared core

As pieces turn out to be identical to fe3h-pc-recomp's (and through it to totk-pc-recomp's), list them here. They are
candidates for a shared Switch recomp library.

Identical at the start (Phase 0):

- `recomp/nso.py`: unchanged through all three projects.
- `tools/check_content.py`: only the marker name differs per project.
- `tools/build_hactool.py`, `tools/extract_exefs.py`, `tools/extract_romfs.py`, `tools/romfs_reader.py`: unchanged
  from fe3h-pc-recomp apart from their headers. The title id and version come from the dump, so nothing in them
  names a game.

The copy of 2026-11-02 (Phase 1), from fe3h-pc-recomp @ `47ef991`, 176 text files plus the vendored translator sources:

- 86 files copied unchanged: all of `recomp/` (the lifter ran unchanged over this game's three modules in Phase 0 with
  no unsupported instruction), `tests/`, the NVN front end's core, the Vulkan back end's larger part, the texture
  code, the platform layer, the shaders and their SPIR-V headers, `tools/xlate/`.
- 90 files that differ only by the rename of `fe3h` to `mp4` and `FE3H_` to `MP4_` (the executable, settings file,
  environment variables, window): the kernel, SVCs, IPC, fs, RomFS, audio renderer, the rest of the NVN and Vulkan
  code, the menu, the build tools. Each file's first line says which.
- Changed since (2026-11-05): `runtime/tex_deswizzle.c` gained `tex_bl_read_box` (a box read the partial uploads use)
  and `runtime/vk_exec_xfer.c` passes the bytes its copies touch to the VRAM mirror; the rest of the change is in files
  that already differed.
- Rewritten for this game: the tables listed under Phase 1. Not taken: FE3H's overrides, patch tables, expected-hash
  files, installer and release tools.

The answer so far: a third game needed one mechanical copy and about a hundred lines of game-bound tables to reach
a build. What the boot reveals goes under Phase 1.
