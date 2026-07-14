/*
 * jni_bridge.c — JNI entry points cho native sideload library.
 * Kết nối C protocol layer ↔ Kotlin/Java Android layer.
 *
 * Exported JNI methods (được gọi từ NativeBridge.kt):
 *   nativeInit(filesDir: String)
 *   nativeConnect() → Boolean
 *   nativePair()    → Boolean
 *   nativeSideload(ipaPath: String) → Boolean
 *   nativeGetUdid() → String?
 *   nativeReset()
 *
 * C → Kotlin callbacks (được gọi từ bên trong C):
 *   UsbTransport.nativeBulkWrite(data: ByteArray, timeoutMs: Int) → Int
 *   UsbTransport.nativeBulkRead (buf:  ByteArray, timeoutMs: Int) → Int
 *   CertHelper.generateCertChain(devicePubDer: ByteArray) → Array<String>?
 *   NativeBridge.onNativeLog(line: String)
 *   NativeBridge.onTrustRequired()
 *   NativeBridge.dismissTrust()
 *   TlsHelper.handshake(certPem: ByteArray, keyPem: ByteArray, connPtr: Long) → Boolean
 */
#include "usbmux.h"
#include "lockdown.h"
#include "pairing.h"
#include "afc.h"
#include "install_proxy.h"
#include <jni.h>
#include <stdio.h>
#include <string.h>
#include <stdlib.h>
#include <android/log.h>

#define TAG "jni_bridge"
#define LOGI(...) __android_log_print(ANDROID_LOG_INFO,  TAG, __VA_ARGS__)
#define LOGE(...) __android_log_print(ANDROID_LOG_ERROR, TAG, __VA_ARGS__)

/* ── Global state ────────────────────────────────────────────────────────── */
static JavaVM      *g_jvm      = NULL;
static mux_conn_t   g_mux;
static lockdown_t   g_ld;
static pair_record_t g_rec;
static char         g_files_dir[512];
static char         g_udid[64];

/* ── JNI_OnLoad ──────────────────────────────────────────────────────────── */
JNIEXPORT jint JNICALL JNI_OnLoad(JavaVM *vm, void *reserved) {
    (void)reserved;
    g_jvm = vm;
    LOGI("libsideloadnative loaded.");
    return JNI_VERSION_1_6;
}

/* ── USB bulk I/O callbacks (gọi UsbTransport static methods từ C) ────────── */
static int usb_bulk_write(const void *buf, int len) {
    JNIEnv *env = NULL;
    (*g_jvm)->GetEnv(g_jvm, (void**)&env, JNI_VERSION_1_6);
    if (!env) return -1;

    jclass cls = (*env)->FindClass(env, "com/superalpha/sideload/bridge/UsbTransport");
    if (!cls) return -1;
    jmethodID mid = (*env)->GetStaticMethodID(env, cls, "nativeBulkWrite", "([BI)I");
    if (!mid) return -1;

    jbyteArray jarr = (*env)->NewByteArray(env, len);
    (*env)->SetByteArrayRegion(env, jarr, 0, len, (const jbyte*)buf);
    jint result = (*env)->CallStaticIntMethod(env, cls, mid, jarr, (jint)5000);
    (*env)->DeleteLocalRef(env, jarr);
    (*env)->DeleteLocalRef(env, cls);
    return (int)result;
}

static int usb_bulk_read(void *buf, int len) {
    JNIEnv *env = NULL;
    (*g_jvm)->GetEnv(g_jvm, (void**)&env, JNI_VERSION_1_6);
    if (!env) return -1;

    jclass cls = (*env)->FindClass(env, "com/superalpha/sideload/bridge/UsbTransport");
    if (!cls) return -1;
    jmethodID mid = (*env)->GetStaticMethodID(env, cls, "nativeBulkRead", "([BI)I");
    if (!mid) return -1;

    jbyteArray jarr = (*env)->NewByteArray(env, len);
    jint n = (*env)->CallStaticIntMethod(env, cls, mid, jarr, (jint)5000);
    if (n > 0) (*env)->GetByteArrayRegion(env, jarr, 0, n, (jbyte*)buf);
    (*env)->DeleteLocalRef(env, jarr);
    (*env)->DeleteLocalRef(env, cls);
    return (int)n;
}

