import threading, socket, time, statistics, http.client, subprocess, re, csv, urllib.request

PI  = '10.160.116.212'
GW  = '10.160.116.65'
PORT = 8080
OUT_CSV = 'wifi_latency_results.csv'

def pct(sorted_vals, p):
    if not sorted_vals: return None
    k = (len(sorted_vals) - 1) * p
    f = int(k); c = min(f + 1, len(sorted_vals) - 1)
    return sorted_vals[f] + (sorted_vals[c] - sorted_vals[f]) * (k - f)

def summarize(lat):
    if not lat: return None
    s = sorted(lat)
    return {
        'mean': statistics.mean(s), 'p50': pct(s, 0.5), 'p95': pct(s, 0.95),
        'p99': pct(s, 0.99), 'min': s[0], 'max': s[-1],
        'stdev': statistics.pstdev(s) if len(s) > 1 else 0.0
    }

def icmp_probe(host, n=300, wait_ms=900, size=32):
    out = subprocess.run(['ping', '-n', str(n), '-w', str(wait_ms), '-l', str(size), host],
                         capture_output=True, text=True, timeout=n * 3 + 60).stdout
    lat = []
    for m in re.finditer(r'time[=<]([\d.]+)\s*ms', out, re.I):
        val = float(m.group(1))
        lat.append(0.5 if '<' in m.group(0) else val)
    recv = len(re.findall(r'Reply from', out, re.I))
    return n, recv, lat

def tcp_probe(host, port, n=300, timeout=0.4, pace=0.012):
    lat = []; sent = 0
    for _ in range(n):
        sent += 1
        t = time.perf_counter()
        try:
            s = socket.create_connection((host, port), timeout=timeout)
            lat.append((time.perf_counter() - t) * 1000.0)
            s.close()
        except OSError:
            pass
        time.sleep(pace)
    return sent, len(lat), lat

def http_probe(host, port, path='/data', n=150, timeout=2.0, pace=0.012):
    lat = []; sent = 0; bad = 0
    for _ in range(n):
        sent += 1
        t = time.perf_counter()
        try:
            c = http.client.HTTPConnection(host, port, timeout=timeout)
            c.request('GET', path)
            r = c.getresponse(); body = r.read(); c.close()
            if r.status == 200 and b'"driver_state"' in body:
                lat.append((time.perf_counter() - t) * 1000.0)
            else:
                bad += 1
        except Exception:
            pass
        time.sleep(pace)
    return sent, len(lat), lat, bad

def wlan_info():
    out = subprocess.run(['netsh', 'wlan', 'show', 'interfaces'],
                         capture_output=True, text=True).stdout
    d = {}
    for k, v in re.findall(r'^\s*(.+?)\s*:\s*(.+?)\s*$', out, re.M):
        d[k.strip()] = v.strip()
    return {'signal': d.get('Signal', '?'), 'rx': d.get('Receive rate (Mbps)', '?'),
            'tx': d.get('Transmit rate (Mbps)', '?'), 'radio': d.get('Radio type', '?'),
            'band': d.get('Band', '?')}

load_stop = threading.Event()
load_stats = {'frames': 0, 'bytes': 0, 'errors': 0, 't0': None}

def video_load():
    while not load_stop.is_set():
        try:
            r = urllib.request.urlopen('http://%s:%d/video_feed' % (PI, PORT), timeout=5)
            if load_stats['t0'] is None:
                load_stats['t0'] = time.time()
            while not load_stop.is_set():
                d = r.read(65536)
                if not d: break
                load_stats['bytes'] += len(d)
                load_stats['frames'] += d.count(b'--frame')
        except Exception:
            load_stats['errors'] += 1
            if not load_stop.is_set(): time.sleep(0.3)

rows = []

