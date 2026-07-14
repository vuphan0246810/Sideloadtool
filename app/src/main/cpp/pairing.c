/*
 * pairing.c — Apple device pairing flow.
 * Tham chiếu: libimobiledevice/src/lockdown.c lockdownd_pair()
 *
 * Flow:
 *  1. GetValue(DevicePublicKey) ← lấy RSA public key của thiết bị
 *  2. CertHelper.generateCertChain(devicePublicKey) ← tạo root/host/device cert
 *  3. Gửi Pair request (PairRecord: DeviceCertificate, HostCertificate, RootCertificate)
 *  4. Nếu PairingDialogResponsePending → chờ Trust popup (tối đa 120s)
 *  5. Nếu Success → lưu pair record, gọi StartSession → StartTLS
 */
#include "pairing.h"
#include "uuid_compat.h"
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <android/log.h>
#include <jni.h>

#define TAG "pairing"
#define LOGI(...) __android_log_print(ANDROID_LOG_INFO,  TAG, __VA_ARGS__)
#define LOGE(...) __android_log_print(ANDROID_LOG_ERROR, TAG, __VA_ARGS__)

/* ── JNI helper: gọi CertHelper.generateCertChain(devicePubDer) ─────────── */
static int jni_generate_certs(JNIEnv *env, const unsigned char *dev_pub_der,
                               int dev_pub_len, pair_record_t *rec) {
    jclass cls = (*env)->FindClass(env, "com/superalpha/sideload/bridge/CertHelper");
    if (!cls) { LOGE("Không tìm thấy CertHelper"); return -1; }

    jmethodID mid = (*env)->GetStaticMethodID(env, cls, "generateCertChain", "([B)[Ljava/lang/String;");
    if (!mid) { LOGE("Không tìm thấy generateCertChain"); return -1; }

    jbyteArray jarr = (*env)->NewByteArray(env, dev_pub_len);
    (*env)->SetByteArrayRegion(env, jarr, 0, dev_pub_len, (const jbyte*)dev_pub_der);

    jobjectArray result = (jobjectArray)(*env)->CallStaticObjectMethod(env, cls, mid, jarr);
    (*env)->DeleteLocalRef(env, jarr);

    if (!result || (*env)->ExceptionCheck(env)) {
        (*env)->ExceptionClear(env);
        LOGE("generateCertChain thất bại");
        return -1;
    }
    int len = (*env)->GetArrayLength(env, result);
    if (len < 5) { LOGE("generateCertChain trả về %d phần tử (cần 5)", len); return -1; }

    const char *labels[] = { "root_cert", "root_key", "host_cert", "host_key", "device_cert" };
    char **targets[] = { &rec->root_cert_pem, &rec->root_key_pem,
                         &rec->host_cert_pem, &rec->host_key_pem,
                         &rec->device_cert_pem };
    for (int i = 0; i < 5; i++) {
        jstring js = (jstring)(*env)->GetObjectArrayElement(env, result, i);
        const char *str = (*env)->GetStringUTFChars(env, js, NULL);
        *targets[i] = strdup(str);
        (*env)->ReleaseStringUTFChars(env, js, str);
        (*env)->DeleteLocalRef(env, js);
        LOGI("CertHelper[%d] %s: %zu bytes", i, labels[i], strlen(*targets[i]));
    }
    return 0;
}

/* ── JNI helper: gọi NativeBridge.onTrustRequired() ─────────────────────── */
static void jni_notify_trust(JNIEnv *env) {
    jclass cls = (*env)->FindClass(env, "com/superalpha/sideload/bridge/NativeBridge");
    if (!cls) return;
    jmethodID mid = (*env)->GetStaticMethodID(env, cls, "onTrustRequired", "()V");
    if (!mid) return;
    (*env)->CallStaticVoidMethod(env, cls, mid);
    LOGI("[pairing] Trust popup notification gửi đến UI.");
}

static void jni_dismiss_trust(JNIEnv *env) {
    jclass cls = (*env)->FindClass(env, "com/superalpha/sideload/bridge/NativeBridge");
    if (!cls) return;
    jmethodID mid = (*env)->GetStaticMethodID(env, cls, "dismissTrust", "()V");
    if (!mid) return;
    (*env)->CallStaticVoidMethod(env, cls, mid);
}

