#!/usr/bin/env python3
"""Fresh Ubuntu 24.04 deployment only; plan is the default non-mutating operation."""
import sys
sys.dont_write_bytecode = True
import argparse
import datetime
import contextlib
import fcntl
import grp
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import platform
import pwd
import re
import shutil
import signal
import subprocess
import tarfile
import tempfile
import time

import network
import hostfirewall
import kit_runtime
from installlog import InstallLog, redact
from render import (dump, render, settings, SUBSCRIPTION_FILES, account_dir, accounts, client_files, shape_mbps,
                    proxy_ports, share_plan, format_gb, server_config, nginx_config, write_account, KIT_VERSION,
                    port_layout, cloud_ports, hop_ruleset, SPEEDTEST_BYTES,
                    android_guide_text, desktop_guide_text, shadowrocket_guide_text, subscription_endpoints)

HERE = Path(__file__).resolve().parent
CONFIG = Path('/etc/la-vpn')
PROGRAM = Path('/opt/la-vpn')
STATE = Path('/var/lib/la-vpn-deploy')
RUNTIME = Path('/var/lib/la-vpn')
LOGS = Path('/var/log/la-vpn-subscription')
ARCHIVES = Path('/var/lib/la-vpn-archives')
SYSCTL = Path('/etc/sysctl.d/90-la-vpn.conf')
# Added only when the kernel's extra-modules package has no APT candidate.
KERNEL_SOURCE = Path('/etc/apt/sources.list.d/gowest-ubuntu-kernel.sources')
UNITS = Path('/etc/systemd/system')
VERSION = '1.14.3'
# Digests from the official SagerNet GitHub release asset metadata, audited 2026-10-25.
RELEASES = {'x86_64': ('amd64', 'e2bdf179c15a3652955dc44867e33ca0965c9c9b91b34267dcc5a5d639a5feee'),
            'aarch64': ('arm64', '29dae24d76bea3bc62d07dd3e60cfc12003de7debbd6b768df9be9c68360174e')}
PROGRAM_FILES = ('vpn.py', 'render.py', 'routing.py', 'network.py', 'subinfo.py', 'launcher.py',
                 'deploy.sh', 'firewall.py', 'autoconfig.py', 'hostfirewall.py',
                 'kit_runtime.py', 'installlog.py', 'accounting.py', 'unlock.py')
SERVICES = ['la-vpn.service', 'la-vpn-subscription.service', 'la-vpn-fq.service',
            'la-vpn-subinfo.service', 'la-vpn-subinfo.timer', 'la-vpn-hop.service']


@contextlib.contextmanager
def exclusive_operation():
    if platform.system() != 'Linux' or os.geteuid() != 0:
        raise RuntimeError('Mutating commands require root on the new Linux server')
    fd = os.open('/run/la-vpn-deploy.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError('Another deployment/rollback is running; refusing concurrent mutation')
        yield
    finally:
        os.close(fd)


def log_failure(command, message):
    """Private diagnostics only; never print configuration bodies or command arguments."""
    if not STATE.is_dir():
        return
    try:
        fd = os.open(STATE / 'command-errors.log', os.O_CREAT | os.O_APPEND | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'a') as log:
            log.write(datetime.datetime.now(datetime.timezone.utc).isoformat() + ' ' + Path(command).name + '\n')
            log.write(redact(message[:20000]) + '\n')
    except OSError:
        pass  # Preserve the original operational error even if logging fails.


def run(args, *, data=None, timeout=60, check=True):
    # Ubuntu 24.04 needrestart may restart the systemd unit running this very
    # installer after APT upgrades a library. Some Ubuntu hooks still restart
    # services with NEEDRESTART_MODE=l, so suspend that hook for this transaction.
    # This is child-process scoped, not a persistent host policy change; normal
    # restart/update maintenance remains the administrator's responsibility.
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1', DEBIAN_FRONTEND='noninteractive',
               NEEDRESTART_MODE='l', NEEDRESTART_SUSPEND='1')
    # New session protects from SSH hangup; terminate the whole group on a
    # timeout/interrupt so a grandchild cannot race the managed rollback.
    with subprocess.Popen(args, stdin=subprocess.PIPE if data is not None else subprocess.DEVNULL,
                          text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
                          start_new_session=True) as child:
        try:
            stdout, stderr = child.communicate(input=data, timeout=timeout)
        except BaseException as error:
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            child.wait(timeout=5)
            log_failure(args[0], 'command timed out' if isinstance(error, subprocess.TimeoutExpired)
                        else 'command interrupted: ' + type(error).__name__)
            if isinstance(error, subprocess.TimeoutExpired):
                raise subprocess.TimeoutExpired(Path(args[0]).name, timeout) from None
            raise
        p = subprocess.CompletedProcess(args, child.returncode, stdout, stderr)
    if check and p.returncode:
        # Never print commands/config bodies that could contain credentials.
        log_failure(args[0], f'exit={p.returncode}\n' + p.stderr)
        raise RuntimeError(f'{Path(args[0]).name} failed (exit {p.returncode}); inspect private server logs')
    return p


