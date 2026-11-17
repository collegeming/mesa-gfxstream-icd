#!/usr/bin/env python3
"""Offline candidate recipe for crosvm849e: metadata only, no renderer/window operations.

Apply in CI after the SAME baseline compatibility helpers. Changes only three source files.
Coverage: existing transfer_read direct/staging boundary. Import/pool/mmap paths are NOT covered.
No new query, pixel read, window lock, flush, wait, conversion or alpha modification.
"""
import argparse
import hashlib
from pathlib import Path

BLOBS = {
    "devices/src/virtio/gpu/virtio_gpu.rs": "fd10f93626e75b20aa53bc3bb1d9f138006e3145",
    "gpu_display/src/lib.rs": "9b7bb3879f12bdb7ec7223b2106556d76a041e61",
    "gpu_display/src/gpu_display_android.rs": "4a4008518fe6533d958873850d193f5919c58869",
}


def replace(text, old, new, count=1):
    if text.count(old) != count:
        raise ValueError("source anchor count mismatch: " + old[:90])
    return text.replace(old, new)


def transform(path, text):
    if path == "gpu_display/src/lib.rs":
        text = replace(text, "    fourcc: u32,\n}\n\nimpl<'a> GpuDisplayFramebuffer", "    fourcc: u32,\n    // Diagnostic provenance only; never used to select layout or conversion.\n    android_buffer_metadata: Option<(i32, i32)>, // raw format, stride in pixels\n}\n\nimpl<'a> GpuDisplayFramebuffer")
        text = replace(text, "            bytes_per_pixel,\n            fourcc,\n        }", "            bytes_per_pixel,\n            fourcc,\n            android_buffer_metadata: None,\n        }")
        return replace(text, "    pub fn fourcc(&self) -> u32 {\n        self.fourcc\n    }", "    pub fn fourcc(&self) -> u32 {\n        self.fourcc\n    }\n\n    pub fn android_buffer_metadata(&self) -> Option<(i32, i32)> {\n        self.android_buffer_metadata\n    }")
    if path == "gpu_display/src/gpu_display_android.rs":
        text = replace(text, "        Self::new(\n            VolatileSlice::new(buffer),", "        let mut framebuffer = Self::new(\n            VolatileSlice::new(buffer),")
        return replace(text, "            DRM_FORMAT_ABGR8888,\n        )\n    }\n}", "            DRM_FORMAT_ABGR8888,\n        );\n        framebuffer.android_buffer_metadata = Some((anb.format, anb.stride));\n        framebuffer\n    }\n}")
    text = replace(text, "struct VirtioGpuResource {\n    resource_id: u32,", "struct VirtioGpuResource {\n    resource_id: u32,\n    // Creation enum, NOT a FourCC. Blob/restore without provenance stays unknown.\n    diagnostic_resource_format: Option<u32>,")
    text = replace(text, "        VirtioGpuResource {\n            resource_id,", "        VirtioGpuResource {\n            resource_id,\n            diagnostic_resource_format: None,")
    text = replace(text, "        let resource = VirtioGpuResource::new(\n            resource_id,\n            resource_create_3d.width,\n            resource_create_3d.height,\n            0,\n        );", "        let mut resource = VirtioGpuResource::new(\n            resource_id,\n            resource_create_3d.width,\n            resource_create_3d.height,\n            0,\n        );\n        resource.diagnostic_resource_format = Some(resource_create_3d.format);")
    contract = '''// One record per first/changed completed transfer contract; process-wide hard cap 16.
// No mutex, no new window locks, no pixel inspection. Logged only AFTER existing flip.
#[derive(Clone, Debug, PartialEq, Eq)]
struct ScanoutFormatContract {
    resource_id: u32,
    scanout_id: Option<u32>,
    surface_id: u32,
    path: &'static str,
    resource_format: Option<u32>, // creation enum; None = unknown (blob/restore)
    cached_query_ok: bool, // Rutabaga::query reads cached info_3d, NOT a live export
    query_fourcc: Option<u32>,
    query_stride_bytes: Option<u32>,
    scanout_fourcc: Option<u32>,
    scanout_stride_bytes: Option<u32>,
    selected_fourcc: u32,
    sink_fourcc: u32,
    readback_stride_bytes: u32,
    sink_stride_bytes: u32,
    anb_format_stride_pixels: Option<(i32, i32)>,
    readback_ok: bool,
}

fn reserve_scanout_contract_event() -> Option<usize> {
    static EVENTS: std::sync::atomic::AtomicUsize = std::sync::atomic::AtomicUsize::new(0);
    EVENTS.fetch_update(Ordering::Relaxed, Ordering::Relaxed,
        |value| (value < 16).then_some(value + 1)).ok().map(|value| value + 1)
}

'''
    text = replace(text, "struct VirtioGpuScanout {", contract + "struct VirtioGpuScanout {")
    text = replace(text, "    flush_probe_counter: u64,", "    flush_probe_counter: u64,\n    diagnostic_last_contract: Option<ScanoutFormatContract>,")
    text = replace(text, "            flush_probe_counter: 0,", "            flush_probe_counter: 0,\n            diagnostic_last_contract: None,", count=2)
    text = replace(text, "        let transfer_fourcc = rutabaga\n            .query(resource.resource_id)\n            .ok()\n            .map(|query| query.drm_fourcc)", "        // Preserve this EXISTING cached query result; never add a renderer query.\n        let diagnostic_query = rutabaga.query(resource.resource_id);\n        let transfer_fourcc = diagnostic_query\n            .as_ref()\n            .ok()\n            .map(|query| query.drm_fourcc)")
    text = replace(text, "        let readback: VirtioGpuResult = (|| {\n            let packed_stride = self.width as usize * 4;\n            let fb_stride = fb.stride() as usize;\n            if fb_stride == packed_stride && fb.can_copy_direct_from(transfer_fourcc) {", "        let diagnostic_packed_stride = self.width as usize * 4;\n        let diagnostic_direct = fb.stride() as usize == diagnostic_packed_stride\n            && fb.can_copy_direct_from(transfer_fourcc);\n        let mut diagnostic_contract = ScanoutFormatContract {\n            resource_id: resource.resource_id,\n            scanout_id: self.scanout_id,\n            surface_id,\n            path: if diagnostic_direct { \"transfer_read_direct\" } else { \"transfer_read_staging\" },\n            resource_format: resource.diagnostic_resource_format,\n            cached_query_ok: diagnostic_query.is_ok(),\n            query_fourcc: diagnostic_query.as_ref().ok().map(|q| q.drm_fourcc),\n            query_stride_bytes: diagnostic_query.as_ref().ok().map(|q| q.strides[0]),\n            scanout_fourcc: resource.scanout_data.map(|data| data.drm_format.0),\n            scanout_stride_bytes: resource.scanout_data.map(|data| data.strides[0]),\n            selected_fourcc: transfer_fourcc,\n            sink_fourcc: fb.fourcc(),\n            readback_stride_bytes: if diagnostic_direct { fb.stride() } else { diagnostic_packed_stride as u32 },\n            sink_stride_bytes: fb.stride(),\n            anb_format_stride_pixels: fb.android_buffer_metadata(),\n            readback_ok: false,\n        };\n        let readback: VirtioGpuResult = (|| {\n            let packed_stride = self.width as usize * 4;\n            let fb_stride = fb.stride() as usize;\n            if diagnostic_direct {")
    text = replace(text, "        strace!(\"flush.transfer_read.flip.end\");\n        readback", "        strace!(\"flush.transfer_read.flip.end\");\n        diagnostic_contract.readback_ok = readback.is_ok();\n        if self.diagnostic_last_contract.as_ref() != Some(&diagnostic_contract) {\n            if let Some(event) = reserve_scanout_contract_event() {\n                base::warn!(\"SCANOUT_CONTRACT event={}/16 unknown=None query_kind=cached_info_3d query_status={} query_error={:?} {:?}\",\n                    event, if diagnostic_query.is_ok() { \"cached_ok\" } else { \"cached_error\" },\n                    diagnostic_query.as_ref().err(), diagnostic_contract);\n            }\n            self.diagnostic_last_contract = Some(diagnostic_contract);\n        }\n        readback")
    # fb_stride was used only in the original direct predicate. Do not leave a dead binding.
    return replace(text, "            let fb_stride = fb.stride() as usize;\n            if diagnostic_direct {", "            if diagnostic_direct {")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument("--check", action="store_true", help="validate exact original sources; do not write")
    args = parser.parse_args()
    pending = {}
    for path, expected in BLOBS.items():
        raw = (args.root / path).read_bytes()
        blob = hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest()
        if blob != expected:
            raise ValueError("not exact849e metadata source: " + path + " blob=" + blob)
        old = raw.decode("utf-8")
        new = transform(path, old)
        # No operation is duplicated; candidate only retains results of the old operations.
        for operation in (".query(", "transfer_read(", "get_android_surface_buffer(",
                          "post_android_surface_buffer(", "copy_from_frame("):
            assert old.count(operation) == new.count(operation), (path, operation)
        assert old.count("DRM_FORMAT_ABGR8888,") == new.count("DRM_FORMAT_ABGR8888,"), path
        pending[path] = new
    if not args.check:
        for path, text in pending.items():
            (args.root / path).write_text(text, encoding="utf-8", newline="\n")
    for path, text in pending.items():
        print(hashlib.sha256(text.encode()).hexdigest() + "  " + path)
    print("CHECK_ONLY" if args.check else "APPLIED_METADATA_ONLY; NOT_BUILT; NOT_DEVICE_TESTED")


if __name__ == "__main__":
    main()
