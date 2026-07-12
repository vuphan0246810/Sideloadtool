# SUPER ALPHA Sideload — Android

Ứng dụng Android (Kotlin + Jetpack Compose + Chaquopy) để **ký và cài đặt file
.ipa lên iPhone/iPad qua cáp USB**, không cần Termux, không cần Termux:API,
không cần root, không cần máy tính. Đây là bản chuyển đổi từ công cụ CLI gốc
chạy trong Termux sang một ứng dụng Android độc lập, dùng chính điện thoại
Android làm "máy tính trung gian" nói chuyện với iPhone qua USB.

> ⚠️ **Đọc kỹ mục "Rủi ro đã biết" bên dưới trước khi dùng.** Phần giao tiếp
> USB với iPhone (usbmux/lockdown/AFC) là code **tự viết lại, chưa từng được
> test với phần cứng thật**, vì môi trường phát triển dự án này (Replit)
> không có SDK Android, không có Gradle, không có iPhone thật để thử.

---

## 1. Kiến trúc

```
(gốc dự án — chính là thư mục sau khi giải nén zip này)
├── app/
│   ├── build.gradle.kts          # Cấu hình Chaquopy (Python 3.11), pip deps, ABI arm64-v8a
│   └── src/main/
│       ├── java/com/superalpha/sideload/
│       │   ├── SuperAlphaApp.kt          # Khởi động Python interpreter (Chaquopy)
│       │   ├── MainActivity.kt           # Entry point, mount Compose UI
│       │   ├── bridge/
│       │   │   ├── AppPaths.kt           # Đường dẫn filesystem app-private cho Python dùng
│       │   │   ├── NativeLog.kt          # print() (Python) -> SharedFlow -> LogConsole UI
│       │   │   ├── UiPrompt.kt           # input() (Python, vd mã 2FA) -> AlertDialog -> quay lại
│       │   │   ├── UsbTransport.kt       # USB Host API: claim interface, bulk IN/OUT thô
│       │   │   ├── UsbPermissionManager.kt # Xin quyền USB, mở kết nối, đọc UDID (serialNumber)
│       │   │   └── UsbBridgeService.kt   # Foreground service giữ kết nối USB khi app ở nền
│       │   ├── python/PythonBridge.kt    # Gọi vào sideload_core.py từ Compose (Dispatchers.IO)
│       │   └── ui/                       # Màn hình Compose: Sideload, Thu hồi cert, Cài đặt, Log
│       └── python/
│           ├── sideload_core.py   # Điểm vào chính: do_sideload(), do_revoke_certs()
│           ├── apple_auth.py      # Đăng nhập Apple ID (GSA/SRP) + xử lý 2FA
│           ├── developer_api.py   # Gọi Apple Developer API (cert, App ID, provisioning profile)
│           ├── device_link.py     # lockdownd/pairing/AFC/installation_proxy qua mux_usb
│           ├── mux_usb.py         # Giao thức "usbmux" tự triển khai lại trên USB bulk transfer
│           ├── config_manager.py  # Đọc/ghi config.json
│           └── utils.py           # Giải nén/đóng gói IPA, đọc Info.plist, v.v.
├── app/src/main/jniLibs/arm64-v8a/libzsign.so   # Binary zsign (ký lại IPA) — chỉ arm64
└── .github/workflows/build-apk.yml   # CI build APK debug (xem mục 4)
```

**Luồng dữ liệu chính khi bấm "Ký & Cài đặt":**

1. `SideloadScreen` (Compose) gọi `PythonBridge.sideload(...)` trên `Dispatchers.IO`.
2. Chaquopy chạy `sideload_core.do_sideload(...)` — mọi `print()` trong Python
   được chuyển hướng sang `NativeLog` (Kotlin `SharedFlow`) để hiện trong ô
   "Nhật ký" của UI, vì Android không có terminal.
