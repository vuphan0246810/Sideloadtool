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

Giao thức (đã đối chiếu byte-for-byte với mã nguồn tham khảo chính chủ
libimobiledevice/usbmuxd, file src/device.c — hàm device_add(),
device_version_input(), device_data_input(), send_packet(), send_tcp(),
device_tcp_input() — ngày 2026-07-12, xem mục "2. Đã sửa trong bản này" của
README.md để biết chi tiết từng điểm khác biệt so với bản trước):
  1. Bắt tay phiên bản DÙNG HEADER NGẮN 8 BYTE (chỉ {protocol:u32, length:u32},
     KHÔNG có magic/tx_seq/rx_seq) — vì mux_header_size trong usbmuxd thật
     luôn được tính là `(dev->version < 2) ? 8 : sizeof(mux_header)`, và
     dev->version bắt đầu bằng 0 (<2) cho tới khi gói version-reply được xử
     lý xong. Cả gói version request (host gửi) VÀ gói version reply (thiết
     bị trả) đều dùng header 8 byte này, payload là version_header{major,
     minor,padding} 12 byte (3×u32 big-endian) — tổng 20 byte mỗi gói.
     Bản trước của file này gửi/nhận gói version bằng header 20 byte (đủ
     magic+tx_seq+rx_seq) — sai kích thước ngay từ gói đầu tiên, khiến toàn
     bộ phần đọc payload lệch byte và thiết bị bị coi là "không phản hồi
     đúng bắt tay phiên bản" (đúng lỗi người dùng gặp).
  2. Ngay sau khi thương lượng version>=2 thành công, phải gửi một gói
     MUX_PROTO_SETUP (payload 1 byte "\x07", không có "header" riêng) —
     usbmuxd thật gửi gói này ngay trong device_version_input() trước khi
     coi thiết bị là "active". Gói SETUP này cũng là lúc tx_seq/rx_seq bị
     RESET về 0 / 0xFFFF (xem send_packet() case MUX_PROTO_SETUP). Bản
     trước không hề gửi gói SETUP này.
  3. Từ đây, mỗi gói dùng mux_header ĐẦY ĐỦ 16 BYTE: {protocol:u32,
     length:u32, magic:u32, tx_seq:u16, rx_seq:u16} — chú ý tx_seq/rx_seq là
     16-bit, KHÔNG phải 32-bit. Bản trước dùng format "!IIIII" (5×u32 = 20
     byte) — sai 4 byte mỗi gói kể từ gói thứ hai trở đi, tự nó đã đủ làm
     hỏng toàn bộ phần lockdown/pairing dù gói version có sửa đúng hay
     không.
  4. protocol=6 (giống IPPROTO_TCP) đóng gói một "TCP header" tối giản
     (sport,dport,seq,ack,doff_flags,window,checksum,urgent) mô phỏng bắt
     tay SYN/SYN-ACK/ACK rồi truyền dữ liệu qua các gói PSH+ACK, đóng bằng
     FIN. Trường window 16-bit được usbmuxd thật SCALE >>8 khi gửi (và <<8
     khi đọc) để nhồi window thật (131072, không vừa 16-bit) vào trường 16
     -bit — bản trước gửi thẳng 131072 vào một trường "H" (struct.pack sẽ
     ném ngoại lệ vì 131072 > 65535), một lỗi crash độc lập với 2 lỗi trên.
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

# Header "đầy đủ" (version >= 2): protocol, length, magic, tx_seq(u16), rx_seq(u16) = 16 byte.
# usbmuxd/src/device.h: struct mux_header { uint32_t protocol; uint32_t length; uint32_t magic;
#                                            uint16_t tx_seq; uint16_t rx_seq; };
_HEADER_FMT = "!IIIHH"
_HEADER_LEN = struct.calcsize(_HEADER_FMT)
assert _HEADER_LEN == 16

# Header "ngắn" (version < 2, tức là dùng riêng cho đúng cặp gói version
# request/reply lúc chưa thương lượng xong): chỉ protocol + length = 8 byte.
_SHORT_HEADER_FMT = "!II"
_SHORT_HEADER_LEN = struct.calcsize(_SHORT_HEADER_FMT)
assert _SHORT_HEADER_LEN == 8

