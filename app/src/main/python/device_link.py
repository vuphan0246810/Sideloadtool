"""device_link.py — client tối giản cho lockdownd / pairing / AFC /
installation_proxy, xây trên mux_usb.py.

FIX v7 (2026-07-13) — iOS 16.7 mTLS fix:
  - CRITICAL: iOS 16.7 dùng mTLS (mutual TLS) — client PHẢI gửi certificate
    ngay trong SSL ClientHello. _SslPipe anonymous (không có cert) bị lockdownd
    RST ngay khi nhận ClientHello vì thiếu client certificate.
  - Thêm _generate_temp_ssl_cert(): tạo RSA 2048 self-signed cert tạm thời
    cho lần pair đầu (chưa có pair record). Cert này chỉ dùng cho SSL handshake,
    không liên quan đến cert chain trong Pair request.
  - _SslPipe luôn load client cert: pair_record cert nếu có, ngược lại dùng
    temp cert tự sinh — đảm bảo ClientHello chứa Certificate extension.
  - Thêm maximum_version=TLSv1_3 để match chính xác TlsLockdownClient.
  - Tất cả fix v5/v6 giữ nguyên.
"""

import plistlib
import ssl
import struct
import tempfile
import os
import time
import uuid

from mux_usb import get_device, MuxConnection, MuxError, MuxRstError

LOCKDOWN_PORT = 62078


# ─────────────────────────────────────────────────────────────────────────
# UDID
# ─────────────────────────────────────────────────────────────────────────

def get_udid_from_usb():
    """Đọc UDID đã được set qua sideload_core.set_current_udid()."""
    import sideload_core
    return sideload_core.get_cached_udid()


# ─────────────────────────────────────────────────────────────────────────
# Lockdown wire format: [4-byte big-endian length][plist XML]
# ─────────────────────────────────────────────────────────────────────────

def _send_plist(conn: MuxConnection, obj: dict):
    payload = plistlib.dumps(obj, fmt=plistlib.FMT_XML)
    conn.send(struct.pack(">I", len(payload)) + payload)


def _recv_plist(conn: MuxConnection, timeout=15.0) -> dict:
    length_bytes = conn.recv(4, timeout=timeout)
    (length,) = struct.unpack(">I", length_bytes)
    payload = conn.recv(length, timeout=timeout)
    return plistlib.loads(payload)


class LockdownError(Exception):
    pass


class LockdownRstError(LockdownError):
    """Thiết bị gửi RST khi nhận plaintext lockdownd — thường là iOS 16+
    yêu cầu SSL ngay từ đầu. Caller nên retry với use_ssl=True."""
    pass


# ─────────────────────────────────────────────────────────────────────────
# SSL pipe cho iOS 16+ (lockdownd yêu cầu mTLS trước QueryType)
# ─────────────────────────────────────────────────────────────────────────

def _generate_temp_ssl_cert():
    """Tạo RSA 2048 key + self-signed cert tạm thời cho SSL handshake với
    iOS 16.7+ lockdownd.

    iOS 16.7 dùng mTLS (mutual TLS): client PHẢI gửi certificate trong
    ClientHello, ngay cả khi chưa có pair record. Nếu ClientHello không
    chứa Certificate extension, lockdownd gửi RST ngay lập tức (đây là bug
    chính trong v6: _SslPipe anonymous không load cert nào → RST).

    Cert này là self-signed, Apple không verify nội dung cert client —
    họ chỉ kiểm tra sự hiện diện của cert trong handshake. Sau khi pair
    thành công, cert thật (từ pair record) được dùng cho các kết nối tiếp theo.
    """
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    import datetime

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([]))
        .issuer_name(x509.Name([]))
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=1))
        .not_valid_after(now + datetime.timedelta(days=365))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    cert_pem = cert.public_bytes(serialization.Encoding.PEM)
    key_pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    )
    return cert_pem, key_pem


