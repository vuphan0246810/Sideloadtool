#pragma once
/* uuid_compat.h — Inline UUID v4 dùng /dev/urandom (không cần libuuid trong NDK) */
#include <stdio.h>
#include <string.h>
#include <fcntl.h>
#include <unistd.h>
#include <stdint.h>

static inline void uuid_generate_random_str(char out[37]) {
    uint8_t b[16];
    int fd = open("/dev/urandom", O_RDONLY);
    if (fd < 0) { memset(b, 0xAB, 16); } else { read(fd, b, 16); close(fd); }
    b[6] = (b[6] & 0x0F) | 0x40;   /* version 4 */
    b[8] = (b[8] & 0x3F) | 0x80;   /* variant RFC4122 */
    snprintf(out, 37,
        "%02x%02x%02x%02x-%02x%02x-%02x%02x-%02x%02x-%02x%02x%02x%02x%02x%02x",
        b[0],b[1],b[2],b[3], b[4],b[5], b[6],b[7],
        b[8],b[9], b[10],b[11],b[12],b[13],b[14],b[15]);
}
