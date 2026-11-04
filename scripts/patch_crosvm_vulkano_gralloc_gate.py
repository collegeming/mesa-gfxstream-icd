#!/usr/bin/env python3
"""Let a device run opt in to the Vulkano gralloc backend instead of always disabling it.

`src/crosvm/sys/linux.rs` creates the process-wide gralloc table with

    let flags = RutabagaGrallocBackendFlags::new().disable_vulkano();

`RutabagaGrallocBackendFlags::new()` already includes `RUTABAGA_GRALLOC_BACKEND_VULKANO`
(rutabaga_gfx/src/rutabaga_gralloc/gralloc.rs:34-40), and `RutabagaGralloc::new` inserts
`GrallocBackend::Vulkano` only when the `vulkano` cargo feature is compiled in AND
`flags.uses_vulkano()` is true (gralloc.rs:281-294). Clearing the flag here therefore removes
the only backend that can serve `VmMemorySource::Vulkan` imports: the guest blob map that
carries VulkanInfo reaches `gralloc.import_and_map` and fails with
`RutabagaError::InvalidGrallocBackend` because the runtime table has no Vulkano entry.

This patch keeps the old behaviour as the default and makes the choice explicit at runtime:

  * `CROSVM_ENABLE_VULKANO_GRALLOC=1` -> `RutabagaGrallocBackendFlags::new()` unchanged
    (Vulkano enabled);
  * anything else (unset, empty, other values) -> `new().disable_vulkano()`, byte-for-byte the
    previous behaviour.

One `base::warn!` line names the env var and the chosen backend, so the device log states which
path ran. `base::warn!` is already used in this file and `use base::*;` is already imported.
Everything else in the file, including `.context("failed to create gralloc")`, is untouched.
"""
import argparse
from pathlib import Path

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('root', type=Path)
root = parser.parse_args().root.resolve()


def replace(text, old, new):
    if text.count(old) != 1:
        raise RuntimeError(f'expected exactly one anchor: {old[:160]!r} (found {text.count(old)})')
    return text.replace(old, new, 1)


linux = root / 'src/crosvm/sys/linux.rs'
s = linux.read_text(encoding='utf-8')

OLD = '''    let flags = RutabagaGrallocBackendFlags::new().disable_vulkano();
    let gralloc = RutabagaGralloc::new(flags).context("failed to create gralloc")?;
'''

NEW = '''    // RutabagaGrallocBackendFlags::new() already includes RUTABAGA_GRALLOC_BACKEND_VULKANO, and
    // RutabagaGralloc::new registers GrallocBackend::Vulkano only when that flag is set. Clearing
    // it here left the runtime backend table without a Vulkano entry, so every guest blob import
    // that carried VulkanInfo (VmMemorySource::Vulkan -> gralloc.import_and_map) was rejected
    // with RutabagaError::InvalidGrallocBackend. Keep the old behaviour by default; a device run
    // opts in explicitly.
    let vulkano_gralloc_enabled = std::env::var("CROSVM_ENABLE_VULKANO_GRALLOC")
        .map(|value| value == "1")
        .unwrap_or(false);
    base::warn!(
        "gralloc: CROSVM_ENABLE_VULKANO_GRALLOC={} -> Vulkano gralloc backend {}",
        if vulkano_gralloc_enabled { "1" } else { "unset-or-not-1" },
        if vulkano_gralloc_enabled { "ENABLED" } else { "DISABLED (default)" }
    );
    let flags = if vulkano_gralloc_enabled {
        RutabagaGrallocBackendFlags::new()
    } else {
        RutabagaGrallocBackendFlags::new().disable_vulkano()
    };
    let gralloc = RutabagaGralloc::new(flags).context("failed to create gralloc")?;
'''
s = replace(s, OLD, NEW)

linux.write_text(s, encoding='utf-8')
print('patched', linux.name,
      '- Vulkano gralloc backend behind CROSVM_ENABLE_VULKANO_GRALLOC (default: disabled)')