class _SslPipe:
    """Bọc MuxConnection trong SSL dùng MemoryBIO — không cần socket thật.

    iOS 16+ lockdownd yêu cầu SSL ngay sau khi TCP kết nối được thiết lập,
    trước bất kỳ lệnh plist nào (kể cả QueryType). iOS 16.7 cụ thể dùng
    mTLS: client phải gửi certificate ngay trong ClientHello.

    pair_record: nếu có, dùng HostCertificate/HostPrivateKey làm client cert.
    Nếu None (pair lần đầu, chưa có pair record): tự sinh temp RSA cert.
    """

    def __init__(self, conn: MuxConnection, pair_record: dict = None):
        self.conn = conn
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.maximum_version = ssl.TLSVersion.TLSv1_3
        if ssl.OPENSSL_VERSION.lower().startswith("openssl"):
            ctx.set_ciphers("ALL:!aNULL:!eNULL:@SECLEVEL=0")
        ctx.options |= getattr(ssl, "OP_LEGACY_SERVER_CONNECT", 0x4)

        # [FIX v7 CRITICAL] iOS 16.7 dùng mTLS — client PHẢI có cert.
        # v6 bỏ qua bước này khi pair_record=None → ClientHello không có cert
        # → lockdownd gửi RST ngay. Giải pháp: nếu không có pair_record, tự
        # sinh temp RSA self-signed cert chỉ cho SSL handshake.
        if pair_record:
            cert_data = pair_record.get("HostCertificate", b"")
            key_data = pair_record.get("HostPrivateKey", b"")
            if isinstance(cert_data, str):
                cert_data = cert_data.encode()
            if isinstance(key_data, str):
                key_data = key_data.encode()
            print("[lockdown] SSL: dùng HostCertificate từ pair record làm client cert.")
        else:
            print("[lockdown] SSL: sinh temp RSA cert cho iOS 16.7 mTLS handshake...")
            cert_data, key_data = _generate_temp_ssl_cert()
            print("[lockdown] SSL: ✅ Đã sinh temp RSA cert.")

        with tempfile.TemporaryDirectory() as tmp:
            cert_path = os.path.join(tmp, "host.pem")
            key_path = os.path.join(tmp, "host.key")
            with open(cert_path, "wb") as f:
                f.write(cert_data)
            with open(key_path, "wb") as f:
                f.write(key_data)
            ctx.load_cert_chain(cert_path, key_path)

        self._incoming = ssl.MemoryBIO()
        self._outgoing = ssl.MemoryBIO()
        self._ssl_obj = ctx.wrap_bio(self._incoming, self._outgoing, server_hostname=None)
        self._do_handshake()

    def _flush_outgoing(self):
        """Gửi tất cả dữ liệu TLS outgoing (ClientHello, data, v.v.) qua MuxConnection."""
        out = self._outgoing.read()
        if out:
            self.conn.send(out)

    def _do_handshake(self):
        """Thực hiện TLS handshake bằng cách bơm tay dữ liệu qua MuxConnection."""
        print("[lockdown] Đang thực hiện TLS handshake với lockdownd (iOS 16+)...")
        for attempt in range(30):
            try:
                self._ssl_obj.do_handshake()
                # Handshake hoàn tất — flush dữ liệu cuối cùng (Finished message)
                self._flush_outgoing()
                print("[lockdown] ✅ TLS handshake với lockdownd thành công (iOS 16+ mode).")
                return
            except ssl.SSLWantReadError:
                self._flush_outgoing()
                try:
                    raw = self.conn.recv(4096, timeout=10.0)
                    if raw:
                        self._incoming.write(raw)
                except MuxRstError as e:
                    raise LockdownRstError(f"Thiết bị gửi RST trong TLS handshake: {e}")
                except MuxError as e:
                    raise LockdownError(f"Lỗi kết nối trong TLS handshake: {e}")
            except ssl.SSLError as e:
                raise LockdownError(f"TLS handshake thất bại: {e}")
        raise LockdownError("TLS handshake với lockdownd không hoàn tất sau nhiều lần thử.")

    def write(self, data: bytes):
        """Mã hoá và gửi data qua SSL → MuxConnection."""
        self._ssl_obj.write(data)
        self._flush_outgoing()

    def read(self, size: int, timeout=30.0) -> bytes:
        """Đọc và giải mã đúng size byte từ MuxConnection → SSL."""
        buf = b""
        deadline = time.time() + timeout
        while len(buf) < size:
            # Thử đọc từ decrypt buffer SSL trước
            try:
                chunk = self._ssl_obj.read(size - len(buf))
                if chunk:
                    buf += chunk
                    continue
            except ssl.SSLWantReadError:
                pass
            # Cần thêm dữ liệu từ mạng
            remaining = deadline - time.time()
            if remaining <= 0:
                raise LockdownError(f"Timeout khi đọc {size} byte qua SSL từ lockdownd.")
            try:
                raw = self.conn.recv(4096, timeout=min(10.0, remaining))
                if raw:
                    self._incoming.write(raw)
            except MuxRstError as e:
                raise LockdownRstError(f"Kết nối SSL bị RST: {e}")
            except MuxError as e:
                raise LockdownError(f"Lỗi đọc qua SSL: {e}")
        return buf


# ─────────────────────────────────────────────────────────────────────────
# LockdownClient
# ─────────────────────────────────────────────────────────────────────────

