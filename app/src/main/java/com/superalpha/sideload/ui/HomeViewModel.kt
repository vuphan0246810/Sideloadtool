package com.superalpha.sideload.ui

import android.app.Application
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import com.superalpha.sideload.bridge.NativeLog
import com.superalpha.sideload.bridge.UsbTransport
import com.superalpha.sideload.python.PythonBridge
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.launch

/** Shared state across all three screens: the running log console, USB status, and
 * the Settings-managed Apple ID / Anisette server choice (loaded once here so
 * switching to the Settings tab repeatedly does not re-fetch the server list from
 * the network every time — see SettingsScreen.kt). */
class HomeViewModel(app: Application) : AndroidViewModel(app) {
    private val _log = MutableStateFlow<List<String>>(emptyList())
    val log: StateFlow<List<String>> = _log

    private val _usbConnected = MutableStateFlow(false)
    val usbConnected: StateFlow<Boolean> = _usbConnected

    private val _busy = MutableStateFlow(false)
    val busy: StateFlow<Boolean> = _busy

    private val _savedAppleId = MutableStateFlow("")
    val savedAppleId: StateFlow<String> = _savedAppleId

    private val _savedAnisetteUrl = MutableStateFlow("")
    val savedAnisetteUrl: StateFlow<String> = _savedAnisetteUrl

    private val _anisetteServers = MutableStateFlow<List<PythonBridge.AnisetteServer>>(emptyList())
    val anisetteServers: StateFlow<List<PythonBridge.AnisetteServer>> = _anisetteServers

    private val _anisetteServersLoading = MutableStateFlow(false)
    val anisetteServersLoading: StateFlow<Boolean> = _anisetteServersLoading

    init {
        viewModelScope.launch {
            NativeLog.lines.collect { line ->
                _log.value = (_log.value + line).takeLast(500)
            }
        }
        viewModelScope.launch {
            UsbTransport.connected.collect { _usbConnected.value = it }
        }
        viewModelScope.launch {
            _savedAppleId.value = PythonBridge.getSavedAppleId()
            _savedAnisetteUrl.value = PythonBridge.getSavedAnisetteUrl()
        }
    }

    fun setBusy(value: Boolean) {
        _busy.value = value
    }

    fun clearLog() {
        _log.value = emptyList()
    }

    fun saveAppleId(appleId: String) {
        _savedAppleId.value = appleId
        viewModelScope.launch { PythonBridge.saveAppleId(appleId) }
    }

    fun saveAnisetteUrl(url: String) {
        _savedAnisetteUrl.value = url
        viewModelScope.launch { PythonBridge.saveAnisetteUrl(url) }
    }

    /** Fetches the public Anisette server list once and caches it here; call again
     * via [reloadAnisetteServers] to force a refresh. Safe to call every time
     * SettingsScreen is (re)composed — it no-ops once a non-empty list is cached. */
    fun loadAnisetteServersIfNeeded() {
        if (_anisetteServers.value.isNotEmpty() || _anisetteServersLoading.value) return
        reloadAnisetteServers()
    }

    fun reloadAnisetteServers() {
        _anisetteServersLoading.value = true
        viewModelScope.launch {
            _anisetteServers.value = PythonBridge.listAnisetteServers()
            _anisetteServersLoading.value = false
        }
    }
}
