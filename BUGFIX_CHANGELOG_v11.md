# BUGFIX CHANGELOG v11 — SideloadTool Android

Ngày: 2026-07-14  
Nhánh cơ sở: `vuphan0246810/Sideloadtool` (sau v10)

---

## Lỗi phát hiện từ video thiết bị thực

### BUG-1 ❌ [CRITICAL] `AppleAuth` không có attribute `sign_in`
**Triệu chứng:** `AttributeError: 'AppleAuth' object has no attribute 'sign_in'`  
**File:** `sideload_core.py` dòng ~214 (do_sideload) và ~362 (do_revoke_certs)  
**Nguyên nhân:** `sideload_core.py` gọi `auth.sign_in()` nhưng `AppleAuth` chỉ có phương thức `authenticate()`.  
**Fix:** Thay `auth.sign_in(apple_id, password)` → `auth.authenticate(apple_id, password)` ở cả hai hàm. Cũng thêm kiểm tra `session.get("authenticated")` cho chắc chắn.

---

### BUG-2 ❌ [CRITICAL] `DeveloperAPI` khởi tạo sai tham số
**Triệu chứng:** `TypeError` hoặc `AttributeError: 'dict' object has no attribute 'session'`  
**File:** `sideload_core.py` dòng ~221 và ~368  
**Nguyên nhân:** Code gọi `DeveloperAPI(session)` truyền dict session vào tham số đầu, nhưng constructor yêu cầu `DeveloperAPI(apple_auth_instance, dsid, session_token)`. `self.auth.session` bên trong sẽ fail vì `session` là dict không phải `AppleAuth` object.  
**Fix:** `DeveloperAPI(auth, session["dsid"], session["session_token"])` — truyền auth object gốc, dsid và session_token tách riêng.

---

### BUG-3 ❌ [CRITICAL] `create_certificate` gọi sai — method không tồn tại
**Triệu chứng:** `AttributeError: 'DeveloperAPI' object has no attribute 'generate_csr'`  
**File:** `sideload_core.py` dòng ~255-261  
**Nguyên nhân:** Code gọi `dev_api.generate_csr(private_key)` (không tồn tại) và `dev_api.create_certificate(csr_pem)` (sai signature). `create_certificate()` tự sinh RSA key + CSR nội bộ, không nhận tham số nào.  
**Fix:**
- Xóa toàn bộ block sinh key/CSR thủ công
- Gọi `cert_data = dev_api.create_certificate()` (không có arg)
- Lấy kết quả từ đúng fields: `cert_data["certificateId"]`, `cert_data["certContent"]`, `cert_data["_private_key_pem"]`
- Convert DER bytes → PEM bằng base64.encodebytes (zsign cần file PEM)

---

### BUG-4 ❌ [HIGH] `create_app_id` gọi với tham số thừa
**Triệu chứng:** `TypeError: create_app_id() takes 3 positional arguments but 4 were given`  
**File:** `sideload_core.py` dòng ~282  
**Nguyên nhân:** `dev_api.create_app_id(safe_bundle, app_name, team_id)` truyền `team_id` thừa. `team_id` đã được `set_team()` đặt vào `dev_api.team_id` rồi.  
**Fix:** `dev_api.create_app_id(safe_bundle, app_name)` — bỏ arg `team_id`.

---

### BUG-5 ❌ [HIGH] Sai format dict khi tra cứu App ID và UDID thiết bị
**Triệu chứng:** App ID không được tái sử dụng, thiết bị luôn bị đăng ký lại mỗi lần chạy  
**File:** `sideload_core.py` dòng ~276-296  
**Nguyên nhân:** `list_app_ids()` và `list_devices()` dùng **old plist API** (`developerservices2.apple.com`), trả về dict flat với `identifier`, `appIdId`, `deviceNumber` — KHÔNG có lớp `attributes` như App Store Connect v1. Code cũ check `a.get("attributes", {}).get("identifier")` và `d.get("attributes", {}).get("udid")` → luôn None.  
**Fix:**
- App ID lookup: `a.get("identifier") or a.get("attributes", {}).get("identifier", "")`
- App ID ID: `a.get("appIdId") or a.get("id")`
- Device check: `d.get("deviceNumber") or d.get("attributes", {}).get("udid", "")`

---

### BUG-6 ❌ [HIGH] `create_provisioning_profile` không tồn tại
**Triệu chứng:** `AttributeError: 'DeveloperAPI' object has no attribute 'create_provisioning_profile'`  
**File:** `sideload_core.py` dòng ~301  
**Nguyên nhân:** Method đúng là `download_provisioning_profile(app_id_id)`, không phải `create_provisioning_profile(app_id_id, cert_id, [udid])`. Hàm này chỉ nhận `appIdId` (không nhận cert/udid vì đây là Team Provisioning Profile).  
**Fix:** `dev_api.download_provisioning_profile(app_id_id)` — đúng method name.

---

