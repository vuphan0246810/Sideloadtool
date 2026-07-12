"""mux_usb.py — triển khai lại (clean-room) giao thức "usbmux" của Apple chạy
trực tiếp trên USB bulk transfer, không qua daemon usbmuxd.

************************  CẢNH BÁO QUAN TRỌNG  ************************
Đây là thành phần RỦI RO NHẤT và CHƯA ĐƯỢC KIỂM CHỨNG trên phần cứng thật
trong toàn bộ dự án này. Không có SDK Android/Gradle hay iPhone thật trong
môi trường xây dựng (Replit) để build & test file này — nó được viết dựa
trên tài liệu công khai + mã nguồn tham khảo của dự án libimobiledevice/
usbmuxd (usb.h, device.h/.c), KHÔNG phải bằng cách chạy thử với thiết bị.
Rất có khả năng vài chi tiết byte-level (thứ tự trường, giá trị hằng số) cần
chỉnh sửa sau khi test với Android Studio + iPhone thật qua USB debugger/
Wireshark (hoặc so lại trực tiếp với mã nguồn C tại
https://github.com/libimobiledevice/usbmuxd, file src/usb.c + src/device.c).
"Chạy usbmuxd không cần root trên Android" vẫn là vấn đề CHƯA CÓ giải pháp
sẵn trong cộng đồng libimobiledevice (xem các issue mở từ 2019 và 2021 trên
GitHub của họ) — module này là nỗ lực tự triển khai lại, không phải một thư
viện đã được kiểm chứng.
************************************************************************

Kiến trúc:
  - Lớp vật lý (bulk IN/OUT) do Kotlin đảm nhiệm (bridge.UsbTransport), dùng
    USB Host API chuẩn của Android — phần này KHÔNG có gì lạ, đã test được
    bằng mọi ứng dụng Android USB Host thông thường.
  - Lớp trên (đóng khung + đa kênh, tương đương usbmuxd nội bộ) là phần code
    này triển khai lại, mô phỏng một giao thức kiểu TCP tối giản để mở nhiều
    "kết nối" logic (lockdownd, AFC, installation_proxy, ...) trên cùng một
    ống USB vật lý, y hệt cách usbmuxd thật đa kênh hoá cho nhiều client.

Giao thức (theo hiểu biết tốt nhất về usbmuxd/src/usb.h + device.h):
  1. Bắt tay phiên bản: gửi version_header{major=2, minor=0, padding=0},
     nhận lại cùng cấu trúc từ thiết bị. magic 0xfeedface được dùng làm
     trường 'magic' trong mux_header cho version>=2.
  2. Mỗi gói tin sau đó có mux_header (protocol, length, magic, tx_seq,
     rx_seq) rồi tới payload. protocol=6 (giống IPPROTO_TCP) đóng gói một
     "TCP header" tối giản (sport,dport,seq,ack,doff_flags,window,checksum,
     urgent) mô phỏng bắt tay SYN/SYN-ACK/ACK rồi truyền dữ liệu qua các gói
     PSH+ACK, đóng bằng FIN — gần như một triển khai TCP tối giản chạy trên
     "IP ảo" là chính sợi cáp USB.
"""

import struct
import threading
import time
import queue

MAGIC = 0xFEEDFACE
VERSION_MAJOR = 2
VERSION_MINOR = 0

MUX_PROTO_VERSION = 0
MUX_PROTO_CONTROL = 1
MUX_PROTO_SETUP = 2
MUX_PROTO_TCP = 6  # mô phỏng IPPROTO_TCP, dùng cho mọi lưu lượng lockdown/AFC

TCP_FLAG_FIN = 0x01
TCP_FLAG_SYN = 0x02
TCP_FLAG_RST = 0x04
TCP_FLAG_PSH = 0x08
TCP_FLAG_ACK = 0x10

_HEADER_FMT = "!IIIII"  # protocol, length, magic, tx_seq, rx_seq
_HEADER_LEN = struct.calcsize(_HEADER_FMT)
_TCPHDR_FMT = "!HHIIBBHHH"  # sport,dport,seq,ack,doff,flags,window,checksum,urgent
_TCPHDR_LEN = struct.calcsize(_TCPHDR_FMT)

DEFAULT_WINDOW = 131072


class MuxError(Exception):
    pass


