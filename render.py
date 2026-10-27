#!/usr/bin/env python3
"""Offline renderer. No active Clash files or server paths are changed."""
import argparse
import base64
import copy
import datetime
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import uuid
from decimal import Decimal
from urllib.parse import quote, urlencode

import routing

DOMAINS = routing.CLAUDE
PROJECT_NAME = 'gowest'
KIT_VERSION = '0.7.0'
SELECT_GROUP = '出口选择'
AUTO_GROUP = '自动回退'
# Tailscale control plane stays direct on every Mihomo client:
# macOS (daemon/app/extension), Windows (.exe names), Android (package name for CMFA).
TS_PROCESSES = ['tailscaled', 'Tailscale', 'io.tailscale.ipn.macsys.network-extension',
                'tailscaled.exe', 'tailscale-ipn.exe', 'com.tailscale.ipn']
# Desktop client pinned by the clients/ installers; profiles use the same Mihomo YAML.
DESKTOP_CLIENT = 'Clash Verge Rev 2.5.8'
USER_NAME = r'[a-z][a-z0-9-]{0,15}'
MAX_USERS = 32
METERING = ('both', 'outbound')
SUBSCRIPTION_FILES = {'': 'normal.yaml', '-us': 'strict.yaml', '-nodes': 'nodes.txt',
                      '-shadowrocket.conf': 'shadowrocket.conf',
                      '-us-shadowrocket.conf': 'shadowrocket-us.conf'}


def write(path, data, mode=0o600):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('x', encoding='utf-8') as f:
        os.chmod(path, mode)
        f.write(data)


def dump(data):
    # Internal/server files stay JSON. Client subscriptions use block YAML.
    return json.dumps(data, ensure_ascii=False, indent=2) + '\n'


def dump_yaml(data):
    try:
        import yaml
    except ImportError as exc:
        raise RuntimeError('Client rendering requires PyYAML (Ubuntu: python3-yaml)') from exc
    return yaml.safe_dump(data, allow_unicode=True, sort_keys=False,
                          default_flow_style=False, width=120)


def domain(value):
    if not isinstance(value, str) or len(value) > 253 or not re.fullmatch(
            r'[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?', value):
        raise ValueError('Invalid DNS name')
    if any(not label or len(label) > 63 or label.startswith('-') or label.endswith('-')
           for label in value.split('.')):
        raise ValueError('Invalid DNS label')
    return value.lower()


