"""Actions-only static checks and pure MMIO decoder oracle; NEVER executes probe/KVM."""
import pathlib
import re
import subprocess
import sys

source_path = pathlib.Path(sys.argv[1]).resolve()
source = source_path.read_text()
clean = re.sub(r'/\*.*?\*/|//[^\n]*', '', source, flags=re.S)
code = re.sub(r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'', '""', clean)
vulkan = {
    'vkCreateInstance', 'vkDestroyInstance', 'vkEnumeratePhysicalDevices',
    'vkGetPhysicalDeviceProperties', 'vkGetPhysicalDeviceMemoryProperties',
    'vkGetPhysicalDeviceQueueFamilyProperties', 'vkCreateDevice', 'vkDestroyDevice',
    'vkAllocateMemory', 'vkMapMemory', 'vkUnmapMemory', 'vkFreeMemory',
}
assert set(re.findall(r'\b(vk\w+)\s*\(', code)) == vulkan
assert code.count('ioctl(vcpu, KVM_RUN, 0)') == 1
assert code.count('.flags=KVM_MEM_READONLY') == 2
assert '.slot=0' in code and '.slot=1' in code
assert 'memset(init.features, 0, sizeof(init.features))' in code
for capability in ['KVM_CAP_USER_MEMORY', 'KVM_CAP_ONE_REG', 'KVM_CAP_IMMEDIATE_EXIT', 'KVM_CAP_READONLY_MEM']:
    assert f'!require_cap(kvm, {capability})' in code
for required in ['offsetof(struct kvm_regs, member) / sizeof(uint32_t)',
                 'sctlr &= ~SCTLR_CLEAR;', 'sctlr != expected',
                 'pthread_kill(w->runner, SIGUSR1)',
                 '__atomic_store_n(&w->run->immediate_exit, 1, __ATOMIC_RELEASE)',
                 'w.deadline.tv_sec += 1;', 'CLOCK_MONOTONIC',
                 'struct sigaction action = {.sa_handler=interrupt_run}',
                 'mprotect(code, PAGE_BYTES, PROT_READ | PROT_EXEC)',
                 'type.propertyFlags != 7', 'matches != 1',
                 'properties.vendorID == 0x13b5', 'page != PAGE_BYTES',
                 'requested_size <= maps.end - pointer',
                 'requested_size <= smaps.end - pointer',
                 'size > UINTPTR_MAX - address']:
    assert required in code, required
handler = code[code.index('static void interrupt_run'):code.index('struct watchdog')]
assert handler.count('signal_seen = 1;') == 1 and 'LOG(' not in handler
native = code[code.index('static int native_read'):code.index('int main(')]
assert len(re.findall(r'\bmapped_pointer\b', native)) == 5
assert 'void *mapped_pointer = NULL;' in native
assert '(uintptr_t)mapped_pointer' in native
assert 'vkMapMemory(device, allocation, 0, size, 0, &mapped_pointer)' in native
assert 'status = run_read(address, mode);' in native
assert native.count('*(volatile uint32_t *)mapped_pointer') == 2
store = native.index('*(volatile uint32_t *)mapped_pointer = MARKER;')
barrier = native.index('__asm__ volatile(')
assert '__asm__ volatile("dsb sy" ::: "memory");' in source
run_call = native.index('status = run_read(address, mode);')
readback = native.index('uint32_t host_after = *(volatile uint32_t *)mapped_pointer;')
assert native.index('if (!inspect_vma(address, size))') < store < barrier < run_call < readback < native.index('vkUnmapMemory(')
assert native.count('if (mode == READ_NATIVE_MARKED)') == 2
assert '((mode == READ_ANONYMOUS || mode == READ_NATIVE_MARKED) && *low == MARKER)' in code
assert code.count('__asm__') == 1
assert not re.search(r'\b(?:dc|ic)\s+(?:cvac|civac|ivac|cvau|cvap|cvadp|iallu|ivau)\b', clean)
assert not re.search(r'vk(?:Flush|Invalidate)MappedMemoryRanges', code)
assert native.index('run_read(address, mode)') < native.index('vkUnmapMemory(') < native.index('vkFreeMemory(')
for name in ['vkAllocateMemory', 'vkMapMemory', 'vkUnmapMemory', 'vkFreeMemory']:
    assert len(re.findall(r'\b'+name+r'\s*\(', code)) == 1
