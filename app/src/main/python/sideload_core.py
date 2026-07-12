"""sideload_core.py — điểm vào duy nhất mà Kotlin (PythonBridge.kt) gọi vào.

Thay thế main.py gốc (menu CLI tương tác trong Termux) bằng 3 hàm thuần tuý
mà UI Compose gọi trực tiếp: do_sideload(), do_revoke_certs(),
get_connected_udid(). Không còn input()/getpass() chặn màn hình console —
2FA (nếu cần) đi qua UiPrompt (Kotlin) thay vì stdin.

Cũng chịu trách nhiệm:
  - Chuyển hướng mọi print() sang NativeLog (Kotlin) để hiện trong LogConsole
    của app, vì ứng dụng Android không có terminal nào để xem stdout.
  - Lưu/đọc UDID hiện tại (do UsbPermissionManager/SideloadScreen phát hiện
    qua UsbDevice.serialNumber lúc kết nối) để device_link.get_udid_from_usb()
    dùng lại mà không cần hỏi lại lockdownd.
  - Lưu/đọc "pair record" (kết quả ghép nối lockdown) trong AppPaths.filesDir()
    để không phải ghép nối lại (và không phải hỏi người dùng bấm Trust lại)
    ở mỗi lần chạy.
"""

import builtins
import os
import plistlib
import shutil
import time
import uuid
from datetime import datetime, timezone

from com.superalpha.sideload.bridge import AppPaths, NativeLog, UiPrompt

from apple_auth import AppleAuth
from developer_api import DeveloperAPI
from utils import (
    run_command, extract_ipa, find_app_bundle, get_bundle_id, get_app_name,
    set_bundle_id, save_certificate_as_pem, decode_apple_data_field,
)
import config_manager
import device_link

# ── Chuyển hướng print() sang NativeLog (Kotlin SharedFlow -> LogConsole UI) ──
_original_print = builtins.print


def _bridged_print(*args, **kwargs):
    text = " ".join(str(a) for a in args)
    try:
        NativeLog.log(text)
    except Exception:
        pass
    _original_print(*args, **kwargs)


builtins.print = _bridged_print

_current_udid = None


def set_current_udid(udid: str):
    """Gọi từ Kotlin (UsbPermissionManager) ngay khi USB permission được cấp
    và UsbDevice.serialNumber đọc được, TRƯỚC khi do_sideload()/do_revoke_certs()
    chạy — để get_cached_udid()/get_connected_udid() có dữ liệu dùng ngay,
    không cần round-trip lockdown chỉ để hỏi UDID."""
    global _current_udid
    _current_udid = udid


def get_cached_udid():
    return _current_udid


def get_connected_udid():
    return config_manager.get_connected_udid()


def _first_present(d, keys, default=None):
    for k in keys:
        if k in d and d[k]:
            return d[k]
    return default


def _pair_record_path():
    return os.path.join(AppPaths.filesDir(), "pair_record.plist")


def _load_pair_record():
    path = _pair_record_path()
    if os.path.exists(path):
        try:
            with open(path, "rb") as f:
                return plistlib.load(f)
        except Exception as e:
            print(f"[pairing] Không đọc được pair record đã lưu: {e}")
    return None


def _save_pair_record(record: dict):
    with open(_pair_record_path(), "wb") as f:
        plistlib.dump(record, f)


def _get_or_create_pair_record(udid: str) -> dict:
    record = _load_pair_record()
    if record:
        return record
    print("[pairing] Chưa ghép nối với thiết bị này — bắt đầu ghép nối lần đầu...")
    record = device_link.pair_device(udid)
    _save_pair_record(record)
    return record


def _state_path():
    return os.path.join(AppPaths.filesDir(), "sideload_state.json")


def _load_state():
    import json
    path = _state_path()
    if os.path.exists(path):
        try:
            with open(path, "r") as f:
                return json.load(f)
        except Exception as e:
            print(f"[state] Không đọc được state: {e} — bắt đầu với state trống.")
    return {}


def _save_state(state):
    import json
    with open(_state_path(), "w") as f:
        json.dump(state, f, indent=2)


def _clear_cert_from_state(state):
    for key in ["certificate_id", "certificate_pem", "private_key_pem"]:
        state.pop(key, None)
    _save_state(state)


