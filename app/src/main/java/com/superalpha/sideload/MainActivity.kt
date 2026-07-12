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

    // launchMode="singleTop" (AndroidManifest.xml) nghĩa là nếu app đang mở sẵn
    // và người dùng cắm iPhone vào (khớp device_filter.xml), Android gửi intent
    // ACTION_USB_DEVICE_ATTACHED mới vào ĐÂY qua onNewIntent — KHÔNG qua onCreate
    // lần nữa. Trước đây intent này hoàn toàn không được đọc ở cả hai nơi, nên
    // việc cắm dây không tự kết nối được gì — người dùng luôn phải tự bấm "Kết
    // nối" thủ công dù Android đã "biết" có thiết bị mới cắm vào.
    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        setIntent(intent)
        handleUsbAttachIntent(intent)
    }

    private fun handleUsbAttachIntent(intent: Intent?) {
        if (intent?.action != UsbManager.ACTION_USB_DEVICE_ATTACHED) return
        NativeLog.log("Đã phát hiện iPhone/iPad vừa cắm vào — đang tự động kết nối...")
        UsbPermissionManager.requestAndOpen(this) { _, msg -> NativeLog.log(msg) }
    }
}
