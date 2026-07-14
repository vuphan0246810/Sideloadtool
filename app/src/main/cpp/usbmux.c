/*
 * usbmux.c — Triển khai giao thức usbmux của Apple, khớp byte-for-byte với
 * usbmuxd/src/device.c (send_packet, device_data_input, device_version_input).
 *
 * BUGFIX v17 — Read-ahead buffer (nguyên nhân gốc của "SETUP thất bại"):
 *
 *   recv_packet() cũ đọc 8 byte TRƯỚC (header) rồi N-8 byte (body) bằng 2 lần
 *   gọi usb_read riêng biệt. iPhone gửi toàn bộ gói như MỘT USB bulk transfer.
 *   Android bulkTransfer(8) chỉ trả về 8 byte và 12 byte còn lại BỊ MẤT vì
 *   Android USB Host driver KHÔNG buffer phần dư của một bulk IN transfer khi
 *   buffer yêu cầu nhỏ hơn dữ liệu nhận được.
 *
 *   Fix: buffered_read() đọc tối đa MUX_DEV_MRU (65536) byte từ USB vào
 *   rxbuf, rồi phục vụ tất cả yêu cầu nhỏ hơn từ buffer — không bao giờ mất
 *   dữ liệu từ một bulk transfer. Cùng cách usbmuxd/src/device.c dùng pktbuf.
 *
 * Xem ghi chú đầy đủ trong usbmux.h.
 */
#include "usbmux.h"
#include <string.h>
#include <stdlib.h>
#include <arpa/inet.h>   /* htonl / htons / ntohl / ntohs */
#include <android/log.h>
#include <time.h>
#include <stdio.h>

#define TAG "usbmux"
#define LOGI(...) __android_log_print(ANDROID_LOG_INFO,  TAG, __VA_ARGS__)
#define LOGE(...) __android_log_print(ANDROID_LOG_ERROR, TAG, __VA_ARGS__)

#define MUX_PKT_MAX (1u << 20)   /* 1MB — giới hạn an toàn cho một gói mux */

/* ── Macro gửi log lên UI nếu có callback, đồng thời ghi Logcat ─────────── */
#define UI_LOG(c, msg) do { \
    LOGI("%s", (msg)); \
    if ((c)->ui_log) (c)->ui_log(msg); \
} while(0)

/* ── I/O helpers nội bộ ──────────────────────────────────────────────────── */

static int write_all(mux_conn_t *c, const void *buf, int len) {
    const uint8_t *p = buf;
    int sent = 0;
    while (sent < len) {
        int n = c->usb_write(p + sent, len - sent);
        if (n <= 0) { LOGE("usb_write error n=%d", n); return -1; }
        sent += n;
    }
    return sent;
}

/*
 * buffered_read — ĐỌC AN TOÀN từ USB, sử dụng read-ahead buffer.
 *
 * Root cause "SETUP thất bại" (v17 fix):
 *   Android bulkTransfer(N) chỉ trả về N byte và KHÔNG buffer phần dư khi
 *   device gửi nhiều hơn N byte trong 1 bulk transfer. recv_packet() cũ gọi
 *   usb_read(8) rồi usb_read(N-8), khiến 12+ byte của VERSION response bị mất.
 *
 * Cách fix:
 *   Mỗi khi buffer (c->rxbuf) hết, đọc tối đa MUX_DEV_MRU byte từ USB vào
 *   buffer. Phục vụ tất cả yêu cầu nhỏ hơn từ buffer đó — giống pktbuf trong
 *   usbmuxd/src/device.c.
 */
static int buffered_read(mux_conn_t *c, void *buf, int need) {
    uint8_t *dst = (uint8_t *)buf;
    int remaining = need;

    while (remaining > 0) {
        /* Lấy từ buffer nếu còn dữ liệu */
        int avail = c->rxbuf_used - c->rxbuf_pos;
        if (avail > 0) {
            int take = (avail < remaining) ? avail : remaining;
            memcpy(dst, c->rxbuf + c->rxbuf_pos, take);
            c->rxbuf_pos  += take;
            dst            += take;
            remaining      -= take;
            continue;
        }

        /* Buffer trống — nạp lại từ USB */
        c->rxbuf_pos  = 0;
        c->rxbuf_used = 0;

        int n = c->usb_read(c->rxbuf, (int)sizeof(c->rxbuf));  /* tối đa MUX_DEV_MRU */
        if (n <= 0) {
            LOGE("buffered_read: usb_read trả %d (cần %d bytes còn lại)", n, remaining);
            return -1;
        }
        c->rxbuf_used = n;
        LOGI("buffered_read: đọc %d byte từ USB vào rxbuf", n);
    }
    return need;
}

