# Sideloadtool — Nhật ký sửa lỗi v5

> **Ngày:** 2026-07-13  
> **Phiên bản trước:** v4 (xem BUGFIX_CHANGELOG_v4.md)  
> **Files thay đổi:** `mux_usb.py`, `device_link.py`

---

## Vấn đề người dùng báo cáo

1. **Popup "Trust This Computer" KHÔNG xuất hiện trên màn hình iPhone** — app chạy đến bước pairing nhưng iPhone không hề hiện hộp thoại gì.
2. **Quá trình kết nối/ghép nối bị treo** — log dừng lại sau dòng `[mux] Thiết bị chấp nhận phiên bản usbmux 2.0`, tiếp theo là `[mux] Đã thiết lập kênh logic tới cổng thiết bị 62078`, rồi timeout 30 giây với lỗi `Timeout khi chờ dữ liệu từ thiết bị (mux).`

---

## Phân tích nguyên nhân gốc rễ

### Bug 1 — CRITICAL: Thiếu `QueryType` handshake trước `GetValue` (nguyên nhân chính của cả 2 vấn đề)

**File:** `device_link.py`, lớp `LockdownClient.__init__()`

**Vấn đề:** Giao thức lockdownd yêu cầu client phải gửi `QueryType` làm lệnh **đầu tiên** ngay sau khi TCP kết nối được thiết lập. Đây là "ritual" nhận diện client bắt buộc:

- **libimobiledevice** `lockdown.c`: hàm `lockdownd_client_new_with_handshake()` luôn gọi `lockdownd_query_type()` trước bất kỳ lệnh nào.
- **pymobiledevice3** `lockdown.py`: `LockdownClient.__init__()` luôn gọi `self.query_type()` ngay sau khi kết nối.

Trên iOS 14+, nếu không có `QueryType` đầu tiên, lockdownd sẽ **im lặng bỏ qua** các yêu cầu `GetValue` và `Pair` (không trả lỗi, không gửi phản hồi). Kết quả:
- `GetValue(DevicePublicKey)` không bao giờ nhận được phản hồi → timeout 30s
- Không có `DevicePublicKey` → không thể tạo cert → không thể gửi `Pair` → popup Trust không bao giờ xuất hiện trên iPhone

**Fix:** Thêm `self._request_raw({"Request": "QueryType", ...})` vào cuối `LockdownClient.__init__()`, ngay sau khi TCP kết nối thành công. Dùng `_request_raw()` (method mới, không kiểm tra trường Error) để tránh vòng lặp đệ quy. Exception từ QueryType được bắt im lặng vì thiết bị iOS rất cũ (iOS < 5) không có lệnh này.

### Bug 2 — IMPORTANT: `GetValue(DevicePublicKey)` không có retry

**File:** `device_link.py`, hàm `pair_device()`

**Vấn đề:** Nếu `get_value(key="DevicePublicKey")` thất bại (timeout, LockdownError), hàm raise ngay lập tức mà không thử lại. Sau khi fix Bug 1 (QueryType), GetValue sẽ thành công ngay. Nhưng thêm retry làm lưới an toàn để xử lý trường hợp iPhone đang khởi động lại lockdownd nội bộ.

**Fix:** Wrap `get_value()` trong vòng lặp retry 2 lần với backoff 1s. Thêm thông báo lỗi chi tiết hơn khi thất bại hoàn toàn (màn hình bị khoá, cáp kém, thiết bị bị disable).

### Bug 3 — PUMP THREAD: Timeout quá ngắn (5s → 10s)

**File:** `mux_usb.py`, hằng số `_PUMP_READ_TIMEOUT_S`

**Vấn đề:** Pump thread gọi `_recv_raw(timeout_s=5.0)`. Nếu device phản hồi chậm (đặc biệt khi đang xử lý Trust dialog hoặc tạo cert), read_exact() timeout sau 5s, pump thread reset và thử lại từ đầu — trong khi partial data vẫn còn trong `_rx_buffer`, điều này an toàn nhưng tạo log lỗi oan và tăng `_consecutive_no_data` không cần thiết.

