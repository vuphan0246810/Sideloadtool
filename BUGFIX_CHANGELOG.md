# Changelog — BUGFIX v17 (Sideloadtool)

## Vấn đề gốc rễ: "SETUP thất bại"

```
[mux] Gửi SETUP packet...
[mux] ❌ SETUP thất bại
[pairing] ❌ Không kết nối được USB — kiểm tra cáp/quyền USB.
```

USB hiển thị "kết nối xanh ✓" nhưng bắt tay usbmux SETUP vẫn thất bại.  
Nguyên nhân thực sự **KHÔNG phải** dây cáp hay quyền USB — mà là lỗi lập trình.

---

## Lỗi 1 (Nghiêm trọng nhất) — USB Read Fragmentation

**File:** `app/src/main/cpp/usbmux.c` + `usbmux.h`

### Nguyên nhân

`recv_packet()` cũ đọc USB theo **2 bước riêng biệt**:
1. Đọc 8 byte (lấy header để biết tổng độ dài)
2. Đọc `total − 8` byte (đọc phần còn lại của packet)

Nhưng iPhone gửi toàn bộ VERSION response (ví dụ 20 byte) như **một USB bulk transfer duy nhất**.  
Android `bulkTransfer(buf, 8, timeout)` chỉ trả về 8 byte và **CẮT BỎ 12 byte còn lại**  
— Android USB Host driver KHÔNG buffer phần dư khi buffer yêu cầu nhỏ hơn dữ liệu nhận được.  

Lần `usb_read` tiếp theo (đọc 12 byte body) gửi IN token mới tới iPhone, nhưng iPhone đã  
gửi xong → **timeout → `recv_packet` trả NULL → `mux_do_setup` thất bại → "SETUP thất bại"**.

