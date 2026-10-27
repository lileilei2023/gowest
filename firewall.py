#!/usr/bin/env python3
"""Offline UCloud IPv4 inbound allow-list generation/checking; never applies rules."""
import argparse
import ipaddress
import json
from pathlib import Path
import re
import sys

import render

FIELDS = {'策略', '协议及端口', '源地址', '优先级', '备注'}
WORLD = '0.0.0.0/0'


def source_cidr(value):
    if not isinstance(value, str) or '/' not in value:
        raise ValueError('源地址必须是明确的 IPv4 CIDR，例如管理出口 IP/32')
    # Do not silently widen a mistyped host address to a larger network.
    return str(ipaddress.IPv4Network(value, strict=True))


def expected_ports(raw, ssh_port=22):
    s = render.settings(raw)
    if type(ssh_port) is not int or not 1 <= ssh_port <= 65535:
        raise ValueError('SSH 端口必须是 1–65535 的整数')
    subscriptions = [x['port'] for x in render.subscription_endpoints(s)]
    proxies = render.proxy_ports(s)
    if ssh_port in proxies + subscriptions:
        raise ValueError('SSH 端口不能与代理端口或订阅端口冲突')
    return ([f'TCP:{ssh_port}'] + [f'TCP:{port}' for port in subscriptions]
            + [f'{proto}:{port}' for port in proxies for proto in ('TCP', 'UDP')] + ['ICMP'])


def rule(protocol, source, note):
    return {'策略': '接受', '协议及端口': protocol, '源地址': source, '优先级': '高', '备注': note}


def generate(raw, ssh_sources=None, ssh_port=22, allow_world_ssh=False):
    ports = expected_ports(raw, ssh_port)
    if not ssh_sources:
        if not allow_world_ssh:
            raise ValueError('请指定 --ssh-source 管理出口CIDR，或显式选择 --allow-world-ssh 临时开放 SSH')
        ssh_sources = [WORLD]
    sources = [source_cidr(x) for x in ssh_sources]
    if WORLD in sources and not allow_world_ssh:
        raise ValueError('SSH 对全网开放需显式指定 --allow-world-ssh')
    rows = [rule(ports[0], source, 'SSH 管理（建议收窄来源）') for source in sources]
    s = render.settings(raw)
    owners = dict(zip(render.proxy_ports(s), s['users'])) if s['users'] else {}
    for port in ports[1:-1]:
        proto, number = port.split(':')
        if port.startswith('TCP:') and int(number) not in render.proxy_ports(s):
            rows.append(rule(port, WORLD, '订阅下载'))
        else:
            who = (' · ' + owners[int(number)]) if int(number) in owners else ''
            rows.append(rule(port, WORLD, ('VLESS REALITY' if proto == 'TCP' else 'Hysteria2') + who))
    rows.append(rule('ICMP', WORLD, '诊断及路径 MTU'))
    result = {'data': rows}
    report = check(raw, result, ssh_port)
    if report['errors']:
        raise ValueError('; '.join(report['errors']))
    if report['world_open_ssh'] and not allow_world_ssh:
        raise ValueError('SSH 来源合计覆盖全网，需显式指定 --allow-world-ssh')
    return result


