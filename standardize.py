#!/usr/bin/env python3
"""Offline migration of a two-node client profile; never contacts a server.

Preserves every transport/authentication field except the display names.
Only writes a new private output directory. Does not install or rotate keys.
"""
import argparse
import copy
import ipaddress
import json
from pathlib import Path
import re

import yaml
import render


def standardize(source, prefix, strict=False):
    nodes = source.get('proxies', [])
    if len(nodes) != 2 or sorted(p.get('type', '') for p in nodes) != ['hysteria2', 'vless']:
        raise ValueError('Expected exactly one Hysteria2 and one VLESS node')
    hy = next(p for p in nodes if p['type'] == 'hysteria2')
    vl = next(p for p in nodes if p['type'] == 'vless')
    if hy['server'] != vl['server']:
        raise ValueError('Refusing cross-server fallback')
    address = str(ipaddress.IPv4Address(hy['server']))
    if any(p.get('dialer-proxy') for p in nodes):
        raise ValueError('Chained transport requires a separate migration review')
    if not re.fullmatch(r'[A-Za-z][A-Za-z0-9-]{0,24}', prefix):
        raise ValueError('Invalid server label')
    def bandwidth(value):
        match = re.fullmatch(r'([0-9]+(?:\.[0-9]+)?)\s*(?:Mbps)?', str(value))
        if not match:
            raise ValueError('Expected explicit Mbps bandwidth')
        return float(match[1])
    policies = source.get('dns', {}).get('nameserver-policy', {})
    tailnets = [key[2:] for key in policies if key.startswith('+.') and
                key.endswith('.ts.net') and key != '+.ts.net']
    if len(tailnets) > 1:
        raise ValueError('Multiple tailnets need explicit review')
    s = render.settings({
        'server_ip': address, 'node_prefix': prefix,
        'hy2_up_mbps': bandwidth(hy['up']), 'hy2_down_mbps': bandwidth(hy['down']),
        'ipv6': source.get('ipv6', True), 'hosts': source.get('hosts', {}),
        'tailnet': tailnets[0] if tailnets else '',
        'reality_server_name': vl.get('servername', 'www.microsoft.com'),
        'hy2_server_name': hy.get('sni', 'bing.com'),
        'subscription': {'scheme': 'http', 'host': address, 'port': 80,
                         'allow_insecure_http': True}})
    k = {'uuid': vl['uuid'], 'reality_public_key': vl['reality-opts']['public-key'],
         'short_id': vl['reality-opts']['short-id'], 'hy2_password': hy['password'],
         'obfs_password': hy['obfs-password'], 'certificate_fingerprint': hy['fingerprint']}
    result = render.client_config(s, k, strict)
    result['proxies'] = []
    for original in nodes:
        node = copy.deepcopy(original)
        node['name'] = prefix + ('-Hy2' if node['type'] == 'hysteria2' else '-VLESS')
        result['proxies'].append(node)
    # Preserve user's existing DNS overrides, except old scene/proxy routing.
    # Tailnet/local mappings are essential for management and are not telemetry.
    for key, value in policies.items():
        if key.endswith(('.ts.net', '.local', '.lan')) or key == '+.ts.net':
            result['dns']['nameserver-policy'][key] = copy.deepcopy(value)
    return result


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--input', required=True)
    ap.add_argument('--node-prefix', required=True)
    ap.add_argument('--output', required=True)
    args = ap.parse_args()
    source = yaml.safe_load(Path(args.input).read_text())
    out = Path(args.output)
    normal = standardize(source, args.node_prefix)
    strict = standardize(source, args.node_prefix, True)
    out.mkdir(mode=0o700, parents=False, exist_ok=False)
    for name, cfg in [('normal.yaml', normal), ('strict.yaml', strict)]:
        render.write(out / name, render.dump_yaml(cfg))
    print(json.dumps({'output': str(out), 'files': ['normal.yaml', 'strict.yaml'],
                      'credentials_rotated': False, 'network_changed': False}))


if __name__ == '__main__':
    main()
