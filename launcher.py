#!/usr/bin/env python3
"""Chinese deployment entry point; configuration-only init also works on macOS."""
import argparse
from decimal import Decimal, localcontext
import ipaddress
import json
import os
from pathlib import Path
import platform
import re
import subprocess
import sys
import time

sys.dont_write_bytecode = True
import render
import autoconfig
import kit_runtime

ROOT = Path(__file__).resolve().parent
MANAGEMENT = {'plan', 'install', 'status', 'verify', 'urls', 'rollback', 'repair-fq', 'bandwidth', 'traffic',
              'upgrade', 'users', 'unlock', 'endpoints'}


def build_settings(server_ip, up, down, mode, host='', cert='', key='', tailnet='',
                   accept_http_risk=False, subscription_port=None, node_prefix='LA', http_port=80,
                   users=(), quota='', metering='both', on_exceed='warn', user_caps=None,
                   extra_protocols=('tuic', 'anytls'), port_hopping=True, hy2_obfs=True):
    ip = ipaddress.IPv4Address(server_ip)
    if not ip.is_global:
        raise ValueError('请填写新服务器的真实公网 IPv4，不要使用示例/内网地址。')
    if mode not in ('http', 'https', 'both'):
        raise ValueError('订阅方式必须为 http / https / both')
    subscription = {'scheme': 'https' if mode == 'both' else mode, 'host': host or str(ip),
                    'port': (80 if mode == 'http' else 8443) if subscription_port is None else subscription_port}
    if mode == 'http':
        if not accept_http_risk:
            raise ValueError('HTTP 会明文传输订阅凭据，需显式确认风险。')
        subscription['allow_insecure_http'] = True
    else:
        if not host:
            raise ValueError('HTTPS 请填写证书对应的订阅域名。')
        subscription.update(certificate=cert, private_key=key)
        if mode == 'both':
            if not accept_http_risk:
                raise ValueError('同时启用 HTTP 仍会明文暴露同一套凭据，需确认风险。')
            subscription.update(http_port=http_port, allow_insecure_http=True)
    return render.settings({'server_ip': str(ip), 'hy2_up_mbps': up, 'hy2_down_mbps': down,
                            'node_prefix': node_prefix, 'ipv6': True, 'tailnet': tailnet,
                            'hosts': {}, 'subscription': subscription, 'users': list(users),
                            'user_caps': dict(user_caps or {}),
                            'extra_protocols': list(extra_protocols), 'port_hopping': port_hopping,
                            'hy2_obfs': hy2_obfs,
                            'traffic': {'total_bytes': render.parse_bytes(quota), 'expire_epoch': 0,
                                        'metering': metering, 'on_exceed': on_exceed}})


def parse_users(text):
    """Shares chosen by the operator, one subscription URL each.

    '' or '1' -> single; '3' -> user1..user3; 'alice,bob:20Mbps' -> names with an
    optional per-URL cap. Returns (names, caps)."""
    value = str(text or '').strip().replace('，', ',').replace('：', ':').replace(' ', '')
    if value in ('', '1'):
        return [], {}
    if value.isdigit():
        count = int(value)
        if not 2 <= count <= render.MAX_USERS:
            raise ValueError(f'共用份数需在 1-{render.MAX_USERS} 之间')
        return [f'user{i}' for i in range(1, count + 1)], {}
    names, caps = [], {}
    for item in filter(None, value.split(',')):
        name, _, cap = item.partition(':')
        names.append(name.lower())
        if cap:
            caps[name.lower()] = whole_mbps(cap, name + ' 的上限')
    if len(names) == 1:
        raise ValueError('只有一份时直接回车即可；多份请输入份数或至少两个名字')
    return names, caps


def save_settings(config, output):
    path = Path(output).absolute()
    # The renderer's writer uses exclusive creation, so existing files/symlinks are protected.
    render.write(path, render.dump(config), 0o600)
    print('参数已保存：' + str(path))
    print('这里只保存参数；尚未安装软件、生成代理凭据或改变网络。')
    return path


