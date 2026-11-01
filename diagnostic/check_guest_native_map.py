"""Actions-only CPU structural checks. Never load Vulkan or execute the probe."""
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
    'main', 'LOG', 'printf', 'putchar', 'fflush', 'strcmp', 'strncmp',
    'strstr', 'getenv', 'sysconf', 'sizeof', 'if', 'for', 'while',
    'VK_API_VERSION_MAJOR', 'VK_API_VERSION_MINOR', 'VK_API_VERSION_PATCH',
}
# memcpy/memset are allowed ONLY as compiler-generated aggregate operations.
# Explicit source calls are rejected by the C-call allowlist. The mapped pointer
# is restricted to three exact uses below; no payload access is permitted.
NATIVE_IMPORTS = {
    '__libc_start_main', '__cxa_finalize', '__gmon_start__', '__stack_chk_fail',
    '_ITM_deregisterTMCloneTable', '_ITM_registerTMCloneTable',
    'printf', '__printf_chk', 'putchar', 'puts', 'putc', 'fflush', 'stdout',
    'strcmp', 'strncmp', 'strstr', 'getenv', 'sysconf', 'memcpy', 'memset',
}
TOKENS = r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|/\*.*?\*/|//[^\n]*'


def check_source(source):
    clean = re.sub(TOKENS, lambda m: '' if m[0].startswith(('/*', '//')) else m[0], source, flags=re.S)
    code = re.sub(r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'', '""', clean)
    calls = set(re.findall(r'\b([A-Za-z_]\w*)\s*\(', code))
    assert calls == C_CALLS, f'C call allowlist mismatch: {calls ^ C_CALLS}'
    vk_tokens = set(re.findall(r'\bvk[A-Z]\w*', code))
    assert vk_tokens == VULKAN, f'Vulkan token allowlist mismatch: {vk_tokens ^ VULKAN}'
    assert not re.search(r'\b(?:PFN_\w+|Vk\w*(?:Import|Export|External|Buffer|Image|Command|Shader)\w*)\b', code)
    assert not re.search(r'/dev/|/proc/|mmap|ioctl|KVM_', clean)
    assert '#define MEMORY_TYPE_INDEX 8u' in clean
    assert '#define REQUESTED_SIZE 4096u' in clean
    assert '#define MAX_DEVICES 16u' in clean and '#define MAX_FAMILIES 256u' in clean
    assert 'argc != 2 || strcmp(argv[1], "--allow-map-inspection") != 0' in clean
    assert clean.index('return 2;') < clean.index('getenv(') < clean.index('vkCreateInstance(')
    assert 'strcmp(icd, EXPECTED_ICD) != 0' in clean
    assert 'strncmp(libraries, EXPECTED_LIBRARY_PREFIX, prefix_size) != 0' in clean
    assert '#define EXPECTED_ICD "/opt/droidvm-mesa-reference/icd.json"' in clean
    assert '(legacy_icd && legacy_icd[0]) || (layers && layers[0]) || (preload && preload[0])' in clean
    assert 'page > 65536' in clean and 'page != 4096' in clean
    assert 'const VkDeviceSize size = REQUESTED_SIZE;' in clean
    assert '(strstr(properties.deviceName, "Virtio-GPU") || strstr(properties.deviceName, "GFXStream")) &&' in clean
    assert 'strstr(properties.deviceName, "Mali") && properties.apiVersion >= VK_API_VERSION_1_1' in clean
    assert 'if (matches != 1) goto done;' in clean
    assert 'VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT | VK_MEMORY_PROPERTY_HOST_COHERENT_BIT' in clean
    assert '(type.propertyFlags & required) != required' in clean
    assert clean.index('(type.propertyFlags & required) != required') < clean.index('vkCreateDevice(')
    assert re.search(r'VkMemoryAllocateInfo allocation_info = \{\.sType = VK_STRUCTURE_TYPE_MEMORY_ALLOCATE_INFO,\s*\.pNext = NULL, \.allocationSize = size, \.memoryTypeIndex = MEMORY_TYPE_INDEX\};', clean)
    assert '.enabledExtensionCount = 0, .enabledLayerCount = 0, .pEnabledFeatures = NULL' in clean
    assert '.queueCount = 1' in clean and '.queueCreateInfoCount = 1' in clean
    for name in ['vkCreateInstance', 'vkCreateDevice', 'vkAllocateMemory', 'vkMapMemory',
                 'vkUnmapMemory', 'vkFreeMemory', 'vkDestroyDevice', 'vkDestroyInstance']:
        assert len(re.findall(r'\b' + name + r'\s*\(', code)) == 1, name
    assert 'vkMapMemory(device, allocation, 0, size, 0, &mapped_pointer)' in clean
    assert re.findall(r'\bvoid\s*\*\s*\w+', code) == ['void *mapped_pointer']
    assert len(re.findall(r'\bmapped_pointer\b', code)) == 3
    for use in ['void *mapped_pointer = NULL;', '&mapped_pointer)', '(uintptr_t)mapped_pointer']:
        assert use in code, use
    assert not re.search(r'\*\s*\([^;]*uintptr_t|\baddress\s*\[|\(\s*(?:char|void|uint\w*)\s*\*\s*\)\s*address', code)
    assert 'address % alignment != 0' in clean
    assert 'if (mapped) {' in clean and 'if (allocation != VK_NULL_HANDLE) {' in clean
    assert 'if (device != VK_NULL_HANDLE) {' in clean and 'if (instance != VK_NULL_HANDLE) {' in clean
    cleanup = [clean.index(name + '(') for name in ['vkUnmapMemory', 'vkFreeMemory', 'vkDestroyDevice', 'vkDestroyInstance']]
    assert cleanup == sorted(cleanup)
    for status in [10, 11, 12, 13, 14, 15, 16, 17, 18, 19]:
        assert f'status = {status};' in clean
    assert clean.index('status = 0;') > clean.index('address % alignment != 0')
    for stage in ['cli', 'environment', 'page', 'instance', 'enumerate-count', 'enumerate-list',
                  'identity', 'type8', 'queue-family', 'device', 'allocate', 'map', 'address',
                  'unmap', 'free', 'destroy-device', 'destroy-instance', 'done']:
        assert f'stage={stage}' in clean, stage
    for marker in ['NOT GPU user consent', 'parent source safety review', 'driver-internal GPU activity',
                   'no mapped payload reads/writes', 'internal real blob may be larger', 'no substitute memory type',
                   'no payload, coherence, backing or renderer achievement']:
        assert marker in source, marker


