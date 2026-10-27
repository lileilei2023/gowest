"""Line check, run on YOUR computer: what route, latency and speed does it get?

Software tuning moves throughput by tens of percent; the route between you and
the server moves it by multiples. This measures that route directly, with no
proxy in between: the path (traceroute), TCP handshake latency and loss to the
proxy port, and a plain download of the server's private speed-test file.
Nothing is changed anywhere. Run it again in the evening peak (20:00–23:00
China time) to see the line at its worst.
"""
import json
import platform
import re
import shutil
import socket
import statistics
import subprocess
import sys
import time
import urllib.request

# Well-known backbone prefixes seen as hops on the way out of mainland China.
# Labels describe the carrier network, which is what decides peak-hour quality.
BACKBONES = (
    ('59.43.', 'CN2'),
    ('202.97.', '163'),
    ('218.105.', '9929'), ('210.51.', '9929'),
    ('219.158.', '4837'),
    ('223.118.', 'CMI'), ('223.119.', 'CMI'), ('223.120.', 'CMI'),
    ('221.183.', 'CMNET'), ('221.176.', 'CMNET'),
)
VERDICTS = {
    'CN2 GIA': ('电信 CN2 GIA', '优质线路：晚高峰也比较稳'),
    'CN2 GT': ('电信 CN2 GT', '中等：国内段走普通骨干，晚高峰可能变慢'),
    '163': ('电信 163 骨干', '普通线路：晚高峰容易拥堵、丢包'),
    '9929': ('联通 9929（精品网）', '优质线路：晚高峰也比较稳'),
    '4837': ('联通 4837', '普通线路：晚高峰容易拥堵'),
    'CMI': ('移动 CMI', '中等偏好：移动用户通常不错'),
    'CMNET': ('移动 CMNET 骨干', '普通线路'),
}


def trace(ip, max_hops=30):
    """Hop IPs in order ('' for a silent hop). Uses whatever the OS provides."""
    if platform.system() == 'Windows':
        command = ['tracert', '-d', '-h', str(max_hops), '-w', '800', ip]
    elif shutil.which('traceroute'):
        command = ['traceroute', '-n', '-q', '1', '-w', '1', '-m', str(max_hops), ip]
    elif shutil.which('tracepath'):
        command = ['tracepath', '-n', '-m', str(max_hops), ip]
    else:
        return None
    try:
        out = subprocess.run(command, capture_output=True, text=True, timeout=120).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    hops = []
    for line in out.splitlines():
        if not re.match(r'\s*\d+[:\s]', line):
            continue
        found = re.findall(r'\b(?:\d{1,3}\.){3}\d{1,3}\b', line)
        hops.append(found[0] if found else '')
    return hops


def classify(hops):
    """(key, label, verdict) from the backbone prefixes on the path."""
    seen = []
    for hop in hops:
        for prefix, net in BACKBONES:
            if hop.startswith(prefix) and net not in seen:
                seen.append(net)
    if 'CN2' in seen:
        key = 'CN2 GT' if '163' in seen else 'CN2 GIA'
    else:
        key = next((n for n in ('9929', '4837', 'CMI', '163', 'CMNET') if n in seen), '')
    if not key:
        return '', '未识别', '路径上没有常见的国内骨干网特征（可能不在中国大陆，或路由器不回应）'
    return (key,) + VERDICTS[key]


def latency(ip, port, count=20, timeout=3):
    """TCP handshake times in ms; failures count as loss. ICMP is often blocked, TCP is not."""
    times, lost = [], 0
    for _ in range(count):
        start = time.perf_counter()
        try:
            socket.create_connection((ip, port), timeout=timeout).close()
            times.append((time.perf_counter() - start) * 1000)
        except OSError:
            lost += 1
        time.sleep(0.15)
    if not times:
        return {'loss': 100.0}
    return {'avg': round(statistics.mean(times), 1), 'min': round(min(times), 1), 'max': round(max(times), 1),
            'jitter': round(statistics.pstdev(times), 1), 'loss': round(lost * 100 / count, 1)}


def speed(url, limit=16 * 2**20, seconds=15):
    """Mbps of one plain download, deliberately bypassing any proxy settings."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    received, start = 0, time.perf_counter()
    try:
        with opener.open(url, timeout=10) as response:
            while received < limit and time.perf_counter() - start < seconds:
                chunk = response.read(256 * 1024)
                if not chunk:
                    break
                received += len(chunk)
    except Exception as error:
        return {'error': type(error).__name__}
    elapsed = time.perf_counter() - start
    return {'mbps': round(received * 8 / elapsed / 1e6, 1), 'megabytes': round(received / 2**20, 1)}


def check(info):
    ip, port = info['server_ip'], info['proxy_ports'][0]
    hops = trace(ip)
    key, label, verdict = classify(hops or [])
    result = {'route': {'key': key, 'label': label, 'verdict': verdict,
                        'hops': len(hops) if hops is not None else None,
                        'path': [h for h in (hops or []) if h]},
              'latency': latency(ip, port)}
    if info.get('speedtest_url'):
        result['speed'] = speed(info['speedtest_url'])
    hour = time.localtime().tm_hour
    result['peak'] = 20 <= hour <= 23
    return result


def main(argv):
    r = check(json.loads(argv[1]))
    route, lat = r['route'], r['latency']
    print(f"  线路：{route['label']} —— {route['verdict']}")
    if route['hops'] is None:
        print('       （这台电脑没有 traceroute / tracert，跳过路由分析）')
    if 'avg' in lat:
        print(f"  延迟：平均 {lat['avg']} ms（{lat['min']}–{lat['max']}），抖动 {lat['jitter']} ms，丢包 {lat['loss']}%")
    else:
        print('  延迟：全部超时，代理端口连不上')
    if 'speed' in r:
        sp = r['speed']
        print(f"  下载：{sp['mbps']} Mbps（{sp['megabytes']} MB，单连接、不经代理）" if 'mbps' in sp
              else f"  下载：失败（{sp['error']}）")
    print('  ' + ('现在是晚高峰，结果接近最差情况。' if r['peak'] else '建议晚上 8–11 点再测一次，看看晚高峰的表现。'))
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
