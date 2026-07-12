"""device_link.py — client tối giản cho lockdownd / pairing / AFC /
installation_proxy, xây trên mux_usb.py.

Thay thế các lệnh shellout `idevice_id` / `ideviceinstaller` (libimobiledevice,
không có sẵn trên Android) của bản Termux gốc bằng cách tự nói chuyện với
iPhone qua giao thức lockdown thật. Cũng CHƯA được kiểm chứng trên phần cứng
thật — xem cảnh báo ở đầu mux_usb.py, áp dụng tương tự cho toàn bộ file này.

Các phần triển khai, xếp theo độ tin cậy (dựa trên tài liệu công khai):
  - Lockdown QueryType / GetValue / StartService: định dạng bản tin là plist
    XML có tiền tố 4-byte big-endian độ dài — được ghi lại rộng rãi trong
    nhiều dự án mã nguồn mở độc lập, độ tin cậy cao.
  - Pairing (trao đổi certificate) + StartSession + nâng cấp TLS: định dạng
    plist các trường (DeviceCertificate/HostCertificate/RootCertificate/...)
    cũng được ghi lại công khai (độ tin cậy trung bình-cao), nhưng phần bọc
    TLS qua ssl.MemoryBIO bơm tay từng byte qua mux_usb là phần tự ráp nối,
    ĐỘ TIN CẬY THẤP HƠN — nhiều khả năng cần chỉnh sửa khi test thật.
  - AFC (đẩy file lên /PublicStaging) và installation_proxy (Install): định
    dạng gói tin cũng là tài liệu công khai, độ tin cậy trung bình.
"""

import plistlib
import ssl
import struct
import time
import uuid

from mux_usb import get_device, MuxConnection

LOCKDOWN_PORT = 62078


# ─────────────────────────────────────────────────────────────────────────
# UDID
# ─────────────────────────────────────────────────────────────────────────

def get_udid_from_usb():
    """Đọc UDID trực tiếp từ USB serial descriptor của thiết bị — không cần
    mở kết nối mux/lockdown nào cả. Hầu hết iPhone/iPad trả về đúng UDID 40
    ký tự (hoặc 24+16 ký tự có dấu gạch ngang trên các model mới) ở đây."""
    # Không có Context trực tiếp trong Python; UDID thật sự được cung cấp
    # qua sideload_core.set_current_udid() khi Kotlin mở kết nối USB (xem
    # SideloadScreen/UsbPermissionManager — UsbDevice.serialNumber đọc ở đó,
    # nơi có sẵn Context, rồi truyền UDID sang Python một lần).
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


class LockdownClient:
    """Kết nối lockdown cơ bản (không TLS) — đủ cho QueryType/GetValue/
    StartService trên nhiều phiên bản iOS. Một số phiên bản iOS mới hơn có
    thể yêu cầu StartSession (TLS) trước khi cho phép GetValue/StartService
    — nếu gặp lỗi permission, hãy thử pair()+start_session() trước."""

    def __init__(self):
        self.device = get_device()
        self.conn = self.device.connect(LOCKDOWN_PORT)

    def _request(self, request: dict) -> dict:
        _send_plist(self.conn, request)
        response = _recv_plist(self.conn)
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
        """Yêu cầu lockdownd khởi động một dịch vụ (vd
        com.apple.mobile.installation_proxy) và trả về cổng TCP ảo mới để
        kết nối tới dịch vụ đó."""
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
# Pairing — cần thiết cho AFC / installation_proxy trên phần lớn phiên bản
# iOS. Người dùng phải bấm "Trust" (Tin cậy) trên màn hình iPhone khi được
# hỏi lần đầu — hành vi này giống hệt khi cắm iPhone vào máy Mac/PC lần đầu.
# ─────────────────────────────────────────────────────────────────────────

def _select_hash_algorithm(device_version: str | None):
    """Chọn thuật toán băm để ký chuỗi chứng chỉ pairing — khớp hành vi thật
    của lockdownd/idevicepair: SHA-1 cho thiết bị iOS < 4.0.0 (rất hiếm gặp
    ngày nay), SHA-256 cho mọi phiên bản còn lại. Xem
    common/userpref.c::pair_record_generate_keys_and_certs trong
    libimobiledevice."""
    from cryptography.hazmat.primitives import hashes

    if not device_version:
        return hashes.SHA256()
    try:
        parts = tuple(int(x) for x in device_version.split("."))
    except (ValueError, AttributeError):
        return hashes.SHA256()
    return hashes.SHA1() if parts < (4, 0, 0) else hashes.SHA256()


