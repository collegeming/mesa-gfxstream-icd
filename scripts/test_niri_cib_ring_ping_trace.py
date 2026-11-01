#!/usr/bin/env python3
"""Source invariants for bounded ASG ring ping/park/wakeup diagnostics."""
import argparse
from pathlib import Path
p = argparse.ArgumentParser(description=__doc__)
p.add_argument('root', type=Path)
p.add_argument('workflow', type=Path, nargs='?')
args = p.parse_args()
root = args.root.resolve()

def read(rel):
    return (root / rel).read_text()

frontend = read('host/virtio_gpu_frontend.cpp')
asg = read('host/address_space/address_space_graphics.cpp')
if args.workflow:
    workflow = args.workflow.resolve().read_text()
else:
    candidates = [
        root / '.github/workflows/build-droidvm-mali-host.yml',
        root.parent / '.github/workflows/build-droidvm-mali-host.yml',
        root.parent / 'ci/.github/workflows/build-droidvm-mali-host.yml',
    ]
    workflow_path = next((x for x in candidates if x.is_file()), None)
    assert workflow_path is not None, 'CI workflow not found'
    workflow = workflow_path.read_text()

cmd = frontend[frontend.index('int VirtioGpuFrontend::processAddressSpaceCommand('):
                frontend.index('int VirtioGpuFrontend::processCommand(')]
assert cmd.count('CIB1-DIAG-ASG-CMD') == 1
assert cmd.count('CIB1-DIAG-ASG-PING-RESULT') == 1
for token in ['GFXSTREAM_CONTEXT_CREATE', 'GFXSTREAM_CONTEXT_PING',
              'GFXSTREAM_CONTEXT_PING_WITH_RESPONSE',
              'CIB1-DIAG-ASG-CMD: ctx=%u opcode=%u resource=%u context-found=%d',
              'CIB1-DIAG-ASG-PING-RESULT: ctx=%u resource=%u ret=%d',
              'const int cibPingRet = context.PingAddressSpaceGraphicsInstance(',
              'return cibPingRet;', 'GFXSTREAM_NIRI_IMAGE_TRACE']:
    assert token in cmd, token
assert cmd.count('cmdDiagnostics.fetch_add(1, std::memory_order_relaxed) < 32') == 1
assert cmd.count('pingDiagnostics.fetch_add(1, std::memory_order_relaxed) < 32') == 1

notify = asg[asg.index('void AddressSpaceGraphicsContext::perform('):
             asg.index('AsgOnUnavailableReadStatus AddressSpaceGraphicsContext::onUnavailableRead()')]
assert notify.count('CIB1-DIAG-ASG-NOTIFY') == 1
assert 'CIB1-DIAG-ASG-NOTIFY: count=%llu sent=%d' in notify
assert 'const bool cibSent = mConsumerMessages.trySend(ConsumerCommand::Wakeup);' in notify
assert 'asgNotifyCount().load(' in notify
assert notify.count('notifyDiagnostics.fetch_add(1, std::memory_order_relaxed) < 32') == 1

park = asg[asg.index('AsgOnUnavailableReadStatus AddressSpaceGraphicsContext::onUnavailableRead()'):
           asg.index('AddressSpaceDeviceType AddressSpaceGraphicsContext::getDeviceType()')]
assert park.count('CIB1-DIAG-ASG-PARK') == 1
assert park.count('CIB1-DIAG-ASG-WAKE') == 1
for token in ['CIB1-DIAG-ASG-PARK: write=%u read=%u state=%u guest-write=%u host-consumed=%u',
              'CIB1-DIAG-ASG-WAKE: cmd=%d write=%u read=%u state=%u guest-write=%u host-consumed=%u',
              'reinterpret_cast<ring_buffer*>(mRingAllocation.buffer)',
              'reinterpret_cast<asg_ring_config*>(toHost->config)',
              'toHost->write_pos', 'toHost->read_pos', 'toHost->state',
              'ringConfig->guest_write_pos', 'ringConfig->host_consumed_pos',
              'mConsumerMessages.receive(&cmd);']:
    assert token in park, token
assert park.index('CIB1-DIAG-ASG-PARK') < park.index('mConsumerMessages.receive(&cmd);')
assert park.index('mConsumerMessages.receive(&cmd);') < park.index('CIB1-DIAG-ASG-WAKE')
assert park.count('parkDiagnostics.fetch_add(1, std::memory_order_relaxed) < 32') == 1
assert park.count('wakeDiagnostics.fetch_add(1, std::memory_order_relaxed) < 32') == 1

# Existing AHB fix and CIB behavior markers remain.
for token in ['CIB1-DIAG-BUF-VK-AHB-NO-EXTERNAL', 'CIB1-DIAG-BUF-VK-AHB-EXPORT-SKIPPED']:
    assert token in read('host/vulkan/vk_common_operations.cpp'), token

assert workflow.index('patch_niri_cib_ahb_buffer_fix.py') < workflow.index('patch_niri_cib_ring_ping_trace.py')
assert workflow.index('test_niri_cib_ring_ping_trace.py') < workflow.index('cp ci/scripts/*niri_cib_ring_ping_trace* out/')
assert "scripts/*niri_cib_ring_ping_trace*" in workflow
assert '"niri_cib_ring_ping_trace"' in workflow
assert 'diagnostic/niri-cib-ring-ping-trace' in workflow

print('PASS: bounded ASG command/ping/notify/park/wake diagnostics present; original calls, trySend and receive behavior retained')