def on_hangup(signum, frame):
    """SSH disconnect: keep the transaction running instead of dying half-way.

    Output moves to a root-private log; the guard timer remains the backstop.
    Children run in their own session, so the terminal hangup does not reach them.
    """
    try:
        target = STATE / 'session-output.log' if STATE.is_dir() else Path('/dev/null')
        fd = os.open(target, os.O_CREAT | os.O_APPEND | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        null = os.open('/dev/null', os.O_RDONLY)
        os.dup2(null, 0)
        os.dup2(fd, 1)
        os.dup2(fd, 2)
        os.write(fd, (datetime.datetime.now(datetime.timezone.utc).isoformat()
                      + ' terminal hangup; continuing without terminal\n').encode())
    except OSError:
        pass


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def load_state():
    return json.loads((STATE / 'state.json').read_text())


def save_state(s):
    network.save(STATE / 'state.json', s)


def managed_file(s, target, content, mode=0o644, group=None):
    target = Path(target)
    if target.exists() or target.is_symlink():
        raise RuntimeError('Refusing to overwrite ' + str(target))
    target.parent.mkdir(parents=True, exist_ok=True)
    data = content if isinstance(content, bytes) else content.encode()
    # Record intent before exclusive creation, including the expected digest.
    s['files'][str(target)] = hashlib.sha256(data).hexdigest()
    save_state(s)
    with target.open('xb') as f:
        os.fchmod(f.fileno(), mode)
        if group:
            os.fchown(f.fileno(), 0, grp.getgrnam(group).gr_gid)
        f.write(data)


def replace_managed(s, target, content, mode=0o644, group=None):
    """Atomically update a file this installer owns, refusing external edits."""
    target = Path(target)
    expected = s['files'].get(str(target))
    if expected is None or target.is_symlink() or sha(target) != expected:
        raise RuntimeError('Managed file changed externally; refusing update: ' + str(target))
    data = content if isinstance(content, bytes) else content.encode()
    tmp = target.with_name('.' + target.name + '.new')
    with tmp.open('wb') as f:
        os.fchmod(f.fileno(), mode)
        if group:
            os.fchown(f.fileno(), 0, grp.getgrnam(group).gr_gid)
        f.write(data)
    s['files'][str(target)] = hashlib.sha256(data).hexdigest()
    save_state(s)
    os.replace(tmp, target)


def served_dir(user):
    return CONFIG / 'subscriptions' / user if user else CONFIG / 'subscriptions'


def os_release():
    return dict(line.split('=', 1) for line in Path('/etc/os-release').read_text().splitlines()
                if '=' in line)


def preflight(s, confirm):
    if platform.system() != 'Linux' or os.geteuid() != 0:
        raise RuntimeError('Installation requires root on a NEW Ubuntu 24.04 server')
    release = os_release()
    if release.get('ID', '').strip('"') != 'ubuntu' or release.get('VERSION_ID', '').strip('"') != '24.04':
        raise RuntimeError('Only Ubuntu 24.04 is currently supported')
    if platform.machine() not in RELEASES or not Path('/run/systemd/system').is_dir():
        raise RuntimeError('Requires amd64/arm64 and a running systemd')
    if confirm != s['server_ip'] or not ipaddress.ip_address(confirm).is_global:
        raise RuntimeError('Confirm the NEW server public IPv4 with --confirm-new-server')
    conflicts = [CONFIG, PROGRAM, STATE, SYSCTL, RUNTIME, KERNEL_SOURCE,
                 Path('/etc/sing-box'), Path('/usr/bin/sing-box'), Path('/usr/local/bin/sing-box'),
                 UNITS / 'nginx.service', Path('/etc/nginx'), LOGS]
    conflicts += [UNITS / name for name in SERVICES]
    conflicts += [UNITS / 'la-vpn-guard.service', UNITS / 'la-vpn-guard.timer']
    if any(p.exists() or p.is_symlink() for p in conflicts):
        raise RuntimeError('Existing proxy/web/managed installation detected; no overwrite or --force mode')
    if any(shutil.which(name) for name in ('sing-box', 'nginx', 'xray', 'v2ray', 'mihomo', 'clash', 'hysteria')):
        raise RuntimeError('Existing proxy/Nginx executable detected; use a clean server')
    for name in ('la-vpn',):
        try:
            pwd.getpwnam(name)
        except KeyError:
            pass
        else:
            raise RuntimeError('Service account already exists; manual review required')
    if int(next(line.split()[1] for line in Path('/proc/meminfo').read_text().splitlines()
                if line.startswith('MemTotal:'))) < 500000:
        raise RuntimeError('At least 512 MiB RAM is required for this tuning profile')
    alts = [x['alt'] for x in port_layout(s).values()] if s['extra_protocols'] else []
    for proto, ports in [('tcp', proxy_ports(s) + alts + [x['port'] for x in subscription_endpoints(s)]),
                         ('udp', proxy_ports(s) + alts)]:
        for port in ports:
            result = run(['ss', '-H', '-ln' + ('t' if proto == 'tcp' else 'u'), f'sport = :{port}'])
            if result.stdout.strip():
                raise RuntimeError(f'Required {proto} port {port} is already in use')
    hostfirewall.display(hostfirewall.inspect(s))
    if s['subscription']['scheme'] == 'https':
        sub = s['subscription']
        for key in ('certificate', 'private_key'):
            p = Path(sub[key])
            if not p.is_file() or p.is_symlink():
                raise RuntimeError('Missing regular HTTPS certificate/key file')
        if not shutil.which('openssl'):
            raise RuntimeError('openssl is required to validate the provided HTTPS certificate before changes')
        run(['openssl', 'x509', '-in', sub['certificate'], '-noout', '-checkend', '86400'])
        cert = run(['openssl', 'x509', '-in', sub['certificate'], '-pubkey', '-noout']).stdout
        key = run(['openssl', 'pkey', '-in', sub['private_key'], '-pubout']).stdout
        if cert != key:
            raise RuntimeError('HTTPS certificate and private key do not match')
        try:
            ipaddress.ip_address(sub['host'])
            flag = '-checkip'
        except ValueError:
            flag = '-checkhost'
        # OpenSSL 3.0 prints mismatch yet returns 0 with x509 -noout.
        result = run(['openssl', 'x509', '-in', sub['certificate'], '-noout', flag, sub['host']])
        if 'does match certificate' not in result.stdout or 'does NOT match' in result.stdout:
            raise RuntimeError('HTTPS certificate does not match the subscription host')
    return network.interface()


def kernel_source_text():
    archive = ('http://archive.ubuntu.com/ubuntu' if platform.machine() == 'x86_64'
               else 'http://ports.ubuntu.com/ubuntu-ports')
    return ('# Managed by gowest: official Ubuntu archive for this kernel\'s extra modules.\n'
            f'Types: deb\nURIs: {archive}\nSuites: noble noble-updates noble-security\n'
            'Components: main\nSigned-By: /usr/share/keyrings/ubuntu-archive-keyring.gpg\n')


def ensure_share_modules(s, log):
    """HTB/fq_codel/u32 are usually in linux-modules; some cloud kernels ship
    them in linux-modules-extra. Install it, adding the official source if the
    image has none, and otherwise continue without per-URL shaping."""
    if not network.missing_modules():
        return 'available'
    package = 'linux-modules-extra-' + platform.release()
    policy = run(['apt-cache', 'policy', package], check=False).stdout
    if 'Candidate:' not in policy or 'Candidate: (none)' in policy:
        log.emit('内核模块', '软件源中没有 ' + package + '，添加 Ubuntu 官方源后重试')
        managed_file(s, KERNEL_SOURCE, kernel_source_text())
        run(['apt-get', 'update'], timeout=300)
    result = run(['apt-get', 'install', '-y', '--no-install-recommends', package], timeout=600, check=False)
    missing = network.missing_modules()
    if missing:
        log.emit('待确认', '无法加载 ' + '、'.join(missing) + '（' + package + ' 安装'
                 + ('失败' if result.returncode else '后仍缺失') + '）；将不启用按订阅链接的公平分配')
        return 'unavailable'
    log.emit('内核模块', '已安装 ' + package + '，公平分配所需模块可用')
    return 'installed'


def download_binary(s):
    managed_file(s, PROGRAM / 'bin/sing-box', fetch_binary(), 0o755)


def fetch_binary():
    arch, expected = RELEASES[platform.machine()]
    name = f'sing-box-{VERSION}-linux-{arch}'
    url = f'https://github.com/SagerNet/sing-box/releases/download/v{VERSION}/{name}.tar.gz'
    archive = STATE / 'sing-box-release.tar.gz'
    run(['curl', '--fail', '--silent', '--show-error', '--location', '--proto', '=https',
         '--proto-redir', '=https', '--max-time', '180', '--retry', '2', '--output', str(archive), url], timeout=560)
    if sha(archive) != expected:
        raise RuntimeError('Official release SHA-256 mismatch; refusing execution')
    with tarfile.open(archive, 'r:gz') as tf:
        member = tf.getmember(name + '/sing-box')
        if not member.isfile() or member.size > 200 * 1024 * 1024:
            raise RuntimeError('Unexpected executable archive member')
        # Never extract arbitrary archive paths or symlinks.
        return tf.extractfile(member).read()


def installed_command(*arguments):
    if kit_runtime.compiled():
        return [str(PROGRAM / 'gowest'), *arguments]
    return ['/usr/bin/python3', '-B', str(PROGRAM / 'launcher.py'), *arguments]


def program_files():
    if kit_runtime.compiled():
        return {PROGRAM / 'gowest': kit_runtime.executable().read_bytes()}
    return {PROGRAM / name: (HERE / name).read_bytes() for name in PROGRAM_FILES}


def install_program(s):
    for target, data in program_files().items():
        managed_file(s, target, data, 0o755)


def service_files(dev):
    fq = (f'/opt/la-vpn/gowest _boot-fq {dev}' if kit_runtime.compiled()
          else f'/usr/bin/python3 /opt/la-vpn/network.py {dev}')
    stats = ('/opt/la-vpn/gowest _subinfo' if kit_runtime.compiled()
             else '/usr/bin/python3 /opt/la-vpn/subinfo.py')
    return {
        'la-vpn.service': '''[Unit]
Description=gowest dual-protocol sing-box
Wants=network-online.target
After=network-online.target la-vpn-fq.service
StartLimitIntervalSec=60
StartLimitBurst=10
[Service]
User=la-vpn
Group=la-vpn
StateDirectory=la-vpn
ExecStart=/opt/la-vpn/bin/sing-box -D /var/lib/la-vpn -c /etc/la-vpn/server.json run
Restart=on-failure
RestartSec=3
LimitNOFILE=65536
AmbientCapabilities=CAP_NET_BIND_SERVICE
CapabilityBoundingSet=CAP_NET_BIND_SERVICE
NoNewPrivileges=true
PrivateTmp=true
ProtectHome=true
ProtectSystem=strict
[Install]
WantedBy=multi-user.target
''',
        'la-vpn-subscription.service': '''[Unit]
Description=Private-path static Clash subscription (Nginx)
Wants=network-online.target
After=network-online.target
[Service]
Type=simple
RuntimeDirectory=la-vpn-subscription
LogsDirectory=la-vpn-subscription
LogsDirectoryMode=0700
ExecStartPre=/usr/sbin/nginx -t -c /etc/la-vpn/nginx.conf
ExecStart=/usr/sbin/nginx -c /etc/la-vpn/nginx.conf -g "daemon off;"
ExecReload=/bin/kill -HUP $MAINPID
KillSignal=SIGQUIT
Restart=on-failure
RestartSec=3
PrivateTmp=true
ProtectHome=true
ProtectSystem=strict
[Install]
WantedBy=multi-user.target
''',
        'la-vpn-fq.service': f'''[Unit]
Description=Conservative FQ application (unknown qdisc left unchanged)
Wants=network-online.target
After=network-online.target
Before=la-vpn.service
[Service]
Type=oneshot
Environment=PYTHONDONTWRITEBYTECODE=1
ExecStart={fq}
RemainAfterExit=yes
[Install]
WantedBy=multi-user.target
''',
        'la-vpn-subinfo.service': f'''[Unit]
Description=Subscription traffic display (not enforcement)
After=vnstat.service la-vpn-subscription.service
[Service]
Type=oneshot
Environment=PYTHONDONTWRITEBYTECODE=1
ExecStart={stats}
''',
        'la-vpn-hop.service': '''[Unit]
Description=Hy2 port hopping redirect (gowest's own nftables table only)
Wants=network-pre.target
After=network-pre.target
Before=la-vpn.service
[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/sbin/nft -f /etc/la-vpn/hop.nft
ExecStop=-/usr/sbin/nft delete table inet la_vpn_hop
[Install]
WantedBy=multi-user.target
''',
        'la-vpn-subinfo.timer': '''[Unit]
Description=Update subscription traffic display every five minutes
[Timer]
OnBootSec=2min
OnUnitActiveSec=5min
[Install]
WantedBy=timers.target
'''}


def unmask_stock(s):
    mask = UNITS / 'nginx.service'
    if s.get('stock_nginx_masked'):
        if mask.is_symlink() and os.readlink(mask) == '/dev/null':
            mask.unlink()
        elif mask.exists() or mask.is_symlink():
            raise RuntimeError('Nginx service mask changed externally; refusing to alter it')
        run(['systemctl', 'daemon-reload'])
        run(['systemctl', 'disable', 'nginx.service'], check=False)
        s['stock_nginx_masked'] = False
        save_state(s)


def verify(s):
    cfg = json.loads((CONFIG / 'settings.json').read_text())
    for name in ('la-vpn.service', 'la-vpn-subscription.service'):
        run(['systemctl', 'is-active', '--quiet', name])
    run([str(PROGRAM / 'bin/sing-box'), 'check', '-c', str(CONFIG / 'server.json')])
    run(['nginx', '-t', '-c', str(CONFIG / 'nginx.conf')])
    cfg = settings(cfg)
    listeners = [(port, proto) for port in proxy_ports(cfg) for proto in ('t', 'u')]
    for x in port_layout(cfg).values():
        listeners += [(x['alt'], proto) for proto, name in (('t', 'anytls'), ('u', 'tuic'))
                      if name in cfg['extra_protocols']]
    for port, proto in listeners:
        if not run(['ss', '-H', '-ln' + proto, f'sport = :{port}']).stdout.strip():
            raise RuntimeError(f'Proxy listener missing on port {port}')
    if cfg['port_hopping']:
        run(['nft', 'list', 'table', 'inet', 'la_vpn_hop'])
    for user in cfg['users'] or ['']:
        verify_account(cfg, user)
    # This is deliberately local validation, not a fabricated end-to-end throughput check.
    report = network.verify_health(STATE / 'network.json')
    for warning in report['warnings']:
        print('Network warning: ' + warning, file=sys.stderr)


def verify_account(cfg, user):
    sub = cfg['subscription']
    generated, served = STATE / 'generated' / account_dir(user), served_dir(user)
    urls = (generated / 'subscription-urls.txt').read_text().splitlines()
    if len(urls) != 2 or not urls[0].startswith('normal: ') or not urls[1].startswith('strict: '):
        raise RuntimeError('Subscription URL manifest is incomplete or malformed')
    for row, name in zip(urls, ('normal.yaml', 'strict.yaml')):
        url = row.split(': ', 1)[1]
        # URL is sent over stdin, not exposed in process arguments or logs.
        request = f'url = "{url}"\nresolve = "{sub["host"]}:{sub["port"]}:127.0.0.1"\n'
        result = run(['curl', '--config', '-', '--noproxy', '*', '--fail', '--silent', '--show-error',
                      '--max-time', '20'], data=request, timeout=25)
        if hashlib.sha256(result.stdout.encode()).hexdigest() != sha(served / name):
            raise RuntimeError('Served subscription does not match generated configuration')
    extra = generated / 'client-urls.txt'
    if extra.is_file():
        expected = {name for suffix, name in SUBSCRIPTION_FILES.items() if suffix not in ('', '-us')}
        entries = [row.split(': ', 1) for row in extra.read_text().splitlines()]
        if len(entries) != len(expected) or {row[0] for row in entries} != expected:
            raise RuntimeError('Client URL manifest is incomplete or malformed')
        for name, url in entries:
            request = f'url = "{url}"\nresolve = "{sub["host"]}:{sub["port"]}:127.0.0.1"\n'
            result = run(['curl', '--config', '-', '--noproxy', '*', '--fail', '--silent',
                          '--show-error', '--max-time', '20', '--max-filesize', '1048576'],
                         data=request, timeout=25)
            if hashlib.sha256(result.stdout.encode()).hexdigest() != sha(served / name):
                raise RuntimeError('Native client subscription response mismatch')
    transports = generated / 'transport-urls.json'
    if transports.is_file():
        entries = json.loads(transports.read_text())
        expected_endpoints = subscription_endpoints(cfg)
        if [{k: entry.get(k) for k in ('scheme', 'host', 'port')} for entry in entries] != expected_endpoints:
            raise RuntimeError('Subscription transport manifest mismatch')
        for entry in entries:
            for suffix, name in SUBSCRIPTION_FILES.items():
                request = f'url = "{entry["base"]}{suffix}"\nresolve = "{entry["host"]}:{entry["port"]}:127.0.0.1"\n'
                result = run(['curl', '--config', '-', '--noproxy', '*', '--fail', '--silent',
                              '--show-error', '--max-time', '20', '--max-filesize', '1048576'],
                             data=request, timeout=25)
                if hashlib.sha256(result.stdout.encode()).hexdigest() != sha(served / name):
                    raise RuntimeError('Subscription transport content mismatch')


def verify_started(s, timeout=30):
    """Type=simple services may return from systemctl before opening listeners."""
    deadline = time.monotonic() + timeout
    while True:
        try:
            verify(s)
            return
        except (RuntimeError, subprocess.TimeoutExpired):
            if time.monotonic() >= deadline:
                raise
            time.sleep(1)


def install(cfg, confirm, firewall_mode='auto'):
    log = InstallLog()
    try:
        with log.step(1, '只读预检：系统、端口、既有服务与防火墙'):
            dev = preflight(cfg, confirm)
    except BaseException:
        log.emit('停止', '预检未通过，尚未创建安装目录或修改服务；请使用空白 Ubuntu 24.04。')
        log.close()
        raise
    os.umask(0o077)
    STATE.mkdir(mode=0o700)
    s = {'status': 'installing', 'server_ip': cfg['server_ip'], 'files': {}, 'kit_version': KIT_VERSION,
         'created_at': datetime.datetime.now(datetime.timezone.utc).isoformat(),
         'stock_nginx_masked': False}
    save_state(s)
    try:
        log.attach(STATE / 'install.log')
        log.emit('日志', '安装日志：' + str(log.path) + '（仅 root 可读；不记录订阅链接）')
        with log.step(2, '安装管理程序并启动失败回滚保护'):
            PROGRAM.mkdir(mode=0o755)
            os.chmod(PROGRAM, 0o755)
            install_program(s)
            run(['systemd-run', '--unit=la-vpn-guard', '--on-active=30m',
                 '--timer-property=OnUnitActiveSec=2min'] + installed_command('rollback', '--automatic'))
        with log.step(3, '安装系统依赖（APT；较慢时持续显示等待时间）'):
            s['stock_nginx_masked'] = True
            save_state(s)
            (UNITS / 'nginx.service').symlink_to('/dev/null')
            run(['systemctl', 'daemon-reload'])
            run(['apt-get', 'update'], timeout=300)
            packages = ['ca-certificates', 'curl', 'openssl', 'iproute2', 'kmod', 'nginx', 'vnstat',
                        'nftables', 'qrencode']
            if not kit_runtime.compiled():
                packages.append('python3-yaml')
            run(['apt-get', 'install', '-y', '--no-install-recommends'] + packages, timeout=600)
            if cfg['fair_share']:
                s['share_modules'] = ensure_share_modules(s, log)
                save_state(s)
            unmask_stock(s)
            run(['useradd', '--system', '--user-group', '--home-dir', '/var/lib/la-vpn',
                 '--no-create-home', '--shell', '/usr/sbin/nologin', 'la-vpn'])
        with log.step(4, '下载 sing-box 并校验官方 SHA-256'):
            (PROGRAM / 'bin').mkdir(mode=0o755)
            os.chmod(PROGRAM / 'bin', 0o755)
            download_binary(s)
        with log.step(5, '生成独立密钥及全部电脑、Android、iOS 配置'):
            out = render(cfg, STATE / 'generated')
            CONFIG.mkdir(mode=0o755)
            os.chmod(CONFIG, 0o755)
            for name, mode, group in [('server.json', 0o640, 'la-vpn'), ('hy2.key', 0o640, 'la-vpn'),
                                      ('hy2.crt', 0o644, None), ('settings.json', 0o600, None),
                                      ('nginx.conf', 0o600, None), ('subinfo.conf', 0o644, None),
                                      ('hop.nft', 0o600, None)]:
                managed_file(s, CONFIG / name, (out / name).read_bytes(), mode, group)
            s['files'].pop(str(CONFIG / 'subinfo.conf'))  # Stats intentionally change this file.
            save_state(s)
            for user in [''] + cfg['users']:
                folder = served_dir(user)
                folder.mkdir(mode=0o750)
                os.chmod(folder, 0o750)
                os.chown(folder, 0, grp.getgrnam('www-data').gr_gid)
            for user in cfg['users'] or ['']:
                for name in SUBSCRIPTION_FILES.values():
                    managed_file(s, served_dir(user) / name, (out / account_dir(user) / name).read_bytes(),
                                 0o640, 'www-data')
            if cfg['subscription']['scheme'] == 'https':
                for key, name in [('certificate', 'subscription-fullchain.pem'), ('private_key', 'subscription-privkey.pem')]:
                    managed_file(s, CONFIG / name, Path(cfg['subscription'][key]).read_bytes(), 0o600)
            ensure_speedtest()
            run([str(PROGRAM / 'bin/sing-box'), 'check', '-c', str(CONFIG / 'server.json')])
        with log.step(6, '应用保守网络配置并启动专属服务'):
            net = network.apply(STATE / 'network.json', plan=share_plan(cfg))
            result = net.get('shaping', {}).get('result', 'not-recorded')
            if cfg['fair_share'] and result != 'applied':
                log.emit('待确认', '按订阅链接的公平分配未启用：' + result + '；运行 status 查看')
            text = '# Managed by gowest; only explicit conservative parameters.\n'
            text += ''.join(k + ' = ' + v + '\n' for k, v in net['applied'].items())
            managed_file(s, SYSCTL, text)
            for name, text in service_files(dev).items():
                managed_file(s, UNITS / name, text)
            run(['systemctl', 'daemon-reload'])
            run(['systemctl', 'enable', '--now', 'la-vpn-fq.service', 'la-vpn-hop.service', 'la-vpn.service',
                 'la-vpn-subscription.service', 'vnstat.service'], timeout=90)
            run(['vnstat', '--add', '-i', dev], check=False)
        with log.step(7, '校验本机监听、订阅内容与主机防火墙'):
            verify_started(s)
            report = hostfirewall.apply(cfg, s, save_state, firewall_mode)
            hostfirewall.display(report)
            log.emit('防火墙', report['status'])
            for warning in report.get('warnings', []):
                log.emit('待确认', warning)
        with log.step(8, '启用统计任务并保存安装结果'):
            run(['systemctl', 'enable', '--now', 'la-vpn-subinfo.timer'])
            s['status'] = 'installed'
            save_state(s)
            run(['systemctl', 'stop', 'la-vpn-guard.timer'], check=False)
        entry = '/opt/la-vpn/gowest' if kit_runtime.compiled() else 'bash /opt/la-vpn/deploy.sh'
        log.emit('本机安装完成', '全部客户端配置已生成，本机校验通过；公网连通性尚未验收。')
        log.emit('下一步', '单独查看私有订阅：sudo ' + entry + ' urls')
        log.emit('下一步', '分端说明：sudo ' + entry + ' urls --client mac|windows|android|shadowrocket')
        if s['host_firewall']['report']['status'] in ('manual-review', 'check-only-no-changes', 'ufw-overlap-needs-review'):
            log.emit('待处理', '本机防火墙尚需人工处理；不能视为公网部署完成。')
        log.emit('待处理', '云安全组未验证：请确认放行 ' + '、'.join(cloud_ports(cfg)) + '；再由外部客户端验收。')
    except BaseException:
        log.emit('回滚开始', '安装未完成；尝试回滚受管改动，保留已安装依赖。')
        try:
            archive = rollback(automatic=True)
            if archive:
                log.emit('回滚完成', '配置和密钥可恢复；归档：' + str(archive))
                log.emit('日志', '日志已随归档移动：' + str(archive / str(STATE).strip('/').replace('/', '--') / 'install.log'))
            else:
                log.emit('未回滚', '安装状态已提交，自动保护未撤销成功安装；请运行 status 和 verify 核对。')
        except Exception as error:
            log.emit('回滚未完成', str(error) + '；请检查安装日志和服务器服务状态，不要强制覆盖。')
        raise
    finally:
        log.close()


def rollback(automatic=False):
    if platform.system() != 'Linux' or os.geteuid() != 0:
        raise RuntimeError('Rollback requires root on the installation server')
    s = load_state()
    if automatic and s['status'] == 'installed':
        return  # Successful installs are never undone by a late watchdog.
    for path, expected in s['files'].items():
        p = Path(path)
        if p.is_symlink() or (p.exists() and (not p.is_file() or sha(p) != expected)):
            raise RuntimeError('Managed file changed externally; refusing rollback: ' + path)
    net = STATE / 'network.json'
    if net.exists():
        network.check_restore(json.loads(net.read_text()))
    hostfirewall.check_restore(s)
    s['status'] = 'rolling-back'
    save_state(s)
    for name in ['la-vpn-subinfo.timer', 'la-vpn-subinfo.service', 'la-vpn-subscription.service',
                 'la-vpn.service', 'la-vpn-hop.service', 'la-vpn-fq.service']:
        run(['systemctl', 'disable', '--now', name], check=False)
    if shutil.which('nft'):
        run(['nft', 'delete', 'table', 'inet', 'la_vpn_hop'], check=False)
    if net.exists():
        network.restore(net)
    if shutil.which('nft'):
        import accounting
        accounting.remove()
    hostfirewall.restore(s, save_state)
    unmask_stock(s)
    # Preserve every managed file, including keys, instead of deleting the deployment.
    archives = ARCHIVES
    if archives.is_symlink():
        raise RuntimeError('Unsafe archive directory')
    archives.mkdir(mode=0o700, exist_ok=True)
    os.chmod(archives, 0o700)
    archive = Path(tempfile.mkdtemp(prefix='rollback-', dir=archives))
    for path in [SYSCTL, KERNEL_SOURCE] + [UNITS / name for name in SERVICES]:
        if str(path) in s['files'] and path.exists():
            path.rename(archive / path.name)
    run(['systemctl', 'stop', 'la-vpn-guard.timer'], check=False)
    run(['systemctl', 'daemon-reload'])
    s['status'] = 'rolled-back'
    save_state(s)
    for path in [CONFIG, PROGRAM, RUNTIME, LOGS, STATE]:
        if path.exists():
            # Name collisions avoided for /etc/la-vpn and /opt/la-vpn.
            path.rename(archive / (str(path).strip('/').replace('/', '--')))
    print('Managed deployment archived at ' + str(archive))
    print('APT dependencies, vnStat data and the system service account remain; no package purge was performed.')
    return archive


def show_transports(transport, client, user=''):
    cfg = settings(json.loads((CONFIG / 'settings.json').read_text()))
    generated = STATE / 'generated' / account_dir(user)
    path = generated / 'transport-urls.json'
    if path.is_file():
        entries = json.loads(path.read_text())
    else:
        base = (generated / 'subscription-urls.txt').read_text().splitlines()[0].split(': ', 1)[1]
        entries = [dict(subscription_endpoints(cfg)[0], base=base)]
    selected = [entry for entry in entries if transport == 'all' or entry['scheme'] == transport]
    if not selected:
        raise RuntimeError('此安装未启用所选订阅方式；不会自动修改现有服务器配置')
    for entry in selected:
        print('\n' + entry['scheme'].upper() + ' 订阅入口')
        base = entry['base']
        if entry['scheme'] == 'http':
            print('警告：HTTP 明文传输节点凭据，与 HTTPS 共用密钥，双选不等于全部加密。')
        if client in ('all', 'clash'):
            print('normal: ' + base + '\nstrict: ' + base + '-us')
        if client in ('all', 'mac', 'windows'):
            print(desktop_guide_text(base, cfg['node_prefix'], user))
        if client in ('all', 'android'):
            print(android_guide_text(base, cfg['node_prefix'], user))
        if client in ('all', 'shadowrocket'):
            print(shadowrocket_guide_text(base, cfg['node_prefix'], user))


def traffic_summary(record):
    """traffic.json (bytes) -> human GB for status; raw bytes stay in the file."""
    quota = record.get('quota_bytes')
    used = format_gb(record['used_bytes'])
    return {'month': record['month'],
            'used': used + (f' / {format_gb(quota)}（{record["percent"]}%）' if quota else '（未设置上限）'),
            'metering': {'both': '进+出', 'outbound': '只计出方向'}.get(record.get('metering'), record.get('metering')),
            'on_exceed': record.get('on_exceed'),
            'paused': '已暂停（' + record['paused_month'] + '）' if record.get('paused_month') else '否'}


def installed(confirm):
    s = load_state()
    if s['status'] != 'installed' or confirm != s['server_ip']:
        raise RuntimeError('Confirm the installed server public IPv4 with --confirm-server')
    path = CONFIG / 'settings.json'
    if path.is_symlink() or sha(path) != s['files'].get(str(path)):
        raise RuntimeError('Managed file changed externally; refusing update: ' + str(path))
    return s, settings(json.loads(path.read_text()))


def change_bandwidth(text, confirm, user='', cap=None):
    """Re-target the plan bandwidth or one URL's cap; keys and URLs stay the same."""
    import launcher
    s, cfg = installed(confirm)
    if user:
        if user not in cfg['users']:
            raise RuntimeError('没有这个订阅用户：' + user)
        if cap in ('none', '0'):
            cfg['user_caps'].pop(user, None)
        else:
            cfg['user_caps'][user] = launcher.whole_mbps(cap, '上限')
    else:
        mbps = launcher.whole_mbps(text, '带宽')
        cfg['hy2_up_mbps'] = cfg['hy2_down_mbps'] = mbps
    cfg = settings(cfg)
    if cfg['fair_share']:
        # BBR clients carry no bandwidth, so only the share tree changes.
        plan = share_plan(cfg)
        if network.health(STATE / 'network.json')['fair_share']['result'] == 'applied':
            network.set_plan(STATE / 'network.json', plan)
            print(dump({'fair_share_plan': plan}), end='')
            print('已即时生效：每个订阅链接保底 总速率/人数，空闲时可借用到各自上限；无需刷新订阅。')
        else:
            print('公平分配未生效，只更新记录的参数；运行 status 查看原因。')
    elif user:
        raise RuntimeError('单人模式没有按链接的上限')
    else:
        # Single user with Brutal: the Hy2 targets live inside client profiles.
        keys = json.loads((STATE / 'generated/secrets.json').read_text())
        for account in accounts(cfg, keys):
            for name, body in client_files(cfg, account).items():
                replace_managed(s, served_dir(account['name']) / name, body, 0o640, 'www-data')
        print(f'Hy2 目标已改为 {mbps} Mbps；订阅地址不变，请在各客户端“更新订阅”后生效。')
    replace_managed(s, CONFIG / 'settings.json', dump(cfg), 0o600)


def change_traffic(confirm, quota=None, metering=None, on_exceed=None):
    from render import parse_bytes
    s, cfg = installed(confirm)
    if quota is not None:
        cfg['traffic']['total_bytes'] = parse_bytes(quota)
    if metering:
        cfg['traffic']['metering'] = metering
    if on_exceed:
        cfg['traffic']['on_exceed'] = on_exceed
    cfg = settings(cfg)
    replace_managed(s, CONFIG / 'settings.json', dump(cfg), 0o600)
    # Refresh the header and quota decision now instead of waiting five minutes.
    run(['systemctl', 'start', 'la-vpn-subinfo.service'], check=False, timeout=60)
    print(dump({'traffic': dict(cfg['traffic'], monthly_quota=format_gb(cfg['traffic']['total_bytes'])
                                if cfg['traffic']['total_bytes'] else '不设置')}))


def sing_box_version():
    out = run([str(PROGRAM / 'bin/sing-box'), 'version'], check=False).stdout or ''
    words = out.split()
    return words[2] if len(words) > 2 and words[:2] == ['sing-box', 'version'] else ''


def ensure_speedtest():
    """The private line-check download: incompressible-enough zeros (no gzip is configured)."""
    path = CONFIG / 'speedtest.bin'
    if path.is_file() and path.stat().st_size == SPEEDTEST_BYTES:
        return
    tmp = CONFIG / '.speedtest.bin.new'
    with tmp.open('wb') as f:
        f.truncate(SPEEDTEST_BYTES)
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)


