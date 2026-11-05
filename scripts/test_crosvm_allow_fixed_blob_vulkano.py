#!/usr/bin/env python3
"""Assert the fixed-blob/vulkano env gate invariants on a patched crosvm tree.

Run after patch_crosvm_allow_fixed_blob_vulkano.py (which itself must run after
patch_crosvm_mapping_lease.py).

Checks in `src/crosvm/sys/linux/gpu.rs`:
  1. `CROSVM_ALLOW_FIXED_BLOB_WITH_VULKANO` is read exactly once and appears exactly twice in
     total (the read plus the keep-on log line),
  2. the vulkano branch keeps `fixed_blob_mapping` on when the env var is present and otherwise
     clears it, with `warn!` on both paths,
  3. the old `debug!` disabled line for vulkano is gone, so the default path is visible,
  4. the vfio-passthrough branch is untouched (`debug!`, same message),
  5. exactly two `gpu_params.fixed_blob_mapping = false;` sites remain (vfio + vulkano default),
  6. the changed region is brace-balanced with comments and string literals ignored.

Checks in `vm_control/src/lib.rs`:
  7. `CROSVM_CIB_FAIL_CLOSED` appears exactly twice (read + error message) and is compared to "1",
  8. the fail-closed `VmMemoryResponse::Err(SysError::new(libc::ENOSPC))` return sits inside the
     `CIB1-DIAG-CROSVM-MAPPREP` no-prepared-region branch, before the unconditional `return None;`,
  9. `return None;` is still the default, so unset behaviour keeps the runtime memslot fallback,
 10. the changed region is brace-balanced and the file still parses the same
     `Option<VmMemoryResponse>` return type.

Negative controls: re-injecting the original vulkano `debug!` branch and removing the fail-closed
gate must both make the checks fail. The patch script is also re-run and must leave the tree
byte-identical (idempotency), when it is available next to this test.
"""
import pathlib
import subprocess
import sys

root = pathlib.Path(sys.argv[1]).resolve()
out = pathlib.Path(sys.argv[2]).resolve() if len(sys.argv) > 2 else None

ALLOW_ENV = 'CROSVM_ALLOW_FIXED_BLOB_WITH_VULKANO'
FAIL_CLOSED_ENV = 'CROSVM_CIB_FAIL_CLOSED'

GPU = root / 'src/crosvm/sys/linux/gpu.rs'
VMC = root / 'vm_control/src/lib.rs'

OLD_VULKANO = '''        } else if cfg!(feature = "vulkano") {
            // TODO(b/244591751): make fixed_blob_mapping compatible with vulkano for opaque_fd blob
            // mapping.
            debug!("gpu fixed blob mapping disabled: not compatible with vulkano");
            gpu_params.fixed_blob_mapping = false;
        }
'''

KEEP_ON = f'"gpu fixed blob mapping kept on with vulkano: {ALLOW_ENV}"'
DISABLED_WARN = 'warn!("gpu fixed blob mapping disabled: not compatible with vulkano");'
DISABLED_DEBUG = 'debug!("gpu fixed blob mapping disabled: not compatible with vulkano");'
VFIO_DEBUG = 'debug!("gpu fixed blob mapping disabled: not compatible with passthrough GPU.");'
ENV_READ = f'env::var_os("{ALLOW_ENV}").is_some()'
FAIL_READ = f'std::env::var("{FAIL_CLOSED_ENV}")'
FAIL_ERR = 'Some(VmMemoryResponse::Err(SysError::new(libc::ENOSPC)))'
PREP_MARK = 'CIB1-DIAG-CROSVM-MAPPREP: no prepared region for alloc={:?} offset={:#x}'
REGRESSION_TAIL = '''            return None;
        }
    };

    let (descriptor, file_offset, size) = match source {'''


