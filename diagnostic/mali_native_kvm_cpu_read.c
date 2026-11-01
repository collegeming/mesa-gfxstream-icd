#define _GNU_SOURCE
#include <stdint.h>
#include <stdbool.h>
#include <string.h>

#define CODE_GPA UINT64_C(0x100000)
#define DATA_GPA UINT64_C(0x200000)
#define MMIO_GPA UINT64_C(0x300000)
#define PAGE_BYTES 4096u
#define MARKER UINT32_C(0x13579bdf)
/* Assembled independently and checked against these bytes in Actions. */
static const uint32_t guest_code[] = {
    0xb9400002, /* ldr w2, [x0] */
    0xf2f4ab42, /* movk x2, #0xa55a, lsl #48 */
    0xf9000022, /* str x2, [x1] */
    0x00000000, /* udf #0: defensive fallthrough, never resume after MMIO */
};

/* Pure decoder: synthetic fixtures test only this parser, not KVM or memory. */
static bool decode_mmio(uint32_t reason, uint64_t address, uint32_t length,
                        uint8_t is_write, const uint8_t data[8], bool native,
                        uint32_t *low) {
    if (reason != 6 || address != MMIO_GPA || length != 8 || is_write != 1)
        return false;
    uint64_t value = 0;
    for (unsigned i = 0; i < 8; ++i) value |= (uint64_t)data[i] << (8 * i);
    if ((value >> 32) != UINT64_C(0xa55a0000)) return false;
    *low = (uint32_t)value;
    return native || *low == MARKER;
}

#ifndef CPU_READ_ORACLE_ONLY
#include <vulkan/vulkan.h>
#include <errno.h>
#include <fcntl.h>
#include <inttypes.h>
#include <linux/kvm.h>
#include <stddef.h>
#include <pthread.h>
#include <signal.h>
#include <stdio.h>
#include <sys/ioctl.h>
#include <sys/mman.h>
#include <sys/utsname.h>
#include <time.h>
#include <unistd.h>

#define EXPECTED_DEVICE "Mali-G2-Ultra-NX MC16"
#define MEMORY_TYPE_INDEX 8u
#define LOG(...) do { printf(__VA_ARGS__); putchar('\n'); fflush(stdout); } while (0)
#define CORE_ID(member) (KVM_REG_ARM64 | KVM_REG_SIZE_U64 | KVM_REG_ARM_CORE | \
                         (offsetof(struct kvm_regs, member) / sizeof(uint32_t)))
#define SCTLR_ID (KVM_REG_ARM64 | KVM_REG_SIZE_U64 | KVM_REG_ARM64_SYSREG | \
                  (3u << KVM_REG_ARM64_SYSREG_OP0_SHIFT) | \
                  (1u << KVM_REG_ARM64_SYSREG_CRN_SHIFT))
#define SCTLR_CLEAR ((UINT64_C(1)<<0) | (UINT64_C(1)<<2) | \
                     (UINT64_C(1)<<12) | (UINT64_C(1)<<25))