class LockdownClient:
    """Kết nối lockdown — hỗ trợ cả plaintext (iOS < 16) và SSL (iOS 16+).

    use_ssl=False (mặc định): plaintext plist — hoạt động với iOS < 16.
    use_ssl=True: bọc MuxConnection trong SSL ngay sau khi TCP kết nối —
        bắt buộc cho iOS 16+ (lockdownd gửi RST nếu nhận plaintext).

    ssl_pair_record: pair record đã lưu để làm client cert trong SSL handshake.
        Có thể None nếu chưa paired (dùng anonymous SSL lần đầu).
    """

    def __init__(self, use_ssl: bool = False, ssl_pair_record: dict = None):
        self.device = get_device()
        ssl_label = " (SSL/iOS 16+)" if use_ssl else ""
        print(f"[lockdown] Đang mở kết nối lockdownd{ssl_label} (cổng {LOCKDOWN_PORT})...")
        self.conn = self.device.connect(LOCKDOWN_PORT)
        print(f"[lockdown] ✅ Đã kết nối lockdownd{ssl_label}.")

        self._ssl_pipe: _SslPipe | None = None
        if use_ssl:
            try:
                self._ssl_pipe = _SslPipe(self.conn, ssl_pair_record)
            except LockdownRstError:
                raise
            except LockdownError:
                raise
            except Exception as e:
                raise LockdownError(f"Không thiết lập được SSL cho lockdownd: {e}")

        # QueryType — bắt buộc phải gửi làm lệnh đầu tiên (iOS 14+).
        # libimobiledevice lockdown.c: lockdownd_client_new_with_handshake()
        # pymobiledevice3 lockdown.py: LockdownClient.__init__() → query_type()
        # Nếu thiếu QueryType, lockdownd iOS 14+ im lặng bỏ qua GetValue/Pair
        # → Trust popup không bao giờ xuất hiện.
        try:
            qt_resp = self._request_raw(
                {"Request": "QueryType", "Label": "SuperAlphaSideload"},
                timeout=10.0,
            )
            svc_type = qt_resp.get("Type", "?")
            print(f"[lockdown] QueryType OK — dịch vụ: {svc_type}")
        except LockdownRstError:
            # RST ngay khi gửi QueryType → iOS 16+ cần SSL nhưng đang chạy
            # plaintext (use_ssl=False). Re-raise để _open_lockdown() retry SSL.
            raise
        except Exception as _qt_err:
            # Thiết bị cũ (iOS < 5) không có QueryType — không fail toàn bộ.
            print(f"[lockdown] Cảnh báo QueryType: {_qt_err} — tiếp tục...")

    # ── Gửi/nhận plist (tự động dùng SSL nếu _ssl_pipe có mặt) ──────────

    def _send_plist_enc(self, obj: dict):
        payload = plistlib.dumps(obj, fmt=plistlib.FMT_XML)
        framed = struct.pack(">I", len(payload)) + payload
        if self._ssl_pipe:
            self._ssl_pipe.write(framed)
        else:
            self.conn.send(framed)

    def _recv_plist_enc(self, timeout: float = 15.0) -> dict:
        if self._ssl_pipe:
            length_bytes = self._ssl_pipe.read(4, timeout=timeout)
            (length,) = struct.unpack(">I", length_bytes)
            payload = self._ssl_pipe.read(length, timeout=timeout)
            return plistlib.loads(payload)
        return _recv_plist(self.conn, timeout=timeout)

    def _request_raw(self, request: dict, timeout: float = 30.0) -> dict:
        """Gửi + nhận plist, KHÔNG kiểm tra trường Error.
        Dùng nội bộ cho QueryType và pair đầu tiên."""
        try:
            self._send_plist_enc(request)
            return self._recv_plist_enc(timeout=timeout)
        except MuxRstError as e:
            raise LockdownRstError(
                f"Thiết bị gửi RST (kết nối bị đóng) — có thể cần SSL (iOS 16+): {e}"
            )

    def _request(self, request: dict, timeout: float = 30.0) -> dict:
        response = self._request_raw(request, timeout=timeout)
        if response.get("Error"):
            raise LockdownError(f"lockdownd trả lỗi: {response.get('Error')}")
        return response

    def query_type(self):
        return self._request({"Request": "QueryType", "Label": "SuperAlphaSideload"})

    def get_value(self, key=None, domain=None):
        req = {"Request": "GetValue", "Label": "SuperAlphaSideload"}
        if domain:
            req["Domain"] = domain
        if key:
            req["Key"] = key
        response = self._request(req)
        return response.get("Value")

    def start_service(self, service_name: str) -> dict:
        response = self._request({
            "Request": "StartService",
            "Service": service_name,
            "Label": "SuperAlphaSideload",
        })
        port = response.get("Port")
        if not port:
            raise LockdownError(f"Không lấy được cổng cho dịch vụ {service_name}: {response}")
        return response

    def close(self):
        try:
            self.conn.close()
        except Exception:
            pass


