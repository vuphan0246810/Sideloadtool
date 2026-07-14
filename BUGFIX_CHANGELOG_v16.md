# Changelog v16 — Sửa lỗi ghép nối/kết nối iPhone qua USB (usbmux, pairing) + thêm tab "Ghép nối"

## Bối cảnh

Video người dùng gửi cho thấy crash log thật khi bấm "Ký & Cài đặt":

```
Đang kết nối với thiết bị iOS qua USB...
❌ Lỗi không mong đợi trong do_sideload: 'module' object has no attribute 'connect_and_pair'
AttributeError: module 'device_link' has no attribute 'connect_and_pair'
```

Từ log này, đã đối chiếu byte-for-byte toàn bộ tầng usbmux (`usbmux.c`/`usbmux.h`)
với mã nguồn thật của `libimobiledevice/usbmuxd` (`src/device.c`) và phát hiện
toàn bộ bước bắt tay usbmux bị sai — đây là nguyên nhân gốc của rất nhiều lỗi
"không kết nối được", "không hiện Trust popup", "USB tự rớt" mà các bản
BUGFIX trước (v1-v15) đã cố sửa ở lớp trên (UsbTransport, claimInterface...)
nhưng không chạm tới gốc rễ ở tầng giao thức usbmux.

## Tóm tắt

| # | File | Nghiêm trọng | Mô tả |
|---|------|--------------|--------|
| 1 | `usbmux.c` / `usbmux.h` | 🔴 **CHÍNH** | Toàn bộ bắt tay usbmux sai: thiếu bước VERSION, sai magic (0xFADEDEAD thay vì 0xfeedface thật), sai layout header (20 byte cố định thay vì 8/16 byte tuỳ version), sai kiểu tx_seq/rx_seq (32-bit thay vì 16-bit) |
| 2 | `sideload_core.py` | 🔴 **CHÍNH** | Gọi `device_link.connect_and_pair()` — hàm không tồn tại (chính là crash trong video) |
| 3 | `sideload_core.py` | 🔴 | Sau khi "fix" lỗi 2, `install_ipa(signed_ipa)` vẫn gọi sai — thiếu bước `afc_push_ipa()` để stage IPA và thiếu tham số `pair_record` |
| 4 | `pairing.c` | 🟠 | `pairing_save()`/`pairing_load()` cắt cụt mọi chứng chỉ PEM khi lưu/đọc lại — pair record hỏng ngay khi mở lại app |
| 5 | `jni_bridge.c` / `lockdown.c` | 🟡 | Thiếu `#include <stdio.h>` (snprintf/asprintf) — có thể fail build trên toolchain nghiêm ngặt |
| ✨ | UI mới | — | Thêm tab **"Ghép nối"** — ghép nối với iPhone và tạo/xuất file pairing (.plist) độc lập với luồng Cài IPA |

---

### 🔴 Lỗi 1 (GỐC RỄ): usbmux.c/usbmux.h không khớp giao thức thật

**File:** `usbmux.c`, `usbmux.h`

**Đối chiếu với:** `libimobiledevice/usbmuxd/src/device.c` (struct `mux_header`,
struct `version_header`, `send_packet()`, `device_data_input()`,
`device_version_input()`).

**Các sai lệch cụ thể:**

1. **Sai magic number.** Giao thức thật dùng `0xfeedface`. Code cũ dùng
   `0xFADEDEAD` — bản BUGFIX trước tự nhận đã "so khớp byte-for-byte" nhưng
   thực ra đã đảo ngược giá trị đúng ban đầu.
2. **Sai cấu trúc header.** Header thật: `protocol`(4B) + `length`(4B) +
   `magic`(4B) + `tx_seq`(2B) + `rx_seq`(2B) = **16 byte**, nhưng **chỉ 8
   byte đầu** (`protocol`+`length`) được gửi khi phiên bản mux chưa được
   thoả thuận (< 2). Code cũ dùng một header 20-byte cố định
   (`magic+version+message+tx_seq+rx_seq+length`) không tồn tại trong giao
   thức thật.