def change_options(confirm, hopping=None, protocols=None, obfs=None):
    """Turn port hopping, the extra protocols and Hy2 obfuscation on or off.

    Keys, ports and subscription URLs stay the same; clients pick the change up
    on their next subscription update (until then their old nodes keep working)."""
    s, cfg = installed(confirm)
    before = settings(dict(cfg))
    if hopping is not None:
        cfg['port_hopping'] = hopping
    if protocols is not None:
        cfg['extra_protocols'] = protocols
    if obfs is not None:
        cfg['hy2_obfs'] = obfs
    cfg = settings(cfg)
    keys = load_keys()
    ensure_speedtest()
    reconcile(s, config_targets(cfg, keys), cfg, keys)
    after_users_change(s, cfg)
    new_pairs = [pair for pair in hostfirewall.required_ports(cfg) if pair not in hostfirewall.required_ports(before)]
    if new_pairs:
        if hostfirewall.add_ports(cfg, s, save_state, new_pairs):
            print('本机防火墙已放行新增端口。')
        else:
            print('请确认本机防火墙放行：' + '、'.join(f'{p.upper()} {hostfirewall.port_text(n)}' for p, n in new_pairs))
    print('当前：端口跳跃 ' + ('开' if cfg['port_hopping'] else '关') + '；备用协议 '
          + ('、'.join(x.upper() if x == 'tuic' else 'AnyTLS' for x in cfg['extra_protocols']) or '无')
          + '；Hy2 混淆 ' + ('开' if cfg['hy2_obfs'] else '关') + '。')
    print('云安全组需要放行：' + '、'.join(cloud_ports(cfg)))
    print('客户端"更新订阅"后生效；更新前旧的节点仍可使用。')