# ─────────────────────────────────────────────────────────────────────────
# Factory: tự động retry với SSL nếu plaintext bị RST (iOS 16+)
# ─────────────────────────────────────────────────────────────────────────

def _open_lockdown(pair_record: dict = None) -> LockdownClient:
    """Mở LockdownClient với SSL fallback tự động cho iOS 16+.

    Thử plaintext trước. Nếu nhận LockdownRstError (thiết bị gửi RST ngay),
    thử lại với SSL — dùng pair_record làm client cert nếu có, nếu không
    _SslPipe sẽ tự sinh temp RSA cert cho iOS 16.7 mTLS (FIX v7).

    iOS 16.7 cụ thể: plaintext → RST; SSL without cert → RST; SSL with cert → ✅.
    """
    try:
        return LockdownClient(use_ssl=False)
    except LockdownRstError as e:
        print(f"[lockdown] Plaintext bị RST: {e}")
        print("[lockdown] iOS 16+ phát hiện — thử SSL với client cert (iOS 16.7 mTLS)...")
        return LockdownClient(use_ssl=True, ssl_pair_record=pair_record)


# ─────────────────────────────────────────────────────────────────────────
# Pairing
# ─────────────────────────────────────────────────────────────────────────

def _select_hash_algorithm(device_version: str | None):
    from cryptography.hazmat.primitives import hashes
    if not device_version:
        return hashes.SHA256()
    try:
        parts = tuple(int(x) for x in device_version.split("."))
    except (ValueError, AttributeError):
        return hashes.SHA256()
    return hashes.SHA1() if parts < (4, 0, 0) else hashes.SHA256()


def _generate_host_identity(device_public_key_pem: bytes, device_version: str | None = None):
    """Sinh chuỗi chứng chỉ Root CA → Host cert → Device cert cho pairing.
    Khớp chính xác cấu trúc mà lockdownd kỳ vọng (đối chiếu với
    libimobiledevice common/userpref.c và pymobiledevice3 ca.py)."""
    from cryptography import x509
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicKey
    from cryptography.hazmat.primitives.serialization import load_pem_public_key, load_der_public_key
    import datetime

    alg = _select_hash_algorithm(device_version)
    empty_name = x509.Name([])
    not_before = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=1)
    not_after = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=3650)

    key_usage = x509.KeyUsage(
        digital_signature=True, key_encipherment=True, key_cert_sign=False,
        crl_sign=False, content_commitment=False, data_encipherment=False,
        key_agreement=False, encipher_only=False, decipher_only=False,
    )

    root_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    root_cert = (
        x509.CertificateBuilder()
        .subject_name(empty_name).issuer_name(empty_name)
        .public_key(root_key.public_key()).serial_number(1)
        .not_valid_before(not_before).not_valid_after(not_after)
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(root_key, alg)
    )

    host_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    host_cert = (
        x509.CertificateBuilder()
        .subject_name(empty_name).issuer_name(root_cert.subject)
        .public_key(host_key.public_key()).serial_number(1)
        .not_valid_before(not_before).not_valid_after(not_after)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(key_usage, critical=True)
        .sign(root_key, alg)
    )

    device_public_key = None
    try:
        device_public_key = load_pem_public_key(device_public_key_pem)
    except (ValueError, TypeError, UnicodeDecodeError):
        try:
            device_public_key = load_der_public_key(device_public_key_pem)
        except Exception as e2:
            raise LockdownError(f"Không parse được DevicePublicKey (thử PEM và DER đều thất bại): {e2}")
    if not isinstance(device_public_key, RSAPublicKey):
        raise LockdownError("DevicePublicKey trả về từ thiết bị không phải khóa RSA hợp lệ.")

    device_cert = (
        x509.CertificateBuilder()
        .subject_name(empty_name).issuer_name(root_cert.subject)
        .public_key(device_public_key).serial_number(1)
        .not_valid_before(not_before).not_valid_after(not_after)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(key_usage, critical=True)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(device_public_key), critical=False)
        .sign(root_key, alg)
    )

    def pem(cert_or_key, is_key=False):
        if is_key:
            return cert_or_key.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.TraditionalOpenSSL,
                encryption_algorithm=serialization.NoEncryption(),
            )
        return cert_or_key.public_bytes(serialization.Encoding.PEM)

    def der(cert):
        return cert.public_bytes(serialization.Encoding.DER)

    return {
        "root_key_pem": pem(root_key, is_key=True),
        "root_cert_der": der(root_cert),
        "host_cert_der": der(host_cert),
        "device_cert_der": der(device_cert),
        "root_cert_pem": pem(root_cert),
        "host_key_pem": pem(host_key, is_key=True),
        "host_cert_pem": pem(host_cert),
        "device_cert_pem": pem(device_cert),
    }


