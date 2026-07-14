#pragma once
/*
 * usbmux.h — Giao thức usbmux của Apple qua USB.
 *
 * Triển khai lại HOÀN TOÀN dựa theo mã nguồn gốc, xác thực byte-for-byte với:
 *   libimobiledevice/usbmuxd  src/device.c  (struct mux_header, struct
 *   version_header, enum mux_protocol, send_packet(), device_data_input(),
 *   device_version_input())
 *
 * ⚠️ BUGFIX QUAN TRỌNG (thay thế toàn bộ 3 "fix" SAI của các bản trước):
 *   Bản trước tự nhận đã "so khớp byte-for-byte với usbmuxd" nhưng thực tế
 *   KHÔNG khớp — toàn bộ handshake bị sai, khiến iPhone không bao giờ phản
 *   hồi (không hiện "Tin cậy máy tính này?", USB tự rớt kết nối). Cụ thể:
 *
 *   1. MAGIC sai: dùng 0xFADEDEAD trong khi giao thức thật dùng 0xfeedface.
 *   2. Cấu trúc header sai thứ tự & sai kích thước field: bản trước dùng
 *      magic(4)+version(2)+message(2)+tx_seq(4)+rx_seq(4)+length(4) = 20
 *      bytes cố định. Giao thức thật: protocol(4)+length(4)+magic(4)+
 *      tx_seq(2)+rx_seq(2) = 16 bytes, và CHỈ 8 byte đầu (protocol+length)
 *      được gửi khi phiên bản mux < 2 (tức là gói VERSION đầu tiên).
 *   3. Thiếu hoàn toàn bước bắt tay phiên bản (MUX_PROTO_VERSION, protocol=0)
 *      — bản trước nhảy thẳng vào một gói "SETUP" tự chế (protocol/message=8)
 *      chưa từng tồn tại trong giao thức thật. Giao thức thật: gửi VERSION
 *      (protocol=0, header 8 byte, payload version_header{major,minor,
 *      padding}) trước, nhận phản hồi version từ thiết bị, SAU ĐÓ mới gửi
 *      SETUP (protocol=2, header 16 byte vì version≥2, payload 1 byte 0x07).
 *   4. tx_seq/rx_seq ở mux-header là 16-bit (htons), không phải 32-bit
 *      (htonl) như bản trước.
 *
 * enum mux_protocol thật: VERSION=0, CONTROL=1, SETUP=2, TCP=6 (IPPROTO_TCP).
 * (Bản trước dùng MUX_MSG_SETUP=8 và MUX_MSG_TCP=6 — TCP đúng nhưng SETUP sai
 *  hoàn toàn giá trị VÀ sai vai trò: SETUP thật không mang version, VERSION
 *  là một protocol riêng.)
 */

#include <stdint.h>
#include <stddef.h>

/* ── Hằng số (khớp usbmuxd/src/device.c) ─────────────────────────────────── */
#define MUX_MAGIC          0xfeedface   /* usbmuxd device.c: send_packet() */
#define MUX_VERSION_MAJOR  2
#define MUX_VERSION_MINOR  0

/* enum mux_protocol thật (device.c) */
#define MUX_PROTO_VERSION  0
#define MUX_PROTO_CONTROL  1
#define MUX_PROTO_SETUP    2
#define MUX_PROTO_TCP      6   /* IPPROTO_TCP */

/* TCP flags (virtual TCP-over-mux) */
#define TCP_FIN  0x001
#define TCP_SYN  0x002
#define TCP_RST  0x004
#define TCP_PSH  0x008
#define TCP_ACK  0x010

/* Cổng lockdownd */
#define LOCKDOWN_PORT 62078

/* ── Struct wire-format (big-endian trên dây, khớp usbmuxd device.c) ─────── */
#pragma pack(push, 1)

/* struct mux_header thật — 16 byte đầy đủ (chỉ dùng khi version >= 2).
 * Khi version < 2 (gói VERSION đầu tiên) chỉ 8 byte đầu (protocol+length)
 * được gửi/nhận thực tế trên dây — xem mux_header_wire_size(). */
typedef struct {
    uint32_t protocol;
    uint32_t length;      /* tổng bytes của cả gói, kể cả phần header này */
    uint32_t magic;        /* 0xfeedface — chỉ có nghĩa khi version >= 2 */
    uint16_t tx_seq;        /* chỉ có nghĩa khi version >= 2 */
    uint16_t rx_seq;        /* chỉ có nghĩa khi version >= 2 */
} mux_header_t;

/* struct version_header thật — payload của gói MUX_PROTO_VERSION */
typedef struct {
    uint32_t major;
    uint32_t minor;
    uint32_t padding;
} mux_version_header_t;

/* struct tcphdr kiểu BSD, dùng cho gói MUX_PROTO_TCP (20 byte, không option) */
typedef struct {
    uint16_t sport;
    uint16_t dport;
    uint32_t seq;
    uint32_t ack;
    uint8_t  offset;        /* data offset (5 << 4 = 0x50) */
    uint8_t  flags;
    uint16_t wnd;
    uint16_t checksum;      /* 0 — usbmuxd không tính thật */
    uint16_t urg;
} tcp_header_t;
#pragma pack(pop)

/* ── State ───────────────────────────────────────────────────────────────── */
typedef enum {
    MUX_CONN_CONNECTING,
    MUX_CONN_CONNECTED,
    MUX_CONN_CLOSED,
} mux_conn_state_t;

typedef struct mux_conn {
    int              sport;          /* source port ngẫu nhiên (virtual TCP) */
    int              dport;          /* dest port (LOCKDOWN_PORT, ...) */
    uint32_t         tx_seq;         /* virtual TCP: host → device seq */
    uint32_t         rx_seq;         /* virtual TCP: device → host seq (ack) */
    uint32_t         rx_window;      /* receive window từ device */
    mux_conn_state_t state;

    /* Trạng thái bắt tay usbmux (khớp struct mux_device trong device.c) */
    int              version;        /* 0 = chưa thỏa thuận, 1/2 = đã xong */
    uint16_t         dev_tx_seq;     /* mux-header tx_seq (16-bit, tăng mỗi gói gửi) */
    uint16_t         dev_rx_seq;     /* mux-header rx_seq (16-bit, cập nhật khi nhận) */

    /* callbacks vào JNI USB layer */
    int (*usb_write)(const void *buf, int len);
    int (*usb_read )(void *buf, int len);
} mux_conn_t;

/* ── API ─────────────────────────────────────────────────────────────────── */
int  mux_conn_init (mux_conn_t *c,
                    int (*usb_write)(const void*, int),
                    int (*usb_read )(void*, int));

/* Thực hiện TOÀN BỘ bắt tay usbmux: gửi VERSION, nhận phản hồi, rồi gửi SETUP.
 * (Tên hàm giữ nguyên "mux_do_setup" để tương thích ngược với jni_bridge.c,
 *  nhưng bên trong đã bao gồm cả bước VERSION bắt buộc trước SETUP.) */
int  mux_do_setup (mux_conn_t *c);
int  mux_connect  (mux_conn_t *c, int dport); /* SYN → SYN/ACK → ACK */
int  mux_send     (mux_conn_t *c, const void *data, int len);
int  mux_recv     (mux_conn_t *c, void *buf, int maxlen);
int  mux_recv_exact(mux_conn_t *c, void *buf, int len);
void mux_disconnect(mux_conn_t *c);

/* Internal helpers exposed for lockdown/tls */
int  mux_raw_write(mux_conn_t *c, const void *buf, int len);
int  mux_raw_read (mux_conn_t *c, void *buf, int maxlen);