def load_keys():
    return json.loads((STATE / 'generated/secrets.json').read_text())


def config_targets(cfg, keys):
    """Every owned file derived from settings + keys, as path -> (bytes, mode, group)."""
    targets = {CONFIG / 'settings.json': (dump(cfg).encode(), 0o600, None),
               CONFIG / 'server.json': (dump(server_config(cfg, keys)).encode(), 0o640, 'la-vpn'),
               CONFIG / 'nginx.conf': (nginx_config(cfg, keys).encode(), 0o600, None),
               CONFIG / 'hop.nft': (hop_ruleset(cfg).encode(), 0o600, None)}
    for account in accounts(cfg, keys):
        for name, body in client_files(cfg, account).items():
            targets[served_dir(account['name']) / name] = (body.encode(), 0o640, 'www-data')
    return targets


def refresh_generated(cfg, keys):
    """Rewrite the private copies urls/verify read, and the keys they come from."""
    generated = STATE / 'generated'
    with tempfile.TemporaryDirectory(dir=STATE) as tmp:
        for account in accounts(cfg, keys):
            fresh = Path(tmp) / account_dir(account['name'])
            write_account(fresh, cfg, account)
            target = generated / account_dir(account['name'])
            target.mkdir(mode=0o700, parents=True, exist_ok=True)
            for item in fresh.iterdir():
                os.replace(item, target / item.name)
    if cfg['users']:
        for folder in (generated / 'users').iterdir():
            if folder.name not in cfg['users']:
                shutil.rmtree(folder)
    tmp = generated / '.secrets.json.new'
    tmp.write_text(dump(keys))
    os.chmod(tmp, 0o600)
    os.replace(tmp, generated / 'secrets.json')