main = code[code.index('int main('):]
assert main.index('int status = anonymous_control();') < main.index('if (status)') < main.index('return native_read(')
assert main.index('argc != 2') < main.index('sysconf(') < main.index('anonymous_control();')
run = code[code.index('static int run_read'):code.index('static int anonymous_control')]
assert run.index('ioctl(vcpu, KVM_RUN, 0)') < run.index('pthread_join(') < run.index('slots[i].memory_size = 0;') < run.index('munmap(run, run_size)') < run.index('close_logged(vcpu') < run.index('close_logged(vm') < run.index('close_logged(kvm') < run.index('munmap(code, PAGE_BYTES)') < run.rindex('return status;')
assert set(re.findall(r'\bmemcpy\(([^,]+),', code)) == {'code', 'data'}
assert re.findall(r'\bopen\(([^;]+)\)', clean) == ['"/dev/kvm", O_RDWR | O_CLOEXEC']
assert re.findall(r'fopen\(([^;]+)\)', clean) == ['"/proc/self/maps", "re"', '"/proc/self/smaps", "re"']
assert not re.search(r'\b(?:system|popen|fork|exec\w*|socket|connect|syscall|dlopen|dlsym|read|pread\w*|write|pwrite\w*|dup\w*)\s*\(', code)
assert not re.search(r'\b(?:SA_RESTART|KVM_CREATE_IRQCHIP|KVM_CREATE_DEVICE|KVM_ARM_VCPU_FINALIZE)\b', code)
assert 'pagemap' not in clean
print('PASS static boundaries: readonly code/data; control gates native; one RUN; targeted watchdog; exactly one marked host uint32 store/read, no CMO; cleanup order')
if len(sys.argv) > 2:
    symbols = {line.split()[-1].split('@')[0] for line in pathlib.Path(sys.argv[2]).read_text().splitlines() if line.split()}
    assert {s for s in symbols if s.startswith('vk')} == vulkan
    assert not {s for s in symbols if re.fullmatch(r'(?:fork|vfork|exec\w*|dup\w*|syscall|socket|connect|dlopen|dlsym|pread\w*|pwrite\w*|process_vm_\w*|ptrace|system|popen)', s)}
    assert {'mmap', 'munmap', 'mprotect', 'ioctl', 'open', 'close', 'pthread_kill', 'pthread_create', 'pthread_join'} <= symbols
    print('PASS imported symbol boundary')
