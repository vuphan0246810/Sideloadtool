#pragma once
/*
 * usbmux.h — Giao thức usbmux của Apple qua USB.
 *
 * Triển khai lại dựa theo mã nguồn gốc usbmuxd/src/device.c, xác thực với:
 *   libimobiledevice/usbmuxd  src/device.c  (struct mux_header, struct
 *   version_header, enum mux_protocol, send_packet(), device_data_input(),
 *   device_version_input())
 *
 * ⚠️ BUGFIX QUAN TRỌNG (v17 — read-ahead buffer):
 *   Bản trước (v16) sửa đúng giao thức (VERSION→SETUP, magic 0xfeedface, 16-bit
 *   seq, layout header đúng) nhưng vẫn bị lỗi "SETUP thất bại" trong thực tế vì:
 *
 *   recv_packet() đọc 8 byte TRƯỚC (header) rồi đọc phần còn lại. Nhưng iPhone
 *   gửi toàn bộ packet (ví dụ 20 byte) như MỘT USB bulk transfer. Android USB
 *   Host API (bulkTransfer) chỉ trả về đúng số byte đã yêu cầu (8 byte) và KHÔNG
 *   buffering phần dư — 12 byte còn lại BỊ MẤT. Lần đọc tiếp theo gửi IN token
 *   mới tới iPhone, vốn đã xong việc → timeout → SETUP thất bại.
 *
 *   Fix: thêm read-ahead buffer 65536 byte (DEV_MRU từ usbmuxd/src/device.c) vào
 *   mux_conn_t. buffered_read() lấp đầy buffer bằng một USB bulk read lớn (tối đa
 *   65536 byte) rồi phục vụ các yêu cầu nhỏ hơn từ buffer đó — đảm bảo không bao
 *   giờ mất dữ liệu từ một bulk transfer.
 *
 *   Ngoài ra thêm ui_log callback để mux_do_setup() gửi log chi tiết lên UI thay
 *   vì chỉ ghi Logcat — người dùng thấy chính xác bước nào thất bại.
 *
 * enum mux_protocol thật: VERSION=0, CONTROL=1, SETUP=2, TCP=6 (IPPROTO_TCP).
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

/*
 * DEV_MRU — kích thước buffer đọc trước (khớp DEV_MRU trong usbmuxd/src/device.c).
 * Mỗi lần USB đọc, ta đọc tối đa DEV_MRU byte vào rxbuf để tránh mất dữ liệu
 * khi iPhone gửi nhiều byte trong một bulk transfer.
 */
#define MUX_DEV_MRU 65536

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

    /*
     * Read-ahead buffer (FIX v17).
     *
     * Vấn đề: recv_packet() đọc 8 byte (header) rồi N-8 byte (body) bằng 2
     * lần gọi usb_read riêng biệt. iPhone gửi toàn bộ gói (ví dụ 20 byte)
     * trong MỘT USB bulk transfer. Android bulkTransfer(8 byte) chỉ trả về 8
     * byte và 12 byte còn lại BỊ MẤT — không buffered bởi driver.
     *
     * Giải pháp: mỗi khi cần dữ liệu, đọc MUX_DEV_MRU byte từ USB vào rxbuf
     * (giống DEV_MRU = 65536 trong usbmuxd/src/device.c). buffered_read()
     * phục vụ tất cả yêu cầu nhỏ hơn từ buffer — không bao giờ mất dữ liệu.
     */
    uint8_t  rxbuf[MUX_DEV_MRU];
    int      rxbuf_used;     /* số byte hợp lệ trong rxbuf [0..rxbuf_used) */
    int      rxbuf_pos;      /* vị trí đọc tiếp theo trong rxbuf */

    /*
     * UI log callback — nếu được set, mux_do_setup() gọi hàm này để gửi
     * log chi tiết lên UI (thay vì chỉ ghi Logcat qua LOGI/LOGE).
     * Set bởi jni_bridge.c sau khi gọi mux_conn_init().
     */
    void (*ui_log)(const char *msg);
} mux_conn_t;

/* ── API ─────────────────────────────────────────────────────────────────── */
int  mux_conn_init (mux_conn_t *c,
                    int (*usb_write)(const void*, int),
                    int (*usb_read )(void*, int));

/* Thực hiện TOÀN BỘ bắt tay usbmux: gửi VERSION, nhận phản hồi, rồi gửi SETUP.
 * Nếu c->ui_log != NULL, gọi nó để ghi log chi tiết từng bước lên UI.
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
