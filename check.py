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
    assert len(server['inbounds'][0]['users']) == len(server['inbounds'][1]['users']) == len(users)
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
            server['inbounds'][1]['tls']['certificate_path'] = str(src / 'hy2.crt')
            server['inbounds'][1]['tls']['key_path'] = str(src / 'hy2.key')
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
    assert hy['password'] == server['inbounds'][1]['users'][index]['password']
    assert hy['obfs-password'] == server['inbounds'][1]['obfs']['password']
    assert vl['uuid'] == server['inbounds'][0]['users'][index]['uuid']
    assert server['inbounds'][1]['users'][index].get('name', '') == name
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
