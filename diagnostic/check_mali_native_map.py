"""Actions-only static boundary assertions; never executes the probe."""
import pathlib
import re
import sys

source = pathlib.Path(sys.argv[1]).read_text()
clean = re.sub(r'/\*.*?\*/|//[^\n]*', '', source, flags=re.S)
vulkan_allowed = {
    'vkCreateInstance', 'vkDestroyInstance', 'vkEnumeratePhysicalDevices',
    'vkGetPhysicalDeviceProperties', 'vkGetPhysicalDeviceMemoryProperties',
    'vkGetPhysicalDeviceQueueFamilyProperties', 'vkCreateDevice',
    'vkDestroyDevice', 'vkAllocateMemory', 'vkMapMemory', 'vkUnmapMemory', 'vkFreeMemory',
}
calls = set(re.findall(r'\b(vk\w+)\s*\(', clean))
assert calls == vulkan_allowed, f'Unexpected/missing Vulkan calls: {calls ^ vulkan_allowed}'
# Strip strings before identifying C call syntax, so logging text is not a call.
code = re.sub(r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'', '""', clean)
all_calls = set(re.findall(r'\b([A-Za-z_]\w*)\s*\(', code))
allowed = vulkan_allowed | {
    'LOG', 'do', 'while', 'if', 'for', 'sizeof', 'main', 'parse_vma', 'contains',
    'print_vma', 'inspect_vma', 'printf', 'putchar', 'fflush', 'sscanf', 'fopen',
    'fgets', 'ferror', 'fclose', 'strcmp', 'strncmp', 'setvbuf', 'uname', 'sysconf',
    'VK_API_VERSION_MAJOR', 'VK_API_VERSION_MINOR', 'VK_API_VERSION_PATCH',
}
assert all_calls <= allowed, f'Unexpected C calls: {all_calls - allowed}'
proc_paths = re.findall(r'"(/proc/[^"\n]*)"', source)
assert proc_paths == ['/proc/self/maps', '/proc/self/smaps'], proc_paths
assert not re.search(r'/dev/|pagemap|PFN_vk|vkGet\w*ProcAddr|Vk\w*(?:Import|Export|External)', clean)
assert re.findall(r'fopen\(([^;]+)\)', clean) == [
    '"/proc/self/maps", "re"', '"/proc/self/smaps", "re"']
assert '#define MEMORY_TYPE_INDEX 8u' in source
assert '#define EXPECTED_DEVICE "Mali-G2-Ultra-NX MC16"' in source
assert 'properties.vendorID == 0x13b5' in source
assert 'matches != 1' in source
assert 'page > 65536' in source and 'page > 4096 ? (uint64_t)page : 4096' in source
assert 'VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT | VK_MEMORY_PROPERTY_HOST_COHERENT_BIT' in source
assert '(type.propertyFlags & required) != required' in source
assert re.search(r'VkMemoryAllocateInfo allocation_info = \{\.sType = VK_STRUCTURE_TYPE_MEMORY_ALLOCATE_INFO,\s*\.pNext = NULL, \.allocationSize = size, \.memoryTypeIndex = MEMORY_TYPE_INDEX\};', clean)
assert re.search(r'\.enabledExtensionCount = 0, \.enabledLayerCount = 0, \.pEnabledFeatures = NULL', clean)
assert '.queueCount = 1' in source and '.queueCreateInfoCount = 1' in source
for name in ['vkAllocateMemory', 'vkMapMemory', 'vkUnmapMemory', 'vkFreeMemory']:
    assert len(re.findall(r'\b' + name + r'\s*\(', clean)) == 1, name
assert 'vkMapMemory(device, allocation, 0, size, 0, &mapped_pointer)' in source
# The payload pointer has only declaration, map output, and pointer-to-integer conversion.
assert len(re.findall(r'\bmapped_pointer\b', code)) == 3
assert 'void *mapped_pointer = NULL;' in code
assert '(uintptr_t)mapped_pointer' in code
assert clean.index('if (mapped) { vkUnmapMemory') < clean.index('vkFreeMemory(device') < clean.index('vkDestroyDevice(device') < clean.index('vkDestroyInstance(instance')
for marker in ['NOT Vulkan allocation extent', 'driver-internal GPU activity', 'no mapped payload reads/writes', 'VmFlags:', 'KernelPageSize:', 'MMUPageSize:']:
    assert marker in source, marker
print('PASS source: fixed type8 ordinary allocation; one map/unmap; proc text only; no payload access or workload API')
if len(sys.argv) > 2:
    symbols = pathlib.Path(sys.argv[2]).read_text()
    names = {line.split()[-1].split('@')[0] for line in symbols.splitlines() if line.split()}
    vk_symbols = {name for name in names if name.startswith('vk')}
    assert vk_symbols == vulkan_allowed, f'Unexpected/missing Vulkan imports: {vk_symbols ^ vulkan_allowed}'
    forbidden = r'(?:ioctl|syscall|mmap\w*|munmap|mprotect|open\w*|read|pread\w*|write|pwrite\w*|process_vm_\w*|ptrace|system|popen|exec\w*|dlopen|dlsym|socket|connect)'
    bad = {name for name in names if re.fullmatch(forbidden, name)}
    assert not bad, f'Forbidden native imports: {bad}'
    # Compiler-generated stack aggregate initialization/copy may import memset/memcpy;
    # explicit source calls are rejected above. Neither is a payload-access permission.
    print('PASS symbols: exact Vulkan API allowlist; no raw I/O, mapping, dynamic dispatch, KVM or workload imports')
print('LIMIT static assertions plus human review; not a proof about Vulkan driver internals; binary NOT executed')
