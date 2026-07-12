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
import hashlib
import os
import plistlib
import shutil
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


# ── Cầu nối cho màn "Cài đặt" (SettingsScreen.kt / PythonBridge.kt) ──────────
# Apple ID được lưu lại để tự điền ở các tab khác; mật khẩu KHÔNG bao giờ đi
# qua các hàm này (xem ghi chú trong config_manager.get_apple_id).

def get_saved_apple_id() -> str:
    return config_manager.get_apple_id()


def save_apple_id(apple_id: str):
    config_manager.set_apple_id(apple_id)


def get_saved_anisette_url() -> str:
    return config_manager.get_anisette_url()


def save_anisette_url(url: str):
    config_manager.set_anisette_url(url)


def list_anisette_servers() -> str:
    """Trả về danh sách server Anisette công khai (SideStore) dạng CHUỖI
    JSON — Kotlin (PythonBridge.kt) parse bằng org.json.JSONArray, thay vì
    trả một list[dict] Python trực tiếp qua Chaquopy (chuyển đổi kiểu phức
    tạp hơn và kém rõ ràng hơn một chuỗi JSON đơn giản). Dùng cho dropdown
    chọn server trong SettingsScreen."""
    import json
    try:
        servers = fetch_official_servers()
        simplified = [
            {"name": s.get("name") or "?", "address": s.get("address")}
            for s in servers if s.get("address")
        ]
        return json.dumps(simplified)
    except Exception as e:
        print(f"[anisette] Lỗi khi lấy danh sách server cho Cài đặt: {e}")
        return "[]"


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


# ═════════════════════════════════════════════════════════════════════
# Quản lý App ID — đăng ký / tái sử dụng trong hạn mức 10 App ID / 7 ngày
#
# Đây là phần được viết lại theo đúng cách iLoader, AltStore/SideStore và
# các tool sideload khác xử lý 2 tình huống lỗi khác nhau khi tạo App ID
# (xem classify_app_id_error() trong developer_api.py):
#
#   'unavailable' — bundle id bị trùng TOÀN CẦU (không phải do giới hạn tài
#                   khoản của bạn). Xử lý: tự thêm hậu tố riêng cho tài
#                   khoản (ổn định, không đổi mỗi lần chạy) rồi thử lại.
#   'quota'       — tài khoản đã dùng hết 10 App ID mới trong 7 ngày. Xử lý:
#                   (1) nếu app NÀY đã có App ID đăng ký trong 7 ngày qua
#                       (lưu trong registry cục bộ) → dùng lại luôn, không
#                       cần tạo mới, không tính vào hạn mức.
#                   (2) nếu chưa, tìm một App ID CŨ DO CHÍNH TOOL NÀY tạo mà
#                       hiện không app nào (trên thiết bị đang cắm) dùng →
#                       xoá nó để giải phóng chỗ, rồi tạo App ID mới.
# ═════════════════════════════════════════════════════════════════════

def _app_id_registry(state):
    return state.setdefault("app_id_registry", {})


def _stable_bundle_suffix(apple_id: str, original_bundle_id: str) -> str:
    """Hậu tố ỔN ĐỊNH (luôn ra cùng 1 giá trị cho cùng Apple ID + bundle id
    gốc) — KHÔNG dùng uuid/random ở bước đầu, để:
      1. Không đổi bundle id mỗi lần chạy lại (App ID cũ vẫn còn hạn 7 ngày
         thì lần chạy sau tìm lại đúng App ID đó qua list_app_ids(), không
         tạo App ID mới tốn hạn mức).
      2. Có tính riêng tư/khó đoán hơn 1 hậu tố cố định kiểu ".sideload" mà
         nhiều người dùng chung — mỗi Apple ID ra một hậu tố khác nhau nên
         không tự đụng lẫn nhau khi cùng đăng ký 1 app phổ biến.
    Đây đúng là cách AltServer xử lý bundle id của SideStore (thêm hậu tố
    theo Team ID / Apple ID để tránh trùng bundle id 'com.SideStore.SideStore'
    mà rất nhiều người khác cũng đã đăng ký trước đó với TÀI KHOẢN CỦA HỌ)."""
    digest = hashlib.sha1(f"{apple_id.strip().lower()}:{original_bundle_id}".encode("utf-8")).hexdigest()
    return digest[:8]