def run_condition(label):
    w = wlan_info()
    print('\n########## CONDITION: %s ##########' % label)
    print('link: %s | %s | signal %s | rx %s / tx %s Mbps' % (w['radio'], w['band'], w['signal'], w['rx'], w['tx']))

    for probename, host in (('ICMP 32B -> Raspberry Pi', PI), ('ICMP 32B -> phone gateway', GW)):
        sent, recv, lat = icmp_probe(host, n=300)
        st = summarize(lat)
        if st:
            print('%-26s sent=%d recv=%d PDR=%5.1f%%  mean=%6.2f  p95=%6.2f  p99=%6.2f  min=%5.2f max=%6.2f ms'
                  % (probename, sent, recv, 100.0 * recv / sent, st['mean'], st['p95'], st['p99'], st['min'], st['max']))
            rows.append([label, probename, sent, recv, round(100.0 * recv / sent, 1),
                         round(st['mean'], 2), round(st['p50'], 2), round(st['p95'], 2), round(st['p99'], 2),
                         round(st['min'], 2), round(st['max'], 2), round(st['stdev'], 2), w['signal'], w['rx'], w['tx']])
        else:
            print('%-26s sent=%d recv=%d PDR=0%% (no ICMP replies)' % (probename, sent, recv))
            rows.append([label, probename, sent, recv, 0.0, '', '', '', '', '', '', '', w['signal'], w['rx'], w['tx']])

    sent, recv, lat = tcp_probe(PI, PORT, n=300)
    st = summarize(lat)
    print('%-26s sent=%d recv=%d PDR=%5.1f%%  mean=%6.2f  p95=%6.2f  p99=%6.2f  min=%5.2f max=%6.2f ms'
          % ('TCP connect :8080', sent, recv, 100.0 * recv / sent, st['mean'], st['p95'], st['p99'], st['min'], st['max']))
    rows.append([label, 'TCP connect :8080', sent, recv, round(100.0 * recv / sent, 1),
                 round(st['mean'], 2), round(st['p50'], 2), round(st['p95'], 2), round(st['p99'], 2),
                 round(st['min'], 2), round(st['max'], 2), round(st['stdev'], 2), w['signal'], w['rx'], w['tx']])

    sent, recv, lat, bad = http_probe(PI, PORT, '/data', n=150)
    st = summarize(lat)
    print('%-26s sent=%d recv=%d PDR=%5.1f%%  mean=%6.2f  p95=%6.2f  p99=%6.2f  min=%5.2f max=%6.2f ms  (bad resp: %d)'
          % ('HTTP GET /data (app-level)', sent, recv, 100.0 * recv / sent, st['mean'], st['p95'], st['p99'], st['min'], st['max'], bad))
    rows.append([label, 'HTTP GET /data (app-level)', sent, recv, round(100.0 * recv / sent, 1),
                 round(st['mean'], 2), round(st['p50'], 2), round(st['p95'], 2), round(st['p99'], 2),
                 round(st['min'], 2), round(st['max'], 2), round(st['stdev'], 2), w['signal'], w['rx'], w['tx']])

print('=== Wi-Fi latency / PDR measurement: PC 10.160.116.211 -> Raspberry Pi ===')
print('started: %s' % time.strftime('%Y-%m-%d %H:%M:%S'))

run_condition('Idle (no video stream)')

print('\n--- starting video load (~20 fps MJPEG) on /video_feed ---')
th = threading.Thread(target=video_load, daemon=True)
th.start()
time.sleep(4)
t_load = time.time()
run_condition('Loaded (camera streaming)')
load_dur = time.time() - t_load
load_stop.set()
time.sleep(0.5)

if load_stats['t0'] and load_dur > 0:
    print('\nload during measurement: %d frames / %.1f KiB over ~%.1f s  (~%.1f fps, ~%.1f KiB/s), errors=%d'
          % (load_stats['frames'], load_stats['bytes'] / 1024.0, load_dur,
             load_stats['frames'] / load_dur, load_stats['bytes'] / 1024.0 / load_dur, load_stats['errors']))

with open(OUT_CSV, 'w', newline='', encoding='utf-8') as f:
    wtr = csv.writer(f)
    wtr.writerow(['wi_fi_condition', 'probe', 'packets_sent', 'packets_received', 'pdr_percent',
                  'mean_latency_ms', 'p50_ms', 'p95_ms', 'p99_ms', 'min_ms', 'max_ms', 'stdev_ms',
                  'signal', 'rx_mbps', 'tx_mbps'])
    wtr.writerows(rows)
print('\nCSV written: %s' % OUT_CSV)
