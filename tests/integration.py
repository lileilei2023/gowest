#!/usr/bin/env python3
"""Loopback-only protocol test. No TUN, system proxy, production keys or Internet."""
import argparse
import contextlib
import copy
import http.server
import http.client
import json
from pathlib import Path
import socket
import secrets
import ssl
import struct
import subprocess
import sys
import tempfile
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import render


def port():
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


class Handler(http.server.BaseHTTPRequestHandler):
    def do_HEAD(self):
        self.send_response(204 if self.path == '/health' else 200)
        self.end_headers()

    def do_GET(self):
        self.send_response(204 if self.path == '/health' else 200)
        self.end_headers()
        if self.path != '/health': self.wfile.write(b'la-vpn-isolated-ok')

    def log_message(self, *args):
        pass


def wait_port(number, process):
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        if process.poll() is not None: raise RuntimeError('Isolated process exited; inspect private test log')
        try:
            with socket.create_connection(('127.0.0.1', number), timeout=.1): return
        except OSError: time.sleep(.05)
    raise RuntimeError('Isolated listener failed to start')


@contextlib.contextmanager
def child(args, log):
    with log.open('wb') as output:
        p = subprocess.Popen(args, stdout=output, stderr=subprocess.STDOUT)
        try: yield p
        finally:
            if p.poll() is None:
                p.terminate()
                try: p.wait(timeout=4)
                except subprocess.TimeoutExpired: p.kill(); p.wait(timeout=4)


def read_exact(sock, count):
    out = b''
    while len(out) < count:
        more = sock.recv(count - len(out))
        if not more: raise RuntimeError('SOCKS connection closed')
        out += more
    return out


