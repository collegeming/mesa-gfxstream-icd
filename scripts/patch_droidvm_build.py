#!/usr/bin/env python3
"""Apply the minimal Android/NDK build adaptations to Droid-VM/gfxstream.

The upstream CMake build assumes a desktop Linux host. This script ports the
same build fixes used by the working mali-compat CI branch:
  - VK_USE_PLATFORM_ANDROID_KHR instead of XCB
  - Android native_sub_window source
  - EGL translator without X11/GLX
  - bionic graphics/log libraries
  - vendored AOSP headers on the include path
"""
import argparse
import os

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("root", help="Path to the pinned gfxstream source checkout")
ROOT = os.path.abspath(parser.parse_args().root)


def edit(rel, old, new, count=1):
    path = os.path.join(ROOT, rel)
    with open(path, "r", encoding="utf-8") as f:
        text = f.read()
    if text.count(old) != count:
        raise SystemExit(f"{rel}: expected {count} occurrences, found {text.count(old)}")
    with open(path, "w", encoding="utf-8") as f:
        f.write(text.replace(old, new))
    print("patched", rel)


# 1. Platform definitions: Android is a UNIX in CMake, so it must be tested first.
edit(
    "CMakeLists.txt",
    """elseif(UNIX)
    # Use X11 version of EGL platform specific definitions.
    add_compile_definitions(USE_X11)
    add_compile_definitions(VK_USE_PLATFORM_XCB_KHR)""",
    """elseif(ANDROID)
    # Android has no X11; use the Android WSI and EGL from the NDK.
    add_compile_definitions(VK_USE_PLATFORM_ANDROID_KHR)
elseif(UNIX)
    # Use X11 version of EGL platform specific definitions.
    add_compile_definitions(USE_X11)
    add_compile_definitions(VK_USE_PLATFORM_XCB_KHR)""",
)

# 2. Vulkan server: Android WSI.
edit(
    "host/vulkan/CMakeLists.txt",
    """elseif (UNIX)
target_compile_definitions(gfxstream-vulkan-server PRIVATE -DVK_USE_PLATFORM_XCB_KHR)""",
    """elseif (ANDROID)
target_compile_definitions(gfxstream-vulkan-server PRIVATE -DVK_USE_PLATFORM_ANDROID_KHR)
elseif (UNIX)
target_compile_definitions(gfxstream-vulkan-server PRIVATE -DVK_USE_PLATFORM_XCB_KHR)""",
)

# 3. Vulkan cereal: Android WSI.
edit(
    "host/vulkan/cereal/CMakeLists.txt",
    """elseif(UNIX)
    target_compile_definitions(OpenglRender_vulkan_cereal PRIVATE -DVK_USE_PLATFORM_XCB_KHR)""",
    """elseif(ANDROID)
    target_compile_definitions(OpenglRender_vulkan_cereal PRIVATE -DVK_USE_PLATFORM_ANDROID_KHR)
elseif(UNIX)
    target_compile_definitions(OpenglRender_vulkan_cereal PRIVATE -DVK_USE_PLATFORM_XCB_KHR)""",
)

# 4. EGL translator: no X11/GLX on Android; EGL is supplied by the NDK.
edit(
    "host/gl/glestranslator/egl/CMakeLists.txt",
    """elseif (QNX)
    add_library(
        EGL_translator_static
        ${egl-translator-common-sources}
        ${egl-translator-qnx-sources})
else()""",
    """elseif (QNX)
    add_library(
        EGL_translator_static
        ${egl-translator-common-sources}
        ${egl-translator-qnx-sources})
elseif (ANDROID)
    add_library(
        EGL_translator_static
        ${egl-translator-common-sources}
        ${egl-translator-qnx-sources})
else()""",
)
edit(
    "host/gl/glestranslator/egl/CMakeLists.txt",
    """elseif (QNX)
    target_link_libraries(EGL_translator_static PUBLIC "-lscreen -lregex -lEGL -lGLESv2")
else()""",
    """elseif (QNX)
    target_link_libraries(EGL_translator_static PUBLIC "-lscreen -lregex -lEGL -lGLESv2")
elseif (ANDROID)
    target_link_libraries(EGL_translator_static PUBLIC "-ldl -llog -lEGL -lGLESv2")
else()""",
)