def snapshot(path):
    st = path.stat()
    return path.read_bytes(), st.st_mode & 0o777, st.st_gid


def reconcile(s, targets, cfg, keys, removed=()):
    """Bring owned files to `targets` and remove `removed`, all or nothing.

    Every file is checked against its recorded digest before anything changes;
    a validation or start failure puts every file back and restarts again.
    Returns the restarted services, or None when nothing differed."""
    changed = {p: v for p, v in targets.items() if p.is_symlink() or not p.exists() or p.read_bytes() != v[0]}
    removed = [p for p in removed if p.exists() or p.is_symlink()]
    for path in list(changed) + removed:
        recorded = s['files'].get(str(path))
        if recorded is None and (path.exists() or path.is_symlink()):
            raise RuntimeError('File not owned by gowest; refusing change: ' + str(path))
        if recorded is not None and (path.is_symlink() or not path.exists() or sha(path) != recorded):
            raise RuntimeError('Managed file changed externally; refusing change: ' + str(path))
    if not changed and not removed:
        return None
    backup = {path: snapshot(path) if path.exists() else None for path in list(changed) + removed}
    generated = STATE / 'generated'
    saved = STATE / 'generated.previous'
    shutil.rmtree(saved, ignore_errors=True)
    shutil.copytree(generated, saved, symlinks=True)
    names = {path.name for path in list(changed) + removed}
    restart = []
    if names & {'server.json', 'sing-box', 'la-vpn.service'}:
        restart.append('la-vpn.service')
    if names & {'nginx.conf', 'la-vpn-subscription.service'}:
        restart.append('la-vpn-subscription.service')
    hop = bool(names & {'hop.nft', 'la-vpn-hop.service'})
    try:
        for path, (data, mode, group) in changed.items():
            if str(path) in s['files']:
                replace_managed(s, path, data, mode, group)
            else:
                managed_file(s, path, data, mode, group)
        for path in removed:
            s['files'].pop(str(path))
            save_state(s)
            path.unlink()
        refresh_generated(cfg, keys)
        run(['systemctl', 'daemon-reload'])
        run([str(PROGRAM / 'bin/sing-box'), 'check', '-c', str(CONFIG / 'server.json')])
        run(['nginx', '-t', '-c', str(CONFIG / 'nginx.conf')])
        if hop:
            # Redirects load before the proxy restarts; enable covers upgrades from older units.
            run(['systemctl', 'enable', 'la-vpn-hop.service'])
            run(['systemctl', 'restart', 'la-vpn-hop.service'], timeout=60)
        if restart:
            run(['systemctl', 'restart'] + restart, timeout=90)
        verify_started(s)
    except BaseException:
        print('修改未通过校验，正在恢复原来的文件……', file=sys.stderr)
        for path, old in backup.items():
            if old is None:
                path.unlink(missing_ok=True)
                s['files'].pop(str(path), None)
                continue
            data, mode, gid = old
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name('.' + path.name + '.restore')
            tmp.write_bytes(data)
            os.chmod(tmp, mode)
            os.chown(tmp, 0, gid)
            os.replace(tmp, path)
            s['files'][str(path)] = hashlib.sha256(data).hexdigest()
        save_state(s)
        shutil.rmtree(generated)
        saved.rename(generated)
        run(['systemctl', 'daemon-reload'], check=False)
        if hop and (CONFIG / 'hop.nft').exists():
            run(['systemctl', 'restart', 'la-vpn-hop.service'], check=False, timeout=60)
        if restart:
            run(['systemctl', 'restart'] + restart, check=False, timeout=90)
        raise
    shutil.rmtree(saved, ignore_errors=True)
    return restart


