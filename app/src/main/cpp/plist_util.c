/*
 * plist_util.c — Bộ build/parse plist XML tối giản, không dùng libplist.
 * Xử lý flat dict, string, data (base64), integer, boolean.
 */
#include "plist_util.h"
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <ctype.h>

/* ══════════════════════════════════════════════════════════════════════════
 * BASE64
 * ══════════════════════════════════════════════════════════════════════════ */
static const char b64_tbl[] =
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";

char *b64_encode(const unsigned char *src, size_t len) {
    size_t out_len = ((len + 2) / 3) * 4 + 1;
    char *out = malloc(out_len);
    if (!out) return NULL;
    size_t i = 0, j = 0;
    while (i < len) {
        unsigned int a = i < len ? src[i++] : 0;
        unsigned int b = i < len ? src[i++] : 0;
        unsigned int c = i < len ? src[i++] : 0;
        unsigned int triple = (a << 16) | (b << 8) | c;
        out[j++] = b64_tbl[(triple >> 18) & 0x3F];
        out[j++] = b64_tbl[(triple >> 12) & 0x3F];
        out[j++] = (i > len + 1) ? '=' : b64_tbl[(triple >> 6) & 0x3F];
        out[j++] = (i > len)     ? '=' : b64_tbl[(triple     ) & 0x3F];
    }
    out[j] = '\0';
    return out;
}

size_t b64_decode(const char *src, unsigned char **out) {
    size_t src_len = strlen(src);
    size_t out_len = (src_len / 4) * 3;
    if (src_len >= 1 && src[src_len-1] == '=') out_len--;
    if (src_len >= 2 && src[src_len-2] == '=') out_len--;
    *out = malloc(out_len + 1);
    if (!*out) return 0;

    static const uint8_t dec[256] = {
        ['A']=0,['B']=1,['C']=2,['D']=3,['E']=4,['F']=5,['G']=6,['H']=7,
        ['I']=8,['J']=9,['K']=10,['L']=11,['M']=12,['N']=13,['O']=14,['P']=15,
        ['Q']=16,['R']=17,['S']=18,['T']=19,['U']=20,['V']=21,['W']=22,['X']=23,
        ['Y']=24,['Z']=25,['a']=26,['b']=27,['c']=28,['d']=29,['e']=30,['f']=31,
        ['g']=32,['h']=33,['i']=34,['j']=35,['k']=36,['l']=37,['m']=38,['n']=39,
        ['o']=40,['p']=41,['q']=42,['r']=43,['s']=44,['t']=45,['u']=46,['v']=47,
        ['w']=48,['x']=49,['y']=50,['z']=51,['0']=52,['1']=53,['2']=54,['3']=55,
        ['4']=56,['5']=57,['6']=58,['7']=59,['8']=60,['9']=61,['+']=62,['/']=63,
    };
    size_t i = 0, j = 0;
    while (i < src_len) {
        uint8_t a = src[i] == '=' ? 0 : dec[(uint8_t)src[i]]; i++;
        uint8_t b = src[i] == '=' ? 0 : dec[(uint8_t)src[i]]; i++;
        uint8_t c = src[i] == '=' ? 0 : dec[(uint8_t)src[i]]; i++;
        uint8_t d = src[i] == '=' ? 0 : dec[(uint8_t)src[i]]; i++;
        uint32_t triple = (a<<18)|(b<<12)|(c<<6)|d;
        if (j < out_len) (*out)[j++] = (triple >> 16) & 0xFF;
        if (j < out_len) (*out)[j++] = (triple >>  8) & 0xFF;
        if (j < out_len) (*out)[j++] = (triple      ) & 0xFF;
    }
    (*out)[out_len] = '\0';
    return out_len;
}

/* ══════════════════════════════════════════════════════════════════════════
 * BUILDER
 * ══════════════════════════════════════════════════════════════════════════ */
static char *strbuf_printf(const char *fmt, ...) {
    va_list ap;
    va_start(ap, fmt);
    int n = vsnprintf(NULL, 0, fmt, ap);
    va_end(ap);
    char *buf = malloc(n + 1);
    if (!buf) return NULL;
    va_start(ap, fmt);
    vsnprintf(buf, n + 1, fmt, ap);
    va_end(ap);
    return buf;
}