def _try_reuse_existing_cert(dev_api, state, cert_pem_path, key_pem_path):
    cert_id = state.get("certificate_id")
    cert_pem = state.get("certificate_pem")
    key_pem = state.get("private_key_pem")

    print("[cert] Đang kiểm tra danh sách certificate trên Apple để tái sử dụng...")
    try:
        existing_certs = dev_api.list_certificates()
        if not existing_certs:
            print("[cert] Không tìm thấy certificate nào trên Apple.")
            return False

        target_cert = None
        if cert_id:
            for cert in existing_certs:
                if str(cert.get("id")) == str(cert_id):
                    target_cert = cert
                    break

        if not target_cert and cert_pem:
            clean_pem = (
                cert_pem.replace("-----BEGIN CERTIFICATE-----", "")
                .replace("-----END CERTIFICATE-----", "")
                .replace("\n", "").replace("\r", "").strip()
            )
            for cert in existing_certs:
                if cert.get("attributes", {}).get("certificateContent") == clean_pem:
                    target_cert = cert
                    cert_id = cert.get("id")
                    break

        if not target_cert and len(existing_certs) == 1 and key_pem:
            target_cert = existing_certs[0]
            cert_id = target_cert.get("id")

        if not target_cert:
            return False

        attrs = target_cert.get("attributes", {})
        exp = attrs.get("expirationDate", "?")
        try:
            exp_dt = datetime.fromisoformat(exp.replace("Z", "+00:00"))
            days_left = (exp_dt - datetime.now(timezone.utc)).days
            if days_left < 3:
                print(f"[cert] Cert {cert_id} sắp hết hạn ({days_left} ngày) — sẽ tạo mới.")
                return False
            print(f"[cert] ✅ Tái sử dụng cert {cert_id} (còn {days_left} ngày).")
        except Exception:
            print(f"[cert] ✅ Tái sử dụng cert {cert_id}.")

        content_from_api = attrs.get("certificateContent")
        if content_from_api:
            save_certificate_as_pem(content_from_api, cert_pem_path)
        elif cert_pem:
            with open(cert_pem_path, "w") as f:
                f.write(cert_pem)
        if key_pem:
            with open(key_pem_path, "w") as f:
                f.write(key_pem)

        with open(cert_pem_path, "r") as f:
            state["certificate_pem"] = f.read()
        state["certificate_id"] = cert_id
        state["private_key_pem"] = key_pem
        _save_state(state)
        return True
    except Exception as e:
        print(f"[cert] Lỗi khi kiểm tra tái sử dụng cert: {e}")
        return False


def _choose_replacement_app_id(app_ids, original_bundle_id, installed_bundle_ids):
    def identifier_of(a):
        return a.get("identifier") or a.get("bundleId") or ""

    exact, prefixed, other = [], [], []
    for a in app_ids:
        ident = identifier_of(a)
        if not ident or "*" in ident or ident in installed_bundle_ids:
            continue
        if ident == original_bundle_id:
            exact.append(a)
        elif ident.startswith(original_bundle_id + "."):
            prefixed.append(a)
        else:
            other.append(a)
    for bucket in (exact, prefixed, other):
        if bucket:
            return identifier_of(bucket[0]), bucket[0]
    return None, None


