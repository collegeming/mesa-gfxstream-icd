#include "native_backing_lease.h"
#include <cassert>
#include <iostream>
using namespace gfxstream::host::native;
static LeaseDescriptor descriptor() {
    LeaseDescriptor d{}; d.size = 64; d.version = 1;
    d.offered = kDetachMask; d.required = kDetachMask;
    std::memcpy(d.contractId, kContractId, 16); return d;
}
static bool parse(Offer& offer, LeaseDescriptor& d) {
    return offer.parse(kLeaseParam, reinterpret_cast<uintptr_t>(&d));
}
int main() {
    assert(!publicationReady());
    { Initialization init; assert(init.started()); Offer absent; init.success(absent); }
    assert(!publicationReady()); // new host, old VMM
    { std::lock_guard<std::mutex> lock(Session::get().lifecycle); Session::get().stopped(); }
    auto d = descriptor();
    { Initialization init; assert(init.started()); Offer offer; assert(parse(offer, d)); }
    assert(d.accepted == 0 && d.epoch == 0 && !publicationReady()); // init failure
    for (unsigned error = 0; error != 9; ++error) {
        auto invalid = descriptor();
        if (error == 0) invalid.size = 8;
        if (error == 1) invalid.version = 2;
        if (error == 2) invalid.offered = 7;
        if (error == 3) invalid.required = 0;
        if (error == 4) invalid.offered |= 16;
        if (error == 5) invalid.hostSupported = 15;
        if (error == 6) invalid.accepted = 15;
        if (error == 7) invalid.epoch = 1;
        if (error == 8) invalid.contractId[15] = 2;
        Offer offer; assert(!parse(offer, invalid)); assert(!offer.valid());
    }
    { Offer offer; assert(!offer.parse(kLeaseParam, 0)); }
    { Offer offer; auto duplicate = descriptor(); assert(parse(offer, duplicate)); assert(!parse(offer, duplicate)); }
    d = descriptor();
    uint64_t oldEpoch;
    { Initialization init; assert(init.started()); Offer offer; assert(parse(offer, d)); init.success(offer); oldEpoch = d.epoch; }
    assert(d.accepted == kDetachMask && d.hostSupported == kDetachMask && oldEpoch);
    assert(Session::get().ready(oldEpoch));
    { Initialization duplicateInit; assert(!duplicateInit.started()); }
    {
        std::lock_guard<std::mutex> lock(Session::get().lifecycle);
        Session::get().revoke();
        assert(!Session::get().ready(oldEpoch));
        Session::get().stopped();
    }
    d = descriptor();
    { Initialization init; assert(init.started()); Offer offer; assert(parse(offer, d)); init.success(offer); }
    assert(d.epoch != oldEpoch && !Session::get().ready(oldEpoch));
    assert(Session::get().ready(d.epoch));
    { std::lock_guard<std::mutex> lock(Session::get().lifecycle); Session::get().stopped(); }
    Reclaimer::get().drain();
    std::cout << "PASS: exact64byte production Offer/Initialization/Session; absent/invalid/duplicate/init-failure no ack, valid success ack, reset revoke, stale epoch rejection\n";
}
