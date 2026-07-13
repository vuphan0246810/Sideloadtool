"""mux_usb.py — triển khai lại (clean-room) giao thức "usbmux" của Apple chạy
trực tiếp trên USB bulk transfer, không qua daemon usbmuxd.

Giao thức đã đối chiếu byte-for-byte với mã nguồn tham khảo chính chủ
libimobiledevice/usbmuxd (src/device.c, src/usb.h, src/device.h).

FIX v6 (2026-07-13):
  - CRITICAL: RST fast-fail — khi thiết bị gửi RST, đặt b"" vào _rx_queue để
    recv() bị unblock ngay lập tức thay vì chờ hết timeout (10s / 30s). Điều
    này cực kỳ quan trọng với iOS 16+ vì lockdownd gửi RST khi nhận plaintext
    (kỳ vọng SSL), và recv() cũ sẽ chờ 30s trước khi báo lỗi — trông như app
    bị treo.
  - recv(): Khi nhận b"" sentinel từ queue và _closed đang set, raise MuxError
    "RST/FIN" ngay lập tức thay vì loop tiếp (tránh leak qua vòng lặp).
  - Tất cả fix từ v5 giữ nguyên (header 8/16 byte, SETUP, window scaling,
    pump timeout 10s, consecutive-error counter, ZLP bulkWrite, v.v.).
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
MUX_PROTO_TCP = 6  # mô phỏng IPPROTO_TCP

TCP_FLAG_FIN = 0x01
TCP_FLAG_SYN = 0x02
TCP_FLAG_RST = 0x04
TCP_FLAG_PSH = 0x08
TCP_FLAG_ACK = 0x10

# Header đầy đủ (version >= 2): protocol, length, magic, tx_seq(u16), rx_seq(u16) = 16 byte.
_HEADER_FMT = "!IIIHH"
_HEADER_LEN = struct.calcsize(_HEADER_FMT)
assert _HEADER_LEN == 16

# Header ngắn (version < 2, chỉ dùng cho cặp gói version request/reply): 8 byte.
_SHORT_HEADER_FMT = "!II"
_SHORT_HEADER_LEN = struct.calcsize(_SHORT_HEADER_FMT)
assert _SHORT_HEADER_LEN == 8

_VERSION_HDR_FMT = "!III"  # major, minor, padding
_VERSION_HDR_LEN = struct.calcsize(_VERSION_HDR_FMT)

_TCPHDR_FMT = "!HHIIBBHHH"  # sport,dport,seq,ack,doff,flags,window,checksum,urgent
_TCPHDR_LEN = struct.calcsize(_TCPHDR_FMT)

DEFAULT_WINDOW = 131072

# Pump read timeout — đủ dài để device phản hồi chậm (Trust dialog) mà không
# bị đếm oan là "thiết bị im lặng".
_PUMP_READ_TIMEOUT_S = 10.0


class MuxError(Exception):
    pass


class MuxRstError(MuxError):
    """Thiết bị gửi RST — kết nối bị từ chối/đóng sớm.
    Phân biệt với MuxError thông thường để caller có thể thử lại với SSL."""
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
        stall_count = 0
        while offset < len(data):
            chunk = data[offset: offset + 16384]
            written = self._transport.bulkWrite(chunk)
            if written is None or written < 0:
                raise MuxError("bulkWrite thất bại (USB có thể đã bị rút ra).")
            if written == 0:
                stall_count += 1
                if stall_count >= 20:
                    raise MuxError(
                        "bulkWrite liên tục trả về 0 byte — endpoint USB bị treo "
                        "hoặc thiết bị đã bị rút ra giữa chừng."
                    )
                time.sleep(0.01)
                continue
            stall_count = 0
            offset += written

    def read_exact(self, n, timeout_s=15.0):
        """Đọc chính xác n byte từ USB, tích lũy qua nhiều lần bulkRead nếu cần."""
        deadline = time.time() + timeout_s
        while len(self._rx_buffer) < n:
            remaining = deadline - time.time()
            if remaining <= 0:
                raise MuxError(
                    f"Timeout khi đọc {n} byte từ USB "
                    f"(mới có {len(self._rx_buffer)} byte trong buffer)."
                )
            poll_ms = int(max(50, min(remaining * 1000, 2000)))
            chunk = self._transport.bulkRead(poll_ms)
            if chunk:
                self._rx_buffer.extend(bytes(chunk))
        result = bytes(self._rx_buffer[:n])
        del self._rx_buffer[:n]
        return result


class MuxConnection:
    """Một kênh logic (multiplex) trên cùng một kết nối USB vật lý."""

    def __init__(self, device: "MuxDevice", src_port: int, dst_port: int):
        self.device = device
        self.src_port = src_port
        self.dst_port = dst_port
        self.seq = 0
        self.ack = 0
        self.peer_window = DEFAULT_WINDOW
        self._rx_queue: "queue.Queue[bytes]" = queue.Queue()
        self._connected = threading.Event()
        self._closed = threading.Event()
        self._rx_leftover = b""

    def _on_segment(self, tcp_flags, seq, ack, payload):
        if tcp_flags & TCP_FLAG_RST:
            print(f"[mux] Thiết bị gửi RST tới cổng host {self.src_port} — kết nối bị từ chối/đóng.")
            self._closed.set()
            self._connected.set()
            # [FIX v6 CRITICAL] Đặt sentinel vào queue để recv() unblock ngay
            # lập tức thay vì chờ hết timeout (10s / 30s). Không có dòng này,
            # recv() luôn mất đúng bằng thời gian timeout của nó mỗi khi RST
            # đến — khiến toàn bộ pairing flow trông như bị treo 30s im lặng.
            self._rx_queue.put(b"")
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
        deadline = time.time() + timeout
        poll_interval = 3.0
        waited = 0.0
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                raise MuxError(
                    f"Timeout mở kết nối mux tới cổng {self.dst_port} sau {timeout:.0f}s — "
                    "thiết bị không gửi SYN-ACK. Kiểm tra: cáp USB, màn hình iPhone không bị khoá."
                )
            wait_slice = min(poll_interval, remaining)
            if self._connected.wait(wait_slice):
                break
            waited += wait_slice
            print(f"[mux] Vẫn đang chờ thiết bị phản hồi kết nối tới cổng {self.dst_port}... ({waited:.0f}s/{timeout:.0f}s)")
        if self._closed.is_set():
            raise MuxRstError(f"Thiết bị từ chối kết nối tới cổng {self.dst_port} (RST).")

    def send(self, data: bytes):
        if not data:
            return
        self.device._send_tcp(self, TCP_FLAG_ACK | TCP_FLAG_PSH, payload=data)
        self.seq += len(data)

    def recv(self, size: int, timeout=30.0) -> bytes:
        deadline = time.time() + timeout
        poll_interval = 10.0
        waited = 0.0
        verbose = timeout > poll_interval
        while len(self._rx_leftover) < size:
            # [FIX v6] Fast-fail nếu kết nối đã bị RST/FIN và queue rỗng.
            # Không có check này, vòng lặp tiếp tục chờ cho đến khi deadline —
            # gây ra "30s treo im lặng" mỗi khi iOS 16+ lockdownd gửi RST.
            if self._closed.is_set() and self._rx_queue.empty():
                raise MuxRstError(
                    f"Kết nối bị đóng sớm (RST/FIN) bởi thiết bị "
                    f"(cổng {self.dst_port}) — không còn dữ liệu để đọc."
                )
            remaining = deadline - time.time()
            if remaining <= 0:
                raise MuxError("Timeout khi chờ dữ liệu từ thiết bị (mux).")
            slice_timeout = min(poll_interval, remaining)
            try:
                chunk = self._rx_queue.get(timeout=slice_timeout)
            except queue.Empty:
                waited += slice_timeout
                if verbose:
                    print(f"[mux] Vẫn đang chờ dữ liệu phản hồi từ thiết bị (cổng {self.dst_port})... ({waited:.0f}s/{timeout:.0f}s)")
                continue
            # [FIX v6] Khi nhận được sentinel b"" (từ RST hoặc FIN), raise ngay
            # nếu closed đã set — tránh loop tiếp mà không bao giờ nhận data.
            if chunk == b"":
                if self._closed.is_set():
                    raise MuxRstError(
                        f"Kết nối bị đóng sớm (RST/FIN) bởi thiết bị "
                        f"(cổng {self.dst_port}) — không còn dữ liệu để đọc."
                    )
                continue
            self._rx_leftover += chunk
        result, self._rx_leftover = self._rx_leftover[:size], self._rx_leftover[size:]
        return result

    def close(self):
        if not self._closed.is_set():
            try:
                self.device._send_tcp(self, TCP_FLAG_FIN | TCP_FLAG_ACK, payload=b"")
                self.seq += 1
            except Exception:
                pass
        self._closed.set()
        self.device._unregister(self)


class MuxDevice:
    """Quản lý một phiên usbmux trên một thiết bị Apple đã claim qua USB Host API."""

    def __init__(self):
        self._io = _RawIo()
        self._io_lock = threading.Lock()
        self._tx_seq = 0
        self._rx_seq = 0
        self._version = 0
        self._connections = {}
        self._next_src_port = 40000
        self._pump_thread = None
        self._stop = threading.Event()

    def start(self):
        # Xả sạch bộ đệm nhận USB trước khi gửi version request.
        _flush_count = 0
        while True:
            try:
                stale = self._io._transport.bulkRead(100)
                if stale is None:
                    break
                _flush_count += len(bytes(stale))
            except Exception:
                break
        if _flush_count:
            print(f"[mux] Đã xả {_flush_count} byte dữ liệu cũ trong bộ đệm USB.")

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

        self._version = major
        if self._version >= 2:
            self._send_raw(MUX_PROTO_SETUP, b"\x07")
            time.sleep(0.05)

        self._pump_thread = threading.Thread(target=self._pump_loop, daemon=True, name="mux-pump")
        self._pump_thread.start()
        print("[mux] Pump thread đã khởi động.")

    def stop(self):
        self._stop.set()

    def _header_size(self):
        return _HEADER_LEN if self._version >= 2 else _SHORT_HEADER_LEN

    def _send_raw(self, protocol, payload: bytes):
        header_size = self._header_size()
        total_len = header_size + len(payload)
        if header_size == _HEADER_LEN:
            if protocol == MUX_PROTO_SETUP:
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
        _consecutive_no_data = 0
        _had_data_recently = False
        while not self._stop.is_set():
            try:
                header, payload = self._recv_raw(timeout_s=_PUMP_READ_TIMEOUT_S)
                _consecutive_no_data = 0
                _had_data_recently = True
            except MuxError:
                if not self._io._rx_buffer:
                    _consecutive_no_data += 1
                if _consecutive_no_data >= 12:
                    if _had_data_recently:
                        print(f"[mux] Cảnh báo: không có dữ liệu từ USB trong {_consecutive_no_data * _PUMP_READ_TIMEOUT_S:.0f}s — thiết bị có thể đã bị rút.")
                    for conn in list(self._connections.values()):
                        if not conn._closed.is_set():
                            conn._rx_queue.put(b"")
                            conn._closed.set()
                    _consecutive_no_data = 0
                    _had_data_recently = False
                continue
            except Exception as e:
                print(f"[mux] Lỗi vòng lặp đọc USB: {e}")
                time.sleep(0.2)
                continue

            protocol = header[0]
            if protocol == MUX_PROTO_CONTROL:
                if payload:
                    kind = {3: "ERROR", 5: "WARNING", 7: "INFO"}.get(payload[0], f"type{payload[0]}")
                    msg = payload[1:].decode("utf-8", errors="replace")
                    print(f"[mux][control:{kind}] Thiết bị gửi thông báo: {msg}")
                    if payload[0] == 3:
                        print("[mux] Thiết bị gửi MUX_PROTO_CONTROL ERROR — đóng tất cả kết nối.")
                        for conn in list(self._connections.values()):
                            if not conn._closed.is_set():
                                conn._rx_queue.put(b"")
                                conn._closed.set()
                continue
            if protocol != MUX_PROTO_TCP:
                print(f"[mux][debug] Bỏ qua gói protocol lạ: {protocol}")
                continue
            if len(payload) < _TCPHDR_LEN:
                print(f"[mux][debug] TCP payload quá ngắn: {len(payload)} < {_TCPHDR_LEN}")
                continue
            sport, dport, seq, ack, doff, flags, window, checksum, urgent = struct.unpack(
                _TCPHDR_FMT, payload[:_TCPHDR_LEN]
            )
            data = payload[_TCPHDR_LEN:]
            conn = self._connections.get(dport)
            if conn:
                conn.peer_window = (window << 8) or DEFAULT_WINDOW
                conn._on_segment(flags, seq, ack, data)
            else:
                print(
                    f"[mux][debug] Gói TCP tới cổng host {dport} (từ cổng thiết bị {sport}, "
                    f"flags=0x{flags:02x}, data={len(data)}B) không khớp kết nối đang mở nào — bỏ qua."
                )

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
        """Mở một kênh logic mới tới dst_port trên thiết bị."""
        src_port = self._next_src_port
        self._next_src_port += 1
        conn = MuxConnection(self, src_port, dst_port)
        self._connections[src_port] = conn
        print(f"[mux] Mở kênh logic: cổng host {src_port} -> cổng thiết bị {dst_port} (gửi SYN)...")
        self._send_tcp(conn, TCP_FLAG_SYN, payload=b"")
        conn.seq += 1
        try:
            conn.wait_connected(timeout=timeout)
        except MuxError:
            self._unregister(conn)
            raise
        print(f"[mux] ✅ Đã thiết lập kênh logic tới cổng thiết bị {dst_port}.")
        return conn

    def _unregister(self, conn: MuxConnection):
        self._connections.pop(conn.src_port, None)


_device_singleton = None
_device_lock = threading.Lock()


def get_device() -> MuxDevice:
    """Trả về (và khởi tạo nếu cần) phiên MuxDevice dùng chung."""
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
    """Huỷ singleton MuxDevice hiện tại một cách an toàn."""
    global _device_singleton
    old = None
    with _device_lock:
        if _device_singleton is not None:
            old = _device_singleton
            old.stop()
        _device_singleton = None
    if old is not None and old._pump_thread is not None:
        old._pump_thread.join(timeout=6.0)
        if old._pump_thread.is_alive():
            print("[mux] Cảnh báo: pump thread cũ không thoát trong 6s — tiếp tục.")
    print("[mux] Đã reset MuxDevice singleton.")