/*
 * mux_header_wire_size — số byte thực sự của mux_header trên dây.
 * Khớp usbmuxd device.c: int mux_header_size = ((dev->version < 2) ? 8 : sizeof(struct mux_header));
 */
static inline int mux_header_wire_size(const mux_conn_t *c) {
    return (c->version < 2) ? 8 : (int)sizeof(mux_header_t);
}

/*
 * send_packet — dựng và gửi một gói mux hoàn chỉnh, khớp send_packet() trong
 * usbmuxd/src/device.c. `extra_hdr`/`extra_hdr_len` tương ứng "header" param
 * gốc (version_header cho VERSION, tcp_header_t cho TCP, NULL cho SETUP).
 */
static int send_packet(mux_conn_t *c, uint32_t proto,
                        const void *extra_hdr, int extra_hdr_len,
                        const void *data, int data_len) {
    int hdr_size = mux_header_wire_size(c);
    int total = hdr_size + extra_hdr_len + data_len;
    if (total <= 0 || (uint32_t)total > MUX_PKT_MAX) {
        LOGE("send_packet: kích thước gói không hợp lệ %d", total);
        return -1;
    }

    uint8_t *buf = calloc(1, total);
    if (!buf) return -1;

    /* protocol + length luôn được ghi (8 byte đầu, mọi phiên bản) */
    uint32_t protocol_be = htonl(proto);
    uint32_t length_be   = htonl((uint32_t)total);
    memcpy(buf + 0, &protocol_be, 4);
    memcpy(buf + 4, &length_be,   4);

    if (c->version >= 2) {
        uint32_t magic_be = htonl(MUX_MAGIC);
        memcpy(buf + 8, &magic_be, 4);

        if (proto == MUX_PROTO_SETUP) {
            /* Khớp device.c: SETUP luôn reset tx_seq=0, rx_seq=0xFFFF */
            c->dev_tx_seq = 0;
            c->dev_rx_seq = 0xFFFF;
        }
        uint16_t tx_be = htons(c->dev_tx_seq);
        uint16_t rx_be = htons(c->dev_rx_seq);
        memcpy(buf + 12, &tx_be, 2);
        memcpy(buf + 14, &rx_be, 2);
        c->dev_tx_seq++;   /* tăng sau MỌI gói gửi khi version >= 2 */
    }

    if (extra_hdr && extra_hdr_len > 0)
        memcpy(buf + hdr_size, extra_hdr, extra_hdr_len);
    if (data && data_len > 0)
        memcpy(buf + hdr_size + extra_hdr_len, data, data_len);

    int r = write_all(c, buf, total);
    free(buf);
    if (r < 0) { LOGE("send_packet: gửi thất bại (proto=%u total=%d)", proto, total); return -1; }
    return total;
}

/*
 * recv_packet — đọc một gói mux hoàn chỉnh qua buffered_read().
 *
 * BUGFIX v17: thay read_all(8) + read_all(N-8) bằng buffered_read() để không
 * bị mất dữ liệu khi iPhone gửi toàn bộ gói trong 1 USB bulk transfer.
 *
 * Trả về buffer malloc() (caller free), *out_len = tổng bytes, *out_proto =
 * giá trị protocol (host order). *out_body = con trỏ ngay sau mux header
 * (hdr_size bytes đầu), *out_body_len = phần còn lại.
 */