def whole_mbps(text, label):
    """Parse bit-rate input exactly, then emit whole decimal Mbps for Mihomo."""
    if not isinstance(text, str) or len(text) > 64:
        raise ValueError(label + '请填写 30、30Mbps 或 1Gbps')
    match = re.fullmatch(r'([0-9]+(?:\.[0-9]+)?)\s*([mMgG]bps)?', text.strip())
    if not match:
        raise ValueError(label + '请填写 30、30Mbps 或 1Gbps（不是 MB/s 或 GB/s）')
    with localcontext() as context:
        context.prec = 80
        value = Decimal(match[1]) * (1000 if (match[2] or '').lower() == 'gbps' else 1)
        if not 1 <= value <= 100000 or value != value.to_integral_value():
            raise ValueError(label + '换算后须为 1–100000 的整数 Mbps，例如 30Mbps、1Gbps')
    return int(value)


def bandwidth_values(bandwidth=None, up=None, down=None):
    if bandwidth is not None:
        if up is not None or down is not None:
            raise ValueError('--bandwidth 不能与 --up/--down 同时使用')
        value = whole_mbps(bandwidth, '带宽')
        return value, value
    if up is None or down is None:
        raise ValueError('请填写 --bandwidth 30Mbps，或高级用法 --up 10Mbps --down 30Mbps')
    return whole_mbps(up, 'Hy2 上行带宽'), whole_mbps(down, 'Hy2 下行带宽')


def prompt_bandwidth():
    value = prompt('带宽（例如 30Mbps 或 1Gbps；不对称线路输入 advanced）')
    if value.lower() == 'advanced':
        print('方向以客户端为准：上行 = 客户端上传，下行 = 客户端下载。')
        up, down = bandwidth_values(
            up=prompt('客户端上行带宽'), down=prompt('客户端下载带宽'))
    else:
        up, down = bandwidth_values(bandwidth=value)
        print('已将同一数值用于两个方向的 Hy2 目标；这是配置默认值，不代表线路一定对称。')
        print('云套餐标称通常可能仅限制服务器出站；1Gbps 端口也不等于保证跨境吞吐。')
    print(f'Hy2 配置：客户端上行 {up} Mbps，下行 {down} Mbps。不会测速或修改云套餐。')
    return up, down


def prompt(label, default=''):
    value = input(label + (f' [{default}]' if default else '') + '：').strip()
    return value or default


