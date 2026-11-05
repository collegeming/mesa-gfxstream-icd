#!/usr/bin/env python3
"""Keep fixed_blob_mapping enabled with the vulkano backend behind an env gate.

`src/crosvm/sys/linux/gpu.rs` unconditionally clears `gpu_params.fixed_blob_mapping` when the
`vulkano` cargo feature is compiled in:

    } else if cfg!(feature = "vulkano") {
        debug!("gpu fixed blob mapping disabled: not compatible with vulkano");
        gpu_params.fixed_blob_mapping = false;
    }

With `vulkano` in the binary (the DroidVM build) the `--gpu ...,fixed-blob-mapping=true` that the
wrapper passes therefore never takes effect: every CIB blob is mapped through the runtime memslot
fallback in `vm_control::try_map_to_prepared_region`, because no prepared region exists. The log
line that would explain this is `debug!`, which the device log level does not show.

This patch keeps the old behaviour as the default and makes the choice explicit:

  * `CROSVM_ALLOW_FIXED_BLOB_WITH_VULKANO` set (any value) -> `fixed_blob_mapping` stays `true`
    and one `warn!` says the gate was taken;
  * unset -> `fixed_blob_mapping = false` exactly as before, now at `warn!` so the default path
    is visible in the device log.

The vfio-passthrough branch above is untouched (`debug!`), and `external_blob` still derives from
the resulting `fixed_blob_mapping`.

Optional fail-closed fallback (`CROSVM_CIB_FAIL_CLOSED=1`, default off): in
`vm_control/src/lib.rs::try_map_to_prepared_region`, an `ExistingAllocation` whose allocation has
no prepared region currently returns `None`, so `RegisterMemory` silently allocates a fresh
runtime memslot (`MAPREGION fallback`). With the env var set to exactly "1" that path returns
`VmMemoryResponse::Err(SysError::new(libc::ENOSPC))` instead, which reaches the guest as a failed
map_blob. Unset (or any other value) keeps `return None` and the existing fallback.

Ordering: `vm_control/src/lib.rs` must already carry patch_crosvm_mapping_lease.py's
`CIB1-DIAG-CROSVM-MAPPREP` diagnostic block; this patch extends that block and refuses to write
otherwise. Re-running the script is a no-op once both patches are present.
"""
import argparse
import pathlib
import sys
from pathlib import Path

ALLOW_ENV = 'CROSVM_ALLOW_FIXED_BLOB_WITH_VULKANO'
FAIL_CLOSED_ENV = 'CROSVM_CIB_FAIL_CLOSED'

OLD_VULKANO = '''        } else if cfg!(feature = "vulkano") {
            // TODO(b/244591751): make fixed_blob_mapping compatible with vulkano for opaque_fd blob
            // mapping.
            debug!("gpu fixed blob mapping disabled: not compatible with vulkano");
            gpu_params.fixed_blob_mapping = false;
        }
'''

NEW_VULKANO = f'''        }} else if cfg!(feature = "vulkano") {{
            // TODO(b/244591751): make fixed_blob_mapping compatible with vulkano for opaque_fd blob
            // mapping.
            if env::var_os("{ALLOW_ENV}").is_some() {{
                warn!(
                    "gpu fixed blob mapping kept on with vulkano: {ALLOW_ENV}"
                );
            }} else {{
                warn!("gpu fixed blob mapping disabled: not compatible with vulkano");
                gpu_params.fixed_blob_mapping = false;
            }}
        }}
'''

OLD_PREP = '''            base::warn!(
                "CIB1-DIAG-CROSVM-MAPPREP: no prepared region for alloc={:?} offset={:#x}",
                allocation,
                dest_offset
            );
            return None;
'''

NEW_PREP = f'''            base::warn!(
                "CIB1-DIAG-CROSVM-MAPPREP: no prepared region for alloc={{:?}} offset={{:#x}}",
                allocation,
                dest_offset
            );
            if std::env::var("{FAIL_CLOSED_ENV}")
                .map(|value| value == "1")
                .unwrap_or(false)
            {{
                base::error!(
                    "CIB1-DIAG-CROSVM-MAPPREP: {FAIL_CLOSED_ENV}=1 refused runtime memslot fallback for alloc={{:?}} offset={{:#x}}",
                    allocation,
                    dest_offset
                );
                return Some(VmMemoryResponse::Err(SysError::new(libc::ENOSPC)));
            }}
            return None;
'''


