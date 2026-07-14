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
 * FIX: Thêm kiểm tra UsbTransport.isConnected() trước khi gọi native C.
 *
 * Root cause Python "không thấy" USB:
 *   nativeConnect() ở C gọi usb_bulk_write/read → UsbTransport.nativeBulkWrite/Read.
 *   Nếu UsbTransport chưa kết nối (connection == null), bulk I/O trả -1 ngay,
 *   C code không có thông báo rõ ràng — Python nhận LockdownError mơ hồ.
 *   Fix: kiểm tra UsbTransport.isConnected() TRƯỚC khi gọi native; nếu chưa kết nối
 *   emit thông báo rõ ràng bằng tiếng Việt và trả false ngay.
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

        // FIX: Kiểm tra USB đã được kết nối qua UsbTransport chưa.
        // Nếu chưa, native C sẽ thất bại ngay vì usb_bulk_write/read trả -1.
        // Phát thông báo rõ ràng để log hiện hướng dẫn cụ thể cho người dùng.
        if (!UsbTransport.isConnected()) {
            NativeLog.emit(
                "[DeviceNative] ❌ USB chưa kết nối — vui lòng cắm cáp và bấm \"Kết nối\" trước."
            )
            return@runBlocking false
        }

        val connected = b.connect()
        if (!connected) {
            NativeLog.emit("[DeviceNative] ❌ connect() thất bại — kiểm tra cáp USB và thiết bị đã mở khoá.")
            return@runBlocking false
        }

        val paired = b.pair()
        if (!paired) {
            NativeLog.emit("[DeviceNative] ❌ pair() thất bại — nếu iPhone hỏi \"Tin cậy?\" hãy bấm Tin cậy.")
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
        if (!UsbTransport.isConnected()) {
            NativeLog.emit("[DeviceNative] ❌ USB đã ngắt trong quá trình cài đặt — thử lại từ đầu.")
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