**Fix:** Tăng `_PUMP_READ_TIMEOUT_S = 10.0`. Cũng thêm logic: chỉ tăng counter `_consecutive_no_data` khi `_rx_buffer` thực sự trống (không có partial data đang được đọc dở).

### Bug 4 — PUMP THREAD: Counter consecutive errors không reset đúng

**File:** `mux_usb.py`, hàm `_pump_loop()`

**Vấn đề:** `_consecutive_no_data` tăng cả khi pump thread nhận được partial data (data đang đến chậm theo từng chunk nhỏ). Nếu device gửi response theo nhiều USB bulk transfer riêng lẻ với khoảng cách > 5s mỗi cái, counter có thể đạt ngưỡng 12 và pump thread sẽ thông báo "thiết bị bị rút" oan.

**Fix:** Reset counter = 0 khi nhận được bất kỳ packet hoàn chỉnh nào; không tăng counter nếu `_io._rx_buffer` không trống.

### Bug 5 — PUMP THREAD: Race condition nhỏ sau SETUP packet

**File:** `mux_usb.py`, hàm `start()`

**Vấn đề:** Sau khi gửi SETUP packet, pump thread được khởi động ngay lập tức. Trên một số thiết bị phản hồi rất nhanh, device có thể gửi dữ liệu ngay sau SETUP trước khi pump thread kịp gọi `_recv_raw()` lần đầu tiên. Dữ liệu này sẽ được giữ trong USB hardware buffer và được đọc ở lần sau, nên đây không phải lỗi mất data — nhưng thêm `time.sleep(0.05)` làm rõ ràng hơn thứ tự khởi tạo.

**Fix:** Thêm `time.sleep(0.05)` sau khi gửi SETUP, trước khi start pump thread.

---

## Tóm tắt thay đổi

| File | Thay đổi |
|------|----------|
| `device_link.py` | Thêm `_request_raw()` method; thêm `QueryType` call trong `__init__`; retry GetValue 2 lần; thông báo lỗi chi tiết hơn |
| `mux_usb.py` | Tăng pump timeout từ 5s lên 10s; sửa logic counter consecutive errors; thêm sleep 50ms sau SETUP; thêm log CONTROL packet; cải thiện log diagnostic |

---

## Kết quả kỳ vọng sau khi áp dụng fix

```
[mux] Bắt tay phiên bản usbmux qua USB (header ngắn 8 byte)...
[mux] Thiết bị chấp nhận phiên bản usbmux 2.0.
[mux] Pump thread đã khởi động.
[lockdown] Đang mở kết nối lockdownd (cổng 62078)...
[mux] Mở kênh logic: cổng host 40000 -> cổng thiết bị 62078 (gửi SYN)...
[mux] ✅ Đã thiết lập kênh logic tới cổng thiết bị 62078.
[lockdown] ✅ Đã kết nối lockdownd.
[lockdown] QueryType OK — dịch vụ: com.apple.mobile.lockdown   ← MỚI (v5 fix)
[pairing] Đang lấy DevicePublicKey từ lockdownd...
[pairing] Đã có DevicePublicKey — đang tạo chuỗi chứng chỉ host...
[pairing] Đang gửi yêu cầu ghép nối...
[pairing] *** Kiểm tra màn hình iPhone — bấm 'Tin cậy' (Trust This Computer) ***
[pairing] Thiết bị đang hiện hộp thoại Trust — đang chờ bạn bấm...
[pairing] ✅ Ghép nối thành công.
```

---

## Lịch sử các bản fix trước

- **v4:** Sửa header 8/16 byte, SETUP packet, window scaling >>8, ZLP trong bulkWrite, PEM vs DER cho cert, thêm trường DeviceCertificate vào PairRecord — xem BUGFIX_CHANGELOG_v4.md
