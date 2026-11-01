"""Actions-only textual safety gates; not a proof of Vulkan/host safety."""
import pathlib
import re
import sys

VULKAN = {
    'vkCreateInstance', 'vkDestroyInstance', 'vkEnumeratePhysicalDevices',
    'vkGetPhysicalDeviceProperties', 'vkGetPhysicalDeviceMemoryProperties',
    'vkGetPhysicalDeviceQueueFamilyProperties', 'vkCreateDevice', 'vkDestroyDevice',
    'vkGetDeviceQueue', 'vkCreateBuffer', 'vkDestroyBuffer', 'vkGetBufferMemoryRequirements',
    'vkAllocateMemory', 'vkFreeMemory', 'vkBindBufferMemory', 'vkMapMemory', 'vkUnmapMemory',
    'vkCreateCommandPool', 'vkDestroyCommandPool', 'vkAllocateCommandBuffers',
    'vkBeginCommandBuffer', 'vkEndCommandBuffer', 'vkCmdPipelineBarrier', 'vkCmdCopyBuffer',
    'vkCreateFence', 'vkDestroyFence', 'vkQueueSubmit', 'vkWaitForFences',
}
CALLS = VULKAN | {
    'main', 'pattern', 'payload_bounds', 'prepare_payload', 'verify_payload', 'quarantine',
    'LOG', 'CHECK', 'printf', 'putchar', 'fflush', 'pause', 'strcmp', 'strncmp', 'strstr',
    'getenv', 'sysconf', 'sizeof', '_Alignof', 'if', 'for', 'while', 'UINT32_C', 'UINT64_C',
}
NATIVE = {
    '__libc_start_main', '__cxa_finalize', '__gmon_start__', '__stack_chk_fail', '__stack_chk_guard',
    '_ITM_deregisterTMCloneTable', '_ITM_registerTMCloneTable', 'abort', 'printf', '__printf_chk',
    'putchar', 'puts', 'putc', 'fflush', 'stdout', 'strcmp', 'strncmp', 'strstr', 'getenv',
    'sysconf', 'pause', 'memcpy', 'memset',
}
TOKENS = r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|/\*.*?\*/|//[^\n]*'