def pair_device(udid: str) -> dict:
    """Thực hiện pairing lần đầu với thiết bị. Thiết bị sẽ hiện hộp thoại
    "Trust This Computer?" — người dùng cần bấm Trust để tiếp tục.

    Tự động dùng SSL nếu iOS 16+ (plaintext bị RST)."""
    # _open_lockdown() tự động thử plaintext rồi SSL nếu cần.
    lockdown = _open_lockdown(pair_record=None)
    try:
        print("[pairing] Đang lấy DevicePublicKey từ lockdownd...")
        device_public_key_response = None
        for _gpk_attempt in range(2):
            try:
                device_public_key_response = lockdown.get_value(key="DevicePublicKey")
                if device_public_key_response:
                    break
            except LockdownError as _gpk_err:
                if _gpk_attempt == 0:
                    print(f"[pairing] GetValue DevicePublicKey lần 1 thất bại ({_gpk_err}) — thử lại sau 1s...")
                    time.sleep(1.0)
                else:
                    raise
        if not device_public_key_response:
            raise LockdownError(
                "Không lấy được DevicePublicKey sau 2 lần thử.\n"
                "Nguyên nhân thường gặp:\n"
                "  • iPhone đang bị khoá màn hình — mở khoá rồi thử lại.\n"
                "  • Cáp USB kém chất lượng — thử cổng/cáp khác.\n"
                "  • iPhone đã bị disable (quá nhiều lần nhập sai mật mã).\n"
                "Nếu các lần trước đã Trust nhưng bị xoá: cắm lại cáp và thử lại."
            )
        print("[pairing] Đã có DevicePublicKey — đang tạo chuỗi chứng chỉ host...")

        device_version = None
        try:
            device_version = lockdown.get_value(key="ProductVersion")
        except Exception:
            pass

        # Lấy WiFiAddress TRƯỚC khi gửi Pair (iOS 7 tự ngắt kết nối nếu lấy sau)
        wifi_mac_address = None
        try:
            wifi_mac_address = lockdown.get_value(key="WiFiAddress")
        except Exception:
            pass

        identity = _generate_host_identity(device_public_key_response, device_version)

        host_id = str(uuid.uuid4()).upper()
        system_buid = str(uuid.uuid4()).upper()

        pair_record = {
            "PairRecordID": host_id,
            "HostID": host_id,
            "SystemBUID": system_buid,
            "DeviceCertificate": identity["device_cert_pem"],
            "HostCertificate": identity["host_cert_pem"],
            "RootCertificate": identity["root_cert_pem"],
            "RootPrivateKey": identity["root_key_pem"],
            "HostPrivateKey": identity["host_key_pem"],
        }

        # PairRecord gửi lên thiết bị PHẢI dùng PEM (ASCII text).
        # libimobiledevice lockdown.c hàm lockdownd_pair_record_to_plist():
        # plist_new_data(pair_record->device_certificate, strlen(cert)) —
        # strlen() chỉ đúng với PEM, DER binary sẽ bị cắt ngắn tại byte 0x00.
        request = {
            "Request": "Pair",
            "Label": "SuperAlphaSideload",
            "PairRecord": {
                "DeviceCertificate": identity["device_cert_pem"],
                "HostCertificate": identity["host_cert_pem"],
                "RootCertificate": identity["root_cert_pem"],
                "HostID": host_id,
                "SystemBUID": system_buid,
            },
            "ProtocolVersion": "2",
            "PairingOptions": {"ExtendedPairingErrors": True},
        }

        print("[pairing] Đang gửi yêu cầu ghép nối...")
        print("[pairing] *** Kiểm tra màn hình iPhone — bấm 'Tin cậy' (Trust This Computer) ***")
        print("[pairing] iPhone PHẢI còn sáng màn hình và chưa bị khoá trong bước này.")
        print("[pairing] Bạn có tối đa 60 giây để bấm Trust sau khi popup xuất hiện.")

        lockdown._send_plist_enc(request)

        MAX_PENDING = 12
        PENDING_WAIT = 5.0
        response = None
        for attempt in range(MAX_PENDING):
            try:
                response = lockdown._recv_plist_enc(timeout=60.0)
            except Exception as exc:
                raise LockdownError(
                    f"Timeout 60s chờ phản hồi Pair: {exc}\n"
                    "Đảm bảo iPhone không bị khoá màn hình và hãy bấm Trust khi được hỏi."
                )
            err = response.get("Error", "")
            if err == "PairingDialogResponsePending":
                if attempt == 0:
                    print("[pairing] Thiết bị đang hiện hộp thoại Trust — đang chờ bạn bấm...")
                time.sleep(PENDING_WAIT)
                lockdown._send_plist_enc(request)
                continue
            elif err == "UserDeniedPairing":
                raise LockdownError(
                    "Bạn đã bấm 'Không tin cậy' (Don't Trust) trên iPhone.\n"
                    "Ngắt và cắm lại USB, rồi bấm 'Ký & Cài đặt' lại — lần này hãy bấm 'Tin cậy'."
                )
            elif err == "PasswordProtected":
                raise LockdownError(
                    "iPhone đang bị khoá bằng mã PIN / mật khẩu.\n"
                    "Hãy mở khoá màn hình iPhone (nhập mã PIN), sau đó thử lại."
                )
            elif err == "InvalidHostID":
                raise LockdownError(
                    "Thiết bị báo InvalidHostID. Xoá pair record cũ và thử lại."
                )
            elif err:
                print(f"[pairing] lockdownd trả lỗi không xác định: {err!r} — phản hồi đầy đủ: {response}")
                raise LockdownError(f"Pairing bị từ chối bởi thiết bị: {err}")
            break

        if response is None or (response.get("Result") != "Success" and "EscrowBag" not in response):
            raise LockdownError(f"Pairing thất bại hoặc bị từ chối: {response}")

        pair_record["EscrowBag"] = response.get("EscrowBag")
        if wifi_mac_address:
            pair_record["WiFiMACAddress"] = wifi_mac_address
        print("[pairing] ✅ Ghép nối thành công.")
        return pair_record
    finally:
        lockdown.close()


