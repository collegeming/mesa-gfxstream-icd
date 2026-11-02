#!/usr/bin/env python3
"""Log image<->buffer copy parameters (bounded) to trace screenshot readback.

The screenshot pixels do not come from the CIB1 ColorBuffers (those dump as
zero), so the actual readback path must be identified from its copy parameters:
image/buffer identity, bufferOffset, bufferRowLength, imageExtent and
imageOffset. Log the first N copies in both directions; the screenshot's
vkCmdCopyImageToBuffer is the readback that produces the PNG.
"""
import argparse
from pathlib import Path

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('root', type=Path)
root = parser.parse_args().root.resolve()

d = root / 'host/vulkan/vk_decoder_global_state.cpp'
text = d.read_text(encoding='utf-8')

OLD = '''        std::lock_guard<std::mutex> lock(mMutex);
        auto* imageInfo = gfxstream::base::find(mImageInfo, srcImage);
        auto* bufferInfo = gfxstream::base::find(mBufferInfo, dstBuffer);'''
NEW = '''        std::lock_guard<std::mutex> lock(mMutex);
        {
            // Bounded readback trace: the screenshot path is the copy whose
            // parameters must match the guest's mmap/read buffer.
            static std::atomic<unsigned> sCopyImgToBuf{0};
            if (sCopyImgToBuf.fetch_add(1, std::memory_order_relaxed) < 48 && regionCount) {
                const VkBufferImageCopy& r0 = pRegions[0];
                GFXSTREAM_WARNING(
                    "COPY-IMG2BUF: image=%p layout=%d buffer=%p regions=%u bufOff=%llu "
                    "rowLen=%u imgH=%u extent=%ux%ux%u imgOff=%d,%d,%d",
                    (void*)srcImage, (int)srcImageLayout, (void*)dstBuffer, regionCount,
                    (unsigned long long)r0.bufferOffset, r0.bufferRowLength,
                    r0.bufferImageHeight, r0.imageExtent.width, r0.imageExtent.height,
                    r0.imageExtent.depth, r0.imageOffset.x, r0.imageOffset.y,
                    r0.imageOffset.z);
            }
        }
        auto* imageInfo = gfxstream::base::find(mImageInfo, srcImage);
        auto* bufferInfo = gfxstream::base::find(mBufferInfo, dstBuffer);'''
if text.count(OLD) != 1:
    raise SystemExit(f'expected one img2buf anchor, found {text.count(OLD)}')
text = text.replace(OLD, NEW)

OLD = '''        std::lock_guard<std::mutex> lock(mMutex);
        auto* imageInfo = gfxstream::base::find(mImageInfo, dstImage);'''
NEW = '''        std::lock_guard<std::mutex> lock(mMutex);
        {
            static std::atomic<unsigned> sCopyBufToImg{0};
            if (sCopyBufToImg.fetch_add(1, std::memory_order_relaxed) < 48 && regionCount) {
                const VkBufferImageCopy& r0 = pRegions[0];
                GFXSTREAM_WARNING(
                    "COPY-BUF2IMG: buffer=%p image=%p layout=%d regions=%u bufOff=%llu "
                    "rowLen=%u imgH=%u extent=%ux%ux%u imgOff=%d,%d,%d",
                    (void*)srcBuffer, (void*)dstImage, (int)dstImageLayout, regionCount,
                    (unsigned long long)r0.bufferOffset, r0.bufferRowLength,
                    r0.bufferImageHeight, r0.imageExtent.width, r0.imageExtent.height,
                    r0.imageExtent.depth, r0.imageOffset.x, r0.imageOffset.y,
                    r0.imageOffset.z);
            }
        }
        auto* imageInfo = gfxstream::base::find(mImageInfo, dstImage);'''
if text.count(OLD) != 1:
    raise SystemExit(f'expected one buf2img anchor, found {text.count(OLD)}')
text = text.replace(OLD, NEW)


OLD = '''        std::lock_guard<std::mutex> lock(mMutex);
        auto* imageInfo = gfxstream::base::find(mImageInfo, pCopyImageToBufferInfo->srcImage);
        auto* bufferInfo = gfxstream::base::find(mBufferInfo, pCopyImageToBufferInfo->dstBuffer);
        if (!imageInfo || !bufferInfo) return;
        if (!imageInfo->compressInfo) {
            vk->vkCmdCopyImageToBuffer2(commandBuffer, pCopyImageToBufferInfo);'''
