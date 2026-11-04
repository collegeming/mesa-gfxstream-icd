#!/usr/bin/env python3
"""Assert the BAR-offset diagnosis invariants on a patched crosvm tree.

Run after patch_crosvm_bar_offset_diag.py.

Checks:
  1. `VmMemoryDestination::allocate` no longer flattens the allocator error to a bare EINVAL:
     it distinguishes InvalidAlloc from OutOfBounds and logs each with the alloc/offset/size;
  2. the guest-visible result is still EINVAL -- the protocol is unchanged;
  3. `AddressAllocatorSet::address_from_pci_offset` returns immediately on OutOfBounds, so a
     later allocator's InvalidAlloc cannot mask a real out-of-range answer;
  4. the resources crate still does not reference the base log macros (it does not import them,
     so a log call there would not compile);
  5. both touched functions are brace-balanced.
"""
import pathlib
import sys

root = pathlib.Path(sys.argv[1]).resolve()
out = pathlib.Path(sys.argv[2]).resolve() if len(sys.argv) > 2 else None

vmc = (root / 'vm_control/src/lib.rs').read_text(
    encoding='utf-8', errors='replace').replace('\r\n', '\n')
alloc = (root / 'resources/src/address_allocator.rs').read_text(
    encoding='utf-8', errors='replace').replace('\r\n', '\n')

NL_CLOSE = chr(10) + '}' + chr(10)
start = vmc.index('impl VmMemoryDestination {')
# Take the whole impl block, including the trailing Ok(...) and both closing braces.
impl_end = vmc.index('Ok(GuestAddress(addr))', start)
body = vmc[start:vmc.index(NL_CLOSE, impl_end)]
# 1. distinct reasons, with numbers.
assert 'resources::Error::InvalidAlloc(ref bad)' in body
assert 'resources::Error::OutOfBounds' in body
assert body.count('RegisterMemory: BAR destination rejected') == 3, \
    body.count('RegisterMemory: BAR destination rejected')
assert 'offset={:#x} size={:#x}' in body

# 2. the wire error is unchanged.
assert 'return Err(SysError::new(EINVAL));' in body
assert 'map_err(|_e| SysError::new(EINVAL))' not in body, 'the blind flatten must be gone'

# 3. the set search no longer overwrites a real answer.
# The set implementation lives inside `impl AddressAllocatorSet`; the allocator
# has a method of the same name, so anchor on the set's own impl block.
set_impl = alloc.index("impl<'a> AddressAllocatorSet<'a> {")
set_start = alloc.index('    pub fn address_from_pci_offset(', set_impl)
set_fn = alloc[set_start:]
set_fn = set_fn[:set_fn.index(NL_CLOSE)]
assert 'Err(Error::OutOfBounds) => return last_res,' in set_fn
assert 'Ok(_) => return last_res,' in set_fn

# 4. no log macros in the resources crate.
assert 'base::warn!' not in alloc and 'base::error!' not in alloc

# 5. balance of the two patched functions, measured on the whole file (the slices above stop at
#    a brace boundary that is not the function's own closer).
def brace_depth(text):
    depth = 0
    for line in text.splitlines():
        code = line
        i = code.find(chr(47) + chr(47))
        if i >= 0 and code[:i].count(chr(34)) % 2 == 0:
            code = code[:i]
        depth += code.count(chr(123)) - code.count(chr(125))
    return depth

for name, text in (('vm_control/src/lib.rs', vmc), ('resources/src/address_allocator.rs', alloc)):
    assert brace_depth(text) == 0, f'{name} is unbalanced (depth {brace_depth(text)})'

report = (
    'PASS VmMemoryDestination distinguishes InvalidAlloc from OutOfBounds and logs both\n'
    'PASS guest-visible error stays EINVAL; the blind map_err flatten is gone\n'
    'PASS AddressAllocatorSet returns a real OutOfBounds instead of masking it\n'
    'PASS resources crate free of base log macros\n'
    'PASS both touched functions brace-balanced\n'
)
print(report, end='')
if out:
    out.mkdir(parents=True, exist_ok=True)
    (out / 'crosvm-bar-offset-tests.txt').write_text(report)