# ─────────────────────────────────────────────────────────────────────────
# TLS session (StartSession) — dùng sau khi đã paired
# ─────────────────────────────────────────────────────────────────────────

def start_session_tls(pair_record: dict) -> "TlsLockdownClient":
    """Mở LockdownClient mới rồi StartSession + nâng cấp TLS bằng cặp
    chứng chỉ đã lưu trong pair_record. Tự động dùng SSL nếu iOS 16+."""
    # Truyền pair_record để _SslPipe dùng làm client cert nếu cần SSL
    lockdown = _open_lockdown(pair_record=pair_record)
    response = lockdown._request({
        "Request": "StartSession",
        "Label": "SuperAlphaSideload",
        "HostID": pair_record["HostID"],
        "SystemBUID": pair_record["SystemBUID"],
    })
    if response.get("EnableSessionSSL"):
        return TlsLockdownClient(lockdown, pair_record, response.get("SessionID"))
    return TlsLockdownClient(lockdown, pair_record, response.get("SessionID"), tls=False)


class TlsLockdownClient:
    """Bọc LockdownClient đã StartSession trong TLS (nếu EnableSessionSSL=True),
    dùng ssl.MemoryBIO để bơm tay qua MuxConnection.

    Khi LockdownClient đã có _ssl_pipe (iOS 16+ mode), TLS session được tạo
    TRÊN TOP của SSL layer hiện có — đây là "SSL-in-SSL" hợp lệ vì StartSession
    trả EnableSessionSSL=True yêu cầu thêm một lớp nữa."""

    def __init__(self, lockdown: LockdownClient, pair_record: dict, session_id, tls=True):
        self.lockdown = lockdown
        self.session_id = session_id
        self._ssl_obj = None
        if tls:
            self._wrap_tls(pair_record)

    def _wrap_tls(self, pair_record):
        # lockdownd dùng TLS rất cũ/tuỳ biến (DH yếu, legacy renegotiation).
        # OpenSSL 3.x @SECLEVEL=2 mặc định từ chối — phải hạ @SECLEVEL=0.
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.maximum_version = ssl.TLSVersion.TLSv1_3
        if ssl.OPENSSL_VERSION.lower().startswith("openssl"):
            ctx.set_ciphers("ALL:!aNULL:!eNULL:@SECLEVEL=0")
        else:
            ctx.set_ciphers("ALL:!aNULL:!eNULL")
        ctx.options |= getattr(ssl, "OP_LEGACY_SERVER_CONNECT", 0x4)

        cert_data = pair_record.get("HostCertificate", b"")
        key_data = pair_record.get("HostPrivateKey", b"")
        if isinstance(cert_data, str):
            cert_data = cert_data.encode()
        if isinstance(key_data, str):
            key_data = key_data.encode()
        with tempfile.TemporaryDirectory() as tmp:
            cert_path = os.path.join(tmp, "host.pem")
            key_path = os.path.join(tmp, "host.key")
            with open(cert_path, "wb") as f:
                f.write(cert_data)
            with open(key_path, "wb") as f:
                f.write(key_data)
            ctx.load_cert_chain(cert_path, key_path)

        self._incoming = ssl.MemoryBIO()
        self._outgoing = ssl.MemoryBIO()
        self._ssl_obj = ctx.wrap_bio(self._incoming, self._outgoing, server_hostname=None)
        self._pump_handshake()

    def _pump_handshake(self):
        for _ in range(20):
            try:
                self._ssl_obj.do_handshake()
                return
            except ssl.SSLWantReadError:
                self._outgoing_flush()
                data = self.lockdown.conn.recv(4096, timeout=10)
                self._incoming.write(data)
        raise LockdownError("TLS handshake với lockdownd không hoàn tất sau nhiều lần thử.")

    def _outgoing_flush(self):
        chunk = self._outgoing.read()
        if chunk:
            self.lockdown.conn.send(chunk)

    def send_plist(self, obj: dict):
        payload = plistlib.dumps(obj, fmt=plistlib.FMT_XML)
        framed = struct.pack(">I", len(payload)) + payload
        if self._ssl_obj:
            self._ssl_obj.write(framed)
            self._outgoing_flush()
        else:
            self.lockdown.conn.send(framed)

    def recv_plist(self, timeout=15.0) -> dict:
        if self._ssl_obj:
            while True:
                try:
                    length_bytes = self._ssl_obj.read(4)
                    if len(length_bytes) == 4:
                        break
                except ssl.SSLWantReadError:
                    pass
                data = self.lockdown.conn.recv(4096, timeout=timeout)
                self._incoming.write(data)
            (length,) = struct.unpack(">I", length_bytes)
            payload = b""
            while len(payload) < length:
                try:
                    payload += self._ssl_obj.read(length - len(payload))
                except ssl.SSLWantReadError:
                    data = self.lockdown.conn.recv(4096, timeout=timeout)
                    self._incoming.write(data)
            return plistlib.loads(payload)
        return _recv_plist(self.lockdown.conn, timeout=timeout)

    def close(self):
        self.lockdown.close()


