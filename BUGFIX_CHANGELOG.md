# Changelog — Lần 2: Sửa triệt để lỗi Trust popup

## Tóm tắt nhanh

| # | File | Nghiêm trọng | Mô tả |
|---|------|--------------|--------|
| 1 | mux_usb.py | 🔴 CHÍNH | seq=0, SYN+1, FIN+1 — TCP sequence đúng |
| 2 | device_link.py | 🔴 CHÍNH | DER cert thay PEM trong PairRecord gửi thiết bị |
| 3 | **mux_usb.py** | 🔴 **MỚI** | **MuxDevice singleton không reset giữa các lần chạy** |
| 4 | **sideload_core.py** | 🔴 **MỚI** | **Pair record cũ/hỏng không được kiểm tra → bỏ qua pairing** |
| 5 | device_link.py | 🟠 MỚI | Timeout `_request` 15s → 30s |
| 6 | device_link.py | 🟠 MỚI | `PasswordProtected` chưa xử lý (iPhone đang khoá) |
| 7 | device_link.py | 🟠 MỚI | DevicePublicKey: chỉ thử PEM, không thử DER nếu PEM lỗi |
| 8 | mux_usb.py | 🟡 MỚI | Pump loop không unblock recv() khi USB bị rút |
| 9 | sideload_core.py | 🟡 MỚI | Thiếu hàm `delete_pair_record()` để force re-pair |
| 10 | device_link.py | 🟡 C1 | PairingDialogResponsePending retry (iOS 13+) |
| 11 | device_link.py | 🟡 C1 | 60s timeout chờ Trust response |

---

## Chi tiết các lỗi MỚI (lần này)

---

### 🔴 Lỗi 3 (NGUYÊN NHÂN CHÍNH của lần 2): MuxDevice singleton không reset

**File:** `mux_usb.py` → `sideload_core.py`

**Triệu chứng:** Sau khi sideload thất bại lần đầu (hoặc iPhone rút-cắm lại),
bấm "Sideload" lần 2 → Trust popup không xuất hiện, mọi request đều timeout.

**Nguyên nhân kỹ thuật:**
- `MuxDevice` là **singleton toàn cục** trong Python process.
- Lần chạy đầu: singleton được tạo, `start()` thực hiện **version handshake**,
  pump thread bắt đầu chạy → trạng thái `_version=2`.
- Lần chạy thứ 2 (kể cả sau khi rút/cắm lại cáp USB):
  - `get_device()` trả về **singleton cũ** (không tạo lại).
  - Pump thread từ lần 1 vẫn đang chạy, đang consume dữ liệu USB.
  - Thiết bị vừa reconnect → cần **version handshake mới**.
  - Code nhảy thẳng vào gửi SYN với **16-byte full header** (`_version=2`)
    trong khi thiết bị đang chờ **8-byte short header** của version handshake.
  - → Giao thức lệch ngay từ byte đầu tiên → mọi request bị thiết bị bỏ qua.

**Sửa (`sideload_core.py`):**
```python
# Gọi đầu tiên trong do_sideload() trước mọi thứ khác
device_link.reset_mux_device()   # ← reset singleton, dừng pump thread cũ
```

**Sửa (`device_link.py`):**
```python
def reset_mux_device():
    from mux_usb import reset_device as _mux_reset
    _mux_reset()
```

---

### 🔴 Lỗi 4 (NGUYÊN NHÂN CHÍNH của lần 2): Pair record hỏng bỏ qua pairing

**File:** `sideload_core.py`

**Triệu chứng:** Trust popup không xuất hiện ngay cả khi iPhone kết nối lần đầu.

**Nguyên nhân kỹ thuật:**
- Nếu một lần ghép nối trước đó **bắt đầu thành công nhưng không hoàn thành**
  (ví dụ: iOS trả `{"Result": "Success"}` không có `EscrowBag`, hoặc app crash
  sau khi ghi file), một `pair_record.plist` **không hợp lệ** vẫn được lưu.
- `_get_or_create_pair_record()` **load và trả về ngay** mà không kiểm tra.
- → `pair_device()` không bao giờ được gọi → Trust popup không bao giờ xuất hiện.

**Sửa:**
```python
def _get_or_create_pair_record(udid):
    record = _load_pair_record()
    if record:
        if device_link.validate_pair_record(record):   # ← KIỂM TRA trước khi dùng
            return record
        delete_pair_record()   # ← XÓA nếu hỏng
    # ... gọi pair_device() bình thường
```

`validate_pair_record()` kiểm tra: `HostID`, `HostCertificate`, `HostPrivateKey`,
`RootCertificate`, `RootPrivateKey`, `SystemBUID` đều có mặt và không rỗng,
**VÀ** `EscrowBag` tồn tại (bắt buộc cho iOS 7+).

---

### 🟠 Lỗi 5: `_request` timeout cứng 15s

**Sửa:** `def _request(self, request: dict, timeout: float = 30.0)` — mỗi `GetValue`
request bây giờ chờ 30s thay vì 15s.

---

### 🟠 Lỗi 6: `PasswordProtected` không được xử lý

**Triệu chứng:** iPhone đang khoá bằng mã PIN → lockdownd trả `PasswordProtected`
→ app báo lỗi chung chung "Pairing thất bại", không hướng dẫn người dùng.

**Sửa:** Thêm case `PasswordProtected` với thông báo rõ ràng bằng tiếng Việt.

---

### 🟠 Lỗi 7: DevicePublicKey không có DER fallback

Nếu thiết bị trả DER thay vì PEM, `load_pem_public_key()` ném ValueError và
toàn bộ pairing crash ngay. **Sửa:** thử PEM trước, nếu lỗi thử DER.

---

### 🟡 Lỗi 8: Pump loop không unblock recv() khi USB rút

**Sửa:** Sau 20 lần timeout liên tiếp (100s), pump loop đóng tất cả kết nối
đang mở để `recv()` trên main thread nhận được EOF thay vì chờ 60s.

---

### 🟡 Lỗi 9: Thiếu hàm `delete_pair_record()`

**Sửa:** Thêm hàm `delete_pair_record()` trong `sideload_core.py`. Gọi từ Kotlin
qua `PythonBridge` khi người dùng cần force re-pair:

```kotlin
// Thêm vào PythonBridge.kt nếu muốn có nút "Ghép nối lại iPhone" trong UI:
fun deletePairRecord(): Boolean = callPython("delete_pair_record")
```

---

## Hướng dẫn Debug khi Trust popup không xuất hiện

Kiểm tra log theo thứ tự:

1. **`[mux] Bắt tay phiên bản usbmux...`** → mux TCP handshake đang chạy
2. **`[mux] Thiết bị chấp nhận phiên bản usbmux 2.0`** → USB OK
3. **`[pairing] Bắt đầu ghép nối lần đầu...`** → pair_record.plist không tồn tại hoặc đã bị xóa
4. **`[pairing] Đang gửi yêu cầu ghép nối...`** → Pair request đã được gửi
5. **`[pairing] *** Kiểm tra màn hình iPhone ***`** → XEM MÀTN HÌNH IPHONE NGAY LÚC NÀY
6. Nếu thấy **`[pairing] ✅ Đã ghép nối thành công`** nhưng không thấy popup:
   → iPhone đã trust từ trước (profile còn hạn) — đây không phải lỗi!

Nếu thấy **`[pairing] ✅ Đã ghép nối trước đó — dùng lại pair record`**:
→ Ghép nối đã xong từ trước. Trust popup chỉ xuất hiện lần đầu tiên.
