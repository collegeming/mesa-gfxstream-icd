#!/usr/bin/env python3
"""Assert the Vulkano gralloc gate invariants on a patched crosvm tree.

Run after patch_crosvm_vulkano_gralloc_gate.py.

Checks in `src/crosvm/sys/linux.rs`:
  1. the gated `let flags = if vulkano_gralloc_enabled { ... } else { ... };` block exists exactly
     once,
  2. the default (else) branch still calls `RutabagaGrallocBackendFlags::new().disable_vulkano()`,
  3. the env-gated (then) branch does not call `disable_vulkano()`,
  4. the single `base::warn!` log line names `CROSVM_ENABLE_VULKANO_GRALLOC` and states which
     backend was chosen,
  5. the replacement is unique: the unpatched two-line call site is gone, `disable_vulkano()`
     appears exactly once in the file, and `.context("failed to create gralloc")` appears exactly
     once and still consumes the gated `flags`,
  6. the changed region is brace-balanced after comments and string literals are ignored
     (the log line's `{}` placeholders must not be counted).

It also re-injects the unpatched two lines over the gate and asserts the checks fire on that
text, so the test is proven to catch the defect it exists for.
"""
import pathlib
import sys

root = pathlib.Path(sys.argv[1]).resolve()
out = pathlib.Path(sys.argv[2]).resolve() if len(sys.argv) > 2 else None

ENV_VAR = 'CROSVM_ENABLE_VULKANO_GRALLOC'
ENV_READ = f'std::env::var("{ENV_VAR}")'
GATE_IF = 'let flags = if vulkano_gralloc_enabled {'
BLOCK_START = '    // RutabagaGrallocBackendFlags::new() already includes'
CONTEXT = '.context("failed to create gralloc")?;'
UNPATCHED = (
    '    let flags = RutabagaGrallocBackendFlags::new().disable_vulkano();\n'
    '    let gralloc = RutabagaGralloc::new(flags).context("failed to create gralloc")?;\n'
)
DEFAULT_FLAGS = 'RutabagaGrallocBackendFlags::new().disable_vulkano()'

linux_text = (root / 'src/crosvm/sys/linux.rs').read_text(
    encoding='utf-8', errors='replace').replace('\r\n', '\n')


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


def analyze(text):
    problems = []

    if 'use base::*;' not in text:
        problems.append('base glob import is gone; base::warn! would not resolve')
    if text.count(UNPATCHED) != 0:
        problems.append('unpatched call site (flags = new().disable_vulkano()) is still present')
    if text.count(ENV_READ) != 1:
        problems.append(f'{ENV_READ} must appear exactly once (found {text.count(ENV_READ)})')
    if text.count(DEFAULT_FLAGS) != 1:
        problems.append(
            f'{DEFAULT_FLAGS} must appear exactly once (found {text.count(DEFAULT_FLAGS)})')
    if text.count(CONTEXT) != 1:
        problems.append(f'{CONTEXT} must appear exactly once (found {text.count(CONTEXT)})')
    if text.count(BLOCK_START) != 1:
        problems.append(f'gate comment must appear exactly once (found {text.count(BLOCK_START)})')
    if text.count(GATE_IF) != 1:
        problems.append(f'gated branch {GATE_IF!r} must appear exactly once '
                        f'(found {text.count(GATE_IF)})')
        return problems
    if text.count(BLOCK_START) != 1:
        return problems

    gate_start = text.index(GATE_IF)
    else_at = text.find('} else {', gate_start)
    if else_at < 0:
        problems.append('gated branch has no else (default) arm')
        return problems
    close_at = text.find('    };', else_at)
    if close_at < 0:
        problems.append('gated branch is not closed by "    };"')
        return problems
    then_branch = text[gate_start + len(GATE_IF):else_at]
    else_branch = text[else_at + len('} else {'):close_at]

    if 'RutabagaGrallocBackendFlags::new()' not in then_branch:
        problems.append('env-gated branch does not call RutabagaGrallocBackendFlags::new()')
    if 'disable_vulkano' in then_branch:
        problems.append('env-gated branch must not call disable_vulkano()')
    if DEFAULT_FLAGS not in else_branch:
        problems.append('default branch no longer calls disable_vulkano()')

    block_start = text.index(BLOCK_START)
    block_end = text.index(CONTEXT, block_start) + len(CONTEXT)
    block = text[block_start:block_end]

    if text.index('let gralloc = RutabagaGralloc::new(flags)', block_start) > block_end:
        problems.append('gralloc is not built from the gated flags variable')
    if block.count('base::warn!') != 1:
        problems.append(f'the gate must log exactly one line (found {block.count("base::warn!")})')
    else:
        log_start = block.index('base::warn!')
        log_end = block.index(');', log_start)
        log_line = block[log_start:log_end]
        if ENV_VAR not in log_line:
            problems.append(f'log line does not name {ENV_VAR}')
        if 'ENABLED' not in log_line or 'DISABLED (default)' not in log_line:
            problems.append('log line does not state both possible backend choices')

    if text.find(ENV_READ, block_start) > block_end:
        problems.append(f'{ENV_VAR} is not read inside the gate block')

    depth = 0
    for line in code_only(block).splitlines():
        depth += line.count('{') - line.count('}')
    if depth != 0:
        problems.append(f'gate block is brace-unbalanced (depth {depth})')

    return problems


problems = analyze(linux_text)
assert not problems, 'invariants violated:\n  ' + '\n  '.join(problems)

# Negative control: put the original two lines back in place of the gate and require a failure.
gate_start = linux_text.index(BLOCK_START)
gate_end = linux_text.index(CONTEXT, gate_start) + len(CONTEXT) + 1
regressed = linux_text[:gate_start] + UNPATCHED + linux_text[gate_end:]
regressed_problems = analyze(regressed)
assert regressed_problems, 'negative control failed: the unpatched call site was not detected'
assert any('unpatched call site' in p for p in regressed_problems), regressed_problems
assert any('gated branch' in p for p in regressed_problems), regressed_problems

report = (
    'PASS gated flags branch present with env-enabled and default arms\n'
    'PASS default arm still calls RutabagaGrallocBackendFlags::new().disable_vulkano()\n'
    'PASS env-gated arm does not call disable_vulkano()\n'
    'PASS one base::warn! line names CROSVM_ENABLE_VULKANO_GRALLOC and the chosen backend\n'
    'PASS unpatched call site absent; disable_vulkano() and the gralloc context are unique\n'
    'PASS gate block is brace-balanced (comments and string literals ignored)\n'
    'PASS negative control: re-injecting the unpatched two lines fails the checks\n'
)
print(report, end='')
if out:
    out.mkdir(parents=True, exist_ok=True)
    (out / 'crosvm-vulkano-gralloc-tests.txt').write_text(report)
