package com.superalpha.sideload.bridge

import android.content.Context
import kotlinx.coroutines.runBlocking

/**
 * DeviceNative — Synchronous (blocking) wrapper around NativeBridge for Chaquopy
 * Python code to call via Java interop.
 *
 * Chaquopy Python không thể gọi Kotlin suspend functions trực tiếp. Object này
 * cung cấp các phương thức JVM thông thường (blocking) để:
 *   - device_link.py gọi các thao tác USB/lockdown (đã port sang native C)
 *   - Python code không cần import bất cứ thứ gì từ mux_usb.py / device_link.py cũ
 *
 * Chỉ USB/lockdown là native — apple_auth.py, developer_api.py, sideload_core.py
 * vẫn chạy bình thường trong Python qua Chaquopy.
 */
object DeviceNative {
    @Volatile private var bridge: NativeBridge? = null

    /** Gọi từ SuperAlphaApp.onCreate() */
    fun init(context: Context) {
        bridge = NativeBridge(context)
        bridge!!.init()
    }

    /**
     * Kết nối USB mux + thực hiện lockdown pairing.
     * Gọi từ device_link.py: DeviceNative.connectAndPair()
     * Trả về True nếu thành công.
     */
    @JvmStatic
    fun connectAndPair(): Boolean = runBlocking {
        val b = bridge ?: run {
            NativeLog.emit("[DeviceNative] ❌ Chưa init — gọi DeviceNative.init() trước.")
            return@runBlocking false
        }
        val connected = b.connect()
        if (!connected) {
            NativeLog.emit("[DeviceNative] ❌ connect() thất bại.")
            return@runBlocking false
        }
        val paired = b.pair()
        if (!paired) {
            NativeLog.emit("[DeviceNative] ❌ pair() thất bại.")
        }
        paired
    }

    /**
     * Lấy UDID của thiết bị đã kết nối.
     * Gọi từ device_link.py: DeviceNative.getUdid()
     */
    @JvmStatic
    fun getUdid(): String? = runBlocking {
        bridge?.getUdid() ?: AppConfig.lastUdid.ifBlank { null }
    }

    /**
     * Đẩy IPA lên device qua AFC rồi cài đặt qua install_proxy.
     * Gọi từ device_link.py: DeviceNative.sideloadIpa(localIpaPath)
     * Trả về True nếu thành công.
     */
    @JvmStatic
    fun sideloadIpa(localIpaPath: String): Boolean = runBlocking {
        val b = bridge ?: run {
            NativeLog.emit("[DeviceNative] ❌ Chưa init.")
            return@runBlocking false
        }
        b.sideload(localIpaPath)
    }

    /**
     * Reset toàn bộ trạng thái native (MuxDevice, lockdown session).
     * Gọi từ device_link.py: DeviceNative.reset()
     */
    @JvmStatic
    fun reset() {
        bridge?.reset()
    }
}
