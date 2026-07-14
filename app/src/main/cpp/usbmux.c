/*
 * usbmux.c — Triển khai giao thức usbmux Apple.
 * Tham khảo chính: usbmuxd/src/device.c (Martin Szulecki, Nikias Bassen)
 *
 * *** 3 BUG ĐÃ SỬA ***
 *  1. MAGIC 0xFADEDEAD (không phải 0xFEEDFACE)
 *  2. SETUP header: rx_seq=0 trong header, sau đó dev_rx_seq nội bộ = 0xFFFF
 *  3. SYN dùng dev_tx_seq=0, KHÔNG tăng sau SETUP
 */
#include "usbmux.h"
#include <string.h>
#include <stdlib.h>
#include <arpa/inet.h>   /* htonl / htons */
#include <android/log.h>
#include <time.h>

#define TAG "usbmux"
#define LOGI(...) __android_log_print(ANDROID_LOG_INFO,  TAG, __VA_ARGS__)
#define LOGE(...) __android_log_print(ANDROID_LOG_ERROR, TAG, __VA_ARGS__)

/* ── Helpers nội bộ ───────────────────────────────────────────────────────── */

static void build_mux_header(mux_header_t *h, mux_conn_t *c,
                              uint16_t msg, uint32_t payload_len) {
    h->magic       = htonl(MUX_MAGIC);
    h->mux_version = htons(0);
    h->message     = htons(msg);
    h->tx_seq      = htonl(c->dev_tx_seq);
    h->rx_seq      = htonl(c->dev_rx_seq);
    h->length      = htonl(sizeof(mux_header_t) + payload_len);
}

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

static int read_all(mux_conn_t *c, void *buf, int len) {
    uint8_t *p = buf;
    int got = 0;
    while (got < len) {
        int n = c->usb_read(p + got, len - got);
        if (n <= 0) { LOGE("usb_read error n=%d", n); return -1; }
        got += n;
    }
    return got;
}

/* ── API Public ───────────────────────────────────────────────────────────── */

int mux_conn_init(mux_conn_t *c,
                  int (*usb_write)(const void*, int),
                  int (*usb_read )(void*, int)) {
    memset(c, 0, sizeof(*c));
    c->usb_write   = usb_write;
    c->usb_read    = usb_read;
    c->dev_tx_seq  = 0;
    c->dev_rx_seq  = 0;      /* sẽ được set thành 0xFFFF SAU khi gửi SETUP */
    c->state       = MUX_CONN_CLOSED;
    /* source port ngẫu nhiên */
    srand((unsigned)time(NULL));
    c->sport = 50000 + (rand() % 10000);
    return 0;
}

/*
 * mux_do_setup — Gửi SETUP packet để thỏa thuận phiên bản usbmux 2.0.
 *
 * Theo usbmuxd/src/device.c device_version_input():
 *   mhdr->rx_seq = 0  ← trong header (KHÔNG phải 0xFFFF)
 *   dev->rx_seq = 0xFFFF  ← được set SAU KHI gửi xong
 *   dev->tx_seq KHÔNG thay đổi sau SETUP (vẫn là 0 cho SYN tiếp theo)
 */
int mux_do_setup(mux_conn_t *c) {
    /* Payload: 8 bytes version request */
    uint8_t payload[8];
    uint32_t *p = (uint32_t *)payload;
    p[0] = htonl(MUX_VERSION_MAJOR);   /* version major */
    p[1] = htonl(MUX_VERSION_MINOR);   /* version minor */

    /* Build header — dev_rx_seq PHẢI là 0 tại thời điểm này */
    uint8_t pkt[sizeof(mux_header_t) + sizeof(payload)];
    mux_header_t *h = (mux_header_t *)pkt;
    build_mux_header(h, c, MUX_MSG_SETUP, sizeof(payload));
    /* KIỂM TRA: rx_seq trong header phải là 0 (htonl(0)) */
    LOGI("[mux] SETUP: tx_seq=%u rx_seq=%u magic=0x%08X",
         c->dev_tx_seq, c->dev_rx_seq, MUX_MAGIC);

    memcpy(pkt + sizeof(mux_header_t), payload, sizeof(payload));

    if (write_all(c, pkt, sizeof(pkt)) < 0) {
        LOGE("SETUP write failed");
        return -1;
    }

    /* SAU KHI GỬI: set dev_rx_seq = 0xFFFF (như usbmuxd device.c) */
    c->dev_rx_seq = 0xFFFF;
    /* dev_tx_seq GIỮ NGUYÊN = 0 (không tăng sau SETUP) */

    /* Đọc response của thiết bị */
    uint8_t resp[sizeof(mux_header_t) + 8];
    if (read_all(c, resp, sizeof(resp)) < 0) {
        LOGE("SETUP read response failed");
        return -1;
    }
    mux_header_t *rh = (mux_header_t *)resp;
    uint32_t magic = ntohl(rh->magic);
    if (magic != MUX_MAGIC) {
        LOGE("SETUP response magic sai: 0x%08X (expected 0x%08X)", magic, MUX_MAGIC);
        return -1;
    }
    uint32_t *rv = (uint32_t *)(resp + sizeof(mux_header_t));
    uint32_t major = ntohl(rv[0]), minor = ntohl(rv[1]);
    LOGI("[mux] Thiết bị chấp nhận phiên bản usbmux %u.%u ✅", major, minor);
    return 0;
}

