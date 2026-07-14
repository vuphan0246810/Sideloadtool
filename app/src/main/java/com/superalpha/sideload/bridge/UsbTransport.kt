package com.superalpha.sideload.bridge

import android.hardware.usb.*
import android.util.Log
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.asStateFlow

/**
 * UsbTransport — Raw USB bulk I/O qua Android USB Host API.
 *
 * FIX: Thêm delay 150ms + retry 5 lần cho claimInterface().
 *
 * Root cause lỗi "claimInterface() thất bại" (lặp vô tận):
 *   1. Android cần một khoảng thời gian sau openDevice() để giải phóng
 *      kernel driver và sẵn sàng cho claimInterface(forceClaim=true).
 *   2. Khi claimInterface fail và ta gọi conn.close(), Android đôi khi
 *      re-enumerate device → gửi lại USB_DEVICE_ATTACHED → vòng lặp.
 *   Fix: sleep 150ms trước lần thử đầu + retry tối đa 5 lần với delay tăng
 *   dần (200ms, 400ms, 600ms, 800ms) trước khi bỏ cuộc.
 *
 * @JvmStatic nativeBulkWrite / nativeBulkRead để C JNI gọi được.
 */
object UsbTransport {
    private const val TAG = "UsbTransport"
    const val VENDOR_ID_APPLE = 0x05AC
    private const val INTERFACE_CLASS    = 0xFF
    private const val INTERFACE_SUBCLASS = 0xFE
    private const val INTERFACE_PROTOCOL = 0x02
    const val USB_MRU = 16384

    @Volatile private var connection: UsbDeviceConnection? = null
    @Volatile private var usbInterface: UsbInterface? = null
    @Volatile private var endpointIn:   UsbEndpoint? = null
    @Volatile private var endpointOut:  UsbEndpoint? = null

    private val _connected = MutableStateFlow(false)
    val connected = _connected.asStateFlow()

    @Volatile private var lastError: String? = null
    @JvmStatic fun lastError(): String? = lastError

    fun isConnected() = _connected.value

    fun findAppleDevice(usbManager: UsbManager): UsbDevice? =
        usbManager.deviceList.values.firstOrNull { it.vendorId == VENDOR_ID_APPLE }

    private data class FoundInterface(val config: UsbConfiguration, val iface: UsbInterface)

    private fun findUsbmuxInterfaceWithConfig(device: UsbDevice): FoundInterface? {
        for (ci in 0 until device.configurationCount) {
            val config = device.getConfiguration(ci)
            for (ii in 0 until config.interfaceCount) {
                val iface = config.getInterface(ii)
                if (iface.interfaceClass    == INTERFACE_CLASS &&
                    iface.interfaceSubclass == INTERFACE_SUBCLASS &&
                    iface.interfaceProtocol == INTERFACE_PROTOCOL) return FoundInterface(config, iface)
            }
        }
        return null
    }

    fun findUsbmuxInterface(device: UsbDevice): UsbInterface? =
        findUsbmuxInterfaceWithConfig(device)?.iface

