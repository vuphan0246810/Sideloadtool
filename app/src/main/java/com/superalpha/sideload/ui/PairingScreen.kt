package com.superalpha.sideload.ui

import android.content.Intent
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.CheckCircle
import androidx.compose.material.icons.filled.Link
import androidx.compose.material.icons.filled.Share
import androidx.compose.material.icons.filled.Usb
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.Icon
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import androidx.core.content.FileProvider
import com.superalpha.sideload.bridge.AppConfig

/**
 * PairingScreen — tab "Ghép nối", tách riêng khỏi luồng Cài IPA.
 *
 * Mục đích (theo yêu cầu người dùng): cho phép ghép nối với iPhone và tạo
 * file pairing MÀ KHÔNG cần chọn/ký một IPA trước — hữu ích để:
 *   - Kiểm tra riêng bước ghép nối USB có hoạt động đúng không (bước hay lỗi
 *     nhất, xem BUGFIX usbmux.c/usbmux.h: thiếu bắt tay VERSION trước SETUP).
 *   - Xuất pair record ra file .plist chuẩn (định dạng idevicepair) để lưu
 *     trữ/dùng lại với công cụ khác, hoặc backup trước khi reset ứng dụng.
 *
 * Luồng: Kết nối USB (UsbPermissionManager) → viewModel.connectAndPair()
 * (nativeConnect + nativePair, tự hiện Trust banner qua PromptDialogHost đã
 * gắn toàn cục ở MainActivity) → khi isPaired=true, cho phép xuất file.
 */
@Composable
fun PairingScreen(viewModel: HomeViewModel) {
    val context = androidx.compose.ui.platform.LocalContext.current
    val logLines by viewModel.log.collectAsState()
    val busy by viewModel.busy.collectAsState()
    val usbConnected by viewModel.usbConnected.collectAsState()
    val isPaired by viewModel.isPaired.collectAsState()
    val trustRequired by viewModel.trustRequired.collectAsState()

    var exportedPath by remember { mutableStateOf<String?>(null) }
    var exporting by remember { mutableStateOf(false) }

    LaunchedEffect(Unit) {
        // Không có cách đọc trạng thái "đã pair" từ trước nếu chưa gọi
        // connectAndPair() trong session này — native state chỉ tồn tại
        // trong bộ nhớ tiến trình (không phải đọc lại pair record trên đĩa
        // lúc mở màn hình), nên nhãn trạng thái mặc định là "chưa ghép nối"
        // cho tới khi người dùng bấm nút.
    }

    Column(modifier = Modifier.fillMaxSize().padding(16.dp)) {
        Text("Ghép nối iPhone", style = MaterialTheme.typography.titleLarge)
        Spacer(Modifier.height(8.dp))
        Text(
            "Kết nối và ghép nối (pairing) với iPhone qua USB độc lập với bước " +
                "Cài IPA — dùng để kiểm tra riêng bước bắt tay usbmux/Trust, hoặc " +
                "để tạo và lưu lại file pairing (.plist) cho thiết bị này.",
            style = MaterialTheme.typography.bodySmall
        )

        Spacer(Modifier.height(16.dp))

        Card(
            modifier = Modifier.fillMaxWidth(),
            colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surfaceVariant)
        ) {
            Column(modifier = Modifier.padding(16.dp)) {
                Row(verticalAlignment = Alignment.CenterVertically) {
                    Icon(
                        Icons.Filled.Usb,
                        contentDescription = null,
                        tint = if (usbConnected)
                            com.superalpha.sideload.ui.theme.BrandAccent
                        else
                            com.superalpha.sideload.ui.theme.BrandTextDim
                    )
                    Text(
                        text = if (usbConnected) "Đã kết nối USB" else "Chưa kết nối USB",
                        modifier = Modifier.padding(start = 8.dp)
                    )
                }
                Spacer(Modifier.height(4.dp))
                Row(verticalAlignment = Alignment.CenterVertically) {
                    Icon(
                        Icons.Filled.CheckCircle,
                        contentDescription = null,
                        tint = if (isPaired)
                            com.superalpha.sideload.ui.theme.BrandAccent
                        else
                            com.superalpha.sideload.ui.theme.BrandTextDim
                    )
                    Text(
                        text = if (isPaired) "Đã ghép nối (paired)" else "Chưa ghép nối",
                        modifier = Modifier.padding(start = 8.dp)
                    )
                }
                if (AppConfig.lastUdid.isNotBlank()) {
                    Spacer(Modifier.height(4.dp))
                    Text(
                        "UDID: ${AppConfig.lastUdid}",
                        style = MaterialTheme.typography.bodySmall
                    )
                }
                if (trustRequired) {
                    Spacer(Modifier.height(8.dp))
                    Text(
                        "⚠️ Kiểm tra màn hình iPhone và bấm \"Tin cậy\" (Trust This Computer).",
                        color = MaterialTheme.colorScheme.error,
                        style = MaterialTheme.typography.bodySmall
                    )
                }
            }
        }

        Spacer(Modifier.height(16.dp))

        Button(
            enabled = !busy,
            modifier = Modifier.fillMaxWidth(),
            onClick = { viewModel.connectAndPair() }
        ) {
            if (busy) {
                CircularProgressIndicator(modifier = Modifier.height(16.dp), strokeWidth = 2.dp)
                Spacer(Modifier.height(0.dp))
                Text(" Đang ghép nối...", modifier = Modifier.padding(start = 8.dp))
            } else {
                Icon(Icons.Filled.Link, contentDescription = null)
                Text(" Kết nối & Ghép nối iPhone", modifier = Modifier.padding(start = 8.dp))
            }
        }

        Spacer(Modifier.height(8.dp))

        OutlinedButton(
            enabled = isPaired && !exporting,
            modifier = Modifier.fillMaxWidth(),
            onClick = {
                exporting = true
                viewModel.exportPairingFile { file ->
                    exporting = false
                    if (file != null) {
                        exportedPath = file.absolutePath
                        try {
                            val uri = FileProvider.getUriForFile(
                                context, "${context.packageName}.fileprovider", file
                            )
                            val shareIntent = Intent(Intent.ACTION_SEND).apply {
                                type = "application/x-plist"
                                putExtra(Intent.EXTRA_STREAM, uri)
                                addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
                            }
                            context.startActivity(
                                Intent.createChooser(shareIntent, "Lưu / chia sẻ file pairing")
                            )
                        } catch (_: Exception) {
                            // Không có app nào xử lý share — người dùng vẫn thấy
                            // đường dẫn file qua Text bên dưới.
                        }
                    }
                }
            }
        ) {
            Icon(Icons.Filled.Share, contentDescription = null)
            Text(" Tạo & chia sẻ file pairing (.plist)", modifier = Modifier.padding(start = 8.dp))
        }

        if (exportedPath != null) {
            Spacer(Modifier.height(4.dp))
            Text(
                "Đã lưu tại: $exportedPath",
                style = MaterialTheme.typography.bodySmall
            )
        }

        Spacer(Modifier.height(16.dp))
        Row(modifier = Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween) {
            Text("Nhật ký:", style = MaterialTheme.typography.labelLarge)
        }
        Spacer(Modifier.height(4.dp))
        LogConsole(lines = logLines, modifier = Modifier.weight(1f))
    }
}
