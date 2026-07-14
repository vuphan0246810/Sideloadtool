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
 * ═══════════════════════════════════════════════════════════════════
 * FIX: Cooldown chính xác để phá vòng lặp claimInterface → close → re-enumerate → ATTACHED
 *
 * Root cause vòng lặp:
 *   a) UsbTransport.open() thực hiện retry tối đa 5 lần, mỗi lần cách nhau
 *      200-800ms → tổng thời gian thất bại ≈ 3-4 giây.
 *   b) lastAttemptTime được ghi lúc BẮT ĐẦU lần thử. Khi finish(false) được
 *      gọi sau 3-4 giây, elapsed đã ≥ cooldown (3s) → cooldown KHÔNG chặn được!
 *   c) Kết quả: mỗi lần claimInterface fail → conn.close() → Android re-enum →
 *      ATTACHED → requestAndOpen() bypass cooldown → fail → loop vô tận.
 *
 * Fix (2 thay đổi):
 *   1. Trong finish(false), RESET lastAttemptTime = System.currentTimeMillis().
 *      Cooldown bây giờ tính từ lúc THẤT BẠI được ghi nhận, không phải lúc bắt đầu.
 *   2. Tăng cooldown từ 3s lên 8s — đủ dài để Android hoàn tất re-enumerate
 *      và ATTACHED event được xử lý, nhưng không quá lâu để UX chịu được.
 * ═══════════════════════════════════════════════════════════════════
 */
object UsbPermissionManager {
    private const val ACTION_USB_PERMISSION = "com.superalpha.sideload.USB_PERMISSION"

    /** Cooldown SAU KHI thất bại (ms) — ngăn vòng lặp ATTACHED → fail → ATTACHED */
    private const val AUTO_CONNECT_COOLDOWN_MS = 8_000L

    /**
     * Thời điểm THẤT BẠI gần nhất được ghi nhận (System.currentTimeMillis()).
     * BẮT ĐẦU LÚC 0 → never failed → elapsed luôn lớn → lần đầu tiên luôn được thử.
     * Được RESET về now() trong finish(false) — không phải lúc bắt đầu lần thử.
     */
    private val lastFailTimestampMs = AtomicLong(0L)

    private val ioExecutor = Executors.newSingleThreadExecutor()

    @Volatile private var requestInFlight = false
    @Volatile private var pendingReceiver: BroadcastReceiver? = null

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
     * @param fromAutoAttach true khi gọi từ USB_DEVICE_ATTACHED (auto-connect) →
     *   áp dụng cooldown 8 giây sau thất bại để tránh vòng lặp.
     *   false khi người dùng bấm "Kết nối" thủ công → bỏ qua cooldown.
     */
    fun requestAndOpen(
        context: Context,
        fromAutoAttach: Boolean = false,
        onResult: (Boolean, String) -> Unit = { _, _ -> }
    ) {
        // ── Cooldown check (chỉ áp dụng cho auto-attach) ───────────────────────
        if (fromAutoAttach) {
            val elapsed = System.currentTimeMillis() - lastFailTimestampMs.get()
            if (elapsed < AUTO_CONNECT_COOLDOWN_MS) {
                // Còn trong 8 giây sau lần fail cuối → bỏ qua hoàn toàn
                NativeLog.emit(
                    "[usb] Bỏ qua auto-connect: cooldown ${AUTO_CONNECT_COOLDOWN_MS / 1000}s " +
                    "sau thất bại (còn ${(AUTO_CONNECT_COOLDOWN_MS - elapsed) / 1000}s)."
                )
                return
            }
        }

        // ── Guard: Chỉ một lần thử tại một thời điểm ──────────────────────────
        if (requestInFlight) {
            if (!fromAutoAttach) {
                onResult(false, "Đang xử lý yêu cầu kết nối trước đó — vui lòng đợi một chút rồi thử lại.")
            }
            return
        }
        requestInFlight = true

        val mainHandler = android.os.Handler(android.os.Looper.getMainLooper())

        /** Kết thúc một lần thử.
         *  QUAN TRỌNG: Nếu fail, reset lastFailTimestampMs = NOW để cooldown
         *  tính từ thời điểm thất bại được ghi nhận, không phải lúc bắt đầu lần thử.
         *  Nếu không reset ở đây, vòng lặp retry (3-4 giây) làm cho cooldown đã
         *  hết hạn ngay khi finish() được gọi. */
        fun finish(ok: Boolean, msg: String) {
            if (!ok) {
                // ← FIX CHÍNH: Reset cooldown timer TẠI THỜI ĐIỂM THẤT BẠI
                lastFailTimestampMs.set(System.currentTimeMillis())
            }
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

        // ── Xin quyền USB (system dialog) ─────────────────────────────────────
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
