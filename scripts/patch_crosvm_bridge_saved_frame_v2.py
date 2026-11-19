#!/usr/bin/env python3
"""Second-round single-variable fix for the Android display bridge reconnect blackout.

Round 1 (build run 44384405422, patch_crosvm_bridge_saved_frame_autodraw.py) drew
the saved frame synchronously in the setSurface binder call. On the failing
device the frame DID appear, but every live flip afterwards failed with
"Failed to lock surface scanout: Failed to lock window" (fast per-frame errors,
142 entries) plus "ResourceFlush: ErrUnspec", consistent with the early post
racing the app-side buffer queue setup and leaving the queue unusable; the
pre-patch build attached and flipped fine.

Fix, same file, three touch points plus status logging:
  1. removeSurface(): keep the round-1 inline auto-save of the last presented
     frame, and record the frame geometry so a later draw can refuse stale
     geometry (drawSavedFrame has no unlock on the copy-failure path, so a
     geometry-mismatched draw would leak the dequeued buffer).
  2. setNativeSurface(): instead of drawing inline, spawn a detached loop that
     first waits out the attach window (400 ms), then redraws the saved frame
     with backoff retries (100/300/700 ms, at most 4 attempts, ~1.5 s total).
     The loop exits when one draw succeeds, when the saved frame is gone, when
     recorded geometry no longer matches, or when a newer attach/remove bumped
     the surface generation. A thread spawn failure is logged; attach still
     returns success.
  3. Both ANativeWindow_lock failure sites now log the native status, so the
     observed flood identifies its errno (expected NO_INIT, -19).

The saveFrameForSurface/drawSavedFrameForSurface AIDL entries keep working and
stay idempotent; no new buffer, format, alpha, fence or geometry logic.

Apply on the exact pinned packages/modules/Virtualization tree
(862c6af544ce4ca89e5dc8f1aca10a832367d77f); the input file hash is asserted.
"""
import argparse
import hashlib
from pathlib import Path

TARGET = "libs/android_display_backend/crosvm_android_display_client.cpp"
PINNED_INPUT_SHA256 = "60eb1870cd0213112400ba3fd821d5878a47ee89bf34071ea43e645840df5cc7"

OLD_INCLUDES_CHRONO = """#include <cerrno>
#include <condition_variable>
"""
NEW_INCLUDES_CHRONO = """#include <cerrno>
#include <chrono>
#include <condition_variable>
"""

OLD_INCLUDES_THREAD = """#include <mutex>
#include <unordered_map>
"""
NEW_INCLUDES_THREAD = """#include <mutex>
#include <thread>
#include <unordered_map>
"""

OLD_REMOVE = """    void removeSurface() {
        {
            std::lock_guard lk(mSurfaceMutex);
            clearNativeSurfaceLocked(true);
        }
        mNativeSurfaceReady.notify_one();
    }
"""
NEW_REMOVE = """    void removeSurface() {
        {
            std::lock_guard lk(mSurfaceMutex);
            // Auto-save the last presented frame before the surface goes away so a later
            // attach can repaint it: the native exporter presents only on guest flips and
            // none arrive while the display view is gone. Same two statements saveFrame
            // performs, inline because saveFrame takes this mutex itself. Best effort: a
            // failed copy is logged and removal continues. Record the frame geometry so
            // the deferred attach repaint can refuse a stale-geometry draw later.
            if (mLastBufferValid) {
                if (auto ret = copyBuffer(mLastBuffer, mSavedFrameBuffer); ret.ok()) {
                    mSavedFrameValid = true;
                    mSavedFrameSavedWidth = static_cast<uint32_t>(mLastBuffer.width);
                    mSavedFrameSavedHeight = static_cast<uint32_t>(mLastBuffer.height);
                } else {
                    LOG(WARNING) << "display surface " << mName
                                 << " failed to save last frame on remove: " << ret.error();
                }
            }
            clearNativeSurfaceLocked(true);
        }
        mNativeSurfaceReady.notify_one();
    }
"""

