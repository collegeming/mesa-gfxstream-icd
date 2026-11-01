"""Actions-only structural checks; no Vulkan loading or probe execution."""
import pathlib
import re
import sys

VULKAN = {
    'vkCreateInstance', 'vkDestroyInstance', 'vkEnumeratePhysicalDevices',
    'vkGetPhysicalDeviceProperties', 'vkGetPhysicalDeviceMemoryProperties',
    'vkGetPhysicalDeviceQueueFamilyProperties', 'vkCreateDevice',
    'vkDestroyDevice', 'vkAllocateMemory', 'vkMapMemory', 'vkUnmapMemory', 'vkFreeMemory',
}
C_CALLS = VULKAN | {
    'main', 'cpu_first_touch', 'LOG', 'printf', 'putchar', 'fflush', 'strcmp',
    'strncmp', 'strstr', 'getenv', 'sysconf', 'sizeof', '_Alignof', 'if', 'for',
    'while', 'UINT32_C', 'VK_API_VERSION_MAJOR', 'VK_API_VERSION_MINOR', 'VK_API_VERSION_PATCH',
}
NATIVE_IMPORTS = {
    '__libc_start_main', '__cxa_finalize', '__gmon_start__', '__stack_chk_fail',
    '__stack_chk_guard', 'abort', '_ITM_deregisterTMCloneTable', '_ITM_registerTMCloneTable',
    'printf', '__printf_chk', 'putchar', 'puts', 'putc', 'fflush', 'stdout',
    'strcmp', 'strncmp', 'strstr', 'getenv', 'sysconf', 'memcpy', 'memset',
}
TOKENS = r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|/\*.*?\*/|//[^\n]*'


def check_source(source):
    clean = re.sub(TOKENS, lambda m: '' if m[0].startswith(('/*', '//')) else m[0], source, flags=re.S)
    code = re.sub(r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'', '""', clean)
    assert set(re.findall(r'\b([A-Za-z_]\w*)\s*\(', code)) == C_CALLS
    assert set(re.findall(r'\bvk[A-Z]\w*', code)) == VULKAN
    assert not re.search(r'\b(?:PFN_\w+|Vk\w*(?:Import|Export|External|Buffer|Image|Command|Shader)\w*)\b', code)
    assert not re.search(r'/dev/|/proc/|mmap|ioctl|KVM_', clean)
    helper = clean[clean.index('static int cpu_first_touch('):clean.index('#ifndef FIRST_TOUCH_CPU_ONLY\n', clean.index('static int cpu_first_touch('))]
    assert not re.search(r'\b(?:for|while|memcpy|memset)\b', helper)
    assert 'extent != REQUESTED_SIZE' in helper
    assert 'start % _Alignof(uint32_t) != 0' in helper
    assert 'start > UINTPTR_MAX - (REQUESTED_SIZE - 1u)' in helper
    assert '4092u > extent || sizeof(uint32_t) > extent - 4092u' in helper
    assert helper.index('return 20;') < helper.index('volatile uint32_t *first')
    assert 'volatile uint32_t *first = (volatile uint32_t *)base;' in helper
    assert 'volatile uint32_t *last = (volatile uint32_t *)((unsigned char *)base + 4092u);' in helper
    accesses = ['uint32_t initial = *first;', '*first = UINT32_C(0x13579bdf);',
                'uint32_t first_value = *first;', '*last = UINT32_C(0x2468ace0);',
                'uint32_t last_value = *last;']
    stages = ['first-read', 'first-write', 'first-readback', 'last-write', 'last-readback']
    for access, stage in zip(accesses, stages):
        assert re.search(r'LOG\("BEGIN stage=' + stage + r' [^\n]*\);\s*' + re.escape(access), helper)
    assert [helper.index(a) for a in accesses] == sorted(helper.index(a) for a in accesses)
    assert len(re.findall(r'\*(?:first|last)\b', code)) == 7  # 2 declarations + 5 accesses
    assert len(re.findall(r'\bfirst\b', re.sub(r'"(?:\\.|[^"\\])*"', '""', helper))) == 4
    assert len(re.findall(r'\blast\b', re.sub(r'"(?:\\.|[^"\\])*"', '""', helper))) == 3
    assert not re.search(r'\b(?:first|last|base|mapped_pointer)\s*\[', code)
    assert 'if (first_value != UINT32_C(0x13579bdf)) return 21;' in helper
    assert 'if (last_value != UINT32_C(0x2468ace0)) return 22;' in helper
    assert 'putchar(\'\\n\'); fflush(stdout);' in clean
    assert 'argc != 2 || strcmp(argv[1], "--allow-cpu-first-touch") != 0' in clean
    assert clean.index('return 2;') < clean.index('getenv(') < clean.index('vkCreateInstance(')
    for required in ['#define MEMORY_TYPE_INDEX 8u', '#define REQUESTED_SIZE 4096u',
                     '#define EXPECTED_ICD "/opt/droidvm-mesa-reference/icd.json"',
                     '#define EXPECTED_LIBRARY_PREFIX "/opt/droidvm-mesa-reference/usr/local/lib/aarch64-linux-gnu"',
                     'strcmp(icd, EXPECTED_ICD) != 0', 'strncmp(libraries, EXPECTED_LIBRARY_PREFIX, prefix_size) != 0',
                     "(libraries[prefix_size] != '\\0' && libraries[prefix_size] != ':')",
                     '(legacy_icd && legacy_icd[0]) || (layers && layers[0]) || (preload && preload[0])',
                     'page != 4096', 'const VkDeviceSize size = REQUESTED_SIZE;',
                     '(strstr(properties.deviceName, "Virtio-GPU") || strstr(properties.deviceName, "GFXStream")) &&',
                     'strstr(properties.deviceName, "Mali") && properties.apiVersion >= VK_API_VERSION_1_1',
                     'if (matches != 1) goto done;',
                     'VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT | VK_MEMORY_PROPERTY_HOST_COHERENT_BIT',
                     '(type.propertyFlags & required) != required', 'VK_MEMORY_PROPERTY_PROTECTED_BIT',
                     '.pNext = NULL, .allocationSize = size, .memoryTypeIndex = MEMORY_TYPE_INDEX',
                     '.enabledExtensionCount = 0, .enabledLayerCount = 0, .pEnabledFeatures = NULL',
                     'vkMapMemory(device, allocation, 0, size, 0, &mapped_pointer)',
                     'address % alignment != 0', 'status = cpu_first_touch(mapped_pointer, (size_t)size);',
                     'NOT GPU user consent', 'timeout cannot guarantee recovery', 'parent source safety review',
                     'NOT GPU coherence proof', 'no root first-fault']:
        assert required in clean, required
    assert clean.index('status = cpu_first_touch(') > clean.index('address % alignment != 0')
    assert len(re.findall(r'\bmapped_pointer\b', code)) == 4
    assert len(re.findall(r'\bcpu_first_touch\s*\(', code)) == 2
    for name in ['vkCreateInstance', 'vkCreateDevice', 'vkAllocateMemory', 'vkMapMemory',
                 'vkUnmapMemory', 'vkFreeMemory', 'vkDestroyDevice', 'vkDestroyInstance']:
        assert len(re.findall(r'\b' + name + r'\s*\(', code)) == 1, name
    cleanup = [clean.index(name + '(') for name in ['vkUnmapMemory', 'vkFreeMemory', 'vkDestroyDevice', 'vkDestroyInstance']]
    assert cleanup == sorted(cleanup)
    for guard in ['if (mapped) {', 'if (allocation != VK_NULL_HANDLE) {',
                  'if (device != VK_NULL_HANDLE) {', 'if (instance != VK_NULL_HANDLE) {']:
        assert guard in code