/* Strip PEM headers/footers và whitespace để lấy raw base64 */
static char *pem_to_b64(const char *pem) {
    if (!pem) return strdup("");
    /* Bỏ qua dòng đầu (BEGIN...) và dòng cuối (END...) */
    const char *p = strchr(pem, '\n');
    if (!p) return strdup(pem);
    p++;
    /* Tìm dòng END */
    const char *end = strstr(p, "-----END");
    size_t len = end ? (size_t)(end - p) : strlen(p);
    /* Copy và xoá newlines */
    char *buf = malloc(len + 1);
    if (!buf) return NULL;
    size_t j = 0;
    for (size_t i = 0; i < len; i++) {
        if (p[i] != '\n' && p[i] != '\r') buf[j++] = p[i];
    }
    buf[j] = '\0';
    return buf;
}

static const char *PLIST_HEADER =
    "<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n"
    "<!DOCTYPE plist PUBLIC \"-//Apple//DTD PLIST 1.0//EN\""
    " \"http://www.apple.com/DTDs/PropertyList-1.0.dtd\">\n"
    "<plist version=\"1.0\"><dict>";
static const char *PLIST_FOOTER = "</dict></plist>";

char *plist_build_dict_request(const char *key, const char *value) {
    /* Used for simple key-value requests like GetValue */
    char *result = NULL;
    int n = asprintf(&result,
        "%s<key>%s</key><string>%s</string>%s",
        PLIST_HEADER, key, value, PLIST_FOOTER);
    return (n > 0) ? result : NULL;
}

char *plist_build_pair_request(const char *req_type,
                                const char *device_cert_pem,
                                const char *host_cert_pem,
                                const char *root_cert_pem,
                                const char *host_id) {
    char *dc = pem_to_b64(device_cert_pem);
    char *hc = pem_to_b64(host_cert_pem);
    char *rc = pem_to_b64(root_cert_pem);
    char *result = NULL;
    asprintf(&result,
        "%s"
        "<key>Label</key><string>SideloadAndroid</string>"
        "<key>PairingOptions</key><dict>"
          "<key>ExtendedPairingErrors</key><true/>"
        "</dict>"
        "<key>PairRecord</key><dict>"
          "<key>DeviceCertificate</key><data>%s</data>"
          "<key>HostCertificate</key><data>%s</data>"
          "<key>RootCertificate</key><data>%s</data>"
          "<key>HostID</key><string>%s</string>"
          "<key>SystemBUID</key><string>00000000-0000-0000-0000-000000000000</string>"
        "</dict>"
        "<key>Request</key><string>%s</string>"
        "%s",
        PLIST_HEADER, dc, hc, rc, host_id, req_type, PLIST_FOOTER);
    free(dc); free(hc); free(rc);
    return result;
}

char *plist_build_start_service(const char *service_name) {
    char *result = NULL;
    asprintf(&result,
        "%s"
        "<key>Label</key><string>SideloadAndroid</string>"
        "<key>Request</key><string>StartService</string>"
        "<key>Service</key><string>%s</string>"
        "%s",
        PLIST_HEADER, service_name, PLIST_FOOTER);
    return result;
}

char *plist_build_start_session(const char *system_buid, const char *host_id) {
    char *result = NULL;
    asprintf(&result,
        "%s"
        "<key>Label</key><string>SideloadAndroid</string>"
        "<key>Request</key><string>StartSession</string>"
        "<key>HostID</key><string>%s</string>"
        "<key>SystemBUID</key><string>%s</string>"
        "%s",
        PLIST_HEADER, host_id, system_buid, PLIST_FOOTER);
    return result;
}

char *plist_build_install_request(const char *pkg_path) {
    char *result = NULL;
    asprintf(&result,
        "%s"
        "<key>Command</key><string>Install</string>"
        "<key>PackagePath</key><string>%s</string>"
        "<key>ClientOptions</key><dict></dict>"
        "%s",
        PLIST_HEADER, pkg_path, PLIST_FOOTER);
    return result;
}

char *plist_build_install_poll(void) {
    char *result = NULL;
    asprintf(&result, "%s<key>Command</key><string>Lookup</string>%s",
             PLIST_HEADER, PLIST_FOOTER);
    return result;
}