# ─────────────────────────────────────────────────────────────────────────
# installation_proxy
# ─────────────────────────────────────────────────────────────────────────

def install_ipa(pair_record: dict, device_ipa_path: str, progress_cb=None):
    """Gửi lệnh Install tới com.apple.mobile.installation_proxy."""
    tls = start_session_tls(pair_record)
    try:
        service_response = tls.lockdown._request({
            "Request": "StartService",
            "Service": "com.apple.mobile.installation_proxy",
            "Label": "SuperAlphaSideload",
        })
        port = service_response["Port"]
        device = get_device()
        conn = device.connect(port)
        try:
            _send_plist(conn, {
                "Command": "Install",
                "PackagePath": device_ipa_path,
                "ClientOptions": {"PackageType": "Developer"},
            })
            while True:
                response = _recv_plist(conn, timeout=60)
                status = response.get("Status")
                percent = response.get("PercentComplete", 0)
                if progress_cb:
                    progress_cb(percent, status or "")
                print(f"[install] {status} ({percent}%)")
                if status in ("Complete",):
                    return True
                if response.get("Error"):
                    raise LockdownError(f"Cài đặt thất bại: {response.get('Error')} — {response.get('ErrorDescription', '')}")
        finally:
            conn.close()
    finally:
        tls.close()


def list_installed_apps(pair_record: dict) -> list:
    """Trả về danh sách bundle identifier đã cài trên máy."""
    tls = start_session_tls(pair_record)
    try:
        service_response = tls.lockdown._request({
            "Request": "StartService",
            "Service": "com.apple.mobile.installation_proxy",
            "Label": "SuperAlphaSideload",
        })
        conn = get_device().connect(service_response["Port"])
        try:
            _send_plist(conn, {
                "Command": "Browse",
                "ClientOptions": {"ReturnAttributes": ["CFBundleIdentifier"]},
            })
            bundle_ids = []
            while True:
                response = _recv_plist(conn, timeout=30)
                for entry in response.get("CurrentList", []) or []:
                    bid = entry.get("CFBundleIdentifier")
                    if bid:
                        bundle_ids.append(bid)
                if response.get("Status") == "Complete":
                    break
            return bundle_ids
        finally:
            conn.close()
    finally:
        tls.close()


# ─────────────────────────────────────────────────────────────────────────
# AFC
# ─────────────────────────────────────────────────────────────────────────

AFC_MAGIC = b"CFA6LPAA"
AFC_HEADER_FMT = "<8sQQQQ"  # magic, entire_len, this_len, packet_num, operation
AFC_HEADER_LEN = struct.calcsize(AFC_HEADER_FMT)

