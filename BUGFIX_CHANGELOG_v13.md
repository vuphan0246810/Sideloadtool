# Changelog v13 — Sửa lỗi "An App ID with Identifier '...' is not available"

## Bối cảnh

Log người dùng report khi bấm "Ký & Cài đặt" với `primary:SideStore (6).ipa`:

```
[python] [developer_api] Failed to create App ID: {'userString': "An App ID with
Identifier 'com.SideStore.SideStore' is not available. Please enter a different
string.", 'resultCode': 9401, 'resultString': "An App ID with Identifier
'com.SideStore.SideStore' is not available. Please enter a different string.", ...}
[python] ❌ Không tạo được App ID: unavailable
Lỗi: Cài đặt thất bại — xem nhật ký.
```

## Tóm tắt

| # | File | Nghiêm trọng | Mô tả |
|---|------|--------------|--------|
| 1 | `sideload_core.py` | 🔴 **CHÍNH** | Lỗi 'unavailable' (App ID bị trùng toàn cầu) khiến sideload bỏ cuộc ngay — không có logic tự đổi bundle id như `developer_api.py` đã dự tính |
| 2 | `sideload_core.py` | 🔴 **CHÍNH** | `set_bundle_id()` bị gọi với bundle id có tiền tố Team ID thừa → CFBundleIdentifier không khớp entitlement của provisioning profile → cài đặt luôn thất bại dù ký "thành công" |

---

### 🔴 Lỗi 1 (NGUYÊN NHÂN CHÍNH): thiếu `_resolve_app_id()`

**File:** `sideload_core.py`

**Triệu chứng:** Ký & Cài đặt luôn thất bại ngay ở bước tạo App ID với thông
báo `An App ID with Identifier 'com.SideStore.SideStore' is not available.`
(resultCode 9401).

**Nguyên nhân:** App ID (bundle id) trên Apple Developer là chuỗi **duy nhất
TOÀN CẦU** — không chỉ trong tài khoản của bạn. Vì `com.SideStore.SideStore`
là bundle id gốc (chưa đổi) của một app rất phổ biến, hàng nghìn người khác
đã từng đăng ký đúng chuỗi này bằng Apple ID riêng của họ trước bạn → tài
khoản của bạn không thể đăng ký lại chuỗi đó nữa.

`developer_api.py` đã có sẵn `classify_app_id_error()` để phân biệt lỗi này
('unavailable') với lỗi giới hạn tài khoản ('quota'), và docstring của nó ghi
rõ ý định gọi một hàm `sideload_core.py::_resolve_app_id()` để xử lý — nhưng
hàm đó **chưa từng được viết**. `do_sideload()` cũ chỉ in lời giải thích lỗi
rồi `return False` ngay, không hề thử tự sửa.

**Fix:** Thêm `_resolve_app_id()` trong `sideload_core.py`:

- Nếu lỗi là `'unavailable'`: tự thêm hậu tố ngẫu nhiên 5 ký tự vào bundle id
  gốc (vd `com.SideStore.SideStore-a1b2c`) và thử tạo lại, tối đa 5 lần —
  đúng cách AltStore/SideStore/Sideloadly xử lý vấn đề này trong thực tế.
- Nếu lỗi là `'quota'` (tài khoản free đã tạo đủ 10 App ID mới/7 ngày): tự
  xoá 1 App ID cũ do **chính tool này** tạo trước đó (tra trong registry cục
  bộ `sideload_state.json["app_id_map"]`, không bao giờ đụng App ID không rõ
  nguồn gốc) để giải phóng hạn mức, rồi thử lại.
- Bundle id hiệu lực cuối cùng + `appIdId` được lưu lại trong state, để lần
  sideload SAU của **cùng app trên cùng tài khoản** dùng lại đúng App ID đó
  — tránh vừa tốn thêm App ID trong hạn mức mỗi lần bấm nút, vừa tránh cài
  app đó thành 2 bản khác nhau trên máy chỉ vì bundle id đổi ngẫu nhiên mỗi
  lần chạy.

---

### 🔴 Lỗi 2: `set_bundle_id()` bị gọi với tiền tố Team ID thừa

**File:** `sideload_core.py`, dòng cũ ~360 (`set_bundle_id(app_dir, f"{team_id}.{safe_bundle}")`)

**Triệu chứng:** Ngay cả khi bước tạo App ID/Provisioning Profile ở trên
thành công và zsign báo "Ký IPA thành công", việc cài đặt lên iPhone thật vẫn
có thể bị từ chối (hoặc app crash khi mở) vì code signing không hợp lệ.

**Nguyên nhân:** `CFBundleIdentifier` trong `Info.plist` của app **không bao
giờ** chứa Team ID — Team ID chỉ xuất hiện trong entitlement
`application-identifier` của provisioning profile dưới dạng
`"TEAMID.<CFBundleIdentifier>"`, do Apple tự ghép khi tạo profile, không phải
do tool ghép vào `Info.plist`. Code cũ gọi
`set_bundle_id(app_dir, f"{team_id}.{safe_bundle}")` — tự ý ghép thêm Team ID
vào `CFBundleIdentifier`. Kết quả: App ID đăng ký với Apple là
`"<team_id>.com.SideStore.SideStore[-xxxxx]"`, nhưng `CFBundleIdentifier` bị
ghi thành `"<team_id>.com.SideStore.SideStore[-xxxxx]"` **lặp lại** Team ID
một lần nữa so với App ID entitlement thực tế trong profile → không khớp →
`installd` từ chối cài hoặc app không chạy được.

**Fix:** `set_bundle_id(app_dir, effective_bundle)` — chỉ dùng bundle id thật
(không có Team ID), đúng với bundle id đã đăng ký App ID ở bước trên.

---

## Files đã thay đổi

| File | Thay đổi |
|------|----------|
| `app/src/main/python/sideload_core.py` | Thêm `_resolve_app_id()`, `_find_app_id_by_bundle()`, `_free_one_own_app_id()`; sửa lời gọi `set_bundle_id()` |
| `app/build.gradle.kts` | Bump `versionCode`/`versionName` |

## Không thay đổi (đã đúng từ các bản trước)

- `developer_api.py` — `classify_app_id_error()`, `delete_app_id()`, `create_app_id()` đúng, chỉ chưa được tận dụng đầy đủ ở lớp gọi.
- `afc.c` / `afc.h` / `usbmux.c` — logic AFC/usbmux đã đúng từ v11.
- `TlsHelper.kt` — TLS 1.2 only, đúng từ v11.
