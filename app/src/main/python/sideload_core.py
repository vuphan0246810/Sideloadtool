"""sideload_core.py — điểm vào duy nhất mà Kotlin (PythonBridge.kt) gọi vào.

Thay thế main.py gốc (menu CLI tương tác trong Termux) bằng 3 hàm thuần tuý
mà UI Compose gọi trực tiếp: do_sideload(), do_revoke_certs(),
get_connected_udid(). Không còn input()/getpass() chặn màn hình console —
2FA (nếu cần) đi qua UiPrompt (Kotlin) thay vì stdin.

Cũng chịu trách nhiệm:
  - Chuyển hướng mọi print() và sys.stderr sang NativeLog (Kotlin) để hiện
    trong LogConsole của app, vì ứng dụng Android không có terminal nào xem.
  - Lưu/đọc UDID hiện tại (do UsbPermissionManager/SideloadScreen phát hiện).
  - Lưu/đọc "pair record" (kết quả ghép nối lockdown) trong AppPaths.filesDir().
"""

import builtins
import hashlib
import os
import plistlib
import shutil
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone

from com.superalpha.sideload.bridge import AppPaths, NativeLog, UiPrompt

from apple_auth import AppleAuth, fetch_official_servers
from developer_api import DeveloperAPI, classify_app_id_error
from utils import (
    run_command, extract_ipa, find_app_bundle, get_bundle_id, get_app_name,
    set_bundle_id, save_certificate_as_pem, decode_apple_data_field,
)
import config_manager
import device_link

# Apple: tài khoản Apple ID miễn phí chỉ được tạo tối đa 10 App ID MỚI mỗi
# 7 ngày — mỗi App ID mới tạo cũng chỉ "sống" đủ 7 ngày trước khi bị Apple
# tự vô hiệu. Toàn bộ hằng số ngày dưới đây tham chiếu đúng chu kỳ 7 ngày này.
APP_ID_QUOTA_WINDOW_DAYS = 7


# ══════════════════════════════════════════════════════════════════════════════
# Chuyển hướng print() VÀ sys.stderr sang NativeLog (Kotlin SharedFlow → UI)
#
# FIX: Dùng NativeLog.log(tag, message) — phiên bản 2 tham số, được đánh dấu
# @JvmStatic trong NativeLog.kt — thay vì NativeLog.log(message) (1 tham số,
# không @JvmStatic). Chaquopy tìm kiếm static method signature: nếu method
# không có @JvmStatic, nó chỉ tồn tại trên NativeLog.INSTANCE (instance method)
# chứ không phải trên class → Chaquopy ném NoSuchMethodError → _bridged_print
# bắt ngoại lệ và bỏ qua silently → log Python không hiện trên UI.
# ══════════════════════════════════════════════════════════════════════════════

_original_print = builtins.print


def _bridged_print(*args, **kwargs):
    """Ghi log vào NativeLog UI VÀ stdout gốc (Logcat qua Chaquopy)."""
    text = " ".join(str(a) for a in args)
    try:
        NativeLog.log("python", text)      # @JvmStatic 2-arg version
    except Exception:
        pass
    _original_print(*args, **kwargs)       # cũng ghi vào Logcat


builtins.print = _bridged_print


class _StderrBridge:
    """Chuyển hướng sys.stderr (traceback Python) sang NativeLog để hiện trong UI."""

    def __init__(self):
        self._buf = ""

    def write(self, s):
        self._buf += s
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            line = line.rstrip("\r")
            if line:
                try:
                    NativeLog.log("python-err", line)
                except Exception:
                    pass

    def flush(self):
        chunk = self._buf.strip()
        if chunk:
            try:
                NativeLog.log("python-err", chunk)
            except Exception:
                pass
        self._buf = ""

    def isatty(self):
        return False


sys.stderr = _StderrBridge()


# ── State helpers ─────────────────────────────────────────────────────────────

_current_udid = None


def set_current_udid(udid: str):
    """Gọi từ Kotlin (UsbPermissionManager) ngay khi USB permission được cấp."""
    global _current_udid
    _current_udid = udid


def get_cached_udid():
    return _current_udid


def get_connected_udid():
    return config_manager.get_connected_udid()


# ── Cầu nối cho màn "Cài đặt" ────────────────────────────────────────────────

def get_saved_apple_id() -> str:
    return config_manager.get_apple_id()


def save_apple_id(apple_id: str):
    config_manager.set_apple_id(apple_id)


def get_saved_anisette_url() -> str:
    return config_manager.get_anisette_url()