/* ── Log callback (gọi NativeBridge.onNativeLog) ─────────────────────────── */
static void jni_log(JNIEnv *env, const char *line) {
    jclass cls = (*env)->FindClass(env, "com/superalpha/sideload/bridge/NativeBridge");
    if (!cls) return;
    jmethodID mid = (*env)->GetStaticMethodID(env, cls, "onNativeLog", "(Ljava/lang/String;)V");
    if (!mid) { (*env)->DeleteLocalRef(env, cls); return; }
    jstring jline = (*env)->NewStringUTF(env, line);
    (*env)->CallStaticVoidMethod(env, cls, mid, jline);
    (*env)->DeleteLocalRef(env, jline);
    (*env)->DeleteLocalRef(env, cls);
}

/* ── TLS helper: gọi TlsHelper.handshake() từ C ─────────────────────────── */
static int jni_start_tls(JNIEnv *env, lockdown_t *ld) {
    jclass cls = (*env)->FindClass(env, "com/superalpha/sideload/bridge/TlsHelper");
    if (!cls) return -1;
    jmethodID mid = (*env)->GetStaticMethodID(env, cls, "handshake", "([B[BJ)Z");
    if (!mid) { (*env)->DeleteLocalRef(env, cls); return -1; }

    /* Convert PEM strings → jbyteArray */
    const char *cert_pem = ld->host_cert_pem ? ld->host_cert_pem : "";
    const char *key_pem  = ld->host_key_pem  ? ld->host_key_pem  : "";
    int clen = (int)strlen(cert_pem);
    int klen = (int)strlen(key_pem);

    jbyteArray jcert = (*env)->NewByteArray(env, clen);
    jbyteArray jkey  = (*env)->NewByteArray(env, klen);
    (*env)->SetByteArrayRegion(env, jcert, 0, clen, (const jbyte*)cert_pem);
    (*env)->SetByteArrayRegion(env, jkey,  0, klen, (const jbyte*)key_pem);

    jlong conn_ptr = (jlong)(intptr_t)ld;
    jboolean ok = (*env)->CallStaticBooleanMethod(env, cls, mid, jcert, jkey, conn_ptr);

    (*env)->DeleteLocalRef(env, jcert);
    (*env)->DeleteLocalRef(env, jkey);
    (*env)->DeleteLocalRef(env, cls);
    if ((*env)->ExceptionCheck(env)) { (*env)->ExceptionClear(env); return -1; }
    ld->tls_active = (ok == JNI_TRUE) ? 1 : 0;
    return ok == JNI_TRUE ? 0 : -1;
}

/* ── NativeBridge.onTrustRequired / dismissTrust ─────────────────────────── */
/* (Cũng được gọi từ pairing.c qua jni_bridge, nhưng định nghĩa ở đây để
 *  pairing.c gọi trực tiếp qua JNI với env đã có sẵn.) */

/* ══════════════════════════════════════════════════════════════════════════
 * JNI EXPORTED METHODS
 * ══════════════════════════════════════════════════════════════════════════ */

#define PKG "com/superalpha/sideload/bridge/NativeBridge"

JNIEXPORT void JNICALL
Java_com_superalpha_sideload_bridge_NativeBridge_nativeInit(
        JNIEnv *env, jobject thiz, jstring j_files_dir) {
    const char *fd = (*env)->GetStringUTFChars(env, j_files_dir, NULL);
    strncpy(g_files_dir, fd, sizeof(g_files_dir) - 1);
    (*env)->ReleaseStringUTFChars(env, j_files_dir, fd);
    memset(&g_mux, 0, sizeof(g_mux));
    memset(&g_ld,  0, sizeof(g_ld));
    memset(&g_rec, 0, sizeof(g_rec));
    memset(g_udid, 0, sizeof(g_udid));
    char _logbuf[600]; snprintf(_logbuf, sizeof(_logbuf), "[native] nativeInit: files_dir=%s", g_files_dir); jni_log(env, _logbuf);
    LOGI("nativeInit: files_dir=%s", g_files_dir);
}

