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
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.unit.dp
import androidx.core.content.FileProvider
import com.superalpha.sideload.bridge.AppConfig
import com.superalpha.sideload.bridge.UsbPermissionManager

/**
 * PairingScreen — tab "Ghép nối", tách riêng khỏi luồng Cài IPA.
 *
 * Mục đích: cho phép ghép nối với iPhone và tạo file pairing MÀ KHÔNG cần
 * chọn/ký một IPA trước.
 *
 * BUGFIX v17 — Luồng nút "Kết nối & Ghép nối iPhone":
 *   Trước đây nút gọi viewModel.connectAndPair() trực tiếp. Nếu USB chưa
 *   được mở (người dùng mở app TRƯỚC khi cắm cáp, hoặc UsbTransport.open()
 *   chưa được gọi), native C sẽ thất bại ngay khi gọi usb_bulk_write vì
 *   UsbTransport.endpointOut == null → trả -1 → VERSION packet gửi không được
 *   → mux_do_setup() thất bại ngay bước đầu tiên.
 *
 *   Fix: nếu USB chưa kết nối (usbConnected == false), gọi
 *   UsbPermissionManager.requestAndOpen() trước để mở kết nối USB, rồi khi
 *   thành công MỚI gọi viewModel.connectAndPair(). Người dùng thấy rõ hướng
 *   dẫn và không bị "SETUP thất bại" mơ hồ.
 *
 * Luồng hoàn chỉnh:
 *   Nếu USB đã kết nối  → connectAndPair() (VERSION → SETUP → lockdown → Pair)
 *   Nếu USB chưa kết nối → requestAndOpen() → [thành công] → connectAndPair()
 *                                           → [thất bại]   → log lỗi rõ ràng
 */
@Composable
fun PairingScreen(viewModel: HomeViewModel) {
    val context = LocalContext.current
    val logLines by viewModel.log.collectAsState()
    val busy by viewModel.busy.collectAsState()
    val usbConnected by viewModel.usbConnected.collectAsState()
    val isPaired by viewModel.isPaired.collectAsState()

    var exportedPath by remember { mutableStateOf<String?>(null) }
    var exporting by remember { mutableStateOf(false) }

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

        /* ── Thẻ trạng thái USB / Ghép nối ── */
        Card(
            modifier = Modifier.fillMaxWidth(),
            colors = CardDefaults.cardColors(
                containerColor = MaterialTheme.colorScheme.surfaceVariant
            )
        ) {
            Column(modifier = Modifier.padding(12.dp)) {
                Row(verticalAlignment = Alignment.CenterVertically) {
                    Icon(
                        imageVector = Icons.Filled.Usb,
                        contentDescription = null,
                        tint = if (usbConnected) MaterialTheme.colorScheme.primary
                               else MaterialTheme.colorScheme.onSurfaceVariant
                    )
                    Spacer(Modifier.height(0.dp).padding(start = 8.dp))
                    Text(
                        text = if (usbConnected) "Đã kết nối USB" else "Chưa kết nối USB",
                        modifier = Modifier.padding(start = 8.dp),
                        style = MaterialTheme.typography.bodyMedium,
                        color = if (usbConnected) MaterialTheme.colorScheme.primary
                                else MaterialTheme.colorScheme.onSurfaceVariant
                    )
                }
                Spacer(Modifier.height(4.dp))
                Row(verticalAlignment = Alignment.CenterVertically) {
                    Icon(
                        imageVector = Icons.Filled.CheckCircle,
                        contentDescription = null,
                        tint = if (isPaired) MaterialTheme.colorScheme.primary
                               else MaterialTheme.colorScheme.onSurfaceVariant
                    )
                    Text(
                        text = if (isPaired) "Đã ghép nối" else "Chưa ghép nối",
                        modifier = Modifier.padding(start = 8.dp),
                        style = MaterialTheme.typography.bodyMedium,
                        color = if (isPaired) MaterialTheme.colorScheme.primary
                                else MaterialTheme.colorScheme.onSurfaceVariant
                    )
                }
                val udid = AppConfig.lastUdid
                if (udid.isNotBlank()) {
                    Spacer(Modifier.height(4.dp))
                    Text(
                        text = "UDID: $udid",
                        style = MaterialTheme.typography.bodySmall,
                        color = MaterialTheme.colorScheme.onSurfaceVariant
                    )
                }
            }
        }

        Spacer(Modifier.height(16.dp))

        /*
         * Nút "Kết nối & Ghép nối iPhone"
         *
         * BUGFIX v17: Nếu USB chưa kết nối, gọi UsbPermissionManager.requestAndOpen()
         * trước. Sau khi USB mở thành công, MỚI gọi connectAndPair().
         * Tránh lỗi "SETUP thất bại" khi native C gọi usb_bulk_write lúc
         * UsbTransport chưa được khởi tạo.
         */
        Button(
            enabled = !busy,
            modifier = Modifier.fillMaxWidth(),
            onClick = {
                if (usbConnected) {
                    /* USB đã mở sẵn → pair ngay */
                    viewModel.connectAndPair()
                } else {
                    /* USB chưa mở → xin quyền & mở trước */
                    viewModel.setBusy(true)
                    UsbPermissionManager.requestAndOpen(
                        context = context,
                        fromAutoAttach = false   /* người dùng chủ động bấm → không cooldown */
                    ) { ok, msg ->
                        viewModel.emitLog(if (ok) "[usb] \u2705 $msg" else "[usb] \u274c $msg")
                        if (ok) {
                            /* USB vừa mở thành công → tiến hành pair */
                            viewModel.connectAndPair()
                        } else {
                            viewModel.setBusy(false)
                        }
                    }
                }
            }
        ) {
            if (busy) {
                CircularProgressIndicator(modifier = Modifier.height(16.dp), strokeWidth = 2.dp)
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
                            /* Không có app nào xử lý share — đường dẫn vẫn hiện bên dưới */
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