/*
 * mux_connect — Thực hiện TCP-over-USB handshake để kết nối đến dport.
 * Gửi SYN, nhận SYN+ACK, gửi ACK.
 * SYN dùng dev_tx_seq = 0 (= 0 vì chưa bị tăng sau SETUP).
 */
int mux_connect(mux_conn_t *c, int dport) {
    c->dport = dport;
    c->tx_seq = 0;
    c->rx_seq = 0;

    /* ── Gửi SYN ─────────────────────────────────────────────────────── */
    uint8_t syn_pkt[sizeof(mux_header_t) + sizeof(tcp_header_t)];
    memset(syn_pkt, 0, sizeof(syn_pkt));

    mux_header_t *mh = (mux_header_t *)syn_pkt;
    build_mux_header(mh, c, MUX_MSG_TCP, sizeof(tcp_header_t));

    tcp_header_t *th = (tcp_header_t *)(syn_pkt + sizeof(mux_header_t));
    th->sport    = htons(c->sport);
    th->dport    = htons(dport);
    th->seq      = htonl(c->tx_seq);
    th->ack      = htonl(0);
    th->offset   = 0x50;    /* data offset: 5 * 4 = 20 bytes, no options */
    th->flags    = TCP_SYN;
    th->wnd      = htons(0xFFFF);
    th->checksum = 0;
    th->urg      = 0;

    LOGI("[mux] SYN → port %d  dev_tx_seq=%u dev_rx_seq=%u",
         dport, c->dev_tx_seq, c->dev_rx_seq);

    if (write_all(c, syn_pkt, sizeof(syn_pkt)) < 0) {
        LOGE("SYN write failed"); return -1;
    }
    c->dev_tx_seq++;   /* SYN được tính, tăng cho lần sau */

    /* ── Đọc SYN+ACK ─────────────────────────────────────────────────── */
    uint8_t resp[sizeof(mux_header_t) + sizeof(tcp_header_t)];
    if (read_all(c, resp, sizeof(resp)) < 0) {
        LOGE("SYN+ACK read failed"); return -1;
    }
    mux_header_t *rm = (mux_header_t *)resp;
    c->dev_rx_seq = ntohl(rm->tx_seq);

    tcp_header_t *rt = (tcp_header_t *)(resp + sizeof(mux_header_t));
    if (!(rt->flags & TCP_SYN) || !(rt->flags & TCP_ACK)) {
        LOGE("Unexpected flags 0x%02X (expected SYN+ACK=0x12)", rt->flags);
        return -1;
    }
    c->rx_seq  = ntohl(rt->seq) + 1;   /* ack = remote seq + 1 */
    c->tx_seq  = ntohl(rt->ack);
    c->rx_window = ntohs(rt->wnd);
    LOGI("[mux] SYN+ACK nhận ✅ rx_seq=%u tx_seq=%u wnd=%u",
         c->rx_seq, c->tx_seq, c->rx_window);

    /* ── Gửi ACK ─────────────────────────────────────────────────────── */
    uint8_t ack_pkt[sizeof(mux_header_t) + sizeof(tcp_header_t)];
    memset(ack_pkt, 0, sizeof(ack_pkt));

    mux_header_t *am = (mux_header_t *)ack_pkt;
    build_mux_header(am, c, MUX_MSG_TCP, sizeof(tcp_header_t));

    tcp_header_t *at = (tcp_header_t *)(ack_pkt + sizeof(mux_header_t));
    at->sport    = htons(c->sport);
    at->dport    = htons(dport);
    at->seq      = htonl(c->tx_seq);
    at->ack      = htonl(c->rx_seq);
    at->offset   = 0x50;
    at->flags    = TCP_ACK;
    at->wnd      = htons(0xFFFF);

    if (write_all(c, ack_pkt, sizeof(ack_pkt)) < 0) {
        LOGE("ACK write failed"); return -1;
    }
    c->dev_tx_seq++;
    c->state = MUX_CONN_CONNECTED;
    LOGI("[mux] Kết nối TCP-over-USB thành công đến port %d ✅", dport);
    return 0;
}

