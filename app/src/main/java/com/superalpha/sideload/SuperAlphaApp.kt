package com.superalpha.sideload

import android.app.Application
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.hardware.usb.UsbDevice
import android.hardware.usb.UsbManager
import android.os.Build
import com.chaquo.python.Python
import com.chaquo.python.android.AndroidPlatform
import com.superalpha.sideload.bridge.NativeLog
import com.superalpha.sideload.bridge.UsbTransport

/**
 * Application entry point. Starts the embedded Python interpreter exactly once per
 * process, and registers a process-wide listener for USB detach events so a stale
 * [UsbTransport] connection is reset the moment the iPhone/iPad is unplugged or
 * re-enumerates — otherwise the next "Kết nối" tap could try to reuse endpoints/an
 * interface handle from a connection the OS has already torn down, which previously
 * had no dedicated cleanup path (only [UsbTransport.open] ever called `close()`,
 * right before opening a fresh connection — never on an unplug by itself). Everything
 * else (USB permission flow, UI) is initialized lazily from MainActivity.
 */
class SuperAlphaApp : Application() {
    override fun onCreate() {
        super.onCreate()
        if (!Python.isStarted()) {
            Python.start(AndroidPlatform(this))
        }
        registerUsbDetachReceiver()
    }

    private fun registerUsbDetachReceiver() {
        val receiver = object : BroadcastReceiver() {
            override fun onReceive(context: Context, intent: Intent) {
                if (intent.action != UsbManager.ACTION_USB_DEVICE_DETACHED) return
                val device: UsbDevice? = intent.getParcelableExtra(UsbManager.EXTRA_DEVICE)
                if (device != null && device.vendorId != UsbTransport.VENDOR_ID_APPLE) return
                if (!UsbTransport.isConnected()) return
                UsbTransport.close()
                NativeLog.log("Thiết bị USB đã rút — đã đóng kết nối. Cắm lại và bấm \"Kết nối\" để tiếp tục.")
            }
        }
        val filter = IntentFilter(UsbManager.ACTION_USB_DEVICE_DETACHED)
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            registerReceiver(receiver, filter, Context.RECEIVER_EXPORTED)
        } else {
            registerReceiver(receiver, filter)
        }
    }
}
