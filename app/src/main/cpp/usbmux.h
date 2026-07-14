#pragma once
/*
 * usbmux.h — Giao thức usbmux Apple qua USB, dựa theo:
 *   usbmuxd/src/device.h  (struct mux_header, tcp_header, constants)
 *   usbmuxd/src/device.c  (state machine, SETUP/SYN/ACK/DATA/RST logic)
 *
 * *** 3 BUG ĐÃ SỬA SO VỚI mux_usb.py GỐC ***
 *   1. MAGIC   : 0xFADEDEAD   (Python gốc dùng 0xFEEDFACE — SAI)
 *   2. SETUP   : tx_seq=0, rx_seq=0 trong header, sau đó rx_seq nội bộ = 0xFFFF
 *                (Python gốc đặt self._rx_seq=0xFFFF TRƯỚC khi build header)
 *   3. SYN     : tx_seq=0, KHÔNG increment sau SETUP
 *                (Python gốc tăng tx_seq lên 1 trước SYN)
 */

#include <stdint.h>
#include <stddef.h>

/* ── Hằng số ─────────────────────────────────────────────────────────────── */
#define MUX_MAGIC          0xFADEDEAD   /* ⬅️ SỬA (gốc Python: 0xFEEDFACE)  */
#define MUX_VERSION_MAJOR  2
#define MUX_VERSION_MINOR  0

/* Message types (mhdr.message) */
#define MUX_MSG_SETUP   8
#define MUX_MSG_TCP     6

/* TCP flags */
#define TCP_SYN  0x002
#define TCP_ACK  0x010
#define TCP_RST  0x004
#define TCP_FIN  0x001

/* Cổng lockdownd */
#define LOCKDOWN_PORT 62078

/* ── Struct wire-format (big-endian trên dây) ────────────────────────────── */
#pragma pack(push, 1)
typedef struct {
    uint32_t magic;         /* 0xFADEDEAD */
    uint16_t mux_version;   /* 0 */
    uint16_t message;       /* MUX_MSG_SETUP hoặc MUX_MSG_TCP */
    uint32_t tx_seq;
    uint32_t rx_seq;        /* ⬅️ phải là 0 trong SETUP header */
    uint32_t length;        /* tổng bytes kể cả header */
} mux_header_t;

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
    int              sport;          /* source port ngẫu nhiên */
    int              dport;          /* dest port (LOCKDOWN_PORT) */
    uint32_t         tx_seq;         /* host → device seq */
    uint32_t         rx_seq;         /* device → host seq (ack) */
    uint32_t         rx_window;      /* receive window từ device */
    mux_conn_state_t state;

    /* device-level seq (usbmuxd device.c) */
    uint32_t         dev_tx_seq;     /* incrementing SETUP/TCP packet counter */
    uint32_t         dev_rx_seq;     /* last RX seq, starts 0xFFFF after SETUP */

    /* callbacks vào JNI USB layer */
    int (*usb_write)(const void *buf, int len);
    int (*usb_read )(void *buf, int len);
} mux_conn_t;

/* ── API ─────────────────────────────────────────────────────────────────── */
int  mux_conn_init (mux_conn_t *c,
                    int (*usb_write)(const void*, int),
                    int (*usb_read )(void*, int));

int  mux_do_setup (mux_conn_t *c);       /* gửi SETUP packet */
int  mux_connect  (mux_conn_t *c, int dport); /* SYN → ACK handshake */
int  mux_send     (mux_conn_t *c, const void *data, int len);
int  mux_recv     (mux_conn_t *c, void *buf, int maxlen);
int  mux_recv_exact(mux_conn_t *c, void *buf, int len);
void mux_disconnect(mux_conn_t *c);

/* Internal helpers exposed for lockdown/tls */
int  mux_raw_write(mux_conn_t *c, const void *buf, int len);
int  mux_raw_read (mux_conn_t *c, void *buf, int maxlen);
