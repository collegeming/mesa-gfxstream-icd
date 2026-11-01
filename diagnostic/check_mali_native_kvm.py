"""Actions-only static boundary assertions; no binary execution."""
import pathlib
import re
import sys

source = pathlib.Path(sys.argv[1]).read_text()
clean = re.sub(r'/\*.*?\*/|//[^\n]*', '', source, flags=re.S)
code = re.sub(r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'', '""', clean)
vulkan_allowed = {
    'vkCreateInstance', 'vkDestroyInstance', 'vkEnumeratePhysicalDevices',
    'vkGetPhysicalDeviceProperties', 'vkGetPhysicalDeviceMemoryProperties',
    'vkGetPhysicalDeviceQueueFamilyProperties', 'vkCreateDevice', 'vkDestroyDevice',
    'vkAllocateMemory', 'vkMapMemory', 'vkUnmapMemory', 'vkFreeMemory',
}
assert set(re.findall(r'\b(vk\w+)\s*\(', code)) == vulkan_allowed
allowed = vulkan_allowed | {
    'LOG', 'do', 'while', 'if', 'for', 'sizeof', '_Static_assert', 'main', 'parse_vma',
    'contains', 'print_vma', 'inspect_vma', 'register_empty_vm', 'printf', 'putchar',
    'fflush', 'sscanf', 'fopen', 'fgets', 'ferror', 'fclose', 'strcmp', 'strncmp',
    'setvbuf', 'uname', 'sysconf', 'open', 'close', 'ioctl', 'UINT64_C',
    'VK_API_VERSION_MAJOR', 'VK_API_VERSION_MINOR', 'VK_API_VERSION_PATCH',
}
assert set(re.findall(r'\b([A-Za-z_]\w*)\s*\(', code)) <= allowed
assert re.findall(r'"(/proc/[^"\n]*)"', source) == ['/proc/self/maps', '/proc/self/smaps']
assert re.findall(r'"(/dev/[^"\n]*)"', source) == ['/dev/kvm']
assert re.findall(r'fopen\(([^;]+)\)', clean) == ['"/proc/self/maps", "re"', '"/proc/self/smaps", "re"']
assert re.findall(r'\bopen\(([^;]+)\)', clean) == ['"/dev/kvm", O_RDWR | O_CLOEXEC']
assert re.findall(r'\bioctl\(([^;]+)\)', code) == [
    'kvm, KVM_GET_API_VERSION, 0', 'kvm, KVM_CREATE_VM, 0',
    'vm, KVM_SET_USER_MEMORY_REGION, &region', 'vm, KVM_SET_USER_MEMORY_REGION, &region',
]
assert set(re.findall(r'\bKVM_[A-Z_0-9]+', code)) == {
    'KVM_API_VERSION', 'KVM_GET_API_VERSION', 'KVM_CREATE_VM', 'KVM_SET_USER_MEMORY_REGION',
}
assert '#include <linux/kvm.h>' in source
for abi in ['KVM_API_VERSION == 12', 'KVM_GET_API_VERSION == 0xae00', 'KVM_CREATE_VM == 0xae01', 'KVM_SET_USER_MEMORY_REGION == 0x4020ae46', 'sizeof(struct kvm_userspace_memory_region) == 32']:
    assert abi in source
assert '#define MEMORY_TYPE_INDEX 8u' in source
assert '#define EXPECTED_DEVICE "Mali-G2-Ultra-NX MC16"' in source
assert 'properties.vendorID == 0x13b5' in source and 'matches != 1' in source
assert 'page > 65536' in source and 'page > 4096 ? (uint64_t)page : 4096' in source
assert 'VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT | VK_MEMORY_PROPERTY_HOST_COHERENT_BIT' in source
assert '(type.propertyFlags & required) != required' in source
assert re.search(r'VkMemoryAllocateInfo allocation_info = \{\.sType = VK_STRUCTURE_TYPE_MEMORY_ALLOCATE_INFO,\s*\.pNext = NULL, \.allocationSize = size, \.memoryTypeIndex = MEMORY_TYPE_INDEX\};', clean)
assert '.enabledExtensionCount = 0, .enabledLayerCount = 0, .pEnabledFeatures = NULL' in source
assert '.queueCount = 1' in source and '.queueCreateInfoCount = 1' in source
for name in ['vkAllocateMemory', 'vkMapMemory', 'vkUnmapMemory', 'vkFreeMemory']:
    assert len(re.findall(r'\b' + name + r'\s*\(', code)) == 1
