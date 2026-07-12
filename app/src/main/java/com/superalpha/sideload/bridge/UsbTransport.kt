package com.superalpha.sideload.bridge

import android.hardware.usb.UsbConfiguration
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

    /** Kết quả tìm interface usbmux: interface đó thuộc CẤU HÌNH (configuration)
     * USB cụ thể nào — xem giải thích chi tiết ở [findUsbmuxInterfaceWithConfig]. */
    private data class FoundInterface(val config: UsbConfiguration, val iface: UsbInterface)

    /** Giữ lại cho khả năng tương thích ngược / log; KHÔNG dùng kết quả này để
     * claim trực tiếp nữa — dùng [findUsbmuxInterfaceWithConfig] (xem lý do ở đó). */
    fun findUsbmuxInterface(device: UsbDevice): UsbInterface? =
        findUsbmuxInterfaceWithConfig(device)?.iface

    /**
     * Tìm interface usbmux (class=0xFF sub=0xFE proto=0x02) CÙNG VỚI cấu hình USB
     * (UsbConfiguration) thực sự chứa nó.
     *
     * Đây là chỗ sửa lỗi cốt lõi cho "Không claim được interface usbmux" xảy ra
     * NGAY CẢ SAU KHI ĐÃ THỬ LẠI NHIỀU LẦN (dấu hiệu cho thấy đây không phải lỗi
     * tạm thời mà retry có thể tự sửa được): `UsbDevice.getInterface()` gộp
     * chung danh sách interface từ TẤT CẢ các cấu hình mà thiết bị khai báo, kể
     * cả những cấu hình KHÔNG PHẢI cấu hình đang thực sự hoạt động trên phần
     * cứng. iPhone thường khai báo nhiều hơn 1 cấu hình USB (ví dụ một số máy
     * mặc định enum ở cấu hình "tối giản" trước khi được host chọn cấu hình đầy
     * đủ có interface usbmux). Code cũ luôn gọi `setConfiguration(getConfiguration(0))`
     * — tức luôn ép về cấu hình ĐẦU TIÊN — bất kể interface usbmux tìm được thực
     * sự nằm ở cấu hình nào; nếu nó nằm ở cấu hình khác, `claimInterface()` sẽ
     * luôn thất bại vì ở tầng kernel, interface đó chưa từng active trên cấu
     * hình hiện tại của thiết bị. Vì đây không phải lỗi "may rủi theo thời
     * điểm" mà là ép sai cấu hình một cách hệ thống, không có số lần thử lại
     * nào sửa được — khớp với việc log của người dùng báo lỗi này liên tục,
     * ngay cả sau khi UsbTransport đã thử lại 3 lần.
     */
    private fun findUsbmuxInterfaceWithConfig(device: UsbDevice): FoundInterface? {
        for (c in 0 until device.configurationCount) {
            val config = device.getConfiguration(c)
            for (i in 0 until config.interfaceCount) {
                val iface = config.getInterface(i)
                if (iface.interfaceClass == INTERFACE_CLASS &&
                    iface.interfaceSubclass == INTERFACE_SUBCLASS &&
                    iface.interfaceProtocol == INTERFACE_PROTOCOL
                ) {
                    return FoundInterface(config, iface)
                }
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

        val found = findUsbmuxInterfaceWithConfig(device)
        if (found == null) {
            return "Không tìm thấy interface usbmux (class=0xFF sub=0xFE proto=0x02) trên thiết bị."
        }
        val (targetConfig, iface) = found

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

        // QUAN TRỌNG: phải set ĐÚNG cấu hình chứa interface usbmux tìm được ở trên
        // (targetConfig), KHÔNG phải luôn luôn cấu hình đầu tiên (getConfiguration(0))
        // như trước — xem giải thích đầy đủ ở kdoc của findUsbmuxInterfaceWithConfig().
        // Đây là nguyên nhân gốc thực sự của lỗi "Không claim được interface usbmux"
        // lặp lại mãi kể cả sau khi đã thử lại nhiều lần.
        try {
            val configured = conn.setConfiguration(targetConfig)
            if (!configured) {
                Log.w(TAG, "setConfiguration(id=${targetConfig.id}) trả false (có thể thiết bị chỉ có 1 cấu hình sẵn có — bỏ qua, thử claim trực tiếp).")
            }
        } catch (e: Exception) {
            Log.w(TAG, "setConfiguration(id=${targetConfig.id}) lỗi (bỏ qua, thử claimInterface trực tiếp): $e")
        }

        if (!conn.claimInterface(iface, true)) {
            conn.close()
            return "Không claim được interface usbmux (thiết bị có thể đang bị một app/dịch vụ " +
                "khác giữ, hoặc cần rút cắm lại cáp USB)."
        }

        // Kích hoạt đúng alternate setting của interface sau khi claim — cần thiết
        // với các thiết bị khai báo nhiều altsetting cho cùng một interface (không
        // phải mọi iPhone đều vậy, nhưng gọi luôn cho chắc; lỗi ở đây không nghiêm
        // trọng bằng lỗi claim nên chỉ log cảnh báo, không fail toàn bộ open()).
        try {
            if (!conn.setInterface(iface)) {
                Log.w(TAG, "setInterface() trả false (thường vô hại nếu interface chỉ có 1 altsetting).")
            }
        } catch (e: Exception) {
            Log.w(TAG, "setInterface() lỗi (bỏ qua): $e")
        }

        connection = conn
        usbInterface = iface
        endpointIn = inEp
        endpointOut = outEp
        _connected.value = true
        lastError = null
        Log.i(TAG, "Đã mở kết nối USB tới ${device.deviceName} (config id=${targetConfig.id}, maxPacketSize in=${inEp.maxPacketSize} out=${outEp.maxPacketSize}).")
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
