# Sideloadtool Bug-Fix Changelog — v8

## Vấn đề (Observed in v7)

Tab "Thu hồi chứng chỉ" luôn hiện lỗi:

```
Lỗi: Tính năng này cần được port sang Kotlin HTTP client.
```

Dù bấm "Thu hồi" với mọi Apple ID/mật khẩu hợp lệ.

## Root Cause (v8)

Khi port sang native C (v6–v7), **Chaquopy Python runtime bị xoá hoàn toàn** khỏi
`app/build.gradle.kts`. `PythonBridge.kt` được viết lại thành stub — `revokeCerts()`
và `sideload()` đều trả về lỗi cứng `NOT_PORTED_MESSAGE` mà không cố chạy bất kỳ
code nào.

Vấn đề: **"Thu hồi chứng chỉ" không dùng USB/lockdown**. Nó chỉ giao tiếp với
Apple qua HTTPS (`apple_auth.py` + `developer_api.py`). Không có lý do gì để port
tính năng này sang Kotlin — Python HTTP là đủ và đơn giản hơn nhiều.

## Scope đúng của "port sang native"

| Thành phần | v7 | v8 |
|---|---|---|
| `mux_usb.py` — USB bulk I/O, usbmux protocol | ❌ Python (dùng Chaquopy) | ✅ Native C (`usbmux.c`) |
| `device_link.py` — lockdown, pairing, AFC, install | ❌ Python (dùng Chaquopy) | ✅ Native C (`lockdown.c`, `pairing.c`, `afc.c`, `install_proxy.c`) |
| `apple_auth.py` — đăng nhập Apple ID qua GSA/SRP | ❌ Stub (không hoạt động) | ✅ Python qua Chaquopy |
| `developer_api.py` — Apple Developer Services HTTP | ❌ Stub (không hoạt động) | ✅ Python qua Chaquopy |
| `sideload_core.py` — điểm vào chính | ❌ Stub (không hoạt động) | ✅ Python qua Chaquopy |
| `config_manager.py` — đọc/ghi config JSON | ❌ Stub (không hoạt động) | ✅ Python qua Chaquopy |
| `utils.py` — zsign subprocess, IPA utilities | ❌ Stub (không hoạt động) | ✅ Python qua Chaquopy |

## Fixes Applied (v8)

### `app/build.gradle.kts`
- **Khôi phục** `id("com.chaquo.python")` plugin
- **Thêm** khối `python { pip { install("requests"); install("cryptography"); install("srp") } }`
- Root `build.gradle.kts` đã có `id("com.chaquo.python") version "17.0.0" apply false` từ trước — không đổi

### `SuperAlphaApp.kt`
- **Khôi phục** `Python.start(AndroidPlatform(this))` trong `onCreate()`
- **Thêm** `AppPaths.init(this)` (trước đây chỉ có trong `MainActivity.onCreate()` — Python cần nó khi chạy background)
- **Thêm** `DeviceNative.init(this)` để khởi tạo native bridge

### New: `bridge/DeviceNative.kt`
- Kotlin object mới: synchronous (blocking) wrapper để **Python gọi native C qua Chaquopy Java interop**
- `connectAndPair()` → `runBlocking { bridge.connect() && bridge.pair() }`
- `sideloadIpa(localIpaPath)` → `runBlocking { bridge.sideload(localIpaPath) }`
- `getUdid()` → `runBlocking { bridge.getUdid() }`
- `reset()` → `bridge.reset()`
- Chaquopy không thể gọi Kotlin suspend functions trực tiếp — `DeviceNative` cung cấp các method JVM thông thường

### `python/PythonBridge.kt`
- **Khôi phục** Chaquopy imports (`com.chaquo.python.Python`)
- `revokeCerts()`: gọi `Python.getInstance().getModule("sideload_core").callAttr("do_revoke_certs", ...)`
  - Thứ tự tham số đúng: `apple_id, password, anisette_url, cert_selector`
- `sideload()`: gọi `sideload_core.do_sideload(...)` qua Chaquopy
- `listAnisetteServers()`: **giữ nguyên** Kotlin OkHttp (không cần Python, đã port ở v7)

### `python/mux_usb.py` — viết lại
- Thay thế toàn bộ implementation cũ bằng **stub rõ ràng**
- Giữ lại class `MuxError`, `MuxRstError` để không phá import cũ
- `get_device()` → raise `MuxError` với message giải thích (USB đã là native C)
- `reset_device()` → delegate sang `DeviceNative.reset()`
- Không còn import `struct`, `threading`, `queue`, không còn USB I/O Python

### `python/device_link.py` — viết lại
- Thay thế toàn bộ implementation cũ (SSL, plist, AFC) bằng **native delegate**
- `pair_device(udid)` → `DeviceNative.connectAndPair()` → trả `{"native": True, "udid": ...}`
- `pair_with_device(udid)` → alias của `pair_device()` (backward compat)
- `validate_pair_record(record)` → trả `True` nếu `record.get("native") is True`
- `afc_push_ipa(...)` → stage IPA path (không push ngay)
- `install_ipa(...)` → `DeviceNative.sideloadIpa(staged_path)` (push + install trong một lần)
- `list_installed_apps(...)` → trả `[]` (TODO v9: thêm JNI method)
- `reset_mux_device()` → `DeviceNative.reset()`
- `get_udid_from_usb()` → `sideload_core.get_cached_udid()`

## Luồng hoạt động sau v8

### Thu hồi chứng chỉ (đã sửa ✅)
```
UI bấm "Thu hồi"
  → PythonBridge.revokeCerts() [Kotlin]
  → sideload_core.do_revoke_certs() [Python/Chaquopy]
  → apple_auth.AppleAuth.authenticate() [Python HTTP → gsa.apple.com]
  → developer_api.DeveloperAPI.list_certificates() [Python HTTP → developer.apple.com]
  → developer_api.DeveloperAPI.revoke_certificate() [Python HTTP → developer.apple.com]
  → ✅ Thành công
```

### Sideload IPA với Apple ID (phần USB đã là native)
```
UI bấm "Ký & Cài đặt"
  → PythonBridge.sideload() [Kotlin]
  → sideload_core.do_sideload() [Python/Chaquopy]
  → apple_auth: đăng nhập Apple ID [Python HTTP]
  → developer_api: cert, App ID, profile [Python HTTP]
  → zsign: ký IPA [subprocess native]
  → device_link.pair_device() → DeviceNative.connectAndPair() → C JNI (usbmux + lockdown)
  → device_link.afc_push_ipa() [stage path]
  → device_link.install_ipa() → DeviceNative.sideloadIpa() → C JNI (AFC + install_proxy)
  → ✅ Thành công
```

## Lưu ý build

- `minSdk = 26` — Chaquopy 17.0.0 yêu cầu tối thiểu API 21, nên không vấn đề
- Chaquopy tự động tải Python 3.11 và các wheel Android (requests, cryptography, srp) khi build
- `srp` được cài từ PyPI — wheel cho Android arm64 có sẵn trong Chaquopy's package server
- Nếu build lần đầu mất lâu hơn bình thường là do Chaquopy đang tải Python runtime (~15 MB)