def do_sideload(ipa_path: str, apple_id: str, password: str, udid_override: str = "", anisette_url: str = "") -> bool:
    """Cổng vào chính cho tab "Sideload". Trả True/False cho PythonBridge.kt."""
    anisette_url = anisette_url or None
    try:
        print("\n=== Bắt đầu quá trình Sideload ===")
        state = _load_state()

        udid = udid_override or get_cached_udid()
        if not udid:
            print("Lỗi: Chưa xác định được UDID — hãy kết nối USB tới iPhone trước.")
            return False
        print(f"[device] UDID: {udid}")

        print("\n[Bước 1/6] Xác thực Apple ID...")
        auth = AppleAuth(anisette_url=anisette_url, input_func=UiPrompt.requestInput)
        auth_result = auth.authenticate(apple_id, password)
        if not auth_result or not auth_result.get("authenticated"):
            print("Lỗi: Xác thực thất bại.")
            return False
        if auth_result.get("authenticated") == "2fa_completed":
            print("2FA hoàn tất. Hãy bấm 'Sideload' lại lần nữa (lần này sẽ không hỏi mã 2FA nữa).")
            return False

        dsid = auth_result["dsid"]
        session_token = auth_result["session_token"]
        dev_api = DeveloperAPI(auth, dsid, session_token)

        teams = dev_api.list_teams()
        if not teams:
            print("Lỗi: Không tìm thấy Team ID (đăng nhập Xcode/SideStore với Apple ID này ít nhất 1 lần trước).")
            return False
        team_id = _first_present(teams[0], ["teamId", "teamID", "id"])
        dev_api.set_team(team_id)

        print(f"\n[Bước 1.5/6] Kiểm tra thiết bị {udid} trên Apple...")
        devices = dev_api.list_devices()
        device_exists = any(d.get("deviceNumber") == udid or d.get("udid") == udid for d in devices)
        if not device_exists:
            print(f"Đang đăng ký thiết bị {udid}...")
            dev_api.register_device(udid, f"iPhone-{udid[:8]}")
        else:
            print("Thiết bị đã được đăng ký.")

        print("\n[Bước 2/6] Chuẩn bị Certificate và Key...")
        cert_pem_path = os.path.join(AppPaths.filesDir(), "cert.pem")
        key_pem_path = os.path.join(AppPaths.filesDir(), "key.pem")

        if not _try_reuse_existing_cert(dev_api, state, cert_pem_path, key_pem_path):
            print("[cert] Không thể tái sử dụng certificate — thử tạo mới...")
            existing_certs = dev_api.list_certificates()
            if len(existing_certs) >= 1:
                print(f"[cert] Tài khoản đã có {len(existing_certs)} certificate (giới hạn tài khoản free là 1).")
                print("[cert] Sẽ tự động thu hồi certificate cũ nhất để lấy chỗ tạo mới.")
                oldest = existing_certs[0]
                if not dev_api.revoke_certificate(oldest.get("id")):
                    print("Lỗi: Không thể thu hồi certificate cũ — không còn chỗ để tạo mới.")
                    return False
                if str(oldest.get("id")) == str(state.get("certificate_id", "")):
                    _clear_cert_from_state(state)
                time.sleep(2)

            machine_name = f"sideload-{uuid.uuid4().hex[:8]}"
            cert_data = dev_api.create_certificate(machine_name)
            if not cert_data:
                print("Lỗi: Không thể tạo certificate mới.")
                return False

            cert_id = cert_data.get("id")
            cert_content = cert_data.get("attributes", {}).get("certificateContent") or cert_data.get("certContent")
            if not cert_content:
                print("Lỗi: Apple không trả về nội dung certificate sau khi tạo.")
                return False
            save_certificate_as_pem(cert_content, cert_pem_path)

            key_pem = cert_data.get("_private_key_pem")
            if not key_pem:
                print("Lỗi: Không tìm thấy private key trong cert_data.")
                return False
            with open(key_pem_path, "w") as f:
                f.write(key_pem)
            with open(cert_pem_path, "r") as f:
                state["certificate_pem"] = f.read()
            state["certificate_id"] = cert_id
            state["private_key_pem"] = key_pem
            _save_state(state)

        print("\n[Bước 3/6] Xử lý App ID và Provisioning Profile...")
        work_dir = os.path.join(AppPaths.workDir(), "extract")
        if os.path.exists(work_dir):
            shutil.rmtree(work_dir)
        os.makedirs(work_dir)

        extracted_dir = os.path.join(work_dir, "extracted")
        extract_ipa(ipa_path, extracted_dir)
        app_bundle_path = find_app_bundle(extracted_dir)
        bundle_id = get_bundle_id(app_bundle_path)
        app_name = get_app_name(app_bundle_path)

        app_ids = dev_api.list_app_ids()
        target_app_id = next((a for a in app_ids if a.get("identifier") == bundle_id), None)
        final_bundle_id = bundle_id

        if not target_app_id:
            print(f"Đang tạo App ID mới cho {bundle_id}...")
            target_app_id = dev_api.create_app_id(bundle_id, app_name)
            if not target_app_id:
                last_err = getattr(dev_api, "last_error", {}) or {}
                user_string = str(last_err.get("userString", ""))
                if "maximum number of App IDs" in user_string or "limit" in user_string.lower():
                    print("[!] Đạt giới hạn 10 App ID/7 ngày — tìm App ID có sẵn để dùng lại...")
                    pair_record = _get_or_create_pair_record(udid)
                    installed_ids = set(device_link.list_installed_apps(pair_record))
                    new_bundle_id, existing_app_id = _choose_replacement_app_id(app_ids, bundle_id, installed_ids)
                    if not new_bundle_id:
                        print("Lỗi: Không còn App ID nào trống để dùng lại.")
                        return False
                    print(f"[!] Dùng App ID có sẵn: {new_bundle_id}")
                    set_bundle_id(app_bundle_path, new_bundle_id)
                    final_bundle_id = new_bundle_id
                    target_app_id = existing_app_id
                else:
                    print("Lỗi: Không thể tạo App ID mới.")
                    return False

        if not target_app_id:
            print("Lỗi: Không có App ID hợp lệ để ký.")
            return False

        app_id_id = _first_present(target_app_id, ["appIdId", "id"])
        profile = dev_api.download_provisioning_profile(app_id_id)
        if not profile:
            print("Lỗi: Không thể tải Provisioning Profile.")
            return False

        profile_content = decode_apple_data_field(
            _first_present(profile, ["encodedProfile", "content", "profileContent"])
        )
        with open(os.path.join(app_bundle_path, "embedded.mobileprovision"), "wb") as f:
            f.write(profile_content)

        print("\n[Bước 4/6] Ký IPA bằng zsign...")
        signed_ipa_path = os.path.join(AppPaths.workDir(), f"{app_name}_signed.ipa")
        tmp_dir = os.path.join(AppPaths.workDir(), "zsign_tmp")
        os.makedirs(tmp_dir, exist_ok=True)
        zsign_cmd = [
            AppPaths.zsignPath(), "-f",
            "-t", tmp_dir,
            "-c", cert_pem_path,
            "-k", key_pem_path,
            "-m", os.path.join(app_bundle_path, "embedded.mobileprovision"),
            "-o", signed_ipa_path,
            app_bundle_path,
        ]
        run_command(zsign_cmd)

        print("\n[Bước 5/6] Ghép nối với thiết bị (nếu chưa) — kiểm tra màn hình iPhone...")
        pair_record = _get_or_create_pair_record(udid)

        print("\n[Bước 6/6] Đẩy IPA lên thiết bị và cài đặt...")
        remote_filename = f"{final_bundle_id}.ipa"

        def _push_progress(sent, total):
            pct = int(sent * 100 / total) if total else 0
            print(f"[afc] Đang tải lên: {pct}%")

        device_path = device_link.afc_push_ipa(pair_record, signed_ipa_path, remote_filename, progress_cb=_push_progress)
        device_link.install_ipa(pair_record, device_path)

        print("\n=== Sideload hoàn tất ✅ ===")
        return True
    except Exception as e:
        print(f"\n❌ Lỗi trong quá trình sideload: {e}")
        return False