/* ── Thực hiện full pairing ──────────────────────────────────────────────── */
int pairing_do(lockdown_t *ld, pair_record_t *rec_out,
               void *jni_env_ptr, void *jni_this_unused) {
    JNIEnv *env = (JNIEnv *)jni_env_ptr;
    memset(rec_out, 0, sizeof(*rec_out));

    /* 1. Tạo HostID và SystemBUID */
    char host_id[37], sys_buid[37];
    uuid_generate_random_str(host_id);
    uuid_generate_random_str(sys_buid);
    rec_out->host_id    = strdup(host_id);
    rec_out->system_buid = strdup(sys_buid);

    /* 2. Lấy DevicePublicKey từ lockdownd */
    char *dev_pub_b64 = NULL;
    LOGI("[pairing] GetValue(DevicePublicKey)...");
    if (lockdown_get_value(ld, NULL, "DevicePublicKey", &dev_pub_b64) < 0) {
        LOGE("[pairing] Không lấy được DevicePublicKey");
        return -1;
    }
    /* Decode base64 → DER bytes */
    unsigned char *dev_pub_der = NULL;
    /* Xoá whitespace khỏi base64 */
    size_t b64_len = strlen(dev_pub_b64);
    char *b64_clean = malloc(b64_len + 1);
    size_t j = 0;
    for (size_t i = 0; i < b64_len; i++)
        if (dev_pub_b64[i] != '\n' && dev_pub_b64[i] != '\r')
            b64_clean[j++] = dev_pub_b64[i];
    b64_clean[j] = '\0';
    free(dev_pub_b64);

    size_t der_len = b64_decode(b64_clean, &dev_pub_der);
    free(b64_clean);
    LOGI("[pairing] DevicePublicKey DER: %zu bytes", der_len);

    /* 3. Tạo cert chain qua CertHelper (JNI) */
    LOGI("[pairing] Gọi CertHelper.generateCertChain...");
    if (jni_generate_certs(env, dev_pub_der, (int)der_len, rec_out) < 0) {
        free(dev_pub_der);
        return -1;
    }
    free(dev_pub_der);

    /* 4. Gửi Pair request */
    LOGI("[pairing] Gửi Pair request...");
    char *pair_req = plist_build_pair_request("Pair",
        rec_out->device_cert_pem,
        rec_out->host_cert_pem,
        rec_out->root_cert_pem,
        host_id);
    if (!pair_req) return -1;

    plist_dict_t *pair_resp = NULL;
    if (lockdown_exchange(ld, pair_req, &pair_resp) < 0) {
        free(pair_req);
        LOGE("[pairing] Gửi Pair request thất bại");
        return -1;
    }
    free(pair_req);

    /* 5. Kiểm tra response — xử lý PairingDialogResponsePending */
    const char *err = pair_resp ? plist_get_str(pair_resp, "Error") : "NullResponse";
    if (pair_resp && err && strcmp(err, "PairingDialogResponsePending") == 0) {
        plist_free(pair_resp);
        LOGI("[pairing] PairingDialogResponsePending → thông báo UI, chờ tối đa 120s");
        jni_notify_trust(env);  /* Hiện banner Trust trên UI */

        /* Poll mỗi 3 giây trong 120 giây */
        int waited = 0;
        int success = 0;
        while (waited < 120) {
            sleep(3);
            waited += 3;
            char *retry_req = plist_build_pair_request("Pair",
                rec_out->device_cert_pem,
                rec_out->host_cert_pem,
                rec_out->root_cert_pem,
                host_id);
            plist_dict_t *retry_resp = NULL;
            lockdown_exchange(ld, retry_req, &retry_resp);
            free(retry_req);

            const char *re = retry_resp ? plist_get_str(retry_resp, "Error") : "null";
            if (!re) {
                /* Không có Error = Success */
                LOGI("[pairing] ✅ Người dùng đã bấm Trust! (%ds)", waited);
                plist_free(retry_resp);
                success = 1;
                break;
            }
            if (strcmp(re, "PairingDialogResponsePending") == 0) {
                LOGI("[pairing] Vẫn chờ Trust... (%d/%ds)", waited, 120);
                plist_free(retry_resp);
                continue;
            }
            LOGE("[pairing] Lỗi khi chờ Trust: %s", re);
            plist_free(retry_resp);
            jni_dismiss_trust(env);
            return -1;
        }
        jni_dismiss_trust(env);
        if (!success) {
            LOGE("[pairing] Hết thời gian chờ Trust (120s)");
            return -1;
        }
    } else if (pair_resp && err) {
        LOGE("[pairing] Pair thất bại: %s", err);
        plist_free(pair_resp);
        return -1;
    } else {
        if (pair_resp) plist_free(pair_resp);
        LOGI("[pairing] ✅ Pair thành công ngay (thiết bị đã được tin cậy trước đó)");
    }

    LOGI("[pairing] ✅ Pairing hoàn tất. HostID=%s", host_id);
    return 0;
}