def wizard():
    if not sys.stdin.isatty():
        raise ValueError('交互部署需要终端。非交互用法请运行当前入口的 --help。')
    print(render.PROJECT_NAME + ' 新机部署向导（Ubuntu 24.04 amd64/arm64）')
    print('先保存参数和展示计划；确认显示的目标 IP 并输入 INSTALL 后才安装。不会连接旧服务器。')
    print('安装同时生成 Clash 订阅、小火箭节点订阅和两份分流配置；手机导入说明含完整链接。')
    output = Path(prompt('参数文件', 'settings.local.json')).absolute()
    if output.exists() or output.is_symlink():
        if prompt('文件已存在，只读复用？输入 yes 确认', 'no') != 'yes':
            print('已停止，原文件未改动。')
            return
        config = render.settings(json.loads(output.read_text()))
    else:
        try:
            detected = autoconfig.detect_public_ipv4()
            print('自动识别公网 IPv4：' + detected['address'] + '（' + detected['source'] + '）')
            address = prompt('回车使用此 IP；不正确时可手动修改', detected['address'])
        except ValueError as error:
            print(str(error))
            address = prompt('新服务器公网 IPv4')
        prefix = prompt('服务器标签（仅区分节点，例如 LA、DC；各机代理组名称一致）', 'LA')
        up, down = prompt_bandwidth()
        users, caps = parse_users(prompt('共用份数或名字（回车 = 1 份；如 3 或 alice,bob:20Mbps,carol）'))
        if users:
            print(f'{len(users)} 份，每份一个订阅链接和独立端口；忙时按链接均分，闲时可借满（或到该链接上限）。')
        quota = prompt('每月流量上限（如 1TB、512GB，只写数字按 GB；回车 = 不设置）')
        on_exceed = 'warn'
        if render.parse_bytes(quota):
            on_exceed = prompt('超出后：warn = 只提醒，stop = 暂停代理到下月', 'warn')
        stable = prompt('端口跳跃 + 备用协议 TUIC/AnyTLS，晚高峰更稳（回车 = 开启，输入 n = 关闭）', 'y').lower() != 'n'
        print('所有方式都生成电脑、Android、小火箭配置，无需逐端选择。')
        print('1 = HTTPS（需域名证书）；2 = HTTP（明文风险）；3 或 1,2 = 同时提供 HTTPS 和 HTTP。')
        choice = prompt('订阅方式（默认 HTTP）', '2')
        choice = choice.replace(' ', '').replace('，', ',')
        if choice not in ('1', '2', '3', '1,2', '2,1'):
            raise ValueError('请选择 1、2 或 3（也可输入 1,2）。')
        mode = {'1': 'https', '2': 'http'}.get(choice, 'both')
        tls = mode != 'http'
        host = prompt('订阅域名（DNS 需指向此服务器）') if tls else address
        cert = prompt('完整证书的绝对路径') if tls else ''
        key = prompt('证书私钥的绝对路径') if tls else ''
        accepted = mode == 'https' or prompt('确认 HTTP 会明文传输同一套节点凭据，请输入 HTTP', '') == 'HTTP'
        tailnet = prompt('可选：自己的 xxx.ts.net 域名，留空跳过')
        config = build_settings(address, up, down, mode, host, cert, key, tailnet, accepted,
                                node_prefix=prefix, users=users, quota=quota, on_exceed=on_exceed,
                                user_caps=caps, extra_protocols=render.EXTRA_PROTOCOLS if stable else (),
                                port_hopping=stable)
        save_settings(config, output)
    subprocess.run(kit_runtime.command(['plan', '--settings', str(output)]), check=True)
    if platform.system() != 'Linux' or os.geteuid() != 0:
        print('当前不是 Linux root 环境，只完成了参数准备。请将对应架构的 Linux 安装包和参数传到新机后运行：')
        entry = './gowest' if kit_runtime.compiled() else 'bash deploy.sh'
        print('sudo ' + entry + ' install --settings <参数文件路径> --confirm-new-server ' + config['server_ip'])
        return
    print('安装会检查本机防火墙并尝试安全新增 UFW 规则；不清空规则、不改变 SSH、不自动启停防火墙。')
    print('云安全组需要云平台授权；当前无法自动验证，必须放行：' + '、'.join(render.cloud_ports(config)) + '。')
    confirmation = prompt('当前目标公网 IPv4 为 ' + config['server_ip'] + '；输入 INSTALL 开始安装，留空取消')
    if confirmation != 'INSTALL':
        print('未确认安装，服务器配置未改变。')
        return
    dispatch(['install', '--settings', str(output), '--confirm-new-server', config['server_ip']])


def protocol_list(text):
    value = (text or '').strip().lower()
    return [] if value in ('', 'none') else [x.strip() for x in value.split(',')]


