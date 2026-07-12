# Changelog — Sửa lỗi Trust popup + Mux timeout

## Các lỗi đã sửa

### 🔴 Lỗi 1 (NGUYÊN NHÂN CHÍNH): `MuxConnection.seq` sai — gây ĐỒNG THỜI cả hai triệu chứng

**File:** `app/src/main/python/mux_usb.py`

**Triệu chứng:** Sau khi bắt tay usbmux 2.0 thành công, app báo lỗi
`Timeout khi chờ dữ liệu từ thiết bị (mux)`. Trust popup không xuất hiện.

**Nguyên nhân kỹ thuật:**
- `MuxConnection.seq` được khởi tạo bằng `1` (thay vì `0` như usbmuxd gốc).
- Gói SYN được gửi với `seq=1`, nhưng `seq` **không được tăng** sau SYN.
- Thiết bị nhận SYN với seq=1, gửi lại SYN-ACK với `ack=2` (báo rằng nó
  chờ data bắt đầu từ seq=2).
- Gói data đầu tiên (lockdown `GetValue DevicePublicKey`) được gửi với
  `seq=1` — **trùng với SYN seq** → thiết bị coi là packet đã nhận lại, bỏ qua.
- Lockdownd không nhận được request → không trả lời → timeout 15s.
- Vì lockdownd không bao giờ nhận được yêu cầu Pair, Trust popup không bao
  giờ xuất hiện trên iPhone.

**Sửa (3 điểm):**
```python
# mux_usb.py — MuxConnection.__init__
self.seq = 0  # ← đổi từ 1 thành 0 (ISN=0, khớp usbmuxd)

# MuxDevice.connect() — sau khi gửi SYN
conn.seq += 1  # ← SYN chiếm 1 seq number, data đầu phải dùng seq=1

# MuxConnection.close() — sau khi gửi FIN
self.seq += 1  # ← FIN cũng chiếm 1 seq number
```

---

### 🟠 Lỗi 2 (QUAN TRỌNG): Cert PEM thay vì DER trong PairRecord → Trust popup bị từ chối im lặng

**File:** `app/src/main/python/device_link.py`

**Triệu chứng:** Ngay cả khi TCP connection mux hoạt động, Trust popup vẫn
không xuất hiện; lockdownd im lặng từ chối request Pair mà không báo lỗi.

**Nguyên nhân kỹ thuật:**
- `_generate_host_identity()` trả về certificate dạng **PEM bytes**
  (`-----BEGIN CERTIFICATE-----...`).
- `pair_device()` truyền PEM bytes vào trường `DeviceCertificate`,
  `HostCertificate`, `RootCertificate` trong PairRecord.
- lockdownd kỳ vọng **DER binary** trong các trường này. PEM text bị parse
  lỗi → thiết bị từ chối yêu cầu Pair trước khi kịp hiện hộp thoại Trust.

**Sửa:**
```python
# _generate_host_identity() — thêm hàm DER encoder và trả thêm DER bytes
def der(cert):
    return cert.public_bytes(serialization.Encoding.DER)

# Return dict bổ sung: "root_cert_der", "host_cert_der", "device_cert_der"

# pair_device() — PairRecord gửi lên thiết bị dùng DER
"PairRecord": {
    "DeviceCertificate": identity["device_cert_der"],  # ← DER, không PEM
    "HostCertificate": identity["host_cert_der"],
    "RootCertificate": identity["root_cert_der"],
    ...
}
# pair_record lưu đĩa vẫn dùng PEM (để ssl.SSLContext load được)
```

---

### 🟡 Lỗi 3 (PHỤ): Timeout chờ Trust quá ngắn (15s)

**File:** `app/src/main/python/device_link.py`

**Triệu chứng:** Người dùng không kịp bấm Trust trên iPhone trong 15 giây.

**Sửa:** Tăng timeout phản hồi Pair lên **60 giây**.

---

### 🟡 Lỗi 4 (PHỤ): Không xử lý `PairingDialogResponsePending` (iOS 13+)

**File:** `app/src/main/python/device_link.py`

**Triệu chứng:** Trên iOS 13+, lockdownd trả về `PairingDialogResponsePending`
trước khi hiện dialog Trust. App không retry → raise lỗi sớm.

**Sửa:** Thêm vòng retry (tối đa 12 lần × 5s) khi gặp `PairingDialogResponsePending`.

---

### 🟢 Cải tiến: Thông báo rõ hơn

**File:** `app/src/main/python/sideload_core.py`, `device_link.py`

- Thông báo rõ ràng khi nào người dùng cần bấm Trust.
- Thông báo lỗi cụ thể khi người dùng bấm "Không tin cậy" (Don't Trust):
  hướng dẫn ngắt/cắm lại USB và thử lại.
- Thông báo `InvalidHostID` riêng với hướng dẫn xoá pair record cũ.

---

## Tóm tắt

| # | File | Loại | Mô tả |
|---|------|-------|--------|
| 1 | mux_usb.py | 🔴 Nghiêm trọng | seq=0, SYN+1, FIN+1 — fix timeout + Trust popup |
| 2 | device_link.py | 🟠 Quan trọng | DER cert thay PEM trong PairRecord |
| 3 | device_link.py | 🟡 Phụ | Timeout Pair response 15s → 60s |
| 4 | device_link.py | 🟡 Phụ | Xử lý PairingDialogResponsePending (iOS 13+) |
| 5 | sideload_core.py | 🟢 UX | Thông báo hướng dẫn Trust rõ ràng hơn |
