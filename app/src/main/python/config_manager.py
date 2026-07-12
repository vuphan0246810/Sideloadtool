"""Cấu hình + UDID — bản thích ứng cho Android.

So với bản gốc (Termux):
  - config.json trong thư mục làm việc hiện tại  ->  filesDir()/config.json
    (Android không có khái niệm "thư mục làm việc" hữu ích cho một app; dùng
    AppPaths.filesDir(), thư mục dữ liệu riêng của app, luôn ghi được).
  - get_connected_udid() gọi `idevice_id -l` qua subprocess (cần
    libimobiledevice cài qua Termux)  ->  không có binary đó trên Android.
    Thay bằng: đọc UsbDevice.serialNumber (mô tả serial USB chuẩn) — hầu hết
    iPhone/iPad trả về UDID thật ở đây khi thiết bị đã tin cậy máy tính. Đây
    là phương án đơn giản hơn nhiều so với hỏi lockdownd (QueryType/GetValue),
    và không cần một kết nối mux đã thiết lập.
"""

import json
import os

from com.superalpha.sideload.bridge import AppPaths


def _config_path():
    return os.path.join(AppPaths.filesDir(), "config.json")


def load_config():
    path = _config_path()
    if os.path.exists(path):
        try:
            with open(path, "r") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def save_config(config):
    with open(_config_path(), "w") as f:
        json.dump(config, f, indent=4)


def get_connected_udid():
    """Trả về UDID của iPhone/iPad đang cắm qua USB, hoặc None nếu không có
    thiết bị nào / không đọc được serial. Xem device_link.get_udid_from_usb()
    để biết chi tiết — hàm đó là nguồn thực sự, đây chỉ là lớp tương thích
    giữ tên hàm giống bản gốc để sideload_core.py gọi quen tay."""
    try:
        from device_link import get_udid_from_usb
        return get_udid_from_usb()
    except Exception as e:
        print(f"[config] Không lấy được UDID từ USB: {e}")
        return None
