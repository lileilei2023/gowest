#!/usr/bin/env python3
"""Validate generated files using isolated copies and supplied core binaries."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import base64
import re
from urllib.parse import urlsplit, parse_qs, unquote

import yaml
import render


def check(directory, sing_box=None, mihomo=None, geodata=None):
    src = Path(directory).resolve()
    server = json.loads((src / 'server.json').read_text())
    users = json.loads((src / 'users.json').read_text()) if (src / 'users.json').is_file() else ['']
    fair = server['inbounds'][1]['ignore_client_bandwidth']
    assert len(server['inbounds']) == 2 * len(users), 'Expected one VLESS + Hy2 pair per user'
    assert len({i['listen_port'] for i in server['inbounds']}) == len(users), 'Each user needs its own port'
    folders = []
    for index, name in enumerate(users):
        folder = src / render.account_dir(name)
        check_account(src, folder, server, index, name, fair)
        folders.append(folder)
    tokens = [(f / 'subscription-urls.txt').read_text().split('/sub/')[1].split()[0] for f in folders]
    assert len(set(tokens)) == len(tokens), 'Subscription tokens must be unique per user'
    with tempfile.TemporaryDirectory(prefix='la-vpn-check-') as tmp:
        root = Path(tmp)
        os.chmod(root, 0o700)
        if sing_box:
            for inbound in server['inbounds']:
                if inbound['type'] == 'hysteria2':
                    inbound['tls']['certificate_path'] = str(src / 'hy2.crt')
                    inbound['tls']['key_path'] = str(src / 'hy2.key')
            candidate = root / 'server.json'
            candidate.write_text(json.dumps(server))
            p = subprocess.run([str(Path(sing_box).resolve()), 'check', '-c', str(candidate)],
                               cwd=root, capture_output=True, timeout=30)
            if p.returncode:
                raise RuntimeError('sing-box validation failed: ' + p.stderr.decode(errors='replace')[:1500])
        if mihomo:
            if not geodata:
                raise ValueError('Supply --geodata: copies are used, never the active data directory')
            for name in ('Country.mmdb', 'geosite.dat', 'geoip.dat', 'ASN.mmdb'):
                p = Path(geodata) / name
                if p.is_file():
                    shutil.copyfile(p, root / name)
            for name in ('normal.yaml', 'strict.yaml'):
                shutil.copyfile(folders[0] / name, root / name)
                p = subprocess.run([str(Path(mihomo).resolve()), '-t', '-d', str(root), '-f', str(root / name)],
                                   cwd=root, capture_output=True, timeout=60)
                if p.returncode:
                    raise RuntimeError('Mihomo rejected ' + name + ': ' + p.stdout.decode(errors='replace')[:1500])
    return {'cross_config_checks': 'passed', 'users': len(users), 'fair_share': fair, 'shadowrocket_projection': 'passed (not iOS runtime)',
            'sing_box': 'passed' if sing_box else 'not run',
            'mihomo': 'passed' if mihomo else 'not run', 'network_changes': False}


def check_account(src, folder, server, index, name, fair):
    normal = yaml.safe_load((folder / 'normal.yaml').read_text())
    strict = yaml.safe_load((folder / 'strict.yaml').read_text())
    hy = next(p for p in normal['proxies'] if p['type'] == 'hysteria2')
    vl = next(p for p in normal['proxies'] if p['type'] == 'vless')
    assert normal['proxies'] == strict['proxies'], 'Node mismatch'
    suffix = '-' + name if name else ''
    vin = next(i for i in server['inbounds'] if i['tag'] == 'vless-in' + suffix)
    hin = next(i for i in server['inbounds'] if i['tag'] == 'hy2-in' + suffix)
    assert len(vin['users']) == len(hin['users']) == 1, 'One credential per inbound pair'
    assert hy['password'] == hin['users'][0]['password']
    assert hy['obfs-password'] == hin['obfs']['password']
    assert vl['uuid'] == vin['users'][0]['uuid']
    assert hin['users'][0].get('name', '') == name
    assert hy['port'] == vl['port'] == hin['listen_port'] == vin['listen_port'], 'Client port must match its inbound'
    if fair:
        # Shared server: no client Brutal targets, the server commands BBR.
        assert 'up' not in hy and 'down' not in hy, 'Fair-share clients must not carry Brutal bandwidth'
    else:
        # Same pattern as Mihomo common/utils/mbps.go; a mismatch silently disables Brutal.
        for key in ('up', 'down'):
            assert re.fullmatch(r'(\d+)\s*([KMGT]?)([Bb])ps', str(hy[key])), 'Hy2 ' + key + ' not parseable by Mihomo'
    uris = base64.b64decode((folder / 'nodes.txt').read_text().strip(), validate=True).decode().splitlines()
    assert len(uris) == 2, 'Expected exactly two native node URIs'
    hy_uri, vl_uri = map(urlsplit, uris)
    assert hy_uri.scheme == 'hysteria2' and vl_uri.scheme == 'vless'
    assert hy_uri.hostname == hy['server'] and vl_uri.hostname == vl['server']
    assert hy_uri.port == vl_uri.port == hy['port'], 'Native URI port must match the profile'
    assert unquote(hy_uri.username) == hy['password'] and vl_uri.username == vl['uuid']
    assert parse_qs(hy_uri.query)['obfs-password'] == [hy['obfs-password']]
    assert parse_qs(hy_uri.query)['pinSHA256'] == [hy['fingerprint']]
    assert parse_qs(vl_uri.query)['pbk'] == [vl['reality-opts']['public-key']]
    assert parse_qs(vl_uri.query)['sid'] == [vl['reality-opts']['short-id']]
    cert = subprocess.run(['openssl', 'x509', '-in', str(src / 'hy2.crt'), '-outform', 'DER'],
                          check=True, capture_output=True).stdout
    assert hashlib.sha256(cert).hexdigest() == hy['fingerprint'], 'Certificate pin mismatch'
    for client in (normal, strict):
        assert client['tun']['enable'] is False, 'Renderer must not enable TUN'
        for group in client['proxy-groups']:
            if group['type'] == 'fallback':
                assert group['proxies'] == [hy['name'], vl['name']]
                assert 'DIRECT' not in group['proxies']
        assert not any(k.startswith('external-controller') or k == 'secret' for k in client)
    assert not any(x in strict['rules'] for x in ['GEOSITE,cn,DIRECT', 'GEOIP,CN,DIRECT'])
    for name, cfg in [('shadowrocket.conf', normal), ('shadowrocket-us.conf', strict)]:
        render.validate_shadowrocket_config((folder / name).read_text(), cfg)


if __name__ == '__main__':
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--generated', required=True)
    ap.add_argument('--sing-box')
    ap.add_argument('--mihomo')
    ap.add_argument('--geodata')
    a = ap.parse_args()
    print(json.dumps(check(a.generated, a.sing_box, a.mihomo, a.geodata), indent=2))
