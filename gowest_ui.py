#!/usr/bin/env python3
"""gowest local web UI: a page on YOUR computer that manages servers over SSH.

Nothing new runs on, or is opened on, any server: every button calls the same
deploy.sh commands as remote.sh, over your own SSH keys. This process listens
on 127.0.0.1 only and every API call must carry a random per-run token, so
other web pages and other machines cannot drive it.

    python3 gowest_ui.py            # or ./remote.sh ui
"""
import argparse
import hmac
import io
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import shlex
import subprocess
import sys
import tarfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit
import webbrowser

KIT = Path(__file__).resolve().parent
UI = KIT / 'ui'
HOME = Path(os.environ.get('GOWEST_HOME', Path.home() / '.gowest'))
SERVERS = HOME / 'servers.json'
REMOTE_DIR = 'gowest'
# What the server runs; the rest of the repository stays on this computer.
SKIP = {'.git', 'tests', 'docs', 'dist', 'clients', 'native', 'ui', '__pycache__', '.DS_Store'}
SSH = ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10', '-o', 'StrictHostKeyChecking=accept-new',
       '-o', 'ServerAliveInterval=15']
TARGET = re.compile(r'[A-Za-z0-9._-]+(@[A-Za-z0-9._:-]+)?')
NAME = re.compile(r'[a-z][a-z0-9-]{0,15}')
VALUE = re.compile(r'[A-Za-z0-9./:-]{1,32}')
ANSI = re.compile(r'\x1b\[[0-9;?]*[A-Za-z]')
TYPES = {'.html': 'text/html; charset=utf-8', '.css': 'text/css; charset=utf-8',
         '.js': 'text/javascript; charset=utf-8', '.svg': 'image/svg+xml'}


class UserError(Exception):
    """A message meant for the person at the browser."""


# ---------- saved servers ----------

def load_servers():
    try:
        return json.loads(SERVERS.read_text())
    except (OSError, ValueError):
        return []


def save_servers(servers):
    HOME.mkdir(mode=0o700, exist_ok=True)
    tmp = SERVERS.with_suffix('.new')
    tmp.write_text(json.dumps(servers, ensure_ascii=False, indent=1))
    os.chmod(tmp, 0o600)
    os.replace(tmp, SERVERS)


def find(server_id):
    for server in load_servers():
        if server['id'] == server_id:
            return server
    raise UserError('找不到这台服务器')


# ---------- SSH ----------

def explain(stderr):
    """Turn SSH/sudo failures into one actionable sentence."""
    text = stderr.strip()
    if 'Permission denied' in text or 'Host key verification failed' in text:
        return '无法用 SSH 密钥登录。请先在终端里能用 ssh 免密码登录这台服务器（ssh-copy-id），再回来刷新。'
    if 'a password is required' in text or 'sudo: a terminal is required' in text:
        return '这个用户执行 sudo 需要密码。请改用 root 登录，或为该用户配置免密码 sudo。'
    if 'Could not resolve' in text or 'timed out' in text or 'Connection refused' in text or 'No route' in text:
        return '连不上服务器（SSH）。请检查 IP / 别名、网络和安全组里的 SSH 端口。'
    if '/opt/la-vpn/' in text and 'No such file' in text:
        return '这台服务器还没有安装 gowest。'
    lines = [line for line in text.splitlines() if line.strip()]
    for line in reversed(lines):
        for prefix in ('ERROR: ', '操作停止：'):
            if line.startswith(prefix):
                return line[len(prefix):]
    return lines[-1] if lines else '命令失败'


def installed_command(args):
    quoted = ' '.join(shlex.quote(a) for a in args)
    return ('if [ -x /opt/la-vpn/gowest ]; then sudo -n /opt/la-vpn/gowest ' + quoted +
            '; else sudo -n bash /opt/la-vpn/deploy.sh ' + quoted + '; fi')