def quick_install(a):
    """Install straight away with defaults; every choice can be changed later."""
    if platform.system() != 'Linux' or os.geteuid() != 0:
        raise ValueError('quick 需要在新服务器上以 root 运行（在自己电脑上用 ./remote.sh root@<IP> quick 100Mbps）')
    up, down = bandwidth_values(a.bandwidth)
    names, caps = parse_users(a.users)
    detected = autoconfig.detect_public_ipv4()
    address = detected['address']
    print(f'公网 IPv4：{address}（{detected["source"]}）；带宽 {down} Mbps；'
          + (f'{len(names)} 个订阅链接' if names else '1 份') + '；订阅走 HTTP（明文，勿在不可信网络里更新订阅）。')
    config = build_settings(address, up, down, 'http', accept_http_risk=True, node_prefix=a.node_prefix,
                            users=names, quota=a.monthly_quota, on_exceed=a.on_exceed, user_caps=caps,
                            extra_protocols=protocol_list(a.protocols), port_hopping=not a.no_port_hopping,
                            hy2_obfs=not a.no_obfs)
    output = ROOT / 'settings.local.json'
    if output.exists() or output.is_symlink():
        output = ROOT / time.strftime('settings-%Y%m%d-%H%M%S.json')
    save_settings(config, output)
    print('云安全组请放行：' + '、'.join(render.cloud_ports(config))
          + ('（多人时建议直接放行 TCP/UDP 20001-27000，以后加人不用再改）' if names else '') + '。')
    dispatch(['install', '--settings', str(output), '--confirm-new-server', address])