3. **Thiếu hoàn toàn bước bắt tay VERSION.** Giao thức thật: gửi
   `MUX_PROTO_VERSION` (protocol=0, header 8 byte, payload
   `version_header{major=2, minor=0, padding=0}`) → nhận phản hồi phiên bản
   từ thiết bị → **sau đó** mới gửi `MUX_PROTO_SETUP` (protocol=2, header 16
   byte, payload 1 byte `0x07`, tự reset `tx_seq=0`/`rx_seq=0xFFFF`). Code cũ
   bỏ qua bước VERSION, gửi thẳng một gói "SETUP" tự chế với
   `MUX_MSG_SETUP=8` (giá trị không tồn tại trong giao thức thật) mang theo
   payload version — khiến iPhone không bao giờ phản hồi đúng cách, dẫn tới
   không hiện "Tin cậy máy tính này?" hoặc bị USB tự rớt kết nối.
4. **Sai độ rộng tx_seq/rx_seq.** Giao thức thật dùng 16-bit (`htons`). Code
   cũ dùng 32-bit (`htonl`) qua các trường `dev_tx_seq`/`dev_rx_seq` riêng.

**Đã sửa:** Viết lại hoàn toàn `usbmux.h`/`usbmux.c`, triển khai đúng bước
VERSION → SETUP như `usbmuxd/src/device.c`, đúng magic `0xfeedface`, đúng độ
rộng 16-bit cho tx_seq/rx_seq ở tầng mux-header, đúng kích thước header tuỳ
theo phiên bản đã thoả thuận (8 byte trước VERSION, 16 byte sau đó). API công
khai (`mux_conn_init`, `mux_do_setup`, `mux_connect`, `mux_send`, `mux_recv`,
`mux_recv_exact`, `mux_disconnect`) giữ nguyên chữ ký hàm nên không cần sửa
`lockdown.c`, `afc.c`, `install_proxy.c`, `jni_bridge.c` (đã xác nhận không
file nào khác truy cập trực tiếp nội bộ `mux_header_t`). Vẫn giữ nguyên fix
"drain excess bytes" hợp lệ đã có trong `mux_recv` từ bản trước.

---

### 🔴 Lỗi 2 (đúng như video crash log): `device_link.connect_and_pair()` không tồn tại

**File:** `sideload_core.py`, bước "Cài đặt lên thiết bị qua USB"

**Nguyên nhân:** `device_link.py` chỉ định nghĩa `pair_device()` (và alias
`pair_with_device()`) — không có `connect_and_pair()`. Đây chính là
`AttributeError` thấy trong video.

**Đã sửa:** Gọi đúng `device_link.pair_device()`, bắt `device_link.LockdownError`
để hiển thị lỗi rõ ràng thay vì crash.

### 🔴 Lỗi 3: `install_ipa()` bị gọi sai tham số, thiếu bước stage AFC

**File:** `sideload_core.py`, cùng đoạn với lỗi 2

**Nguyên nhân:** Code gọi `device_link.install_ipa(signed_ipa)` (1 tham số),
nhưng hàm thật cần `install_ipa(pair_record, remote_ipa_path)` (2 tham số) và
phụ thuộc `afc_push_ipa()` được gọi trước để "stage" đường dẫn IPA local — vốn
`sideload_core.py` chưa từng gọi. Ngay cả khi lỗi 2 được sửa riêng, bước này
vẫn sẽ crash hoặc raise `LockdownError`.

**Đã sửa:** Gọi đúng luồng 3 bước:
`pair_device()` → `afc_push_ipa(pair_record, signed_ipa, basename)` →
`install_ipa(pair_record, remote_path)`, có try/except cho `LockdownError`.

---

### 🟠 Lỗi 4: pair record bị cắt cụt khi lưu/đọc lại (ảnh hưởng trực tiếp tính năng "tạo file pairing")

**File:** `pairing.c` — `pairing_save()` / `pairing_load()`

**Nguyên nhân:** `pairing_save()` ghi mỗi chứng chỉ/khoá PEM dạng
`Key=<PEM>\n`, nhưng PEM luôn chứa newline bên trong
(`-----BEGIN...-----\nMII...\n-----END...-----\n`). `pairing_load()` chỉ đọc
tới newline **đầu tiên** sau `=`, nên khi đọc lại chỉ còn dòng
`-----BEGIN CERTIFICATE-----` — toàn bộ nội dung thật bị mất. Ứng dụng phải
ghép nối lại từ đầu mỗi lần mở lại, dù tưởng như đã lưu pair record thành
công.