def settings(raw):
    allowed = {'server_ip', 'node_prefix', 'hy2_up_mbps', 'hy2_down_mbps', 'ipv6',
               'tailnet', 'hosts', 'reality_server_name', 'hy2_server_name',
               'subscription', 'traffic', 'users', 'fair_share', 'user_caps', 'user_port_base',
               'user_ports', 'user_quota', 'user_expire'}
    if set(raw) - allowed:
        raise ValueError('Unknown settings fields: ' + ', '.join(sorted(set(raw) - allowed)))
    s = copy.deepcopy(raw)
    # A literal IPv4 endpoint makes TUN exclusion and proxy DNS bootstrap deterministic.
    s['server_ip'] = str(ipaddress.IPv4Address(s['server_ip']))
    s.setdefault('node_prefix', 'LA')
    if not re.fullmatch(r'[A-Za-z][A-Za-z0-9-]{0,24}', s['node_prefix']):
        raise ValueError('Invalid node_prefix')
    for key in ['hy2_up_mbps', 'hy2_down_mbps']:
        value = s.get(key)
        # Mihomo parses Hy2 bandwidth with ^(\d+)\s*([KMGT]?)([Bb])ps$; "10.0 Mbps"
        # silently becomes 0 (no Brutal). Only whole Mbps values are accepted.
        if type(value) is float and value.is_integer():
            value = int(value)
        if type(value) is not int or not 0 < value <= 100000:
            raise ValueError(key + ' must be an explicit whole number of Mbps')
        s[key] = value
    s.setdefault('ipv6', True)
    if type(s['ipv6']) is not bool:
        raise ValueError('ipv6 must be boolean')
    s.setdefault('tailnet', '')
    if s['tailnet']:
        s['tailnet'] = domain(s['tailnet'])
        if not s['tailnet'].endswith('.ts.net'):
            raise ValueError('tailnet must be a specific .ts.net domain')
    s.setdefault('hosts', {})
    s['hosts'] = {domain(k): str(ipaddress.ip_address(v)) for k, v in s['hosts'].items()}
    for key, default in [('reality_server_name', 'www.microsoft.com'), ('hy2_server_name', 'bing.com')]:
        s[key] = domain(s.get(key, default))
    sub = s['subscription']
    if set(sub) - {'scheme', 'host', 'port', 'certificate', 'private_key', 'allow_insecure_http', 'http_port'}:
        raise ValueError('Unknown subscription setting')
    if sub['scheme'] not in ('http', 'https'):
        raise ValueError('Subscription scheme must be http or https')
    sub['host'] = domain(sub.get('host', s['server_ip']))
    if type(sub.get('port')) is not int or not 1 <= sub['port'] <= 65535 or sub['port'] in (22, 443):
        raise ValueError('Subscription port must differ from SSH 22 and proxy TCP 443')
    if sub['scheme'] == 'http' and sub.get('allow_insecure_http') is not True:
        raise ValueError('HTTP exposes credentials; explicit allow_insecure_http=true is required')
    if 'http_port' in sub:
        if (sub['scheme'] != 'https' or type(sub['http_port']) is not int
                or not 1 <= sub['http_port'] <= 65535 or sub['http_port'] in (22, 443, sub['port'])):
            raise ValueError('Additional HTTP requires HTTPS primary and a distinct valid port')
        if sub.get('allow_insecure_http') is not True:
            raise ValueError('Dual HTTP/HTTPS exposes the same credentials over HTTP; explicit opt-in required')
    if sub['scheme'] == 'https':
        for key in ('certificate', 'private_key'):
            if not isinstance(sub.get(key), str) or not Path(sub[key]).is_absolute():
                raise ValueError('HTTPS requires absolute certificate/private_key paths')
    s.setdefault('traffic', {})
    if set(s['traffic']) - {'total_bytes', 'expire_epoch', 'metering', 'on_exceed'}:
        raise ValueError('Unknown traffic setting')
    # total_bytes is the server's monthly quota (0 = not declared); it is shared
    # by all users because the cloud plan meters the whole server.
    for key in ('total_bytes', 'expire_epoch'):
        value = s['traffic'].setdefault(key, 0)
        if type(value) is not int or value < 0:
            raise ValueError('Invalid traffic display value')
    if s['traffic'].setdefault('metering', 'both') not in METERING:
        raise ValueError('traffic.metering must be both or outbound')
    if s['traffic'].setdefault('on_exceed', 'warn') not in ('warn', 'stop'):
        raise ValueError('traffic.on_exceed must be warn or stop')
    if s['traffic']['on_exceed'] == 'stop' and not s['traffic']['total_bytes']:
        raise ValueError('on_exceed=stop requires a monthly quota (total_bytes)')
    users = s.setdefault('users', [])
    if (type(users) is not list or len(users) > MAX_USERS or len(set(users)) != len(users)
            or not all(isinstance(u, str) and re.fullmatch(USER_NAME, u) for u in users)):
        raise ValueError(f'users must be 1-{MAX_USERS} unique names matching {USER_NAME}')
    # Several people sharing one server: each subscription URL is one share with
    # its own port; HTB guarantees bandwidth/N per URL and lends idle capacity.
    s.setdefault('fair_share', len(users) > 1)
    if type(s['fair_share']) is not bool or (s['fair_share'] and not users):
        raise ValueError('fair_share needs named users')
    caps = s.setdefault('user_caps', {})
    if type(caps) is not dict or set(caps) - set(users) or any(
            type(v) is not int or not 0 < v <= 100000 for v in caps.values()):
        raise ValueError('user_caps must map existing users to whole Mbps')
    base = s.setdefault('user_port_base', 20000)
    # Below the Linux ephemeral range (32768+) so outgoing sockets never hold them.
    if type(base) is not int or not 1024 <= base or base + len(users) > 32767:
        raise ValueError('user_port_base must keep per-user ports within 1025-32767')
    # Ports are pinned per name once users change after install, so removing
    # one URL never moves anyone else's port (and breaks their subscription).
    pinned = s.setdefault('user_ports', {})
    if type(pinned) is not dict or (pinned and set(pinned) != set(users)) or any(
            type(v) is not int or not 1025 <= v <= 32767 for v in pinned.values()):
        raise ValueError('user_ports must give every user a port within 1025-32767')
    if len(set(proxy_ports(s))) != len(proxy_ports(s)):
        raise ValueError('Per-user proxy ports must be unique')
    quota = s.setdefault('user_quota', {})
    if type(quota) is not dict or set(quota) - set(users) or any(
            type(v) is not int or v <= 0 for v in quota.values()):
        raise ValueError('user_quota must map existing users to a positive byte count')
    expire = s.setdefault('user_expire', {})
    if type(expire) is not dict or set(expire) - set(users):
        raise ValueError('user_expire must map existing users to a date')
    for value in expire.values():
        parse_date(value)
    reserved = {22, 443} | {x['port'] for x in subscription_endpoints(s)}
    if users and reserved & set(proxy_ports(s)):
        raise ValueError('Per-user proxy ports collide with SSH, 443 or subscription ports')
    return s


def parse_date(text):
    """'2026-12-31' or '2026/12/31' -> '2026-12-31'; the URL works through that whole day."""
    value = str(text).strip().replace('/', '-')
    try:
        return datetime.date.fromisoformat(value).isoformat()
    except ValueError:
        raise ValueError('到期日请写成 2026-12-31 这样的日期') from None