def read_source(path: pathlib.Path) -> str:
    """Read with universal newlines so anchors are LF on every host."""
    with path.open('r', encoding='utf-8', newline=None) as handle:
        return handle.read()


def write_source(path: pathlib.Path, text: str) -> None:
    """Write LF regardless of os.linesep, matching the git checkout on the runner."""
    with path.open('w', encoding='utf-8', newline='\n') as handle:
        handle.write(text)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('root', type=Path)
    root = parser.parse_args().root.resolve()

    gpu = root / 'src/crosvm/sys/linux/gpu.rs'
    vmc = root / 'vm_control/src/lib.rs'
    for path in (gpu, vmc):
        if not path.is_file():
            raise SystemExit(f'missing input: {path}')

    gpu_text = read_source(gpu)
    vmc_text = read_source(vmc)

    # Required preconditions for the replacement text to compile.
    if 'use std::env;' not in gpu_text:
        raise SystemExit(f'{gpu}: `use std::env;` is missing; env::var_os would not resolve')
    if 'warn!(' not in gpu_text:
        raise SystemExit(f'{gpu}: no `warn!` in this file; the macro would not resolve')

    gpu_changed = False
    if ALLOW_ENV in gpu_text:
        # Already patched: verify the exact expected result instead of writing anything.
        if gpu_text.count(NEW_VULKANO) != 1:
            raise SystemExit(
                f'{gpu}: {ALLOW_ENV} present but the expected gated branch appears '
                f'{gpu_text.count(NEW_VULKANO)} times; refusing to guess')
        if gpu_text.count(OLD_VULKANO) != 0:
            raise SystemExit(f'{gpu}: {ALLOW_ENV} present but the unpatched branch is still there')
        print(f'skip {gpu.name}: already carries {ALLOW_ENV} (no-op)')
    else:
        found = gpu_text.count(OLD_VULKANO)
        if found != 1:
            raise SystemExit(
                f'{gpu}: expected exactly one vulkano fixed_blob_mapping anchor, found {found}')
        gpu_text = gpu_text.replace(OLD_VULKANO, NEW_VULKANO, 1)
        gpu_changed = True

    vmc_changed = False
    if FAIL_CLOSED_ENV in vmc_text:
        if vmc_text.count(NEW_PREP) != 1:
            raise SystemExit(
                f'{vmc}: {FAIL_CLOSED_ENV} present but the expected fail-closed block appears '
                f'{vmc_text.count(NEW_PREP)} times; refusing to guess')
        if vmc_text.count(OLD_PREP) != 0:
            raise SystemExit(
                f'{vmc}: {FAIL_CLOSED_ENV} present but the ungated None branch is still there')
        print(f'skip {vmc.name}: already carries {FAIL_CLOSED_ENV} (no-op)')
    else:
        found = vmc_text.count(OLD_PREP)
        if found != 1:
            raise SystemExit(
                f'{vmc}: expected exactly one CIB1-DIAG-CROSVM-MAPPREP None branch, found {found}; '
                'apply patch_crosvm_mapping_lease.py first')
        vmc_text = vmc_text.replace(OLD_PREP, NEW_PREP, 1)
        vmc_changed = True

    # Both replacements validated; only now touch the tree.
    if gpu_changed:
        write_source(gpu, gpu_text)
    if vmc_changed:
        write_source(vmc, vmc_text)

    print('patched' if (gpu_changed or vmc_changed) else 'already-patched',
          'src/crosvm/sys/linux/gpu.rs + vm_control/src/lib.rs',
          f'- fixed blob mapping kept with vulkano behind {ALLOW_ENV} (default: disabled);',
          f'runtime memslot fallback fails closed behind {FAIL_CLOSED_ENV}=1 (default: off)')
    return 0


if __name__ == '__main__':
    sys.exit(main())