static uint8_t *recv_packet(mux_conn_t *c, uint32_t *out_proto,
                             uint8_t **out_body, uint32_t *out_body_len) {
    uint8_t head[8];
    /* FIX v17: dùng buffered_read thay vì read_all trực tiếp */
    if (buffered_read(c, head, 8) < 0) {
        LOGE("recv_packet: không đọc được 8 byte header");
        return NULL;
    }

    uint32_t proto, total;
    memcpy(&proto, head + 0, 4);
    memcpy(&total, head + 4, 4);
    proto = ntohl(proto);
    total = ntohl(total);

    if (total < 8 || total > MUX_PKT_MAX) {
        LOGE("recv_packet: length không hợp lệ %u (proto=%u)", total, proto);
        return NULL;
    }

    uint8_t *buf = malloc(total);
    if (!buf) return NULL;
    memcpy(buf, head, 8);
    if (total > 8) {
        /* FIX v17: buffered_read đảm bảo không mất dữ liệu */
        if (buffered_read(c, buf + 8, (int)(total - 8)) < 0) {
            LOGE("recv_packet: không đọc được body (%u bytes)", total - 8);
            free(buf);
            return NULL;
        }
    }

    int hdr_size = mux_header_wire_size(c);
    if ((uint32_t)hdr_size > total) { free(buf); return NULL; }

    /* Khớp device.c: dev->rx_seq = ntohs(mhdr->rx_seq) khi version >= 2,
     * áp dụng cho MỌI gói nhận được (đọc TRƯỚC KHI cập nhật version). */
    if (c->version >= 2 && total >= sizeof(mux_header_t)) {
        uint16_t rx_be;
        memcpy(&rx_be, buf + 14, 2);
        c->dev_rx_seq = ntohs(rx_be);
    }

    if (out_proto) *out_proto = proto;
    if (out_body)  *out_body  = buf + hdr_size;
    if (out_body_len) *out_body_len = total - (uint32_t)hdr_size;
    return buf;
}

/* ── API Public ───────────────────────────────────────────────────────────── */

int mux_conn_init(mux_conn_t *c,
                  int (*usb_write)(const void*, int),
                  int (*usb_read )(void*, int)) {
    memset(c, 0, sizeof(*c));
    c->usb_write   = usb_write;
    c->usb_read    = usb_read;
    c->version     = 0;       /* chưa thỏa thuận phiên bản */
    c->dev_tx_seq  = 0;
    c->dev_rx_seq  = 0;
    c->rxbuf_pos   = 0;
    c->rxbuf_used  = 0;
    c->state       = MUX_CONN_CLOSED;
    c->ui_log      = NULL;    /* sẽ được set bởi jni_bridge.c */
    /* source port ngẫu nhiên cho virtual TCP-over-mux */
    srand((unsigned)time(NULL) ^ (unsigned)(intptr_t)c);
    c->sport = 50000 + (rand() % 10000);
    return 0;
}

/*
 * mux_do_setup — Bắt tay usbmux đầy đủ, khớp device_add() + device_version_input()
 * trong usbmuxd/src/device.c:
 *   1. Gửi MUX_PROTO_VERSION (header 8 byte vì version=0) với payload
 *      version_header{major=2, minor=0, padding=0}.
 *   2. Nhận phản hồi VERSION từ thiết bị qua buffered_read() (FIX v17).
 *   3. Đặt c->version = major thiết bị trả về (1 hoặc 2).
 *   4. Nếu version >= 2: gửi MUX_PROTO_SETUP (header 16 byte, payload 1 byte
 *      0x07) — bên trong tự reset dev_tx_seq=0/dev_rx_seq=0xFFFF.
 *
 * Nếu c->ui_log != NULL, gọi nó để gửi log chi tiết từng bước lên UI.
 */
int mux_do_setup(mux_conn_t *c) {
    char logbuf[256];

    mux_version_header_t vh;
    vh.major   = htonl(MUX_VERSION_MAJOR);
    vh.minor   = htonl(MUX_VERSION_MINOR);
    vh.padding = 0;

    /* Reset read buffer trước khi handshake mới */
    c->rxbuf_pos  = 0;
    c->rxbuf_used = 0;

    snprintf(logbuf, sizeof(logbuf),
             "[mux] Gửi VERSION packet (major=%d minor=%d)...",
             MUX_VERSION_MAJOR, MUX_VERSION_MINOR);
    UI_LOG(c, logbuf);

    if (send_packet(c, MUX_PROTO_VERSION, &vh, sizeof(vh), NULL, 0) < 0) {
        UI_LOG(c, "[mux] \u274c Gửi VERSION packet thất bại — lỗi USB write");
        return -1;
    }

    UI_LOG(c, "[mux] Đã gửi VERSION, đang chờ phản hồi từ iPhone...");

    uint32_t proto = 0, body_len = 0;
    uint8_t *body = NULL;
    uint8_t *pkt = recv_packet(c, &proto, &body, &body_len);
    if (!pkt) {
        UI_LOG(c, "[mux] \u274c Không nhận được phản hồi VERSION — timeout hoặc lỗi USB read");
        return -1;
    }

    if (proto != MUX_PROTO_VERSION || body_len < sizeof(mux_version_header_t)) {
        snprintf(logbuf, sizeof(logbuf),
                 "[mux] \u274c Phản hồi VERSION không hợp lệ (proto=%u body_len=%u)",
                 proto, body_len);
        UI_LOG(c, logbuf);
        free(pkt);
        return -1;
    }

    mux_version_header_t rvh;
    memcpy(&rvh, body, sizeof(rvh));
    free(pkt);

    uint32_t dev_major = ntohl(rvh.major);
    uint32_t dev_minor = ntohl(rvh.minor);

    if (dev_major != 1 && dev_major != 2) {
        snprintf(logbuf, sizeof(logbuf),
                 "[mux] \u274c Thiết bị trả về version không hỗ trợ: %u.%u",
                 dev_major, dev_minor);
        UI_LOG(c, logbuf);
        return -1;
    }
    c->version = (int)dev_major;

    snprintf(logbuf, sizeof(logbuf),
             "[mux] \u2705 Thiết bị chấp nhận usbmux v%u.%u", dev_major, dev_minor);
    UI_LOG(c, logbuf);

    if (c->version >= 2) {
        UI_LOG(c, "[mux] Gửi SETUP packet (protocol=2, payload=0x07)...");
        const uint8_t setup_payload = 0x07;
        if (send_packet(c, MUX_PROTO_SETUP, NULL, 0, &setup_payload, 1) < 0) {
            UI_LOG(c, "[mux] \u274c Gửi SETUP packet thất bại — lỗi USB write");
            return -1;
        }
        snprintf(logbuf, sizeof(logbuf),
                 "[mux] \u2705 SETUP hoàn tất (dev_tx_seq=%u dev_rx_seq=%u)",
                 c->dev_tx_seq, c->dev_rx_seq);
        UI_LOG(c, logbuf);
    }
    return 0;
}