_VERSION_HDR_FMT = "!III"  # major, minor, padding
_VERSION_HDR_LEN = struct.calcsize(_VERSION_HDR_FMT)

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
        self.seq = 0  # ISN=0 — khớp usbmuxd (tx_seq bắt đầu từ 0)
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
                self.seq += 1  # FIN cũng chiếm 1 seq
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
        # 0 = chưa thương lượng (dùng header ngắn 8 byte, giống dev->version=0
        # trong usbmuxd thật — xem device_add()/device_data_input()).
        self._version = 0
        self._connections = {}
        self._next_src_port = 40000
        self._pump_thread = None
        self._stop = threading.Event()

    def start(self):
        print("[mux] Bắt tay phiên bản usbmux qua USB (header ngắn 8 byte)...")
        version_payload = struct.pack(_VERSION_HDR_FMT, VERSION_MAJOR, VERSION_MINOR, 0)
        self._send_raw(MUX_PROTO_VERSION, version_payload)
        header, payload = self._recv_raw()
        protocol = header[0]
        if protocol != MUX_PROTO_VERSION or len(payload) < _VERSION_HDR_LEN:
            raise MuxError("Thiết bị không phản hồi đúng bắt tay phiên bản usbmux.")
        major, minor, _padding = struct.unpack(_VERSION_HDR_FMT, payload[:_VERSION_HDR_LEN])
        if major not in (1, 2):
            raise MuxError(f"Thiết bị dùng phiên bản usbmux không được hỗ trợ: {major}.{minor}.")
        print(f"[mux] Thiết bị chấp nhận phiên bản usbmux {major}.{minor}.")

        # Từ đây header đầy đủ 16 byte được dùng (xem _header_size()). Phải
        # set self._version TRƯỚC khi gửi gói SETUP để _send_raw() chọn đúng
        # định dạng — đúng thứ tự usbmuxd thật làm trong device_version_input().
        self._version = major
        if self._version >= 2:
            # usbmuxd thật: gói MUX_PROTO_SETUP không có "header" phụ, chỉ có
            # payload 1 byte "\x07", và đây là lúc tx_seq/rx_seq bị reset.
            self._send_raw(MUX_PROTO_SETUP, b"\x07")

        self._pump_thread = threading.Thread(target=self._pump_loop, daemon=True)
        self._pump_thread.start()

    def stop(self):
        self._stop.set()

    def _header_size(self):
        return _HEADER_LEN if self._version >= 2 else _SHORT_HEADER_LEN

    def _send_raw(self, protocol, payload: bytes):
        header_size = self._header_size()
        total_len = header_size + len(payload)
        if header_size == _HEADER_LEN:
            if protocol == MUX_PROTO_SETUP:
                # send_packet() trong device.c reset seq ngay trước gói SETUP
                # đầu tiên: dev->tx_seq = 0; dev->rx_seq = 0xFFFF;
                self._tx_seq = 0
                self._rx_seq = 0xFFFF
            header = struct.pack(
                _HEADER_FMT, protocol, total_len, MAGIC,
                self._tx_seq & 0xFFFF, self._rx_seq & 0xFFFF,
            )
            self._tx_seq = (self._tx_seq + 1) & 0xFFFF
        else:
            header = struct.pack(_SHORT_HEADER_FMT, protocol, total_len)
        with self._io_lock:
            self._io.write(header + payload)

    def _recv_raw(self, timeout_s=15.0):
        header_size = self._header_size()
        raw_header = self._io.read_exact(header_size, timeout_s=timeout_s)
        if header_size == _HEADER_LEN:
            protocol, length, magic, tx_seq, rx_seq = struct.unpack(_HEADER_FMT, raw_header)
            # usbmuxd thật gương lại đúng trường rx_seq (không phải tx_seq)
            # của gói vừa nhận làm rx_seq cho gói kế tiếp ta gửi đi — xem
            # device_data_input(): "if (dev->version >= 2) dev->rx_seq =
            # ntohs(mhdr->rx_seq);". Đây là quy ước bắt buộc phải theo đúng
            # dù không thực sự có ý nghĩa "ACK" như TCP thật.
            self._rx_seq = rx_seq
        else:
            protocol, length = struct.unpack(_SHORT_HEADER_FMT, raw_header)
            tx_seq = rx_seq = 0
        if length < header_size:
            raise MuxError(f"mux_header length không hợp lệ: {length}")
        payload_len = length - header_size
        payload = self._io.read_exact(payload_len, timeout_s=timeout_s) if payload_len else b""
        return (protocol, length, tx_seq, rx_seq), payload

    def _pump_loop(self):
        _consecutive_errors = 0
        while not self._stop.is_set():
            try:
                header, payload = self._recv_raw(timeout_s=5.0)
                _consecutive_errors = 0  # reset on success
            except MuxError:
                # Không có dữ liệu trong 5s — bình thường khi đang chờ, không phải lỗi.
                _consecutive_errors += 1
                if _consecutive_errors >= 20:
                    # 20 × 5s = 100s không có dữ liệu nào từ USB — thiết bị có thể đã
                    # bị rút ra. Thông báo cho tất cả kết nối đang chờ để chúng không
                    # phải đợi hết timeout (có thể tới 60s) mới biết USB đã chết.
                    with self._io_lock:
                        for conn in list(self._connections.values()):
                            if not conn._closed.is_set():
                                conn._rx_queue.put(b"")
                                conn._closed.set()
                    _consecutive_errors = 0
                continue
            except Exception as e:
                print(f"[mux] Lỗi vòng lặp đọc USB: {e}")
                time.sleep(0.2)
                continue
            protocol = header[0]
            if protocol == MUX_PROTO_CONTROL:
                # device_control_input() thật: byte đầu là loại (3=ERROR,
                # 5=WARNING, 7=INFO), phần còn lại là chuỗi thông báo — log
                # lại để dễ chẩn đoán khi thiết bị từ chối/báo lỗi ở tầng mux.
                if payload:
                    kind = {3: "ERROR", 5: "WARNING", 7: "INFO"}.get(payload[0], f"type{payload[0]}")
                    msg = payload[1:].decode("utf-8", errors="replace")
                    print(f"[mux][control:{kind}] {msg}")
                continue
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
                # window trên dây bị usbmuxd thật scale >>8 khi gửi để nhồi
                # window thật (có thể > 65535) vào trường 16-bit — phải <<8
                # lại khi đọc, xem send_tcp()/device_tcp_input() trong device.c.
                conn.peer_window = (window << 8) or DEFAULT_WINDOW
                conn._on_segment(flags, seq, ack, data)

    def _send_tcp(self, conn: "MuxConnection", flags: int, payload: bytes):
        window_field = (DEFAULT_WINDOW >> 8) & 0xFFFF
        tcp_header = struct.pack(
            _TCPHDR_FMT,
            conn.src_port, conn.dst_port,
            conn.seq, conn.ack,
            (5 << 4), flags,
            window_field, 0, 0,
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
        conn.seq += 1  # SYN chiếm 1 seq — data đầu tiên phải dùng seq=1
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