def upgrade(confirm):
    """Swap in this kit's code and re-render configs from the stored keys.

    Keys, ports and subscription URLs stay the same."""
    s, cfg = installed(confirm)
    keys = load_keys()
    dev = json.loads((STATE / 'network.json').read_text())['interface']
    targets = {path: (data, 0o755, None) for path, data in program_files().items()}
    targets.update({UNITS / name: (text.encode(), 0o644, None) for name, text in service_files(dev).items()})
    targets.update(config_targets(cfg, keys))
    current = sing_box_version()
    if current and current != VERSION:
        targets[PROGRAM / 'bin/sing-box'] = (fetch_binary(), 0o755, None)
    previous = s.get('kit_version', '0.5.9 或更早')
    # Tools newer versions rely on (per-URL counters, QR codes); installs made
    # before them lack the packages. A failure only disables those features.
    missing = [name for name, tool in (('nftables', 'nft'), ('qrencode', 'qrencode')) if not shutil.which(tool)]
    if missing and run(['apt-get', 'install', '-y', '--no-install-recommends'] + missing,
                       check=False, timeout=600).returncode:
        print('未能安装 ' + '、'.join(missing) + '；分链接统计或二维码暂不可用，可稍后重试 upgrade。')
    ensure_speedtest()
    restart = reconcile(s, targets, cfg, keys)
    s['kit_version'] = KIT_VERSION
    save_state(s)
    if restart is None:
        print(f'已是最新：gowest {KIT_VERSION}，sing-box {current or VERSION}；没有文件需要更新。')
        return
    after_users_change(s, cfg)
    print(f'已升级：gowest {previous} → {KIT_VERSION}'
          + (f'，重启了 {"、".join(restart)}' if restart else '，无需重启代理') + '。')
    print('密钥、端口和订阅地址都没有变；客户端下次更新订阅时会拿到新配置。')


def after_users_change(s, cfg):
    """Queue tree, counters and subscription headers follow the current URL list."""
    if not cfg['users']:
        return
    plan = share_plan(cfg)
    if plan and network.health(STATE / 'network.json')['fair_share']['result'] == 'applied':
        network.replace_plan(STATE / 'network.json', plan)
    run(['systemctl', 'start', 'la-vpn-subinfo.service'], check=False, timeout=60)


def show_qr(base, client, user):
    """Phone imports: Shadowrocket scans the node subscription itself; on
    Android the camera opens the clashmeta:// link in CMFA."""
    from render import install_link, profile_names
    if not shutil.which('qrencode'):
        print('\n显示二维码需要 qrencode：sudo apt-get install -y qrencode')
        return
    label = json.loads((CONFIG / 'settings.json').read_text()).get('node_prefix', 'LA')
    codes = []
    if client in ('all', 'shadowrocket'):
        codes.append(('iPhone 小火箭：首页右上角扫码，导入节点订阅', base + '-nodes'))
    if client in ('all', 'android'):
        codes.append(('Android：用相机扫码，在 CMFA 中打开（日常分流）',
                      install_link('clashmeta', base, profile_names(label, user)['normal'])))
    if not codes:
        print('\n电脑端请用上面的链接导入；二维码用于手机（--client android 或 shadowrocket）。')
    for title, text in codes:
        print('\n' + title + '（含私密订阅，勿截图外传）')
        print(run(['qrencode', '-t', 'ANSIUTF8', '-m', '2'], data=text).stdout, end='')


def user_rows(cfg):
    usage = STATE / 'user-traffic.json'
    record = json.loads(usage.read_text()) if usage.is_file() else {}
    rows = []
    for name, port in zip(cfg['users'], proxy_ports(cfg)):
        row = record.get('users', {}).get(name, {})
        used = row.get('upload', 0) + row.get('download', 0)
        quota = cfg['user_quota'].get(name, 0)
        rows.append({'name': name, 'port': port, 'cap_mbps': cfg['user_caps'].get(name),
                     'used_bytes': used, 'quota_bytes': quota, 'upload': row.get('upload', 0),
                     'download': row.get('download', 0), 'paused': row.get('paused', ''),
                     'expire_date': cfg['user_expire'].get(name, ''),
                     'cap': f"{cfg['user_caps'][name]} Mbps" if name in cfg['user_caps'] else '不限',
                     'used': format_gb(used) + (f' / {format_gb(quota)}' if quota else ''),
                     'expire': cfg['user_expire'].get(name, '不限'),
                     'status': {'quota': '已暂停（超额度）', 'expired': '已暂停（已到期）'}.get(row.get('paused'), '正常')})
    return rows


def print_users(cfg):
    if not cfg['users']:
        print('单人模式（端口 443），没有分链接的设置。')
        return
    print(f'{"名字":<12}{"端口":<8}{"速度上限":<12}{"本月用量 / 额度":<24}{"到期日":<12}状态')
    for r in user_rows(cfg):
        print(f"{r['name']:<14}{r['port']:<10}{r['cap']:<14}{r['used']:<28}{r['expire']:<15}{r['status']}")


def status_report():
    """Everything the local web UI shows for one server, machine-readable."""
    s = load_state()
    cfg = settings(json.loads((CONFIG / 'settings.json').read_text()))
    usage = STATE / 'traffic.json'
    traffic = json.loads(usage.read_text()) if usage.is_file() else {}
    health = network.health(STATE / 'network.json')
    return {'status': s['status'], 'server_ip': s['server_ip'], 'created_at': s['created_at'],
            'kit_version': s.get('kit_version', '0.5.9 或更早'), 'sing_box': sing_box_version() or 'unknown',
            'services': {name: run(['systemctl', 'is-active', name], check=False).stdout.strip() for name in SERVICES},
            'node_prefix': cfg['node_prefix'], 'bandwidth_mbps': cfg['hy2_down_mbps'],
            'subscription_scheme': cfg['subscription']['scheme'], 'fair_share': cfg['fair_share'],
            'fair_share_result': health['fair_share']['result'], 'bbr': not any('BBR' in w for w in health['warnings']),
            'warnings': health['warnings'],
            'traffic': {'month': traffic.get('month'), 'used_bytes': traffic.get('used_bytes'),
                        'quota_bytes': cfg['traffic']['total_bytes'], 'metering': cfg['traffic']['metering'],
                        'on_exceed': cfg['traffic']['on_exceed'], 'paused_month': traffic.get('paused_month')},
            'single': not cfg['users'], 'users': user_rows(cfg) if cfg['users'] else [],
            'options': {'port_hopping': cfg['port_hopping'], 'extra_protocols': cfg['extra_protocols'],
                        'hy2_obfs': cfg['hy2_obfs']},
            'cloud_ports': cloud_ports(cfg)}


def url_report(user):
    """One URL's subscription links and app deep links, machine-readable."""
    from render import install_link, profile_names
    cfg = settings(json.loads((CONFIG / 'settings.json').read_text()))
    if cfg['users'] and user not in cfg['users']:
        raise RuntimeError('多人模式请指定其中一人：' + '、'.join(cfg['users']))
    generated = STATE / 'generated' / account_dir(user)
    base = (generated / 'subscription-urls.txt').read_text().splitlines()[0].split(': ', 1)[1]
    names = profile_names(cfg['node_prefix'], user)
    return {'user': user, 'base': base,
            'files': {name: base + suffix for suffix, name in SUBSCRIPTION_FILES.items()},
            'clash': {'normal': install_link('clash', base, names['normal']),
                      'strict': install_link('clash', base + '-us', names['strict'])},
            'clashmeta': {'normal': install_link('clashmeta', base, names['normal']),
                          'strict': install_link('clashmeta', base + '-us', names['strict'])}}