Đây là cách `usbmuxd/src/device.c` chính thức (Apple's daemon trên macOS/Linux) hoạt động:  
nó định nghĩa `DEV_MRU = 65536` và đọc vào `pktbuf` tối đa 65536 byte mỗi lần, **không bao giờ đọc 2 bước**.

### Fix

Thêm **read-ahead buffer** vào `mux_conn_t`:
```c
uint8_t  rxbuf[65536];   // MUX_DEV_MRU — khớp DEV_MRU trong usbmuxd/src/device.c
int      rxbuf_used;
int      rxbuf_pos;
```

`buffered_read()` mới: khi buffer cạn, đọc tối đa `MUX_DEV_MRU` byte từ USB vào `rxbuf`  
trong **một lần gọi duy nhất**, rồi phục vụ các yêu cầu nhỏ hơn từ buffer đó.  
`recv_packet()` được sửa để dùng `buffered_read()` thay vì `read_all()` trực tiếp.

**Files thay đổi:**
- `app/src/main/cpp/usbmux.h` — thêm `rxbuf[65536]`, `rxbuf_used`, `rxbuf_pos`, `ui_log` vào `mux_conn_t`; thêm hằng `MUX_DEV_MRU 65536`
- `app/src/main/cpp/usbmux.c` — thêm `buffered_read()`; sửa `recv_packet()` dùng `buffered_read()`; sửa `mux_conn_init()` khởi tạo các trường mới

---

## Lỗi 2 — UI log mơ hồ, thiếu chi tiết

**File:** `app/src/main/cpp/jni_bridge.c` + `usbmux.h` + `usbmux.c`

### Nguyên nhân

`mux_do_setup()` chỉ ghi log vào Logcat (`LOGI/LOGE`) — không hiện trên UI.  
`jni_bridge.c` chỉ ghi `"[mux] Gửi SETUP packet..."` (sai — thực ra bước đầu là VERSION, không phải SETUP)  
và chỉ ghi `"[mux] ❌ SETUP thất bại"` khi thất bại — không biết bước nào bị lỗi.

### Fix

Thêm `void (*ui_log)(const char *msg)` callback vào `mux_conn_t`.  
`mux_do_setup()` gọi `UI_LOG()` macro để ghi log lên **cả Logcat và UI** cho từng bước:
- `[mux] Gửi VERSION packet (major=2 minor=0)...`
- `[mux] Đã gửi VERSION, đang chờ phản hồi từ iPhone...`
- `[mux] ✅ Thiết bị chấp nhận usbmux v2.0`
- `[mux] Gửi SETUP packet (protocol=2, payload=0x07)...`
- `[mux] ✅ SETUP hoàn tất (...)`

`jni_bridge.c` gắn `g_mux.ui_log = jni_log_ui_cb` sau `mux_conn_init()`.  
`jni_log_ui_cb()` là static function lấy `JNIEnv` từ `g_jvm` và gọi `NativeBridge.onNativeLog()`.

**Files thay đổi:**
- `app/src/main/cpp/usbmux.h` — thêm `void (*ui_log)(const char *msg)` vào `mux_conn_t`
- `app/src/main/cpp/usbmux.c` — thêm macro `UI_LOG(c, msg)`; sửa `mux_do_setup()` dùng `UI_LOG`
- `app/src/main/cpp/jni_bridge.c` — thêm `jni_log_ui_cb()`; gắn `g_mux.ui_log = jni_log_ui_cb` sau `mux_conn_init()`; sửa text log trong `nativeConnect()` cho chính xác

---

## Lỗi 3 — JNI local ref leak

**File:** `app/src/main/cpp/jni_bridge.c`

### Nguyên nhân

`usb_bulk_write()` và `usb_bulk_read()`:
```c
jmethodID mid = (*env)->GetStaticMethodID(env, cls, ...);
if (!mid) return -1;  // ← thiếu DeleteLocalRef(env, cls)!
```
`cls` local ref bị rò khi `GetStaticMethodID()` thất bại.

### Fix

Thêm `(*env)->DeleteLocalRef(env, cls)` trước `return -1` trong tất cả nhánh lỗi.  
Cũng thêm kiểm tra `NewByteArray()` trả NULL (OOM) với cleanup đầy đủ.

---

## Lỗi 4 — Nút "Kết nối & Ghép nối" không mở USB trước khi pair

**File:** `app/src/main/java/com/superalpha/sideload/ui/PairingScreen.kt`

### Nguyên nhân

Nút gọi `viewModel.connectAndPair()` trực tiếp. Nếu người dùng **mở app trước khi cắm cáp**  
rồi cắm cáp rồi bấm nút — `UsbTransport` chưa được mở (`endpointOut == null`).  
`usb_bulk_write()` trả `-1` ngay → VERSION packet không gửi được → "SETUP thất bại".

### Fix

Nút kiểm tra `usbConnected`:
- `usbConnected == true` → gọi `connectAndPair()` ngay
- `usbConnected == false` → gọi `UsbPermissionManager.requestAndOpen()` trước;  
  chỉ khi callback `ok == true` mới gọi `connectAndPair()`.  
  Người dùng thấy log rõ ràng về từng bước.

**Files thay đổi:**
- `app/src/main/java/com/superalpha/sideload/ui/PairingScreen.kt`
- `app/src/main/java/com/superalpha/sideload/ui/HomeViewModel.kt` — thêm `emitLog()` helper

---

## Bảng tóm tắt

| # | File | Mức độ | Fix |
|---|------|--------|-----|
| 1 | `usbmux.c` / `usbmux.h` | **CRITICAL** | Thêm read-ahead buffer 65536 byte, `buffered_read()` |
| 2 | `jni_bridge.c` / `usbmux.c` | HIGH | UI log callback `ui_log`; log chi tiết từng bước VERSION/SETUP |
| 3 | `jni_bridge.c` | MEDIUM | Fix JNI local ref leak trong `usb_bulk_write/read` |
| 4 | `PairingScreen.kt` / `HomeViewModel.kt` | MEDIUM | Mở USB trước khi pair nếu chưa kết nối |
