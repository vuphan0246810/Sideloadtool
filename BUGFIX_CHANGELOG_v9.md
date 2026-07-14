# BUGFIX v9 — USB claimInterface loop + Python không thấy USB

## Lỗi đã sửa

### 1. `claimInterface() thất bại` lặp vô tận (USB loop)

**Triệu chứng:** Log hiển thị liên tục:
```
Mở kết nối USB thất bại: claimInterface() thất bại
[usb] Thiết bị USB đã rút — bấm Kết nối để thử lại.
Đã phát hiện iPhone/iPad vừa cắm vào — đang tự động kết nối...
Mở kết nối USB thất bại: claimInterface() thất bại
...
```

**Root cause (2 tầng):**

a) **Android USB timing**: `claimInterface()` được gọi ngay sau `openDevice()` mà không có delay. Hệ điều hành cần ~150ms để giải phóng kernel driver và sẵn sàng cho `forceClaim=true`. Thiếu delay → `claimInterface()` luôn trả `false`.

b) **Vòng lặp re-enumerate**: Khi `claimInterface()` fail, ta gọi `conn.close()`. Một số phiên bản Android (đặc biệt Android 10+) sau đó re-enumerate thiết bị USB → gửi lại `ACTION_USB_DEVICE_ATTACHED` → `handleUsbAttachIntent()` → `requestAndOpen()` → fail → close → loop.

**Fixes:**

- **`UsbTransport.kt`**: Thêm `Thread.sleep(150)` trước lần `claimInterface()` đầu tiên. Thêm retry 5 lần với delay tăng dần (200ms → 400ms → 600ms → 800ms) trước khi bỏ cuộc.

- **`UsbPermissionManager.kt`**: Thêm cooldown 3 giây (`AUTO_CONNECT_COOLDOWN_MS = 3000`) cho các lần gọi auto-connect (`fromAutoAttach = true`). Nếu lần thử trước đó thất bại và chưa qua 3 giây, bỏ qua ngay — phá vòng lặp.

- **`MainActivity.kt`**: Truyền `fromAutoAttach = true` khi gọi từ `handleUsbAttachIntent()` (auto-connect khi cắm dây). Khi người dùng bấm "Kết nối" thủ công truyền `fromAutoAttach = false` (bỏ qua cooldown).

### 2. Python không thấy USB / báo lỗi mơ hồ

**Triệu chứng:** Bấm "Ký & Cài đặt" khi chưa kết nối USB → Python chạy một phần rồi fail không rõ lý do.

**Root cause:** Nút "Ký & Cài đặt" không kiểm tra `usbConnected`. Khi người dùng bấm mà USB chưa kết nối:
1. Python gọi `device_link.pair_device()` → `DeviceNative.connectAndPair()` → `NativeBridge.connect()` → native C gọi `usb_bulk_write/read` qua JNI
2. `UsbTransport.nativeBulkWrite/Read()` kiểm tra `connection` — `null` → trả `-1`
3. C code nhận `-1` → báo fail nhưng thông báo không rõ ràng

**Fixes:**

- **`SideloadScreen.kt`**: Thêm `usbConnected` vào điều kiện `enabled` của nút "Ký & Cài đặt". Hiển thị cảnh báo rõ ràng khi USB chưa kết nối: *"⚠ Vui lòng cắm cáp USB và bấm 'Kết nối' trước khi ký & cài đặt."*

- **`DeviceNative.kt`**: Thêm kiểm tra `UsbTransport.isConnected()` trong `connectAndPair()` và `sideloadIpa()`. Nếu USB chưa kết nối → emit thông báo rõ ràng bằng tiếng Việt và trả `false` ngay — không để native C fail mơ hồ.

## Files changed

- `app/src/main/java/com/superalpha/sideload/bridge/UsbTransport.kt` — retry claimInterface
- `app/src/main/java/com/superalpha/sideload/bridge/UsbPermissionManager.kt` — cooldown anti-loop
- `app/src/main/java/com/superalpha/sideload/MainActivity.kt` — fromAutoAttach flag
- `app/src/main/java/com/superalpha/sideload/bridge/DeviceNative.kt` — USB check before native
- `app/src/main/java/com/superalpha/sideload/ui/SideloadScreen.kt` — require USB for sideload button
- `app/python-config.gradle` — Python version 3.11 (fix từ v8.1)
