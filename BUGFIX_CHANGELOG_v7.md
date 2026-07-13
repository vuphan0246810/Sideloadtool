# Sideloadtool Bug-Fix Changelog — v7

## Vấn đề (Observed in v6)

Sau khi v6 thêm SSL fallback, log cho thấy iOS 16.7 gửi RST **trong chính TLS
handshake** — trước khi SSL hoàn tất:

```
[lockdown] Đang thực hiện TLS handshake với lockdownd (iOS 16+)...
[mux] Thiết bị gửi RST tới cổng host 40001 — kết nối bị từ chối/đóng.
❌ Thiết bị gửi RST trong TLS handshake: Kết nối bị đóng sớm (RST/FIN) ...
```

## Root Cause (v7)

**iOS 16.7 dùng mTLS (mutual TLS)** — không phải one-way TLS.

Trong mTLS, khi server (lockdownd) xử lý ClientHello của client và thấy client
không khai báo client certificate capability (hoặc không có cert), server có thể
gửi RST ngay thay vì tiếp tục với CertificateRequest.

Cụ thể: `_SslPipe.__init__` trong v6 chỉ load cert khi `pair_record != None`.
Khi pair lần đầu (`pair_record=None`), SSL context không có client cert → lockdownd
iOS 16.7 nhận ClientHello không có cert → gửi RST → handshake fail.

**Sequence iOS 16.7:**
- Plaintext → RST (iOS 16+ yêu cầu SSL)  ← đã fix trong v6
- SSL without client cert → RST (iOS 16.7 mTLS)  ← đây là bug v6/root cause v7
- SSL **with** client cert → handshake OK → QueryType → Trust popup → Pair ✅

## Fixes Applied (v7)

### `app/src/main/python/device_link.py`

**1. Thêm `_generate_temp_ssl_cert()`**
- Sinh RSA 2048 key + self-signed cert tạm thời bằng `cryptography` library
- Cert này chỉ dùng cho SSL handshake với iOS 16.7 lockdownd
- Apple **không verify** nội dung cert client — chỉ cần cert phải có mặt
  trong ClientHello (để mTLS handshake hoàn tất)
- Sau khi pair thành công, cert thật từ pair record được dùng cho các kết
  nối tiếp theo

**2. `_SslPipe.__init__` luôn load client cert**
- Trước (v6): chỉ load cert khi `pair_record != None` — anonymous khi pair lần đầu
- Sau (v7): nếu `pair_record != None` dùng `HostCertificate/HostPrivateKey`; 
  nếu `pair_record == None` (pair lần đầu) gọi `_generate_temp_ssl_cert()` và load
- Đảm bảo ClientHello luôn chứa Certificate extension, lockdownd không RST

**3. Thêm `ctx.maximum_version = ssl.TLSVersion.TLSv1_3`**
- Match chính xác cấu hình TlsLockdownClient (đã tồn tại trong v6)
- Đảm bảo SSL negotiation range [TLS 1.2, TLS 1.3] — không thấp hơn, không cao hơn

## Luồng hoạt động sau v7

```
USB kết nối
  → usbmux v2.0 handshake ✅
  → TCP kết nối tới port 62078 ✅
  → Plaintext QueryType → RST (iOS 16+) → catch LockdownRstError
  → Retry: TCP kết nối mới tới port 62078 ✅
  → _SslPipe: sinh temp RSA cert
  → SSL handshake với temp cert → ✅ (iOS 16.7 mTLS OK)
  → QueryType qua SSL ✅
  → GetValue(DevicePublicKey) qua SSL ✅
  → Pair request gửi qua SSL
  → iPhone hiện "Trust This Computer?" popup ← người dùng bấm Trust
  → Pair response: Success + EscrowBag ✅
  → Pair record lưu lại
  → start_session_tls → TlsLockdownClient (dùng HostCert từ pair record)
  → InstallationProxy → cài app ✅
```

## Lưu ý cho người dùng

- Khi app chạy tới bước **"Đang gửi yêu cầu ghép nối"**, iPhone sẽ hiện hộp
  thoại **"Trust This Computer?"**
- **PHẢI bấm "Tin cậy" (Trust)** trong vòng 60 giây
- Nếu không thấy popup: cắm lại cáp USB và thử lại
- iPhone phải còn **sáng màn hình** (không bị khoá) trong bước này