def proxy_ports(s):
    """One port per subscription URL in shared mode (TCP VLESS + UDP Hy2)."""
    if not s['users']:
        return [443]
    if s.get('user_ports'):
        return [s['user_ports'][name] for name in s['users']]
    return [s['user_port_base'] + i for i in range(1, len(s['users']) + 1)]


def free_port(s):
    """Lowest unused per-user port above the base, for a newly added URL."""
    used = set(proxy_ports(s)) | {22, 443} | {x['port'] for x in subscription_endpoints(s)}
    port = s['user_port_base'] + 1
    while port in used:
        port += 1
    if port > 32767:
        raise ValueError('No free per-user port below 32768')
    return port


def parse_bytes(text):
    """'1TB', '512GB', '800', '1.5TiB' -> bytes. A bare number is GB, like a bare
    bandwidth is Mbps. GB/TB are decimal (as most cloud plans bill); GiB/TiB are
    binary. Empty or 0 means no quota."""
    value = str(text).strip().replace(' ', '')
    if value in ('', '0'):
        return 0
    match = re.fullmatch(r'(\d+(?:\.\d+)?)(GB|TB|GiB|TiB)?', value, re.I)
    if not match:
        raise ValueError('流量上限请写成 800（即 800GB）、512GB、1TB、1.5TB 或 1TiB 这样的形式')
    units = {'gb': 10**9, 'tb': 10**12, 'gib': 2**30, 'tib': 2**40}
    result = int(Decimal(match.group(1)) * units[(match.group(2) or 'GB').lower()])
    if result <= 0:
        raise ValueError('流量上限必须大于 0')
    return result


def format_gb(value):
    """Bytes -> '312.4 GB' (decimal, matching how quotas are entered)."""
    gb = round(value / 10**9, 1)
    return (f'{gb:.0f}' if gb == int(gb) else f'{gb:.1f}') + ' GB'