_Static_assert(KVM_API_VERSION == 12, "API12");
_Static_assert(KVM_GET_API_VERSION == 0xae00 && KVM_CREATE_VM == 0xae01, "KVM ABI");
_Static_assert(KVM_CHECK_EXTENSION == 0xae03 && KVM_GET_VCPU_MMAP_SIZE == 0xae04, "KVM ABI");
_Static_assert(KVM_CREATE_VCPU == 0xae41 && KVM_RUN == 0xae80, "KVM ABI");
_Static_assert(KVM_SET_USER_MEMORY_REGION == 0x4020ae46, "region ABI");
_Static_assert(sizeof(struct kvm_userspace_memory_region) == 32, "region size");
_Static_assert(KVM_GET_ONE_REG == 0x4010aeab && KVM_SET_ONE_REG == 0x4010aeac, "oneReg ABI");
_Static_assert(KVM_ARM_PREFERRED_TARGET == 0x8020aeaf && KVM_ARM_VCPU_INIT == 0x4020aeae, "ARM ABI");
_Static_assert(sizeof(struct kvm_vcpu_init) == 32 && sizeof(struct kvm_one_reg) == 16, "ARM structures");
_Static_assert(CORE_ID(regs.regs[0]) == UINT64_C(0x6030000000100000), "X0 ID");
_Static_assert(CORE_ID(regs.regs[1]) == UINT64_C(0x6030000000100002), "X1 ID");
_Static_assert(CORE_ID(regs.pc) == UINT64_C(0x6030000000100040), "PC ID");
_Static_assert(CORE_ID(regs.pstate) == UINT64_C(0x6030000000100042), "PSTATE ID");
_Static_assert(SCTLR_ID == UINT64_C(0x603000000013c080), "SCTLR_EL1 ID");
_Static_assert(KVM_CAP_USER_MEMORY == 3 && KVM_CAP_ONE_REG == 70 && KVM_CAP_READONLY_MEM == 81 &&
               KVM_CAP_IMMEDIATE_EXIT == 136 && KVM_MEM_READONLY == 2, "capability ABI");
_Static_assert(KVM_EXIT_MMIO == 6 && offsetof(struct kvm_run, immediate_exit) == 1,
               "run ABI");

/* Only scalar metadata is extracted from proc text, never mapped payload. */
struct vma {
    uintptr_t start, end;
    uint64_t offset;
    char permissions[5];
};

static bool parse_vma(const char *line, struct vma *vma) {
    return sscanf(line, "%" SCNxPTR "-%" SCNxPTR " %4s %" SCNx64,
                  &vma->start, &vma->end, vma->permissions, &vma->offset) == 4 &&
           vma->start < vma->end;
}

static bool contains(const struct vma *vma, uintptr_t pointer) {
    return vma->start <= pointer && pointer < vma->end;
}

static void print_vma(const char *label, const struct vma *vma, uintptr_t pointer,
                      uint64_t requested_size) {
    LOG("%s start=0x%" PRIxPTR " end=0x%" PRIxPTR " permissions=%s offset=0x%" PRIx64
        " pointerOffsetInVMA=%" PRIuPTR " requestedRangeWithinThisVMA=%u",
        label, vma->start, vma->end, vma->permissions, vma->offset,
        pointer - vma->start, requested_size <= vma->end - pointer);
}

static bool inspect_vma(uintptr_t pointer, uint64_t requested_size) {
    char line[4096];
    struct vma maps = {0}, smaps = {0}, current;
    bool maps_found = false, smaps_found = false;
    bool vmflags = false, kernel_page = false, mmu_page = false;
    FILE *file = fopen("/proc/self/maps", "re");
    if (!file) {
        LOG("RESULT FAIL maps open errno=%d", errno);
        return false;
    }
    while (fgets(line, sizeof(line), file)) {
        if (parse_vma(line, &current) && contains(&current, pointer)) {
            maps = current;
            maps_found = true;
            print_vma("MAPS_VMA", &maps, pointer, requested_size);
            break;
        }
    }
    bool maps_error = ferror(file) != 0;
    if (fclose(file) != 0) maps_error = true;
    if (!maps_found || maps_error) {
        LOG("RESULT FAIL maps matchingVMA=%u readError=%u", maps_found, maps_error);
        return false;
    }
    file = fopen("/proc/self/smaps", "re");
    if (!file) {
        LOG("RESULT FAIL smaps open errno=%d", errno);
        return false;
    }
    bool matching = false;
    while (fgets(line, sizeof(line), file)) {
        if (parse_vma(line, &current)) {
            if (matching) break;
            matching = contains(&current, pointer);
            if (matching) {
                smaps = current;
                smaps_found = true;
                print_vma("SMAPS_VMA", &smaps, pointer, requested_size);
            }
        } else if (matching) {
            if (!strncmp(line, "VmFlags:", 8)) {
                LOG("SMAPS %s", line);
                vmflags = true;
            } else if (!strncmp(line, "KernelPageSize:", 15)) {
                LOG("SMAPS %s", line);
                kernel_page = true;
            } else if (!strncmp(line, "MMUPageSize:", 12)) {
                LOG("SMAPS %s", line);
                mmu_page = true;
            }
        }
    }
    bool smaps_error = ferror(file) != 0;
    if (fclose(file) != 0) smaps_error = true;
    bool same = smaps_found && maps.start == smaps.start && maps.end == smaps.end &&
        maps.offset == smaps.offset && !strcmp(maps.permissions, smaps.permissions);
    LOG("VMA_METADATA matchingMapsSmaps=%u VmFlags=%u KernelPageSize=%u MMUPageSize=%u readError=%u",
        same, vmflags, kernel_page, mmu_page, smaps_error);
    LOG("LIMIT VMA boundaries are process metadata, NOT Vulkan allocation extent; proc snapshots are not atomic; VmFlags/page sizes do not prove backing identity, physical contiguity or coherence");
    bool covered = same && requested_size <= maps.end - pointer && requested_size <= smaps.end - pointer;
    bool writable = same && maps.permissions[0] == 'r' && maps.permissions[1] == 'w';
    LOG("VMA_REGISTER_GUARD requestedRangeCovered=%u readableWritable=%u; snapshot only, not PFN eligibility proof", covered, writable);
    return same && covered && writable && vmflags && kernel_page && mmu_page && !smaps_error;
}

