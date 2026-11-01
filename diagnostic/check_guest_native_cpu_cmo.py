"""Actions-only structural/ELF/disassembly checks. Never execute the CMO probe."""
import pathlib
import re
import sys

VULKAN = {
    'vkCreateInstance', 'vkDestroyInstance', 'vkEnumeratePhysicalDevices',
    'vkGetPhysicalDeviceProperties', 'vkGetPhysicalDeviceMemoryProperties',
    'vkGetPhysicalDeviceQueueFamilyProperties', 'vkCreateDevice', 'vkDestroyDevice',
    'vkAllocateMemory', 'vkMapMemory', 'vkUnmapMemory', 'vkFreeMemory',
    'vkFlushMappedMemoryRanges',
}
NATIVE_IMPORTS = {
    '__libc_start_main', '__cxa_finalize', '__gmon_start__', '__stack_chk_fail',
    '__stack_chk_guard', 'abort', '_ITM_deregisterTMCloneTable', '_ITM_registerTMCloneTable',
    'printf', '__printf_chk', 'putchar', 'puts', 'putc', 'fflush', 'stdout',
    'strcmp', 'strncmp', 'strstr', 'getenv', 'sysconf', 'memcpy', 'memset',
    'sigaction', 'sigemptyset', 'sigaddset', 'sigismember', 'pthread_sigmask',
    '__sigsetjmp', 'siglongjmp', '__longjmp_chk', '_Exit',
}
TOKENS = r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|/\*.*?\*/|//[^\n]*'

def check(source):
    clean = re.sub(TOKENS, lambda m: '' if m[0].startswith(('/*', '//')) else m[0], source, flags=re.S)
    code = re.sub(r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'', '""', clean)
    assert set(re.findall(r'\bvk[A-Z]\w*', code)) == VULKAN
    assert not re.search(r'\b(?:PFN_\w+|Vk\w*(?:Import|Export|External|Buffer|Image|Command|Shader|Fence)\w*)\b', code)
    assert not re.search(r'/dev/|/proc/|mmap|ioctl|KVM_|__clear_cache|dc cvac|dc ivac', clean)
    assert len(re.findall(r'\b__asm__\s+volatile', clean)) == 1
    assert clean.count('dc civac,') == 2
    assert '"dsb sy\\n\\tdc civac, %0\\n\\tdc civac, %1\\n\\tdsb sy"' in clean
    assert ': : "r"(base), "r"(last) : "memory"' in clean
    handler = clean.split('static void cmo_signal_handler(int signal_number) {', 1)[1].split('}', 1)[0]
    assert handler.strip() == 'if (!cmo_armed) _Exit(128 + signal_number);\n    cmo_signal = signal_number;\n    siglongjmp(cmo_jump, 1);'
    assert 'static _Thread_local sigjmp_buf cmo_jump;' in clean
    assert 'static _Thread_local volatile sig_atomic_t cmo_armed;' in clean
    assert 'static _Thread_local volatile sig_atomic_t cmo_signal;' in clean
    guard = clean.split('static int cpu_cmo(', 1)[1].split('\n#endif\n\n#ifndef CROSSVIEW_CPU_ONLY', 1)[0]
    assert not re.search(r'\bvk[A-Z]|cpu_crossview_words|__clear_cache|memcpy|memset', guard)
    for text in ['start % REQUESTED_SIZE != 0', 'void *last = (unsigned char *)base + 4092u;',
                 'const int signals[3] = {SIGILL, SIGBUS, SIGSEGV};',
                 'action.sa_mask = blocked;', 'if (sigsetjmp(cmo_jump, 1) == 0)',
                 '} else status = 26;', 'return (int)status;', '#if defined(__aarch64__)',
                 'return 27;', 'if (pthread_sigmask(SIG_BLOCK, &blocked, NULL) != 0) _Exit(25);',
                 'if (sigaction(signals[i], &previous[i], NULL) != 0) _Exit(25);',
                 'if (pthread_sigmask(SIG_SETMASK, &original, NULL) != 0) _Exit(25);']:
        assert text in guard, text
    assert guard.index('sigsetjmp(') < guard.index('cmo_armed = 1;') < guard.index('__asm__') < guard.index('sigaction(signals[i]')
    assert 'if (pthread_sigmask(SIG_BLOCK, &blocked, NULL) != 0) _Exit(25);\n    cmo_armed = 0;\n    for (unsigned' in guard
    assert guard.index('cmo_armed = 0;') < guard.index('sigaction(signals[installed]')
    helper = clean.split('static int cpu_crossview_words(', 1)[1].split('\n#ifndef CROSSVIEW_CPU_ONLY', 1)[0]
    assert not re.search(r'\b(?:for|while|memcpy|memset)\b', helper)
    for text in ['extent != REQUESTED_SIZE', 'start % _Alignof(uint32_t) != 0',
                 'start > UINTPTR_MAX - (REQUESTED_SIZE - 1u)',
                 '4092u > extent || sizeof(uint32_t) > extent - 4092u',
                 '*first = UINT32_C(0x13579bdf);', '*last = UINT32_C(0x2468ace0);',
                 'uint32_t first_value = *first;', 'uint32_t last_value = *last;',
                 'first_value == UINT32_C(0x89abcdef) && last_value == UINT32_C(0xfedcba98)']:
        assert text in helper, text
    assert len(re.findall(r'\*(?:first|last)\b', helper)) == 6
    assert len(re.findall(r'\*(?:first|last)\b', code)) == 7  # plus CMO last-address declaration
    assert not re.search(r'\b(?:first|last|base|mapped_pointer)\s*\[', code)
    assert 'argc != 2 || strcmp(argv[1], "--allow-cpu-cmo") != 0' in clean
    for text in ['#define MEMORY_TYPE_INDEX 8u', '#define REQUESTED_SIZE 4096u',
                 '.pApplicationName = "guest-native-cpu-crossview"',
                 'GFXSTREAM_NATIVE_CPU_CROSSVIEW=1', 'page != 4096',
                 'vkMapMemory(device, allocation, 0, size, 0, &mapped_pointer)',
                 '.pNext = NULL, .allocationSize = size, .memoryTypeIndex = MEMORY_TYPE_INDEX',
                 '.pNext = NULL, .memory = allocation, .offset = 0, .size = size};']:
        assert text in clean, text
    main = clean.split('int main(', 1)[1]
    request = 'status = cpu_crossview_words(mapped_pointer, (size_t)size, false);'
    cmo = 'status = cpu_cmo(mapped_pointer, (size_t)size);'
    reply = 'status = cpu_crossview_words(mapped_pointer, (size_t)size, true);'
    assert main.index(request) < main.index(cmo) < main.index('vkFlushMappedMemoryRanges(') < main.index(reply)
    assert cmo + '\n    if (status != 0) goto done;' in main
    assert 'if (result != VK_SUCCESS) goto done;\n    ' + reply in main
    for name in VULKAN - {'vkEnumeratePhysicalDevices', 'vkGetPhysicalDeviceQueueFamilyProperties'}:
        assert len(re.findall(r'\b' + name + r'\s*\(', code)) == 1, name
    assert main.index(reply) < main.index('vkUnmapMemory(') < main.index('vkFreeMemory(') < main.index('vkDestroyDevice(') < main.index('vkDestroyInstance(')

