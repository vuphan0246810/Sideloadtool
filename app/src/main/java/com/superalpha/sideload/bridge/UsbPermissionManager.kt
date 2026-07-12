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

/**
 * Wraps the standard three-step Android USB permission dance:
 *   1. find the device,
 *   2. ask the user (system dialog) if not already granted,
 *   3. hand the granted device to [UsbTransport.open].
 */
object UsbPermissionManager {
    private const val ACTION_USB_PERMISSION = "com.superalpha.sideload.USB_PERMISSION"

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

    /**
     * Looks for an attached Apple device and, if found, requests permission (or opens
     * it immediately if permission is already granted from a previous attach). Calls
     * [onResult] with true/connected or false/not-found-or-denied. Safe to call
     * multiple times; a stale receiver is unregistered before a new one is added.
     */
    fun requestAndOpen(context: Context, onResult: (Boolean, String) -> Unit) {
        val usbManager = context.getSystemService(Context.USB_SERVICE) as UsbManager
        val device = UsbTransport.findAppleDevice(usbManager)
        if (device == null) {
            onResult(false, "Không tìm thấy iPhone/iPad nào đang cắm qua USB.")
            return
        }

        if (usbManager.hasPermission(device)) {
            val ok = UsbTransport.open(usbManager, device)
            if (ok) publishUdid(device)
            onResult(ok, if (ok) "Đã kết nối USB." else "Mở kết nối USB thất bại.")
            return
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
                val granted = intent.getBooleanExtra(UsbManager.EXTRA_PERMISSION_GRANTED, false)
                if (!granted) {
                    onResult(false, "Người dùng từ chối quyền truy cập USB.")
                    return
                }
                val grantedDevice: UsbDevice? = intent.getParcelableExtra(UsbManager.EXTRA_DEVICE)
                if (grantedDevice == null) {
                    onResult(false, "Không nhận được thiết bị sau khi cấp quyền.")
                    return
                }
                val ok = UsbTransport.open(usbManager, grantedDevice)
                if (ok) publishUdid(grantedDevice)
                onResult(ok, if (ok) "Đã kết nối USB." else "Mở kết nối USB thất bại.")
            }
        }

        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            context.registerReceiver(receiver, IntentFilter(ACTION_USB_PERMISSION), Context.RECEIVER_NOT_EXPORTED)
        } else {
            context.registerReceiver(receiver, IntentFilter(ACTION_USB_PERMISSION))
        }
        usbManager.requestPermission(device, permissionIntent)
    }
}