NEW = '''        std::lock_guard<std::mutex> lock(mMutex);
        {
            static std::atomic<unsigned> sCopyImg2Buf2{0};
            if (sCopyImg2Buf2.fetch_add(1, std::memory_order_relaxed) < 48 &&
                pCopyImageToBufferInfo->regionCount) {
                const VkBufferImageCopy2& r0 = pCopyImageToBufferInfo->pRegions[0];
                GFXSTREAM_WARNING(
                    "COPY-IMG2BUF2: image=%p layout=%d buffer=%p regions=%u bufOff=%llu "
                    "rowLen=%u imgH=%u extent=%ux%ux%u imgOff=%d,%d,%d",
                    (void*)pCopyImageToBufferInfo->srcImage,
                    (int)pCopyImageToBufferInfo->srcImageLayout,
                    (void*)pCopyImageToBufferInfo->dstBuffer,
                    pCopyImageToBufferInfo->regionCount,
                    (unsigned long long)r0.bufferOffset, r0.bufferRowLength,
                    r0.bufferImageHeight, r0.imageExtent.width, r0.imageExtent.height,
                    r0.imageExtent.depth, r0.imageOffset.x, r0.imageOffset.y,
                    r0.imageOffset.z);
            }
        }
        auto* imageInfo = gfxstream::base::find(mImageInfo, pCopyImageToBufferInfo->srcImage);
        auto* bufferInfo = gfxstream::base::find(mBufferInfo, pCopyImageToBufferInfo->dstBuffer);
        if (!imageInfo || !bufferInfo) return;
        if (!imageInfo->compressInfo) {
            vk->vkCmdCopyImageToBuffer2(commandBuffer, pCopyImageToBufferInfo);'''
if text.count(OLD) != 1:
    raise SystemExit(f'expected one img2buf2 anchor, found {text.count(OLD)}')
text = text.replace(OLD, NEW)

OLD = '''        std::lock_guard<std::mutex> lock(mMutex);
        auto* imageInfo = gfxstream::base::find(mImageInfo, pCopyImageToBufferInfo->srcImage);
        auto* bufferInfo = gfxstream::base::find(mBufferInfo, pCopyImageToBufferInfo->dstBuffer);
        if (!imageInfo || !bufferInfo) return;
        if (!imageInfo->compressInfo) {
            vk->vkCmdCopyImageToBuffer2KHR(commandBuffer, pCopyImageToBufferInfo);'''
NEW = '''        std::lock_guard<std::mutex> lock(mMutex);
        {
            static std::atomic<unsigned> sCopyImg2Buf2Khr{0};
            if (sCopyImg2Buf2Khr.fetch_add(1, std::memory_order_relaxed) < 48 &&
                pCopyImageToBufferInfo->regionCount) {
                const VkBufferImageCopy2KHR& r0 = pCopyImageToBufferInfo->pRegions[0];
                GFXSTREAM_WARNING(
                    "COPY-IMG2BUF2KHR: image=%p layout=%d buffer=%p regions=%u bufOff=%llu "
                    "rowLen=%u imgH=%u extent=%ux%ux%u imgOff=%d,%d,%d",
                    (void*)pCopyImageToBufferInfo->srcImage,
                    (int)pCopyImageToBufferInfo->srcImageLayout,
                    (void*)pCopyImageToBufferInfo->dstBuffer,
                    pCopyImageToBufferInfo->regionCount,
                    (unsigned long long)r0.bufferOffset, r0.bufferRowLength,
                    r0.bufferImageHeight, r0.imageExtent.width, r0.imageExtent.height,
                    r0.imageExtent.depth, r0.imageOffset.x, r0.imageOffset.y,
                    r0.imageOffset.z);
            }
        }
        auto* imageInfo = gfxstream::base::find(mImageInfo, pCopyImageToBufferInfo->srcImage);
        auto* bufferInfo = gfxstream::base::find(mBufferInfo, pCopyImageToBufferInfo->dstBuffer);
        if (!imageInfo || !bufferInfo) return;
        if (!imageInfo->compressInfo) {
            vk->vkCmdCopyImageToBuffer2KHR(commandBuffer, pCopyImageToBufferInfo);'''
if text.count(OLD) != 1:
    raise SystemExit(f'expected one img2buf2khr anchor, found {text.count(OLD)}')
text = text.replace(OLD, NEW)
d.write_text(text, encoding='utf-8', newline='')
print('copy parameter diagnostics patched')