    fun open(device: UsbDevice, usbManager: UsbManager): Boolean {
        close()
        lastError = null

        val found = findUsbmuxInterfaceWithConfig(device) ?: run {
            lastError = "Không tìm thấy usbmux interface (class=0xFF sub=0xFE proto=0x02)"
            Log.e(TAG, lastError!!); return false
        }
        val iface = found.iface

        var ep_in: UsbEndpoint? = null
        var ep_out: UsbEndpoint? = null
        for (ei in 0 until iface.endpointCount) {
            val ep = iface.getEndpoint(ei)
            if (ep.type != UsbConstants.USB_ENDPOINT_XFER_BULK) continue
            if (ep.direction == UsbConstants.USB_DIR_IN  && ep_in  == null) ep_in  = ep
            if (ep.direction == UsbConstants.USB_DIR_OUT && ep_out == null) ep_out = ep
        }
        if (ep_in == null || ep_out == null) {
            lastError = "Thiếu bulk endpoint"; return false
        }

        val conn = usbManager.openDevice(device) ?: run {
            lastError = "openDevice() trả null — quyền USB chưa được cấp"; return false
        }

        // ── FIX ROOT CAUSE claimInterface ─────────────────────────────────────
        // Root cause thực sự của lỗi claimInterface() luôn trả false:
        //
        //   Android gọi openDevice() → trả UsbDeviceConnection hợp lệ, NHƯNG
        //   thiết bị iOS lúc này có thể đang ở USB configuration 0 (unconfigured)
        //   hoặc ở một configuration khác không chứa usbmux interface (class=0xFF).
        //
        //   findUsbmuxInterfaceWithConfig() tìm interface trong DESCRIPTOR (chỉ
        //   là metadata), không phải trong hardware state hiện tại. Nếu hardware
        //   chưa được set về đúng configuration, claimInterface() luôn fail dù
        //   forceClaim=true — vì interface đó chưa "tồn tại" ở hardware level.
        //
        // Fix: Gọi setConfiguration(found.config) TRƯỚC claimInterface().
        //   setConfiguration() gửi USB SET_CONFIGURATION request xuống device,
        //   chuyển hardware sang configuration chứa usbmux interface. Sau đó
        //   claimInterface() mới có thể claim thành công.
        //
        // Tham chiếu: libimobiledevice-android, pymobiledevice3 USB backend,
        //   Android USB Host API Guide §"Communicating with a device".
        // ─────────────────────────────────────────────────────────────────────
        try {
            val ok = conn.setConfiguration(found.config)
            Log.i(TAG, "setConfiguration(${found.config.id}) → $ok")
        } catch (e: Exception) {
            // Có thể fail nếu device đã ở đúng config — tiếp tục, không return
            Log.w(TAG, "setConfiguration() exception (ignored): ${e.message}")
        }

        // Cho USB subsystem và iOS device thời gian xử lý SET_CONFIGURATION
        try { Thread.sleep(500) } catch (_: InterruptedException) {}

        // Retry tối đa 8 lần, delay tăng theo cap 2000ms
        var claimed = false
        for (attempt in 1..8) {
            if (conn.claimInterface(iface, true)) {
                claimed = true
                Log.i(TAG, "✅ claimInterface() thành công ở lần $attempt")
                break
            }
            val delay = minOf(attempt * 300L, 2000L)
            Log.w(TAG, "claimInterface() thất bại lần $attempt/8, thử lại sau ${delay}ms…")
            try { Thread.sleep(delay) } catch (_: InterruptedException) {}
        }

        if (!claimed) {
            conn.close()
            lastError = "claimInterface() thất bại sau 8 lần thử — thử rút/cắm lại cáp USB"
            Log.e(TAG, lastError!!)
            return false
        }

        // setInterface() sau claim — đảm bảo alternate setting = 0 được kích hoạt
        try { conn.setInterface(iface) } catch (_: Exception) {}

        connection = conn
        usbInterface = iface
        endpointIn = ep_in
        endpointOut = ep_out
        _connected.value = true
        Log.i(TAG, "✅ USB kết nối thành công: ${device.productName}")
        return true
    }

    fun close() {
        try { connection?.releaseInterface(usbInterface) } catch (_: Exception) {}
        try { connection?.close() } catch (_: Exception) {}
        connection = null; usbInterface = null; endpointIn = null; endpointOut = null
        _connected.value = false
    }

    fun bulkRead(size: Int, timeout: Long = 5000L): ByteArray? {
        val ep = endpointIn ?: return null; val conn = connection ?: return null
        val buf = ByteArray(size); val n = conn.bulkTransfer(ep, buf, size, timeout.toInt())
        return if (n >= 0) buf.copyOf(n) else null
    }

    fun bulkWrite(data: ByteArray, timeout: Long = 5000L): Int {
        val ep = endpointOut ?: return -1; val conn = connection ?: return -1
        return conn.bulkTransfer(ep, data, data.size, timeout.toInt())
    }

    /** Được gọi từ C JNI: GetStaticMethodID(cls, "nativeBulkWrite", "([BI)I") */
    @JvmStatic
    fun nativeBulkWrite(data: ByteArray, timeoutMs: Int): Int {
        val ep = endpointOut ?: return -1; val conn = connection ?: return -1
        return conn.bulkTransfer(ep, data, data.size, timeoutMs)
    }

    /** Được gọi từ C JNI: GetStaticMethodID(cls, "nativeBulkRead", "([BI)I") */
    @JvmStatic
    fun nativeBulkRead(buf: ByteArray, timeoutMs: Int): Int {
        val ep = endpointIn ?: return -1; val conn = connection ?: return -1
        return conn.bulkTransfer(ep, buf, buf.size, timeoutMs)
    }
}