3. Nếu Apple yêu cầu mã 2FA, `apple_auth.py` gọi `input()` như bản CLI gốc,
   nhưng ở đây `input_func` được thay bằng `UiPrompt.requestInput(...)` — hàm
   này chặn (block) luồng Python và hiện một `AlertDialog` trong Compose;
   Python chỉ tiếp tục chạy sau khi người dùng nhập mã và bấm xác nhận.
4. `developer_api.py` gọi thẳng Apple Developer API (HTTPS, qua `requests`) —
   phần này **không** đi qua USB, giống hệt bản CLI gốc.
5. `device_link.py` mở kết nối tới iPhone qua `mux_usb.py` (xem mục 2) để:
   ghép nối (pairing, cần bấm "Trust" trên iPhone lần đầu), đẩy file IPA đã
   ký qua AFC, rồi ra lệnh cài đặt qua `installation_proxy`.
6. `libzsign.so` được gọi như một tiến trình con (`subprocess`) để ký lại IPA
   bằng certificate/provisioning profile vừa lấy từ Apple.

---

## 2. Rủi ro đã biết (đọc trước khi dùng)

Dự án này được viết trong môi trường Replit, **không có SDK Android, không
có Gradle, không có trình giả lập/thiết bị Android, và không có iPhone thật**
để build hay test. Vì vậy:

| Thành phần | Mức độ tin cậy | Ghi chú |
|---|---|---|
| UI Compose, điều hướng, luồng gọi Python | Cao | Logic thuần Kotlin/Compose, không phụ thuộc phần cứng đặc thù |
| `apple_auth.py`, `developer_api.py` | Cao | Copy gần như nguyên vẹn từ bản CLI gốc đã hoạt động trong Termux, chỉ đổi `input()` |
| `utils.py` (giải nén/ký IPA, đọc plist) | Cao | Thuần Python, không đổi so với bản gốc |
| USB Host API claim/bulk transfer (`UsbTransport.kt`) | Trung bình | Dùng đúng API chuẩn của Android, nhưng chưa test với iPhone thật cắm qua USB |
| **`mux_usb.py`** (giao thức usbmux tự viết lại) | **Thấp — CHƯA KIỂM CHỨNG** | Xem chi tiết bên dưới |
| **`device_link.py`**, đặc biệt `TlsLockdownClient`/`start_session_tls` | **Thấp — CHƯA KIỂM CHỨNG** | Nâng cấp TLS thủ công qua `ssl.MemoryBIO`, chưa test |
| **AFC (đẩy file lên iPhone)** | **Thấp — CHƯA KIỂM CHỨNG** | Giao thức nhị phân tự triển khai lại theo tài liệu |