/* The signal handler never touches Vulkan, logs, or performs cleanup. */
static volatile sig_atomic_t signal_seen;
static void interrupt_run(int signal_number) {
    (void)signal_number;
    signal_seen = 1;
}
struct watchdog {
    pthread_mutex_t mutex;
    pthread_cond_t cond;
    bool done;
    pthread_t runner;
    struct kvm_run *run;
    struct timespec deadline;
    int error;
    bool fired;
};
static void *watchdog_main(void *opaque) {
    struct watchdog *w = opaque;
    pthread_mutex_lock(&w->mutex);
    int error = 0;
    while (!w->done && !error)
        error = pthread_cond_timedwait(&w->cond, &w->mutex, &w->deadline);
    if (!w->done) {
        w->fired = true;
        __atomic_store_n(&w->run->immediate_exit, 1, __ATOMIC_RELEASE);
        w->error = pthread_kill(w->runner, SIGUSR1);
        if (error != ETIMEDOUT && !w->error) w->error = error;
    }
    pthread_mutex_unlock(&w->mutex);
    return NULL;
}
static bool reg_access(int vcpu, unsigned long op, uint64_t id, uint64_t *value) {
    struct kvm_one_reg reg = {.id = id, .addr = (uint64_t)(uintptr_t)value};
    errno = 0;
    int result = ioctl(vcpu, op, &reg);
    LOG("ONE_REG op=%s id=0x%" PRIx64 " result=%d errno=%d value=0x%" PRIx64,
        op == KVM_GET_ONE_REG ? "get" : "set", id, result, errno, *value);
    return result == 0;
}
static bool require_cap(int kvm, int cap) {
    errno = 0;
    int result = ioctl(kvm, KVM_CHECK_EXTENSION, cap);
    LOG("CAP id=%d result=%d errno=%d", cap, result, errno);
    return result > 0;
}
static bool close_logged(int fd, const char *label) {
    if (fd < 0) return true;
    errno = 0;
    int result = close(fd);
    LOG("CLOSE %s result=%d errno=%d; never retry/reuse fd", label, result, errno);
    return result == 0;
}

