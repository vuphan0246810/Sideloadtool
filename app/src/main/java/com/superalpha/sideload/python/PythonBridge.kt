package com.superalpha.sideload.python

import com.chaquo.python.Python
import com.superalpha.sideload.bridge.AppConfig
import com.superalpha.sideload.bridge.NativeLog
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import okhttp3.OkHttpClient
import okhttp3.Request
import org.json.JSONObject

/**
 * PythonBridge — cầu nối Kotlin ↔ Python (Chaquopy).
 *
 * v8: Khôi phục Chaquopy cho các hàm Apple API:
 *   - revokeCerts() → sideload_core.do_revoke_certs()
 *                     (Python HTTP thuần, KHÔNG cần USB — hàm này hoàn toàn
 *                     giao tiếp với Apple qua HTTPS, không đụng đến thiết bị iOS)
 *   - sideload()    → sideload_core.do_sideload()
 *                     (Python xử lý auth + signing; USB đi qua DeviceNative → native C)
 *   - listAnisetteServers() → vẫn dùng Kotlin OkHttp (đã port từ v7)
 *
 * Chỉ mux_usb.py và device_link.py được port sang native C.
 * apple_auth.py, developer_api.py, sideload_core.py, config_manager.py, utils.py
 * vẫn chạy hoàn toàn bằng Python qua Chaquopy.
 */
object PythonBridge {
    data class AnisetteServer(val name: String, val address: String)
    data class Outcome(val success: Boolean, val message: String)

    private val httpClient by lazy { OkHttpClient() }

    // ── Config helpers (đọc/ghi từ AppConfig, không cần Python) ─────────────
    fun getSavedAppleId(): String = AppConfig.appleId
    fun saveAppleId(v: String) { AppConfig.appleId = v }
    fun getSavedAnisetteUrl(): String = AppConfig.anisetteUrl
    fun saveAnisetteUrl(v: String) { AppConfig.anisetteUrl = v }

    // ── Python module helper ─────────────────────────────────────────────────
    private fun pythonModule(name: String) = Python.getInstance().getModule(name)

    /**
     * Thu hồi chứng chỉ Development bằng Apple ID.
     *
     * Gọi sideload_core.do_revoke_certs(apple_id, password, anisette_url, cert_selector).
     * Hàm Python này chỉ dùng HTTP (apple_auth + developer_api) — không cần USB/device.
     *
     * Chữ ký Python:
     *   do_revoke_certs(apple_id: str, password: str,
     *                   anisette_url: str = "", cert_selector: str = "") -> bool
     */
    suspend fun revokeCerts(
        appleId: String,
        password: String,
        anisetteUrl: String?,
        certSelector: String
    ): Outcome = withContext(Dispatchers.IO) {
        try {
            NativeLog.emit("[python] Đang đăng nhập & tra cứu chứng chỉ...")
            val core = pythonModule("sideload_core")
            // Lưu apple_id để tự điền lần sau
            AppConfig.appleId = appleId
            val effectiveAnisetteUrl = anisetteUrl?.takeIf { it.isNotBlank() } ?: ""
            // Đúng thứ tự tham số Python: apple_id, password, anisette_url, cert_selector
            val ok = core.callAttr(
                "do_revoke_certs",
                appleId,
                password,
                effectiveAnisetteUrl,
                certSelector
            ).toBoolean()
            Outcome(ok, if (ok) "Thu hồi chứng chỉ thành công." else "Thu hồi thất bại — xem nhật ký.")
        } catch (e: Exception) {
            val msg = e.message ?: e.toString()
            NativeLog.emit("[python] ❌ revokeCerts lỗi: $msg")
            Outcome(false, msg)
        }
    }

    /**
     * Ký và cài đặt IPA bằng Apple ID.
     *
     * Gọi sideload_core.do_sideload() trong Python:
     *   - Python xử lý: auth Apple ID, tạo/tái sử dụng cert & App ID, ký IPA (zsign)
     *   - Native C xử lý: USB connect, lockdown pair, AFC push, install_proxy
     *     (thông qua DeviceNative.kt được gọi từ device_link.py wrapper)
     *
     * Chữ ký Python:
     *   do_sideload(ipa_path, apple_id, password, udid_override="", anisette_url="") -> bool
     */
    suspend fun sideload(
        ipaPath: String,
        appleId: String,
        password: String,
        twoFaCode: String?,
        anisetteUrl: String?
    ): Outcome = withContext(Dispatchers.IO) {
        try {
            NativeLog.emit("[python] Bắt đầu quá trình ký và cài đặt IPA...")
            val core = pythonModule("sideload_core")
            AppConfig.appleId = appleId
            val effectiveAnisetteUrl = anisetteUrl?.takeIf { it.isNotBlank() } ?: ""
            val ok = core.callAttr(
                "do_sideload",
                ipaPath,
                appleId,
                password,
                AppConfig.lastUdid,
                effectiveAnisetteUrl
            ).toBoolean()
            Outcome(ok, if (ok) "Cài đặt IPA thành công." else "Cài đặt thất bại — xem nhật ký.")
        } catch (e: Exception) {
            val msg = e.message ?: e.toString()
            NativeLog.emit("[python] ❌ sideload lỗi: $msg")
            Outcome(false, msg)
        }
    }

    /**
     * Danh sách server Anisette công khai — dùng Kotlin OkHttp (không cần Python).
     */
    suspend fun listAnisetteServers(): List<AnisetteServer> = withContext(Dispatchers.IO) {
        try {
            val request = Request.Builder()
                .url("https://servers.sidestore.io/servers.json")
                .build()
            httpClient.newCall(request).execute().use { response ->
                val body = response.body?.string()
                if (!response.isSuccessful || body.isNullOrBlank()) return@withContext defaultServers()
                val arr = JSONObject(body).optJSONArray("servers") ?: return@withContext defaultServers()
                val parsed = (0 until arr.length()).mapNotNull { i ->
                    val obj = arr.optJSONObject(i) ?: return@mapNotNull null
                    val address = obj.optString("address", "")
                    if (address.isBlank()) null else AnisetteServer(obj.optString("name", "?"), address)
                }
                parsed.ifEmpty { defaultServers() }
            }
        } catch (_: Exception) {
            defaultServers()
        }
    }

    private fun defaultServers(): List<AnisetteServer> =
        AppConfig.defaultAnisetteServers.map { AnisetteServer(it.name, it.url) }
}