AFC_OP_STATUS = 0x00000001
AFC_OP_DATA = 0x00000002
AFC_OP_MAKE_DIR = 0x00000009
AFC_OP_FILE_OPEN = 0x0000000D
AFC_OP_FILE_CLOSE = 0x00000014
AFC_OP_FILE_WRITE = 0x00000010

AFC_FOPEN_WRONLY = 0x00000003


class AfcClient:
    def __init__(self, pair_record: dict):
        self.tls = start_session_tls(pair_record)
        service_response = self.tls.lockdown._request({
            "Request": "StartService",
            "Service": "com.apple.afc",
            "Label": "SuperAlphaSideload",
        })
        self.conn = get_device().connect(service_response["Port"])
        self._packet_num = 0

    def _send_packet(self, operation: int, header_payload: bytes, data_payload: bytes = b""):
        this_len = AFC_HEADER_LEN + len(header_payload)
        entire_len = this_len + len(data_payload)
        header = struct.pack(AFC_HEADER_FMT, AFC_MAGIC, entire_len, this_len, self._packet_num, operation)
        self._packet_num += 1
        self.conn.send(header + header_payload + data_payload)

    def _recv_packet(self, timeout=30.0):
        header = self.conn.recv(AFC_HEADER_LEN, timeout=timeout)
        magic, entire_len, this_len, packet_num, operation = struct.unpack(AFC_HEADER_FMT, header)
        if magic != AFC_MAGIC:
            raise LockdownError("Gói tin AFC không hợp lệ (sai magic).")
        header_payload_len = this_len - AFC_HEADER_LEN
        header_payload = self.conn.recv(header_payload_len, timeout=timeout) if header_payload_len else b""
        data_len = entire_len - this_len
        data_payload = self.conn.recv(data_len, timeout=timeout) if data_len else b""
        return operation, header_payload, data_payload

    def push_file(self, local_path: str, remote_path: str, progress_cb=None):
        self._send_packet(AFC_OP_FILE_OPEN, struct.pack("<Q", AFC_FOPEN_WRONLY) + remote_path.encode() + b"\x00")
        operation, header_payload, _ = self._recv_packet()
        if operation != AFC_OP_FILE_OPEN and operation != AFC_OP_STATUS:
            raise LockdownError(f"AFC file_open trả về operation lạ: {operation}")
        (file_handle,) = struct.unpack("<Q", header_payload[:8])

        total = 0
        file_size = os.path.getsize(local_path)
        with open(local_path, "rb") as f:
            while True:
                chunk = f.read(1 << 16)
                if not chunk:
                    break
                self._send_packet(AFC_OP_FILE_WRITE, struct.pack("<Q", file_handle), chunk)
                self._recv_packet()
                total += len(chunk)
                if progress_cb:
                    progress_cb(total, file_size)

        self._send_packet(AFC_OP_FILE_CLOSE, struct.pack("<Q", file_handle))
        self._recv_packet()

    def close(self):
        try:
            self.conn.close()
        except Exception:
            pass
        self.tls.close()


def afc_push_ipa(pair_record: dict, local_ipa_path: str, remote_filename: str, progress_cb=None) -> str:
    """Đẩy file IPA lên /PublicStaging/<remote_filename> trên thiết bị."""
    afc = AfcClient(pair_record)
    try:
        remote_path = f"PublicStaging/{remote_filename}"
        try:
            afc._send_packet(AFC_OP_MAKE_DIR, b"PublicStaging\x00")
            afc._recv_packet()
        except Exception:
            pass  # thư mục có thể đã tồn tại
        afc.push_file(local_ipa_path, remote_path, progress_cb=progress_cb)
        return f"/{remote_path}"
    finally:
        afc.close()


# ─────────────────────────────────────────────────────────────────────────
# Tiện ích cho sideload_core.py
# ─────────────────────────────────────────────────────────────────────────

def reset_mux_device():
    """Huỷ singleton MuxDevice hiện tại. Gọi ở đầu mỗi lần chạy do_sideload()."""
    from mux_usb import reset_device as _mux_reset
    _mux_reset()


def validate_pair_record(pair_record: dict) -> bool:
    """Kiểm tra nhanh pair record: đảm bảo các trường bắt buộc có mặt và không rỗng."""
    required_keys = ["HostID", "SystemBUID", "HostCertificate", "HostPrivateKey",
                     "RootCertificate", "RootPrivateKey"]
    for key in required_keys:
        val = pair_record.get(key)
        if not val:
            print(f"[pairing] Pair record thiếu hoặc trống trường '{key}' — cần ghép nối lại.")
            return False
    if not pair_record.get("EscrowBag"):
        print("[pairing] Pair record không có EscrowBag (iOS 7+) — cần ghép nối lại.")
        return False
    return True
