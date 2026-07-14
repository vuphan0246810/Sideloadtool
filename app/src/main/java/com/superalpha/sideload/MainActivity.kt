package com.superalpha.sideload

import android.content.Intent
import android.hardware.usb.UsbManager
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.viewModels
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.material3.Surface
import androidx.compose.ui.Modifier
import com.superalpha.sideload.bridge.AppPaths
import com.superalpha.sideload.bridge.NativeLog
import com.superalpha.sideload.bridge.UsbPermissionManager
import com.superalpha.sideload.ui.AppNavHost
import com.superalpha.sideload.ui.HomeViewModel
import com.superalpha.sideload.ui.theme.SuperAlphaTheme

class MainActivity : ComponentActivity() {
    private val viewModel: HomeViewModel by viewModels()

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        AppPaths.init(applicationContext)
        NativeLog.log("SUPER ALPHA Sideload đã khởi động.")

        setContent {
            SuperAlphaTheme {
                Surface(modifier = Modifier.fillMaxSize()) {
                    AppNavHost(viewModel = viewModel)
                    com.superalpha.sideload.ui.PromptDialogHost()
                }
            }
        }

        handleUsbAttachIntent(intent)
    }

    // launchMode="singleTop" → Android gửi intent USB_DEVICE_ATTACHED mới vào
    // đây qua onNewIntent khi app đang chạy. Truyền fromAutoAttach=true để
    // UsbPermissionManager áp dụng cooldown 3 giây, tránh vòng lặp vô tận khi
    // claimInterface() fail.
    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        setIntent(intent)
        handleUsbAttachIntent(intent)
    }

    private fun handleUsbAttachIntent(intent: Intent?) {
        if (intent?.action != UsbManager.ACTION_USB_DEVICE_ATTACHED) return
        NativeLog.log("Đã phát hiện iPhone/iPad vừa cắm vào — đang tự động kết nối...")
        // FIX: fromAutoAttach = true → áp dụng cooldown để phá vòng lặp
        // claimInterface fail → conn.close() → re-enumerate → ATTACHED → fail → ...
        UsbPermissionManager.requestAndOpen(this, fromAutoAttach = true) { _, msg ->
            NativeLog.log(msg)
        }
    }
}