# 5. OpenGLES dispatch: no USE_X11 on Android.
edit(
    "host/gl/OpenGLESDispatch/CMakeLists.txt",
    """if (APPLE)
    add_compile_definitions(__APPLE__)
elseif(UNIX)
    add_compile_definitions(USE_X11)
endif()""",
    """if (APPLE)
    add_compile_definitions(__APPLE__)
elseif(ANDROID)
    # Android uses the NDK EGL/GLES headers, no X11.
elseif(UNIX)
    add_compile_definitions(USE_X11)
endif()""",
)

# 6. Native sub-window: use the Android implementation instead of X11.
edit(
    "host/CMakeLists.txt",
    """elseif (QNX)
    set(stream-server-core-platform-sources native_sub_window_qnx.cpp)
else()
    set(stream-server-core-platform-sources native_sub_window_x11.cpp)
endif()""",
    """elseif (QNX)
    set(stream-server-core-platform-sources native_sub_window_qnx.cpp)
elseif (ANDROID)
    set(stream-server-core-platform-sources native_sub_window_android.cpp)
else()
    set(stream-server-core-platform-sources native_sub_window_x11.cpp)
endif()""",
)

# 7. Vendored AOSP headers and bionic graphics libraries.
edit(
    "CMakeLists.txt",
    "set(GFXSTREAM_REPO_ROOT ${CMAKE_CURRENT_SOURCE_DIR})",
    """set(GFXSTREAM_REPO_ROOT ${CMAKE_CURRENT_SOURCE_DIR})

if(ANDROID)
    # The NDK does not ship AOSP platform headers (cutils/native_handle.h,
    # system/graphics.h, ...); gfxstream vendors them here.
    include_directories(${GFXSTREAM_REPO_ROOT}/third_party/android/include)
endif()""",
)
edit(
    "host/CMakeLists.txt",
    """if (APPLE)
    set_target_properties(gfxstream_backend
        PROPERTIES
        LINK_FLAGS  "-undefined dynamic_lookup -flat_namespace")
endif()""",
    """if (APPLE)
    set_target_properties(gfxstream_backend
        PROPERTIES
        LINK_FLAGS  "-undefined dynamic_lookup -flat_namespace")
endif()

if (ANDROID)
    # ANativeWindow/AHardwareBuffer live in libandroid; EGL/GLES/vulkan and
    # liblog are the platform graphics/log libraries.
    target_link_libraries(gfxstream_backend PRIVATE
        android EGL GLESv2 vulkan log)
endif()""",
)

# 8. The vendored development stub shadows NDK api-level.h. Use the real NDK
# definitions (including __ANDROID_API_U__) rather than changing the target API.
edit(
    "third_party/android/include/android/api-level.h",
    "#define __ANDROID_API__ 10000",
    """#if defined(__ANDROID__)
#include_next <android/api-level.h>
#else
#define __ANDROID_API__ 10000
#endif""",
)

# 9. Android's realtime functions are in bionic; there is no separate librt.
edit(
    "common/base/CMakeLists.txt",
    """elseif(LINUX)
    set(gfxstream_common_base_platform_deps
        dl
        rt)""",
    """elseif(ANDROID)
    set(gfxstream_common_base_platform_deps
        dl)
elseif(LINUX)
    set(gfxstream_common_base_platform_deps
        dl
        rt)""",
)

# 10. DroidVM's decoder references its existing profiling implementation, but
# only Android.bp lists it. Add that same implementation to the CMake target.
edit(
    "host/vulkan/CMakeLists.txt",
    "            dependency_graph.cpp",
    "            decoder_profile.cpp\n            dependency_graph.cpp",
)

# Its unused helper is accepted by Android.bp but triggers CMake's -Werror.
# Limit this diagnostic exception to the existing profiler translation unit.
edit(
    "host/vulkan/CMakeLists.txt",
    "set_source_files_properties(vk_decoder.cpp PROPERTIES COMPILE_FLAGS -Wno-unused-variable)",
    """set_source_files_properties(decoder_profile.cpp PROPERTIES COMPILE_FLAGS -Wno-unused-function)
set_source_files_properties(vk_decoder.cpp PROPERTIES COMPILE_FLAGS -Wno-unused-variable)""",
)

print("done")