def shape_mbps(s):
    """Shared rate: 95% of the plan so queues form here, not in the cloud shaper."""
    return max(1, int(s['hy2_down_mbps'] * 95 // 100)) if s['fair_share'] else 0


def share_plan(s):
    """What network.py builds: one HTB class per subscription URL."""
    if not s['fair_share']:
        return None
    total = shape_mbps(s)
    return {'rate_mbps': total, 'users': [
        {'name': name, 'port': port, 'cap_mbps': min(s['user_caps'].get(name, total), total)}
        for name, port in zip(s['users'], proxy_ports(s))]}


def accounts(s, k):
    """Per-user credentials merged with the shared server keys.

    Without named users this is the legacy single account (flat file layout)."""
    if not s['users']:
        return [dict(k, name='', port=443)]
    return [dict(k, port=port, **user) for user, port in zip(k['users'], proxy_ports(s))]


def new_credentials():
    """One URL's own secrets: proxy identity plus an unguessable subscription token."""
    return {'uuid': str(uuid.uuid4()), 'hy2_password': secrets.token_urlsafe(32),
            'subscription_token': secrets.token_hex(24)}


def make_secrets(out, s):
    def run(args, data=None):
        return subprocess.run(['openssl'] + args, input=data, check=True,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout
    private_der = run(['genpkey', '-algorithm', 'X25519', '-outform', 'DER'])
    public_der = run(['pkey', '-inform', 'DER', '-pubout', '-outform', 'DER'], private_der)
    if len(private_der) != 48 or len(public_der) != 44:
        raise ValueError('Unexpected X25519 DER format; refusing to guess key bytes')
    b64 = lambda v: base64.urlsafe_b64encode(v).decode().rstrip('=')
    run(['req', '-x509', '-newkey', 'ec', '-pkeyopt', 'ec_paramgen_curve:P-256',
         '-nodes', '-days', '3650', '-subj', '/CN=' + s['hy2_server_name'],
         '-addext', 'subjectAltName=DNS:' + s['hy2_server_name'],
         '-keyout', str(out / 'hy2.key'), '-out', str(out / 'hy2.crt')])
    os.chmod(out / 'hy2.key', 0o600)
    der = run(['x509', '-in', str(out / 'hy2.crt'), '-outform', 'DER'])
    k = {'reality_private_key': b64(private_der[-32:]),
         'reality_public_key': b64(public_der[-32:]), 'short_id': secrets.token_hex(8),
         'obfs_password': secrets.token_urlsafe(32), 'certificate_fingerprint': hashlib.sha256(der).hexdigest()}
    user = new_credentials
    if s['users']:
        # Each person gets independent credentials and a private subscription
        # token, so one link can be revoked or leak without exposing others.
        k['users'] = [dict(user(), name=name) for name in s['users']]
    else:
        k.update(user())
    return k


def server_config(s, k):
    if not s['users']:
        return {'log': {'level': 'warn'}, 'inbounds': inbound_pair(s, k, accounts(s, k), 443, ''),
                'outbounds': [{'type': 'direct'}]}
    # Shared mode: each URL owns a port, which is how the egress queue tells
    # users apart (packets to clients carry no user identity otherwise).
    inbounds = []
    for a in accounts(s, k):
        inbounds += inbound_pair(s, k, [a], a['port'], '-' + a['name'])
    return {'log': {'level': 'warn'}, 'inbounds': inbounds, 'outbounds': [{'type': 'direct'}]}


def inbound_pair(s, k, users, port, suffix):
    named = lambda a, extra: dict({'name': a['name']} if a['name'] else {}, **extra)
    return [
        {'type': 'vless', 'tag': 'vless-in' + suffix, 'listen': '::', 'listen_port': port,
         'users': [named(a, {'uuid': a['uuid'], 'flow': 'xtls-rprx-vision'}) for a in users],
         'tls': {'enabled': True, 'server_name': s['reality_server_name'],
                 'reality': {'enabled': True,
                             'handshake': {'server': s['reality_server_name'], 'server_port': 443},
                             'private_key': k['reality_private_key'], 'short_id': [k['short_id']]}}},
        {'type': 'hysteria2', 'tag': 'hy2-in' + suffix, 'listen': '::', 'listen_port': port,
         'users': [named(a, {'password': a['hy2_password']}) for a in users],
         'obfs': {'type': 'salamander', 'password': k['obfs_password']},
         # Shared servers: clients are told to use BBR instead of fixed-rate
         # Brutal, which would ignore the fair-share queue and starve others.
         'ignore_client_bandwidth': s['fair_share'],
         'tls': {'enabled': True, 'alpn': ['h3'], 'certificate_path': '/etc/la-vpn/hy2.crt',
                 'key_path': '/etc/la-vpn/hy2.key'}}]


def client_config(s, k, strict=False):
    prefix = s['node_prefix']
    hy, vl = prefix + '-Hy2', prefix + '-VLESS'
    auto, select = AUTO_GROUP, SELECT_GROUP
    group = lambda name: {'name': name, 'type': 'fallback', 'proxies': [hy, vl],
                          'url': 'https://www.gstatic.com/generate_204', 'interval': 60,
                          'timeout': 5000, 'lazy': False, 'max-failed-times': 2, 'expected-status': 204}
    policies = routing.dns_policies()
    if s['tailnet']:
        policies['+.' + s['tailnet']] = '100.100.100.100'
    if not strict:
        # Only mainland domains use mainland DoH; everything else (including the
        # resolution triggered by GEOIP,CN) goes through proxied DoH, so foreign
        # browsing domains are not disclosed to domestic resolvers.
        policies['geosite:cn'] = ['https://dns.alidns.com/dns-query', 'https://doh.pub/dns-query']
    rules = ['PROCESS-NAME,' + p + ',DIRECT' for p in TS_PROCESSES]
    rules += ['IP-CIDR,100.64.0.0/10,DIRECT,no-resolve', 'IP-CIDR6,fd7a:115c:a1e0::/48,DIRECT,no-resolve']
    rules += ['DOMAIN-SUFFIX,' + d + ',DIRECT' for d in ('ts.net', 'tailscale.com', 'tailscale.io')]
    rules += routing.scene_rules()
    rules += ['GEOSITE,private,DIRECT', 'GEOIP,private,DIRECT,no-resolve']
    if not strict:
        rules += ['GEOSITE,cn,DIRECT', 'GEOIP,CN,DIRECT']
    rules += ['MATCH,' + select]
    return {'mixed-port': 7897, 'allow-lan': False, 'mode': 'rule', 'log-level': 'info',
            'ipv6': s['ipv6'], 'hosts': s['hosts'], 'find-process-mode': 'strict',
            'profile': {'store-selected': True},
            'tun': {'enable': False, 'stack': 'gvisor', 'auto-route': True,
                    'auto-detect-interface': True, 'strict-route': False,
                    'dns-hijack': ['any:53', 'tcp://any:53'],
                    'route-exclude-address': ['100.64.0.0/10', 'fd7a:115c:a1e0::/48', s['server_ip'] + '/32']},
            'dns': {'enable': True, 'use-hosts': True, 'ipv6': s['ipv6'], 'respect-rules': True,
                    'enhanced-mode': 'fake-ip', 'fake-ip-range': '198.18.0.1/16',
                    'fake-ip-filter-mode': 'blacklist',
                    'fake-ip-filter': ['+.ts.net', '+.lan', '+.local', 'geosite:cn'],
                    'nameserver-policy': policies,
                    'default-nameserver': ['223.5.5.5', '119.29.29.29'],
                    'proxy-server-nameserver': ['223.5.5.5', '119.29.29.29'],
                    'nameserver': ['https://1.1.1.1/dns-query#' + select, 'https://8.8.8.8/dns-query#' + select]},
            'sniffer': {'enable': True, 'force-dns-mapping': True, 'parse-pure-ip': True,
                        'override-destination': True,
                        'skip-domain': ['+.ts.net', '+.tailscale.com', '+.tailscale.io'],
                        'sniff': {'HTTP': {'ports': [80, '8080-8880'], 'override-destination': True},
                                  'TLS': {'ports': [443, 8443]}, 'QUIC': {'ports': [443, 8443]}}},
            'proxies': [
                {'name': vl, 'type': 'vless', 'server': s['server_ip'], 'port': k.get('port', 443), 'uuid': k['uuid'],
                 'udp': True, 'tls': True, 'network': 'tcp', 'flow': 'xtls-rprx-vision',
                 'servername': s['reality_server_name'], 'client-fingerprint': 'chrome',
                 'reality-opts': {'public-key': k['reality_public_key'], 'short-id': k['short_id']}},
                {'name': hy, 'type': 'hysteria2', 'server': s['server_ip'], 'port': k.get('port', 443),
                 'password': k['hy2_password'], 'obfs': 'salamander', 'obfs-password': k['obfs_password'],
                 'sni': s['hy2_server_name'], 'alpn': ['h3'], 'skip-cert-verify': True,
                 'fingerprint': k['certificate_fingerprint'],
                 **({} if s['fair_share'] else
                    {'up': f"{int(s['hy2_up_mbps'])} Mbps", 'down': f"{int(s['hy2_down_mbps'])} Mbps"})}],
            'proxy-groups': [{'name': select, 'type': 'select',
                              'proxies': [auto, hy, vl] + ([] if strict else ['DIRECT']),
                              'default-selected': auto}, group(auto),
                             {'name': 'AI', 'type': 'select', 'proxies': [auto, hy, vl]},
                             {'name': 'Claude', 'type': 'select', 'proxies': ['AI', hy, vl]},
                             {'name': 'YouTube', 'type': 'select', 'proxies': [auto, hy, vl]},
                             {'name': 'TikTok', 'type': 'select', 'proxies': [auto, hy, vl]}], 'rules': rules}


def node_subscription(s, k):
    """Native URI list, base64 encoded, for node-only mobile subscriptions.

    Hysteria2 URI bandwidth extensions are not portable. Clients importing this
    list may need to set up/down manually; the Clash YAML retains both targets.
    """
    hy = urlencode({'sni': s['hy2_server_name'], 'insecure': '1', 'obfs': 'salamander',
                    'obfs-password': k['obfs_password'], 'pinSHA256': k['certificate_fingerprint']})
    vl = urlencode({'encryption': 'none', 'security': 'reality', 'type': 'tcp',
                    'sni': s['reality_server_name'], 'fp': 'chrome',
                    'pbk': k['reality_public_key'], 'sid': k['short_id'], 'flow': 'xtls-rprx-vision'})
    port = k.get('port', 443)
    lines = [f"hysteria2://{quote(k['hy2_password'], safe='')}@{s['server_ip']}:{port}/?{hy}#{s['node_prefix']}-Hy2",
             f"vless://{k['uuid']}@{s['server_ip']}:{port}?{vl}#{s['node_prefix']}-VLESS"]
    return base64.b64encode(('\n'.join(lines) + '\n').encode()).decode() + '\n'


def shadowrocket_config(s, k, strict=False):
    """Rule config referencing the exact node names imported via -nodes.

    No MITM/scripts, no broad subscription regex, no automatic DIRECT fallback.
    """
    cfg = client_config(s, k, strict)
    hy = s['node_prefix'] + '-Hy2'
    # Pin DNS to this server's concrete nodes, never the app-wide current node.
    dns = 'https://1.1.1.1/dns-query#proxy=' + quote(hy, safe='')
    fallback_dns = 'https://1.1.1.1/dns-query#proxy=' + quote(s['node_prefix'] + '-VLESS', safe='')
    lines = ['# ' + PROJECT_NAME + ' ' + KIT_VERSION + '; import -nodes first; keep node names unchanged.',
             '# Profile: ' + s['node_prefix'] + (' full-public proxy' if strict else ' daily split'),
             '# App global routing must be Configuration; select and enable THIS config.',
             '# Home node selection does not override these explicitly named policies.',
             '# App-wide fallback and Wi-Fi/cellular Scenes are separate app settings.',
             '# DNS primary/fallback use two protocols on the SAME server.',
             '[General]', 'dns-server = ' + dns, 'fallback-dns-server = ' + fallback_dns,
             'ipv6 = ' + str(s['ipv6']).lower(), 'prefer-ipv6 = false',
             'dns-direct-system = false', 'dns-direct-fallback-proxy = false',
             'udp-policy-not-supported-behaviour = REJECT', 'block-quic = always-allow',
             'skip-proxy = localhost, *.local, 10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16',
             'tun-excluded-routes = 100.64.0.0/10, fd7a:115c:a1e0::/48, ' + s['server_ip'] + '/32']
    if not strict:
        lines += ['direct-dns-server = https://dns.alidns.com/dns-query']
    lines += ['', '[Proxy Group]']
    for group in cfg['proxy-groups']:
        members = ', '.join(group['proxies'])
        line = group['name'] + ' = ' + group['type'] + ', ' + members
        if group['type'] == 'fallback':
            line += ', url=https://www.gstatic.com/generate_204, interval=60'
        elif group['type'] == 'select':
            line += ', policy-select-name=' + group['proxies'][0]
        lines.append(line)
    lines += ['', '[Rule]']
    for rule in cfg['rules']:
        if rule.startswith(('PROCESS-NAME,', 'GEOSITE,', 'GEOIP,private,')):
            continue
        if rule.startswith('MATCH,'):
            # Explicit local exceptions; no broad GEOIP private portability dependency.
            lines += ['IP-CIDR,' + net + ',DIRECT,no-resolve' for net in
                      ('10.0.0.0/8', '127.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16', '169.254.0.0/16')]
            lines += ['IP-CIDR6,fc00::/7,DIRECT,no-resolve', 'IP-CIDR6,fe80::/10,DIRECT,no-resolve']
            rule = rule.replace('MATCH,', 'FINAL,', 1)
        lines.append(rule)
    if s['hosts']:
        lines += ['', '[Host]'] + [name + ' = ' + ip for name, ip in s['hosts'].items()]
    text = '\n'.join(lines) + '\n'
    validate_shadowrocket_config(text, cfg)
    return text


def validate_shadowrocket_config(text, cfg):
    """Check our native projection, not a substitute for the iOS app parser.

    Reject accidental PROXY/default-node or cross-subscription references.
    Clash-only rule types are deliberately omitted by this projection.
    """
    def require(condition, message):
        if not condition:
            raise ValueError('Shadowrocket projection: ' + message)
    sections = {}
    current = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith('#'):
            continue
        if line.startswith('[') and line.endswith(']'):
            current = line[1:-1]
            require(current not in sections, 'duplicate section')
            sections[current] = []
        else:
            require(current is not None, 'missing section')
            sections[current].append(line)
    require(set(sections) <= {'General', 'Proxy Group', 'Rule', 'Host'}, 'unsupported section')
    nodes = {p['name'] for p in cfg['proxies']}
    expected_groups = {g['name']: g for g in cfg['proxy-groups']}
    allowed = nodes | set(expected_groups) | {'DIRECT'}
    groups = {}
    for line in sections.get('Proxy Group', []):
        name, value = line.split('=', 1)
        name = name.strip()
        require(name not in groups and name in expected_groups, 'unexpected group')
        fields = [x.strip() for x in value.split(',')]
        members = [x for x in fields[1:] if '=' not in x]
        options = dict(x.split('=', 1) for x in fields[1:] if '=' in x)
        expected = expected_groups[name]
        require(fields[0] == expected['type'] and members == expected['proxies'], 'group members changed')
        require(set(members) <= allowed, 'unknown or cross-server member')
        if fields[0] == 'select':
            require(options.get('policy-select-name') == members[0], 'default selection changed')
        else:
            require(fields[0] == 'fallback' and set(members) == nodes, 'unsafe fallback')
        require('policy-regex-filter' not in options, 'all-subscription filter forbidden')
        groups[name] = fields
    require(set(groups) == set(expected_groups), 'missing group')
    rules = sections.get('Rule', [])
    require(bool(rules) and rules[-1] == 'FINAL,' + SELECT_GROUP, 'missing explicit final policy')
    for rule in rules:
        fields = rule.split(',')
        require(fields[0] not in {'GEOSITE', 'MATCH', 'PROCESS-NAME'}, 'Clash-only rule')
        target = fields[-2] if fields[-1] == 'no-resolve' else fields[-1]
        require(target in allowed, 'unknown or app-default rule target')
    require(('GEOIP,CN,DIRECT' in rules) == ('GEOIP,CN,DIRECT' in cfg['rules']), 'daily/strict mismatch')
    general = dict(line.split(' = ', 1) for line in sections.get('General', []))
    hy, vl = (next(p['name'] for p in cfg['proxies'] if p['type'] == t) for t in ('hysteria2', 'vless'))
    for key, node in [('dns-server', hy), ('fallback-dns-server', vl)]:
        require(general.get(key) == 'https://1.1.1.1/dns-query#proxy=' + quote(node, safe=''), 'DNS not pinned to same server')
    require(general.get('udp-policy-not-supported-behaviour') == 'REJECT', 'unsupported UDP must not go direct')


def subscription_endpoints(s):
    sub = s['subscription']
    primary = {key: sub[key] for key in ('scheme', 'host', 'port')}
    return [primary] + ([dict(primary, scheme='http', port=sub['http_port'])]
                        if 'http_port' in sub else [])


def endpoint_base(sub, token):
    port = '' if (sub['scheme'], sub['port']) in [('http', 80), ('https', 443)] else ':' + str(sub['port'])
    return f"{sub['scheme']}://{sub['host']}{port}/sub/{token}"


def subscription_base(s, k):
    return endpoint_base(s['subscription'], k['subscription_token'])


def transport_manifest(s, k):
    return [dict(endpoint, base=endpoint_base(endpoint, k['subscription_token']))
            for endpoint in subscription_endpoints(s)]


def shadowrocket_import_guide(s, k):
    """Private, server-specific copy/paste instructions; never served by nginx."""
    return shadowrocket_guide_text(subscription_base(s, k), s['node_prefix'], k.get('name', ''))


def owner(user):
    return f'（用户 {user}）' if user else ''


def shadowrocket_guide_text(base, label, user=''):
    return f'''{label} 小火箭导入说明{owner(user)}（{PROJECT_NAME} {KIT_VERSION}）— 含私密订阅入口，请勿公开

1. 节点订阅： 首页 → ＋ → 类型 Subscribe → 粘贴以下完整网址 → 保存。
{base}-nodes
应看到 {label}-Hy2 和 {label}-VLESS。请勿单独改名。

2. 日常分流（YouTube / TikTok / Claude 代理；命中国内规则的连接直连）：
{base}-shadowrocket.conf
配置 → ＋ → 粘贴完整网址 → 下载 → 点新文件 → 使用配置（出现勾选）。

3. 全公网代理（可选；国内普通公网连接也走代理）：
{base}-us-shadowrocket.conf
导入位置同第 2 步；两份配置一次只启用一份。

开始连接：首页选 {label}-Hy2 → 全局路由选“配置” → 打开连接。
出口选择 / AI / YouTube / TikTok → 自动回退；Claude → AI。
自动回退仅在本机 Hy2 / VLESS 之间按健康检查结果切换，不跨订阅、不回落直连。
首页节点供默认策略使用；本配置的显式分组不随首页点选改为另一台服务器。

全局路由：配置 = 按规则；代理 = 普通流量按首页节点/所选分组代理；
直连 = 不经代理；场景 = 根据 Wi-Fi/蜂窝等条件切换路由、配置及节点。
日常版、全公网版都应选“配置”。场景不是 Claude / YouTube 的应用分类。
应用级“启用回退”默认建议关闭；它与配置内“自动回退”不同。
VPN 开关、全局路由、首页节点、场景和应用级回退不能靠导入本文件代替手动确认。

二维码/跳转无反应时，复制上面的完整网址到指定入口；不依赖 URL Scheme。
只有首页节点、或正在用 default.conf 且代理分组为 0，不代表已启用上述分流。
严格版也不是系统级断网保护，不能保证零泄漏或账号安全。
'''


def profile_names(label, user=''):
    stem = PROJECT_NAME + '-' + label + ('-' + user if user else '')
    return {'normal': stem + '-日常', 'strict': stem + '-全公网'}


def install_link(scheme, base, name):
    """clash://install-config deep link. name must precede url: Clash Verge Rev
    takes everything after 'url=' as the subscription address."""
    return f"{scheme}://install-config?name={quote(name, safe='')}&url={quote(base, safe='')}"


def desktop_guide_text(base, label, user=''):
    names = profile_names(label, user)
    return f'''{label} Mac / Windows 导入说明{owner(user)}（{PROJECT_NAME} {KIT_VERSION}，{DESKTOP_CLIENT}）— 含私密订阅入口，请勿公开

日常分流订阅（国内直连）：
{base}
全公网订阅（国内普通公网也走代理）：
{base}-us

推荐：运行项目 clients/ 目录里的一键脚本（Mac：clients/macos/gowest-client.sh；
Windows：双击 clients/windows/gowest-client.cmd），按提示粘贴上面的订阅地址。
脚本会检测环境、安装校验过的 Clash Verge Rev，并自动导入；不改系统代理和 TUN。

手动导入：Clash Verge Rev → 订阅 → 粘贴订阅地址 → 导入。
或在本机浏览器打开下面的链接（会唤起 Clash Verge Rev 并自动导入）：
{install_link('clash', base, names['normal'])}
{install_link('clash', base + '-us', names['strict'])}

导入后：在“订阅”页点选 {names['normal']}（或 {names['strict']}）使其生效；
需要时在“设置”里手动打开“系统代理”或“虚拟网卡模式（TUN）”。
不要同时运行 Clash for Windows / ClashX 等其他代理客户端；Clash for Windows 的内核不支持本项目的节点。
'''


def android_guide_text(base, label, user=''):
    names = profile_names(label, user)
    return f'''{label} Android 导入说明{owner(user)}（{PROJECT_NAME} {KIT_VERSION}，Clash Meta for Android）— 含私密订阅入口，请勿公开

在手机浏览器打开下面的链接，会唤起 CMFA 的“新配置”页面并填好地址：
日常分流：{install_link('clashmeta', base, names['normal'])}
全公网：  {install_link('clashmeta', base + '-us', names['strict'])}

链接无反应时手动导入：配置 → ＋ → URL → 名称填 {names['normal']}，
URL 粘贴 {base}（全公网版用 {base}-us）→ 保存。
然后在“配置”里选中它，回首页点“已停止”启动；首次会请求 VPN 权限。
Android 同一时间只能运行一个 VPN：与 Tailscale 等 VPN 应用互斥。
请使用 Clash Meta for Android（mihomo 内核）；旧版 Clash for Android 不支持本项目的节点。
'''


def client_import_guide_text(base, label, user=''):
    """All four clients in one private file; served by nothing, printed by `urls`."""
    return '\n'.join([desktop_guide_text(base, label, user), android_guide_text(base, label, user),
                      shadowrocket_guide_text(base, label, user)])


def client_files(s, k):
    return {'normal.yaml': dump_yaml(client_config(s, k)),
            'strict.yaml': dump_yaml(client_config(s, k, strict=True)),
            'nodes.txt': node_subscription(s, k),
            'shadowrocket.conf': shadowrocket_config(s, k),
            'shadowrocket-us.conf': shadowrocket_config(s, k, strict=True)}


def nginx_config(s, k):
    sub = s['subscription']
    tls = sub['scheme'] == 'https'
    text = '''user www-data;
worker_processes auto;
pid /run/la-vpn-subscription/nginx.pid;
error_log /var/log/la-vpn-subscription/error.log warn;
events { worker_connections 512; }
http {
    access_log off;
    server_tokens off;
    client_body_temp_path /run/la-vpn-subscription/client-body;
    proxy_temp_path /run/la-vpn-subscription/proxy;
    fastcgi_temp_path /run/la-vpn-subscription/fastcgi;
    uwsgi_temp_path /run/la-vpn-subscription/uwsgi;
    scgi_temp_path /run/la-vpn-subscription/scgi;
    server {
'''
    text += f"        listen {sub['port']}{' ssl' if tls else ''};\n        server_name {sub['host']};\n"
    if 'http_port' in sub:
        text += f"        listen {sub['http_port']};\n"
    if tls:
        text += '''        ssl_certificate /etc/la-vpn/subscription-fullchain.pem;
        ssl_certificate_key /etc/la-vpn/subscription-privkey.pem;
        ssl_protocols TLSv1.2 TLSv1.3;
'''
    for a in accounts(s, k):
        folder = a['name'] + '/' if a['name'] else ''
        label = s['node_prefix'] + ('-' + a['name'] if a['name'] else '')
        # Each URL shows its own usage, quota and expiry. The bracket makes the
        # include a glob, so Nginx starts before the timer first writes the file.
        subinfo = f"/etc/la-vpn/subinfo.d/{a['name']}.con[f]" if a['name'] else '/etc/la-vpn/subinfo.conf'
        for suffix, file in SUBSCRIPTION_FILES.items():
            text += f'''        location = /sub/{a['subscription_token']}{suffix} {{
            alias /etc/la-vpn/subscriptions/{folder}{file};
            default_type {'text/yaml' if file.endswith('.yaml') else 'text/plain'};
            include {subinfo};
            add_header profile-update-interval 24 always;
            add_header Content-Disposition 'attachment; filename="{label}-{file}"' always;
            add_header Cache-Control "no-store" always;
        }}
'''
    return text + '        location / { return 404; }\n    }\n}\n'


def account_dir(name):
    """Legacy single user keeps the flat layout; named users get users/<name>/."""
    return Path('users') / name if name else Path('.')


def write_account(target, s, a):
    for name, body in client_files(s, a).items():
        write(target / name, body)
    base = subscription_base(s, a)
    label, user = s['node_prefix'], a['name']
    write(target / 'subscription-urls.txt', 'normal: ' + base + '\nstrict: ' + base + '-us\n')
    write(target / 'client-urls.txt', ''.join(name + ': ' + base + suffix + '\n'
          for suffix, name in SUBSCRIPTION_FILES.items() if suffix not in ('', '-us')))
    write(target / 'shadowrocket-import.txt', shadowrocket_import_guide(s, a))
    write(target / 'client-import.txt', client_import_guide_text(base, label, user))
    write(target / 'transport-urls.json', dump(transport_manifest(s, a)))
    write(target / 'transport-import.txt', '\n'.join(
        item['scheme'].upper() + ' 订阅入口\n' + client_import_guide_text(item['base'], label, user)
        for item in transport_manifest(s, a)))


def render(raw, directory):
    s = settings(raw)
    out = Path(directory).absolute()
    # An exclusive new directory is intentional: never rotate existing keys implicitly.
    out.mkdir(mode=0o700, parents=False, exist_ok=False)
    os.chmod(out, 0o700)
    k = make_secrets(out, s)
    write(out / 'secrets.json', dump(k))
    write(out / 'settings.json', dump(s))
    write(out / 'server.json', dump(server_config(s, k)))
    write(out / 'nginx.conf', nginx_config(s, k))
    write(out / 'subinfo.conf', '# Populated by optional vnStat display timer.\n')
    for a in accounts(s, k):
        write_account(out / account_dir(a['name']), s, a)
    if s['users']:
        write(out / 'users.json', dump(s['users']))
    if s['tailnet']:
        write(out / 'tailnet-resolver.conf', 'nameserver 100.100.100.100\n')
    return out


if __name__ == '__main__':
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--settings', required=True)
    ap.add_argument('--output', required=True)
    args = ap.parse_args()
    result = render(json.loads(Path(args.settings).read_text()), args.output)
    print('Generated private files in ' + str(result) + '; no services or client settings changed.')
