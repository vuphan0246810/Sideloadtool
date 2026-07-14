# BUGFIX v10 — USB loop triệt để + Python log đầy đủ

## Lỗi 1: claimInterface() loop vẫn xảy ra (v9 fix không đủ)

### Root cause chính xác

Trong v9, `lastAttemptTime` được set lúc **BẮT ĐẦU** lần thử.
`UsbTransport.open()` thực hiện 5 lần retry với delay tăng dần:
```
150ms (pre-delay) + 200ms + 400ms + 600ms + 800ms = ~2.15 giây
```
Khi `finish(false)` được gọi, `elapsed = now - lastAttemptTime ≈ 2-3s`.
Cooldown 3s → `elapsed >= cooldown` → **cooldown bị bypass!**

### Fix trong `UsbPermissionManager.kt`

1. Đổi từ `lastAttemptTime` sang `lastFailTimestampMs` — chỉ ghi nhận thời điểm **THẤT BẠI**, không phải lúc bắt đầu.
2. Reset `lastFailTimestampMs = System.currentTimeMillis()` bên trong `finish(false)` — ngay khi lỗi được biết.
3. Tăng cooldown từ 3s → **8 giây** (buffer đủ lớn cho re-enumerate + event processing).
4. Emit log rõ ràng khi cooldown chặn: "Bỏ qua auto-connect: cooldown 8s sau thất bại (còn Xs)."

Sequence mới:
```
T=0:   ATTACHED → requestAndOpen() → lastFailTimestamp=0 → elapsed=∞ → proceed
T=0:   open() bắt đầu, retry 5 lần (~2s)
T=2:   claimInterface fail hoàn toàn → conn.close()
T=2:   finish(false) → lastFailTimestampMs = T=2  ← FIX
T=2:   conn.close() trigger re-enumerate → ATTACHED lại
T=3:   ATTACHED → requestAndOpen(fromAutoAttach=true)
T=3:   elapsed = T3 - T2 = 1s < 8s → RETURN (loop bị phá!) ✓
T=10:  elapsed = 8s → cho phép thử lại nếu user vẫn cắm dây
```

## Lỗi 2: Python log không hiện đầy đủ

### Root cause

`NativeLog.emit()` và `NativeLog.log(String)` (1 tham số) **thiếu `@JvmStatic`**.

Với Kotlin `object`, không có `@JvmStatic` → method chỉ tồn tại trên `NativeLog.INSTANCE` (instance method), không phải static method trên class. Chaquopy tìm kiếm static method signature → `NoSuchMethodError` → `_bridged_print` bắt ngoại lệ và bỏ qua silently → **không có log nào từ Python xuất hiện trên UI**.

### Fix 1: `NativeLog.kt`
Thêm `@JvmStatic` vào cả `emit()`, `log(String)`, và `log(String, String)`.

### Fix 2: `sideload_core.py`
Đổi `NativeLog.log(text)` → `NativeLog.log("python", text)` (2-arg static version).
Thêm redirect `sys.stderr` qua `_StderrBridge` → `NativeLog.log("python-err", line)`.
Kết quả: Python traceback (nhiều dòng) hiện đầy đủ trong LogConsole UI.

### Fix 3: `PythonBridge.kt`
Trong catch block, emit từng dòng của exception message riêng biệt:
```kotlin
full.lines().forEach { line ->
    if (line.isNotBlank()) NativeLog.emit("$prefix $line")
}
```
Chaquopy exception message chứa Python traceback nhiều dòng — phát từng dòng để LogConsole không bị cụt.

## Files changed (v10)

| File | Thay đổi |
|------|----------|
| `NativeLog.kt` | Thêm `@JvmStatic` vào `emit()`, `log(String)`, `log(String,String)` |
| `UsbPermissionManager.kt` | Cooldown từ failure timestamp, 8s, log khi bị chặn |
| `sideload_core.py` | `NativeLog.log("python",text)`, thêm `_StderrBridge` cho sys.stderr, full `do_sideload()` |
| `PythonBridge.kt` | Emit exception line-by-line |
| `UsbTransport.kt` | (giữ từ v9) delay 150ms + 5 retry |
| `MainActivity.kt` | (giữ từ v9) fromAutoAttach=true |
| `DeviceNative.kt` | (giữ từ v9) check isConnected() trước native call |
| `SideloadScreen.kt` | (giữ từ v9) disable button khi chưa có USB |
| `python-config.gradle` | (giữ từ v8) Python 3.11 |