def code_only(text):
    """Drop // comments and "..." literals so brace counting reflects code structure only."""
    kept = []
    i = 0
    in_string = False
    while i < len(text):
        c = text[i]
        if in_string:
            if c == '\\' and i + 1 < len(text):
                i += 2
                continue
            if c == '"':
                in_string = False
            i += 1
            continue
        if c == '"':
            in_string = True
            i += 1
            continue
        if c == '/' and i + 1 < len(text) and text[i + 1] == '/':
            newline = text.find('\n', i)
            if newline < 0:
                break
            i = newline
            continue
        kept.append(c)
        i += 1
    return ''.join(kept)


def block_balanced(text, start_marker, end_marker):
    start = text.index(start_marker)
    end = text.index(end_marker, start) + len(end_marker)
    depth = 0
    for line in code_only(text[start:end]).splitlines():
        depth += line.count('{') - line.count('}')
    return depth


def analyze_gpu(text):
    problems = []

    if text.count(ALLOW_ENV) != 2:
        problems.append(f'{ALLOW_ENV} must appear exactly twice (found {text.count(ALLOW_ENV)})')
    if text.count(ENV_READ) != 1:
        problems.append(f'{ENV_READ} must appear exactly once (found {text.count(ENV_READ)})')
    if text.count(KEEP_ON) != 1:
        problems.append(f'keep-on log line must appear exactly once (found {text.count(KEEP_ON)})')
    if text.count(DISABLED_DEBUG) != 0:
        problems.append('vulkano disabled path still uses debug!; the default path is invisible')
    if text.count(DISABLED_WARN) != 1:
        problems.append(f'vulkano disabled path must warn exactly once '
                        f'(found {text.count(DISABLED_WARN)})')
    if text.count(VFIO_DEBUG) != 1:
        problems.append('vfio-passthrough branch changed or disappeared')
    if text.count('gpu_params.fixed_blob_mapping = false;') != 2:
        problems.append('fixed_blob_mapping must only be cleared in the vfio and vulkano branches')
    if text.count(OLD_VULKANO) != 0:
        problems.append('unpatched vulkano branch is still present')

    if KEEP_ON in text and ENV_READ in text:
        env_at = text.index(ENV_READ)
        keep_at = text.index(KEEP_ON)
        clear_at = text.index(DISABLED_WARN)
        if not (env_at < keep_at < clear_at):
            problems.append('env read, keep-on warn and default warn are not in source order')
        start = text.rfind('} else if cfg!(feature = "vulkano") {', 0, env_at)
        if start < 0:
            problems.append('env gate is not inside the vulkano else-if branch')
        elif block_balanced(
                text,
                '    if gpu_params.fixed_blob_mapping {',
                '\n    }') != 0:
            problems.append('fixed_blob_mapping branch is brace-unbalanced')

    return problems


def analyze_vmc(text):
    problems = []

    if text.count(FAIL_CLOSED_ENV) != 2:
        problems.append(f'{FAIL_CLOSED_ENV} must appear exactly twice '
                        f'(found {text.count(FAIL_CLOSED_ENV)})')
    if text.count(FAIL_READ) != 1:
        problems.append(f'{FAIL_READ} must appear exactly once (found {text.count(FAIL_READ)})')
    if '.map(|value| value == "1")' not in text:
        problems.append(f'{FAIL_CLOSED_ENV} is not compared to "1"')
    if text.count(FAIL_ERR) != 1:
        problems.append(f'{FAIL_ERR} must appear exactly once (found {text.count(FAIL_ERR)})')
    if text.count(PREP_MARK) != 1:
        problems.append('mapping-lease CIB1-DIAG-CROSVM-MAPPREP diagnostic is missing')
    if 'fn try_map_to_prepared_region(\n' not in text:
        problems.append('try_map_to_prepared_region signature changed')
    if '-> Option<VmMemoryResponse>' not in text:
        problems.append('try_map_to_prepared_region no longer returns Option<VmMemoryResponse>')

    if PREP_MARK in text and FAIL_ERR in text and REGRESSION_TAIL in text:
        prep_at = text.index(PREP_MARK)
        err_at = text.index(FAIL_ERR)
        none_at = text.index(REGRESSION_TAIL, err_at)
        if not (prep_at < err_at < none_at):
            problems.append('fail-closed error does not precede the unconditional return None')
        # REGRESSION_TAIL itself starts with the default `return None;`, which keeps the
        # runtime memslot fallback reachable when the env var is unset.
        if text.count(REGRESSION_TAIL) != 1:
            problems.append('default return None / fallback tail is missing or duplicated')
        start = text.rfind('        None => {', 0, prep_at)
        if start < 0:
            problems.append('fail-closed block is not inside the mapped_regions None arm')
        else:
            if block_balanced(text, '        None => {', REGRESSION_TAIL) != 0:
                problems.append('None arm is brace-unbalanced')

    return problems