/* Returns only after the sole KVM_RUN returned and every KVM reference closed. */
static int run_read(uintptr_t data_address, bool native) {
    int status = 3, kvm = -1, vm = -1, vcpu = -1;
    bool registered[2] = {false, false};
    void *code = MAP_FAILED;
    struct kvm_run *run = MAP_FAILED;
    size_t run_size = 0;
    struct kvm_userspace_memory_region slots[2] = {
        {.slot=0, .flags=KVM_MEM_READONLY, .guest_phys_addr=CODE_GPA,
         .memory_size=PAGE_BYTES},
        {.slot=1, .flags=KVM_MEM_READONLY, .guest_phys_addr=DATA_GPA,
         .memory_size=PAGE_BYTES, .userspace_addr=(uint64_t)data_address}
    };
    code = mmap(NULL, PAGE_BYTES, PROT_READ | PROT_WRITE,
                MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
    if (code == MAP_FAILED) goto cleanup;
    memcpy(code, guest_code, sizeof(guest_code));
    __builtin___clear_cache((char *)code, (char *)code + sizeof(guest_code));
    if (mprotect(code, PAGE_BYTES, PROT_READ | PROT_EXEC)) goto cleanup;
    slots[0].userspace_addr = (uint64_t)(uintptr_t)code;
    kvm = open("/dev/kvm", O_RDWR | O_CLOEXEC);
    if (kvm < 0) { LOG("FAIL open /dev/kvm errno=%d", errno); goto cleanup; }
    if (ioctl(kvm, KVM_GET_API_VERSION, 0) != 12 ||
        !require_cap(kvm, KVM_CAP_USER_MEMORY) ||
        !require_cap(kvm, KVM_CAP_ONE_REG) ||
        !require_cap(kvm, KVM_CAP_IMMEDIATE_EXIT) ||
        !require_cap(kvm, KVM_CAP_READONLY_MEM)) goto cleanup;
    vm = ioctl(kvm, KVM_CREATE_VM, 0);
    if (vm < 0) goto cleanup;
    struct kvm_vcpu_init init = {0};
    if (ioctl(vm, KVM_ARM_PREFERRED_TARGET, &init)) goto cleanup;
    memset(init.features, 0, sizeof(init.features));
    vcpu = ioctl(vm, KVM_CREATE_VCPU, 0);
    if (vcpu < 0 || ioctl(vcpu, KVM_ARM_VCPU_INIT, &init)) goto cleanup;
    int bytes = ioctl(kvm, KVM_GET_VCPU_MMAP_SIZE, 0);
    if (bytes < (int)sizeof(struct kvm_run) || bytes > 1024*1024) goto cleanup;
    run_size = (size_t)bytes;
    run = mmap(NULL, run_size, PROT_READ | PROT_WRITE, MAP_SHARED, vcpu, 0);
    if (run == MAP_FAILED) goto cleanup;
    for (unsigned i=0; i<2; ++i) {
        LOG("REGISTER slot=%u GPA=0x%" PRIx64 " HVA=0x%" PRIx64 " bytes=4096 READONLY=1",
            i, (uint64_t)slots[i].guest_phys_addr, (uint64_t)slots[i].userspace_addr);
        if (ioctl(vm, KVM_SET_USER_MEMORY_REGION, &slots[i])) goto cleanup;
        registered[i] = true;
    }
    uint64_t x0=DATA_GPA, x1=MMIO_GPA, pc=CODE_GPA, pstate=0x3c5, sctlr=0;
    if (!reg_access(vcpu, KVM_SET_ONE_REG, CORE_ID(regs.regs[0]), &x0) ||
        !reg_access(vcpu, KVM_SET_ONE_REG, CORE_ID(regs.regs[1]), &x1) ||
        !reg_access(vcpu, KVM_SET_ONE_REG, CORE_ID(regs.pc), &pc) ||
        !reg_access(vcpu, KVM_SET_ONE_REG, CORE_ID(regs.pstate), &pstate) ||
        !reg_access(vcpu, KVM_GET_ONE_REG, SCTLR_ID, &sctlr)) goto cleanup;
    sctlr &= ~SCTLR_CLEAR;
    uint64_t expected = sctlr;
    if (!reg_access(vcpu, KVM_SET_ONE_REG, SCTLR_ID, &sctlr) ||
        !reg_access(vcpu, KVM_GET_ONE_REG, SCTLR_ID, &sctlr) || sctlr != expected)
        goto cleanup;
    LOG("LIMIT MMU off, no stage1 tables; no Normal-NC assertion; first PFN read only, NOT GPU coherence proof");

    /* No Vulkan or cleanup while watchdog/runner can still reference run/backing. */
    struct sigaction action = {.sa_handler=interrupt_run}, previous;
    sigemptyset(&action.sa_mask); /* flags=0: explicitly no SA_RESTART */
    sigset_t set, old_mask;
    sigemptyset(&set); sigaddset(&set, SIGUSR1);
    if (sigaction(SIGUSR1, &action, &previous)) goto cleanup;
    int error = pthread_sigmask(SIG_UNBLOCK, &set, &old_mask);
    if (error) { sigaction(SIGUSR1, &previous, NULL); goto cleanup; }
    struct watchdog w = {.mutex=PTHREAD_MUTEX_INITIALIZER, .runner=pthread_self(), .run=run};
    pthread_condattr_t attr;
    error = pthread_condattr_init(&attr);
    if (!error) {
        error = pthread_condattr_setclock(&attr, CLOCK_MONOTONIC);
        if (!error) error = pthread_cond_init(&w.cond, &attr);
        pthread_condattr_destroy(&attr);
    }
    bool cond_ready = !error;
    if (!error && clock_gettime(CLOCK_MONOTONIC, &w.deadline)) error = errno;
    w.deadline.tv_sec += 1;
    signal_seen = 0;
    run->immediate_exit = 0;
    pthread_t watchdog;
    if (!error) error = pthread_create(&watchdog, NULL, watchdog_main, &w);
    if (!error) {
        LOG("RUN_BEGIN mode=%s one ioctl only; watchdog=1s targeted SIGUSR1+immediate_exit; supervisor timeout cannot guarantee recovery from uninterruptible kernel/host hang; NO retry", native ? "native-read" : "anonymous-control");
        errno = 0;
        int result = ioctl(vcpu, KVM_RUN, 0);
        int saved_errno = errno;
        pthread_mutex_lock(&w.mutex);
        w.done = true;
        pthread_cond_signal(&w.cond);
        pthread_mutex_unlock(&w.mutex);
        /* If join cannot complete, do NOT free backing or close live references. */
        int joined = pthread_join(watchdog, NULL);
        if (joined) {
            LOG("FATAL watchdog join=%d; park forever, no cleanup of potentially live references", joined);
            for (;;) pause();
        }
        LOG("RUN_RETURN result=%d errno=%d signal=%d watchdog=%u watchdogError=%d exitReason=%u",
            result, saved_errno, signal_seen, w.fired, w.error, run->exit_reason);
        if (result < 0) LOG("KVM_ERROR errno=%d EFAULT=%d ENOEXEC=%d; safe close after return, no retry", saved_errno, EFAULT, ENOEXEC);
        uint32_t low = 0;
        if (result == 0 && !signal_seen && !w.fired && !w.error &&
            decode_mmio(run->exit_reason, run->mmio.phys_addr, run->mmio.len,
                        run->mmio.is_write, run->mmio.data, native, &low)) {
            LOG("READ_RESULT mode=%s low32=0x%08" PRIx32 " %s", native ? "native-read" : "anonymous-control",
                low, native ? "initial contents UNKNOWN; no known expected value; no GPU/coherence claim" : "known marker matched");
            status = 0;
        } else if (result == 0 && run->exit_reason == KVM_EXIT_MMIO) {
            LOG("FAIL unexpected MMIO addr=0x%" PRIx64 " len=%u write=%u; dataGPA read is NOT a payload result; no emulation/resume",
                (uint64_t)run->mmio.phys_addr, run->mmio.len, run->mmio.is_write);
        }
    } else LOG("FAIL watchdog setup error=%d; KVM_RUN not called", error);
    if (cond_ready) pthread_cond_destroy(&w.cond);
    pthread_mutex_destroy(&w.mutex);
    /* SIGUSR1 stays unblocked throughout; join guarantees no further watchdog signals. */
    if (sigaction(SIGUSR1, &previous, NULL)) status = 4;
    if (pthread_sigmask(SIG_SETMASK, &old_mask, NULL)) status = 4;
cleanup:
    LOG("KVM_CLEANUP status=%d errnoSnapshot=%d; no execution still running", status, errno);
    for (unsigned i=0; i<2; ++i) if (registered[i]) {
        slots[i].memory_size = 0;
        errno = 0;
        int result = ioctl(vm, KVM_SET_USER_MEMORY_REGION, &slots[i]);
        LOG("UNREGISTER slot=%u result=%d errno=%d; failure uses close-all-refs fallback before backing cleanup", i, result, errno);
        if (result) status = 4;
    }
    if (run != MAP_FAILED && munmap(run, run_size)) status = 4;
    if (!close_logged(vcpu, "vcpu")) status = 4;
    if (!close_logged(vm, "vm")) status = 4;
    if (!close_logged(kvm, "kvm")) status = 4;
    if (code != MAP_FAILED && munmap(code, PAGE_BYTES)) status = 4;
    LOG("KVM_PHASE_COMPLETE all vCPU/run/VM/KVM refs released before data backing cleanup status=%d", status);
    return status;
}
static int anonymous_control(void) {
    void *data = mmap(NULL, PAGE_BYTES, PROT_READ | PROT_WRITE,
                      MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
    if (data == MAP_FAILED) return 1;
    uint32_t marker = MARKER;
    memcpy(data, &marker, sizeof(marker)); /* anonymous control ONLY */
    int status = run_read((uintptr_t)data, false);
    if (munmap(data, PAGE_BYTES)) status = 4;
    LOG("ANONYMOUS_CONTROL_DONE status=%d", status);
    return status;
}

static int native_read(void) {
    setvbuf(stdout, NULL, _IONBF, 0);
    LOG("NATIVE_BEGIN ordinary type8 map, no host mapped payload read/write; no GPU workload, queue retrieval, import/export or existing VM touched");
    LOG("BOUNDARY Vulkan initialization may create worker threads/driver-internal GPU activity; no application GPU commands");
    struct utsname system_info;
    if (!uname(&system_info)) LOG("SYSTEM %s %s %s", system_info.sysname, system_info.release, system_info.machine);
    long page = sysconf(_SC_PAGESIZE);
    if (page != PAGE_BYTES) { LOG("FAIL requires exact 4096-byte system page, got=%ld; no rounding", page); return 1; }
    uint64_t size = PAGE_BYTES;
    LOG("REQUEST allocationSize=%" PRIu64 " systemPageSize=%ld maximum=65536 memoryTypeIndex=%u allocationPNext=NULL mapCount=1 mapOffset=0 mapSize=requestedAllocationSize", size, page, MEMORY_TYPE_INDEX);
    VkApplicationInfo application = {.sType = VK_STRUCTURE_TYPE_APPLICATION_INFO,
        .pApplicationName = "mali-native-kvm-cpu-read", .apiVersion = VK_API_VERSION_1_1};
    VkInstanceCreateInfo instance_info = {.sType = VK_STRUCTURE_TYPE_INSTANCE_CREATE_INFO,
        .pNext = NULL, .pApplicationInfo = &application,
        .enabledLayerCount = 0, .enabledExtensionCount = 0};
    VkInstance instance = VK_NULL_HANDLE;
    VkResult result = vkCreateInstance(&instance_info, NULL, &instance);
    LOG("CREATE_INSTANCE result=%d", result);
    if (result != VK_SUCCESS) return 1;
    int status = 1;
    VkDevice device = VK_NULL_HANDLE;
    VkDeviceMemory allocation = VK_NULL_HANDLE;
    bool mapped = false;
    void *mapped_pointer = NULL;
    uint32_t count = 0;
    result = vkEnumeratePhysicalDevices(instance, &count, NULL);
    if (result != VK_SUCCESS || !count || count > 16) {
        LOG("RESULT FAIL physicalDeviceCount=%u result=%d", count, result);
        goto done;
    }
    VkPhysicalDevice devices[16];
    result = vkEnumeratePhysicalDevices(instance, &count, devices);
    if (result != VK_SUCCESS) { LOG("RESULT FAIL enumerate result=%d", result); goto done; }
    VkPhysicalDevice selected = VK_NULL_HANDLE;
    uint32_t matches = 0;
    VkPhysicalDeviceProperties selected_properties = {0};
    for (uint32_t i = 0; i < count; ++i) {
        VkPhysicalDeviceProperties properties;
        vkGetPhysicalDeviceProperties(devices[i], &properties);
        LOG("DEVICE name=%s vendor=0x%x device=0x%x driver=0x%x api=%u.%u.%u", properties.deviceName,
            properties.vendorID, properties.deviceID, properties.driverVersion,
            VK_API_VERSION_MAJOR(properties.apiVersion), VK_API_VERSION_MINOR(properties.apiVersion),
            VK_API_VERSION_PATCH(properties.apiVersion));
        if (!strcmp(properties.deviceName, EXPECTED_DEVICE) && properties.vendorID == 0x13b5 &&
            properties.apiVersion >= VK_API_VERSION_1_1) {
            selected = devices[i]; selected_properties = properties; ++matches;
        }
    }
    if (matches != 1) { LOG("RESULT FAIL exact expected Mali matches=%u; no fallback", matches); goto done; }
    VkPhysicalDeviceMemoryProperties memory;
    vkGetPhysicalDeviceMemoryProperties(selected, &memory);
    if (memory.memoryTypeCount <= MEMORY_TYPE_INDEX) { LOG("RESULT FAIL type8 absent"); goto done; }
    VkMemoryType type = memory.memoryTypes[MEMORY_TYPE_INDEX];
    VkMemoryPropertyFlags required = VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT | VK_MEMORY_PROPERTY_HOST_COHERENT_BIT;
    LOG("TYPE index=%u flags=0x%x heap=%u HOST_VISIBLE=%u HOST_COHERENT=%u", MEMORY_TYPE_INDEX,
        type.propertyFlags, type.heapIndex, !!(type.propertyFlags & VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT),
        !!(type.propertyFlags & VK_MEMORY_PROPERTY_HOST_COHERENT_BIT));
    if (type.propertyFlags != 7 || (type.propertyFlags & required) != required ||
        (type.propertyFlags & VK_MEMORY_PROPERTY_PROTECTED_BIT) ||
        type.heapIndex >= memory.memoryHeapCount || memory.memoryHeaps[type.heapIndex].size < size) {
        LOG("RESULT FAIL type8 flags/heap rejected; no fallback"); goto done;
    }
    uint32_t queue_count = 0;
    vkGetPhysicalDeviceQueueFamilyProperties(selected, &queue_count, NULL);
    if (!queue_count || queue_count > 256) { LOG("RESULT FAIL queue family count=%u", queue_count); goto done; }
    VkQueueFamilyProperties families[256];
    vkGetPhysicalDeviceQueueFamilyProperties(selected, &queue_count, families);
    uint32_t family = 0;
    while (family < queue_count && !families[family].queueCount) ++family;
    if (family == queue_count) { LOG("RESULT FAIL no queue family"); goto done; }
    float priority = 1.0f;
    VkDeviceQueueCreateInfo queue_info = {.sType = VK_STRUCTURE_TYPE_DEVICE_QUEUE_CREATE_INFO,
        .pNext = NULL, .queueFamilyIndex = family, .queueCount = 1, .pQueuePriorities = &priority};
    VkDeviceCreateInfo device_info = {.sType = VK_STRUCTURE_TYPE_DEVICE_CREATE_INFO,
        .pNext = NULL, .queueCreateInfoCount = 1, .pQueueCreateInfos = &queue_info,
        .enabledExtensionCount = 0, .enabledLayerCount = 0, .pEnabledFeatures = NULL};
    LOG("CREATE_DEVICE family=%u queueCount=1 initialization only; never retrieve or use queue; extensions=0 features=NULL pNext=NULL", family);
    result = vkCreateDevice(selected, &device_info, NULL, &device);
    LOG("CREATE_DEVICE result=%d", result);
    if (result != VK_SUCCESS) goto done;
    VkMemoryAllocateInfo allocation_info = {.sType = VK_STRUCTURE_TYPE_MEMORY_ALLOCATE_INFO,
        .pNext = NULL, .allocationSize = size, .memoryTypeIndex = MEMORY_TYPE_INDEX};
    result = vkAllocateMemory(device, &allocation_info, NULL, &allocation);
    LOG("ALLOCATE result=%d requestedAllocationSize=%" PRIu64 " type=8 pNext=NULL", result, size);
    if (result != VK_SUCCESS) goto done;
    result = vkMapMemory(device, allocation, 0, size, 0, &mapped_pointer);
    LOG("MAP result=%d", result);
    if (result != VK_SUCCESS) goto done;
    mapped = true;
    uintptr_t address = (uintptr_t)mapped_pointer;
    if (!address) { LOG("RESULT FAIL map returned NULL"); goto done; }
    LOG("MAP_POINTER address=0x%" PRIxPTR " systemPageSize=%ld pageRemainder=%" PRIuPTR
        " pageAligned=%u minMemoryMapAlignment=%zu requestedAllocationSize=%" PRIu64,
        address, page, address % (uintptr_t)page, address % (uintptr_t)page == 0,
        selected_properties.limits.minMemoryMapAlignment, size);
    if (size != (uint64_t)page || size > 65536 || size % (uint64_t)page ||
        address % (uintptr_t)page || UINT64_C(0x100000) % (uint64_t)page ||
        size > UINTPTR_MAX - address || size > UINT64_MAX - UINT64_C(0x100000)) {
        LOG("RESULT FAIL registration requires exact one system page, aligned pointer/GPA and no address overflow; no rounding or fallback");
        goto done;
    }
    if (!inspect_vma(address, size)) { status = 2; goto done; }
    status = run_read(address, true);
    LOG("KVM_PHASE_COMPLETE all new vCPU/VM/KVM descriptors closed before Vulkan cleanup status=%d", status);
done:
    if (mapped) { vkUnmapMemory(device, allocation); LOG("CLEANUP unmapped"); }
    if (allocation != VK_NULL_HANDLE) { vkFreeMemory(device, allocation, NULL); LOG("CLEANUP freed"); }
    if (device != VK_NULL_HANDLE) { vkDestroyDevice(device, NULL); LOG("CLEANUP device destroyed"); }
    vkDestroyInstance(instance, NULL);
    LOG("NATIVE_DONE status=%d; only first guest CPU read returned through MMIO, not coherence/backing identity proof", status);
    return status;
}

int main(int argc, char **argv) {
    setvbuf(stdout, NULL, _IONBF, 0);
    if (argc != 2 || (strcmp(argv[1], "--anonymous-control") && strcmp(argv[1], "--native-read"))) {
        LOG("REFUSE default: explicit --anonymous-control or --native-read required; native-read always runs control FIRST; no device operation performed");
        return 64;
    }
    LOG("PROBE mali-native-kvm-cpu-read-v1 commit=%s VulkanHeaders=%u AndroidAPI=%d", PROBE_REVISION, VK_HEADER_VERSION, __ANDROID_API__);
    LOG("SAFETY fresh ARM64 VM, no GIC/disks/OS/network; readonly code+data slots; only one data LDR; no automatic retry. Native read may hang kernel/host despite watchdog and external supervisor; accepted residual, NOT guaranteed bounded kernel execution");
    if (sysconf(_SC_PAGESIZE) != PAGE_BYTES) { LOG("FAIL requires 4096-byte host pages"); return 1; }
    int status = anonymous_control();
    if (status) { LOG("REFUSE native: anonymous control/cleanup failed status=%d", status); return status; }
    if (!strcmp(argv[1], "--anonymous-control")) return 0;
    LOG("CONTROL_PASSED native allocation permitted for this invocation only");
    return native_read();
}
#endif