/*
 * mux_connect — Thực hiện TCP-over-USB handshake (SYN → SYN/ACK → ACK) đến
 * dport, đóng gói qua MUX_PROTO_TCP (encapsulated trong mux header đã thỏa
 * thuận version ở mux_do_setup).
 */
int mux_connect(mux_conn_t *c, int dport) {
    if (c->version == 0) {
        LOGE("[mux] mux_connect: chưa gọi mux_do_setup() thành công");
        return -1;
    }
    c->dport = dport;
    c->tx_seq = (uint32_t)(rand() & 0xFFFF);
    c->rx_seq = 0;
    c->state  = MUX_CONN_CONNECTING;

    /* Gửi SYN */
    tcp_header_t th;
    memset(&th, 0, sizeof(th));
    th.sport  = htons((uint16_t)c->sport);
    th.dport  = htons((uint16_t)c->dport);
    th.seq    = htonl(c->tx_seq);
    th.ack    = 0;
    th.offset = 0x50;
    th.flags  = TCP_SYN;
    th.wnd    = htons(0xFFFF);
    LOGI("[mux] SYN → port %d", dport);
    if (send_packet(c, MUX_PROTO_TCP, &th, sizeof(th), NULL, 0) < 0) {
        LOGE("[mux] \u274c Gửi SYN thất bại");
        return -1;
    }
    c->tx_seq++;

    /* Nhận SYN+ACK */
    uint32_t proto = 0, body_len = 0;
    uint8_t *body = NULL;
    uint8_t *pkt = recv_packet(c, &proto, &body, &body_len);
    if (!pkt) { LOGE("[mux] \u274c Không nhận được SYN+ACK"); return -1; }

    if (proto != MUX_PROTO_TCP || body_len < sizeof(tcp_header_t)) {
        LOGE("[mux] \u274c Phản hồi SYN không hợp lệ (proto=%u body_len=%u)", proto, body_len);
        free(pkt);
        return -1;
    }
    tcp_header_t rt;
    memcpy(&rt, body, sizeof(rt));
    free(pkt);

    if ((rt.flags & (TCP_SYN | TCP_ACK)) != (TCP_SYN | TCP_ACK)) {
        LOGE("[mux] \u274c Cờ TCP không đúng 0x%02X (cần SYN+ACK=0x12)", rt.flags);
        return -1;
    }
    c->rx_seq    = ntohl(rt.seq) + 1;
    c->rx_window = ntohs(rt.wnd);
    LOGI("[mux] \u2705 SYN+ACK nhận: rx_seq=%u tx_seq=%u wnd=%u",
         c->rx_seq, c->tx_seq, c->rx_window);

    /* Gửi ACK */
    tcp_header_t at;
    memset(&at, 0, sizeof(at));
    at.sport  = htons((uint16_t)c->sport);
    at.dport  = htons((uint16_t)c->dport);
    at.seq    = htonl(c->tx_seq);
    at.ack    = htonl(c->rx_seq);
    at.offset = 0x50;
    at.flags  = TCP_ACK;
    at.wnd    = htons(0xFFFF);
    if (send_packet(c, MUX_PROTO_TCP, &at, sizeof(at), NULL, 0) < 0) {
        LOGE("[mux] \u274c Gửi ACK thất bại");
        return -1;
    }

    c->state = MUX_CONN_CONNECTED;
    LOGI("[mux] \u2705 Kết nối TCP-over-USB thành công đến port %d", dport);
    return 0;
}

