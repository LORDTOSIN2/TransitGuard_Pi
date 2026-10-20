import socket, time, struct, statistics

HOST = '10.160.116.212'
PORT = 8080
PATH = '/video_feed'
DURATION = 15.0

def parse_sof(data):
    p = data.find(b'\xff\xd8')
    if p < 0:
        return None
    i = p + 2
    while i + 9 < len(data):
        if data[i] != 0xFF:
            i += 1
            continue
        m = data[i+1]
        if m in (0xC0,0xC1,0xC2,0xC3,0xC5,0xC6,0xC7,0xC9,0xCA,0xCB,0xCD,0xCE,0xCF):
            h = struct.unpack('>H', data[i+5:i+7])[0]
            w = struct.unpack('>H', data[i+7:i+9])[0]
            return w, h, hex(m)
        if m == 0xD8 or 0xD0 <= m <= 0xD9 or m == 0x01:
            i += 2
            continue
        if i + 4 > len(data):
            break
        ln = struct.unpack('>H', data[i+2:i+4])[0]
        i += 2 + ln
    return None

s = socket.create_connection((HOST, PORT), timeout=5)
s.sendall(("GET %s HTTP/1.1\r\nHost: %s\r\nConnection: close\r\n\r\n" % (PATH, HOST)).encode())

buf = b''
while b'\r\n\r\n' not in buf:
    c = s.recv(4096)
    if not c:
        raise SystemExit('connection closed while reading headers')
    buf += c
head, pending = buf.split(b'\r\n\r\n', 1)
print('--- response head ---')
print(head.decode(errors='replace'))

BOUND = b'--frame'
SOI = b'\xff\xd8\xff'
frame_times = []
sec_counts = {}
bytes_total = 0
first_frame_blob = bytearray()
t0 = time.time()
s.settimeout(5)

while time.time() - t0 < DURATION:
    try:
        chunk = s.recv(65536)
    except socket.timeout:
        print('!! 5s with no data - stream may have stalled')
        break
    if not chunk:
        break
    now = time.time() - t0
    bytes_total += len(chunk)
    pending += chunk
    while True:
        idx = pending.find(BOUND)
        if idx == -1:
            break
        frame_times.append(time.time() - t0)
        if len(first_frame_blob) < 16384:
            first_frame_blob += pending[idx: idx + 16384]
        pending = pending[idx + len(BOUND):]
        si = int(now)
        sec_counts[si] = sec_counts.get(si, 0) + 1

total_time = time.time() - t0
s.close()

n = len(frame_times)
print('')
print('frames received (boundary markers): %d' % n)
if n >= 2:
    span = frame_times[-1] - frame_times[0]
    fps = (n - 1) / span if span > 0 else 0
    deltas = [frame_times[i+1] - frame_times[i] for i in range(n - 1)]
    print('measurement window: %.2f s (first->last frame)' % span)
    print('>>> AVERAGE FPS: %.2f' % fps)
    print('inter-frame ms: min %.1f | avg %.1f | max %.1f | stdev %.1f' % (
        min(deltas) * 1000, statistics.mean(deltas) * 1000,
        max(deltas) * 1000, statistics.pstdev(deltas) * 1000))
    print('')
    print('per-second frame counts:')
    for k in sorted(sec_counts):
        print('  t=%2ds : %d frames' % (k, sec_counts[k]))
    print('')
    print('throughput: %.1f KiB/s' % (bytes_total / total_time / 1024))
    print('avg per frame incl. headers: %.1f KiB' % (bytes_total / n / 1024))

blob = bytes(first_frame_blob)
print('JPEG SOI markers seen in capture: %d (sanity cross-check)' % blob.count(SOI))
print('resolution: %s' % (parse_sof(blob),))