/* ── Lưu/Load pair record ────────────────────────────────────────────────── */
/*
 * BUGFIX: bản trước lưu PEM (chứng chỉ/khoá) trực tiếp dưới dạng
 * "Key=<PEM>\n" — nhưng PEM LUÔN chứa newline bên trong
 * ("-----BEGIN...-----\nMII...\n-----END...-----\n"), nên fprintf "%s\n" ghi
 * PEM tràn qua NHIỀU dòng, và read_field() (dừng ở newline ĐẦU TIÊN) chỉ đọc
 * lại được dòng "-----BEGIN...-----" — dữ liệu bị cắt cụt hoàn toàn. Mọi pair
 * record lưu ra đều hỏng ngay khi load lại (pairing_load "thành công" nhưng
 * các trường cert/key rỗng/cụt), khiến ứng dụng phải pair lại từ đầu mỗi lần.
 *
 * Fix: base64-encode toàn bộ nội dung PEM (giữ nguyên newline bên trong)
 * trước khi ghi thành MỘT dòng — dùng b64_encode/b64_decode đã có sẵn trong
 * plist_util. Round-trip khôi phục lại PEM chính xác từng byte.
 */
int pairing_save(const pair_record_t *rec, const char *path) {
    FILE *f = fopen(path, "w");
    if (!f) return -1;

    fprintf(f, "HostID=%s\n",     rec->host_id     ? rec->host_id     : "");
    fprintf(f, "SystemBUID=%s\n", rec->system_buid ? rec->system_buid : "");

    const char *pem_keys[] = { "RootCert", "RootKey", "HostCert", "HostKey", "DeviceCert" };
    const char *pem_vals[] = { rec->root_cert_pem, rec->root_key_pem,
                               rec->host_cert_pem, rec->host_key_pem,
                               rec->device_cert_pem };
    for (int i = 0; i < 5; i++) {
        const char *v = pem_vals[i] ? pem_vals[i] : "";
        char *b64 = b64_encode((const unsigned char *)v, strlen(v));
        fprintf(f, "%s=%s\n", pem_keys[i], b64 ? b64 : "");
        free(b64);
    }
    fclose(f);
    LOGI("pairing_save: lưu vào %s", path);
    return 0;
}

static char *read_field_raw(const char *data, const char *key) {
    char prefix[64];
    snprintf(prefix, sizeof(prefix), "%s=", key);
    const char *p = strstr(data, prefix);
    if (!p) return NULL;
    p += strlen(prefix);
    const char *end = strchr(p, '\n');
    if (!end) end = p + strlen(p);
    char *val = malloc(end - p + 1);
    memcpy(val, p, end - p);
    val[end - p] = '\0';
    return val;
}

/* Đọc field base64 (PEM đã encode) và decode lại thành PEM gốc */
static char *read_field_pem(const char *data, const char *key) {
    char *b64 = read_field_raw(data, key);
    if (!b64) return NULL;
    if (b64[0] == '\0') { return b64; }   /* rỗng — không cần decode */
    unsigned char *der = NULL;
    size_t len = b64_decode(b64, &der);
    free(b64);
    if (!der) return NULL;
    /* b64_decode() đã null-terminate tại der[len] */
    (void)len;
    return (char *)der;
}

int pairing_load(pair_record_t *rec, const char *path) {
    FILE *f = fopen(path, "r");
    if (!f) return -1;
    fseek(f, 0, SEEK_END);
    long sz = ftell(f);
    fseek(f, 0, SEEK_SET);
    char *data = malloc(sz + 1);
    fread(data, 1, sz, f);
    data[sz] = '\0';
    fclose(f);

    memset(rec, 0, sizeof(*rec));
    rec->host_id         = read_field_raw(data, "HostID");
    rec->system_buid     = read_field_raw(data, "SystemBUID");
    rec->root_cert_pem   = read_field_pem(data, "RootCert");
    rec->root_key_pem    = read_field_pem(data, "RootKey");
    rec->host_cert_pem   = read_field_pem(data, "HostCert");
    rec->host_key_pem    = read_field_pem(data, "HostKey");
    rec->device_cert_pem = read_field_pem(data, "DeviceCert");
    free(data);

    if (!rec->host_id || !rec->host_cert_pem || !rec->host_cert_pem[0]) {
        LOGE("pairing_load: pair record thiếu HostID/HostCert — coi như không hợp lệ");
        return -1;
    }
    LOGI("pairing_load: đọc từ %s OK", path);
    return 0;
}

int pairing_exists(const char *path) {
    FILE *f = fopen(path, "r");
    if (!f) return 0;
    fclose(f);
    return 1;
}

void pairing_free(pair_record_t *rec) {
    if (!rec) return;
    free(rec->host_id);       free(rec->system_buid);
    free(rec->root_cert_pem); free(rec->root_key_pem);
    free(rec->host_cert_pem); free(rec->host_key_pem);
    free(rec->device_cert_pem);
    memset(rec, 0, sizeof(*rec));
}