def check(raw, document, ssh_port=22):
    """Validate this kit's narrow allow-list format, not arbitrary cloud ACL semantics."""
    ports = expected_ports(raw, ssh_port)
    errors, warnings = [], []
    seen, found, ssh_networks = set(), set(), []
    if not isinstance(document, dict) or set(document) != {'data'} or not isinstance(document['data'], list):
        errors.append('模板顶层必须仅含 data 规则数组')
        rows = []
    else:
        rows = document['data']
    for index, row in enumerate(rows, 1):
        label = f'规则 {index}: '
        if not isinstance(row, dict) or set(row) != FIELDS:
            errors.append(label + '字段必须与云端导出的中文模板一致')
            continue
        if row['策略'] != '接受' or row['优先级'] != '高' or not isinstance(row['备注'], str):
            errors.append(label + '本工程模板仅支持 接受 / 高优先级 / 文本备注')
            continue
        protocol = row['协议及端口']
        if not isinstance(protocol, str) or not re.fullmatch(r'(?:TCP|UDP):[1-9][0-9]{0,4}|ICMP', protocol):
            errors.append(label + '协议格式应为 TCP:端口、UDP:端口或 ICMP')
            continue
        if protocol not in ports:
            errors.append(label + '非本部署必需的协议/端口（含 RDP 3389 时应移除）')
            continue
        try:
            source = source_cidr(row['源地址'])
        except ValueError:
            errors.append(label + '源地址必须是有效、无主机位的 IPv4 CIDR')
            continue
        identity = (protocol, source)
        if identity in seen:
            errors.append(label + '重复规则')
            continue
        seen.add(identity)
        if protocol == ports[0]:
            ssh_networks.append(ipaddress.IPv4Network(source))
        elif source != WORLD:
            errors.append(label + '本模板要求代理、订阅及 ICMP 使用 0.0.0.0/0；定制收窄需另行审查')
            continue
        found.add(protocol)
    for protocol in ports:
        if protocol not in found:
            errors.append('缺少必要规则: ' + protocol)
    collapsed = list(ipaddress.collapse_addresses(ssh_networks))
    world_open = ipaddress.IPv4Network(WORLD) in collapsed
    if world_open:
        warnings.append('SSH 对全网开放：仅作初始部署便利，稳定后收窄为可信管理出口 CIDR')
    if len(collapsed) < len(ssh_networks) and any(a.overlaps(b) for i, a in enumerate(ssh_networks)
                                               for b in ssh_networks[i+1:]):
        warnings.append('SSH 来源网段重叠，请核对是否确有需要')
    warnings.append('仅离线检查 IPv4 入站白名单；未验证云端导入、默认拒绝、存量规则、IPv6、出站或主机防火墙')
    return {'valid': not errors, 'errors': errors, 'warnings': warnings,
            'required_protocols': ports, 'world_open_ssh': world_open, 'rules_applied': False}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    inspect = commands.add_parser('inspect', help='只读检查服务器本机防火墙；不代表云安全组检查')
    inspect.add_argument('--settings', required=True)
    for name in ('generate', 'check'):
        item = commands.add_parser(name, help='离线生成新模板' if name == 'generate' else '离线核对模板与部署参数')
        item.add_argument('--settings', required=True)
        item.add_argument('--ssh-port', type=int, default=22, help='仅生成/检查规则，不修改 sshd 监听端口')
        if name == 'generate':
            item.add_argument('--ssh-source', action='append', help='可信管理出口 IPv4 CIDR，可重复指定')
            item.add_argument('--allow-world-ssh', action='store_true', help='显式允许初始部署时 SSH 对全网开放')
            item.add_argument('--output', required=True, help='仅新建，不覆盖已有文件或符号链接')
        else:
            item.add_argument('--file', required=True)
    args = parser.parse_args(argv)
    raw = json.loads(Path(args.settings).read_text(encoding='utf-8'))
    if args.command == 'inspect':
        import hostfirewall
        report = hostfirewall.inspect(render.settings(raw))
        hostfirewall.display(report)
        return 0 if report['status'] != 'manual-review' else 1
    if args.command == 'generate':
        document = generate(raw, args.ssh_source, args.ssh_port, args.allow_world_ssh)
        render.write(Path(args.output).absolute(), render.dump(document))
    else:
        document = json.loads(Path(args.file).read_text(encoding='utf-8'))
    report = check(raw, document, args.ssh_port)
    if args.command == 'generate':
        report['output'] = str(Path(args.output).absolute())
    print(render.dump(report), end='')
    return 0 if report['valid'] else 1


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (Exception, KeyboardInterrupt) as exc:
        print('防火墙模板操作停止：' + str(exc), file=sys.stderr)
        sys.exit(1)
