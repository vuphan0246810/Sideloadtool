cmake_minimum_required(VERSION 3.22.1)
project(sideloadnative)

# Tất cả file C của native layer
add_library(
    sideloadnative
    SHARED
    jni_bridge.c
    usbmux.c
    plist_util.c
    lockdown.c
    pairing.c
    afc.c
    install_proxy.c
)

# Include thư mục hiện tại (để header .h tìm thấy nhau)
target_include_directories(sideloadnative PRIVATE ${CMAKE_CURRENT_SOURCE_DIR})

# Link Android log library
find_library(log-lib log)
target_link_libraries(sideloadnative ${log-lib})

# Cờ biên dịch C11
target_compile_options(sideloadnative PRIVATE
    -std=c11
    -Wall
    -Wno-unused-function
    -Wno-unused-variable
    -fvisibility=hidden
)