def _find_app_id_by_identifier(app_ids, identifier):
    for a in app_ids:
        if (a.get("identifier") or a.get("bundleId")) == identifier:
            return a
    return None


def _registry_entry_expired(entry) -> bool:
    created_at = entry.get("created_at")
    if not created_at:
        return True
    try:
        created_dt = datetime.fromisoformat(created_at)
    except Exception:
        return True
    return (datetime.now(timezone.utc) - created_dt) >= timedelta(days=APP_ID_QUOTA_WINDOW_DAYS)


def _remember_app_id(state, original_bundle_id, identifier, app_id_obj, now_iso):
    registry = _app_id_registry(state)
    registry[original_bundle_id] = {
        "identifier": identifier,
        "app_id_id": _first_present(app_id_obj, ["appIdId", "id"]),
        "created_at": now_iso,
    }
    _save_state(state)


def _choose_replacement_app_id(app_ids, original_bundle_id, installed_bundle_ids):
    """[Fallback cuối cùng] Tìm một App ID BẤT KỲ đang rảnh (không có app nào
    trong installed_bundle_ids dùng) để đổi tên bundle id sang đó. Chỉ dùng
    khi không còn cách nào khác (không tự xoá được App ID nào của tool này) —
    xem cảnh báo trong _resolve_app_id(). Giữ nguyên logic gốc."""
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


def _free_up_app_id_quota(dev_api, state, udid, current_bundle_id):
    """Cố gắng xoá 1 App ID CŨ DO CHÍNH TOOL NÀY tạo (ghi trong registry cục
    bộ) để giải phóng 1 chỗ trong hạn mức 10/7 ngày. CHỈ xoá App ID:
      - có trong registry (tức chính tool này đã tạo, không phải của Xcode
        hay app khác cài thủ công);
      - KHÔNG phải của app đang xử lý (current_bundle_id);
      - KHÔNG thuộc app nào đang cài trên chính thiết bị đang cắm.
    Trả True nếu xoá được ít nhất 1 App ID (nên thử tạo lại sau khi gọi hàm
    này), False nếu không tìm được ứng viên an toàn để xoá."""
    registry = _app_id_registry(state)
    if not registry:
        return False

    try:
        pair_record = _get_or_create_pair_record(udid)
        installed_ids = set(device_link.list_installed_apps(pair_record))
    except Exception as e:
        print(f"[app_id] Không lấy được danh sách app đã cài trên thiết bị: {e} "
              "— để an toàn, sẽ không tự xoá App ID nào.")
        return False

    # Ưu tiên xoá entry CŨ NHẤT trước (nhiều khả năng đã hết hạn sử dụng thật).
    candidates = sorted(
        (
            (orig, entry) for orig, entry in registry.items()
            if orig != current_bundle_id
            and entry.get("identifier") not in installed_ids
            and entry.get("app_id_id")
        ),
        key=lambda item: item[1].get("created_at", ""),
    )
    if not candidates:
        print("[app_id] Không có App ID nào do tool này tạo mà hiện đang rảnh để xoá.")
        return False

    orig_bundle_id, entry = candidates[0]
    print(f"[app_id] Giải phóng hạn mức: xoá App ID cũ '{entry.get('identifier')}' "
          f"(đã đăng ký cho app '{orig_bundle_id}', không còn cài trên thiết bị này)...")
    if dev_api.delete_app_id(entry["app_id_id"]):
        registry.pop(orig_bundle_id, None)
        _save_state(state)
        time.sleep(2)
        return True
    return False


