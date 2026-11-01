#!/usr/bin/env python3
"""Bounded virtio resource create/CB/attach diagnostics; no behavior changes."""
import argparse
from pathlib import Path
p = argparse.ArgumentParser(description=__doc__)
p.add_argument('root', type=Path)
root = p.parse_args().root.resolve()
def edit(rel, old, new):
    path = root/rel
    text = path.read_text()
    if text.count(old) != 1:
        raise SystemExit(f'{rel}: expected one anchor ({text.count(old)}): {old[:100]}')
    path.write_text(text.replace(old, new))

frontend = 'host/virtio_gpu_frontend.cpp'
edit(frontend, '''int VirtioGpuFrontend::createResource(struct stream_renderer_resource_create_args* args,
                                      struct iovec* iov, uint32_t num_iovs) {
    auto resourceOpt = VirtioGpuResource::Create(args, iov, num_iovs);
    if (!resourceOpt) {
        GFXSTREAM_ERROR("Failed to create resource %u.", args->handle);
        return -EINVAL;
    }
    mResources[args->handle] = std::move(*resourceOpt);
    return 0;
}''', '''int VirtioGpuFrontend::createResource(struct stream_renderer_resource_create_args* args,
                                      struct iovec* iov, uint32_t num_iovs) {
    const bool diag = gfxstream::base::getEnvironmentVariable("GFXSTREAM_NIRI_IMAGE_TRACE") == "1";
    if (diag) {
        static std::atomic<uint32_t> argsDiagnostics{0};
        if (argsDiagnostics.fetch_add(1, std::memory_order_relaxed) < 16) {
            GFXSTREAM_INFO("CIB1-DIAG-RES-CREATE-ARGS: handle=%u target=%u format=%u bind=0x%x "
                           "width=%u height=%u depth=%u array=%u last-level=%u samples=%u flags=0x%x iovs=%u",
                           args->handle, args->target, args->format, args->bind, args->width,
                           args->height, args->depth, args->array_size, args->last_level,
                           args->nr_samples, args->flags, num_iovs);
        }
    }
    const bool existed = mResources.find(args->handle) != mResources.end();
    auto resourceOpt = VirtioGpuResource::Create(args, iov, num_iovs);
    if (diag) {
        static std::atomic<uint32_t> resultDiagnostics{0};
        if (resultDiagnostics.fetch_add(1, std::memory_order_relaxed) < 16) {
            uint32_t stride = 0;
            if (resourceOpt) {
                struct stream_renderer_resource_info info{};
                if (resourceOpt->GetInfo(&info) == 0) stride = info.stride;
            }
            GFXSTREAM_INFO("CIB1-DIAG-RES-CREATE-RESULT: handle=%u created=%d existed=%d "
                           "stride=%u map-size=%zu",
                           args->handle, resourceOpt.has_value(), existed, stride, mResources.size());
        }
    }
    if (!resourceOpt) {
        GFXSTREAM_ERROR("Failed to create resource %u.", args->handle);
        return -EINVAL;
    }
    mResources[args->handle] = std::move(*resourceOpt);
    return 0;
}''')

edit(frontend, '''void VirtioGpuFrontend::attachResource(uint32_t contextId, uint32_t resourceId) {
    D("ctxid: %u resid: %u", contextId, resourceId);

    auto contextIt = mContexts.find(contextId);
    if (contextIt == mContexts.end()) {
        GFXSTREAM_ERROR("failed to attach resource %u to context %u: context not found.",
                        resourceId, contextId);
        return;
    }
    auto& context = contextIt->second;

    auto resourceIt = mResources.find(resourceId);
    if (resourceIt == mResources.end()) {
        GFXSTREAM_ERROR("failed to attach resource %u to context %u: resource not found.",
                        resourceId, contextId);
        return;
    }
    auto& resource = resourceIt->second;

    context.AttachResource(resource);
}''', '''void VirtioGpuFrontend::attachResource(uint32_t contextId, uint32_t resourceId) {
    D("ctxid: %u resid: %u", contextId, resourceId);

    auto contextIt = mContexts.find(contextId);
    auto resourceIt = mResources.find(resourceId);
    if (gfxstream::base::getEnvironmentVariable("GFXSTREAM_NIRI_IMAGE_TRACE") == "1") {
        static std::atomic<uint32_t> diagnostics{0};
        if (diagnostics.fetch_add(1, std::memory_order_relaxed) < 16) {
            GFXSTREAM_INFO("CIB1-DIAG-ATTACH: context=%u resource=%u context-found=%d "
                           "resource-found=%d map-size=%zu",
                           contextId, resourceId, contextIt != mContexts.end(),
                           resourceIt != mResources.end(), mResources.size());
        }
    }
    if (contextIt == mContexts.end()) {
        GFXSTREAM_ERROR("failed to attach resource %u to context %u: context not found.",
                        resourceId, contextId);
        return;
    }
    auto& context = contextIt->second;

    if (resourceIt == mResources.end()) {
        GFXSTREAM_ERROR("failed to attach resource %u to context %u: resource not found.",
                        resourceId, contextId);
        return;
    }
    auto& resource = resourceIt->second;

    context.AttachResource(resource);
}''')