class _RawIo:
    """Bọc UsbTransport.bulkRead/bulkWrite (Kotlin, qua Chaquopy) thành một
    luồng đọc/ghi liên tục, gộp các mảnh USB_MRU nhỏ lại nếu cần."""

    def __init__(self):
        from com.superalpha.sideload.bridge import UsbTransport
        self._transport = UsbTransport
        self._rx_buffer = bytearray()

    def write(self, data: bytes):
        offset = 0
        while offset < len(data):
            chunk = data[offset: offset + 16384]
            written = self._transport.bulkWrite(chunk)
            if written is None or written < 0:
                raise MuxError("bulkWrite thất bại (USB có thể đã bị rút ra).")
            offset += written

    def read_exact(self, n, timeout_s=15.0):
        deadline = time.time() + timeout_s
        while len(self._rx_buffer) < n:
            remaining = deadline - time.time()
            if remaining <= 0:
                raise MuxError(f"Timeout khi đọc {n} byte từ USB (mới có {len(self._rx_buffer)}).")
            chunk = self._transport.bulkRead(int(max(50, min(remaining * 1000, 2000))))
            if chunk:
                self._rx_buffer.extend(bytes(chunk))
        result = bytes(self._rx_buffer[:n])
        del self._rx_buffer[:n]
        return result


class MuxConnection:
    """Một kênh logic (giống một `int fd` trong libimobiledevice) multiplex
    trên cùng một kết nối USB vật lý. Giao diện tối giản kiểu socket: send()/
    recv()/close(). Không thread-safe giữa nhiều MuxConnection cùng lúc — bộ
    định tuyến gói tin nằm ở MuxDevice, chạy trên MỘT thread nền dùng chung."""

    def __init__(self, device: "MuxDevice", src_port: int, dst_port: int):
        self.device = device
        self.src_port = src_port
        self.dst_port = dst_port
        self.seq = 1
        self.ack = 0
        self.peer_window = DEFAULT_WINDOW
        self._rx_queue: "queue.Queue[bytes]" = queue.Queue()
        self._connected = threading.Event()
        self._closed = threading.Event()
        self._rx_leftover = b""

    def _on_segment(self, tcp_flags, seq, ack, payload):
        if tcp_flags & TCP_FLAG_RST:
            self._closed.set()
            self._connected.set()
            return
        if tcp_flags & TCP_FLAG_SYN and tcp_flags & TCP_FLAG_ACK:
            self.ack = seq + 1
            self.device._send_tcp(self, TCP_FLAG_ACK, payload=b"")
            self._connected.set()
            return
        if payload:
            self.ack = seq + len(payload)
            self._rx_queue.put(payload)
            self.device._send_tcp(self, TCP_FLAG_ACK, payload=b"")
        if tcp_flags & TCP_FLAG_FIN:
            self.ack += 1
            self.device._send_tcp(self, TCP_FLAG_ACK, payload=b"")
            self._rx_queue.put(b"")  # đánh dấu EOF
            self._closed.set()

    def wait_connected(self, timeout=15.0):
        if not self._connected.wait(timeout):
            raise MuxError(f"Timeout mở kết nối mux tới cổng {self.dst_port} (lockdownd/AFC/...).")
        if self._closed.is_set():
            raise MuxError(f"Thiết bị từ chối kết nối tới cổng {self.dst_port} (RST).")

    def send(self, data: bytes):
        if not data:
            return
        self.device._send_tcp(self, TCP_FLAG_ACK | TCP_FLAG_PSH, payload=data)
        self.seq += len(data)

    def recv(self, size: int, timeout=30.0) -> bytes:
        while len(self._rx_leftover) < size:
            try:
                chunk = self._rx_queue.get(timeout=timeout)
            except queue.Empty:
                raise MuxError("Timeout khi chờ dữ liệu từ thiết bị (mux).")
            if chunk == b"" and self._closed.is_set():
                break
            self._rx_leftover += chunk
        result, self._rx_leftover = self._rx_leftover[:size], self._rx_leftover[size:]
        return result

    def close(self):
        if not self._closed.is_set():
            try:
                self.device._send_tcp(self, TCP_FLAG_FIN | TCP_FLAG_ACK, payload=b"")
            except Exception:
                pass
        self._closed.set()
        self.device._unregister(self)