assert 'vkMapMemory(device, allocation, 0, size, 0, &mapped_pointer)' in source
assert len(re.findall(r'\bmapped_pointer\b', code)) == 3
assert 'void *mapped_pointer = NULL;' in code and '(uintptr_t)mapped_pointer' in code
for guard in ['size != (uint64_t)page', 'size % (uint64_t)page', 'address % (uintptr_t)page', 'UINT64_C(0x100000) % (uint64_t)page', 'size > UINTPTR_MAX - address', 'size > UINT64_MAX - UINT64_C(0x100000)', 'requested_size <= maps.end - pointer', 'requested_size <= smaps.end - pointer']:
    assert guard in code
assert re.search(r'\.slot = 0, \.flags = 0, \.guest_phys_addr = UINT64_C\(0x100000\),\s*\.memory_size = size, \.userspace_addr = \(uint64_t\)address', code)
assert code.count('region.memory_size = 0;') == 1
assert code.count('close(vm)') == 1 and code.count('close(kvm)') == 1
helper = code[code.index('static int register_empty_vm'):code.index('int main')]
assert helper.count('return status;') == 2
assert 'if (kvm < 0) return status;' in helper
assert helper.index('region.memory_size = 0;') < helper.index('close(vm)') < helper.index('close(kvm)') < helper.rindex('return status;')
assert 'if (vm >= 0)' in helper
assert 'if (result != 12) goto close_fds;' in helper
assert 'if (vm < 0) goto close_fds;' in helper
assert 'if (result != 0) goto close_fds;' in helper
assert 'if (!inspect_vma(address, size)) { status = 2; goto done; }' in code
assert code.index('status = register_empty_vm(address, size);') < code.index('vkUnmapMemory(device') < code.index('vkFreeMemory(device') < code.index('vkDestroyDevice(device') < code.index('vkDestroyInstance(instance')
assert not re.search(r'pagemap|PFN_vk|vkGet\w*ProcAddr|Vk\w*(?:Import|Export|External)', clean)
for marker in ['registration success != fault/pfn/coherence proof', 'KVM might validate only VMA', 'NOT successful unregister', 'no mapped payload reads/writes', 'no existing VM touched', 'NOT Vulkan allocation extent', 'VmFlags:', 'KernelPageSize:', 'MMUPageSize:']:
    assert marker in source
print('PASS source: fixed type8 one map; proc metadata; exact single-page slot0; only GET_API_VERSION/CREATE_VM/basic SET_USER_MEMORY_REGION; no vCPU/data/execution')
print('PASS lifetime assertions: successful registration has same-slot size0 unregister; failed unregister closes sole empty-VM fd; VM+KVM closed before Vulkan cleanup')
if len(sys.argv) > 2:
    lines = pathlib.Path(sys.argv[2]).read_text().splitlines()
    names = {line.split()[-1].split('@')[0] for line in lines if line.split()}
    assert {name for name in names if name.startswith('vk')} == vulkan_allowed
    forbidden = r'(?:syscall|mmap\w*|munmap|mprotect|openat\w*|open64|read|pread\w*|write|pwrite\w*|process_vm_\w*|ptrace|system|popen|exec\w*|fork|vfork|clone\w*|dlopen|dlsym|socket|connect|dup\w*)'
    assert not {name for name in names if re.fullmatch(forbidden, name)}
    assert {'open', 'close', 'ioctl'} <= names
    print('PASS symbols: exact Vulkan imports; no manual mapping, payload I/O, fork/exec/dlopen, VM duplication or workload imports')
print('LIMIT static assertions + human review only; binary NOT executed; not proof about driver internals or PFNs/coherence')