source = pathlib.Path(sys.argv[1]).read_text()
check(source)
mutations = [source.replace('dc civac, %1', 'dc civac, %0'),
             source.replace('dc civac, %0', 'dc cvac, %0'),
             source.replace('cpu_cmo(mapped_pointer, (size_t)size);\n    if (status != 0) goto done;', 'cpu_cmo(mapped_pointer, (size_t)size);'),
             source.replace('done:', 'vkQueueSubmit(0,0,0,0); done:'),
             source.replace('done:', 'vkInvalidateMappedMemoryRanges(0,0,0); done:'),
             source.replace('done:', '*last = 0; done:'),
             source.replace('first_value == UINT32_C(0x89abcdef)', 'first_value == UINT32_C(0x13579bdf)'),
             source.replace('--allow-cpu-cmo', '--unsafe'),
             source.replace('.pApplicationName = "guest-native-cpu-crossview"', '.pApplicationName = "guest-native-cpu-cmo"'),
             source.replace('siglongjmp(cmo_jump, 1);', 'printf("fail"); siglongjmp(cmo_jump, 1);')]
for mutation in mutations:
    assert mutation != source
    try:
        check(mutation)
    except AssertionError:
        continue
    raise AssertionError('unsafe mutation accepted')
print('PASS source order, exact two VA CIVAC operations, signal failure skips flush, host app gate, 10 forbidden mutations rejected')
if len(sys.argv) == 5:
    names = {line.split()[-1].split('@')[0] for line in pathlib.Path(sys.argv[2]).read_text().splitlines() if line.split()}
    assert names & VULKAN == VULKAN
    assert names <= VULKAN | NATIVE_IMPORTS, names - VULKAN - NATIVE_IMPORTS
    elf = pathlib.Path(sys.argv[3]).read_text()
    assert re.search(r'Machine:\s+AArch64', elf) and '/lib/ld-linux-aarch64.so.1' in elf
    assert set(re.findall(r'\(NEEDED\).*?\[(.*?)\]', elf)) == {'libvulkan.so.1', 'libc.so.6', 'ld-linux-aarch64.so.1'}
    dis = pathlib.Path(sys.argv[4]).read_text()
    cmos = re.findall(r'^\s*([0-9a-f]+):\s+[0-9a-f]+\s+dc\s+civac,\s*(x\d+)', dis, re.M)
    assert len(cmos) == 2, cmos
    assert cmos[0][1] != cmos[1][1], cmos
    assert not re.search(r'\bdc\s+(?!civac\b)\w+', dis)
    instructions = re.findall(r'^\s*[0-9a-f]+:\s+[0-9a-f]+\s+(.+)$', dis, re.M)
    positions = [i for i, text in enumerate(instructions) if text.startswith('dc\t') or text.startswith('dc ')]
    assert len(positions) == 2 and positions[1] == positions[0] + 1
    assert re.fullmatch(r'dsb\s+sy', instructions[positions[0] - 1].strip())
    assert re.fullmatch(r'dsb\s+sy', instructions[positions[1] + 1].strip())
    print('PASS ELF imports and actual binary disassembly: DSB SY, two CIVAC distinct registers, DSB SY; binary NEVER executed')
else:
    assert len(sys.argv) == 2
print('LIMIT array tests exclude CMO/guard; no signal recovery or cache instruction runtime test; not coherence/FWB proof')