def dispatch(arguments):
    if kit_runtime.compiled():
        import vpn
        return vpn.main(arguments)
    # exec replaces this process without flushing; over SSH (no TTY) stdout is
    # block-buffered, so earlier messages would silently disappear.
    sys.stdout.flush()
    sys.stderr.flush()
    os.execv(sys.executable, [sys.executable, '-B', str(ROOT / 'vpn.py')] + arguments)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv == ['wizard']:
        wizard()
        return
    if argv[0] in MANAGEMENT:
        dispatch(argv)
        return
    if argv[0] == 'firewall':
        import firewall
        raise SystemExit(firewall.main(argv[1:]))
    if argv[0] == '_boot-fq':
        import network
        return network.main(argv[1:])
    if argv[0] == '_subinfo':
        if len(argv) != 1:
            raise ValueError('_subinfo 不接受参数')
        import subinfo
        return subinfo.main()
    parser = argparse.ArgumentParser(prog='gowest', description=render.PROJECT_NAME + '：双客户端代理部署与配置生成', epilog=(
        '无参数：交互向导；quick 100Mbps：零问答安装。管理命令：plan / install / upgrade / status / verify / urls / users / '
        'unlock / bandwidth / traffic / rollback / repair-fq。'
        'firewall generate / check 离线准备云端模板。'
        '各命令可加 --help；脚本不会自动 SSH，也不会自动提权。'))
    parser.add_argument('--version', action='version', version=render.PROJECT_NAME + ' ' + render.KIT_VERSION)
    commands = parser.add_subparsers(dest='command', required=True)
    init = commands.add_parser('init', help='只创建参数；省略 IP 时联网探测，显式 IP 时不联网')
    init.add_argument('--server-ip', help='省略时仅在目标 Linux 服务器上自动探测公网 IPv4')
    init.add_argument('--node-prefix', default='LA', help='服务器标签，例如 LA、DC；代理组名称统一，不随标签改变')
    init.add_argument('--bandwidth', help='常规用法：只填一次，如 30Mbps、1Gbps 或 30；两方向共用此目标')
    init.add_argument('--up', help='高级：客户端上行目标，与 --down 成对使用，如 10Mbps')
    init.add_argument('--down', help='高级：客户端下载目标，与 --up 成对使用，如 30Mbps')
    method = init.add_mutually_exclusive_group()
    method.add_argument('--http', action='store_true', help='默认方式；仍须 --accept-http-risk')
    method.add_argument('--https', action='store_true')
    method.add_argument('--both', action='store_true', help='HTTPS 主入口，同时提供 HTTP（需风险确认）')
    init.add_argument('--accept-http-risk', action='store_true')
    init.add_argument('--subscription-host', default='')
    init.add_argument('--subscription-port', type=int)
    init.add_argument('--http-port', type=int, default=80, help='--both 的额外 HTTP 端口')
    init.add_argument('--certificate', default='')
    init.add_argument('--private-key', default='')
    init.add_argument('--tailnet', default='')
    init.add_argument('--users', default='', help='共用份数（如 3）或名字，可带上限（alice,bob:20Mbps）；默认 1 份')
    init.add_argument('--monthly-quota', default='', help='每月流量上限，如 1TB、512GB 或 800（按 GB）；默认不设置')
    init.add_argument('--metering', choices=('both', 'outbound'), default='both',
                      help='云套餐计量方式：both = 进+出（默认，偏保守）；outbound = 只计出方向')
    init.add_argument('--on-exceed', choices=('warn', 'stop'), default='warn',
                      help='超出上限：warn 只提醒；stop 暂停代理直到下月或上限调高')
    init.add_argument('--no-port-hopping', action='store_true')
    init.add_argument('--protocols', default='tuic,anytls', help='备用协议：tuic,anytls 中任选；none = 只用 Hy2 + VLESS')
    init.add_argument('--no-obfs', action='store_true')
    init.add_argument('--output', default='settings.local.json')
    quick = commands.add_parser('quick', help='零问答安装：自动识别 IP，HTTP 订阅，其余用默认值；以后都能再改')
    quick.add_argument('bandwidth', help='套餐带宽，如 100、100Mbps、1Gbps')
    quick.add_argument('--users', default='', help='共用份数（如 3）或名字（alice,bob,carol）；默认 1 份')
    quick.add_argument('--monthly-quota', default='', help='整台服务器每月流量上限，如 1TB；默认不设置')
    quick.add_argument('--on-exceed', choices=('warn', 'stop'), default='warn')
    quick.add_argument('--node-prefix', default='LA')
    quick.add_argument('--no-port-hopping', action='store_true', help='不开端口跳跃（安全组只能开少量端口时）')
    quick.add_argument('--protocols', default='tuic,anytls', help='备用协议：tuic,anytls 中任选；none = 只用 Hy2 + VLESS')
    quick.add_argument('--no-obfs', action='store_true', help='Hy2 不加 salamander 混淆')
    commands.add_parser('firewall', help='离线生成/校验防火墙模板；详见 firewall --help')
    offline = commands.add_parser('render', help='离线生成全部客户端配置；不安装、不发布链接')
    offline.add_argument('--settings', required=True)
    offline.add_argument('--output', required=True)
    a = parser.parse_args(argv)
    if a.command == 'quick':
        return quick_install(a)
    if a.command == 'render':
        result = render.render(json.loads(Path(a.settings).read_text()), a.output)
        print('全部客户端配置已生成至私有目录：' + str(result) + '；尚未安装或发布。')
        return
    try:
        up, down = bandwidth_values(a.bandwidth, a.up, a.down)
    except ValueError as error:
        parser.error(str(error))
    mode = 'both' if a.both else ('https' if a.https else 'http')
    if mode != 'https' and not a.accept_http_risk:
        parser.error('默认 HTTP 会明文传输节点凭据；需 --accept-http-risk，或显式选择 --https')
    names, caps = parse_users(a.users)
    address = a.server_ip or autoconfig.detect_public_ipv4()['address']
    config = build_settings(address, up, down, mode,
                            a.subscription_host, a.certificate, a.private_key, a.tailnet,
                            a.accept_http_risk, a.subscription_port, a.node_prefix, a.http_port,
                            names, a.monthly_quota, a.metering, a.on_exceed, caps,
                            protocol_list(a.protocols), not a.no_port_hopping, not a.no_obfs)
    save_settings(config, a.output)


if __name__ == '__main__':
    try:
        main()
    except (Exception, KeyboardInterrupt) as e:
        print('操作停止：' + str(e), file=sys.stderr)
        sys.exit(1)