class MuxDevice:
    """Quản lý một phiên usbmux trên một thiết bị Apple đã claim qua USB Host
    API. Chạy một thread nền bơm dữ liệu đọc được từ USB vào đúng MuxConnection
    theo (src_port,dst_port); các lệnh gửi đi được khoá bằng _io_lock để tránh
    xen kẽ giữa hai kết nối logic ghi cùng lúc lên một ống USB vật lý."""

    def __init__(self):
        self._io = _RawIo()
        self._io_lock = threading.Lock()
        self._tx_seq = 0
        self._rx_seq = 0
        self._connections = {}
        self._next_src_port = 40000
        self._pump_thread = None
        self._stop = threading.Event()

    def start(self):
        print("[mux] Bắt tay phiên bản usbmux qua USB...")
        version_payload = struct.pack("!III", VERSION_MAJOR, VERSION_MINOR, 0)
        self._send_raw(MUX_PROTO_VERSION, version_payload)
        header, payload = self._recv_raw()
        if header[0] != MUX_PROTO_VERSION or len(payload) < 8:
            raise MuxError("Thiết bị không phản hồi đúng bắt tay phiên bản usbmux.")
        major, minor, _ = struct.unpack("!III", payload[:12]) if len(payload) >= 12 else (0, 0, 0)
        print(f"[mux] Thiết bị chấp nhận phiên bản usbmux {major}.{minor}.")

        self._pump_thread = threading.Thread(target=self._pump_loop, daemon=True)
        self._pump_thread.start()

    def stop(self):
        self._stop.set()

    def _send_raw(self, protocol, payload: bytes):
        self._tx_seq += 1
        header = struct.pack(_HEADER_FMT, protocol, _HEADER_LEN + len(payload), MAGIC, self._tx_seq, self._rx_seq)
        with self._io_lock:
            self._io.write(header + payload)

    def _recv_raw(self, timeout_s=15.0):
        raw_header = self._io.read_exact(_HEADER_LEN, timeout_s=timeout_s)
        protocol, length, magic, tx_seq, rx_seq = struct.unpack(_HEADER_FMT, raw_header)
        if length < _HEADER_LEN:
            raise MuxError(f"mux_header length không hợp lệ: {length}")
        payload_len = length - _HEADER_LEN
        payload = self._io.read_exact(payload_len, timeout_s=timeout_s) if payload_len else b""
        self._rx_seq = tx_seq
        return (protocol, length, magic, tx_seq, rx_seq), payload

    def _pump_loop(self):
        while not self._stop.is_set():
            try:
                header, payload = self._recv_raw(timeout_s=5.0)
            except MuxError:
                continue
            except Exception as e:
                print(f"[mux] Lỗi vòng lặp đọc USB: {e}")
                time.sleep(0.2)
                continue
            protocol = header[0]
            if protocol != MUX_PROTO_TCP or len(payload) < _TCPHDR_LEN:
                continue
            sport, dport, seq, ack, doff, flags, window, checksum, urgent = struct.unpack(
                _TCPHDR_FMT, payload[:_TCPHDR_LEN]
            )
            data = payload[_TCPHDR_LEN:]
            # sport/dport ở đây là góc nhìn của THIẾT BỊ: sport=cổng dịch vụ
            # trên device, dport=cổng "ảo" ta tự chọn ở host. Tra theo dport.
            conn = self._connections.get(dport)
            if conn:
                conn.peer_window = window or DEFAULT_WINDOW
                conn._on_segment(flags, seq, ack, data)

    def _send_tcp(self, conn: "MuxConnection", flags: int, payload: bytes):
        tcp_header = struct.pack(
            _TCPHDR_FMT,
            conn.src_port, conn.dst_port,
            conn.seq, conn.ack,
            (5 << 4), flags,
            DEFAULT_WINDOW, 0, 0,
        )
        self._send_raw(MUX_PROTO_TCP, tcp_header + payload)

    def connect(self, dst_port: int, timeout=15.0) -> MuxConnection:
        """Mở một kênh logic mới tới `dst_port` trên thiết bị (vd 62078 cho
        lockdownd). Trả về MuxConnection đã bắt tay SYN/SYN-ACK/ACK xong."""
        src_port = self._next_src_port
        self._next_src_port += 1
        conn = MuxConnection(self, src_port, dst_port)
        self._connections[src_port] = conn
        self._send_tcp(conn, TCP_FLAG_SYN, payload=b"")
        conn.wait_connected(timeout=timeout)
        return conn

    def _unregister(self, conn: MuxConnection):
        self._connections.pop(conn.src_port, None)


_device_singleton = None
_device_lock = threading.Lock()


def get_device() -> MuxDevice:
    """Trả về (và khởi tạo nếu cần) phiên MuxDevice dùng chung cho toàn bộ
    Python runtime — chỉ có một iPhone được claim qua UsbTransport tại một
    thời điểm, nên một singleton là đủ và tránh mở hai bắt tay phiên bản
    chồng chéo trên cùng một dây USB."""
    global _device_singleton
    with _device_lock:
        if _device_singleton is None:
            from com.superalpha.sideload.bridge import UsbTransport
            if not UsbTransport.isConnected():
                raise MuxError("Chưa kết nối USB tới iPhone/iPad. Hãy bấm 'Kết nối' trước.")
            dev = MuxDevice()
            dev.start()
            _device_singleton = dev
        return _device_singleton


def reset_device():
    global _device_singleton
    with _device_lock:
        if _device_singleton is not None:
            _device_singleton.stop()
        _device_singleton = None
