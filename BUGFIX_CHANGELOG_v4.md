# Changelog v4 — Kiểm chứng độc lập lại toàn bộ giao thức + vá lỗ hổng "treo im lặng"

## Bối cảnh

Người dùng báo cáo: log dừng lại đúng ở dòng

```
[mux] Thiết bị chấp nhận phiên bản usbmuxd 2.0
```

và không có gì xảy ra tiếp theo — không popup "Trust This Computer?" trên
iPhone, không có dòng log lỗi nào sau đó.

## Việc đã làm trong lần rà soát này

1. **Kiểm chứng lại toàn bộ định dạng gói tin mux/TCP-giả-lập** trực tiếp với
   mã nguồn gốc `libimobiledevice/usbmuxd` (`device.c`, `device.h`, `usb.c`,
   `usb.h` — tải trực tiếp từ repo upstream, KHÔNG dựa vào các lần sửa trước).
   Đã đối chiếu từng byte: header ngắn 8 byte cho bắt tay phiên bản, header
   đầy đủ 16 byte (`protocol,length,magic,tx_seq,rx_seq`), gói `SETUP`,
   layout `struct tcphdr` kiểu BSD (20 byte), việc scale trường `window`
   (`>>8` lúc gửi / `<<8` lúc nhận), thứ tự bit trong byte `doff|flags`,
   ngữ nghĩa SYN/SYN-ACK/ACK, cách các trường `checksum`/`urgent` luôn bằng 0.
   **Kết quả: không tìm thấy sai lệch nào so với mã nguồn thật** — phần giao
   thức mức thấp mà 3 lần sửa trước đã làm là đúng.

2. **So sánh kiến trúc với `termux-usbmuxd` (LLOS-Lord)** để trả lời câu hỏi
   "cách làm này có khả thi không": `termux-usbmuxd` KHÔNG viết lại giao thức
   — nó biên dịch `usbmuxd`/`libimobiledevice` THẬT (C, qua Termux + NDK/
   clang) và chỉ dùng `termux-usb` để lấy quyền truy cập USB thô, sau đó chạy
   `idevicepair`/`ideviceinfo` (binary thật) qua socket. Đây là lý do nó đáng
   tin cậy hơn về mặt lý thuyết: không có chỗ nào tự triển khai lại giao thức
   nhị phân từ đầu. Sideloadtool đi theo hướng khó hơn nhiều (viết lại toàn bộ
   usbmux + TCP-giả-lập + lockdown + TLS bằng Python chạy trên Chaquopy,
   không cần Termux/root) — về giao thức thì đã đúng theo kiểm chứng ở trên,
   nhưng chưa từng được xác nhận trên phần cứng thật.

3. **Tìm ra một lỗ hổng thực sự có thể gây "treo vĩnh viễn, không log, không
   lỗi"**: `_RawIo.write()` trong `mux_usb.py` có vòng lặp
   `while offset < len(data): ... offset += written`. Nếu `bulkWrite()` phía
   Kotlin từng trả về đúng `0` (không phải `None`, không âm — nghĩa là "không
   lỗi nhưng chưa ghi được byte nào", có thể xảy ra khi endpoint tạm thời bị
   kẹt), vòng lặp này **không bao giờ tiến lên và cũng không bao giờ ném lỗi**
   — khớp chính xác với triệu chứng "log dừng lại, im lặng tuyệt đối, không
   có exception nào được `do_sideload()` bắt và in ra". Đã vá: đếm số lần ghi
   0-byte liên tiếp, sau 20 lần thì raise lỗi rõ ràng thay vì treo mãi mãi.

4. **Vá "im lặng" ở các điểm chờ dài** — đây là thay đổi quan trọng nhất để
   CHẨN ĐOÁN chính xác vấn đề ở lần chạy tiếp theo, vì trước đây log không hề
   cho biết app đang ở bước nào trong lúc chờ:
   - `MuxConnection.wait_connected()` (chờ SYN-ACK khi mở kênh tới lockdownd/
     AFC): trước đây chờ im lặng 15 giây rồi mới có 1 dòng lỗi (hoặc không ai
     thấy nếu người dùng tắt sớm). Giờ in tiến độ mỗi ~3 giây.
   - `MuxConnection.recv()` (dùng cho MỌI lần đợi phản hồi, kể cả 60 giây chờ
     người dùng bấm "Tin cậy" trên hộp thoại Pair): trước đây chờ im lặng cả
     60 giây. Giờ in tiến độ mỗi ~10 giây khi timeout đủ dài.
   - `LockdownClient.__init__()`: thêm log "Đang mở kết nối lockdownd..." và
     "Đã kết nối lockdownd" — đây chính là bước gọi `connect()` ĐẦU TIÊN sau
     dòng "chấp nhận phiên bản usbmuxd 2.0", tức là đúng khoảng trống mà
     người dùng báo cáo bị im lặng.
   - `pair_device()`: thêm log trước/sau khi lấy `DevicePublicKey`.
   - Vòng lặp đọc USB (`_pump_loop`): thêm log khi nhận được gói TCP không
     khớp kết nối nào đang mở, và log khi nhận gói với protocol lạ — để phân
     biệt "thiết bị hoàn toàn không phản hồi gì" với "thiết bị có phản hồi
     nhưng bị lệch cổng ở đâu đó".
   - `MuxDevice.connect()`: dọn dẹp connection khỏi `_connections` khi
     `wait_connected()` thất bại (trước đây rò rỉ, để lại connection "chết"
     trong dict mãi mãi).

## Kết luận thực tế (nói thẳng, không tô hồng)

- **Không tìm thấy lỗi giao thức mới** ở tầng usbmux/TCP-giả-lập/pairing —
  đã kiểm chứng độc lập với mã nguồn thật, khớp hoàn toàn. 3 lần sửa trước
  (PEM vs DER, thiếu DeviceCertificate, TLS SECLEVEL, thứ tự WiFiAddress...)
  có vẻ đã giải quyết đúng các lỗi giao thức thực sự tồn tại trước đó.
- Lỗi còn lại nhiều khả năng nằm ở tầng USB vật lý (cáp, hub, driver OEM của
  từng dòng máy Android cụ thể) — thứ **không thể kiểm chứng được trong môi
  trường này vì không có iPhone và cổng USB thật để cắm vào**.
- Sau bản vá này, lần chạy tiếp theo sẽ cho biết CHÍNH XÁC app đang treo ở
  bước nào (mở kết nối mux, chờ lockdownd trả lời, hay chờ bạn bấm Trust) —
  đây là thông tin bắt buộc phải có để sửa tiếp nếu vẫn còn lỗi, vì hiện tại
  không ai (kể cả người viết lại mã nguồn) có cách nào biết được nếu không
  có log chi tiết hơn từ một lần chạy thật trên máy.
- Nếu sau bản vá này log vẫn dừng ngay tại `[lockdown] Đang mở kết nối
  lockdownd...` và không có dòng "vẫn đang chờ..." nào xuất hiện sau 3 giây,
  đó là dấu hiệu treo ở tầng USB Host API (Kotlin) chứ không phải giao thức
  Python — cần xem log USB (permission, claim interface) trong Logcat.