def do_revoke_certs(apple_id: str, password: str, anisette_url: str = "", cert_selector: str = "") -> bool:
    """Cổng vào chính cho tab "Thu hồi Certificate". cert_selector là index
    dạng chuỗi (1-based, theo thứ tự list_certificates trả về) hoặc "all"."""
    anisette_url = anisette_url or None
    try:
        print("\n=== Thu hồi Certificate ===")
        auth = AppleAuth(anisette_url=anisette_url, input_func=UiPrompt.requestInput)
        auth_result = auth.authenticate(apple_id, password)
        if not auth_result or not auth_result.get("authenticated"):
            print("Lỗi: Đăng nhập thất bại.")
            return False
        if auth_result.get("authenticated") == "2fa_completed":
            print("2FA hoàn tất. Hãy bấm 'Thu hồi' lại lần nữa.")
            return False

        dsid = auth_result["dsid"]
        session_token = auth_result["session_token"]
        dev_api = DeveloperAPI(auth, dsid, session_token)

        teams = dev_api.list_teams()
        if not teams:
            print("Lỗi: Không lấy được team.")
            return False
        team = teams[0]
        team_id = team.get("teamId") or team.get("teamID") or team.get("id")
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
        if state.get("certificate_id") and any(str(c.get("id")) == str(state.get("certificate_id")) for c in targets):
            _clear_cert_from_state(state)

        print("\nXong.")
        return all_ok
    except Exception as e:
        print(f"\n❌ Lỗi khi thu hồi certificate: {e}")
        return False