JNIEXPORT jboolean JNICALL
Java_com_superalpha_sideload_bridge_NativeBridge_nativeConnect(
        JNIEnv *env, jobject thiz) {
    jni_log(env, "[mux] Bắt đầu kết nối usbmux...");

    /* 1. Khởi tạo mux connection */
    if (mux_conn_init(&g_mux, usb_bulk_write, usb_bulk_read) < 0) {
        jni_log(env, "[mux] ❌ Không khởi tạo được mux conn");
        return JNI_FALSE;
    }

    /* 2. SETUP packet → thỏa thuận usbmux 2.0 */
    jni_log(env, "[mux] Gửi SETUP packet...");
    if (mux_do_setup(&g_mux) < 0) {
        jni_log(env, "[mux] ❌ SETUP thất bại");
        return JNI_FALSE;
    }
    jni_log(env, "[mux] ✅ SETUP thành công");

    /* 3. Mở lockdown connection (kết nối TCP-over-USB đến port 62078) */
    jni_log(env, "[lockdown] Kết nối đến lockdownd port 62078...");
    if (lockdown_open(&g_ld, &g_mux) < 0) {
        jni_log(env, "[lockdown] ❌ Kết nối lockdownd thất bại");
        return JNI_FALSE;
    }
    jni_log(env, "[lockdown] ✅ Kết nối lockdownd thành công");

    /* 4. GetValue UDID */
    char *udid = NULL;
    lockdown_get_value(&g_ld, NULL, "UniqueDeviceID", &udid);
    if (udid) {
        strncpy(g_udid, udid, sizeof(g_udid) - 1);
        char log_buf[128];
        snprintf(log_buf, sizeof(log_buf), "[device] UDID: %s", g_udid);
        jni_log(env, log_buf);
        free(udid);
    }
    return JNI_TRUE;
}

JNIEXPORT jboolean JNICALL
Java_com_superalpha_sideload_bridge_NativeBridge_nativePair(
        JNIEnv *env, jobject thiz) {
    jni_log(env, "[pairing] Bắt đầu pairing...");

    /* Kiểm tra xem đã có pair record chưa */
    char pair_path[600];
    snprintf(pair_path, sizeof(pair_path), "%s/pair_record_%s.txt",
             g_files_dir, g_udid[0] ? g_udid : "default");

    if (pairing_exists(pair_path)) {
        jni_log(env, "[pairing] Đã có pair record, load lại...");
        if (pairing_load(&g_rec, pair_path) == 0) {
            jni_log(env, "[pairing] ✅ Pair record hợp lệ, bỏ qua re-pair.");
            return JNI_TRUE;
        }
        jni_log(env, "[pairing] ⚠️ Pair record không hợp lệ, thực hiện pair lại...");
    }

    /* Thực hiện pairing */
    if (pairing_do(&g_ld, &g_rec, env, NULL) < 0) {
        jni_log(env, "[pairing] ❌ Pairing thất bại");
        return JNI_FALSE;
    }

    /* Lưu pair record */
    pairing_save(&g_rec, pair_path);
    jni_log(env, "[pairing] ✅ Pairing hoàn tất và đã lưu pair record.");

    /* StartSession → StartTLS */
    jni_log(env, "[lockdown] Bắt đầu StartSession...");
    char *start_sess_req = plist_build_start_session(
        g_rec.system_buid ? g_rec.system_buid : "00000000-0000-0000-0000-000000000000",
        g_rec.host_id     ? g_rec.host_id     : "");
    plist_dict_t *ss_resp = NULL;
    lockdown_exchange(&g_ld, start_sess_req, &ss_resp);
    free(start_sess_req);

    const char *use_ssl_str = ss_resp ? plist_get_str(ss_resp, "EnableSessionSSL") : NULL;
    int use_ssl = use_ssl_str && strcmp(use_ssl_str, "true") == 0;
    if (ss_resp) plist_free(ss_resp);

    if (use_ssl) {
        jni_log(env, "[tls] Bắt đầu TLS handshake...");
        g_ld.host_cert_pem = strdup(g_rec.host_cert_pem ? g_rec.host_cert_pem : "");
        g_ld.host_key_pem  = strdup(g_rec.host_key_pem  ? g_rec.host_key_pem  : "");
        if (jni_start_tls(env, &g_ld) < 0) {
            jni_log(env, "[tls] ❌ TLS handshake thất bại");
            return JNI_FALSE;
        }
        jni_log(env, "[tls] ✅ TLS handshake thành công");
    }
    return JNI_TRUE;
}

