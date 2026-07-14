package com.superalpha.sideload.ui

import android.app.Application
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import com.superalpha.sideload.bridge.AppConfig
import com.superalpha.sideload.bridge.NativeBridge
import com.superalpha.sideload.bridge.NativeLog
import com.superalpha.sideload.bridge.UsbTransport
import com.superalpha.sideload.python.PythonBridge
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.launch

/**
 * ĐÃ SỬA: Dùng NativeBridge (C JNI) + AppConfig thay vì PythonBridge cho phần
 * kết nối/sideload trực tiếp qua USB. Danh sách server Anisette (dùng ở
 * SettingsScreen) vẫn lấy qua PythonBridge.listAnisetteServers() — hàm đó giờ
 * gọi thẳng OkHttp tới servers.sidestore.io thay vì Python, và được load một
 * lần rồi cache ở đây để chuyển qua lại tab Cài đặt không phải tải lại mạng.
 *
 * BUGFIX v17:
 *   - Thêm emitLog() để PairingScreen.kt có thể ghi log USB-open lên UI.
 *   - connectAndPair() vẫn an toàn gọi khi USB đã mở (PairingScreen gọi sau
 *     khi requestAndOpen() thành công) hoặc trực tiếp (USB đã open sẵn).
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

    private val _anisetteServers = MutableStateFlow<List<PythonBridge.AnisetteServer>>(emptyList())
    val anisetteServers: StateFlow<List<PythonBridge.AnisetteServer>> = _anisetteServers
    private val _anisetteServersLoading = MutableStateFlow(false)
    val anisetteServersLoading: StateFlow<Boolean> = _anisetteServersLoading

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

    /**
     * emitLog — ghi thẳng một dòng log vào UI log console.
     *
     * BUGFIX v17: PairingScreen cần ghi kết quả USB-open (thành công/thất bại)
     * lên console trước khi gọi connectAndPair(). NativeLog.emit() là cách
     * chính xác vì HomeViewModel subscribe vào NativeLog.lines để đổ vào _log.
     */
    fun emitLog(line: String) = NativeLog.emit(line)

    /** Tải danh sách server Anisette công khai một lần rồi cache lại; gọi
     * [reloadAnisetteServers] để buộc tải lại. Gọi an toàn ở mỗi lần
     * SettingsScreen recompose — không làm gì nếu đã có danh sách. */
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

    // ── Tab "Ghép nối" (Pairing) ────────────────────────────────────────────
    private val _isPaired = MutableStateFlow(false)
    val isPaired: StateFlow<Boolean> = _isPaired

    /**
     * connectAndPair — thực hiện toàn bộ luồng bắt tay usbmux + pairing native.
     *
     * Giả định USB ĐÃ ĐƯỢC MỞ trước khi gọi (UsbTransport.isConnected() == true).
     * PairingScreen.kt gọi hàm này CHỈ SAU KHI:
     *   - USB đã kết nối (usbConnected == true), HOẶC
     *   - UsbPermissionManager.requestAndOpen() thành công.
     *
     * nativeBridge.connect() gọi mux_conn_init() + mux_do_setup() trong C.
     * Với BUGFIX v17 (read-ahead buffer), mux_do_setup() sẽ không còn mất
     * 12 bytes của VERSION response và SETUP thất bại.
     */
    fun connectAndPair() {
        if (_busy.value) return
        _busy.value = true
        viewModelScope.launch {
            val connected = nativeBridge.connect()
            if (!connected) {
                NativeLog.emit("[pairing] ❌ Không kết nối được USB — kiểm tra cáp/quyền USB.")
                _busy.value = false
                return@launch
            }
            nativeBridge.getUdid()?.let { AppConfig.lastUdid = it }
            val paired = nativeBridge.pair()
            _isPaired.value = paired
            if (paired) {
                NativeLog.emit("[pairing] ✅ Ghép nối với iPhone thành công.")
            } else {
                NativeLog.emit("[pairing] ❌ Ghép nối thất bại — kiểm tra log/Trust popup trên iPhone.")
            }
            _busy.value = false
        }
    }

    /** Xuất pair record hiện tại thành file .plist. Trả kết quả qua callback
     * vì Compose cần File để mở share sheet ngay khi có, không qua StateFlow. */
    fun exportPairingFile(onResult: (java.io.File?) -> Unit) {
        viewModelScope.launch {
            onResult(nativeBridge.exportPairingFile())
        }
    }

    override fun onCleared() { super.onCleared(); nativeBridge.reset() }
}