/* Gửi data (ACK + PSH) */
int mux_send(mux_conn_t *c, const void *data, int len) {
    if (c->state != MUX_CONN_CONNECTED) return -1;

    tcp_header_t th;
    memset(&th, 0, sizeof(th));
    th.sport  = htons((uint16_t)c->sport);
    th.dport  = htons((uint16_t)c->dport);
    th.seq    = htonl(c->tx_seq);
    th.ack    = htonl(c->rx_seq);
    th.offset = 0x50;
    th.flags  = TCP_ACK | TCP_PSH;
    th.wnd    = htons(0xFFFF);

    int r = send_packet(c, MUX_PROTO_TCP, &th, sizeof(th), data, len);
    if (r < 0) { LOGE("mux_send failed"); return -1; }

    c->tx_seq += (uint32_t)len;
    return len;
}

/* Nhận data — trả về số bytes thực tế */
int mux_recv(mux_conn_t *c, void *buf, int maxlen) {
    if (c->state != MUX_CONN_CONNECTED) return -1;

    uint32_t proto = 0, body_len = 0;
    uint8_t *body = NULL;
    uint8_t *pkt = recv_packet(c, &proto, &body, &body_len);
    if (!pkt) return -1;

    if (proto != MUX_PROTO_TCP || body_len < sizeof(tcp_header_t)) {
        LOGE("mux_recv: gói không hợp lệ (proto=%u body_len=%u)", proto, body_len);
        free(pkt);
        return -1;
    }
    tcp_header_t th;
    memcpy(&th, body, sizeof(th));
    c->rx_window = ntohs(th.wnd);

    uint32_t payload_len = body_len - (uint32_t)sizeof(tcp_header_t);
    const uint8_t *payload = body + sizeof(tcp_header_t);

    int got = 0;
    if (payload_len > 0) {
        uint32_t copy_len = ((uint32_t)maxlen < payload_len) ? (uint32_t)maxlen : payload_len;
        memcpy(buf, payload, copy_len);
        got = (int)copy_len;
        c->rx_seq += copy_len;
    }
    free(pkt);

    /* Gửi ACK ngay */
    tcp_header_t at;
    memset(&at, 0, sizeof(at));
    at.sport  = htons((uint16_t)c->sport);
    at.dport  = htons((uint16_t)c->dport);
    at.seq    = htonl(c->tx_seq);
    at.ack    = htonl(c->rx_seq);
    at.offset = 0x50;
    at.flags  = TCP_ACK;
    at.wnd    = htons(0xFFFF);
    send_packet(c, MUX_PROTO_TCP, &at, sizeof(at), NULL, 0);

    return got;
}

/* Nhận đúng len bytes */
int mux_recv_exact(mux_conn_t *c, void *buf, int len) {
    uint8_t *p = buf;
    int got = 0;
    while (got < len) {
        int n = mux_recv(c, p + got, len - got);
        if (n <= 0) return -1;
        got += n;
    }
    return got;
}

void mux_disconnect(mux_conn_t *c) {
    if (c->state != MUX_CONN_CONNECTED) return;
    tcp_header_t th;
    memset(&th, 0, sizeof(th));
    th.sport  = htons((uint16_t)c->sport);
    th.dport  = htons((uint16_t)c->dport);
    th.seq    = htonl(c->tx_seq);
    th.ack    = htonl(c->rx_seq);
    th.offset = 0x50;
    th.flags  = TCP_FIN | TCP_ACK;
    th.wnd    = htons(0xFFFF);
    send_packet(c, MUX_PROTO_TCP, &th, sizeof(th), NULL, 0);
    c->state = MUX_CONN_CLOSED;
    LOGI("[mux] Đã đóng kết nối TCP-over-USB.");
}

int mux_raw_write(mux_conn_t *c, const void *buf, int len) {
    return write_all(c, buf, len);
}

int mux_raw_read(mux_conn_t *c, void *buf, int maxlen) {
    return buffered_read(c, buf, maxlen);
}
