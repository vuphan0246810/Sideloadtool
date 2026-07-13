# Changelog v3 — Phân tích từ source code libimobiledevice + pymobiledevice3

## Nguyên nhân gốc rễ thực sự của "không thấy Trust popup"

### 🔴 LỖI GỐC (đã sửa lần này): Pair request gửi DER thay vì PEM

**File:** `device_link.py`, hàm `pair_device()`

Phiên 1 (lần sửa đầu tiên) đã nhận định sai: cho rằng lockdownd cần DER
nên đổi PEM → DER trong PairRecord gửi cho thiết bị. Đây là nguyên nhân
trực tiếp khiến Trust popup không xuất hiện.

**Bằng chứng từ hai nguồn tham chiếu chính thức:**

1. **libimobiledevice C** — `lockdown.c` hàm `lockdownd_pair_record_to_plist()`:
   ```c
   plist_new_data(pair_record->device_certificate,
                  strlen(pair_record->device_certificate))
   ```
   `strlen()` chỉ đúng với chuỗi ASCII (PEM). DER là binary — `strlen()` sẽ
   dừng sớm ở byte `0x00` đầu tiên → chiều dài sai hoàn toàn.

2. **pymobiledevice3 Python** — `ca.py` hàm `generate_pairing_cert_chain()`:
   ```python
   return (
       serialize_cert_pem(host_cert),    # PEM
       serialize_cert_pem(device_cert),  # PEM
       serialize_cert_pem(root_cert),    # PEM
       ...
   )
   ```
   Cả hai nguồn đều gửi **PEM** trong Pair request.

**Sửa:** `DeviceCertificate`, `HostCertificate`, `RootCertificate` trong
PairRecord gửi thiết bị đổi về PEM. Pair record lưu đĩa vẫn dùng PEM
(đúng cho Python ssl module khi StartSession/TLS sau này).

---

### 🔴 Race condition: pump thread cũ đọc USB song song với start() mới

**File:** `mux_usb.py`, hàm `reset_device()`

`reset_device()` chỉ gọi `stop()` (đặt Event) mà không chờ pump thread
thực sự thoát. Pump thread đang blocking trong `bulkRead(2000ms)` nên có
thể sống thêm tới 2 giây sau khi `reset_device()` trả về. Trong 2 giây
đó, `MuxDevice.start()` mới cũng gọi `bulkRead()` → hai thread gọi
`UsbTransport.bulkTransfer()` đồng thời → hành vi không xác định.

**Sửa:** Thêm `old._pump_thread.join(timeout=6.0)` trong `reset_device()`
*ngoài* `_device_lock` để tránh deadlock tiềm ẩn.

---

### 🟡 USB hardware buffer không được xả trước version handshake

**File:** `mux_usb.py`, hàm `start()`

Khi tạo MuxDevice mới sau reset, USB hardware receive buffer có thể còn dữ
liệu từ phiên cũ (partial TCP packets chưa đọc). Nếu `_recv_raw()` đọc rác
thay vì version reply → MuxError "không phản hồi đúng bắt tay phiên bản".

**Sửa:** Đầu `start()` đọc tất cả dữ liệu USB pending với timeout 100ms
và bỏ qua chúng trước khi gửi version request.

---

### 🟡 Sai lock trong pump loop (từ phiên 2)

**File:** `mux_usb.py`, pump loop khi USB chết

Dùng `_io_lock` (lock serialize USB write) để iterate `_connections` dict —
sai về mặt ngữ nghĩa. `_connections` không được bảo vệ bởi `_io_lock`.

**Sửa:** Bỏ `with self._io_lock:`, dùng `list(_connections.values())`
snapshot trực tiếp.

---

## Tổng kết tất cả lỗi qua 3 phiên

| # | Phiên | File | Mức | Mô tả |
|---|-------|------|-----|--------|
| 1 | 1 | mux_usb.py | 🔴 | TCP seq=0/SYN+1/FIN+1 |
| 2 | 1 | device_link.py | 🔴 | 60s timeout chờ Trust |
| 3 | 1 | device_link.py | 🟠 | PairingDialogResponsePending retry |
| 4 | 2 | sideload_core.py | 🔴 | MuxDevice singleton reset trước mỗi lần chạy |
| 5 | 2 | sideload_core.py | 🔴 | Pair record không hợp lệ bị phát hiện + xóa |
| 6 | 2 | device_link.py | 🟠 | _request timeout 30s |
| 7 | 2 | device_link.py | 🟠 | PasswordProtected handling |
| 8 | 2 | device_link.py | 🟠 | DevicePublicKey DER fallback |
| 9 | **3** | **device_link.py** | **🔴 CHÍNH** | **Pair request → PEM thay vì DER** |
| 10 | **3** | **mux_usb.py** | **🔴** | **reset_device() join() pump thread** |
| 11 | **3** | **mux_usb.py** | **🟡** | **USB buffer flush trước version handshake** |
| 12 | **3** | **mux_usb.py** | **🟡** | **Bỏ sai lock trong pump loop** |

## Log cần kiểm tra khi test

```
[mux] Đã reset MuxDevice singleton.           ← reset OK
[mux] Bắt tay phiên bản usbmux...            ← version handshake bắt đầu
[mux] Thiết bị chấp nhận phiên bản usbmux 2.0 ← version handshake thành công
[pairing] Bắt đầu ghép nối lần đầu...         ← pair_record.plist chưa có / bị xóa
[pairing] Đang gửi yêu cầu ghép nối...        ← Pair request đã gửi (PEM certs)
[pairing] *** Kiểm tra màn hình iPhone ***     ← XEM IPHONE NGAY LÚC NÀY!
```

Nếu thấy popup trên iPhone → bấm "Tin cậy" (Trust) → thành công.
