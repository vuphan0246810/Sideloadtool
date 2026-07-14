#pragma once
/*
 * plist_util.h — Bộ build/parse plist XML tối giản, không phụ thuộc libplist.
 * Đủ để xử lý các plist mà lockdownd gửi/nhận (flat dict, string/data/integer).
 */
#include <stddef.h>

/* ── Kiểu giá trị plist ─────────────────────────────────────────────────── */
typedef enum {
    PLIST_STR  = 0,
    PLIST_DATA = 1,   /* base64 <data> */
    PLIST_INT  = 2,
    PLIST_BOOL = 3,
    PLIST_DICT = 4,   /* chỉ parse dict flat, không lồng nhau */
    PLIST_NONE = -1,
} plist_type_t;

typedef struct plist_item {
    char          *key;
    plist_type_t   type;
    char          *str_val;    /* for STR, DATA, DICT raw inner */
    long long      int_val;    /* for INT */
    int            bool_val;   /* for BOOL */
    struct plist_item *next;
} plist_item_t;

typedef struct {
    plist_item_t *head;
} plist_dict_t;

/* ── Builder ────────────────────────────────────────────────────────────── */
/* Trả về plist XML string được malloc(). Caller phải free(). */
char *plist_build_dict_request(const char *key, const char *value);
char *plist_build_pair_request(const char *req_type,
                                const char *device_cert_pem,
                                const char *host_cert_pem,
                                const char *root_cert_pem,
                                const char *host_id);
char *plist_build_start_service(const char *service_name);
char *plist_build_start_session(const char *system_buid, const char *host_id);
char *plist_build_install_request(const char *pkg_path);
char *plist_build_install_poll(void);

/* Xuất pair record hiện tại thành plist chuẩn kiểu Apple (idevicepair-style),
 * dùng cho tab "Ghép nối" — người dùng có thể lưu/chia sẻ file .plist này.
 * Certs/keys được strip PEM armor rồi nhúng dưới dạng <data> base64, giống
 * định dạng pairing record thật của libimobiledevice/usbmuxd. */
char *plist_build_pairing_export(const char *udid,
                                  const char *host_id,
                                  const char *system_buid,
                                  const char *root_cert_pem,
                                  const char *root_key_pem,
                                  const char *host_cert_pem,
                                  const char *host_key_pem,
                                  const char *device_cert_pem);

/* ── Parser ─────────────────────────────────────────────────────────────── */
plist_dict_t *plist_parse(const char *xml);
const char   *plist_get_str (plist_dict_t *d, const char *key);
long long     plist_get_int (plist_dict_t *d, const char *key);
void          plist_free    (plist_dict_t *d);

/* ── Base64 ─────────────────────────────────────────────────────────────── */
char   *b64_encode(const unsigned char *src, size_t len);
size_t  b64_decode(const char *src, unsigned char **out);
