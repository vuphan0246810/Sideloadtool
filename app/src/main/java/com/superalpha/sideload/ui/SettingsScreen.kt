package com.superalpha.sideload.ui

import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.material3.Divider
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import com.superalpha.sideload.bridge.AppPaths

/** Static info screen: app-private paths, and an explicit disclaimer about the
 * experimental USB transport, so the user always has this context inside the app
 * itself (not just in the README they may never open). */
@Composable
fun SettingsScreen(viewModel: HomeViewModel) {
    Column(modifier = Modifier.fillMaxSize().padding(16.dp)) {
        Text("Thông tin & Cảnh báo", style = MaterialTheme.typography.titleLarge)
        Spacer(Modifier.height(12.dp))
        Text(
            "Ứng dụng này kết nối trực tiếp tới iPhone qua cổng USB bằng USB Host API " +
                "của Android (không cần Termux, không cần root). Lớp giao tiếp usbmux " +
                "(mux_usb.py) là phần tự triển khai lại từ giao thức gốc của libimobiledevice " +
                "và CHƯA được kiểm chứng trên phần cứng thật — hãy xem README.md, mục " +
                "\"Rủi ro đã biết\" trước khi dùng với thiết bị quan trọng.",
            style = MaterialTheme.typography.bodyMedium
        )
        Spacer(Modifier.height(16.dp))
        Divider()
        Spacer(Modifier.height(16.dp))
        Text("Thư mục dữ liệu ứng dụng:", style = MaterialTheme.typography.labelLarge)
        Text(AppPaths.filesDir(), style = MaterialTheme.typography.bodySmall)
        Spacer(Modifier.height(8.dp))
        Text("Đường dẫn zsign:", style = MaterialTheme.typography.labelLarge)
        Text(AppPaths.zsignPath(), style = MaterialTheme.typography.bodySmall)
    }
}
