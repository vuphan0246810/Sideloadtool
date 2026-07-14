package com.superalpha.sideload.ui

import android.app.Application
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import com.superalpha.sideload.bridge.AppConfig
import com.superalpha.sideload.bridge.NativeBridge
import com.superalpha.sideload.bridge.NativeLog
import com.superalpha.sideload.bridge.UsbTransport
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.launch

/**
 * ĐÃ SỬA: Dùng NativeBridge (C JNI) + AppConfig thay vì PythonBridge.
 */
class HomeViewModel(app: Application) : AndroidViewModel(app) {
    val nativeBridge = NativeBridge(app)
    private val _log = MutableStateFlow<List<String>>(emptyList())
    val log: StateFlow<List<String>> = _log
    private val _usbConnected = MutableStateFlow(false)
    val usbConnected: StateFlow<Boolean> = _usbConnected
    private val _busy = MutableStateFlow(false)
    val busy: StateFlow<Boolean> = _busy
    private val _savedAppleId = MutableStateFlow(AppConfig.appleId)
    val savedAppleId: StateFlow<String> = _savedAppleId
    private val _savedAnisetteUrl = MutableStateFlow(AppConfig.anisetteUrl)
    val savedAnisetteUrl: StateFlow<String> = _savedAnisetteUrl

    val anisetteServers = AppConfig.defaultAnisetteServers
    val trustRequired = NativeBridge.trustRequired

    init {
        nativeBridge.init()
        viewModelScope.launch {
            NativeLog.lines.collect { line ->
                _log.value = (_log.value + line).takeLast(500)
            }
        }
        viewModelScope.launch {
            UsbTransport.connected.collect { _usbConnected.value = it }
        }
    }

    fun setBusy(v: Boolean) { _busy.value = v }
    fun clearLog() { _log.value = emptyList() }
    fun saveAppleId(v: String) { _savedAppleId.value = v; AppConfig.appleId = v }
    fun saveAnisetteUrl(v: String) { _savedAnisetteUrl.value = v; AppConfig.anisetteUrl = v }
    fun dismissTrust() = NativeBridge.dismissTrust()

    fun onUsbReady() {
        if (_busy.value) return
        _busy.value = true
        viewModelScope.launch {
            val ok = nativeBridge.connect()
            if (ok) {
                val udid = nativeBridge.getUdid()
                if (udid != null) { AppConfig.lastUdid = udid; NativeLog.emit("[device] UDID: $udid") }
            }
            _busy.value = false
        }
    }

    override fun onCleared() { super.onCleared(); nativeBridge.reset() }
}