gpu_text = GPU.read_text(encoding='utf-8', errors='replace').replace('\r\n', '\n')
vmc_text = VMC.read_text(encoding='utf-8', errors='replace').replace('\r\n', '\n')

gpu_problems = analyze_gpu(gpu_text)
vmc_problems = analyze_vmc(vmc_text)
problems = gpu_problems + vmc_problems
assert not problems, 'invariants violated:\n  ' + '\n  '.join(problems)

# Negative control 1: restore the original vulkano branch and require detection.
start = gpu_text.index('        } else if cfg!(feature = "vulkano") {')
end = gpu_text.index('\n        }', gpu_text.index(DISABLED_WARN)) + len('\n        }\n')
regressed_gpu = gpu_text[:start] + OLD_VULKANO + gpu_text[end:]
regressed = analyze_gpu(regressed_gpu)
assert regressed, 'negative control failed: the original vulkano branch was not detected'
assert any('debug!' in p or 'unpatched vulkano' in p for p in regressed), regressed

# Negative control 2: drop the fail-closed gate and require detection.
gate_at = vmc_text.index(FAIL_READ)
gate_start = vmc_text.rfind('            if ', 0, gate_at)
gate_end = vmc_text.index('            return None;', gate_at)
regressed_vmc = vmc_text[:gate_start] + vmc_text[gate_end:]
regressed = analyze_vmc(regressed_vmc)
assert regressed, 'negative control failed: the missing fail-closed gate was not detected'
assert any(FAIL_CLOSED_ENV in p for p in regressed), regressed

# Idempotency: a second patch run must succeed and leave both files byte-identical.
patch_script = pathlib.Path(__file__).with_name('patch_crosvm_allow_fixed_blob_vulkano.py')
idempotent = None
if patch_script.is_file() and sys.executable:
    before_gpu = GPU.read_bytes()
    before_vmc = VMC.read_bytes()
    proc = subprocess.run([sys.executable, '-I', str(patch_script), str(root)],
                          capture_output=True, text=True)
    assert proc.returncode == 0, f'patch re-run failed:\n{proc.stdout}\n{proc.stderr}'
    assert GPU.read_bytes() == before_gpu, 're-running the patch changed gpu.rs'
    assert VMC.read_bytes() == before_vmc, 're-running the patch changed vm_control/src/lib.rs'
    assert 'skip' in proc.stdout, f'patch re-run did not report a no-op:\n{proc.stdout}'
    idempotent = 'second patch run is a byte-identical no-op'

report_lines = [
    f'PASS {ALLOW_ENV} is read once and named once in the keep-on warn',
    'PASS vulkano default path clears fixed_blob_mapping and warns (debug! gone)',
    'PASS vfio-passthrough branch unchanged (debug!, same message)',
    'PASS only the vfio and vulkano branches clear fixed_blob_mapping',
    f'PASS {FAIL_CLOSED_ENV}=1 returns ENOSPC before the unconditional return None',
    'PASS unset/other values still return None (runtime memslot fallback preserved)',
    'PASS both changed regions are brace-balanced (comments and strings ignored)',
    'PASS negative control: original vulkano branch and missing gate are detected',
]
if idempotent:
    report_lines.append(f'PASS idempotency: {idempotent}')
report = '\n'.join(report_lines) + '\n'
print(report, end='')
if out:
    out.mkdir(parents=True, exist_ok=True)
    (out / 'crosvm-allow-fixed-blob-tests.txt').write_text(report)