def udp_roundtrip(proxy, destination):
    with socket.create_connection(('127.0.0.1', proxy), timeout=5) as control:
        control.sendall(b'\x05\x01\x00')
        assert read_exact(control, 2) == b'\x05\x00'
        control.sendall(b'\x05\x03\x00\x01' + b'\x00' * 6)
        prefix = read_exact(control, 4)
        assert prefix[:2] == b'\x05\x00'
        if prefix[3] == 1: host = socket.inet_ntoa(read_exact(control, 4))
        elif prefix[3] == 4: host = socket.inet_ntop(socket.AF_INET6, read_exact(control, 16))
        else: raise RuntimeError('Unexpected SOCKS reply address family')
        relay = struct.unpack('!H', read_exact(control, 2))[0]
        if host in ('0.0.0.0', '::'): host = '127.0.0.1'
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp:
            udp.settimeout(5)
            packet = b'\x00\x00\x00\x01' + socket.inet_aton('127.0.0.1') + struct.pack('!H', destination) + b'isolated-udp'
            udp.sendto(packet, (host, relay))
            data, _ = udp.recvfrom(2048)
            assert data.endswith(b'isolated-udp'), 'UDP roundtrip mismatch'


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--sing-box', required=True)
    ap.add_argument('--mihomo', required=True)
    ap.add_argument('--work', required=True, help='Must be a new directory; private logs are retained')
    a = ap.parse_args()
    root = Path(a.work).resolve()
    root.mkdir(mode=0o700, exist_ok=False)
    s = json.loads((Path(render.__file__).parent / 'settings.example.json').read_text())
    s['server_ip'] = '127.0.0.1'; s['reality_server_name'] = 'bing.com'
    out = render.render(s, root / 'generated')
    server = json.loads((out / 'server.json').read_text())
    import yaml
    client = yaml.safe_load((out / 'normal.yaml').read_text())
    tcp_proxy_port = port()
    tls = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    tls.daemon_threads = True
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_3
    context.load_cert_chain(out / 'hy2.crt', out / 'hy2.key')
    tls.socket = context.wrap_socket(tls.socket, server_side=True)
    web = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    web.daemon_threads = True
    echo = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    echo.bind(('127.0.0.1', 0)); echo.settimeout(.2)
    stop = threading.Event()
    def echo_loop():
        while not stop.is_set():
            try:
                data, address = echo.recvfrom(2048); echo.sendto(data, address)
            except socket.timeout: pass
            except OSError: break
    threads = [threading.Thread(target=f, daemon=True) for f in (tls.serve_forever, web.serve_forever, echo_loop)]
    for t in threads: t.start()
    for inbound in server['inbounds']:
        inbound['listen'] = '127.0.0.1'; inbound['listen_port'] = tcp_proxy_port
    server['inbounds'][0]['tls']['reality']['handshake'] = {'server': '127.0.0.1', 'server_port': tls.server_port}
    server['inbounds'][1]['tls']['certificate_path'] = str(out / 'hy2.crt')
    server['inbounds'][1]['tls']['key_path'] = str(out / 'hy2.key')
    (root / 'server.json').write_text(json.dumps(server))
    binary = str(Path(a.sing_box).resolve()); mihomo = str(Path(a.mihomo).resolve())
    results = {}
    try:
        with child([binary, 'run', '-c', str(root / 'server.json')], root / 'server.log') as backend:
            wait_port(tcp_proxy_port, backend)
            for case in ('hy2', 'vless', 'fallback', 'both-dead', 'wrong-pin'):
                proxies = copy.deepcopy(client['proxies'])
                for p in proxies: p['port'] = tcp_proxy_port
                for p in proxies:
                    if (case in ('fallback', 'both-dead') and p['type'] == 'hysteria2') or case == 'both-dead':
                        p['port'] = port()
                    if case == 'wrong-pin' and p['type'] == 'hysteria2': p['fingerprint'] = '00' * 32
                local = port()
                control_port = port()
                control_secret = secrets.token_hex(24)
                target = 'LA-Hy2' if case in ('hy2', 'wrong-pin') else 'LA-VLESS'
                cfg = {'mixed-port': local, 'bind-address': '127.0.0.1', 'allow-lan': False,
                       'mode': 'rule', 'log-level': 'warning', 'proxies': proxies, 'tun': {'enable': False},
                       'dns': {'enable': False}, 'rules': ['MATCH,' + target]}
                cfg['external-controller'] = f'127.0.0.1:{control_port}'
                cfg['secret'] = control_secret
                if case in ('fallback', 'both-dead'):
                    cfg['proxy-groups'] = [{'name': 'auto', 'type': 'fallback', 'proxies': ['LA-Hy2', 'LA-VLESS'],
                                           'url': f'http://127.0.0.1:{web.server_port}/health',
                                           'interval': 1, 'timeout': 1000, 'lazy': False, 'expected-status': 204}]
                    cfg['rules'] = ['MATCH,auto']
                cdir = root / case; cdir.mkdir()
                (cdir / 'config.json').write_text(json.dumps(cfg))
                with child([mihomo, '-d', str(cdir), '-f', str(cdir / 'config.json')], cdir / 'core.log') as core:
                    wait_port(local, core)
                    # Wait for the disposable core's health state, not an assumed failover latency.
                    if case == 'fallback':
                        deadline = time.monotonic() + 25
                        while True:
                            connection = http.client.HTTPConnection('127.0.0.1', control_port, timeout=2)
                            connection.request('GET', '/proxies/auto', headers={'Authorization': 'Bearer ' + control_secret})
                            state = json.loads(connection.getresponse().read())
                            connection.close()
                            if state.get('now') == 'LA-VLESS': break
                            if time.monotonic() > deadline:
                                raise RuntimeError('Isolated fallback did not select healthy VLESS; last selection: ' + str(state.get('now')))
                            time.sleep(.2)
                    response = subprocess.run(['curl', '--proxy', f'http://127.0.0.1:{local}', '--noproxy', '',
                                               '--fail', '--silent', '--max-time', '6', f'http://127.0.0.1:{web.server_port}/'],
                                              capture_output=True, timeout=8)
                    if case in ('both-dead', 'wrong-pin'):
                        assert response.returncode != 0, case + ' unexpectedly reached direct target'
                        results[case] = 'request rejected as expected'
                    else:
                        assert response.returncode == 0 and response.stdout == b'la-vpn-isolated-ok', case + ' HTTP failed'
                        udp_roundtrip(local, echo.getsockname()[1])
                        results[case] = 'HTTP and UDP passed'
    finally:
        stop.set(); echo.close(); tls.shutdown(); web.shutdown(); tls.server_close(); web.server_close()
    print(json.dumps({'loopback_only': True, 'results': results, 'production_config_changed': False}, indent=2))


if __name__ == '__main__':
    main()
