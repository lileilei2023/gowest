#!/usr/bin/env python3
"""Opt-in, bounded client checks. Does not edit proxy settings or select nodes."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import datetime
import ipaddress
import json
import math
import secrets
import socket
import statistics
import struct
import subprocess
import sys
import time
from urllib.parse import urlsplit


def mapped_address(data, transaction):
    if len(data) < 20:
        raise ValueError('Short STUN response')
    kind, length, cookie = struct.unpack('!HHI', data[:8])
    if kind != 0x101 or cookie != 0x2112A442 or data[8:20] != transaction or length > len(data) - 20:
        raise ValueError('Unmatched STUN response')
    offset = 20
    while offset + 4 <= 20 + length:
        tag, size = struct.unpack('!HH', data[offset:offset+4])
        value = data[offset+4:offset+4+size]
        if len(value) != size:
            raise ValueError('Truncated STUN attribute')
        if tag in (0x20, 1) and size >= 8:
            family = value[1]
            count = 4 if family == 1 else 16 if family == 2 else 0
            if count and size == count + 4:
                address = value[4:]
                if tag == 0x20:
                    mask = struct.pack('!I', cookie) + transaction
                    address = bytes(a ^ b for a, b in zip(address, mask))
                return str(ipaddress.ip_address(address))
        offset += 4 + ((size + 3) // 4) * 4
    raise ValueError('No mapped address')


def stun(host, port):
    tx = secrets.token_bytes(12)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.settimeout(5)
        sock.connect((host, port))
        sock.send(struct.pack('!HHI', 1, 0, 0x2112A442) + tx)
        return mapped_address(sock.recv(2048), tx)


def download_round(base, size, index):
    result = {'round': index, 'started_at': datetime.datetime.now(datetime.timezone.utc).isoformat(),
              'expected_bytes': size, 'completed': False}
    try:
        response = subprocess.run(base + ['--max-filesize', str(size), '-o', '/dev/null',
                                         '-H', 'Cache-Control: no-cache', '-w',
                                         '%{http_code}\t%{size_download}\t%{time_total}\t%{speed_download}\t%{time_starttransfer}',
                                         'https://speed.cloudflare.com/__down?bytes=' + str(size)
                                         + '&nonce=' + secrets.token_hex(8)],
                                  capture_output=True, text=True, timeout=35)
    except subprocess.TimeoutExpired:
        result['error'] = 'curl process timed out; incomplete sample'
        return result
    result['curl_exit'] = response.returncode
    try:
        code, count, seconds, speed, ttfb = response.stdout.strip().split('\t')
        count, seconds, speed, ttfb = float(count), float(seconds), float(speed), float(ttfb)
        if (not all(math.isfinite(x) for x in (count, seconds, speed, ttfb)) or count < 0
                or not count.is_integer() or not 0 <= ttfb <= seconds):
            raise ValueError('Invalid curl metrics')
        result.update(http_status=int(code), downloaded_bytes=int(count), seconds=seconds, first_byte_seconds=ttfb)
    except (ValueError, OverflowError):
        result['error'] = 'invalid curl metrics; incomplete sample'
        return result
    if response.returncode != 0 or int(code) != 200 or count != size or seconds <= 0 or speed <= 0:
        result['error'] = 'requires curl success, HTTP 200, full byte count and positive timing/speed'
    else:
        result.update(completed=True, download_mbps=round(speed * 8 / 1000000, 2))
        if seconds > ttfb:
            result['body_phase_mbps_estimate'] = round(count * 8 / (seconds - ttfb) / 1e6, 2)
            result['body_phase_note'] = 'Excludes request/first-byte latency; estimate, not a sustained-speed guarantee'
    return result


def summarize_downloads(rounds):
    speeds = [row['download_mbps'] for row in rounds if row['completed']]
    return {'attempted': len(rounds), 'completed': len(speeds), 'failed': len(rounds) - len(speeds),
            'all_completed': bool(rounds) and len(speeds) == len(rounds),
            'min_mbps': min(speeds) if speeds else None,
            'median_mbps': round(statistics.median(speeds), 2) if speeds else None,
            'max_mbps': max(speeds) if speeds else None,
            'statistics_scope': 'complete samples only; not an upload or livestream stability test'}


def download_batch(base, size, index, parallel):
    started = time.monotonic()
    with ThreadPoolExecutor(max_workers=parallel) as pool:
        rows = list(pool.map(lambda _: download_round(base, size, index), range(parallel)))
    seconds = time.monotonic() - started
    result = {'round': index, 'streams': rows, 'seconds': seconds,
              'completed': seconds > 0 and all(row['completed'] for row in rows)}
    if result['completed']:
        # Aggregate bytes / common wall-clock, NEVER sum independent average rates.
        result['download_mbps'] = round(size * parallel * 8 / seconds / 1e6, 2)
    return result


def upload_round(base, size, index):
    result = {'round': index, 'expected_bytes': size, 'completed': False}
    try:
        p = subprocess.run(base + ['--max-filesize', '65536', '--data-binary', '@-',
                                  '-o', '/dev/null', '-w',
                                  '%{http_code}\t%{size_upload}\t%{time_total}\t%{speed_upload}',
                                  'https://speed.cloudflare.com/__up'], input=b'\0' * size,
                           capture_output=True, timeout=35)
        code, count, seconds, speed = p.stdout.decode().strip().split('\t')
        count, seconds, speed = float(count), float(seconds), float(speed)
        if not all(math.isfinite(x) for x in (count, seconds, speed)) or count < 0 or not count.is_integer():
            raise ValueError('Invalid upload metrics')
        result.update(curl_exit=p.returncode, http_status=int(code), uploaded_bytes=int(count), seconds=seconds)
        if (p.returncode == 0 and int(code) == 200 and count == size and seconds > 0 and speed > 0
                and all(math.isfinite(x) for x in (count, seconds, speed))):
            result.update(completed=True, upload_mbps=round(speed * 8 / 1e6, 2))
        else:
            result['error'] = 'Upload incomplete or HTTP/timing validation failed'
    except (subprocess.TimeoutExpired, ValueError, UnicodeError, OverflowError):
        result['error'] = 'Upload timeout or invalid metrics'
    return result


def utilization(summary, target):
    median = summary['median_mbps']
    return {'reference_mbps': target,
            'median_percent': round(median / target * 100, 1) if median is not None else None,
            'note': 'Application payload rate, not wire rate; short bursts do not prove sustained capacity'}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--proxy', default='http://127.0.0.1:7897')
    ap.add_argument('--stun', action='store_true', help='Send direct OS UDP; TUN state determines whether it is intercepted')
    ap.add_argument('--download-mib', type=int, choices=range(1, 9), default=0,
                    help='Optional 1–8 MiB per stream; larger public-endpoint requests can return HTTP 403')
    ap.add_argument('--upload-mib', type=int, choices=range(1, 9), default=0,
                    help='Optional 1–8 MiB upload each round; not a livestream certification')
    ap.add_argument('--parallel', type=int, choices=range(1, 5), default=1,
                    help='1–4 simultaneous downloads; report aggregate wall-clock throughput')
    ap.add_argument('--target-mbps', type=int, default=30,
                    help='Reference capacity for utilization only, does not configure the proxy (default: 30)')
    ap.add_argument('--rounds', type=int, choices=range(1, 11), default=1,
                    help='Repeat on the same running proxy, no restart/reselection (default: 1)')
    ap.add_argument('--interval', type=int, choices=range(0, 61), default=5,
                    help='Seconds between download rounds (default: 5)')
    ap.add_argument('--expect-exit-ip', type=ipaddress.IPv4Address,
                    help='Abort before downloads if the HTTP trace exit is not this new server IPv4')
    a = ap.parse_args(argv)
    if not 1 <= a.target_mbps <= 100000:
        ap.error('--target-mbps must be between 1 and 100000')
    p = urlsplit(a.proxy)
    if (p.scheme != 'http' or p.hostname not in ('127.0.0.1', 'localhost') or p.username or p.password
            or p.path not in ('', '/') or p.query or p.fragment):
        ap.error('Use an unauthenticated local HTTP proxy URL')
    if not (a.download_mib or a.upload_mib) and a.rounds != 1:
        ap.error('--rounds requires --download-mib or --upload-mib')
    if not a.download_mib and a.parallel != 1:
        ap.error('--parallel requires --download-mib')
    budget = (a.download_mib * a.parallel + a.upload_mib) * a.rounds
    if budget > 256:
        ap.error('Requested payload exceeds 256 MiB; reduce size, rounds or parallelism')
    # -q must be first: ignore ~/.curlrc, including hidden retries/redirects/proxy overrides.
    base = ['curl', '-q', '--proxy', a.proxy, '--noproxy', '', '--fail', '--silent', '--show-error',
            '--proto', '=https', '--retry', '0', '--connect-timeout', '10', '--max-time', '30']
    trace = subprocess.run(base + ['https://www.cloudflare.com/cdn-cgi/trace'], capture_output=True, text=True, timeout=35)
    if trace.returncode:
        raise RuntimeError('HTTP proxy trace failed')
    result = {'http_proxy_exit': dict(x.split('=', 1) for x in trace.stdout.splitlines() if '=' in x).get('ip'),
              'tun_state_changed': False, 'ipv6_and_browser_webrtc_tested': False}
    try:
        ipaddress.ip_address(result['http_proxy_exit'])
    except ValueError:
        raise RuntimeError('HTTP proxy trace did not return a valid exit IP')
    if a.expect_exit_ip:
        result['expected_exit_ip'] = str(a.expect_exit_ip)
        result['exit_ip_matches'] = result['http_proxy_exit'] == str(a.expect_exit_ip)
        if not result['exit_ip_matches']:
            result['error'] = 'Unexpected HTTP exit; downloads and STUN skipped'
            print(json.dumps(result, indent=2))
            return 1
    passed = True
    if budget:
        result['requested_payload_budget_mib'] = budget
        result['budget_scope'] = 'requested bodies only; excludes traces, STUN, headers and retransmissions'
    if a.stun:
        result['udp_stun'] = {}
        for host, port in [('stun.l.google.com', 19302), ('stun.cloudflare.com', 3478)]:
            try:
                result['udp_stun'][host] = stun(host, port)
            except (OSError, ValueError) as e:
                result['udp_stun'][host] = 'failed: ' + type(e).__name__
                passed = False
    if a.download_mib:
        size = a.download_mib * 1024 * 1024
        result['download_budget_mib'] = a.download_mib * a.rounds * a.parallel
        result['parallel_downloads'] = a.parallel
        result['download_rounds'] = []
        for index in range(1, a.rounds + 1):
            if index > 1:
                time.sleep(a.interval)
            row = (download_round(base, size, index) if a.parallel == 1 else
                   download_batch(base, size, index, a.parallel))
            result['download_rounds'].append(row)
            print(f'Download round {index}/{a.rounds}: ' + ('complete' if row['completed'] else 'FAILED'),
                  file=sys.stderr, flush=True)
        summary = summarize_downloads(result['download_rounds'])
        result['download_summary'] = summary
        result['download_utilization'] = utilization(summary, a.target_mbps)
        passed = passed and summary['all_completed']
        if a.rounds == 1:
            result['download_mbps'] = result['download_rounds'][0].get('download_mbps', 'failed')
    if a.upload_mib:
        rows = []
        for index in range(1, a.rounds + 1):
            if index > 1:
                time.sleep(a.interval)
            row = upload_round(base, a.upload_mib * 1048576, index)
            rows.append(row)
            print(f'Upload round {index}/{a.rounds}: ' + ('complete' if row['completed'] else 'FAILED'),
                  file=sys.stderr, flush=True)
        summary = summarize_downloads([dict(row, download_mbps=row.get('upload_mbps')) for row in rows])
        summary['statistics_scope'] = 'complete uploads only; not a livestream stability test'
        result.update(upload_rounds=rows, upload_summary=summary,
                      upload_utilization=utilization(summary, a.target_mbps))
        passed = passed and summary['all_completed']
    print(json.dumps(result, indent=2))
    return 0 if passed else 1


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (Exception, KeyboardInterrupt) as exc:
        print('Probe failed: ' + str(exc), file=sys.stderr)
        sys.exit(1)
