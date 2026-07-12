package com.superalpha.sideload.ui

import android.content.Intent
import android.net.Uri
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.material3.Button
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.Icon
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Usb
import androidx.compose.runtime.Composable
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.unit.dp
import com.superalpha.sideload.bridge.NativeLog
import com.superalpha.sideload.bridge.UsbPermissionManager
import com.superalpha.sideload.python.PythonBridge
import kotlinx.coroutines.launch
import java.io.File
import java.io.FileOutputStream

/**
 * Main "sideload an IPA" flow: pick a file, connect USB, enter Apple ID, run.
 * Mirrors main.py's option 1 ("Ký và cài đặt IPA") from the original CLI tool.
 */
@Composable
fun SideloadScreen(viewModel: HomeViewModel) {
    val context = LocalContext.current
    val scope = rememberCoroutineScope()
    val logLines by viewModel.log.collectAsState()
    val usbConnected by viewModel.usbConnected.collectAsState()
    val busy by viewModel.busy.collectAsState()
    val savedAppleId by viewModel.savedAppleId.collectAsState()
    val savedAnisetteUrl by viewModel.savedAnisetteUrl.collectAsState()

    var ipaPath by remember { mutableStateOf<String?>(null) }
    var ipaName by remember { mutableStateOf("Chưa chọn file IPA") }
    var appleId by remember { mutableStateOf("") }
    var appleIdPrefilled by remember { mutableStateOf(false) }
    var password by remember { mutableStateOf("") }

    // Tự điền Apple ID đã lưu trong Cài đặt, một lần duy nhất khi có giá trị
    // (không đè lên nếu người dùng đã tự gõ gì đó trước khi giá trị lưu tải xong).
    androidx.compose.runtime.LaunchedEffect(savedAppleId) {
        if (!appleIdPrefilled && savedAppleId.isNotBlank()) {
            if (appleId.isBlank()) appleId = savedAppleId
            appleIdPrefilled = true
        }
    }

    val pickIpaLauncher = rememberLauncherForActivityResult(ActivityResultContracts.OpenDocument()) { uri: Uri? ->
        if (uri == null) return@rememberLauncherForActivityResult
        // sideload_core.py / zsign need a real filesystem path, not a content:// Uri,
        // so copy the picked file into app-private storage first.
        val dest = File(context.filesDir, "picked.ipa")
        context.contentResolver.openInputStream(uri)?.use { input ->
            FileOutputStream(dest).use { output -> input.copyTo(output) }
        }
        ipaPath = dest.absolutePath
        ipaName = uri.lastPathSegment ?: "IPA đã chọn"
        NativeLog.log("Đã chọn file: $ipaName")
    }

    Column(modifier = Modifier.fillMaxSize().padding(16.dp)) {
        Text("Cài đặt ứng dụng (.ipa) lên iPhone", style = androidx.compose.material3.MaterialTheme.typography.titleLarge)
        Spacer(Modifier.height(12.dp))

        Row(verticalAlignment = androidx.compose.ui.Alignment.CenterVertically) {
            Icon(Icons.Filled.Usb, contentDescription = null, tint = if (usbConnected) com.superalpha.sideload.ui.theme.BrandAccent else com.superalpha.sideload.ui.theme.BrandTextDim)
            Spacer(Modifier.height(0.dp))
            Text(
                text = if (usbConnected) "Đã kết nối iPhone qua USB" else "Chưa kết nối USB",
                modifier = Modifier.padding(start = 8.dp)
            )
            Spacer(Modifier.weight(1f))
            TextButton(onClick = {
                UsbPermissionManager.requestAndOpen(context) { ok, msg -> NativeLog.log(msg) }
            }) { Text("Kết nối") }
        }

        Spacer(Modifier.height(16.dp))
        Button(onClick = { pickIpaLauncher.launch(arrayOf("application/octet-stream", "*/*")) }, modifier = Modifier.fillMaxWidth()) {
            Text(ipaName)
        }

        Spacer(Modifier.height(16.dp))
        OutlinedTextField(
            value = appleId, onValueChange = { appleId = it },
            label = { Text("Apple ID") }, modifier = Modifier.fillMaxWidth()
        )
        Spacer(Modifier.height(8.dp))
        OutlinedTextField(
            value = password, onValueChange = { password = it },
            label = { Text("Mật khẩu Apple ID") },
            visualTransformation = PasswordVisualTransformation(),
            modifier = Modifier.fillMaxWidth()
        )

        Spacer(Modifier.height(16.dp))
        Row(modifier = Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween) {
            Button(
                enabled = !busy && ipaPath != null && appleId.isNotBlank() && password.isNotBlank(),
                onClick = {
                    val path = ipaPath ?: return@Button
                    viewModel.setBusy(true)
                    scope.launch {
                        NativeLog.log("Bắt đầu quá trình ký & cài đặt...")
                        val outcome = PythonBridge.sideload(
                            path, appleId, password, null,
                            savedAnisetteUrl.ifBlank { null }
                        )
                        if (!outcome.success && outcome.message.isNotBlank()) {
                            NativeLog.log("Lỗi: ${outcome.message}")
                        }
                        viewModel.setBusy(false)
                    }
                }
            ) {
                if (busy) {
                    CircularProgressIndicator(modifier = Modifier.height(16.dp), strokeWidth = 2.dp)
                } else {
                    Text("Ký & Cài đặt")
                }
            }
            TextButton(onClick = { viewModel.clearLog() }) { Text("Xoá log") }
        }

        Spacer(Modifier.height(12.dp))
        Text("Nhật ký:", style = androidx.compose.material3.MaterialTheme.typography.labelLarge)
        Spacer(Modifier.height(4.dp))
        LogConsole(lines = logLines, modifier = Modifier.weight(1f))
    }
}