def save_anisette_url(url: str):
    config_manager.set_anisette_url(url)


def list_anisette_servers() -> str:
    """Trả về danh sách server Anisette công khai dạng JSON string."""
    import json
    try:
        servers = fetch_official_servers()
        simplified = [
            {"name": s.get("name") or "?", "address": s.get("address")}
            for s in servers if s.get("address")
        ]
        return json.dumps(simplified)
    except Exception as e:
        print(f"[anisette] Lỗi khi lấy danh sách server: {e}")
        return "[]"


# ── Internal state persistence ────────────────────────────────────────────────

def _state_path():
    return os.path.join(str(AppPaths.filesDir()), "sideload_state.json")


def _load_state():
    import json
    try:
        with open(_state_path()) as f:
            return json.load(f)
    except Exception:
        return {}


def _save_state(state: dict):
    import json
    with open(_state_path(), "w") as f:
        json.dump(state, f)


def _clear_cert_from_state(state: dict):
    for key in ("certificate_id", "certificate_pem", "private_key_pem"):
        state.pop(key, None)
    _save_state(state)


# ── Public API ────────────────────────────────────────────────────────────────

def do_sideload(
    ipa_path: str,
    apple_id: str,
    password: str,
    udid_override: str = "",
    anisette_url: str = "",
) -> bool:
    """Ký và cài đặt IPA lên thiết bị iOS đang kết nối USB."""
    try:
        print("══ Bắt đầu quá trình sideload ══")

        # ── Chuẩn bị thư mục làm việc ───────────────────────────────────────
        work_dir = os.path.join(str(AppPaths.filesDir()), "sideload_work")
        if os.path.exists(work_dir):
            shutil.rmtree(work_dir, ignore_errors=True)
        os.makedirs(work_dir, exist_ok=True)

        # ── Giải nén IPA ─────────────────────────────────────────────────────
        print("Đang giải nén IPA...")
        app_dir = extract_ipa(ipa_path, work_dir)
        if not app_dir:
            print("❌ Không giải nén được IPA.")
            return False
        bundle_id = get_bundle_id(app_dir)
        app_name  = get_app_name(app_dir)
        print(f"Ứng dụng: {app_name} ({bundle_id})")

        # ── Xác thực Apple ID ─────────────────────────────────────────────────
        print("Đang đăng nhập Apple ID...")
        effective_anisette = anisette_url or config_manager.get_anisette_url()
        auth = AppleAuth(anisette_url=effective_anisette or None)
        # BUGFIX v11: AppleAuth dùng authenticate() không phải sign_in()
        session = auth.authenticate(apple_id, password)
        if not session or not session.get("authenticated"):
            print("❌ Đăng nhập Apple ID thất bại.")
            return False
        print("✅ Đăng nhập thành công.")

        # ── Lấy Development Team ─────────────────────────────────────────────
        # BUGFIX v11: DeveloperAPI cần (auth_object, dsid, session_token) —
        # không phải session dict. auth.session là requests.Session dùng để gọi HTTP.
        dev_api = DeveloperAPI(auth, session["dsid"], session["session_token"])
        teams = dev_api.list_teams()
        if not teams:
            print("❌ Không lấy được Development Team.")
            return False
        team_id = teams[0].get("teamId") or teams[0].get("teamID") or teams[0].get("id")
        dev_api.set_team(team_id)
        print(f"Team: {team_id}")

        # ── Certificate ───────────────────────────────────────────────────────
        state = _load_state()
        cert_id    = state.get("certificate_id")
        cert_pem   = state.get("certificate_pem")
        key_pem    = state.get("private_key_pem")
        reuse_cert = False

        if cert_id and cert_pem and key_pem:
            existing = [c for c in dev_api.list_certificates() if c.get("id") == cert_id]
            if existing:
                print(f"Dùng lại certificate hiện có: {cert_id}")
                reuse_cert = True

        if not reuse_cert:
            print("Đang tạo certificate mới...")
            # BUGFIX v11: create_certificate() tự sinh RSA key + CSR nội bộ.
            # Không gọi generate_csr() (không tồn tại) hay truyền arg vào.
            # Kết quả trả về: {"certificateId":..., "certContent":..., "_private_key_pem":...}
            cert_data = dev_api.create_certificate()
            if not cert_data:
                print("❌ Không tạo được certificate.")
                return False

            cert_id = cert_data.get("certificateId") or cert_data.get("id", "")
            # certContent là DER bytes (đã được decode_apple_data_field xử lý)
            raw_cert = cert_data.get("certContent") or cert_data.get("certificateContent") or b""
            cert_bytes = decode_apple_data_field(raw_cert)
            if not cert_bytes:
                print("❌ Không lấy được nội dung certificate (certContent rỗng).")
                return False
            # Chuyển DER → PEM để zsign đọc được
            import base64 as _b64
            if isinstance(cert_bytes, bytes) and not cert_bytes.startswith(b"-----"):
                cert_pem = (
                    "-----BEGIN CERTIFICATE-----\n"
                    + _b64.encodebytes(cert_bytes).decode("ascii")
                    + "-----END CERTIFICATE-----\n"
                )
            else:
                cert_pem = cert_bytes.decode("utf-8") if isinstance(cert_bytes, bytes) else cert_bytes

            # _private_key_pem đã là PEM (TraditionalOpenSSL) từ cryptography lib
            key_pem = cert_data.get("_private_key_pem", "")
            if not key_pem:
                print("❌ Không lấy được private key từ create_certificate().")
                return False

            state.update({"certificate_id": cert_id, "certificate_pem": cert_pem, "private_key_pem": key_pem})
            _save_state(state)
            print(f"✅ Tạo certificate thành công: {cert_id}")

        # Ghi cert và key ra file để zsign dùng
        cert_file = os.path.join(work_dir, "cert.pem")
        key_file  = os.path.join(work_dir, "key.pem")
        with open(cert_file, "w") as f: f.write(cert_pem)
        with open(key_file,  "w") as f: f.write(key_pem)

        # ── App ID ────────────────────────────────────────────────────────────
        # BUGFIX v11: list_app_ids() dùng old plist API → trả về list có
        # "identifier" (bundle ID) và "appIdId" (internal ID) ở top-level,
        # không có lớp "attributes" như App Store Connect v1.
        safe_bundle = bundle_id.replace("_", "-")
        full_app_id = f"{team_id}.{safe_bundle}"
        app_ids = dev_api.list_app_ids()
        existing_id = next(
            (a for a in app_ids
             if (a.get("identifier") or a.get("attributes", {}).get("identifier", "")) == full_app_id),
            None
        )
        if existing_id:
            # Old plist API dùng "appIdId", App Store Connect v1 dùng "id"
            app_id_id = existing_id.get("appIdId") or existing_id.get("id")
            print(f"Dùng lại App ID: {full_app_id}")
        else:
            print(f"Đang tạo App ID: {full_app_id}")
            # BUGFIX v11: create_app_id() chỉ nhận (bundle_id, name) —
            # team_id đã được set_team() đặt vào dev_api.team_id rồi.
            result = dev_api.create_app_id(safe_bundle, app_name)
            if not result:
                err_msg = classify_app_id_error(dev_api.last_error)
                print(f"❌ Không tạo được App ID: {err_msg}")
                return False
            app_id_id = result.get("appIdId") or result.get("id")
            print(f"✅ Tạo App ID thành công: {full_app_id}")

        # ── Provisioning Profile ──────────────────────────────────────────────
        print("Đang tạo Provisioning Profile...")
        udid = udid_override or _current_udid or config_manager.get_connected_udid() or str(AppPaths.filesDir())

        # Đăng ký UDID thiết bị nếu chưa có
        # BUGFIX v11: list_devices() dùng old plist API → device dict có
        # "deviceNumber" (UDID), không có lớp "attributes" như v1 API.
        devices = dev_api.list_devices()
        registered = any(
            (d.get("deviceNumber") or d.get("attributes", {}).get("udid", "")) == udid
            for d in devices
        )
        if not registered:
            print(f"Đang đăng ký thiết bị UDID: {udid}")
            dev_api.register_device(udid, "Android Sideload Device")

        # BUGFIX v11: download_provisioning_profile(appIdId) là method đúng.
        # create_provisioning_profile() không tồn tại trong developer_api.py.
        # Old plist API trả về provisioningProfile.encodedProfile (base64 DER).
        profile_data = dev_api.download_provisioning_profile(app_id_id)
        if not profile_data:
            print("❌ Không tải được Provisioning Profile.")
            return False
        # encodedProfile từ old API (không phải attributes.profileContent từ v1)
        raw_profile = profile_data.get("encodedProfile") or profile_data.get("profileContent") or ""
        profile_bytes = decode_apple_data_field(raw_profile)
        if not profile_bytes:
            print("❌ Provisioning Profile rỗng sau decode.")
            return False
        profile_file  = os.path.join(work_dir, "profile.mobileprovision")
        with open(profile_file, "wb") as f:
            f.write(profile_bytes if isinstance(profile_bytes, bytes) else profile_bytes.encode())
        print("✅ Tải Provisioning Profile thành công.")

        # Đặt bundle ID đã sửa vào app bundle
        set_bundle_id(app_dir, f"{team_id}.{safe_bundle}")

        # ── Ký IPA bằng zsign ─────────────────────────────────────────────────
        print("Đang ký IPA bằng zsign...")
        signed_ipa = os.path.join(work_dir, "signed.ipa")
        ok_sign, sign_out = run_command([
            "zsign",
            "-k", key_file,
            "-c", cert_file,
            "-m", profile_file,
            "-o", signed_ipa,
            "-z", "9",
            ipa_path,
        ])
        if not ok_sign:
            print(f"❌ zsign thất bại: {sign_out}")
            return False
        print("✅ Ký IPA thành công.")

        # ── Cài đặt lên thiết bị qua USB ─────────────────────────────────────
        print("Đang kết nối với thiết bị iOS qua USB...")
        if not device_link.connect_and_pair():
            print("❌ Không kết nối được với thiết bị iOS — kiểm tra cáp và bấm Trust nếu iPhone hỏi.")
            return False
        print("Đang cài đặt IPA lên thiết bị...")
        if not device_link.install_ipa(signed_ipa):
            print("❌ Cài đặt thất bại — kiểm tra log.")
            return False

        print("✅ Cài đặt ứng dụng thành công!")
        return True

    except Exception as e:
        import traceback
        print(f"❌ Lỗi không mong đợi trong do_sideload: {e}")
        traceback.print_exc()   # → sys.stderr → _StderrBridge → NativeLog UI
        return False