def check(source):
    assert len(source.splitlines()) < 350
    clean = re.sub(TOKENS, lambda m: '' if m[0].startswith(('/*', '//')) else m[0], source, flags=re.S)
    code = re.sub(r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'', '""', clean)
    assert set(re.findall(r'\b([A-Za-z_]\w*)\s*\(', code)) == CALLS
    assert set(re.findall(r'\bvk[A-Z]\w*', code)) == VULKAN
    assert not re.search(r'\b(?:PFN_\w+|Vk\w*(?:Image|Shader|Import|Export|External|Surface|Swapchain)\w*)\b', code)
    assert not re.search(r'/dev/|/proc/|mmap|ioctl|KVM_', clean)
    required = [
        '#define MEMORY_TYPE_INDEX 8u', '#define BUFFER_SIZE 4096u', '#define MAX_ALLOCATION 65536u',
        '#define WORDS 64u', '#define COPY_BYTES 256u', '#define MAX_DEVICES 16u', '#define MAX_FAMILIES 256u',
        '#define EXPECTED_ICD "/opt/droidvm-mesa-reference/icd.json"',
        '#define EXPECTED_LIBRARY_PREFIX "/opt/droidvm-mesa-reference/usr/local/lib/aarch64-linux-gnu"',
        'argc != 2 || strcmp(argv[1], "--allow-gpu-roundtrip") != 0',
        'strcmp(icd, EXPECTED_ICD) != 0', 'strncmp(libraries, EXPECTED_LIBRARY_PREFIX, prefix_size) != 0',
        "(libraries[prefix_size] != '\\0' && libraries[prefix_size] != ':')",
        '(legacy_icd && legacy_icd[0]) || (layers && layers[0]) || (preload && preload[0])',
        'if (page != 4096)', 'if (matches != 1)',
        '(strstr(p.deviceName, "Virtio-GPU") || strstr(p.deviceName, "GFXStream")) &&',
        'strstr(p.deviceName, "Mali") && p.apiVersion >= VK_API_VERSION_1_1',
        'VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT | VK_MEMORY_PROPERTY_HOST_COHERENT_BIT',
        '(type.propertyFlags & required) != required', 'VK_MEMORY_PROPERTY_PROTECTED_BIT',
        'type.heapIndex >= mp.memoryHeapCount', '.pEnabledFeatures = NULL',
        'VK_QUEUE_TRANSFER_BIT | VK_QUEUE_GRAPHICS_BIT | VK_QUEUE_COMPUTE_BIT',
        'VkBuffer buffers[2]', 'VkDeviceMemory memory[2]', 'VkMemoryRequirements requirements[2]',
        '.usage = VK_BUFFER_USAGE_TRANSFER_SRC_BIT | VK_BUFFER_USAGE_TRANSFER_DST_BIT',
        'requirements[i].size < BUFFER_SIZE || requirements[i].size > MAX_ALLOCATION',
        '!requirements[i].alignment', 'requirements[i].size % requirements[i].alignment != 0',
        'requirements[0].memoryTypeBits & requirements[1].memoryTypeBits & (1u << MEMORY_TYPE_INDEX)',
        'requirements[0].size + requirements[1].size > mp.memoryHeaps[type.heapIndex].size',
        '.pNext = NULL, .allocationSize = requirements[i].size, .memoryTypeIndex = MEMORY_TYPE_INDEX',
        'vkBindBufferMemory(device, buffers[i], memory[i], 0)',
        'vkMapMemory(device, memory[i], 0, BUFFER_SIZE, 0, &mapped[i])',
        '!alignment || address % alignment || !payload_bounds(mapped[i], BUFFER_SIZE)',
        'base && extent == BUFFER_SIZE && (uintptr_t)base % _Alignof(uint32_t) == 0',
        '(uintptr_t)base <= UINTPTR_MAX - COPY_BYTES',
        '(uintptr_t)source + COPY_BYTES <= (uintptr_t)destination',
        '(uintptr_t)destination + COPY_BYTES <= (uintptr_t)source',
        'src[i] = pattern(i); dst[i] = ~pattern(i);',
        'const volatile uint32_t *dst = destination;', 'uint32_t value = dst[i];',
        'if (value != pattern(i))', 'return mismatches ? 21 : 0;',
        '.commandBufferCount = 1', '.flags = VK_COMMAND_BUFFER_USAGE_ONE_TIME_SUBMIT_BIT',
        '.srcAccessMask = VK_ACCESS_HOST_WRITE_BIT',
        '.dstAccessMask = VK_ACCESS_TRANSFER_READ_BIT | VK_ACCESS_TRANSFER_WRITE_BIT',
        'VK_PIPELINE_STAGE_HOST_BIT, VK_PIPELINE_STAGE_TRANSFER_BIT',
        'VkBufferCopy copy = {.srcOffset = 0, .dstOffset = 0, .size = COPY_BYTES}',
        'vkCmdCopyBuffer(command, buffers[0], buffers[1], 1, &copy)',
        '.srcAccessMask = VK_ACCESS_TRANSFER_WRITE_BIT, .dstAccessMask = VK_ACCESS_HOST_READ_BIT',
        'VK_PIPELINE_STAGE_TRANSFER_BIT, VK_PIPELINE_STAGE_HOST_BIT',
        'vkQueueSubmit(queue, 1, &submit, fence)', 'if (result != VK_SUCCESS) quarantine(40, result);',
        'vkWaitForFences(device, 1, &fence, VK_TRUE, UINT64_C(1000000000))',
        'if (result != VK_SUCCESS) quarantine(41, result);',
        'if (result == VK_ERROR_DEVICE_LOST) quarantine(42, result);',
        'static _Noreturn void quarantine(', 'for (;;) pause();',
        'status = verify_payload(mapped[1], BUFFER_SIZE);',
        "putchar('\\n'); fflush(stdout);", 'prior CPU first-touch approval is NOT GPU consent',
        'timeout cannot guarantee recovery', 'do NOT kill probe or retry',
    ]
    for text in required:
        assert text in clean, text
    assert clean.index('return 2;') < clean.index('getenv(') < clean.index('vkCreateInstance(')
    for call in VULKAN - {'vkEnumeratePhysicalDevices', 'vkGetPhysicalDeviceQueueFamilyProperties', 'vkCmdPipelineBarrier'}:
        assert len(re.findall(r'\b' + call + r'\s*\(', code)) == 1, call
    for call in ['vkEnumeratePhysicalDevices', 'vkGetPhysicalDeviceQueueFamilyProperties', 'vkCmdPipelineBarrier']:
        assert len(re.findall(r'\b' + call + r'\s*\(', code)) == 2, call
    # Two buffers/maps/allocations at one lexical call site, not unbounded loops.
    assert code.count('for (uint32_t i = 0; i < 2; ++i)') == 3
    assert len(re.findall(r'\bfor\s*\(', code)) == 7  # 3 resource, 2 payload, 1 devices, 1 quarantine
    ordered = ['status = prepare_payload(', 'vkCmdPipelineBarrier(', 'vkCmdCopyBuffer(',
               'CHECK("end",', 'vkQueueSubmit(', 'quarantine(40, result)', 'vkWaitForFences(',
               'quarantine(41, result)', 'GPU_COMPLETED=1', 'status = verify_payload(', 'done:', 'vkDestroyFence(',
               'vkDestroyCommandPool(', 'vkUnmapMemory(', 'vkDestroyBuffer(', 'vkFreeMemory(', 'vkDestroyDevice(', 'vkDestroyInstance(']
    positions = [clean.index(text) for text in ordered]
    assert positions == sorted(positions)
    quarantine = code[code.index('static _Noreturn void quarantine('):code.index('#define CHECK')]
    assert not re.search(r'\b(?:vk[A-Z]\w*|return|exit|_Exit|free)\b', quarantine)
    assert len(re.findall(r'\bvk\w+', quarantine)) == 0
    assert clean.count('GPU_COMPLETED=1') == 1


