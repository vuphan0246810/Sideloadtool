# Sideloadtool — Nhật ký sửa lỗi v6

> **Ngày:** 2026-07-13  
> **Files thay đổi:** `mux_usb.py`, `device_link.py`

---

## Vấn đề người dùng báo cáo

1. **Popup "Trust This Computer" KHÔNG xuất hiện** — sau khi `[lockdown] ✅ Đã kết nối lockdownd`, app bị treo 30s rồi báo timeout.
2. **Log kết thúc tại:** `[mux] Thiết bị gửi RST tới cổng host 40000 — kết nối bị từ chối/đóng` → `[lockdown] Cảnh báo QueryType: Timeout khi chờ dữ liệu từ thiết bị (mux).`

---

## Phân tích nguyên nhân gốc rễ

### Bug 1 — CRITICAL: iOS 16+ yêu cầu SSL TRƯỚC QueryType

**File:** `device_link.py`

**Vấn đề cốt lõi:** Apple thay đổi giao thức lockdownd từ iOS 16+:
- iOS < 16: Kết nối plaintext (plist text) → QueryType → GetValue → Pair → Trust popup ✅
- iOS 16+: **SSL/TLS bắt buộc ngay sau khi TCP kết nối**, trước mọi lệnh plist. Nếu nhận plaintext, lockdownd gửi **RST ngay lập tức**.

Đây là lý do chính xác:
1. TCP kết nối thành công (SYN → SYN-ACK → ACK)
2. App gửi QueryType dạng plaintext (bản cũ)
3. lockdownd iOS 16+ thấy plaintext, gửi **RST** (từ chối)
4. Pump thread nhận RST, in log, đặt `_closed=True`
5. `recv()` chờ queue 10s (QueryType timeout) → in "Cảnh báo QueryType"
6. `pair_device()` → new `LockdownClient()` → lại kết nối → lại RST → chờ 30s
7. **Không bao giờ gửi được Pair request → không có Trust popup**

**Fix:** Thêm `_SslPipe` class (SSL qua MemoryBIO giống `TlsLockdownClient`) áp dụng ngay sau TCP connect. Thêm `_open_lockdown()` factory tự động:
1. Thử plaintext trước (iOS < 16)
2. Nếu nhận `LockdownRstError` → retry ngay với SSL (iOS 16+)

### Bug 2 — CRITICAL: RST không unblock `recv()` ngay lập tức

**File:** `mux_usb.py`, `_on_segment()` và `recv()`

**Vấn đề:** Khi RST đến:
- `_closed = True` và `_connected = True` được set
- Nhưng `_rx_queue` KHÔNG nhận sentinel `b""` nào
- `recv()` gọi `_rx_queue.get(timeout=slice)` → chờ đến hết `slice_timeout` (10s)
- Sau 10s, `waited += 10`, in log, loop tiếp → chờ tiếp đến `deadline`
- Kết quả: mỗi lần bị RST, `recv()` luôn tốn ĐÚNG BẰNG timeout của nó (10s hoặc 30s)

**Fix:**
1. Trong `_on_segment(RST)`: thêm `self._rx_queue.put(b"")` để unblock `recv()` ngay
2. Trong `recv()`: khi nhận `b""` và `_closed.is_set()`, raise `MuxRstError` ngay thay vì `continue`
3. Trong `recv()`: check `_closed.is_set() and _rx_queue.empty()` trước khi `_rx_queue.get()` để fast-fail

### Bug 3 — Không phân biệt được lỗi RST với các lỗi khác

**File:** `mux_usb.py`, `device_link.py`

**Fix:** Thêm `MuxRstError(MuxError)` trong `mux_usb.py` và `LockdownRstError(LockdownError)` trong `device_link.py` để `_open_lockdown()` biết khi nào cần retry với SSL thay vì báo lỗi chung.

---

## Luồng sau khi fix

### iOS < 16 (không thay đổi):
```
[lockdown] Đang mở kết nối lockdownd (cổng 62078)...
[mux] Mở kênh logic: cổng host 40000 -> cổng thiết bị 62078 (gửi SYN)...
[mux] ✅ Đã thiết lập kênh logic tới cổng thiết bị 62078.
[lockdown] ✅ Đã kết nối lockdownd.
[lockdown] QueryType OK — dịch vụ: com.apple.mobile.lockdown
[pairing] Đang lấy DevicePublicKey từ lockdownd...
[pairing] Đang gửi yêu cầu ghép nối...
[pairing] *** Kiểm tra màn hình iPhone — bấm 'Tin cậy' ***
[pairing] Thiết bị đang hiện hộp thoại Trust — đang chờ bạn bấm...
[pairing] ✅ Ghép nối thành công.
```

### iOS 16+ (SSL tự động):
```
[lockdown] Đang mở kết nối lockdownd (cổng 62078)...
[mux] ✅ Đã thiết lập kênh logic tới cổng thiết bị 62078.
[lockdown] ✅ Đã kết nối lockdownd.
[mux] Thiết bị gửi RST tới cổng host 40000   ← RST ngay (plaintext bị từ chối)
[lockdown] Plaintext bị RST — thử lại với SSL (iOS 16+ mode)...
[lockdown] Đang mở kết nối lockdownd (SSL/iOS 16+) (cổng 62078)...
[lockdown] ✅ Đã kết nối lockdownd (SSL/iOS 16+).
[lockdown] Đang thực hiện TLS handshake với lockdownd (iOS 16+)...
[lockdown] ✅ TLS handshake với lockdownd thành công (iOS 16+ mode).
[lockdown] QueryType OK — dịch vụ: com.apple.mobile.lockdown
[pairing] Đang lấy DevicePublicKey từ lockdownd...
[pairing] *** Kiểm tra màn hình iPhone — bấm 'Tin cậy' ***
[pairing] ✅ Ghép nối thành công.
```


---

## Tóm tắt thay đổi

| File | Thay đổi |
|------|----------|
| `mux_usb.py` | Thêm `MuxRstError`; RST → `_rx_queue.put(b"")` để fast-fail `recv()`; `recv()` raise `MuxRstError` khi nhận sentinel |
| `device_link.py` | Thêm `LockdownRstError`, `_SslPipe`, `_open_lockdown()` với SSL fallback; `LockdownClient(use_ssl=True)` mode; tất cả callers dùng `_open_lockdown()` |