def do_revoke_certs(
    apple_id: str,
    password: str,
    anisette_url: str = "",
    cert_selector: str = "",
) -> bool:
    """Thu hồi certificate Development trên tài khoản Apple ID."""
    try:
        print("Đang đăng nhập & tra cứu chứng chỉ...")
        effective_anisette = anisette_url or config_manager.get_anisette_url()
        auth = AppleAuth(anisette_url=effective_anisette or None)
        # BUGFIX v11: authenticate() — không phải sign_in()
        session = auth.authenticate(apple_id, password)
        if not session or not session.get("authenticated"):
            print("❌ Đăng nhập Apple ID thất bại.")
            return False

        # BUGFIX v11: DeveloperAPI(auth, dsid, session_token) — không phải DeveloperAPI(session)
        dev_api = DeveloperAPI(auth, session["dsid"], session["session_token"])
        teams = dev_api.list_teams()
        if not teams:
            print("❌ Không lấy được team.")
            return False
        team_id = teams[0].get("teamId") or teams[0].get("teamID") or teams[0].get("id")
        dev_api.set_team(team_id)

        certs = dev_api.list_certificates()
        if not certs:
            print("Không có certificate nào trên tài khoản — không có gì để thu hồi.")
            return True

        print(f"Tài khoản hiện có {len(certs)} certificate:")
        for i, cert in enumerate(certs, 1):
            attrs = cert.get("attributes", {})
            print(f"  [{i}] id={cert.get('id', '?')} name={attrs.get('name', '?')} expires={attrs.get('expirationDate', '?')}")

        selector = (cert_selector or "").strip().lower()
        if not selector:
            print("Chưa chọn certificate nào để thu hồi.")
            return False

        targets = certs if selector == "all" else None
        if targets is None:
            try:
                idx = int(selector) - 1
                if not (0 <= idx < len(certs)):
                    print("Số thứ tự không hợp lệ.")
                    return False
                targets = [certs[idx]]
            except ValueError:
                print("Lựa chọn không hợp lệ — dùng số thứ tự hoặc 'all'.")
                return False

        all_ok = True
        for cert in targets:
            cert_id = cert.get("id")
            name = cert.get("attributes", {}).get("name", "?")
            ok = dev_api.revoke_certificate(cert_id)
            print(f"  → {'✅ Đã revoke' if ok else '❌ Revoke thất bại'}: {name} (id={cert_id})")
            all_ok = all_ok and ok

        state = _load_state()
        if state.get("certificate_id") and any(
            str(c.get("id")) == str(state.get("certificate_id")) for c in targets
        ):
            _clear_cert_from_state(state)

        print("\nXong.")
        return all_ok

    except Exception as e:
        import traceback
        print(f"❌ Lỗi trong do_revoke_certs: {e}")
        traceback.print_exc()
        return False
