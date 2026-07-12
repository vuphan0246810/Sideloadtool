package com.superalpha.sideload.bridge

import android.hardware.usb.UsbConstants
import android.hardware.usb.UsbDevice
import android.hardware.usb.UsbDeviceConnection
import android.hardware.usb.UsbEndpoint
import android.hardware.usb.UsbInterface
import android.hardware.usb.UsbManager
import android.util.Log
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.asStateFlow

/**
 * Raw USB I/O for the Apple "usbmux" function, using Android's built-in USB Host API
 * (android.hardware.usb) — no NDK, no libusb, no root required.
 *
 * Every real iPhone/iPad exposes a USB interface with class=0xFF (vendor-specific),
 * subclass=0xFE, protocol=0x02 for the usbmux control channel. These exact numbers
 * (INTERFACE_CLASS/SUBCLASS/PROTOCOL) are taken directly from the libimobiledevice
 * project's usbmuxd/src/usb.h, which is the authoritative reference implementation.
 *
 * This object exposes only a "dumb pipe": bulk IN/OUT transfers on that interface.
 * The actual usbmux wire protocol (framing, TCP-like multiplexed connections) is
 * implemented in Python (see app/src/main/python/mux_usb.py), which calls back into
 * [bulkRead]/[bulkWrite] via Chaquopy's Java-interop. Keeping the protocol logic in
 * Python — rather than in this Kotlin layer or in native C — means it can be
 * inspected/patched without touching the compiled app shell, which matters a lot for
 * a transport this experimental (see README "Rủi ro đã biết").
 */
object UsbTransport {
    private const val TAG = "UsbTransport"

    const val VENDOR_ID_APPLE = 0x05AC

    // From usbmuxd/src/usb.h (libimobiledevice project).
    private const val INTERFACE_CLASS = 0xFF
    private const val INTERFACE_SUBCLASS = 0xFE
    private const val INTERFACE_PROTOCOL = 0x02

    /** USB_MRU from usbmuxd/src/usb.h — the maximum single read chunk usbmuxd uses. */
    const val USB_MRU = 16384

    private const val MAX_OPEN_ATTEMPTS = 3
    private const val OPEN_RETRY_DELAY_MS = 200L

    @Volatile private var connection: UsbDeviceConnection? = null
    @Volatile private var usbInterface: UsbInterface? = null
    @Volatile private var endpointIn: UsbEndpoint? = null
    @Volatile private var endpointOut: UsbEndpoint? = null

    private val _connected = MutableStateFlow(false)
    val connected = _connected.asStateFlow()

    /** Lý do thất bại cụ thể của lần gọi [open] gần nhất (một trong 4 nhánh lỗi
     * bên dưới), hoặc null nếu lần gần nhất thành công/chưa gọi. Trước đây
     * UsbPermissionManager chỉ báo một câu chung "Mở kết nối USB thất bại." cho
     * mọi trường hợp — không phân biệt được "không tìm thấy interface" với
     * "openDevice() trả null" (thường là do hệ thống chưa kịp cấp quyền xong)
     * với "claimInterface() thất bại" (thường là do một tiến trình/app khác
     * đang giữ interface, hoặc thiết bị cần mở lại sau khi rút/cắm). Biết chính
     * xác nhánh nào giúp người dùng (và người debug) biết nên thử lại, rút cắm
     * lại cáp, hay đó là lỗi thật cần sửa code. */
    @Volatile private var lastError: String? = null

    @JvmStatic
    fun lastError(): String? = lastError

    /** Finds the first attached Apple device, or null if none is plugged in. */
    fun findAppleDevice(usbManager: UsbManager): UsbDevice? =
        usbManager.deviceList.values.firstOrNull { it.vendorId == VENDOR_ID_APPLE }

    fun findUsbmuxInterface(device: UsbDevice): UsbInterface? {
        for (i in 0 until device.interfaceCount) {
            val iface = device.getInterface(i)
            if (iface.interfaceClass == INTERFACE_CLASS &&
                iface.interfaceSubclass == INTERFACE_SUBCLASS &&
                iface.interfaceProtocol == INTERFACE_PROTOCOL
            ) {
                return iface
            }
        }
        return null
    }

    /**
     * Claims the usbmux interface and finds its bulk IN/OUT endpoints. Must be called
     * after permission has already been granted (see UsbPermissionManager). Returns
     * true on success. On failure, [lastError] holds the specific reason.
     *
     * Retries a few times with a short delay: right after a permission grant, or
     * right after the device re-enumerates, `openDevice`/`claimInterface` can fail
     * transiently for a brief window before the USB host stack settles — a single
     * immediate attempt does not give it that chance. Must be called off the main
     * thread (it may sleep briefly between attempts); see UsbPermissionManager.
     */
    @Synchronized
    fun open(usbManager: UsbManager, device: UsbDevice): Boolean {
        var attemptError: String? = null
        for (attempt in 1..MAX_OPEN_ATTEMPTS) {
            val error = openOnce(usbManager, device)
            if (error == null) return true
            attemptError = error
            Log.w(TAG, "open() lần $attempt/$MAX_OPEN_ATTEMPTS thất bại: $error")
            if (attempt < MAX_OPEN_ATTEMPTS) {
                try {
                    Thread.sleep(OPEN_RETRY_DELAY_MS)
                } catch (_: InterruptedException) {
                }
            }
        }
        lastError = attemptError
        return false
    }

