package com.superalpha.sideload.python

import com.superalpha.sideload.bridge.AppConfig
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import okhttp3.OkHttpClient
import okhttp3.Request
import org.json.JSONObject

/**
 * PythonBridge — không còn Python (Chaquopy đã bị xoá khỏi app/build.gradle.kts,
 * xem NativeBridge.kt cho luồng USB/JNI mới). Giữ lại tên object này vì
 * SettingsScreen/RevokeCertsScreen/SideloadScreen vẫn gọi qua đây.
 *
 * - sideload()/revokeCerts(): quy trình ký + cài đặt / thu hồi chứng chỉ bằng
 *   Apple ID thật (giống main.py option 1/3 của bản CLI gốc) CHƯA được port
 *   sang Kotlin HTTP client — vẫn là stub trả về thất bại có thông báo rõ ràng.
 * - listAnisetteServers(): ĐÃ port thật sang Kotlin — gọi thẳng OkHttp tới
 *   servers.sidestore.io thay vì qua Python, có fallback về danh sách mặc định
 *   trong AppConfig nếu mất mạng.
 */
object PythonBridge {
    data class AnisetteServer(val name: String, val address: String)
    data class Outcome(val success: Boolean, val message: String)

    private const val NOT_PORTED_MESSAGE =
        "Tính năng này cần được port sang Kotlin HTTP client."
    private val httpClient by lazy { OkHttpClient() }

    fun getSavedAppleId(): String = AppConfig.appleId
    fun saveAppleId(v: String) { AppConfig.appleId = v }
    fun getSavedAnisetteUrl(): String = AppConfig.anisetteUrl
    fun saveAnisetteUrl(v: String) { AppConfig.anisetteUrl = v }

    /** Ký + cài đặt IPA bằng Apple ID thật. Stub — luôn thất bại với thông báo
     * rõ ràng cho tới khi được port sang Kotlin HTTP client. Cài đặt trực tiếp
     * IPA đã ký sẵn qua USB (không cần Apple ID) đã hoạt động thật rồi, xem
     * NativeBridge.sideload(). */
    suspend fun sideload(
        ipaPath: String,
        appleId: String,
        password: String,
        twoFaCode: String?,
        anisetteUrl: String?
    ): Outcome = withContext(Dispatchers.IO) {
        val ok = doSideload(ipaPath, appleId, password, anisetteUrl ?: "", "", "", "", twoFaCode)
        Outcome(ok, if (ok) "" else NOT_PORTED_MESSAGE)
    }

    /** Thu hồi chứng chỉ Development bằng Apple ID thật. Stub — luôn thất bại
     * với thông báo rõ ràng cho tới khi được port sang Kotlin HTTP client. */
    suspend fun revokeCerts(
        appleId: String,
        password: String,
        anisetteUrl: String?,
        certSelector: String
    ): Outcome = withContext(Dispatchers.IO) {
        Outcome(false, revokeCertificates(appleId, password, anisetteUrl ?: "", certSelector))
    }

    private fun doSideload(
        ipaPath: String, appleId: String, password: String,
        anisetteUrl: String, udid: String, bundleId: String,
        appName: String, twoFaCode: String?
    ): Boolean = false

    private fun revokeCertificates(
        appleId: String, password: String,
        anisetteUrl: String, twoFaCode: String?
    ): String = NOT_PORTED_MESSAGE

    /** Danh sách server Anisette công khai, lấy trực tiếp từ servers.sidestore.io
     * (JSON dạng {"servers":[{"name":...,"address":...}]}). Trả về danh sách mặc
     * định trong AppConfig nếu mất mạng hoặc parse lỗi — người dùng vẫn có thể
     * dùng "Tự động" hoặc nhập URL tay. */
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
