"""Actions-only source and ELF checks. Never load Vulkan or execute the probe."""
import pathlib
import re
import sys

VULKAN = set('vkCreateInstance vkDestroyInstance vkEnumeratePhysicalDevices vkGetPhysicalDeviceProperties vkGetPhysicalDeviceMemoryProperties vkGetPhysicalDeviceQueueFamilyProperties vkCreateDevice vkDestroyDevice vkAllocateMemory vkMapMemory vkUnmapMemory vkFreeMemory'.split())
CALLS = VULKAN | set('main decode_pagemap pagemap_offset fault_and_mark own_mapping_metadata hold_mapping LOG printf putchar fflush strcmp strncmp strstr getenv sysconf sizeof _Alignof if for while UINT32_C UINT64_C VK_API_VERSION_MAJOR VK_API_VERSION_MINOR VK_API_VERSION_PATCH fopen fgets sscanf ferror fclose open pread close getpid clock_gettime sleep'.split())
IMPORTS = VULKAN | set('__libc_start_main __cxa_finalize __gmon_start__ __stack_chk_fail __stack_chk_guard abort _ITM_deregisterTMCloneTable _ITM_registerTMCloneTable printf __printf_chk putchar puts putc fflush stdout strcmp strncmp strstr getenv sysconf memcpy memset fopen fgets __fgets_chk __isoc99_sscanf __isoc23_sscanf ferror fclose open open64 pread pread64 close getpid clock_gettime sleep __errno_location'.split())
TOKENS = r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|/\*.*?\*/|//[^\n]*'


def check(source):
    clean = re.sub(TOKENS, lambda m: '' if m[0].startswith(('/*', '//')) else m[0], source, flags=re.S)
    code = re.sub(r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'', '""', clean)
    assert set(re.findall(r'\b([A-Za-z_]\w*)\s*\(', code)) == CALLS
    assert set(re.findall(r'\bvk[A-Z]\w*', code)) == VULKAN
    assert not re.search(r'\b(?:PFN_\w+|Vk\w*(?:Import|Export|External|Buffer|Image|Command|Shader)\w*)\b', code)
    assert re.findall(r'\bfopen\(([^;]+)\);', clean) == ['"/proc/self/maps", "r"']
    assert re.findall(r'\bopen\(([^;]+)\);', clean) == ['"/proc/self/pagemap", O_RDONLY | O_CLOEXEC']
    assert re.findall(r'"(/proc/[^"\n]*)"', clean) == ['/proc/self/maps', '/proc/self/pagemap']
    assert not re.search(r'/dev/|mmap|ioctl|KVM_|asm\b', clean)
    assert re.findall(r'\bsleep\(([^)]+)\)', code) == ['45']
    hold = clean[clean.index('static int hold_mapping'):clean.index('\n#endif', clean.index('static int hold_mapping'))]
    assert not re.search(r'\b(?:for|while)\b', hold)
    assert 'sleep_error == EINTR' in hold and 'return status;' in hold
    helper = code[code.index('static int fault_and_mark'):code.index('#ifndef MAP_IDENTITY_CPU_ONLY', code.index('static int fault_and_mark'))]
    assert not re.search(r'\b(?:for|while)\b', helper)
    assert len(re.findall(r'\*(?:first|last)\b', code)) == 5  # two declarations + three accesses
    assert not re.search(r'\b(?:first|last|base|mapped_pointer)\s*\[', code)
    for text in [
        '#define MEMORY_TYPE_INDEX 8u', '#define REQUESTED_SIZE 4096u',
        'extent != REQUESTED_SIZE', 'start % _Alignof(uint32_t)',
        'start > UINTPTR_MAX - (REQUESTED_SIZE - 1u)',
        '4092u > extent || sizeof(uint32_t) > extent - 4092u',
        'uint32_t initial = *first;', '*first = UINT32_C(0x13579bdf);', '*last = UINT32_C(0x2468ace0);',
        '(unsigned)(raw >> 63)', '(unsigned)((raw >> 62) & 1u)', 'raw & ((UINT64_C(1) << 55) - 1u)',
        'va / page > (uint64_t)INT64_MAX / 8u', '*offset = (off_t)((va / page) * 8u)',
        'const uintptr_t addresses[2] = {address, address + 4092u};',
        'pread(fd, &raw, sizeof(raw), offset)', 'bytes != (ssize_t)sizeof(raw)',
        '!p.present || p.swapped || !p.pfn || p.pfn > UINT64_MAX / page',
        'low <= address && address < high', 'PROC_MAP_MATCH %s',
        'argc != 2 || strcmp(argv[1], "--allow-mapping-identity") != 0',
        'strcmp(icd, EXPECTED_ICD) != 0', 'strncmp(libraries, EXPECTED_LIBRARY_PREFIX, prefix_size) != 0',
        '(legacy_icd && legacy_icd[0]) || (layers && layers[0]) || (preload && preload[0])',
        'page != 4096', 'if (matches != 1) goto done;',
        'VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT | VK_MEMORY_PROPERTY_HOST_COHERENT_BIT',
        '(type.propertyFlags & required) != required', 'VK_MEMORY_PROPERTY_PROTECTED_BIT',
        '.pNext = NULL, .allocationSize = size, .memoryTypeIndex = MEMORY_TYPE_INDEX',
        'vkMapMemory(device, allocation, 0, size, 0, &mapped_pointer)',
        'address % alignment != 0', 'if (address % (uintptr_t)page)',
        'HOLD_READY PID=', 'HOLD_END PID=', 'no inferred VMA extent', 'zero may mean PFN redaction',
        'NOT hostPFN', 'timeout cannot guarantee recovery', 'lose root',
        "putchar('\\n'); fflush(stdout);",
    ]:
        assert text in clean, text
    assert clean.index('return 2;') < clean.index('getenv(') < clean.index('vkCreateInstance(')
    order = ['status = fault_and_mark(', 'status = own_mapping_metadata(', 'status = hold_mapping(', 'vkUnmapMemory(', 'vkFreeMemory(', 'vkDestroyDevice(', 'vkDestroyInstance(']
    assert [clean.index(s) for s in order] == sorted(clean.index(s) for s in order)
    for name in ['vkCreateInstance', 'vkCreateDevice', 'vkAllocateMemory', 'vkMapMemory', 'vkUnmapMemory', 'vkFreeMemory', 'vkDestroyDevice', 'vkDestroyInstance']:
        assert len(re.findall(r'\b' + name + r'\s*\(', code)) == 1


