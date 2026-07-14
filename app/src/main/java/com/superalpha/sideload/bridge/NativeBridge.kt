package com.superalpha.sideload.bridge

import android.content.Context
import android.util.Log
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.withContext

/**
 * NativeBridge — Kotlin wrapper cho libsideloadnative.so (C JNI layer).
 *
 * Mỗi lần gọi suspend fun chạy trên Dispatchers.IO để không block UI thread.
 * Native C code gọi ngược lại Kotlin qua các @JvmStatic companion methods:
 *   - onNativeLog(line)   → NativeLog.emit(line)
 *   - onTrustRequired()   → hiển thị Trust banner trên UI
 *   - dismissTrust()      → ẩn Trust banner
 */
class NativeBridge(private val context: Context) {

    companion object {
        private const val TAG = "NativeBridge"

        init {
            System.loadLibrary("sideloadnative")
        }

        /* ── Trạng thái trust popup (StateFlow, observe từ UI) ──────────── */
        private val _trustRequired = MutableStateFlow(false)
        val trustRequired: StateFlow<Boolean> = _trustRequired

        /** Được gọi từ C (pairing.c → jni_bridge.c): hiện trust banner */
        @JvmStatic
        fun onTrustRequired() {
            Log.i(TAG, "⚠️ Trust popup yêu cầu — thông báo UI")
            _trustRequired.value = true
            UiPrompt.showTrustBanner(
                "⚠️  Bấm \"Tin cậy\" (Trust This Computer) trên màn hình iPhone!"
            )
        }

        /** Được gọi từ C khi trust đã được xác nhận hoặc hết thời gian */
        @JvmStatic
        fun dismissTrust() {
            Log.i(TAG, "Trust popup đã được xử lý")
            _trustRequired.value = false
            UiPrompt.dismissTrustBanner()
        }

        /** Được gọi từ C (jni_bridge.c) khi có log message */
        @JvmStatic
        fun onNativeLog(line: String) {
            NativeLog.emit(line)
        }
    }

    /** Khởi tạo native layer với filesDir để lưu pair record */
    fun init() {
        nativeInit(context.filesDir.absolutePath)
    }

    /** Gửi SETUP packet + kết nối lockdownd (TCP-over-USB) */
    suspend fun connect(): Boolean = withContext(Dispatchers.IO) {
        try {
            NativeLog.emit("[bridge] Gọi nativeConnect...")
            nativeConnect()
        } catch (e: Exception) {
            Log.e(TAG, "connect() exception: ${e.message}", e)
            NativeLog.emit("[bridge] ❌ connect() exception: ${e.message}")
            false
        }
    }

    /** Thực hiện pairing flow (Pair → Trust → StartSession → TLS) */
    suspend fun pair(): Boolean = withContext(Dispatchers.IO) {
        try {
            NativeLog.emit("[bridge] Gọi nativePair...")
            nativePair()
        } catch (e: Exception) {
            Log.e(TAG, "pair() exception: ${e.message}", e)
            NativeLog.emit("[bridge] ❌ pair() exception: ${e.message}")
            false
        }
    }

    /** Sideload IPA: AFC push + installation_proxy install */
    suspend fun sideload(ipaPath: String): Boolean = withContext(Dispatchers.IO) {
        try {
            NativeLog.emit("[bridge] Gọi nativeSideload: $ipaPath")
            nativeSideload(ipaPath)
        } catch (e: Exception) {
            Log.e(TAG, "sideload() exception: ${e.message}", e)
            NativeLog.emit("[bridge] ❌ sideload() exception: ${e.message}")
            false
        }
    }

    /** Lấy UDID của thiết bị đã kết nối */
    suspend fun getUdid(): String? = withContext(Dispatchers.IO) {
        try {
            nativeGetUdid()
        } catch (e: Exception) {
            Log.e(TAG, "getUdid() exception: ${e.message}", e)
            null
        }
    }

    /** Reset toàn bộ trạng thái native */
    fun reset() {
        try {
            nativeReset()
        } catch (e: Exception) {
            Log.e(TAG, "reset() exception: ${e.message}", e)
        }
    }

    /* ── JNI native declarations (implemented trong jni_bridge.c) ────────── */
    private external fun nativeInit(filesDir: String)
    private external fun nativeConnect(): Boolean
    private external fun nativePair(): Boolean
    private external fun nativeSideload(ipaPath: String): Boolean
    private external fun nativeGetUdid(): String?
    private external fun nativeReset()
}