/* ══════════════════════════════════════════════════════════════════════════
 * PARSER — Flat dict, không lồng nhau (đủ cho lockdownd responses)
 * ══════════════════════════════════════════════════════════════════════════ */
static char *extract_tag(const char *xml, const char *tag, size_t *consumed) {
    char open[64], close[64];
    snprintf(open,  sizeof(open),  "<%s>",  tag);
    snprintf(close, sizeof(close), "</%s>", tag);
    const char *s = strstr(xml, open);
    if (!s) { if (consumed) *consumed = 0; return NULL; }
    s += strlen(open);
    const char *e = strstr(s, close);
    if (!e) { if (consumed) *consumed = 0; return NULL; }
    size_t len = e - s;
    char *val = malloc(len + 1);
    memcpy(val, s, len);
    val[len] = '\0';
    if (consumed) *consumed = (e + strlen(close)) - xml;
    return val;
}

plist_dict_t *plist_parse(const char *xml) {
    plist_dict_t *d = calloc(1, sizeof(plist_dict_t));
    if (!d) return NULL;

    const char *p = xml;
    while (*p) {
        /* Tìm <key>...</key> */
        const char *ks = strstr(p, "<key>");
        if (!ks) break;
        ks += 5;
        const char *ke = strstr(ks, "</key>");
        if (!ke) break;
        size_t klen = ke - ks;
        char *key = malloc(klen + 1);
        memcpy(key, ks, klen);
        key[klen] = '\0';
        p = ke + 6;

        /* Skip whitespace */
        while (*p && isspace((unsigned char)*p)) p++;

        plist_item_t *item = calloc(1, sizeof(plist_item_t));
        item->key = key;

        if (strncmp(p, "<string>", 8) == 0) {
            item->type = PLIST_STR;
            item->str_val = extract_tag(p, "string", NULL);
            const char *e = strstr(p, "</string>");
            p = e ? e + 9 : p + 8;
        } else if (strncmp(p, "<data>", 6) == 0) {
            item->type = PLIST_DATA;
            item->str_val = extract_tag(p, "data", NULL);
            const char *e = strstr(p, "</data>");
            p = e ? e + 7 : p + 6;
        } else if (strncmp(p, "<integer>", 9) == 0) {
            item->type = PLIST_INT;
            char *v = extract_tag(p, "integer", NULL);
            if (v) { item->int_val = atoll(v); free(v); }
            const char *e = strstr(p, "</integer>");
            p = e ? e + 10 : p + 9;
        } else if (strncmp(p, "<true/>", 7) == 0) {
            item->type = PLIST_BOOL; item->bool_val = 1; p += 7;
        } else if (strncmp(p, "<false/>", 8) == 0) {
            item->type = PLIST_BOOL; item->bool_val = 0; p += 8;
        } else if (strncmp(p, "<dict>", 6) == 0) {
            item->type = PLIST_DICT;
            const char *de = strstr(p, "</dict>");
            if (de) {
                size_t dl = de - p - 6;
                item->str_val = malloc(dl + 1);
                memcpy(item->str_val, p + 6, dl);
                item->str_val[dl] = '\0';
                p = de + 7;
            } else p += 6;
        } else {
            /* Unknown tag — skip */
            free(item->key);
            free(item);
            p++;
            continue;
        }

        /* Append to list */
        item->next = NULL;
        if (!d->head) {
            d->head = item;
        } else {
            plist_item_t *cur = d->head;
            while (cur->next) cur = cur->next;
            cur->next = item;
        }
    }
    return d;
}

const char *plist_get_str(plist_dict_t *d, const char *key) {
    for (plist_item_t *i = d->head; i; i = i->next)
        if (strcmp(i->key, key) == 0 && i->str_val) return i->str_val;
    return NULL;
}

long long plist_get_int(plist_dict_t *d, const char *key) {
    for (plist_item_t *i = d->head; i; i = i->next)
        if (strcmp(i->key, key) == 0 && i->type == PLIST_INT) return i->int_val;
    return 0;
}

void plist_free(plist_dict_t *d) {
    if (!d) return;
    plist_item_t *cur = d->head;
    while (cur) {
        plist_item_t *n = cur->next;
        free(cur->key);
        free(cur->str_val);
        free(cur);
        cur = n;
    }
    free(d);
}
