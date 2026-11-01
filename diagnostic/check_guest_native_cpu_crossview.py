"""Actions-only source/ELF allowlists. Never loads Vulkan or executes the guest."""
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
C_CALLS = VULKAN | {
    'main', 'cpu_crossview_words', 'LOG', 'printf', 'putchar', 'fflush', 'strcmp',
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
    assert not re.search(r'\b(?:PFN_\w+|Vk\w*(?:Import|Export|External|Buffer|Image|Command|Shader|Fence)\w*)\b', code)
    assert not re.search(r'/dev/|/proc/|mmap|ioctl|KVM_', clean)
    helper = clean[clean.index('static int cpu_crossview_words('):clean.index('#ifndef CROSSVIEW_CPU_ONLY\n', clean.index('static int cpu_crossview_words('))]
    assert not re.search(r'\b(?:for|while|memcpy|memset)\b', helper)
    for required in ['extent != REQUESTED_SIZE', 'start % _Alignof(uint32_t) != 0',
                     'start > UINTPTR_MAX - (REQUESTED_SIZE - 1u)',
                     '4092u > extent || sizeof(uint32_t) > extent - 4092u',
                     'volatile uint32_t *first = (volatile uint32_t *)base;',
                     'volatile uint32_t *last = (volatile uint32_t *)((unsigned char *)base + 4092u);',
                     'if (!verify_reply) {',
                     'bool match = first_value == UINT32_C(0x89abcdef) && last_value == UINT32_C(0xfedcba98);',
                     'return match ? 0 : 21;']:
        assert required in helper, required
    assert helper.index('return 20;') < helper.index('volatile uint32_t *first')
    accesses = ['*first = UINT32_C(0x13579bdf);', '*last = UINT32_C(0x2468ace0);',
                'uint32_t first_value = *first;', 'uint32_t last_value = *last;']
    stages = ['request-first-write', 'request-last-write', 'reply-first-read', 'reply-last-read']
    for access, stage in zip(accesses, stages):
        assert re.search(r'LOG\("BEGIN stage=' + stage + r' [^\n]*\);\s*' + re.escape(access), helper)
    assert [helper.index(a) for a in accesses] == sorted(helper.index(a) for a in accesses)
    assert helper.index('return 0;') < helper.index(accesses[2])
    assert len(re.findall(r'\*(?:first|last)\b', code)) == 6  # two declarations, four accesses
    for name in ['first', 'last']:
        assert len(re.findall(r'\b' + name + r'\b', re.sub(r'"(?:\\.|[^"\\])*"', '""', helper))) == 3
    assert not re.search(r'\b(?:first|last|base|mapped_pointer)\s*\[', code)
    assert "putchar('\\n'); fflush(stdout);" in clean
    assert 'argc != 2 || strcmp(argv[1], "--allow-cpu-crossview") != 0' in clean
    assert clean.index('return 2;') < clean.index('getenv(') < clean.index('vkCreateInstance(')
    for required in ['#define MEMORY_TYPE_INDEX 8u', '#define REQUESTED_SIZE 4096u',
                     '#ifndef CROSSVIEW_CPU_ONLY\n#include <vulkan/vulkan.h>\n#endif',
                     '#define EXPECTED_ICD "/opt/droidvm-mesa-reference/icd.json"',
                     '#define EXPECTED_LIBRARY_PREFIX "/opt/droidvm-mesa-reference/usr/local/lib/aarch64-linux-gnu"',
                     'strcmp(icd, EXPECTED_ICD) != 0', 'strncmp(libraries, EXPECTED_LIBRARY_PREFIX, prefix_size) != 0',
                     "(libraries[prefix_size] != '\\0' && libraries[prefix_size] != ':')",
                     '(legacy_icd && legacy_icd[0]) || (layers && layers[0]) || (preload && preload[0])',
                     'page != 4096', 'const VkDeviceSize size = REQUESTED_SIZE;',
                     '.pApplicationName = "guest-native-cpu-crossview"',
                     '(strstr(properties.deviceName, "Virtio-GPU") || strstr(properties.deviceName, "GFXStream")) &&',
                     'strstr(properties.deviceName, "Mali") && properties.apiVersion >= VK_API_VERSION_1_1',
                     'if (matches != 1) goto done;',
                     'VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT | VK_MEMORY_PROPERTY_HOST_COHERENT_BIT',
                     '(type.propertyFlags & required) != required', 'VK_MEMORY_PROPERTY_PROTECTED_BIT',
                     '.pNext = NULL, .allocationSize = size, .memoryTypeIndex = MEMORY_TYPE_INDEX',
                     '.enabledExtensionCount = 0, .enabledLayerCount = 0, .pEnabledFeatures = NULL',
                     'vkMapMemory(device, allocation, 0, size, 0, &mapped_pointer)',
                     'address % alignment != 0',
                     'VkMappedMemoryRange range = {.sType = VK_STRUCTURE_TYPE_MAPPED_MEMORY_RANGE,\n        .pNext = NULL, .memory = allocation, .offset = 0, .size = size};',
                     'NOT GPU user consent', 'timeout cannot guarantee recovery', 'parent source safety review',
                     'NEW explicit CPU cross-view authorization', 'prior CPU/GPU permissions are not reused',
                     'GFXSTREAM_NATIVE_CPU_CROSSVIEW=1', 'NOT HOST_COHERENT or GPU coherence proof']:
        assert required in clean, required
    request = 'status = cpu_crossview_words(mapped_pointer, (size_t)size, false);'
    reply = 'status = cpu_crossview_words(mapped_pointer, (size_t)size, true);'
    assert clean.index('address % alignment != 0') < clean.index(request) < clean.index('vkFlushMappedMemoryRanges(') < clean.index(reply)
    assert request + '\n    if (status != 0) goto done;' in clean
    assert re.search(r'LOG\("BEGIN stage=flush [^\n]*\);\s*result = vkFlushMappedMemoryRanges\(device, 1, &range\);', clean)
    assert re.search(r'result = vkFlushMappedMemoryRanges\(device, 1, &range\);\s*LOG\([^\n]*\);\s*if \(result != VK_SUCCESS\) goto done;\s*' + re.escape(reply), clean)
    assert len(re.findall(r'\bmapped_pointer\b', code)) == 5
    assert len(re.findall(r'\bcpu_crossview_words\s*\(', code)) == 3
    for name in VULKAN - {'vkEnumeratePhysicalDevices', 'vkGetPhysicalDeviceQueueFamilyProperties'}:
        assert len(re.findall(r'\b' + name + r'\s*\(', code)) == 1, name
    cleanup = [clean.index(name + '(') for name in ['vkUnmapMemory', 'vkFreeMemory', 'vkDestroyDevice', 'vkDestroyInstance']]
    assert clean.index(reply) < cleanup[0] and cleanup == sorted(cleanup)
    for guard in ['if (mapped) {', 'if (allocation != VK_NULL_HANDLE) {',
                  'if (device != VK_NULL_HANDLE) {', 'if (instance != VK_NULL_HANDLE) {']:
        assert guard in code


def negative_checks(source):
    mutations = {
        'GPU submit': source.replace('done:', 'vkQueueSubmit(0,0,0,0); done:'),
        'invalidate': source.replace('done:', 'vkInvalidateMappedMemoryRanges(0,0,0); done:'),
        'bulk read': source.replace('done:', 'memcpy(0, mapped_pointer, 4096); done:'),
        'manual map': source.replace('done:', 'mmap(0,0,0,0,0,0); done:'),
        'extra read': source.replace('done:', 'uint32_t extra = *first; done:'),
        'extra write': source.replace('done:', '*last = 0; done:'),
        'indexed touch': source.replace('done:', 'last[1] = 0; done:'),
        'bad extent': source.replace('extent != REQUESTED_SIZE', 'extent == REQUESTED_SIZE'),
        'bad offset': source.replace('base + 4092u', 'base + 4096u'),
        'missing log flush': source.replace('fflush(stdout);', ''),
        'missing BEGIN': source.replace('BEGIN stage=flush ', 'OTHER stage=flush '),
        'self-readback false pass': source.replace('first_value == UINT32_C(0x89abcdef)', 'first_value == UINT32_C(0x13579bdf)'),
        'missing gate': source.replace('--allow-cpu-crossview', '--unsafe'),
        'type substitute': source.replace('MEMORY_TYPE_INDEX 8u', 'MEMORY_TYPE_INDEX 0u'),
        'larger allocation': source.replace('REQUESTED_SIZE 4096u', 'REQUESTED_SIZE 65536u'),
        'extra flush': source.replace('done:', 'vkFlushMappedMemoryRanges(device, 1, &range); done:'),
        'read after API error': source.replace('if (result != VK_SUCCESS) goto done;\n    status = cpu_crossview_words', 'status = cpu_crossview_words'),
    }
    assert len(mutations) == 17
    for label, mutation in mutations.items():
        assert mutation != source
        try:
            check_source(mutation)
        except AssertionError:
            continue
        raise AssertionError('Unsafe mutation accepted: ' + label)
    print('PASS 17 forbidden source mutations rejected; strict Vulkan/C API allowlists')


source = pathlib.Path(sys.argv[1]).read_text()
check_source(source)
negative_checks(source)
print('PASS fixed4096 type8; two volatile request writes and two reply reads at0/4092; one flush; API error skips reply; no workload/manual map/cache extras')
if len(sys.argv) == 4:
    names = {line.split()[-1].split('@')[0] for line in pathlib.Path(sys.argv[2]).read_text().splitlines() if line.split()}
    assert names & VULKAN == VULKAN
    assert names <= VULKAN | NATIVE_IMPORTS, names - VULKAN - NATIVE_IMPORTS
    elf = pathlib.Path(sys.argv[3]).read_text()
    assert re.search(r'Machine:\s+AArch64', elf) and re.search(r'Class:\s+ELF64', elf)
    assert '/lib/ld-linux-aarch64.so.1' in elf
    assert set(re.findall(r'\(NEEDED\).*?\[(.*?)\]', elf)) == {'libvulkan.so.1', 'libc.so.6', 'ld-linux-aarch64.so.1'}
    print('PASS AArch64 glibc ELF and import allowlist; compiler aggregate memcpy/memset allowed, explicit source calls forbidden')
else:
    assert len(sys.argv) == 2
print('LIMIT structural checks are not driver-internals/coherence proof; parent guest+host review and fresh authorization mandatory; guest binary NEVER executed')