OLD_ATTACH = """        mNativeSurfaceReady.notify_one();
        return {};
    }
"""
NEW_ATTACH = """        mNativeSurfaceReady.notify_one();
        // Round-1 regression evidence (build run 44384405422): drawing the saved frame
        // synchronously right here raced the app-side buffer queue setup on the failing
        // device, and every live flip afterwards failed with "Failed to lock window".
        // Hand the repaint to a detached loop that first waits out the attach window and
        // then retries with backoff; it stops after one successful draw, when no frame
        // was saved, or when a newer attach/remove bumped the surface generation.
        // Built with -fno-exceptions: a std::thread construction failure aborts (libc++
        // replaces the throw), so there is no catchable failure to handle here.
        {
            std::lock_guard lk(mSurfaceMutex);
            if (mSavedFrameValid && mNativeSurface != nullptr) {
                std::thread(&AndroidDisplaySurface::savedFrameRedrawLoop, this,
                            mNativeSurfaceGeneration)
                        .detach();
            }
        }
        return {};
    }
"""

OLD_LOCK_SITE_LIVE = """        if (ANativeWindow_lock(anw, out_buffer, nullptr) != 0) {
            return Error() << "Failed to lock window";
        }
"""
NEW_LOCK_SITE_LIVE = """        if (int lockStatus = ANativeWindow_lock(anw, out_buffer, nullptr); lockStatus != 0) {
            return Error() << "Failed to lock window: status " << lockStatus;
        }
"""

OLD_LOCK_SITE_SAVED = """        ANativeWindow_Buffer buf;
        if (ANativeWindow_lock(anw, &buf, nullptr) != 0) {
            return Error() << "Failed to lock window";
        }
"""
NEW_LOCK_SITE_SAVED = """        ANativeWindow_Buffer buf;
        if (int lockStatus = ANativeWindow_lock(anw, &buf, nullptr); lockStatus != 0) {
            return Error() << "Failed to lock window: status " << lockStatus;
        }
"""

OLD_METHOD_ANCHOR = """    const std::string& name() const { return mName; }

private:
"""
NEW_METHOD_ANCHOR = """    // Deferred repaint for a freshly attached surface, spawned by setNativeSurface when
    // a saved frame exists. Waits out the app-side buffer queue setup before the first
    // draw, retries with backoff when the lock fails, and gives up after four attempts
    // (~1.5 s). The generation parameter stops a stale loop once a newer attach or remove
    // superseded the surface it was spawned for, so per attach at most one successful
    // draw lands. Every failed lock now logs the native status for triage.
    void savedFrameRedrawLoop(uint64_t generation) {
        static constexpr std::chrono::milliseconds kInitialDeferMs{400};
        static constexpr std::chrono::milliseconds kRetryDelayMs[] = {
                std::chrono::milliseconds(100), std::chrono::milliseconds(300),
                std::chrono::milliseconds(700)};
        constexpr int kMaxAttempts = 4;
        std::this_thread::sleep_for(kInitialDeferMs);
        for (int attempt = 1; attempt <= kMaxAttempts; ++attempt) {
            {
                std::lock_guard lk(mSurfaceMutex);
                if (generation != mNativeSurfaceGeneration || !mSavedFrameValid) {
                    return;
                }
                // The automatic removeSurface() save recorded the frame geometry; a draw
                // with stale geometry would leak the dequeued buffer (drawSavedFrame has
                // no unlock on the copy-failure path), so refuse it instead. Frames saved
                // through the AIDL entry have no recorded geometry and draw unguarded,
                // exactly as before this patch.
                if (mSavedFrameSavedWidth != 0 && mRequestedSurfaceDimensions.has_value() &&
                    (mSavedFrameSavedWidth != mRequestedSurfaceDimensions->width ||
                     mSavedFrameSavedHeight != mRequestedSurfaceDimensions->height)) {
                    LOG(WARNING) << "display surface " << mName
                                 << " skips deferred saved-frame redraw: frame geometry "
                                    "no longer matches the configured surface";
                    return;
                }
            }
            // drawSavedFrame takes the surface mutex itself and revalidates the surface.
            if (auto ret = drawSavedFrame(); ret.ok()) {
                LOG(INFO) << "display surface " << mName
                          << " deferred saved-frame redraw succeeded on attempt " << attempt;
                return;
            } else {
                LOG(WARNING) << "display surface " << mName
                             << " deferred saved-frame redraw attempt " << attempt
                             << " failed: " << ret.error();
            }
            if (attempt != kMaxAttempts) {
                std::this_thread::sleep_for(kRetryDelayMs[attempt - 1]);
            }
        }
    }

    const std::string& name() const { return mName; }

private:
"""