def negative_checks(source):
    mutations = {
        'workload': source.replace('status = 0;', 'vkQueueSubmit(0, 0, 0, 0); status = 0;'),
        'explicit memcpy': source.replace('status = 0;', 'memcpy(0, 0, 1); status = 0;'),
        'payload read': source.replace('status = 0;', 'int payload = *(char *)mapped_pointer; status = 0;'),
        'payload write': source.replace('status = 0;', '*(char *)mapped_pointer = 0; status = 0;'),
        'manual mmap': source.replace('status = 0;', 'mmap(0, 0, 0, 0, 0, 0); status = 0;'),
        'manual ioctl': source.replace('status = 0;', 'ioctl(0, 0); status = 0;'),
        'missing interlock': source.replace('--allow-map-inspection', '--unsafe'),
        'substitute type': source.replace('#define MEMORY_TYPE_INDEX 8u', '#define MEMORY_TYPE_INDEX 0u'),
        'wrong size': source.replace('#define REQUESTED_SIZE 4096u', '#define REQUESTED_SIZE 65536u'),
        'missing coherent requirement': source.replace('VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT | VK_MEMORY_PROPERTY_HOST_COHERENT_BIT', 'VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT'),
        'extra allocation': source.replace('status = 0;', 'vkAllocateMemory(0, 0, 0, 0); status = 0;'),
        'missing Mali check': source.replace('strstr(properties.deviceName, "Mali")', 'true'),
    }
    for label, mutation in mutations.items():
        assert mutation != source, label
        try:
            check_source(mutation)
        except AssertionError:
            continue
        raise AssertionError(f'Unsafe mutation accepted: {label}')
    print(f'PASS CPU structural negative checks: {len(mutations)} forbidden mutations rejected; no Vulkan loaded/executed')


def check_imports(text):
    names = {line.split()[-1].split('@')[0] for line in text.splitlines() if line.split()}
    assert names & VULKAN == VULKAN, f'Missing Vulkan imports: {VULKAN - names}'
    assert names <= VULKAN | NATIVE_IMPORTS, f'Unapproved imports: {names - VULKAN - NATIVE_IMPORTS}'
    print('PASS undefined-symbol allowlist: exact Vulkan boundary; only scalar metadata/string/logging/CRT imports plus compiler aggregate memcpy/memset')


source = pathlib.Path(sys.argv[1]).read_text()
check_source(source)
negative_checks(source)
print('PASS source allowlist: one allocation/map, fixed guest type8, no explicit payload operations, workload APIs, manual mapping or KVM')
if len(sys.argv) == 4:
    check_imports(pathlib.Path(sys.argv[2]).read_text())
    elf = pathlib.Path(sys.argv[3]).read_text()
    assert re.search(r'Machine:\s+AArch64', elf)
    assert re.search(r'Class:\s+ELF64', elf)
    assert '/lib/ld-linux-aarch64.so.1' in elf
    assert set(re.findall(r'\(NEEDED\).*?\[(.*?)\]', elf)) == {'libvulkan.so.1', 'libc.so.6'}
    print('PASS ELF: native aarch64 glibc, normal libvulkan.so.1 loader linkage')
else:
    assert len(sys.argv) == 2
print('LIMIT structural checks are not proof about driver internals; parent source review still required; probe binary NOT executed')