JNIEXPORT jboolean JNICALL
Java_com_superalpha_sideload_bridge_NativeBridge_nativeSideload(
        JNIEnv *env, jobject thiz, jstring j_ipa_path) {
    const char *ipa_path = (*env)->GetStringUTFChars(env, j_ipa_path, NULL);
    char log_buf[512];
    snprintf(log_buf, sizeof(log_buf), "[sideload] Bắt đầu cài đặt %s...", ipa_path);
    jni_log(env, log_buf);

    /* 1. StartService: com.apple.afc */
    int afc_port = 0, afc_ssl = 0;
    jni_log(env, "[lockdown] StartService: com.apple.afc...");
    if (lockdown_start_service(&g_ld, "com.apple.afc", &afc_port, &afc_ssl) < 0) {
        jni_log(env, "[lockdown] ❌ StartService afc thất bại");
        (*env)->ReleaseStringUTFChars(env, j_ipa_path, ipa_path);
        return JNI_FALSE;
    }

    /* 2. Kết nối mux port afc */
    mux_conn_t afc_mux;
    mux_conn_init(&afc_mux, usb_bulk_write, usb_bulk_read);
    if (mux_connect(&afc_mux, afc_port) < 0) {
        jni_log(env, "[afc] ❌ Kết nối AFC port thất bại");
        (*env)->ReleaseStringUTFChars(env, j_ipa_path, ipa_path);
        return JNI_FALSE;
    }

    /* 3. Push IPA qua AFC */
    afc_t afc;
    afc_open(&afc, &afc_mux);
    afc_mkdir(&afc, "/PublicStaging");

    /* Tên file IPA */
    const char *fname = strrchr(ipa_path, '/');
    fname = fname ? fname + 1 : ipa_path;
    char remote_path[256];
    snprintf(remote_path, sizeof(remote_path), "/PublicStaging/%s", fname);

    jni_log(env, "[afc] Đẩy IPA lên thiết bị...");
    if (afc_push_file(&afc, ipa_path, remote_path, NULL) < 0) {
        jni_log(env, "[afc] ❌ Đẩy IPA thất bại");
        afc_close(&afc);
        mux_disconnect(&afc_mux);
        (*env)->ReleaseStringUTFChars(env, j_ipa_path, ipa_path);
        return JNI_FALSE;
    }
    jni_log(env, "[afc] ✅ Đẩy IPA thành công");
    afc_close(&afc);
    mux_disconnect(&afc_mux);

    /* 4. StartService: com.apple.mobile.installation_proxy */
    int ip_port = 0, ip_ssl = 0;
    jni_log(env, "[lockdown] StartService: com.apple.mobile.installation_proxy...");
    if (lockdown_start_service(&g_ld, "com.apple.mobile.installation_proxy",
                               &ip_port, &ip_ssl) < 0) {
        jni_log(env, "[lockdown] ❌ StartService installation_proxy thất bại");
        (*env)->ReleaseStringUTFChars(env, j_ipa_path, ipa_path);
        return JNI_FALSE;
    }

    /* 5. Kết nối install proxy port */
    mux_conn_t ip_mux;
    mux_conn_init(&ip_mux, usb_bulk_write, usb_bulk_read);
    if (mux_connect(&ip_mux, ip_port) < 0) {
        jni_log(env, "[install_proxy] ❌ Kết nối port thất bại");
        (*env)->ReleaseStringUTFChars(env, j_ipa_path, ipa_path);
        return JNI_FALSE;
    }

    /* 6. Gửi Install command */
    install_proxy_t ip;
    install_proxy_open(&ip, &ip_mux);
    jni_log(env, "[install_proxy] Gửi lệnh Install...");

    /* Progress callback (log via JNI) */
    /* Note: không thể dùng closure trong C, dùng global log */
    int r = install_proxy_install(&ip, remote_path, NULL);
    install_proxy_close(&ip);
    mux_disconnect(&ip_mux);

    (*env)->ReleaseStringUTFChars(env, j_ipa_path, ipa_path);

    if (r < 0) {
        jni_log(env, "[install_proxy] ❌ Cài đặt thất bại");
        return JNI_FALSE;
    }
    jni_log(env, "[install_proxy] ✅ Cài đặt hoàn tất!");
    return JNI_TRUE;
}

