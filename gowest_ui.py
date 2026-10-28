#!/usr/bin/env python3
"""gowest local web UI: a page on YOUR computer that manages servers over SSH.

Nothing new runs on, or is opened on, any server: every button calls the same
deploy.sh commands as remote.sh, over your own SSH keys. This process listens
on 127.0.0.1 only and every API call must carry a random per-run token, so
other web pages and other machines cannot drive it.

    python3 gowest_ui.py            # or ./remote.sh ui
"""
import argparse
import errno
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
ASSETS = None
UI_VERSION = None

# Executed via stdin, not installed on the server. Only reads metadata and
# runs status / is-active; never imports server code or returns credentials.
INSPECT_SCRIPT = r'''
import json
import os
from pathlib import Path
import re
import shutil
import subprocess

def inspect():
    state_path = Path('/var/lib/la-vpn-deploy/state.json')
    if not os.path.lexists(state_path):
        paths = ('/etc/sing-box', '/usr/bin/sing-box', '/usr/local/bin/sing-box',
                 '/etc/nginx', '/opt/la-vpn', '/etc/la-vpn', '/var/lib/la-vpn-deploy',
                 '/var/lib/la-vpn', '/etc/sysctl.d/90-la-vpn.conf',
                 '/etc/systemd/system/la-vpn.service', '/etc/systemd/system/nginx.service')
        conflicts = [p for p in paths if os.path.lexists(Path(p))]
        for name in ('sing-box', 'nginx', 'xray', 'v2ray', 'mihomo', 'clash', 'hysteria'):
            if shutil.which(name):
                conflicts.append(name)
        return {'installed': False, 'management': 'unmanaged' if conflicts else 'fresh',
                'conflicts': conflicts, 'can_upgrade': False}
    if state_path.is_symlink():
        raise ValueError('安装记录是符号链接，需要人工检查')
    state = json.loads(state_path.read_text())
    if not isinstance(state, dict) or not isinstance(state.get('files'), dict) or not state.get('server_ip'):
        raise ValueError('安装记录不完整，需要人工检查')
    result = {'installed': True, 'management': 'managed', 'status': state.get('status'),
              'server_ip': state['server_ip'], 'kit_version': state.get('kit_version') or '未知旧版',
              'can_upgrade': False, 'legacy': False}
    source = Path('/opt/la-vpn/render.py')
    if not state.get('kit_version') and source.is_file():
        match = re.search(r"^KIT_VERSION\s*=\s*['\"]([^'\"]+)['\"]", source.read_text(), re.M)
        if match:
            result['kit_version'] = match.group(1)
    binary = Path('/opt/la-vpn/gowest')
    if binary.is_file() and os.access(binary, os.X_OK):
        command = [str(binary)]
    elif Path('/opt/la-vpn/deploy.sh').is_file():
        command = ['bash', '/opt/la-vpn/deploy.sh']
    else:
        result.update(management='incomplete', message='安装记录存在，但管理程序缺失；请先检查备份，不要重新安装。')
        return result
    if state.get('status') != 'installed':
        result.update(management='incomplete', message='安装记录不是已完成状态；需要先检查上次操作，不要直接升级或重装。')
        return result
    status_result = subprocess.run(command + ['status', '--json'], capture_output=True, text=True,
                                   timeout=25, env=dict(os.environ, PYTHONDONTWRITEBYTECODE='1', LC_ALL='C'))
    # --json is deliberately hidden from argparse help in current releases.
    # Only the explicit unknown-argument error identifies an old release.
    legacy = status_result.returncode == 2 and bool(re.search(
        r'(?:unrecognized arguments|unknown option)[^\n]*--json', status_result.stderr))
    if status_result.returncode and not legacy:
        raise ValueError('无法读取已安装程序的状态；请检查服务器上的 gowest')
    if not legacy:
        report = json.loads(status_result.stdout.strip().splitlines()[-1])
        if not isinstance(report, dict) or not isinstance(report.get('services'), dict):
            raise ValueError('状态格式不正确')
        result['report'] = report
    result.update(legacy=legacy, can_upgrade=True)
    if result['legacy']:
        names = ['la-vpn.service', 'la-vpn-subscription.service', 'la-vpn-subinfo.timer']
        services = subprocess.run(['systemctl', 'is-active'] + names, capture_output=True, text=True, timeout=15)
        values = services.stdout.splitlines()
        result['services'] = {name: values[i] if i < len(values) else 'unknown' for i, name in enumerate(names)}
    return result

try:
    print(json.dumps(inspect(), ensure_ascii=False))
except (OSError, ValueError, IndexError, subprocess.TimeoutExpired):
    print(json.dumps({'inspection_error': '无法安全读取服务器安装记录或管理程序，请检查文件权限、磁盘和安装状态；未做任何改动。'}, ensure_ascii=False))
'''


class UserError(Exception):
    """A message meant for the person at the browser."""


def local_read(path):
    """Retry a transient SMB EIO, but never mistake a failed read for no data."""
    for attempt in range(3):
        try:
            return path.read_bytes()
        except FileNotFoundError:
            raise
        except OSError as error:
            if error.errno == errno.EIO and attempt < 2:
                time.sleep(0.05)
                continue
            raise UserError('无法读取本地文件 ' + str(path) + '；请检查磁盘或共享盘连接后重试。') from None


# ---------- saved servers ----------