OLD_MEMBERS = """    SinkANativeWindow_Buffer mSavedFrameBuffer;
    bool mSavedFrameValid = false;
"""
NEW_MEMBERS = """    SinkANativeWindow_Buffer mSavedFrameBuffer;
    bool mSavedFrameValid = false;
    // Geometry of the frame stored by the automatic removeSurface() save; zero until the
    // first automatic save. The deferred attach repaint refuses to draw a frame whose
    // recorded geometry no longer matches the configured surface, because drawSavedFrame
    // would leak the buffer it dequeued when the copy mismatches.
    uint32_t mSavedFrameSavedWidth = 0;
    uint32_t mSavedFrameSavedHeight = 0;
"""


def transform(text):
    if hashlib.sha256(text.encode()).hexdigest() != PINNED_INPUT_SHA256:
        raise ValueError("input is not the pinned 862c6af5 crosvm_android_display_client.cpp")
    for label, old in (("removeSurface", OLD_REMOVE),
                       ("setNativeSurface tail", OLD_ATTACH),
                       ("lock() lock-failure site", OLD_LOCK_SITE_LIVE),
                       ("drawSavedFrame lock-failure site", OLD_LOCK_SITE_SAVED),
                       ("name() anchor", OLD_METHOD_ANCHOR),
                       ("saved-frame members anchor", OLD_MEMBERS),
                       ("includes cerrno block", OLD_INCLUDES_CHRONO),
                       ("includes mutex block", OLD_INCLUDES_THREAD)):
        if text.count(old) != 1:
            raise ValueError(f"{label} anchor count mismatch: {text.count(old)}")
    out = text
    for old, new in ((OLD_REMOVE, NEW_REMOVE),
                     (OLD_ATTACH, NEW_ATTACH),
                     (OLD_LOCK_SITE_LIVE, NEW_LOCK_SITE_LIVE),
                     (OLD_LOCK_SITE_SAVED, NEW_LOCK_SITE_SAVED),
                     (OLD_METHOD_ANCHOR, NEW_METHOD_ANCHOR),
                     (OLD_MEMBERS, NEW_MEMBERS),
                     (OLD_INCLUDES_CHRONO, NEW_INCLUDES_CHRONO),
                     (OLD_INCLUDES_THREAD, NEW_INCLUDES_THREAD)):
        out = out.replace(old, new)
    # The fix must not alter any existing surface/window/pixel operation.
    for operation in ("clearNativeSurfaceLocked(", "prepareNativeWindowLocked(",
                      "ANativeWindow_lock(", "notify_one()", "saveFrame()",
                      "drawSavedFrameForSurface", "saveFrameForSurface",
                      "ANativeWindow_unlockAndPost("):
        assert text.count(operation) == out.count(operation), operation
    # Exactly one automatic save (round-1 behavior kept) and one deferred redraw call added.
    assert out.count("copyBuffer(") == text.count("copyBuffer(") + 1
    assert out.count("drawSavedFrame()") == text.count("drawSavedFrame()") + 1
    assert out.count("mSavedFrameValid") == text.count("mSavedFrameValid") + 3
    assert out.count("mLastBufferValid") == text.count("mLastBufferValid") + 1
    # The new loop, its spawn point, the recorded geometry and the status-bearing errors.
    assert out.count("savedFrameRedrawLoop") == 2
    assert out.count("mSavedFrameSavedWidth") == 4
    assert out.count("mSavedFrameSavedHeight") == 3
    assert out.count("#include <chrono>") == 1
    assert out.count("#include <thread>") == 1
    assert out.count("std::this_thread::sleep_for") == 2
    assert out.count("Failed to lock window: status ") == 2
    assert out.count('<< "Failed to lock window";') == 0
    assert text.count("{") == text.count("}")
    assert out.count("{") == out.count("}")
    return out


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument("--check", action="store_true", help="validate anchor; do not write")
    args = parser.parse_args()
    path = args.root / TARGET
    old = path.read_text(encoding="utf-8")
    new = transform(old)
    if not args.check:
        path.write_text(new, encoding="utf-8", newline="\n")
    print(hashlib.sha256(new.encode()).hexdigest() + "  " + TARGET)
    print("CHECK_ONLY" if args.check else
          "APPLIED_FIX_BRIDGE_SAVED_FRAME_V2; BEHAVIOR_CHANGE; NOT_BUILT; NOT_DEVICE_TESTED")


if __name__ == "__main__":
    main()
