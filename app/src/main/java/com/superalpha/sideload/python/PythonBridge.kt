package com.superalpha.sideload.python

import com.chaquo.python.Python
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext

/**
 * Thin Kotlin-side wrapper around the `sideload_core` Python module. Every call here
 * runs Python on [Dispatchers.IO] (Chaquopy's Python calls are blocking); progress and
 * log lines arrive separately and asynchronously via [com.superalpha.sideload.bridge.NativeLog].
 *
 * Each function returns a simple success/message pair rather than throwing, because
 * the Python side already catches and narrates its own errors in Vietnamese through
 * NativeLog — by the time an exception reaches here it is almost always something
 * unexpected (e.g. a Chaquopy/py4j-level failure), which we still want to surface
 * rather than silently swallow.
 */
object PythonBridge {
    private fun core() = Python.getInstance().getModule("sideload_core")

    data class Outcome(val success: Boolean, val message: String)

    suspend fun sideload(ipaPath: String, appleId: String, password: String, udidOverride: String?, anisetteUrl: String?): Outcome =
        withContext(Dispatchers.IO) {
            try {
                val result = core().callAttr(
                    "do_sideload", ipaPath, appleId, password, udidOverride ?: "", anisetteUrl ?: ""
                )
                Outcome(result.toBoolean(), "")
            } catch (e: Exception) {
                Outcome(false, e.message ?: e.toString())
            }
        }

    suspend fun revokeCerts(appleId: String, password: String, anisetteUrl: String?, certIndexOrAll: String): Outcome =
        withContext(Dispatchers.IO) {
            try {
                val result = core().callAttr("do_revoke_certs", appleId, password, anisetteUrl ?: "", certIndexOrAll)
                Outcome(result.toBoolean(), "")
            } catch (e: Exception) {
                Outcome(false, e.message ?: e.toString())
            }
        }

    suspend fun connectedUdid(): String? = withContext(Dispatchers.IO) {
        try {
            val result = core().callAttr("get_connected_udid")
            if (result == null) null else result.toString()
        } catch (e: Exception) {
            null
        }
    }
}