def load_servers():
    try:
        data = json.loads(local_read(SERVERS))
    except FileNotFoundError:
        return []
    except ValueError:
        raise UserError('服务器列表文件损坏，已保留原文件；请检查 ' + str(SERVERS)) from None
    if not isinstance(data, list) or any(not isinstance(s, dict) or
            not all(isinstance(s.get(k), str) for k in ('id', 'name', 'target', 'ip')) for s in data):
        raise UserError('服务器列表格式不正确，已保留原文件；请检查 ' + str(SERVERS))
    return data


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
    if UI_VERSION is not None:
        return UI_VERSION
    match = re.search(r"KIT_VERSION = '([^']+)'", local_read(KIT / 'render.py').decode())
    return match.group(1) if match else ''


def prepare_ui():
    """Page requests need no shared-drive reads after startup."""
    global ASSETS, UI_VERSION
    assets = {name: local_read(UI / name) for name in
              ('index.html', 'app.js', 'app.css', 'vendor/qrcode.min.js')}
    version = kit_version()
    ASSETS, UI_VERSION = assets, version


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


def inspect_server(server):
    out = remote(server['target'], 'sudo -n python3 -B -', data=INSPECT_SCRIPT.encode())
    try:
        data = json.loads(out.strip().splitlines()[-1])
    except (ValueError, IndexError):
        raise UserError('服务器安装状态无法解析，未做任何改动。') from None
    if not isinstance(data, dict) or data.get('management') not in ('managed', 'unmanaged', 'fresh', 'incomplete'):
        raise UserError('无法安全读取服务器安装记录，请检查权限、磁盘和安装状态；未做任何改动。')
    if data.get('installed') and data.get('server_ip') != server['ip']:
        data.update(can_upgrade=False, message='保存的公网 IP 与安装记录不一致，请核对服务器地址后再升级。')
    return data


def api_status(server):
    info = inspect_server(server)
    if info['management'] != 'managed' or info.get('legacy'):
        return info
    data = info.get('report')
    if data is None:
        data = remote_json(server['target'], ['status', '--json'])
    if not isinstance(data, dict) or not isinstance(data.get('services'), dict):
        raise UserError('服务器状态格式不正确，请检查已安装程序；未做任何改动。')
    data.update(installed=True, management='managed', legacy=False, can_upgrade=info['can_upgrade'])
    if info.get('message'):
        data['message'] = info['message']
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


def api_options(server, body):
    args = ['options']
    for key in ('hopping', 'obfs'):
        if key in body:
            args += ['--' + key, 'on' if body[key] else 'off']
    if 'protocols' in body:
        chosen = [p for p in ('tuic', 'anytls') if p in (body['protocols'] or [])]
        args += ['--protocols', ','.join(chosen) or 'none']
    if len(args) == 1:
        raise UserError('没有要修改的选项')
    out = remote(server['target'], installed_command(args + ['--confirm-server', server['ip']]), timeout=180)
    return {'output': ANSI.sub('', out)}


def api_linecheck(server):
    sys.path.insert(0, str(KIT))
    import linecheck
    return linecheck.check(remote_json(server['target'], ['endpoints']))


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
    if body.get('stable') in (False, 'off', 'false', ''):
        args += ['--no-port-hopping', '--protocols', 'none']
    if clean(body, 'prefix', re.compile(r'[A-Za-z][A-Za-z0-9-]{0,24}')):
        args += ['--node-prefix', clean(body, 'prefix')]
    if inspect_server(server)['management'] != 'fresh':
        raise UserError('服务器已有安装记录或代理/网站，不能重新安装；请先检查或制定迁移方案。')
    command = f'cd {REMOTE_DIR} && sudo -n bash deploy.sh ' + ' '.join(shlex.quote(a) for a in args)
    target = server['target']
    return Job('安装 gowest').run([lambda job: upload(job, target),
                                   lambda job: job.stream(target, command)])


def api_upgrade(server):
    if not inspect_server(server).get('can_upgrade'):
        raise UserError('当前安装状态不允许升级，请刷新状态并检查提示；没有上传或修改服务器文件。')
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
        if ASSETS is not None:
            name = '/'.join(parts)
            if name not in ASSETS:
                return self.send(404, {'error': 'not found'})
            return self.send(200, ASSETS[name], TYPES.get(Path(name).suffix, 'application/octet-stream'))
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
                      'options': lambda: api_options(server, body),
                      'linecheck': lambda: api_linecheck(server),
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
                            'metering': 'both', 'on_exceed': 'stop', 'paused_month': None},
                'options': {'port_hopping': True, 'extra_protocols': ['tuic', 'anytls'], 'hy2_obfs': True},
                'cloud_ports': (['TCP/UDP 20001-20004', 'TCP/UDP 21001-21004', 'UDP 24576-24831', 'TCP 80']
                                if parts[1] == 'la' else ['TCP/UDP 443', 'TCP/UDP 2443', 'UDP 24576-24639', 'TCP 80'])}
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
    if parts[2:] == ['linecheck']:
        return {'route': {'key': 'CN2 GIA', 'label': '电信 CN2 GIA', 'verdict': '优质线路：晚高峰也比较稳', 'hops': 14,
                          'path': ['192.168.1.1', '100.64.0.1', '61.152.24.1', '202.101.63.1', '59.43.246.1',
                                   '59.43.182.1', '59.43.130.1', '203.0.113.1', '203.0.113.7']},
                'latency': {'avg': 156.2, 'min': 152.8, 'max': 171.4, 'jitter': 4.1, 'loss': 0.0},
                'speed': {'mbps': 186.4, 'megabytes': 16.0}, 'peak': True}
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
    prepare_ui()
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