### BUG-7 ❌ [HIGH] Sai field khi đọc Provisioning Profile
**Triệu chứng:** `profile_bytes` rỗng → file .mobileprovision 0 bytes → zsign crash  
**File:** `sideload_core.py` dòng ~304  
**Nguyên nhân:** Code đọc `profile_data.get("attributes", {}).get("profileContent", "")` nhưng `download_provisioning_profile()` trả về dict từ old plist API có field `encodedProfile` ở top-level.  
**Fix:** `profile_data.get("encodedProfile") or profile_data.get("profileContent")` + thêm kiểm tra bytes rỗng.

---

### BUG-8 ❌ [CRITICAL] AFC opcode sai hoàn toàn — toàn bộ file transfer fail
**Triệu chứng:** `afc_check_status: unexpected op 0x1` ngay sau mỗi lệnh AFC  
**File:** `afc.h`  
**Nguyên nhân:** AFC opcode enum bị gán sai giá trị hex:

| Constant | Giá trị sai | Giá trị đúng | Nguồn |
|---|---|---|---|
| `AFC_OP_STATUS` | `0x0000` | **`0x0001`** | libimobiledevice afc_opcode_t |
| `AFC_OP_DATA` | `0x0001` | **`0x0002`** | file-open response = 2 |
| `AFC_OP_STAT` | `0x0001` | **`0x000A`** | GetFileInfo = 10 |
| `AFC_OP_MAKE_DIR` | `0x0006` | **`0x0009`** | MakeDir = 9 |
| `AFC_OP_FILE_TELL` | `0x0011` | **`0x0012`** | FileTell = 18; 0x0011 là FileSeek |

Hậu quả: `afc_mkdir` gửi opcode 0x0006 (không tồn tại) thay vì 0x0009; `afc_check_status` expect 0x0000 nhưng device gửi 0x0001 → reject mọi response → IPA push fail 100%.  
**Fix:** Cập nhật tất cả giá trị theo libimobiledevice/src/afc.c.

---

### BUG-9 ❌ [HIGH] `afc.c` không drain extra bytes khi `payload_out != NULL`
**Triệu chứng:** Packet tiếp theo đọc sai data (lỗi "unexpected magic" hoặc silent corruption)  
**File:** `afc.c`, hàm `afc_recv_pkt()`  
**Nguyên nhân:** Khi `entire_length > this_length` (nghĩa là có data phần "extra" sau `this_length` bytes), code chỉ drain khi `!payload_out`. Nếu caller đã nhận payload, extra bytes vẫn nằm trong USB buffer và làm corrupt packet tiếp theo.  
**Fix:** Luôn drain extra bytes bất kể `payload_out` có NULL hay không.

---

### BUG-10 ❌ [HIGH] `usbmux.c` `mux_recv` không drain excess bytes
**Triệu chứng:** Sau lần đầu nhận packet lớn hơn `maxlen`, mọi packet tiếp theo bị desync (wrong header magic, wrong port numbers)  
**File:** `usbmux.c`, hàm `mux_recv()`  
**Nguyên nhân:** Khi `payload > maxlen`, code chỉ đọc `maxlen` bytes và bỏ qua `payload - maxlen` bytes còn lại trong USB buffer. Các bytes thừa này sẽ được đọc vào đầu packet tiếp theo gây desync hoàn toàn.  
**Fix:** Tính `full_payload` và `read_len` riêng; sau khi đọc `read_len` bytes, drain `full_payload - read_len` bytes thừa vào buffer tạm 512-byte.

---

### BUG-11 ❌ [MEDIUM] TLS 1.0 / 1.1 bị Android 10+ vô hiệu hóa
**Triệu chứng:** TLS handshake fail trên Android 10+: `SSLHandshakeException: No appropriate protocol`  
**File:** `TlsHelper.kt` dòng 41  
**Nguyên nhân:** `enabledProtocols = arrayOf("TLSv1", "TLSv1.1", "TLSv1.2")` — Android 10 (API 29+) tắt TLS 1.0 và 1.1 theo mặc định theo chính sách bảo mật. lockdownd của Apple yêu cầu TLS 1.2.  
**Fix:** `enabledProtocols = arrayOf("TLSv1.2")` — chỉ bật TLS 1.2.

---

## Tóm tắt files đã thay đổi

| File | Bugs fixed |
|------|-----------|
| `app/src/main/python/sideload_core.py` | BUG-1, 2, 3, 4, 5, 6, 7 |
| `app/src/main/cpp/afc.h` | BUG-8 |
| `app/src/main/cpp/afc.c` | BUG-9 |
| `app/src/main/cpp/usbmux.c` | BUG-10 |
| `app/src/main/java/com/superalpha/sideload/bridge/TlsHelper.kt` | BUG-11 |

## Không thay đổi (đã đúng từ v10)

- `UsbTransport.kt` — retry 5 lần, delay 150ms ✓
- `UsbPermissionManager.kt` — cooldown reset on failure, 8s ✓
- `apple_auth.py` — method `authenticate()` đúng ✓
- `developer_api.py` — `set_team()`, `create_certificate()`, `download_provisioning_profile()` đúng ✓
- `device_link.py` — stub adapter gọi `DeviceNative` (C++) ✓
- `usbmux.c` / `lockdown.c` / `pairing.c` — logic USB mux đúng ✓
