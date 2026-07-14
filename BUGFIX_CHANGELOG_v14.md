# Changelog v14 — Sửa lỗi "zsign thất bại: [Errno 2] No such file or directory: 'zsign'"

## Bối cảnh

Log người dùng report khi bấm "Ký & Cài đặt" (App ID đã tạo/tái sử dụng thành công ở bước trước, nhờ fix v13):

```
[python] Đang ký IPA bằng zsign...
[python] ❌ zsign thất bại: [Errno 2] No such file or directory: 'zsign'
Lỗi: Cài đặt thất bại — xem nhật ký.
[usb] Thiết bị USB đã rút — bấm Kết nối để thử lại.
```

## Tóm tắt

| # | File | Nghiêm trọng | Mô tả |
|---|------|--------------|--------|
| 1 | `sideload_core.py` | 🔴 **CHÍNH** | Gọi `subprocess` với tên `"zsign"` trần — không tồn tại trên Android — thay vì dùng binary thật đã đóng gói sẵn trong APK |
| 2 | `sideload_core.py` | 🟠 | Kết quả `register_device()` không được kiểm tra → đăng ký thiết bị thất bại âm thầm, lỗi thật chỉ lộ ra (khó hiểu) ở bước tải Provisioning Profile sau đó |

---

### 🔴 Lỗi 1 (NGUYÊN NHÂN CHÍNH): gọi `"zsign"` thay vì `AppPaths.zsignPath()`

**File:** `sideload_core.py`, bước "Ký IPA bằng zsign"

**Nguyên nhân:** Code cũ gọi:
```python
run_command(["zsign", "-k", key_file, ...])
```
`"zsign"` ở đây là tên binary **trần**, subprocess sẽ tìm nó trong `PATH` của
tiến trình. Android **không có `PATH` kiểu Linux desktop/Termux**, và app
cũng không được phép `exec()` file tuỳ ý ngoài `nativeLibraryDir()` — nên
`FileNotFoundError: [Errno 2] No such file or directory: 'zsign'` là kết quả
tất yếu, không phải lỗi ngẫu nhiên.

Repo đã có sẵn `AppPaths.zsignPath()` (Kotlin, `bridge/AppPaths.kt`) trỏ
đúng tới binary zsign thật được đóng gói cùng APK
(`jniLibs/arm64-v8a/libzsign.so`), và `AppPaths.nativeDepsDir()` để giải nén
`libssl.so.3`/`libcrypto.so.3`/`libc++_shared.so` mà `libzsign.so` cần lúc
chạy — README đã ghi lại đúng thiết kế này (mục "Sửa lỗi ký IPA thất bại:
CANNOT LINK EXECUTABLE... library libssl.so.3 not found") — nhưng
`sideload_core.py` chưa từng thực sự gọi 2 hàm đó, nên vẫn dùng `"zsign"`
trần từ code Termux gốc.

**Fix:**
```python
zsign_bin = AppPaths.zsignPath()
zsign_env = {"LD_LIBRARY_PATH": AppPaths.nativeDepsDir()}
run_command([zsign_bin, "-k", key_file, ...], extra_env=zsign_env)
```
Đồng thời bắt riêng `FileNotFoundError` để báo lỗi rõ ràng nếu binary vẫn
không tồn tại ở đường dẫn mong đợi (vd build sai ABI, thiếu `libzsign.so`
trong `jniLibs/arm64-v8a/`).

---

### 🟠 Lỗi 2: không kiểm tra kết quả `register_device()`

**File:** `sideload_core.py`, bước "Đăng ký UDID thiết bị"

**Nguyên nhân:** `register_device()` trả `None` khi Apple từ chối đăng ký
thiết bị (lưu lý do vào `dev_api.last_error`), nhưng code gọi hàm này không
kiểm tra giá trị trả về — nếu đăng ký thất bại, sideload vẫn tiếp tục đến
bước tải Provisioning Profile như thể thiết bị đã đăng ký, và lỗi thật chỉ
lộ ra ở đó dưới dạng resultCode 8220 khó truy nguyên nhân.

**Fix:** kiểm tra kết quả ngay, dừng lại với thông báo rõ ràng nếu thất bại,
thay vì âm thầm đi tiếp.

---

## Files đã thay đổi

| File | Thay đổi |
|------|----------|
| `app/src/main/python/sideload_core.py` | Dùng `AppPaths.zsignPath()` + `LD_LIBRARY_PATH` khi gọi zsign; kiểm tra kết quả `register_device()` |
| `app/build.gradle.kts` | Bump `versionCode`/`versionName` |

## Không thay đổi (đã đúng từ các bản trước)

- `AppPaths.kt` — `zsignPath()`/`nativeDepsDir()` đã đúng, chỉ chưa được gọi tới ở lớp Python.
- `developer_api.py` — `register_device()` đã trả `None` + `last_error` đúng khi thất bại, chỉ chưa được caller kiểm tra.
- `_resolve_app_id()` (v13) — vẫn giữ nguyên, không liên quan tới lỗi lần này.
