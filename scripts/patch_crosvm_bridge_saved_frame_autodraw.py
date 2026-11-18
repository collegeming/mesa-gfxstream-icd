#!/usr/bin/env python3
"""Single-variable behavior fix for the Android display bridge reconnect blackout.

Problem: with exporter=native the bridge presents only on guest flips (crosvm
flush_resource -> copy_from_frame -> display.flip).  When the app leaves the
native display view it calls removeSurface(); re-entering calls setSurface().
Nothing repaints the last frame on the new attach, so the display stays black
until an input event triggers the next guest flip.

Fix: reuse the bridge's own reviewed saveFrame/drawSavedFrame machinery
automatically, exactly two touch points in one file:
  1. removeSurface(): inside the already-held critical section, copy the last
     presented frame into mSavedFrameBuffer (the same two lines saveFrame
     performs; saveFrame itself takes the mutex and cannot be called here).
     Best effort: a failed copy is logged and removal continues.
  2. setNativeSurface(): after the attach lock is released, call the existing
     drawSavedFrame() once.  It takes the surface mutex itself, is a no-op when
     no frame was saved, and a failed draw is only logged; attach still
     returns success.

No new buffer, format, alpha, fence or geometry logic is introduced; the
saveFrameForSurface/drawSavedFrameForSurface AIDL entries keep working and
remain idempotent with this automation.

Apply on the exact pinned packages/modules/Virtualization tree
(862c6af544ce4ca89e5dc8f1aca10a832367d77f); the input file hash is asserted.
"""
import argparse
import hashlib
from pathlib import Path

TARGET = "libs/android_display_backend/crosvm_android_display_client.cpp"
PINNED_INPUT_SHA256 = "60eb1870cd0213112400ba3fd821d5878a47ee89bf34071ea43e645840df5cc7"

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
            // attach can repaint it immediately: the native exporter presents only on
            // guest flips and none arrive while the display view is gone. Same two
            // statements saveFrame performs, inline because saveFrame takes this mutex
            // itself. Best effort: a failed copy is logged and removal continues.
            if (mLastBufferValid) {
                if (auto ret = copyBuffer(mLastBuffer, mSavedFrameBuffer); ret.ok()) {
                    mSavedFrameValid = true;
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
        // Repaint the saved frame right after attach so a reconnect shows the last
        // image instead of staying black until the next guest flip. The call takes the
        // surface mutex itself and is a no-op when no frame was saved, so the unlocked
        // generation check is unnecessary; a failed draw is logged and attach still
        // returns success.
        if (auto ret = drawSavedFrame(); !ret.ok()) {
            LOG(WARNING) << "display surface " << mName
                         << " failed to redraw saved frame on attach: " << ret.error();
        }
        return {};
    }
"""


def transform(text):
    if hashlib.sha256(text.encode()).hexdigest() != PINNED_INPUT_SHA256:
        raise ValueError("input is not the pinned 862c6af5 crosvm_android_display_client.cpp")
    if text.count(OLD_REMOVE) != 1:
        raise ValueError("removeSurface anchor count mismatch")
    if text.count(OLD_ATTACH) != 1:
        raise ValueError("setNativeSurface tail anchor count mismatch")
    out = text.replace(OLD_REMOVE, NEW_REMOVE).replace(OLD_ATTACH, NEW_ATTACH)
    # The fix must not alter any existing surface/window/pixel operation.
    for operation in ("clearNativeSurfaceLocked(", "prepareNativeWindowLocked(",
                      "ANativeWindow_lock(", "notify_one()", "saveFrame()",
                      "drawSavedFrameForSurface", "saveFrameForSurface"):
        assert text.count(operation) == out.count(operation), operation
    # Exactly one automatic save and one automatic redraw were added.
    assert out.count("copyBuffer(") == text.count("copyBuffer(") + 1
    assert out.count("drawSavedFrame()") == text.count("drawSavedFrame()") + 1
    assert out.count("mSavedFrameValid") == text.count("mSavedFrameValid") + 1
    assert out.count("mLastBufferValid") == text.count("mLastBufferValid") + 1
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
          "APPLIED_FIX_BRIDGE_SAVED_FRAME_AUTODRAW; BEHAVIOR_CHANGE; NOT_BUILT; NOT_DEVICE_TESTED")


if __name__ == "__main__":
    main()