if len(sys.argv) > 3:
    ndk_bin = pathlib.Path(sys.argv[3])
    out = pathlib.Path(sys.argv[4]).resolve()
    assembly = out / 'guest-code.S'
    assembly.write_text('.text\n.global guest_start\nguest_start:\n ldr w2, [x0]\n movk x2, #0xa55a, lsl #48\n str x2, [x1]\n udf #0\n')
    obj = out / 'guest-code.o'
    binary = out / 'guest-code.bin'
    subprocess.run([str(ndk_bin/'aarch64-linux-android33-clang'), '-c', str(assembly), '-o', str(obj)], check=True)
    subprocess.run([str(ndk_bin/'llvm-objcopy'), '-O', 'binary', '--only-section=.text', str(obj), str(binary)], check=True)
    words = re.findall(r'0x[0-9a-f]+', code[code.index('static const uint32_t guest_code'):code.index('static bool decode_mmio')])
    expected = b''.join(int(w, 16).to_bytes(4, 'little') for w in words)
    assert len(expected) == 16 and binary.read_bytes() == expected
    disassembly = subprocess.check_output([str(ndk_bin/'llvm-objdump'), '-d', str(obj)], text=True)
    (out/'guest-code-disassembly.txt').write_text(disassembly)
    print(disassembly)
    assert re.search(r'ldr\s+w2, \[x0\]', disassembly)
    assert re.search(r'movk\s+x2, #(?:0xa55a|42330), lsl #48', disassembly)
    assert re.search(r'str\s+x2, \[x1\]', disassembly)
    assert 'udf' in disassembly
    print('PASS independent ARM64 assembler bytes and disassembly; guest code NEVER executed')
    oracle = out/'decoder-oracle.c'
    oracle.write_text('#define CPU_READ_ORACLE_ONLY\n#include "'+source_path.as_posix()+'"\n'+r'''
#include <assert.h>
#include <stdio.h>
static bool fixture(uint32_t reason, uint64_t address, uint32_t length,
                    uint8_t write, uint64_t value, enum read_mode mode, uint32_t *low) {
    uint8_t bytes[8];
    for (unsigned i=0; i<8; ++i) bytes[i]=(uint8_t)(value>>(8*i));
    return decode_mmio(reason, address, length, write, bytes, mode, low);
}
int main(void) {
    uint32_t low=0;
    assert(sizeof(guest_code)==16);
    const uint64_t good=UINT64_C(0xa55a000013579bdf);
    assert(fixture(6, MMIO_GPA, 8, 1, good, READ_ANONYMOUS, &low) && low==MARKER);
    assert(fixture(6, MMIO_GPA, 8, 1, UINT64_C(0xa55a000000000000), READ_NATIVE_UNKNOWN, &low) && low==0);
    assert(fixture(6, MMIO_GPA, 8, 1, UINT64_C(0xa55a0000ffffffff), READ_NATIVE_UNKNOWN, &low) && low==UINT32_MAX);
    assert(!fixture(6, MMIO_GPA, 8, 1, UINT64_C(0xa55a000000000000), READ_ANONYMOUS, &low));
    assert(fixture(6, MMIO_GPA, 8, 1, good, READ_NATIVE_MARKED, &low) && low==MARKER);
    assert(!fixture(6, MMIO_GPA, 8, 1, UINT64_C(0xa55a000000000000), READ_NATIVE_MARKED, &low));
    assert(!fixture(6, MMIO_GPA, 8, 1, UINT64_C(0xa55a0000ffffffff), READ_NATIVE_MARKED, &low));
    for (unsigned bit=0; bit<64; ++bit)
        assert(!fixture(6, MMIO_GPA, 8, 1, good^(UINT64_C(1)<<bit), READ_NATIVE_MARKED, &low));
    assert(!fixture(6, DATA_GPA, 4, 0, good, READ_NATIVE_MARKED, &low));
    assert(!fixture(5, MMIO_GPA, 8, 1, good, READ_NATIVE_UNKNOWN, &low));
    assert(!fixture(6, DATA_GPA, 8, 0, good, READ_NATIVE_UNKNOWN, &low));
    assert(!fixture(6, CODE_GPA, 8, 1, good, READ_NATIVE_UNKNOWN, &low));
    for (unsigned n=0; n<=16; ++n) if (n!=8)
        assert(!fixture(6, MMIO_GPA, n, 1, good, READ_NATIVE_UNKNOWN, &low));
    for (unsigned n=0; n<=255; ++n) if (n!=1)
        assert(!fixture(6, MMIO_GPA, 8, (uint8_t)n, good, READ_NATIVE_UNKNOWN, &low));
    for (unsigned bit=32; bit<64; ++bit)
        assert(!fixture(6, MMIO_GPA, 8, 1, good^(UINT64_C(1)<<bit), READ_NATIVE_UNKNOWN, &low));
    puts("PASS pure decoder fixtures only; synthetic records are NOT KVM/memory evidence; no probe/KVM calls linked or executed");
    return 0;
}
''')
    executable = out/'decoder-oracle'
    subprocess.run(['cc', '-std=c11', '-Wall', '-Wextra', '-Werror', '-O2', '-fsanitize=undefined,address', str(oracle), '-o', str(executable)], check=True)
    subprocess.run([str(executable)], check=True)
print('LIMIT static checks and pure decoder fixtures only; Android binary NEVER executed, no KVM/device tests')