def _resolve_app_id(dev_api, state, app_ids, bundle_id, app_name, app_bundle_path, apple_id, udid):
    """Xác định App ID (và bundle id cuối cùng để ký) cho app đang sideload.

    Trả (app_id_obj, final_bundle_id) — hoặc (None, None) nếu không thể.
    Toàn bộ logic 'tự động tái sử dụng App ID trong 7 ngày' và 'tự động đổi
    bundle id khi bị trùng toàn cục' nằm ở đây. Xem chú thích khối phía trên.
    """
    now_iso = datetime.now(timezone.utc).isoformat()
    registry = _app_id_registry(state)

    # ── Bước 0: đã đăng ký App ID cho app NÀY trong 7 ngày qua? Dùng lại luôn,
    # không tính vào hạn mức 10/7-ngày và không cần gọi addAppId.action nữa —
    # đây chính là "tự động sử dụng lại app id" mà không cần đợi lỗi quota.
    cached = registry.get(bundle_id)
    if cached and not _registry_entry_expired(cached):
        existing = _find_app_id_by_identifier(app_ids, cached["identifier"])
        if existing:
            print(f"[app_id] ✅ Tái sử dụng App ID đã đăng ký trước đó: {cached['identifier']} "
                  f"(đăng ký lúc {cached['created_at']}, còn hạn trong {APP_ID_QUOTA_WINDOW_DAYS} ngày).")
            if cached["identifier"] != bundle_id:
                set_bundle_id(app_bundle_path, cached["identifier"])
            return existing, cached["identifier"]
        print(f"[app_id] App ID đã lưu ({cached['identifier']}) không còn thấy trên Apple "
              "(có thể đã bị thu hồi/hết hạn) — sẽ đăng ký lại.")
        registry.pop(bundle_id, None)

    # ── Bước 1: bundle id gốc của IPA đã có sẵn trên tài khoản chưa?
    existing = _find_app_id_by_identifier(app_ids, bundle_id)
    if existing:
        print(f"[app_id] Bundle id gốc '{bundle_id}' đã đăng ký sẵn trên tài khoản — dùng lại.")
        _remember_app_id(state, bundle_id, bundle_id, existing, now_iso)
        return existing, bundle_id

    # ── Bước 2: thử tạo App ID mới với ĐÚNG bundle id gốc trước (không đổi
    # gì nếu không cần thiết — một số app dùng bundle id để khớp entitlement/
    # URL scheme, chỉ nên đổi khi thực sự bị trùng).
    candidate_id = bundle_id
    for attempt in range(4):
        print(f"Đang tạo App ID cho {candidate_id}...")
        app_id_obj = dev_api.create_app_id(candidate_id, app_name)
        if app_id_obj:
            if candidate_id != bundle_id:
                set_bundle_id(app_bundle_path, candidate_id)
            _remember_app_id(state, bundle_id, candidate_id, app_id_obj, now_iso)
            return app_id_obj, candidate_id

        error_kind = classify_app_id_error(dev_api.last_error)

        if error_kind == "unavailable":
            # [FIX chính] Đây là lỗi 9401 thấy trong log gốc: bundle id bị
            # trùng TOÀN CẦU (thường do dùng bundle id mặc định của app, vd
            # 'com.SideStore.SideStore', mà rất nhiều người khác cũng đã
            # đăng ký bằng tài khoản riêng của họ) — KHÔNG liên quan đến hạn
            # mức 10/7-ngày của chính bạn. Cách AltStore/SideStore/iLoader xử
            # lý: tự thêm hậu tố rồi thử lại, không báo lỗi chết ngay.
            if attempt == 0:
                suffix = _stable_bundle_suffix(apple_id, bundle_id)
                candidate_id = f"{bundle_id}.{suffix}"
                print(f"[app_id] Bundle id '{bundle_id}' đã bị đăng ký bởi tài khoản khác "
                      f"(App ID là duy nhất TOÀN CẦU, không riêng tài khoản bạn). "
                      f"Tự đổi sang bundle id riêng cho tài khoản này: {candidate_id}")
                # Hậu tố ổn định có thể đã được TÀI KHOẢN NÀY đăng ký từ trước
                # (vd chạy tool lần trước) — kiểm tra lại danh sách trước khi
                # thử tạo, để không tốn thêm hạn mức nếu không cần.
                already = _find_app_id_by_identifier(app_ids, candidate_id)
                if already:
                    print(f"[app_id] ✅ Bundle id riêng '{candidate_id}' đã đăng ký sẵn — dùng lại.")
                    set_bundle_id(app_bundle_path, candidate_id)
                    _remember_app_id(state, bundle_id, candidate_id, already, now_iso)
                    return already, candidate_id
                continue
            # Hậu tố ổn định CŨNG bị trùng (rất hiếm) — thêm vài ký tự ngẫu
            # nhiên nữa và thử lại tối đa 2 lần nữa trước khi bỏ cuộc.
            candidate_id = f"{bundle_id}.{_stable_bundle_suffix(apple_id, bundle_id)}{uuid.uuid4().hex[:4]}"
            print(f"[app_id] Vẫn bị trùng — thử tiếp với: {candidate_id}")
            continue

        if error_kind == "quota":
            print("[!] Đạt giới hạn 10 App ID mới / 7 ngày của tài khoản Apple ID này.")
            if attempt == 0 and _free_up_app_id_quota(dev_api, state, udid, bundle_id):
                print("[app_id] Đã giải phóng 1 chỗ — thử tạo lại App ID...")
                continue
            print("[!] Không tự giải phóng được hạn mức (không có App ID nào do tool này tạo "
                  "mà hiện đang rảnh) — tìm 1 App ID có sẵn trên tài khoản để dùng tạm...")
            try:
                pair_record = _get_or_create_pair_record(udid)
                installed_ids = set(device_link.list_installed_apps(pair_record))
            except Exception as e:
                print(f"[app_id] Không lấy được danh sách app đã cài: {e}")
                installed_ids = set()
            new_bundle_id, existing_app_id = _choose_replacement_app_id(app_ids, bundle_id, installed_ids)
            if not new_bundle_id:
                print("Lỗi: Đã đạt giới hạn App ID và không còn App ID nào trống để dùng lại.")
                return None, None
            print(f"[!] Dùng tạm App ID có sẵn trên tài khoản: {new_bundle_id} "
                  "(App ID này KHÔNG do tool tạo cho app hiện tại — nếu bạn cũng dùng app "
                  "gốc gắn với App ID đó ở nơi khác, có thể cần cài lại app đó sau).")
            set_bundle_id(app_bundle_path, new_bundle_id)
            return existing_app_id, new_bundle_id

        print(f"Lỗi: Không thể tạo App ID mới ({dev_api.last_error}).")
        return None, None

    print("Lỗi: Không thể tạo App ID sau nhiều lần thử (bundle id liên tục bị trùng).")
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
            # [FIX] Bản cũ gọi register_device(udid, f"iPhone-{udid[:8]}") —
            # NGƯỢC thứ tự so với chữ ký thật register_device(device_name,
            # device_udid) bên developer_api.py. Hậu quả: Apple nhận
            # deviceNumber="iPhone-XXXXXXXX" (chuỗi giả) và name=<UDID thật>,
            # tức là UDID thật KHÔNG BAO GIỜ được đăng ký — Apple vẫn coi
            # team "chưa có thiết bị nào", nên downloadTeamProvisioningProfile
            # luôn thất bại với resultCode 8220 "Your team has no devices
            # from which to generate a provisioning profile" dù USB đã kết
            # nối và UDID đọc được đúng. Kết quả trả về cũng không hề được
            # kiểm tra nên lỗi này bị nuốt hoàn toàn, im lặng đi tiếp tới tận
            # bước tải profile mới lộ ra.
            registered_device = dev_api.register_device(f"iPhone-{udid[:8]}", udid)
            if not registered_device:
                last_err = getattr(dev_api, "last_error", None)
                print(f"Lỗi: Không thể đăng ký thiết bị {udid} với Apple "
                      f"({last_err or 'không rõ nguyên nhân'}).")
                return False
            print(f"[device] ✅ Đã đăng ký thiết bị {udid} với Apple.")
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
        target_app_id, final_bundle_id = _resolve_app_id(
            dev_api, state, app_ids, bundle_id, app_name, app_bundle_path, apple_id, udid,
        )

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
        # [FIX "CANNOT LINK EXECUTABLE ... library libssl.so.3 not found"]
        # libzsign.so được build sẵn trong môi trường Termux nên phụ thuộc
        # libssl.so.3/libcrypto.so.3/libc++_shared.so mà máy không cài Termux
        # sẽ không có. AppPaths.nativeDepsDir() giải nén sẵn 3 file này (đóng
        # gói trong assets/zsign_deps/) ra filesDir — set LD_LIBRARY_PATH trỏ
        # vào đó để linker của tiến trình zsign tìm thấy chúng thay vì tìm
        # (không thấy) đường dẫn Termux gốc. Xem chú thích chi tiết trong
        # AppPaths.kt.
        run_command(zsign_cmd, extra_env={"LD_LIBRARY_PATH": AppPaths.nativeDepsDir()})

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
