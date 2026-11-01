#!/system/bin/sh
# Immutable candidate only; caller supplies the existing crosvm argv unchanged.
root=/data/local/tmp/droidvm-native-pair-v1
fail() { printf '%s\n' "native-pair: $*" >&2; exit 2; }
[ -f "$root/bin/crosvm" ] && [ -x "$root/bin/crosvm" ] || fail 'candidate executable missing'
[ -f "$root/lib/libgfxstream_backend.so" ] && [ -r "$root/lib/libgfxstream_backend.so" ] || fail 'host library unreadable'

pending=0
count=0
for arg do
    case "$arg" in
        --vnc-server|--vnc-server=*|--android-display-service|--android-display-service=*)
            fail 'display exporters must be disabled in caller config' ;;
    esac
    if [ "$pending" -eq 1 ]; then
        value=$arg
        pending=0
    else
        case "$arg" in
            --gpu) pending=1 ;;
            --gpu=*) value=${arg#--gpu=} ;;
            *) continue ;;
        esac
        count=$((count + 1))
        [ "$count" -eq 1 ] || fail 'duplicate --gpu'
        [ "$pending" -eq 0 ] || continue
    fi
    case "$value" in
        ''|--*) fail 'missing --gpu value' ;;
    esac
    case ",$value," in
        *',udmabuf=true,'*|*',implicit-render-server=true,'*|*',fixed-blob-mapping=true,'*)
            fail 'caller must disable udmabuf, implicit render server and fixed blob mapping' ;;
    esac
done
[ "$pending" -eq 0 ] || fail 'missing --gpu value'

export LD_LIBRARY_PATH="$root/lib"
# Preserve inherited LD_PRELOAD (app libsimpledump.so + libcompat_a17.so).
export ANDROID_EMU_VK_LOADER_PATH=/system/lib64/libvulkan.so
export GFXSTREAM_EXTERNAL_MEMORY_MODE=AndroidAHB
export GFXSTREAM_RENDERER_FEATURES='ExternalBlob:disabled,SystemBlob:disabled,VulkanAllocateHostVisibleAsUdmabuf:disabled,VulkanAllocateHostMemory:disabled,VirtioGpuNext:enabled,ClassicImageBridge:enabled'
unset GFXSTREAM_DEVICE_LOCAL_MEMORY_TYPE
export GFXSTREAM_GUNYAH_PIN_RINGBLOB=1
export GPU_DISPLAY_COPY_MODE=cpu
unset CROSVM_DISPLAY_VULKAN_LIBRARY
exec "$root/bin/crosvm" --log-level 'info,devices::virtio::gpu::virtio_gpu=debug,vm_control=debug' "$@"