def negative_checks(source):
    mutations = {
        'extra submit': ('done:', 'vkQueueSubmit(0,0,0,0); done:'),
        'idle wait': ('done:', 'vkDeviceWaitIdle(device); done:'),
        'extra workload': ('done:', 'vkCmdFillBuffer(0,0,0,0,0); done:'),
        'manual mapping': ('done:', 'mmap(0,0,0,0,0,0); done:'),
        'larger buffer': ('BUFFER_SIZE 4096u', 'BUFFER_SIZE 65536u'),
        'larger allocation': ('MAX_ALLOCATION 65536u', 'MAX_ALLOCATION 131072u'),
        'larger copy': ('COPY_BYTES 256u', 'COPY_BYTES 4096u'),
        'type substitute': ('MEMORY_TYPE_INDEX 8u', 'MEMORY_TYPE_INDEX 0u'),
        'same destination': ('dst[i] = ~pattern(i)', 'dst[i] = pattern(i)'),
        'removed bounds': ('extent == BUFFER_SIZE', 'extent >= BUFFER_SIZE'),
        'missing compare': ('if (value != pattern(i))', 'if (0)'),
        'missing type intersection': ('requirements[1].memoryTypeBits &', ''),
        'infinite wait': ('UINT64_C(1000000000)', 'UINT64_MAX'),
        'timeout cleanup': ('quarantine(41, result)', 'goto done'),
        'submit cleanup': ('quarantine(40, result)', 'goto done'),
        'device lost cleanup': ('quarantine(42, result)', 'goto done'),
        'exit pending': ('for (;;) pause();', '_Exit(41);'),
        'cleanup pending': ('for (;;) pause();', 'vkDestroyDevice(0,0); for (;;) pause();'),
        'missing flush': ('fflush(stdout);', ''),
        'missing interlock': ('--allow-gpu-roundtrip', '--unsafe'),
    }
    for label, (old, new) in mutations.items():
        mutation = source.replace(old, new)
        assert mutation != source, label
        try:
            check(mutation)
        except AssertionError:
            continue
        raise AssertionError('Unsafe mutation accepted: ' + label)
    print(f'PASS {len(mutations)} unsafe source mutations rejected')


source = pathlib.Path(sys.argv[1]).read_text()
check(source)
negative_checks(source)
print('PASS bounded two4096 buffers/type8 exact requirement intersection/one256 copy/one submit/one1s fence/quarantine paths; textual checks only')
if len(sys.argv) == 4:
    names = {line.split()[-1].split('@')[0] for line in pathlib.Path(sys.argv[2]).read_text().splitlines() if line.split()}
    assert names & VULKAN == VULKAN
    assert names <= VULKAN | NATIVE, names - VULKAN - NATIVE
    elf = pathlib.Path(sys.argv[3]).read_text()
    assert re.search(r'Machine:\s+AArch64', elf) and re.search(r'Class:\s+ELF64', elf)
    assert '/lib/ld-linux-aarch64.so.1' in elf
    needed = set(re.findall(r'\(NEEDED\).*?\[(.*?)\]', elf))
    assert {'libvulkan.so.1', 'libc.so.6'} <= needed <= {'libvulkan.so.1', 'libc.so.6', 'ld-linux-aarch64.so.1'}
    print('PASS AArch64 glibc ELF dependencies and static import allowlist; guest NEVER executed')
else:
    assert len(sys.argv) == 2
print('LIMIT not proof of driver internals or safe teardown; separate parent GPU authorization and source safety review mandatory')