resource = 'host/virtio_gpu_resource.cpp'
edit(resource, '''    } else if (resourceType == VirtioGpuResourceType::COLOR_BUFFER) {
        auto formatOpt = ToGfxstreamFormat(args->format);
        if (!formatOpt) {
            GFXSTREAM_ERROR("Failed to create resource: unsupported format %d", args->format);
            return std::nullopt;
        }
        auto format = *formatOpt;
''', '''    } else if (resourceType == VirtioGpuResourceType::COLOR_BUFFER) {
        const char* trace = std::getenv("GFXSTREAM_NIRI_IMAGE_TRACE");
        const bool diag = trace && std::strcmp(trace, "1") == 0;
        auto formatOpt = ToGfxstreamFormat(args->format);
        if (!formatOpt) {
            if (diag) {
                static std::atomic<uint32_t> formatDiagnostics{0};
                if (formatDiagnostics.fetch_add(1, std::memory_order_relaxed) < 16) {
                    GFXSTREAM_INFO("CIB1-DIAG-RES-CB-FORMAT: handle=%u virgl-format=%u "
                                   "format-string=unsupported",
                                   args->handle, args->format);
                }
            }
            GFXSTREAM_ERROR("Failed to create resource: unsupported format %d", args->format);
            return std::nullopt;
        }
        auto format = *formatOpt;
        if (diag) {
            static std::atomic<uint32_t> createDiagnostics{0};
            if (createDiagnostics.fetch_add(1, std::memory_order_relaxed) < 16) {
                GFXSTREAM_INFO("CIB1-DIAG-RES-CB-CREATE: handle=%u target=%u virgl-format=%u "
                               "format=%s width=%u height=%u depth=%u flags=0x%x",
                               args->handle, args->target, args->format, ToString(format).c_str(),
                               args->width, args->height, args->depth, args->flags);
            }
        }
''')

edit(resource, '''        if (!FrameBuffer::getFB()->createColorBufferWithResourceHandle(args->width, args->height,
                                                                       format, args->handle)) {
            const std::string formatString = ToString(format);
            GFXSTREAM_ERROR("Failed to create color buffer with resource handle %d. (%dx%d, format: %s)",
                            args->handle, args->width, args->height, formatString.c_str());
            return std::nullopt;
        }''', '''        const bool cbCreated = FrameBuffer::getFB()->createColorBufferWithResourceHandle(
            args->width, args->height, format, args->handle);
        if (diag) {
            static std::atomic<uint32_t> resultDiagnostics{0};
            if (resultDiagnostics.fetch_add(1, std::memory_order_relaxed) < 16) {
                GFXSTREAM_INFO("CIB1-DIAG-RES-CB-RESULT: handle=%u format=%s created=%d",
                               args->handle, ToString(format).c_str(), cbCreated);
            }
        }
        if (!cbCreated) {
            const std::string formatString = ToString(format);
            GFXSTREAM_ERROR("Failed to create color buffer with resource handle %d. (%dx%d, format: %s)",
                            args->handle, args->width, args->height, formatString.c_str());
            return std::nullopt;
        }''')

print('Added bounded virtio resource create/CB/attach diagnostics')
