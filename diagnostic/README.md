# Offline pixel acceptance diagnostic

**Hardware acceptance remains UNACHIEVED.** This package contains a diagnostic harness and CPU-only validation, not evidence that graphics work on the device. Audience: developers and reviewers of the guest rendering acceptance check. No diagram is needed for this linear procedure.

## Execution boundary

No GPU, device, guest, SSH, adb, VM, host renderer, or crosvm execution is authorized by this package. GitHub Actions only parses/compiles Python source in memory and tests the production helpers with injected CPU EGL/GL fakes. It never runs a graphics binary or loads native graphics libraries. The workflow needs only the runner's standard-library Python; it installs no dependencies.

The diagnostic CLI refuses native execution unless `--allow-gpu-execution` is explicitly present. Importing the module does not load EGL or GLES. The flag is an accidental-execution protection, **not a security boundary**, permission grant, or confirmation workflow. Calling Python helpers directly can bypass the CLI gate. A parent agent's approval, a CI success, possession of an artifact, or supplying this flag does not bypass the requirement for a separate explicit user confirmation before any future GPU/device execution. No such execution is part of this delivery.

This is a Python source artifact, not a built graphics binary. Neither source nor any graphics binary is to be executed on a device by CI or by this delivery. Existing host-build workflows are unchanged and are not invoked by the diagnostic workflow. There is no deployment or automatic graphics run.

## Harness contract

- The native backend, if separately authorized in the future, uses `libEGL.so.1` and `libGLESv2.so.2`, a surfaceless EGL display, an ES2 context and a requested RGBA8 single-sample pbuffer.
- `--frames` defaults to 1 and accepts only integers from 1 through 32. The production `Harness.run()` also checks the bound; checks and operational calls do not use Python `assert` and remain active with `python -O`.
- Both queried pbuffer dimensions must equal 64. Framebuffer zero is explicitly bound and must report `GL_FRAMEBUFFER_COMPLETE` before drawing.
- Triangle vertices are (-0.8, -0.8), (0.8, -0.8), (0.0, 0.8). GLES pixel coordinates use a lower-left origin. Interior (32, 32) and exterior (0, 63) are sampled as RGBA unsigned bytes with exact comparisons.

| Zero-based frame parity | Triangle / expected (32, 32) | Background / expected (0, 63) |
| --- | --- | --- |
| Even | (255, 0, 0, 255), red | (0, 0, 255, 255), blue |
| Odd | (0, 255, 0, 255), green | (255, 0, 255, 255), magenta |

Both colors change between adjacent frames, rejecting a stale previous frame at either sample. A default one-frame run cannot establish cross-frame freshness; at least two frames are needed for that check. Two samples do not prove all pixels are correct, and a two-state pattern cannot distinguish a replay from two frames ago. No swap, presentation, compositor, long-duration stability, host memory contract, or independent hardware identity is validated.

`glFinish` and readback are checked per frame. Dithering, blending, scissor, depth and cull tests are disabled explicitly. GL errors are checked immediately after each operation, and EGL result/error pairs are checked by stage. Shader compile and program link status and full returned logs are emitted, including on failure (a reported log length above 1 MiB is rejected rather than silently truncated).

The output includes full returned EGL vendor/version/client-API/extensions strings and GL vendor/renderer/version/shading-language/extensions strings as JSON records. JSON escaping preserves string contents without terminal control interpretation. The renderer must contain all three case-insensitive substrings `zink`, `gfxstream`, and `mali`; these are only minimum reported-string checks. They do not independently establish an exact physical GPU ID, model, serial number, or active hardware execution path.

Resources acquired before an error are retained for cleanup. A `try/finally` covers setup and rendering. With a current context, cleanup disables/unbinds GL state and deletes the buffer, program and shaders before releasing current context. It then destroys context, destroys surface, terminates the display and releases the EGL thread. Every cleanup call is checked; remaining steps are attempted after a failure. Primary and cleanup errors are both retained. `PASS` is emitted only after successful cleanup; any runtime or cleanup failure yields a nonzero CLI status and no final `PASS`. Process crashes, native hangs and uncatchable termination cannot be repaired by Python `finally`.

## Offline evidence

`.github/workflows/pixel-acceptance-offline.yml` runs on the isolated diagnostic branch using a hosted CPU runner. The tests patch `ctypes.CDLL` before importing the production module, making native library loading fail closed, and inject a raw fake API into the actual `Harness`. They exercise frame bounds, actual draw/read helpers, stale/corrupt samples, identity checks, exact dimensions, incomplete default FBO, compile/link logs, immediate GL/EGL errors, partial acquisition, ordered teardown, cleanup failures and suppression of final `PASS`. The fake's pixel fixtures are independent of the production expectation function. Tests run normally and with Python optimization enabled.

The Actions artifact includes harness, tests, this README, workflow source, test logs, machine-readable CPU-only evidence, commit/run provenance and source SHA256 checksums. A green run validates Python and stub-driven control flow only; it is not a real EGL/GLES ABI test or device acceptance evidence. There are no local test/compile steps or device steps in this delivery. Consult the Actions run and commit attached to the delivery report for the actual validation result.
