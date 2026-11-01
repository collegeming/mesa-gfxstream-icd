#!/usr/bin/env python3
"""Bounded ASG command/ping/notify/park/wakeup diagnostics; no behavior changes."""
import argparse
from pathlib import Path
p = argparse.ArgumentParser(description=__doc__)
p.add_argument('root', type=Path)
root = p.parse_args().root.resolve()
def edit(rel, old, new):
    path = root/rel
    text = path.read_text()
    if text.count(old) != 1:
        raise SystemExit(f'{rel}: expected one anchor ({text.count(old)}): {old[:100]}')
    path.write_text(text.replace(old, new))

frontend = 'host/virtio_gpu_frontend.cpp'
edit(frontend, '''    auto contextIt = mContexts.find(ctxId);
    if (contextIt == mContexts.end()) {''', '''    const bool cibAsgDiag =
        gfxstream::base::getEnvironmentVariable("GFXSTREAM_NIRI_IMAGE_TRACE") == "1";
    auto contextIt = mContexts.find(ctxId);
    if (cibAsgDiag && (header.opCode == GFXSTREAM_CONTEXT_CREATE ||
                       header.opCode == GFXSTREAM_CONTEXT_PING ||
                       header.opCode == GFXSTREAM_CONTEXT_PING_WITH_RESPONSE)) {
        static std::atomic<uint32_t> cmdDiagnostics{0};
        if (cmdDiagnostics.fetch_add(1, std::memory_order_relaxed) < 32) {
            uint32_t cibResource = 0;
            if (header.opCode == GFXSTREAM_CONTEXT_CREATE) {
                gfxstream::gfxstreamContextCreate cibCreate = {};
                if (SafeDecode(cibCreate, cmd_buffer, cmd_buffer_size))
                    cibResource = cibCreate.resourceId;
            } else {
                gfxstream::gfxstreamContextPing cibPing = {};
                if (SafeDecode(cibPing, cmd_buffer, cmd_buffer_size))
                    cibResource = cibPing.resourceId;
            }
            GFXSTREAM_INFO("CIB1-DIAG-ASG-CMD: ctx=%u opcode=%u resource=%u context-found=%d",
                           (unsigned)ctxId, (unsigned)header.opCode, cibResource,
                           contextIt != mContexts.end());
        }
    }
    if (contextIt == mContexts.end()) {''')
edit(frontend, '''            return context.PingAddressSpaceGraphicsInstance(get_gfxstream_address_space_ops(),
                                                            contextPing.resourceId);''', '''            const int cibPingRet = context.PingAddressSpaceGraphicsInstance(
                get_gfxstream_address_space_ops(), contextPing.resourceId);
            if (cibAsgDiag) {
                static std::atomic<uint32_t> pingDiagnostics{0};
                if (pingDiagnostics.fetch_add(1, std::memory_order_relaxed) < 32) {
                    GFXSTREAM_INFO("CIB1-DIAG-ASG-PING-RESULT: ctx=%u resource=%u ret=%d",
                                   (unsigned)ctxId, contextPing.resourceId, cibPingRet);
                }
            }
            return cibPingRet;''')

asg = 'host/address_space/address_space_graphics.cpp'
edit(asg, '''#include <atomic>
#include <stdlib.h>''', '''#include <atomic>
#include <cstdlib>
#include <cstring>
#include <stdlib.h>''')
edit(asg, '''        asgNotifyCount().fetch_add(1, std::memory_order_relaxed);
        mConsumerMessages.trySend(ConsumerCommand::Wakeup);
        info->metadata = 0;''', '''        asgNotifyCount().fetch_add(1, std::memory_order_relaxed);
        {
            const bool cibSent = mConsumerMessages.trySend(ConsumerCommand::Wakeup);
            const char* trace = std::getenv("GFXSTREAM_NIRI_IMAGE_TRACE");
            if (trace && std::strcmp(trace, "1") == 0) {
                static std::atomic<uint32_t> notifyDiagnostics{0};
                if (notifyDiagnostics.fetch_add(1, std::memory_order_relaxed) < 32) {
                    GFXSTREAM_INFO("CIB1-DIAG-ASG-NOTIFY: count=%llu sent=%d",
                                   (unsigned long long)asgNotifyCount().load(
                                       std::memory_order_relaxed),
                                   cibSent);
                }
            }
        }
        info->metadata = 0;''')
edit(asg, '''AsgOnUnavailableReadStatus AddressSpaceGraphicsContext::onUnavailableRead() {
    ConsumerCommand cmd;
    mConsumerMessages.receive(&cmd);
    switch (cmd) {''', '''AsgOnUnavailableReadStatus AddressSpaceGraphicsContext::onUnavailableRead() {
    const char* trace = std::getenv("GFXSTREAM_NIRI_IMAGE_TRACE");
    const bool diag = trace && std::strcmp(trace, "1") == 0;
    uint32_t ringWrite = 0, ringRead = 0, ringState = 0, ringGuestWrite = 0, ringHostConsumed = 0;
    if (diag && mRingAllocation.buffer) {
        auto* toHost = reinterpret_cast<ring_buffer*>(mRingAllocation.buffer);
        auto* ringConfig = reinterpret_cast<asg_ring_config*>(toHost->config);
        ringWrite = toHost->write_pos;
        ringRead = toHost->read_pos;
        ringState = toHost->state;
        ringGuestWrite = ringConfig->guest_write_pos;
        ringHostConsumed = ringConfig->host_consumed_pos;
    }
    if (diag) {
        static std::atomic<uint32_t> parkDiagnostics{0};
        if (parkDiagnostics.fetch_add(1, std::memory_order_relaxed) < 32) {
            GFXSTREAM_INFO("CIB1-DIAG-ASG-PARK: write=%u read=%u state=%u guest-write=%u "
                           "host-consumed=%u",
                           ringWrite, ringRead, ringState, ringGuestWrite, ringHostConsumed);
        }
    }
    ConsumerCommand cmd;
    mConsumerMessages.receive(&cmd);
    if (diag) {
        static std::atomic<uint32_t> wakeDiagnostics{0};
        if (wakeDiagnostics.fetch_add(1, std::memory_order_relaxed) < 32) {
            GFXSTREAM_INFO("CIB1-DIAG-ASG-WAKE: cmd=%d write=%u read=%u state=%u guest-write=%u "
                           "host-consumed=%u",
                           (int)cmd, ringWrite, ringRead, ringState, ringGuestWrite,
                           ringHostConsumed);
        }
    }
    switch (cmd) {''')
print('Added bounded ASG command/ping/notify/park/wakeup diagnostics')
