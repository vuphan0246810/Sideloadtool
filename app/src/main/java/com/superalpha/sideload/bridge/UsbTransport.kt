package com.superalpha.sideload.bridge

import android.hardware.usb.*
import android.util.Log
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.asStateFlow

/**
 * UsbTransport — Raw USB bulk I/O qua Android USB Host API.
 * THÊM MỚI: @JvmStatic nativeBulkWrite / nativeBulkRead để C JNI gọi được.
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
        var ep_in: UsbEndpoint? = null; var ep_out: UsbEndpoint? = null
        for (ei in 0 until iface.endpointCount) {
            val ep = iface.getEndpoint(ei)
            if (ep.type != UsbConstants.USB_ENDPOINT_XFER_BULK) continue
            if (ep.direction == UsbConstants.USB_DIR_IN  && ep_in  == null) ep_in  = ep
            if (ep.direction == UsbConstants.USB_DIR_OUT && ep_out == null) ep_out = ep
        }
        if (ep_in == null || ep_out == null) { lastError = "Thiếu bulk endpoint"; return false }
        val conn = usbManager.openDevice(device) ?: run {
            lastError = "openDevice() trả null — quyền USB chưa được cấp"; return false
        }
        if (!conn.claimInterface(iface, true)) {
            conn.close(); lastError = "claimInterface() thất bại"; return false
        }
        connection = conn; usbInterface = iface; endpointIn = ep_in; endpointOut = ep_out
        _connected.value = true
        Log.i(TAG, "✅ USB kết nối: ${device.productName}")
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
