#include "classic_image_bridge.h"
#include <cassert>
using namespace gfxstream::host;
int main() {
    static_assert(cib::kVersion == 1 && cib::kContract == 0x43494231);
    static_assert(cib::kConsumer == 0x4349423100000001ULL);
    uint32_t stride=0,size=0;
    assert(cib::staging(1280,720,stride,size) && stride==5120 && size==3686400);
    assert(cib::staging(1,1,stride,size) && stride==16 && size==16);
    assert(!cib::staging(0,720,stride,size));
    assert(!cib::staging(UINT32_MAX,UINT32_MAX,stride,size));
    VkImageCreateInfo ci{};
    ci.sType=VK_STRUCTURE_TYPE_IMAGE_CREATE_INFO;ci.format=VK_FORMAT_B8G8R8A8_UNORM;
    ci.imageType=VK_IMAGE_TYPE_2D;ci.extent={1280,720,1};ci.mipLevels=1;ci.arrayLayers=1;
    ci.samples=VK_SAMPLE_COUNT_1_BIT;ci.sharingMode=VK_SHARING_MODE_EXCLUSIVE;
    ci.initialLayout=VK_IMAGE_LAYOUT_UNDEFINED;ci.flags=VK_IMAGE_CREATE_MUTABLE_FORMAT_BIT;
    ci.usage=VK_IMAGE_USAGE_COLOR_ATTACHMENT_BIT|VK_IMAGE_USAGE_SAMPLED_BIT;
    assert(cib::image(ci));
    ci.flags|=VK_IMAGE_CREATE_ALIAS_BIT;assert(cib::image(ci));
    for (unsigned invalid=0;invalid<8;++invalid) {
        auto bad=ci;
        if(invalid==0)bad.extent.depth=2;
        if(invalid==1)bad.arrayLayers=2;
        if(invalid==2)bad.mipLevels=2;
        if(invalid==3)bad.samples=VK_SAMPLE_COUNT_4_BIT;
        if(invalid==4)bad.flags|=VK_IMAGE_CREATE_SPARSE_BINDING_BIT;
        if(invalid==5)bad.usage|=VK_IMAGE_USAGE_STORAGE_BIT;
        if(invalid==6)bad.format=VK_FORMAT_R16G16B16A16_SFLOAT;
        if(invalid==7)bad.sharingMode=VK_SHARING_MODE_CONCURRENT;
        assert(!cib::image(bad));
    }
    assert(cib::view(VK_FORMAT_B8G8R8A8_UNORM,VK_FORMAT_B8G8R8A8_SRGB));
    assert(!cib::view(VK_FORMAT_B8G8R8A8_UNORM,VK_FORMAT_R8G8B8A8_SRGB));
}