/* Gửi data (DATA + PSH + ACK) */
int mux_send(mux_conn_t *c, const void *data, int len) {
    if (c->state != MUX_CONN_CONNECTED) return -1;
    int total = sizeof(mux_header_t) + sizeof(tcp_header_t) + len;
    uint8_t *pkt = malloc(total);
    if (!pkt) return -1;
    memset(pkt, 0, total);

    mux_header_t *mh = (mux_header_t *)pkt;
    build_mux_header(mh, c, MUX_MSG_TCP, sizeof(tcp_header_t) + len);

    tcp_header_t *th = (tcp_header_t *)(pkt + sizeof(mux_header_t));
    th->sport    = htons(c->sport);
    th->dport    = htons(c->dport);
    th->seq      = htonl(c->tx_seq);
    th->ack      = htonl(c->rx_seq);
    th->offset   = 0x50;
    th->flags    = TCP_ACK | 0x008; /* ACK + PSH */
    th->wnd      = htons(0xFFFF);
    memcpy(pkt + sizeof(mux_header_t) + sizeof(tcp_header_t), data, len);

    int r = write_all(c, pkt, total);
    free(pkt);
    if (r < 0) { LOGE("mux_send failed"); return -1; }

    c->tx_seq    += len;
    c->dev_tx_seq++;
    return len;
}

/* Nhận data — trả về số bytes thực tế */
int mux_recv(mux_conn_t *c, void *buf, int maxlen) {
    if (c->state != MUX_CONN_CONNECTED) return -1;
    uint8_t hdr_buf[sizeof(mux_header_t) + sizeof(tcp_header_t)];
    if (read_all(c, hdr_buf, sizeof(hdr_buf)) < 0) return -1;

    mux_header_t *mh = (mux_header_t *)hdr_buf;
    c->dev_rx_seq = ntohl(mh->tx_seq);
    uint32_t total = ntohl(mh->length);
    uint32_t payload = total - sizeof(mux_header_t) - sizeof(tcp_header_t);
    if (payload == 0) return 0;
    if ((int)payload > maxlen) payload = maxlen;

    tcp_header_t *th = (tcp_header_t *)(hdr_buf + sizeof(mux_header_t));
    c->rx_window = ntohs(th->wnd);

    int got = read_all(c, buf, payload);
    if (got < 0) return -1;
    c->rx_seq += got;

    /* Gửi ACK ngay */
    uint8_t ack_pkt[sizeof(mux_header_t) + sizeof(tcp_header_t)];
    memset(ack_pkt, 0, sizeof(ack_pkt));
    mux_header_t *am = (mux_header_t *)ack_pkt;
    build_mux_header(am, c, MUX_MSG_TCP, sizeof(tcp_header_t));
    tcp_header_t *at = (tcp_header_t *)(ack_pkt + sizeof(mux_header_t));
    at->sport  = htons(c->sport);
    at->dport  = htons(c->dport);
    at->seq    = htonl(c->tx_seq);
    at->ack    = htonl(c->rx_seq);
    at->offset = 0x50;
    at->flags  = TCP_ACK;
    at->wnd    = htons(0xFFFF);
    write_all(c, ack_pkt, sizeof(ack_pkt));
    c->dev_tx_seq++;
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
    /* Gửi FIN+ACK */
    uint8_t fin_pkt[sizeof(mux_header_t) + sizeof(tcp_header_t)];
    memset(fin_pkt, 0, sizeof(fin_pkt));
    mux_header_t *mh = (mux_header_t *)fin_pkt;
    build_mux_header(mh, c, MUX_MSG_TCP, sizeof(tcp_header_t));
    tcp_header_t *th = (tcp_header_t *)(fin_pkt + sizeof(mux_header_t));
    th->sport  = htons(c->sport);
    th->dport  = htons(c->dport);
    th->seq    = htonl(c->tx_seq);
    th->ack    = htonl(c->rx_seq);
    th->offset = 0x50;
    th->flags  = TCP_FIN | TCP_ACK;
    th->wnd    = htons(0xFFFF);
    write_all(c, fin_pkt, sizeof(fin_pkt));
    c->state = MUX_CONN_CLOSED;
    LOGI("[mux] Đã đóng kết nối TCP-over-USB.");
}

int mux_raw_write(mux_conn_t *c, const void *buf, int len) {
    return write_all(c, buf, len);
}
int mux_raw_read(mux_conn_t *c, void *buf, int maxlen) {
    return c->usb_read(buf, maxlen);
}
