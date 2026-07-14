package com.superalpha.sideload.bridge

import android.app.PendingIntent
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.hardware.usb.UsbDevice
import android.hardware.usb.UsbManager
import android.os.Build
import java.util.concurrent.Executors
import java.util.concurrent.atomic.AtomicLong

/**
 * Wraps the standard three-step Android USB permission dance:
 *   1. find the device,
 *   2. ask the user (system dialog) if not already granted,
 *   3. hand the granted device to [UsbTransport.open].
 *
 * FIX: Thêm cooldown 3 giây giữa các lần thử kết nối.
 *
 * Root cause vòng lặp "claimInterface() thất bại" lặp vô tận:
 *   Sau khi claimInterface() fail và ta gọi conn.close(), Android có thể
 *   re-enumerate thiết bị và gửi lại ACTION_USB_DEVICE_ATTACHED. Nếu không
 *   có cooldown, MainActivity.handleUsbAttachIntent() lại gọi requestAndOpen()
 *   ngay lập tức → fail → close → ATTACHED lại → vòng lặp vô hạn.
 *
 *   Giải pháp: Ghi timestamp lần thử cuối. Nếu lần thử gần nhất < 3 giây
 *   trước VÀ thất bại, từ chối ngay (không mở dialog quyền, không gọi open).
 *   Kết quả: vòng lặp tự dừng sau một lần fail, không spam log nữa.
 */
object UsbPermissionManager {
    private const val ACTION_USB_PERMISSION = "com.superalpha.sideload.USB_PERMISSION"

    // Cooldown giữa các lần thử tự động (ms) — ngăn vòng lặp ATTACHED → fail → ATTACHED
    private const val AUTO_CONNECT_COOLDOWN_MS = 3_000L

    // Thời điểm lần thử gần nhất (System.currentTimeMillis())
    private val lastAttemptTime = AtomicLong(0L)
    // Kết quả lần thử cuối: true = thành công, false = thất bại
    @Volatile private var lastAttemptSucceeded = false

    private val ioExecutor = Executors.newSingleThreadExecutor()

    @Volatile private var requestInFlight = false
    @Volatile private var pendingReceiver: BroadcastReceiver? = null

    /**
     * Publishes the connected device's USB serial number (== UDID for essentially every
     * iPhone/iPad) into [AppConfig.lastUdid].
     */
    private fun publishUdid(device: UsbDevice) {
        val serial = try { device.serialNumber } catch (_: Exception) { null }
        if (!serial.isNullOrBlank()) {
            try { AppConfig.lastUdid = serial } catch (_: Exception) {}
        }
    }

    private fun openFailureMessage(): String {
        val detail = UsbTransport.lastError()
        return if (detail.isNullOrBlank()) "Mở kết nối USB thất bại."
        else "Mở kết nối USB thất bại: $detail"
    }

    /**
     * Tìm thiết bị Apple, xin quyền (hoặc mở ngay nếu đã có quyền), gọi [onResult].
     *
     * @param fromAutoAttach true nếu được gọi từ USB_DEVICE_ATTACHED intent (auto-connect).
     *   Khi đó áp dụng cooldown 3 giây để tránh vòng lặp.
     *   false khi người dùng bấm "Kết nối" thủ công — bỏ qua cooldown.
     */
    fun requestAndOpen(
        context: Context,
        fromAutoAttach: Boolean = false,
        onResult: (Boolean, String) -> Unit
    ) {
        // FIX: Kiểm tra cooldown khi gọi từ auto-attach (không áp dụng khi bấm tay)
        if (fromAutoAttach) {
            val now = System.currentTimeMillis()
            val elapsed = now - lastAttemptTime.get()
            if (!lastAttemptSucceeded && elapsed < AUTO_CONNECT_COOLDOWN_MS) {
                // Còn trong cooldown sau lần fail trước — bỏ qua để tránh vòng lặp
                return
            }
        }

        if (requestInFlight) {
            if (!fromAutoAttach) {
                onResult(false, "Đang xử lý yêu cầu kết nối trước đó — vui lòng đợi một chút rồi thử lại.")
            }
            return
        }
        requestInFlight = true
        lastAttemptTime.set(System.currentTimeMillis())

        val mainHandler = android.os.Handler(android.os.Looper.getMainLooper())
        fun finish(ok: Boolean, msg: String) {
            lastAttemptSucceeded = ok
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
                val ok = UsbTransport.open(device, usbManager)
                if (ok) publishUdid(device)
                finish(ok, if (ok) "Đã kết nối USB." else openFailureMessage())
            }
            return
        }

        // Gỡ receiver cũ nếu còn treo
        pendingReceiver?.let {
            try { context.unregisterReceiver(it) } catch (_: Exception) {}
            pendingReceiver = null
        }

        val flags = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S)
            PendingIntent.FLAG_MUTABLE else 0
        val permissionIntent = PendingIntent.getBroadcast(
            context, 0, Intent(ACTION_USB_PERMISSION).setPackage(context.packageName), flags
        )

        val receiver = object : BroadcastReceiver() {
            override fun onReceive(ctx: Context, intent: Intent) {
                if (intent.action != ACTION_USB_PERMISSION) return
                try { ctx.unregisterReceiver(this) } catch (_: Exception) {}
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
                    val ok = UsbTransport.open(grantedDevice, usbManager)
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
