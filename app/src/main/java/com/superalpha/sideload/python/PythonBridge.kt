package com.superalpha.sideload.python

import com.superalpha.sideload.bridge.AppConfig

/**
 * PythonBridge — STUB không còn Python. Giữ lại để SettingsScreen/RevokeCertsScreen compile.
 * Tất cả logic thực đã chuyển sang AppConfig.kt + NativeBridge.kt (C JNI).
 */
object PythonBridge {
    data class AnisetteServer(val name: String, val url: String)

    fun getSavedAppleId()  = AppConfig.appleId
    fun saveAppleId(v: String) { AppConfig.appleId = v }
    fun getSavedAnisetteUrl() = AppConfig.anisetteUrl
    fun saveAnisetteUrl(v: String) { AppConfig.anisetteUrl = v }
    fun listAnisetteServers() = AppConfig.defaultAnisetteServers
        .map { AnisetteServer(it.name, it.url) }

    // Stubs — SettingsScreen/RevokeCerts vẫn dùng tên này
    fun doSideload(ipaPath: String, appleId: String, password: String,
                   anisetteUrl: String, udid: String, bundleId: String,
                   appName: String, twoFaCode: String?): Boolean = false

    fun revokeCertificates(appleId: String, password: String,
                           anisetteUrl: String, twoFaCode: String?): String =
        "Tính năng này cần được port sang Kotlin HTTP client."
}