**Vì sao rủi ro nằm ở lớp USB:** Chạy `usbmuxd` (daemon chuẩn của Apple/
libimobiledevice để nói chuyện với iPhone qua USB) **không cần root** trên
Android là một vấn đề **chưa có giải pháp được cộng đồng công nhận** — xem
các issue còn mở trên GitHub của dự án
[libimobiledevice/usbmuxd](https://github.com/libimobiledevice/usbmuxd) từ
khoảng 2019–2021 bàn về việc này. `mux_usb.py` trong repo này là một nỗ lực
tự triển khai lại giao thức usbmux **trực tiếp trên USB Host API của
Android** (không qua daemon), dựa trên đọc mã nguồn tham khảo
(`usbmuxd/src/usb.c`, `usbmuxd/src/usb.h`, `usbmuxd/src/device.c`) — **không
phải** bằng cách chạy thử và bắt gói tin thật. Có khả năng cao một số chi
tiết byte-level (thứ tự trường trong header, hằng số, cách xử lý window/ACK
của lớp mô phỏng TCP) cần chỉnh sửa sau khi bạn build và test với:

- Android Studio + một điện thoại Android thật (USB Host API không hoạt động
  tốt trên trình giả lập).
- Một iPhone/iPad thật cắm qua cáp USB (không qua hub USB nếu có thể).
- Công cụ bắt gói USB (vd `Wireshark` + `usbmon` trên máy Linux dùng làm cầu
  nối để so sánh, hoặc log chi tiết `Log.d` thêm vào `UsbTransport.kt`).

**Nếu ghép nối/cài đặt qua USB không hoạt động ngay lần đầu**, đây là nơi cần
xem đầu tiên — không phải lỗi ở logic ký hay ở Apple Developer API (hai phần
đó dùng lại gần như nguyên vẹn logic đã chạy được trong bản CLI gốc).

**Phạm vi sử dụng dự định:** chỉ dùng với thiết bị và Apple ID của chính bạn.

---

## 3. Build bằng Android Studio (khuyến nghị để tự test/debug)

1. Cài **Android Studio** (bản mới, hỗ trợ AGP 8.6.x trở lên) và **JDK 17**.
2. Giải nén zip này, rồi mở **thư mục vừa giải nén** bằng Android Studio
   ("Open" -> chọn thư mục đó — đây là gốc dự án, đã chứa sẵn
   `settings.gradle.kts` ngay bên trong).
3. Android Studio sẽ tự tải Gradle wrapper (repo không commit sẵn file
   `gradle-wrapper.jar` — môi trường viết code này không có Java/Gradle để
   tạo file đó). Nếu Android Studio không tự tạo wrapper, chạy trong
   Terminal của Android Studio (nơi có sẵn Gradle đi kèm IDE):
   ```
   gradle wrapper --gradle-version 8.9
   ```
4. Đồng bộ Gradle ("Sync Now"), rồi Build > Build APK(s), hoặc chạy trực tiếp
   lên điện thoại Android thật đã bật "USB debugging" bằng nút Run.
5. Cài file `.ipa` cần sideload vào máy Android (vd tải xuống thư mục
   Downloads) để có thể chọn bằng nút "Chọn file IPA" trong app.

**Lưu ý phần cứng:** cần một điện thoại Android thật hỗ trợ **USB Host mode**
(USB OTG) và một cáp/adapter phù hợp để vừa cấp nguồn vừa truyền dữ liệu tới
iPhone (nhiều điện thoại Android cần cáp USB-C-to-Lightning "data", không
phải cáp sạc thường; hoặc dùng adapter USB OTG + cáp Lightning gốc của Apple).

---

## 4. Build tự động qua GitHub Actions (không cần máy có Android Studio)

Workflow tại **`.github/workflows/build-apk.yml`** (đã có sẵn ở gốc thư mục
zip này) sẽ tự build APK debug mỗi khi có push/PR. Cách lấy APK:

1. Giải nén zip này, tạo một repository GitHub mới (hoặc dùng repo có sẵn),
   rồi đẩy (push) toàn bộ nội dung đã giải nén lên đó — sao cho
   `settings.gradle.kts` và thư mục `.github/` nằm ngay ở gốc repository,
   không nằm trong thư mục con.
2. Vào tab **Actions** trên GitHub -> chọn lần chạy workflow "Build APK" mới
   nhất -> kéo xuống mục **Artifacts** -> tải `superalpha-sideload-debug-apk`.
3. Cài file APK đó lên điện thoại Android (cần bật "Cài từ nguồn không xác
   định" cho ứng dụng bạn dùng để mở file APK).

Vì môi trường CI không có iPhone thật cắm qua USB, workflow này **chỉ xác
nhận code biên dịch được**, không xác nhận luồng USB/ghép nối/cài đặt hoạt
động — việc đó cần test thủ công theo mục 2 và 3.

### Lỗi thường gặp: "Couldn't find Python 3.11"

Chaquopy cần chạy `pip` **ngay trên máy build** (không phải trên máy ảo
Android) để tải các thư viện Python (`requests`, `cryptography`, `srp`), nên
nó cần tìm được một Python 3.11 thật trên `PATH` của máy build — khớp đúng
major.minor với `version = "3.11"` khai báo trong `app/build.gradle.kts`.
Runner `ubuntu-latest` của GitHub Actions không đảm bảo có sẵn Python 3.11
trên `PATH`, nên workflow có bước **"Set up build Python (3.11, required by
Chaquopy)"** (dùng `actions/setup-python@v5`) chạy trước bước build — bước
này **bắt buộc phải có** trước bước "Build debug APK", nếu không Gradle sẽ
báo lỗi này khi chạy task `:app:installDebugPythonRequirements`. Nếu bạn tự
build local bằng Android Studio (mục 3) và gặp lỗi tương tự, hãy cài Python
3.11 trên máy và đảm bảo nó có trên `PATH` (Chaquopy tự tìm bằng lệnh
`python3.11`, rồi `python3`, rồi `python`; xem
[tài liệu buildPython](https://chaquo.com/chaquopy/doc/current/android.html#buildpython)).

---

## 5. Sử dụng ứng dụng

1. Mở app, vào tab **Sideload**.
2. Cắm iPhone/iPad vào điện thoại Android qua cáp USB (xem lưu ý phần cứng ở
   mục 3), bấm **Kết nối** — Android sẽ hỏi quyền truy cập USB, chọn "Cho
   phép" (và có thể tick "luôn dùng cho thiết bị này" để không phải hỏi lại).
3. Bấm **Chọn file IPA**, chọn file `.ipa` cần cài.
4. Nhập **Apple ID** và **mật khẩu**. Nếu tài khoản bật xác thực 2 yếu tố,
   app sẽ hiện hộp thoại yêu cầu nhập mã 6 số gửi tới thiết bị Apple khác của
   bạn — nhập mã rồi bấm xác nhận, sau đó **bấm "Ký & Cài đặt" lại lần nữa**
   (2FA chỉ cần xác thực một lần trong phiên, việc đăng nhập lần đầu dừng lại
   sau khi 2FA xong để bạn bấm chạy lại với phiên đã xác thực).
5. Theo dõi tiến trình trong ô **Nhật ký**. Lần đầu ghép nối với một iPhone,
   màn hình iPhone sẽ hiện hộp thoại "Trust This Computer?" — bấm **Trust**
   và nhập mã khoá màn hình của iPhone khi được hỏi.
6. Nếu gặp lỗi "đã đạt giới hạn certificate" (tài khoản Apple ID miễn phí chỉ
   được 1 certificate Development hoạt động cùng lúc), app sẽ **tự động thu
   hồi certificate cũ nhất** để lấy chỗ tạo mới — không cần vào tab "Thu hồi
   Certificate" thủ công trừ khi bạn muốn chủ động dọn certificate.

### Khác biệt nhỏ so với bản CLI gốc

- **Thu hồi certificate không còn hỏi xác nhận y/n cho từng cái** như bản CLI
  gốc — vì trên UI, việc bạn bấm nút "Thu hồi" (sau khi đã chọn rõ certificate
  nào hoặc "all") đã là một hành động xác nhận rõ ràng, không cần hỏi lại lần
  hai như khi gõ lệnh trong terminal.
- Khi đạt giới hạn certificate, thay vì hỏi bạn có muốn thu hồi cái cũ nhất
  hay không (như bản CLI), app **tự động thu hồi** — vì trên điện thoại không
  có "terminal" để hỏi/trả lời tương tác giữa chừng một tác vụ nền dài; nếu
  bạn muốn kiểm soát certificate nào bị thu hồi, hãy chủ động dùng tab "Thu
  hồi Certificate" trước khi bấm Sideload.

---

## 6. Lưu ý pháp lý / đạo đức

Công cụ này chỉ nên dùng để **cài ứng dụng lên chính thiết bị của bạn, bằng
chính Apple ID của bạn** (sideload cá nhân, không phân phối lại app đã ký cho
người khác). Việc dùng chứng chỉ ký của người khác, chia sẻ file IPA đã ký
cho nhiều người, hoặc dùng để cài phần mềm crack/vi phạm bản quyền đều nằm
ngoài phạm vi và mục đích của công cụ này, và có thể vi phạm điều khoản dịch
vụ của Apple.