    /** Một lần thử mở kết nối. Trả về null nếu thành công, hoặc mô tả lỗi cụ thể. */
    private fun openOnce(usbManager: UsbManager, device: UsbDevice): String? {
        close()

        val iface = findUsbmuxInterface(device)
        if (iface == null) {
            return "Không tìm thấy interface usbmux (class=0xFF sub=0xFE proto=0x02) trên thiết bị."
        }

        var inEp: UsbEndpoint? = null
        var outEp: UsbEndpoint? = null
        for (i in 0 until iface.endpointCount) {
            val ep = iface.getEndpoint(i)
            if (ep.type != UsbConstants.USB_ENDPOINT_XFER_BULK) continue
            if (ep.direction == UsbConstants.USB_DIR_IN) inEp = ep
            if (ep.direction == UsbConstants.USB_DIR_OUT) outEp = ep
        }
        if (inEp == null || outEp == null) {
            return "Interface usbmux thiếu bulk endpoint IN/OUT."
        }

        val conn = usbManager.openDevice(device)
        if (conn == null) {
            return "usbManager.openDevice() trả về null (thường do quyền USB chưa ổn định " +
                "hoặc thiết bị vừa được cắm lại — sẽ tự thử lại)."
        }

        // Chọn cấu hình USB trước khi claim interface. Hầu hết thiết bị Android tự
        // chọn cấu hình mặc định khi mở, nhưng một số driver USB host lại yêu cầu
        // set cấu hình rõ ràng trước khi claimInterface() thành công — bước này
        // trước đây hoàn toàn không có, nên bị bỏ qua âm thầm. setConfiguration()
        // có thể trả false vô hại nếu thiết bị chỉ có 1 cấu hình (đã là mặc định);
        // không coi đó là lỗi nghiêm trọng, chỉ log cảnh báo.
        if (device.configurationCount > 0) {
            try {
                val configured = conn.setConfiguration(device.getConfiguration(0))
                if (!configured) {
                    Log.w(TAG, "setConfiguration() trả false (có thể thiết bị chỉ có 1 cấu hình sẵn có — bỏ qua).")
                }
            } catch (e: Exception) {
                Log.w(TAG, "setConfiguration() lỗi (bỏ qua, thử claimInterface trực tiếp): $e")
            }
        }

        if (!conn.claimInterface(iface, true)) {
            conn.close()
            return "Không claim được interface usbmux (thiết bị có thể đang bị một app/dịch vụ " +
                "khác giữ, hoặc cần rút cắm lại cáp USB)."
        }

        connection = conn
        usbInterface = iface
        endpointIn = inEp
        endpointOut = outEp
        _connected.value = true
        lastError = null
        Log.i(TAG, "Đã mở kết nối USB tới ${device.deviceName} (maxPacketSize in=${inEp.maxPacketSize} out=${outEp.maxPacketSize}).")
        return null
    }

    @Synchronized
    fun close() {
        try {
            usbInterface?.let { connection?.releaseInterface(it) }
        } catch (_: Exception) {
        }
        connection?.close()
        connection = null
        usbInterface = null
        endpointIn = null
        endpointOut = null
        _connected.value = false
    }

    @JvmStatic
    fun isConnected(): Boolean = _connected.value

    @JvmStatic
    fun maxPacketSizeOut(): Int = endpointOut?.maxPacketSize ?: 0

    /**
     * Blocking bulk read, called from Python's mux_usb.py pump thread. Returns null on
     * timeout/error (caller treats null as "no data right now, keep polling"), or an
     * empty/short ByteArray for whatever was actually read.
     */
    @JvmStatic
    fun bulkRead(timeoutMs: Int): ByteArray? {
        val conn = connection ?: return null
        val ep = endpointIn ?: return null
        val buffer = ByteArray(USB_MRU)
        val n = try {
            conn.bulkTransfer(ep, buffer, buffer.size, timeoutMs)
        } catch (e: Exception) {
            Log.e(TAG, "bulkRead lỗi: $e")
            -1
        }
        if (n <= 0) return null
        return buffer.copyOf(n)
    }

    /**
     * Blocking bulk write. Sends [data] as-is via a single bulkTransfer call (Android's
     * USB host stack fragments it into wire-level max-packet-size chunks itself), then
     * — mirroring desktop libusb-based usbmuxd — follows up with a zero-length packet
     * if the payload was an exact multiple of the endpoint's max packet size, so the
     * device's USB stack doesn't keep waiting for "more of this transfer".
     *
     * Returns the number of bytes written, or -1 on error.
     */
    @JvmStatic
    fun bulkWrite(data: ByteArray): Int {
        val conn = connection ?: return -1
        val ep = endpointOut ?: return -1
        val n = try {
            conn.bulkTransfer(ep, data, data.size, 10_000)
        } catch (e: Exception) {
            Log.e(TAG, "bulkWrite lỗi: $e")
            -1
        }
        if (n == data.size && ep.maxPacketSize > 0 && data.size % ep.maxPacketSize == 0 && data.isNotEmpty()) {
            try {
                conn.bulkTransfer(ep, ByteArray(0), 0, 1000)
            } catch (_: Exception) {
            }
        }
        return n
    }
}
