package com.superalpha.sideload.bridge

import android.app.PendingIntent
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.hardware.usb.UsbDevice
import android.hardware.usb.UsbManager
import android.os.Build
import com.chaquo.python.Python
import java.util.concurrent.Executors

/**
 * Wraps the standard three-step Android USB permission dance:
 *   1. find the device,
 *   2. ask the user (system dialog) if not already granted,
 *   3. hand the granted device to [UsbTransport.open].
 */
object UsbPermissionManager {
    private const val ACTION_USB_PERMISSION = "com.superalpha.sideload.USB_PERMISSION"

    // [UsbTransport.open] retries with short sleeps between attempts (see its
    // kdoc) and must not run on the caller's thread when that thread is the main
    // thread — the permission BroadcastReceiver below is delivered on the main
    // thread by default, and requestAndOpen() itself is normally called directly
    // from a Compose click handler (also main thread). This single-thread
    // executor keeps all actual open() calls (and their retries) off the UI
    // thread without pulling in a full coroutine dependency here.
    private val ioExecutor = Executors.newSingleThreadExecutor()

    // Bấm nhiều lần liên tục vào "Kết nối" trước khi lần xin quyền trước hoàn
    // tất từng tạo ra nhiều BroadcastReceiver ACTION_USB_PERMISSION cùng đăng ký
    // song song — mỗi cái gọi onResult() riêng, gây ra chuỗi log trùng lặp kiểu
    // "Mở kết nối USB thất bại" nhiều lần y hệt (đúng như trong video lỗi gốc).
    // Cờ này chặn việc bắt đầu một yêu cầu quyền mới khi một yêu cầu khác chưa
    // xử lý xong.
    @Volatile private var requestInFlight = false
    @Volatile private var pendingReceiver: BroadcastReceiver? = null

    /**
     * Publishes the connected device's USB serial number (== UDID for essentially every
     * iPhone/iPad) into sideload_core.py's cache, so device_link.get_udid_from_usb()
     * never has to round-trip lockdownd just to answer "which device is this". Reading
     * UsbDevice.getSerialNumber() this way (right after our own requestPermission grant)
     * does not need any extra Android runtime permission beyond the USB device grant
     * itself, per the platform's USB Host API contract.
     */
    private fun publishUdid(device: UsbDevice) {
        val serial = try {
            device.serialNumber
        } catch (_: Exception) {
            null
        }
        if (!serial.isNullOrBlank()) {
            try {
                Python.getInstance().getModule("sideload_core").callAttr("set_current_udid", serial)
            } catch (_: Exception) {
                // Chaquopy Python may not be started yet the very first time; harmless,
                // the UDID will just need to be looked up again on next attempt.
            }
        }
    }

    /** Mô tả lỗi cụ thể từ [UsbTransport.open] gần nhất (chi tiết hơn câu chung
     * "Mở kết nối USB thất bại."), nếu có. */
    private fun openFailureMessage(): String {
        val detail = UsbTransport.lastError()
        return if (detail.isNullOrBlank()) {
            "Mở kết nối USB thất bại."
        } else {
            "Mở kết nối USB thất bại: $detail"
        }
    }

    /**
     * Looks for an attached Apple device and, if found, requests permission (or opens
     * it immediately if permission is already granted from a previous attach). Calls
     * [onResult] with true/connected or false/not-found-or-denied — always on the main
     * thread, so callers (Compose click handlers, NativeLog) don't need to worry about
     * which thread it runs on. Safe to call multiple times: if a request is already in
     * flight (e.g. the user tapped "Connect" repeatedly before the first attempt
     * finished), subsequent calls are ignored instead of stacking up duplicate
     * permission receivers/open attempts.
     */
    fun requestAndOpen(context: Context, onResult: (Boolean, String) -> Unit) {
        if (requestInFlight) {
            onResult(false, "Đang xử lý yêu cầu kết nối trước đó — vui lòng đợi một chút rồi thử lại.")
            return
        }
        requestInFlight = true

        val mainHandler = android.os.Handler(android.os.Looper.getMainLooper())
        fun finish(ok: Boolean, msg: String) {
            requestInFlight = false
            mainHandler.post { onResult(ok, msg) }
        }

        val usbManager = context.getSystemService(Context.USB_SERVICE) as UsbManager
        val device = UsbTransport.findAppleDevice(usbManager)
        if (device == null) {
            finish(false, "Không tìm thấy iPhone/iPad nào đang cắm qua USB.")
            return
        }

        if (usbManager.hasPermission(device)) {
            ioExecutor.execute {
                val ok = UsbTransport.open(usbManager, device)
                if (ok) publishUdid(device)
                finish(ok, if (ok) "Đã kết nối USB." else openFailureMessage())
            }
            return
        }

        // Nếu có một receiver xin quyền cũ còn treo (vd từ một lần gọi trước bị bỏ
        // giữa đường), gỡ nó trước khi đăng ký cái mới — tránh hai receiver cùng
        // xử lý một ACTION_USB_PERMISSION.
        pendingReceiver?.let {
            try {
                context.unregisterReceiver(it)
            } catch (_: Exception) {
            }
            pendingReceiver = null
        }

        val flags = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
            PendingIntent.FLAG_MUTABLE
        } else {
            0
        }
        val permissionIntent = PendingIntent.getBroadcast(
            context, 0, Intent(ACTION_USB_PERMISSION).setPackage(context.packageName), flags
        )

        val receiver = object : BroadcastReceiver() {
            override fun onReceive(ctx: Context, intent: Intent) {
                if (intent.action != ACTION_USB_PERMISSION) return
                try {
                    ctx.unregisterReceiver(this)
                } catch (_: Exception) {
                }
                pendingReceiver = null
                val granted = intent.getBooleanExtra(UsbManager.EXTRA_PERMISSION_GRANTED, false)
                if (!granted) {
                    finish(false, "Người dùng từ chối quyền truy cập USB.")
                    return
                }
                val grantedDevice: UsbDevice? = intent.getParcelableExtra(UsbManager.EXTRA_DEVICE)
                if (grantedDevice == null) {
                    finish(false, "Không nhận được thiết bị sau khi cấp quyền.")
                    return
                }
                ioExecutor.execute {
                    val ok = UsbTransport.open(usbManager, grantedDevice)
                    if (ok) publishUdid(grantedDevice)
                    finish(ok, if (ok) "Đã kết nối USB." else openFailureMessage())
                }
            }
        }
        pendingReceiver = receiver

        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            context.registerReceiver(receiver, IntentFilter(ACTION_USB_PERMISSION), Context.RECEIVER_NOT_EXPORTED)
        } else {
            context.registerReceiver(receiver, IntentFilter(ACTION_USB_PERMISSION))
        }
        usbManager.requestPermission(device, permissionIntent)
    }
}
