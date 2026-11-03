#!/usr/bin/env python3
"""Superseded by the emulation-aware coherent contract; verifies it and changes nothing.

This script used to blanket-exempt every guest-owned import (ColorBuffer/Buffer alias or an
explicit fd import) from the HOST_COHERENT downgrade reject. That exemption was wrong in the
same way deleting the guard is wrong: it let the guest believe in coherency nobody provided,
and it said nothing about whether the host could maintain the cache lines. It also made the
reject untestable, because "guest-owned" covers allocations the host never mapped.

The replacement is a precise waiver in patch_droidvm_mali_host.py: an allocation is accepted
only when VulkanDisableCoherentMemoryAndEmulate is enabled, the physical host type is
HOST_CACHED, and the allocation has a shape whose ranges the decoder tracks. Everything else
stays fail-closed. This script now only proves the replacement is in place, so a pipeline that
still expects the old behaviour fails loudly instead of silently losing the guard.
"""
import argparse
from pathlib import Path

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('root', type=Path)
root = parser.parse_args().root.resolve()

path = root / 'host/vulkan/vk_decoder_global_state.cpp'
text = path.read_text(encoding='utf-8')

REQUIRED = [
    'emulationCoversThisAllocation(',
    'validateHostCoherentContractEmulated(',
    'COHERENT-EMULATION-ALLOW',
    'COHERENT-DOWNGRADE-REJECT',
]
missing = [marker for marker in REQUIRED if marker not in text]
if missing:
    raise SystemExit(f'emulation-aware coherent contract missing: {missing}')
if 'COHERENT-DOWNGRADE-ALLOW-IMPORT' in text:
    raise SystemExit('blanket guest-owned import exemption still present; it must not be')
print('guest-owned import exemption superseded by the bounded emulation waiver; no change')
