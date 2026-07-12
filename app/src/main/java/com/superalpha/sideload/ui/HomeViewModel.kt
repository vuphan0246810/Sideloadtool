package com.superalpha.sideload.ui

import android.app.Application
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import com.superalpha.sideload.bridge.NativeLog
import com.superalpha.sideload.bridge.UsbTransport
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.launch

/** Shared state across all three screens: the running log console and USB status. */
class HomeViewModel(app: Application) : AndroidViewModel(app) {
    private val _log = MutableStateFlow<List<String>>(emptyList())
    val log: StateFlow<List<String>> = _log

    private val _usbConnected = MutableStateFlow(false)
    val usbConnected: StateFlow<Boolean> = _usbConnected

    private val _busy = MutableStateFlow(false)
    val busy: StateFlow<Boolean> = _busy

    init {
        viewModelScope.launch {
            NativeLog.lines.collect { line ->
                _log.value = (_log.value + line).takeLast(500)
            }
        }
        viewModelScope.launch {
            UsbTransport.connected.collect { _usbConnected.value = it }
        }
    }

    fun setBusy(value: Boolean) {
        _busy.value = value
    }

    fun clearLog() {
        _log.value = emptyList()
    }
}