def _generate_host_identity(device_public_key_pem: bytes, device_version: str | None = None):
    """Sinh chuỗi chứng chỉ Root CA (tự ký) -> Host cert -> **Device cert**
    dùng cho pairing, khớp CHÍNH XÁC cấu trúc mà lockdownd thật kỳ vọng
    (đối chiếu trực tiếp với common/userpref.c của libimobiledevice và với
    pymobiledevice3/ca.py — hai cách triển khai độc lập, cùng logic):

      - Root CA: subject/issuer RỖNG (x509.Name([])), serial=1, tự ký,
        BasicConstraints CA:TRUE (critical).
      - Host cert (lá): subject rỗng, issuer = root, public key = khóa Host
        do MÁY NÀY sinh ra, ký bởi khóa Root — dùng làm client cert khi
        nâng cấp TLS ở start_session_tls().
      - Device cert (lá) — TRƯỚC ĐÂY BỊ THIẾU HOÀN TOÀN trong bản cũ: subject
        rỗng, issuer = root, public key = **public key của CHÍNH THIẾT BỊ**
        (nhận được từ lockdownd qua GetValue DevicePublicKey), ký bởi khóa
        Root. Đây là "DeviceCertificate" mà request Pair phải gửi lại cho
        thiết bị — thiết bị dùng nó để xác nhận rằng host đã nhận đúng public
        key của mình. Gửi thẳng DevicePublicKey thô (như bản cũ) thay vì
        DeviceCertificate đã ký khiến trường bắt buộc "DeviceCertificate" bị
        thiếu trong PairRecord, nhiều khả năng khiến lockdownd từ chối yêu
        cầu Pair trước khi kịp hiện hộp thoại "Trust" trên iPhone.
    """
    from cryptography import x509
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicKey
    from cryptography.hazmat.primitives.serialization import load_pem_public_key
    import datetime

    alg = _select_hash_algorithm(device_version)
    empty_name = x509.Name([])
    not_before = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=1)
    not_after = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=3650)

    key_usage = x509.KeyUsage(
        digital_signature=True,
        key_encipherment=True,
        key_cert_sign=False,
        crl_sign=False,
        content_commitment=False,
        data_encipherment=False,
        key_agreement=False,
        encipher_only=False,
        decipher_only=False,
    )

    root_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    root_cert = (
        x509.CertificateBuilder()
        .subject_name(empty_name)
        .issuer_name(empty_name)
        .public_key(root_key.public_key())
        .serial_number(1)
        .not_valid_before(not_before)
        .not_valid_after(not_after)
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(root_key, alg)
    )

    host_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    host_cert = (
        x509.CertificateBuilder()
        .subject_name(empty_name)
        .issuer_name(root_cert.subject)
        .public_key(host_key.public_key())
        .serial_number(1)
        .not_valid_before(not_before)
        .not_valid_after(not_after)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(key_usage, critical=True)
        .sign(root_key, alg)
    )

    device_public_key = load_pem_public_key(device_public_key_pem)
    if not isinstance(device_public_key, RSAPublicKey):
        raise LockdownError("DevicePublicKey trả về từ thiết bị không phải khóa RSA hợp lệ.")
    device_cert = (
        x509.CertificateBuilder()
        .subject_name(empty_name)
        .issuer_name(root_cert.subject)
        .public_key(device_public_key)
        .serial_number(1)
        .not_valid_before(not_before)
        .not_valid_after(not_after)
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

    return {
        "root_key_pem": pem(root_key, is_key=True),
        "root_cert_pem": pem(root_cert),
        "host_key_pem": pem(host_key, is_key=True),
        "host_cert_pem": pem(host_cert),
        "device_cert_pem": pem(device_cert),
    }


def pair_device(udid: str) -> dict:
    """Thực hiện pairing lần đầu với thiết bị. Trả về "pair record" (dict)
    cần lưu lại và truyền cho start_session() ở các lần sau. Thiết bị sẽ
    hiện hộp thoại "Trust This Computer?" — cần người dùng bấm Trust rồi
    tool mới nhận được phản hồi thành công.

    PairRecord gửi đi khớp CHÍNH XÁC 5 khóa mà lockdownd_pair_record_to_plist()
    (libimobiledevice src/lockdown.c) tạo ra: DeviceCertificate,
    HostCertificate, HostID, RootCertificate, SystemBUID — không có
    DevicePublicKey (bản cũ gửi sai khóa này), không có private key nào."""
    lockdown = LockdownClient()
    try:
        device_public_key_response = lockdown.get_value(key="DevicePublicKey")
        if not device_public_key_response:
            raise LockdownError("Không lấy được DevicePublicKey — thiết bị có thể yêu cầu pairing khác quy trình chuẩn.")

        device_version = None
        try:
            device_version = lockdown.get_value(key="ProductVersion")
        except Exception:
            pass  # không bắt buộc — chỉ ảnh hưởng lựa chọn SHA-1 và cho iOS rất cũ

        # Lấy WiFiAddress TRƯỚC khi gửi Pair — lấy sau khi Pair xong khiến
        # iOS 7 tự ngắt kết nối (ghi chú y hệt trong lockdownd_do_pair()).
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

        print("[pairing] Đang gửi yêu cầu ghép nối — kiểm tra màn hình iPhone và bấm 'Trust' (Tin cậy) nếu được hỏi...")
        response = lockdown._request(request)
        if response.get("Result") != "Success" and "EscrowBag" not in response:
            raise LockdownError(f"Pairing thất bại hoặc bị từ chối trên thiết bị: {response}")

        pair_record["EscrowBag"] = response.get("EscrowBag")
        if wifi_mac_address:
            pair_record["WiFiMACAddress"] = wifi_mac_address
        print("[pairing] ✅ Ghép nối thành công.")
        return pair_record
    finally:
        lockdown.close()


def start_session_tls(pair_record: dict) -> "TlsLockdownClient":
    """Mở một LockdownClient mới rồi StartSession + nâng cấp TLS bằng cặp
    chứng chỉ đã lưu trong pair_record. Đây là phần ÍT được kiểm chứng nhất
    trong toàn bộ device_link.py (dùng ssl.MemoryBIO bơm tay qua mux_usb)."""
    lockdown = LockdownClient()
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
    """Bọc một LockdownClient đã StartSession thành công bằng TLS (nếu
    EnableSessionSSL=True), dùng ssl.MemoryBIO để không cần một socket hệ
    điều hành thật — mọi byte TLS được bơm qua MuxConnection.send()/recv()
    bằng tay trong _pump_tls()."""

    def __init__(self, lockdown: LockdownClient, pair_record: dict, session_id, tls=True):
        self.lockdown = lockdown
        self.session_id = session_id
        self._ssl_obj = None
        if tls:
            self._wrap_tls(pair_record)

    def _wrap_tls(self, pair_record):
        # lockdownd trên iPhone dùng một stack TLS rất cũ/tuỳ biến (nhóm
        # Diffie-Hellman yếu, không có các phần mở rộng hiện đại). OpenSSL
        # 3.x mặc định áp @SECLEVEL=2, sẽ từ chối handshake này thẳng thừng
        # (lỗi điển hình: "dh key too small" / "sslv3 alert handshake
        # failure" / "certificate verify failed"). Đây là vấn đề tương thích
        # đã biết rộng rãi khi nói chuyện với lockdownd bằng OpenSSL hiện đại
        # (không phải lỗi ở logic pairing) — pymobiledevice3 xử lý bằng cách
        # hạ @SECLEVEL=0 và bật lại cờ legacy renegotiation, áp dụng y hệt ở
        # đây. Thiếu đoạn này thì dù toàn bộ phần pairing ở trên đúng 100%,
        # bước nâng cấp TLS vẫn sẽ luôn thất bại trên phần cứng thật.
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

        import tempfile, os
        with tempfile.TemporaryDirectory() as tmp:
            cert_path = os.path.join(tmp, "host.pem")
            key_path = os.path.join(tmp, "host.key")
            with open(cert_path, "wb") as f:
                f.write(pair_record["HostCertificate"])
            with open(key_path, "wb") as f:
                f.write(pair_record["HostPrivateKey"])
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
# installation_proxy — cài đặt IPA đã ký
# ─────────────────────────────────────────────────────────────────────────

def install_ipa(pair_record: dict, device_ipa_path: str, progress_cb=None):
    """Gửi lệnh Install tới com.apple.mobile.installation_proxy cho file đã
    được AFC push lên /PublicStaging (xem afc_push_file). Poll tiến độ và
    gọi progress_cb(percent, status) nếu được truyền vào."""
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
    """Thay thế `ideviceinstaller list` — dùng lệnh "Browse" của
    installation_proxy, trả về danh sách bundle identifier đã cài trên máy.
    Dùng bởi sideload_core khi cần tránh trùng App ID (xem list_app_ids.py
    gốc: get_installed_app_ids_from_device)."""
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
# AFC — đẩy file IPA lên /PublicStaging trước khi cài
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
        import os
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
    """Đẩy file IPA lên /PublicStaging/<remote_filename> trên thiết bị, trả
    về đường dẫn thiết bị đầy đủ để truyền cho install_ipa()."""
    afc = AfcClient(pair_record)
    try:
        remote_path = f"PublicStaging/{remote_filename}"
        try:
            afc._send_packet(AFC_OP_MAKE_DIR, b"PublicStaging\x00")
            afc._recv_packet()
        except Exception:
            pass  # thư mục có thể đã tồn tại — bỏ qua lỗi ở bước này
        afc.push_file(local_ipa_path, remote_path, progress_cb=progress_cb)
        return f"/{remote_path}"
    finally:
        afc.close()