**Đã sửa:** Base64-encode toàn bộ nội dung PEM (giữ nguyên newline bên
trong) thành một dòng trước khi ghi, dùng lại `b64_encode`/`b64_decode` đã có
sẵn trong `plist_util.c`. `pairing_load()` giờ decode lại đúng PEM gốc, và
trả lỗi rõ ràng nếu file hỏng/thiếu trường bắt buộc thay vì âm thầm trả về
pair record rỗng.

---

### 🟡 Lỗi 5 (rủi ro build): thiếu `#include <stdio.h>`

**File:** `jni_bridge.c`, `lockdown.c`

Cả hai file dùng `snprintf`/`asprintf` nhưng không `#include <stdio.h>` trực
tiếp — chỉ "chạy được" vì một header khác tình cờ include gián tiếp. Đã thêm
include tường minh để tránh lỗi build "implicit declaration" trên toolchain
nghiêm ngặt hơn (kể cả một số phiên bản NDK/Clang mới).

---

## ✨ Tính năng mới: Tab "Ghép nối"

Thêm tab thứ 2 trong thanh điều hướng dưới (`PairingScreen.kt`), độc lập hoàn
toàn với luồng "Cài IPA":

- **Kết nối & Ghép nối iPhone**: chạy `nativeConnect()` + `nativePair()` (dùng
  lại đúng tầng native đã fix ở trên), hiển thị trạng thái USB / đã ghép nối
  / UDID, và banner "Bấm Tin cậy trên iPhone" khi cần (dùng lại
  `PromptDialogHost` đã có sẵn toàn cục).
- **Tạo & chia sẻ file pairing (.plist)**: xuất pair record hiện tại thành
  một file `.plist` chuẩn kiểu Apple (`UDID`, `HostID`, `SystemBUID`,
  `RootCertificate`, `RootPrivateKey`, `HostCertificate`, `HostPrivateKey`,
  `DeviceCertificate` dạng `<data>` base64 — cùng định dạng pairing record
  của `libimobiledevice`/`idevicepair`), rồi mở share sheet Android
  (`Intent.ACTION_SEND` qua `FileProvider`) để người dùng lưu hoặc gửi file
  đi. Bổ sung mới ở tầng native: `plist_build_pairing_export()`
  (`plist_util.c`), `nativeIsPaired()`/`nativeGetPairingPlist()`
  (`jni_bridge.c`, `NativeBridge.kt`), và khai báo `FileProvider` trong
  `AndroidManifest.xml` + `res/xml/file_paths.xml`.

## Các file đã sửa/thêm

- `app/src/main/cpp/usbmux.h`, `usbmux.c` — viết lại hoàn toàn tầng giao thức
- `app/src/main/cpp/pairing.c` — fix lưu/đọc pair record
- `app/src/main/cpp/plist_util.h`, `plist_util.c` — thêm `plist_build_pairing_export()`, thêm `#include <stdint.h>` còn thiếu
- `app/src/main/cpp/jni_bridge.c` — thêm `nativeIsPaired`/`nativeGetPairingPlist`, thêm include còn thiếu
- `app/src/main/cpp/lockdown.c` — thêm include còn thiếu
- `app/src/main/python/sideload_core.py` — fix luồng pair + install
- `app/src/main/java/.../bridge/NativeBridge.kt` — thêm `isPaired()`/`exportPairingFile()`
- `app/src/main/java/.../ui/PairingScreen.kt` — **MỚI**: UI tab Ghép nối
- `app/src/main/java/.../ui/Navigation.kt`, `HomeViewModel.kt` — gắn tab mới
- `app/src/main/AndroidManifest.xml`, `res/xml/file_paths.xml` — **MỚI**: FileProvider

## Đã kiểm tra (không có Android SDK/NDK trong sandbox)

- Toàn bộ file `.c` đã sửa/thêm (`usbmux.c`, `pairing.c`, `plist_util.c`,
  `jni_bridge.c`, `lockdown.c`, `afc.c`, `install_proxy.c`) đã pass
  `gcc -fsyntax-only` với stub `android/log.h`/`jni.h` tối giản — không lỗi
  cú pháp/kiểu dữ liệu.
- Rà soát thủ công `sideload_core.py`/`device_link.py` để đảm bảo đúng chữ
  ký hàm và luồng gọi (không có Python trong sandbox để chạy `py_compile`).
- Chưa build được APK thật (thiếu Android SDK/NDK) — khuyến nghị build và
  test thật trên máy có SDK/NDK + iPhone thật trước khi phát hành.