def negative_checks(source):
    mutations = {
        'workload': source.replace('done:', 'vkQueueSubmit(0,0,0,0); done:'),
        'memcpy': source.replace('done:', 'memcpy(mapped_pointer, 0, 4096); done:'),
        'memset': source.replace('done:', 'memset(mapped_pointer, 0, 4096); done:'),
        'mmap': source.replace('done:', 'mmap(0,0,0,0,0,0); done:'),
        'extra read': source.replace('done:', 'uint32_t extra = *first; done:'),
        'extra write': source.replace('done:', '*last = 0; done:'),
        'indexed touch': source.replace('done:', 'last[1] = 0; done:'),
        'payload loop': source.replace('uint32_t initial = *first;', 'while (1) {} uint32_t initial = *first;'),
        'bad bound': source.replace('extent != REQUESTED_SIZE', 'extent == REQUESTED_SIZE'),
        'bad offset': source.replace('base + 4092u', 'base + 4096u'),
        'missing flush': source.replace('fflush(stdout);', ''),
        'missing BEGIN': source.replace('BEGIN stage=first-read ', 'OTHER stage=first-read '),
        'missing mismatch': source.replace('if (first_value != UINT32_C(0x13579bdf)) return 21;', ''),
        'missing gate': source.replace('--allow-cpu-first-touch', '--unsafe'),
        'type substitute': source.replace('MEMORY_TYPE_INDEX 8u', 'MEMORY_TYPE_INDEX 0u'),
        'larger allocation': source.replace('REQUESTED_SIZE 4096u', 'REQUESTED_SIZE 65536u'),
        'extra map': source.replace('done:', 'vkMapMemory(0,0,0,0,0,0); done:'),
    }
    for label, mutation in mutations.items():
        assert mutation != source
        try:
            check_source(mutation)
        except AssertionError:
            continue
        raise AssertionError('Unsafe mutation accepted: ' + label)
    print(f'PASS {len(mutations)} forbidden source mutations rejected')


source = pathlib.Path(sys.argv[1]).read_text()
check_source(source)
negative_checks(source)
print('PASS fixed4096 type8; exactly five volatile scalar accesses at offsets0/4092; no application workload/manual mapping; flushed stage BEGINs')
if len(sys.argv) == 4:
    names = {line.split()[-1].split('@')[0] for line in pathlib.Path(sys.argv[2]).read_text().splitlines() if line.split()}
    assert names & VULKAN == VULKAN
    assert names <= VULKAN | NATIVE_IMPORTS, names - VULKAN - NATIVE_IMPORTS
    elf = pathlib.Path(sys.argv[3]).read_text()
    assert re.search(r'Machine:\s+AArch64', elf) and re.search(r'Class:\s+ELF64', elf)
    assert '/lib/ld-linux-aarch64.so.1' in elf
    assert set(re.findall(r'\(NEEDED\).*?\[(.*?)\]', elf)) == {'libvulkan.so.1', 'libc.so.6', 'ld-linux-aarch64.so.1'}
    print('PASS AArch64 glibc ELF and import allowlist; compiler-generated aggregate memcpy/memset allowed, explicit source calls forbidden')
else:
    assert len(sys.argv) == 2
print('LIMIT structural checks not proof of driver internals/root first-fault; parent review mandatory; guest binary NEVER executed')
