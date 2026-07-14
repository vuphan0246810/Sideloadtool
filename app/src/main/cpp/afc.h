#pragma once
/* afc.h — AFC (Apple File Conduit) protocol client.
 * Tham chiếu: libimobiledevice/src/afc.c
 * AFC dùng little-endian headers (khác mux vốn là big-endian).
 */
#include "usbmux.h"

#define AFC_MAGIC   "CFA6LPAA"  /* 8 bytes */
#define AFC_FOPEN_WRONLY   0x3  /* write-only, create */
#define AFC_FOPEN_RW       0x2  /* read-write */

typedef enum {
    AFC_OP_STAT         = 0x0001,
    AFC_OP_LIST_DIR     = 0x0003,
    AFC_OP_MAKE_DIR     = 0x0006,
    AFC_OP_DELETE       = 0x0008,
    AFC_OP_FILE_OPEN    = 0x000D,
    AFC_OP_FILE_CLOSE   = 0x0014,
    AFC_OP_FILE_READ    = 0x000F,
    AFC_OP_FILE_WRITE   = 0x0010,
    AFC_OP_FILE_TELL    = 0x0011,
    AFC_OP_STATUS       = 0x0000,
    AFC_OP_DATA         = 0x0001,
} afc_op_t;

#pragma pack(push, 1)
typedef struct {
    char     magic[8];      /* "CFA6LPAA" */
    uint64_t entire_length; /* tổng bytes kể cả header */
    uint64_t this_length;   /* thường = entire_length */
    uint64_t packet_num;    /* sequence number, tăng dần */
    uint64_t operation;     /* AFC_OP_* */
} afc_header_t;
#pragma pack(pop)

typedef struct {
    mux_conn_t *mux;
    uint64_t    next_pkt;   /* packet_num counter */
} afc_t;

int  afc_open   (afc_t *a, mux_conn_t *mux);
int  afc_mkdir  (afc_t *a, const char *path);
int  afc_push_file(afc_t *a, const char *local_path, const char *remote_path,
                   /* progress callback */ void (*progress)(int pct));
void afc_close  (afc_t *a);