def change_users(confirm, action, name='', cap=None, quota=None, expire=None):
    """add / remove / reset / set one subscription URL; other URLs stay unchanged."""
    import launcher
    from render import USER_NAME, MAX_USERS, free_port, new_credentials, parse_bytes, parse_date
    s, cfg = installed(confirm)
    if not cfg['users']:
        raise RuntimeError('这台服务器是单人安装（端口 443），不能增删链接。'
                           '需要多人共用时，请 rollback 后重新安装，并在"共用份数"填 2 或以上。')
    keys = load_keys()
    if not cfg['user_ports']:
        cfg['user_ports'] = dict(zip(cfg['users'], proxy_ports(cfg)))
    removed = []
    if action == 'add':
        if not re.fullmatch(USER_NAME, name or ''):
            raise RuntimeError('名字只能用小写字母、数字和 -，以字母开头，最长 16 个字符')
        if name in cfg['users']:
            raise RuntimeError('已经有这个名字：' + name)
        if len(cfg['users']) >= MAX_USERS:
            raise RuntimeError(f'最多 {MAX_USERS} 个链接')
        cfg['user_ports'][name] = free_port(cfg)
        cfg['users'].append(name)
        keys['users'].append(dict(new_credentials(), name=name))
    elif name not in cfg['users']:
        raise RuntimeError('没有这个订阅用户：' + (name or '（未指定）') + '；现有：' + '、'.join(cfg['users']))
    elif action == 'remove':
        if len(cfg['users']) == 1:
            raise RuntimeError('至少要保留一个链接')
        cfg['users'].remove(name)
        for key in ('user_ports', 'user_caps', 'user_quota', 'user_expire'):
            cfg[key].pop(name, None)
        keys['users'] = [u for u in keys['users'] if u['name'] != name]
        removed = [served_dir(name) / file for file in SUBSCRIPTION_FILES.values()]
    elif action == 'reset':
        keys['users'] = [dict(u, **new_credentials()) if u['name'] == name else u for u in keys['users']]
    if cap is not None:
        if cap in ('none', '0', ''):
            cfg['user_caps'].pop(name, None)
        else:
            cfg['user_caps'][name] = launcher.whole_mbps(cap, '上限')
    if quota is not None:
        if parse_bytes(quota):
            cfg['user_quota'][name] = parse_bytes(quota)
        else:
            cfg['user_quota'].pop(name, None)
    if expire is not None:
        if expire in ('none', '0', ''):
            cfg['user_expire'].pop(name, None)
        else:
            cfg['user_expire'][name] = parse_date(expire)
    cfg = settings(cfg)
    if action == 'add':
        folder = served_dir(name)
        folder.mkdir(mode=0o750, exist_ok=True)
        os.chmod(folder, 0o750)
        os.chown(folder, 0, grp.getgrnam('www-data').gr_gid)
    try:
        reconcile(s, config_targets(cfg, keys), cfg, keys, removed)
    except BaseException:
        if action == 'add':
            shutil.rmtree(served_dir(name), ignore_errors=True)
        raise
    if action == 'remove':
        shutil.rmtree(served_dir(name), ignore_errors=True)
    after_users_change(s, cfg)
    if action == 'add':
        port = cfg['user_ports'][name]
        pairs = [('tcp', port), ('udp', port)]
        if hostfirewall.add_ports(cfg, s, save_state, pairs):
            print(f'本机防火墙已放行 TCP/UDP {port}。')
        else:
            print(f'请确认本机防火墙放行 TCP/UDP {port}。')
        print(f'云安全组也要放行 TCP/UDP {port}（建议一次放行 TCP/UDP 20001-20032，以后加人就不用再改）。')
    print_users(cfg)
    if action in ('add', 'reset'):
        print(f'\n{name} 的新订阅地址（只发给他本人）：')
        print((STATE / 'generated' / account_dir(name) / 'subscription-urls.txt').read_text(), end='')
        if action == 'reset':
            print(f'{name} 的旧链接已失效，需要用新地址重新导入；其他人不受影响。')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    for name in ('plan', 'install'):
        item = sub.add_parser(name)
        item.add_argument('--settings', required=True)
        if name == 'install':
            item.add_argument('--confirm-new-server', required=True)
            item.add_argument('--host-firewall', choices=('auto', 'check'), default='auto',
                              help='auto: 保守新增 UFW 放行规则；check: 仅检查。均不修改云安全组')
    item = sub.add_parser('status')
    item.add_argument('--json', action='store_true', help=argparse.SUPPRESS)
    item = sub.add_parser('repair-fq', help='Repair a reviewed skipped FIFO without restarting proxy services')
    item.add_argument('--confirm-server', required=True)
    item.add_argument('--adopt-default-fifo', action='store_true',
                      help='Explicitly adopt a lone default-options nonzero-handle FIFO; never custom queues')
    item = sub.add_parser('urls')
    item.add_argument('--transport', choices=('primary', 'all', 'http', 'https'), default='primary',
                      help='主入口默认优先 HTTPS；all 显示所有已启用传输方式')
    item.add_argument('--client', choices=('all', 'clash', 'mac', 'windows', 'android', 'shadowrocket'),
                      default='all',
                      help='clash = raw subscription links; mac/windows = Clash Verge Rev; '
                           'android = Clash Meta for Android; shadowrocket = iOS')
    item.add_argument('--user', default='', help='多人共用时指定用户名')
    item.add_argument('--qr', action='store_true', help='在终端显示二维码，手机直接扫码导入')
    item.add_argument('--json', action='store_true', help=argparse.SUPPRESS)
    item = sub.add_parser('bandwidth', help='调整套餐带宽，或某个订阅链接的上限；多人模式即时生效')
    target = item.add_mutually_exclusive_group(required=True)
    target.add_argument('--set', help='总带宽，如 30Mbps、1Gbps')
    target.add_argument('--user', help='多人模式：要调整上限的订阅用户')
    item.add_argument('--cap', help='与 --user 一起用：该链接最高速率，如 20Mbps；none 表示不限（可用满总带宽）')
    item.add_argument('--confirm-server', required=True)
    item = sub.add_parser('traffic', help='调整每月流量上限、计量方式和超额动作')
    item.add_argument('--quota', help='如 800（即 800GB）、512GB、1TB；0 表示不设置')
    item.add_argument('--metering', choices=('both', 'outbound'))
    item.add_argument('--on-exceed', choices=('warn', 'stop'))
    item.add_argument('--confirm-server', required=True)
    item = sub.add_parser('users', help='多人模式：查看、增加、删除、重置链接，设置速度上限、流量额度和到期日')
    item.add_argument('action', nargs='?', default='list', choices=('list', 'add', 'remove', 'reset', 'set'))
    item.add_argument('name', nargs='?', default='')
    item.add_argument('--cap', help='速度上限，如 20Mbps；none 表示不限')
    item.add_argument('--quota', help='每月流量额度，如 100（即 100GB）、1TB；0 表示不限。超出后只停这一个链接')
    item.add_argument('--expire', help='到期日，如 2026-12-31（当天结束后停用）；none 表示不限')
    item.add_argument('--confirm-server')
    item = sub.add_parser('unlock', help='检测这台服务器的 IP 能否使用 Claude、ChatGPT、TikTok、YouTube')
    item.add_argument('--json', action='store_true', help=argparse.SUPPRESS)
    sub.add_parser('endpoints', help=argparse.SUPPRESS)
    item = sub.add_parser('options', help='开关端口跳跃、备用协议（TUIC、AnyTLS）和 Hy2 混淆')
    item.add_argument('--hopping', choices=('on', 'off'))
    item.add_argument('--protocols', help='tuic,anytls 中任选，none 表示只用 Hy2 + VLESS')
    item.add_argument('--obfs', choices=('on', 'off'))
    item.add_argument('--confirm-server', required=True)
    item = sub.add_parser('upgrade', help='用当前这份代码升级已安装的服务器；密钥和订阅地址不变')
    item.add_argument('--confirm-server', required=True)
    item = sub.add_parser('rollback')
    item.add_argument('--automatic', action='store_true', help=argparse.SUPPRESS)
    item = sub.add_parser('verify')
    args = parser.parse_args(argv)
    if args.command in ('plan', 'install'):
        cfg = settings(json.loads(Path(args.settings).read_text()))
        if args.command == 'plan':
            print(dump({'operation': 'PLAN ONLY — no changes', 'target': cfg['server_ip'],
                        'supported_os': 'Ubuntu 24.04 amd64/arm64 with systemd', 'sing_box_version': VERSION,
                        'cloud_security_group': cloud_ports(cfg),
                        'port_hopping': cfg['port_hopping'], 'extra_protocols': cfg['extra_protocols'],
                        'owned_roots': [str(CONFIG), str(PROGRAM), str(STATE)],
                        'network': network.PARAMETERS,
                        'qdisc_policy': ('per-URL HTB fair share on an untouched default root; custom queues left alone'
                                         if cfg['fair_share'] else 'replace only default pfifo_fast; leave custom queues'),
                        'users': cfg['users'] or ['(single)'], 'fair_share': cfg['fair_share'],
                        'fair_share_rate_mbps': shape_mbps(cfg) or None,
                        'monthly_quota': format_gb(cfg['traffic']['total_bytes']) if cfg['traffic']['total_bytes'] else None,
                        'quota_metering': cfg['traffic']['metering'], 'on_exceed': cfg['traffic']['on_exceed'],
                        'fresh_host_required': True, 'firewall_changes': 'install: safe UFW additions only; no cloud changes',
                        'cloud_firewall': 'unverified; manual cloud-console confirmation required', 'client_changes': False,
                        'subscription_transports': [x['scheme'] for x in subscription_endpoints(cfg)]}))
        else:
            def interrupted(signum, frame):
                raise KeyboardInterrupt('Installation interrupted')
            signal.signal(signal.SIGTERM, interrupted)
            signal.signal(signal.SIGHUP, on_hangup)
            with exclusive_operation():
                install(cfg, args.confirm_new_server, args.host_firewall)
    elif args.command == 'rollback':
        signal.signal(signal.SIGHUP, on_hangup)
        with exclusive_operation():
            rollback(args.automatic)
    elif args.command == 'repair-fq':
        with exclusive_operation():
            s = load_state()
            if s['status'] != 'installed' or args.confirm_server != s['server_ip']:
                raise RuntimeError('Confirm the installed server public IPv4 before repair')
            network.repair_fq(STATE / 'network.json', args.adopt_default_fifo)
            print(dump(network.verify_health(STATE / 'network.json')))
    elif args.command == 'bandwidth':
        with exclusive_operation():
            if bool(args.user) != bool(args.cap):
                raise RuntimeError('--user 和 --cap 需要一起使用')
            change_bandwidth(args.set, args.confirm_server, args.user or '', args.cap)
    elif args.command == 'traffic':
        if args.quota is None and not args.metering and not args.on_exceed:
            raise RuntimeError('请至少指定 --quota、--metering 或 --on-exceed')
        with exclusive_operation():
            change_traffic(args.confirm_server, args.quota, args.metering, args.on_exceed)
    elif args.command == 'users':
        if args.action == 'list':
            print_users(settings(json.loads((CONFIG / 'settings.json').read_text())))
            return
        if not args.confirm_server:
            raise RuntimeError('修改链接需要 --confirm-server <服务器公网IP>')
        if args.action == 'set' and args.cap is None and args.quota is None and args.expire is None:
            raise RuntimeError('users set 需要 --cap、--quota 或 --expire')
        with exclusive_operation():
            change_users(args.confirm_server, args.action, args.name, args.cap, args.quota, args.expire)
    elif args.command == 'options':
        if args.hopping is None and args.protocols is None and args.obfs is None:
            raise RuntimeError('请指定 --hopping、--protocols 或 --obfs')
        protocols = None
        if args.protocols is not None:
            protocols = [] if args.protocols in ('none', '') else [x.strip().lower() for x in args.protocols.split(',')]
        with exclusive_operation():
            change_options(args.confirm_server, None if args.hopping is None else args.hopping == 'on',
                           protocols, None if args.obfs is None else args.obfs == 'on')
    elif args.command == 'unlock':
        import unlock
        if args.json:
            print(json.dumps(unlock.report(), ensure_ascii=False))
        else:
            unlock.main()
    elif args.command == 'endpoints':
        # Machine-readable, for remote.sh's outside-in check; prints private URLs.
        cfg = settings(json.loads((CONFIG / 'settings.json').read_text()))
        first = account_dir(cfg['users'][0] if cfg['users'] else '')
        url = (STATE / 'generated' / first / 'subscription-urls.txt').read_text().splitlines()[0].split(': ', 1)[1]
        layout = port_layout(cfg).values()
        print(json.dumps({'server_ip': cfg['server_ip'], 'proxy_ports': proxy_ports(cfg),
                          'alt_ports': [x['alt'] for x in layout] if 'anytls' in cfg['extra_protocols'] else [],
                          'tuic_ports': [x['alt'] for x in layout] if 'tuic' in cfg['extra_protocols'] else [],
                          'hop_ranges': [list(x['hop']) for x in layout] if cfg['port_hopping'] else [],
                          'subscription_ports': [x['port'] for x in subscription_endpoints(cfg)],
                          'subscription_url': url, 'speedtest_url': url + '-speedtest'}))
    elif args.command == 'upgrade':
        signal.signal(signal.SIGHUP, on_hangup)
        with exclusive_operation():
            upgrade(args.confirm_server)
    elif args.command == 'verify':
        state = load_state()
        verify(state)
        cfg = settings(json.loads((CONFIG / 'settings.json').read_text()))
        hostfirewall.display(hostfirewall.inspect(cfg))
        print('Local verification passed; not an external client/throughput test.')
    elif args.command == 'status' and args.json:
        print(json.dumps(status_report(), ensure_ascii=False))
    elif args.command == 'status':
        s = load_state()
        print(dump(dict({k: s[k] for k in ('status', 'server_ip', 'created_at')},
                        kit_version=s.get('kit_version', '0.5.9 或更早'), sing_box=sing_box_version() or 'unknown')))
        for name in SERVICES:
            print(name + ': ' + run(['systemctl', 'is-active', name], check=False).stdout.strip())
        print(dump({'network': network.health(STATE / 'network.json')}))
        print(dump({'host_firewall': s.get('host_firewall', {}).get('report', 'not recorded'),
                    'cloud_firewall': 'unverified'}))
        usage = STATE / 'traffic.json'
        print(dump({'monthly_traffic': traffic_summary(json.loads(usage.read_text())) if usage.is_file()
                    else 'not measured yet (vnStat needs a few minutes)'}))
        cfg = settings(json.loads((CONFIG / 'settings.json').read_text()))
        if cfg['users']:
            print('\n各链接（本月用量为客户端上传 + 下载）：')
            print_users(cfg)
    elif args.command == 'urls':
        if load_state()['status'] != 'installed':
            raise RuntimeError('Deployment is not committed; subscription URLs are not ready')
        if args.json:
            print(json.dumps(url_report(args.user), ensure_ascii=False))
            return
        users = settings(json.loads((CONFIG / 'settings.json').read_text()))['users']
        user = args.user
        if users and user not in users:
            raise RuntimeError('多人模式请用 --user 指定其中一人：' + '、'.join(users))
        if user and not users:
            raise RuntimeError('此服务器是单人模式，不需要 --user')
        generated = STATE / 'generated' / account_dir(user)
        print('Private subscription URLs — do not publish or include in shared logs:')
        if args.transport != 'primary':
            show_transports(args.transport, args.client, user)
            return
        manifest = (generated / 'subscription-urls.txt').read_text()
        if args.client in ('all', 'clash'):
            print(manifest, end='')
        if args.client in ('all', 'mac', 'windows', 'android'):
            # Derived from the committed manifest, so older deployments work too.
            rows = manifest.splitlines()
            if not rows or not rows[0].startswith('normal: '):
                raise RuntimeError('Subscription URL manifest is malformed; do not guess URLs')
            base = rows[0].split(': ', 1)[1]
            label = json.loads((CONFIG / 'settings.json').read_text()).get('node_prefix', 'LA')
            if args.client in ('all', 'mac', 'windows'):
                print('\n' + desktop_guide_text(base, label, user), end='')
            if args.client in ('all', 'android'):
                print('\n' + android_guide_text(base, label, user), end='')
        if args.client in ('all', 'shadowrocket'):
            print()
            guide = generated / 'shadowrocket-import.txt'
            extra = generated / 'client-urls.txt'
            if guide.is_file():
                print(guide.read_text(), end='')
            elif extra.is_file():
                print(extra.read_text(), end='')
                print('首页导入节点订阅；配置页下载 .conf 并使用；全局路由选“配置”。')
            else:
                raise RuntimeError('No Shadowrocket links in this deployment; do not guess URLs')
        if args.qr:
            show_qr(manifest.splitlines()[0].split(': ', 1)[1], args.client, user)


if __name__ == '__main__':
    try:
        main()
    except (Exception, KeyboardInterrupt) as exc:
        print('ERROR: ' + str(exc), file=sys.stderr)
        sys.exit(1)