source = pathlib.Path(sys.argv[1]).read_text()
check(source)
for before, after in [
    ('done:', 'vkQueueSubmit(0,0,0,0); done:'),
    ('done:', 'vkFlushMappedMemoryRanges(0,0,0); done:'),
    ('done:', 'mmap(0,0,0,0,0,0); done:'),
    ('/proc/self/pagemap', '/proc/1/pagemap'),
    ('O_RDONLY | O_CLOEXEC', 'O_RDWR'),
    ('sleep(45)', 'sleep(90)'),
    ('extent != REQUESTED_SIZE', 'extent == REQUESTED_SIZE'),
    ('*last = UINT32_C(0x2468ace0);', '*last = UINT32_C(0x2468ace0); uint32_t extra = *last;'),
    ('!p.pfn ||', ''),
    ('MEMORY_TYPE_INDEX 8u', 'MEMORY_TYPE_INDEX 0u'),
    ('REQUESTED_SIZE 4096u', 'REQUESTED_SIZE 65536u'),
    ('--allow-mapping-identity', '--unsafe'),
]:
    assert before in source
    try:
        check(source.replace(before, after))
    except AssertionError:
        continue
    raise AssertionError('Unsafe mutation accepted: ' + before)
print('PASS source/API/path allowlists, three scalar accesses, one sleep45, 12 unsafe mutations rejected')
if len(sys.argv) == 4:
    names = {line.split()[-1].split('@')[0] for line in pathlib.Path(sys.argv[2]).read_text().splitlines() if line.split()}
    assert names & VULKAN == VULKAN
    assert names <= IMPORTS, names - IMPORTS
    elf = pathlib.Path(sys.argv[3]).read_text()
    assert re.search(r'Machine:\s+AArch64', elf) and re.search(r'Class:\s+ELF64', elf)
    assert '/lib/ld-linux-aarch64.so.1' in elf
    assert set(re.findall(r'\(NEEDED\).*?\[(.*?)\]', elf)) <= {'libvulkan.so.1', 'libc.so.6', 'ld-linux-aarch64.so.1'}
    print('PASS AArch64 glibc ELF/import allowlist; binary inspected NEVER executed')
else:
    assert len(sys.argv) == 2