def remote(target, command, timeout=120, data=None):
    try:
        p = subprocess.run(SSH + [target, command], input=data, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise UserError('服务器响应超时') from None
    except FileNotFoundError:
        raise UserError('这台电脑没有 ssh 命令；Windows 请在"可选功能"里启用 OpenSSH 客户端') from None
    out, err = p.stdout.decode(errors='replace'), p.stderr.decode(errors='replace')
    if p.returncode:
        raise UserError(explain(err or out))
    return out


def remote_json(target, args, timeout=120):
    out = remote(target, installed_command(args), timeout)
    try:
        return json.loads(out.strip().splitlines()[-1])
    except (ValueError, IndexError):
        raise UserError('服务器返回的内容无法解析；请先升级服务器上的 gowest') from None


def kit_archive():
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode='w:gz') as tar:
        for path in sorted(KIT.iterdir()):
            if path.name in SKIP or path.name.startswith('generated') or path.name.startswith('.'):
                continue
            tar.add(path, arcname=path.name,
                    filter=lambda info: None if '__pycache__' in info.name or info.name.endswith('.DS_Store') else info)
    return buffer.getvalue()


# ---------- background jobs (install / upgrade) ----------

JOBS = {}


class Job:
    def __init__(self, title):
        self.id = secrets.token_hex(6)
        self.title, self.lines, self.done, self.ok = title, [], False, None
        JOBS[self.id] = self

    def say(self, text):
        for line in ANSI.sub('', text).replace('\r', '').splitlines():
            self.lines.append(line)

    def stream(self, target, command):
        p = subprocess.Popen(SSH + [target, command], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        for raw in p.stdout:
            self.say(raw.decode(errors='replace'))
        if p.wait():
            raise UserError('服务器上的命令没有成功，详情见上面的输出')

    def run(self, steps):
        def worker():
            try:
                for step in steps:
                    step(self)
                self.ok = True
            except UserError as error:
                self.say('✗ ' + str(error))
                self.ok = False
            except Exception as error:  # Reported in the page, never swallowed silently.
                self.say('✗ ' + type(error).__name__ + ': ' + str(error))
                self.ok = False
            finally:
                self.done = True
        threading.Thread(target=worker, daemon=True).start()
        return self


def upload(job, target):
    job.say('→ 上传 gowest 到服务器 ~/' + REMOTE_DIR)
    remote(target, f"set -e; rm -rf {REMOTE_DIR}.new; mkdir -p {REMOTE_DIR}.new; "
                   f"tar -xzf - -C {REMOTE_DIR}.new 2>/dev/null; rm -rf {REMOTE_DIR}; mv {REMOTE_DIR}.new {REMOTE_DIR}",
           timeout=300, data=kit_archive())
    job.say('✓ 上传完成')


# ---------- API ----------

def kit_version():
    match = re.search(r"KIT_VERSION = '([^']+)'", (KIT / 'render.py').read_text())
    return match.group(1) if match else ''


def server_view(server):
    return {k: server[k] for k in ('id', 'name', 'target', 'ip')}


def api_add_server(body):
    name = str(body.get('name', '')).strip()[:40] or str(body.get('target', ''))
    target = str(body.get('target', '')).strip()
    if not TARGET.fullmatch(target):
        raise UserError('SSH 目标格式应为 root@203.0.113.7，或 ~/.ssh/config 里的别名')
    ip = str(body.get('ip', '')).strip() or target.split('@')[-1]
    try:
        ip = str(ipaddress.IPv4Address(ip))
    except ValueError:
        raise UserError('请填写服务器的公网 IPv4（SSH 目标是别名时需要单独填写）') from None
    remote(target, 'true', timeout=30)
    servers = load_servers()
    if any(s['target'] == target for s in servers):
        raise UserError('这台服务器已经在列表里了')
    server = {'id': secrets.token_hex(4), 'name': name, 'target': target, 'ip': ip}
    save_servers(servers + [server])
    return server_view(server)


def api_status(server):
    probe = 'if sudo -n test -f /var/lib/la-vpn-deploy/state.json; then ' + installed_command(['status', '--json']) + \
            '; else echo \'{"installed": false}\'; fi'
    out = remote(server['target'], probe)
    try:
        data = json.loads(out.strip().splitlines()[-1])
    except (ValueError, IndexError):
        raise UserError('服务器上的 gowest 版本较旧，不支持网页管理；请先点"升级"') from None
    data.setdefault('installed', True)
    return data


def clean(body, key, pattern=VALUE):
    value = str(body.get(key, '')).strip()
    if value and not pattern.fullmatch(value):
        raise UserError('输入格式不正确：' + key)
    return value


def api_users(server, body):
    action = body.get('action')
    if action not in ('add', 'remove', 'reset', 'set'):
        raise UserError('未知操作')
    name = clean(body, 'name', NAME)
    if not name:
        raise UserError('名字只能用小写字母、数字和 -，以字母开头，最长 16 个字符')
    args = ['users', action, name]
    # An empty field clears the setting on the server.
    for key, empty in (('cap', 'none'), ('quota', '0'), ('expire', 'none')):
        if key in body and body[key] is not None:
            args += ['--' + key, clean(body, key) or empty]
    out = remote(server['target'], installed_command(args + ['--confirm-server', server['ip']]), timeout=180)
    return {'output': ANSI.sub('', out)}


def api_bandwidth(server, body):
    value = clean(body, 'bandwidth')
    out = remote(server['target'], installed_command(['bandwidth', '--set', value, '--confirm-server', server['ip']]))
    return {'output': out}


def api_traffic(server, body):
    args = ['traffic', '--quota', clean(body, 'quota') or '0']
    if body.get('on_exceed') in ('warn', 'stop'):
        args += ['--on-exceed', body['on_exceed']]
    if body.get('metering') in ('both', 'outbound'):
        args += ['--metering', body['metering']]
    out = remote(server['target'], installed_command(args + ['--confirm-server', server['ip']]))
    return {'output': out}


def api_rollback(server, body):
    # Typing the IP is the confirmation, as on the command line.
    if str(body.get('confirm', '')).strip() != server['ip']:
        raise UserError('请输入这台服务器的 IP 以确认卸载')
    out = remote(server['target'], installed_command(['rollback']), timeout=300)
    return {'output': ANSI.sub('', out)}


def api_check(server):
    sys.path.insert(0, str(KIT))
    import localcheck
    info = remote_json(server['target'], ['endpoints'])
    return {'rows': localcheck.check(info)}


def api_install(server, body):
    bandwidth = clean(body, 'bandwidth')
    if not bandwidth:
        raise UserError('请填写带宽，例如 100Mbps')
    args = ['quick', bandwidth]
    users = clean(body, 'users', re.compile(r'[a-z0-9,:-]{1,200}'))
    if users and users != '1':
        args += ['--users', users]
    if clean(body, 'quota'):
        args += ['--monthly-quota', clean(body, 'quota'), '--on-exceed',
                 body.get('on_exceed') if body.get('on_exceed') in ('warn', 'stop') else 'warn']
    if clean(body, 'prefix', re.compile(r'[A-Za-z][A-Za-z0-9-]{0,24}')):
        args += ['--node-prefix', clean(body, 'prefix')]
    command = f'cd {REMOTE_DIR} && sudo -n bash deploy.sh ' + ' '.join(shlex.quote(a) for a in args)
    target = server['target']
    return Job('安装 gowest').run([lambda job: upload(job, target),
                                   lambda job: job.stream(target, command)])


def api_upgrade(server):
    command = f'cd {REMOTE_DIR} && sudo -n bash deploy.sh upgrade --confirm-server ' + shlex.quote(server['ip'])
    target = server['target']
    return Job('升级 gowest').run([lambda job: upload(job, target),
                                   lambda job: job.stream(target, command)])


class Handler(BaseHTTPRequestHandler):
    token = ''
    port = 0

    def log_message(self, *args):
        pass  # Requests carry subscription URLs; never echo them to the terminal.

    def send(self, code, body, kind='application/json; charset=utf-8'):
        data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header('Content-Type', kind)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        if kind.startswith('text/html'):
            self.send_header('Content-Security-Policy', "default-src 'self'; img-src 'self' data:; "
                             "style-src 'self' 'unsafe-inline'; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(data)

    def trusted(self):
        # Host pinning defeats DNS rebinding; the token defeats other local pages.
        host = self.headers.get('Host', '')
        if host not in (f'127.0.0.1:{self.port}', f'localhost:{self.port}'):
            return False
        origin = self.headers.get('Origin')
        if origin and origin not in (f'http://127.0.0.1:{self.port}', f'http://localhost:{self.port}'):
            return False
        return True

    def do_GET(self):
        self.route('GET')

    def do_POST(self):
        self.route('POST')

    def do_DELETE(self):
        self.route('DELETE')

    def route(self, method):
        if not self.trusted():
            return self.send(403, {'error': 'forbidden'})
        url = urlsplit(self.path)
        parts = [p for p in url.path.split('/') if p]
        if method == 'GET' and (not parts or parts[0] in ('app.js', 'app.css', 'vendor')):
            return self.static(parts or ['index.html'])
        if not hmac.compare_digest(self.headers.get('X-Gowest-Token', ''), self.token):
            return self.send(401, {'error': '页面已过期，请回到终端重新打开链接'})
        try:
            length = int(self.headers.get('Content-Length') or 0)
            body = json.loads(self.rfile.read(length) or b'{}') if length else {}
            self.send(200, self.api(method, parts[1:] if parts[:1] == ['api'] else [], body,
                                    parse_qs(url.query)))
        except UserError as error:
            self.send(400, {'error': str(error)})
        except Exception as error:
            self.send(500, {'error': type(error).__name__ + ': ' + str(error)})

    def static(self, parts):
        path = (UI / '/'.join(parts)).resolve()
        if UI.resolve() not in path.parents or not path.is_file():
            return self.send(404, {'error': 'not found'})
        self.send(200, path.read_bytes(), TYPES.get(path.suffix, 'application/octet-stream'))

    def api(self, method, parts, body, query):
        if DEMO:
            return demo(method, parts, body)
        if parts == ['servers'] and method == 'GET':
            return {'servers': [server_view(s) for s in load_servers()], 'kit_version': kit_version()}
        if parts == ['servers'] and method == 'POST':
            return api_add_server(body)
        if parts[:1] == ['jobs'] and len(parts) == 2:
            job = JOBS.get(parts[1])
            if not job:
                raise UserError('任务不存在')
            start = int(query.get('from', ['0'])[0])
            return {'title': job.title, 'lines': job.lines[start:], 'next': len(job.lines),
                    'done': job.done, 'ok': job.ok}
        if parts[:1] != ['servers'] or len(parts) < 2:
            raise UserError('未知请求')
        if method == 'DELETE' and len(parts) == 2:
            save_servers([s for s in load_servers() if s['id'] != parts[1]])
            return {'ok': True}
        server = find(parts[1])
        action = parts[2] if len(parts) > 2 else ''
        if method == 'GET' and action == 'status':
            return api_status(server)
        if method == 'GET' and action == 'urls':
            user = query.get('user', [''])[0]
            if user and not NAME.fullmatch(user):
                raise UserError('名字格式不正确')
            return remote_json(server['target'], ['urls', '--json', '--user', user])
        if method == 'POST':
            simple = {'unlock': lambda: remote_json(server['target'], ['unlock', '--json'], timeout=180),
                      'check': lambda: api_check(server),
                      'users': lambda: api_users(server, body),
                      'bandwidth': lambda: api_bandwidth(server, body),
                      'traffic': lambda: api_traffic(server, body),
                      'rollback': lambda: api_rollback(server, body),
                      'install': lambda: {'job': api_install(server, body).id},
                      'upgrade': lambda: {'job': api_upgrade(server).id}}
            if action in simple:
                return simple[action]()
        raise UserError('未知请求')


DEMO = False


def demo(method, parts, body):
    """Sample data for screenshots and trying the page; nothing is contacted."""
    if parts == ['servers'] and method == 'GET':
        return {'kit_version': kit_version(), 'servers': [
            {'id': 'la', 'name': '洛杉矶', 'target': 'root@203.0.113.7', 'ip': '203.0.113.7'},
            {'id': 'tk', 'name': '东京', 'target': 'root@198.51.100.24', 'ip': '198.51.100.24'},
            {'id': 'new', 'name': '新加坡（新机）', 'target': 'root@192.0.2.80', 'ip': '192.0.2.80'}]}
    if parts[:1] == ['jobs']:
        lines = ['→ 上传 gowest 到服务器 ~/gowest', '✓ 上传完成'] + [
            f'2026-10-27T10:0{i}:00 [{"完成" if j else "开始"}] {i}/8 ' + name
            for i, name in enumerate(['只读预检', '回滚保护', '系统依赖', 'sing-box 校验', '密钥与四端配置',
                                      '网络优化与服务', '监听与订阅校验', '统计与保存'], 1) for j in (0, 1)]
        return {'title': '安装 gowest', 'lines': lines, 'next': len(lines), 'done': True, 'ok': True}
    GB = 10**9
    if parts[2:] == ['status']:
        if parts[1] == 'new':
            return {'installed': False}
        users = [('alice', 20001, 186.4, 0, None, '', ''), ('bob', 20002, 200.0, 200, 20, '', 'quota'),
                 ('carol', 20003, 42.7, 300, None, '2026-12-31', ''), ('dave', 20004, 8.1, 100, None, '2027-03-31', '')]
        rows = [{'name': n, 'port': p, 'used_bytes': int(u * GB), 'quota_bytes': q * GB, 'cap_mbps': c,
                 'expire_date': e, 'paused': z} for n, p, u, q, c, e, z in users]
        active = {n: 'active' for n in ('la-vpn.service', 'la-vpn-subscription.service', 'la-vpn-fq.service',
                                        'la-vpn-subinfo.timer')}
        return {'installed': True, 'status': 'installed', 'server_ip': '203.0.113.7', 'kit_version': kit_version(),
                'sing_box': '1.14.3', 'services': active, 'node_prefix': 'LA', 'bandwidth_mbps': 300,
                'subscription_scheme': 'http', 'fair_share': parts[1] == 'la', 'fair_share_result': 'applied',
                'warnings': [], 'single': parts[1] != 'la', 'users': rows if parts[1] == 'la' else [],
                'traffic': {'month': '2026-10', 'used_bytes': int(873.6 * GB), 'quota_bytes': 2000 * GB,
                            'metering': 'both', 'on_exceed': 'stop', 'paused_month': None}}
    if parts[2:] == ['urls']:
        base = 'http://203.0.113.7/sub/3f9c2d7e1b4a6058c9e2f1d0a7b3c4e5d6f7a8b9c0d1e2f3'
        return {'base': base, 'files': {'normal.yaml': base, 'strict.yaml': base + '-us', 'nodes.txt': base + '-nodes',
                                        'shadowrocket.conf': base + '-shadowrocket.conf',
                                        'shadowrocket-us.conf': base + '-us-shadowrocket.conf'},
                'clash': {'normal': 'clash://install-config?name=gowest-LA&url=' + base, 'strict': 'clash://install-config?url=' + base + '-us'},
                'clashmeta': {'normal': 'clashmeta://install-config?name=gowest-LA&url=' + base, 'strict': 'clashmeta://install-config?url=' + base + '-us'}}
    if parts[2:] == ['check']:
        return {'rows': [{'name': 'TCP 20001', 'ok': True, 'text': '代理端口（VLESS）可连接'},
                         {'name': 'TCP 20002', 'ok': True, 'text': '代理端口（VLESS）可连接'},
                         {'name': 'TCP 80', 'ok': True, 'text': '订阅端口可连接'},
                         {'name': '订阅下载', 'ok': True, 'text': '成功（HTTP 200）'},
                         {'name': 'UDP 20001、20002', 'ok': None, 'text': 'Hy2 无法从外部探测；请确认安全组也放行了 UDP'}]}
    if parts[2:] == ['unlock']:
        return {'ip': {'ip': '203.0.113.7', 'country': 'US', 'city': 'Los Angeles', 'org': 'AS-EXAMPLE Hosting'},
                'checks': [{'name': 'Claude', 'level': 'ok', 'text': '可用'},
                           {'name': 'ChatGPT', 'level': 'warn', 'text': '网页可用，App 会提示 VPN（IP 被识别为代理）'},
                           {'name': 'TikTok', 'level': 'ok', 'text': '可用，识别地区 US'},
                           {'name': 'YouTube', 'level': 'ok', 'text': '可用，地区 US'}]}
    if parts[2:] in (['install'], ['upgrade']):
        return {'job': 'demo'}
    return {'ok': True}


def main(argv=None):
    global DEMO
    parser = argparse.ArgumentParser(description='gowest 本地管理页面（只在这台电脑上运行）')
    parser.add_argument('--port', type=int, default=0, help='默认随机端口')
    parser.add_argument('--no-browser', action='store_true')
    parser.add_argument('--demo', action='store_true', help='用示例数据演示页面，不连接任何服务器')
    args = parser.parse_args(argv)
    DEMO = args.demo
    Handler.token = secrets.token_urlsafe(24)
    httpd = ThreadingHTTPServer(('127.0.0.1', args.port), Handler)
    Handler.port = httpd.server_address[1]
    url = f'http://127.0.0.1:{Handler.port}/#{Handler.token}'
    print('gowest 管理页面：' + url)
    print('只在这台电脑上可以打开；关闭这个终端窗口（或按 Ctrl+C）就会停止。')
    if not args.no_browser:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print('\n已停止。')


if __name__ == '__main__':
    main()