JNIEXPORT jstring JNICALL
Java_com_superalpha_sideload_bridge_NativeBridge_nativeGetUdid(
        JNIEnv *env, jobject thiz) {
    if (g_udid[0]) return (*env)->NewStringUTF(env, g_udid);
    return NULL;
}

/* ── Tab "Ghép nối" (Pairing): kiểm tra trạng thái + xuất file pairing ────── */
JNIEXPORT jboolean JNICALL
Java_com_superalpha_sideload_bridge_NativeBridge_nativeIsPaired(
        JNIEnv *env, jobject thiz) {
    (void)env; (void)thiz;
    return (g_rec.host_id && g_rec.host_id[0] &&
            g_rec.host_cert_pem && g_rec.host_cert_pem[0]) ? JNI_TRUE : JNI_FALSE;
}

JNIEXPORT jstring JNICALL
Java_com_superalpha_sideload_bridge_NativeBridge_nativeGetPairingPlist(
        JNIEnv *env, jobject thiz) {
    (void)thiz;
    if (!g_rec.host_id || !g_rec.host_id[0]) {
        jni_log(env, "[pairing] Chưa có pair record để xuất — hãy ghép nối trước.");
        return NULL;
    }
    char *xml = plist_build_pairing_export(
        g_udid[0] ? g_udid : "unknown",
        g_rec.host_id,
        g_rec.system_buid ? g_rec.system_buid : "00000000-0000-0000-0000-000000000000",
        g_rec.root_cert_pem, g_rec.root_key_pem,
        g_rec.host_cert_pem, g_rec.host_key_pem,
        g_rec.device_cert_pem);
    if (!xml) return NULL;
    jstring result = (*env)->NewStringUTF(env, xml);
    free(xml);
    return result;
}

JNIEXPORT void JNICALL
Java_com_superalpha_sideload_bridge_NativeBridge_nativeReset(
        JNIEnv *env, jobject thiz) {
    lockdown_close(&g_ld);
    mux_disconnect(&g_mux);
    pairing_free(&g_rec);
    memset(&g_mux,  0, sizeof(g_mux));
    memset(&g_ld,   0, sizeof(g_ld));
    memset(g_udid,  0, sizeof(g_udid));
    jni_log(env, "[native] Reset hoàn tất.");
}

/* ── TlsHelper C callbacks (được gọi từ TlsHelper.kt external fun) ─────────
 * TlsHelper cần gửi/nhận raw bytes qua mux connection.
 * Các hàm này expose mux send/recv cho Kotlin.
 * ── */
JNIEXPORT jboolean JNICALL
Java_com_superalpha_sideload_bridge_TlsHelper_nativeTlsSend(
        JNIEnv *env, jclass cls, jlong conn_ptr, jbyteArray jdata) {
    (void)env; (void)cls;
    lockdown_t *ld = (lockdown_t *)(intptr_t)conn_ptr;
    if (!ld || !ld->mux) return JNI_FALSE;
    jsize len = (*env)->GetArrayLength(env, jdata);
    jbyte *data = (*env)->GetByteArrayElements(env, jdata, NULL);
    int r = mux_send(ld->mux, data, (int)len);
    (*env)->ReleaseByteArrayElements(env, jdata, data, JNI_ABORT);
    return r >= 0 ? JNI_TRUE : JNI_FALSE;
}

JNIEXPORT jbyteArray JNICALL
Java_com_superalpha_sideload_bridge_TlsHelper_nativeTlsRecv(
        JNIEnv *env, jclass cls, jlong conn_ptr, jint max_len) {
    (void)cls;
    lockdown_t *ld = (lockdown_t *)(intptr_t)conn_ptr;
    if (!ld || !ld->mux) return NULL;
    char *buf = malloc(max_len);
    int n = mux_recv(ld->mux, buf, max_len);
    if (n <= 0) { free(buf); return NULL; }
    jbyteArray jarr = (*env)->NewByteArray(env, n);
    (*env)->SetByteArrayRegion(env, jarr, 0, n, (jbyte*)buf);
    free(buf);
    return jarr;
}
