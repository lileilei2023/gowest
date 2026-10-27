"""Outside-in check, run on YOUR computer: can the internet reach the server?

The server cannot tell whether a cloud security group lets traffic in; only a
connection from outside can. TCP ports are probed directly and one
subscription is downloaded. UDP (Hy2) gives no answer to a probe, so it is
reported as unverifiable rather than guessed.
"""
import json
import socket
import sys
import urllib.request


def tcp(ip, port, timeout=5):
    try:
        socket.create_connection((ip, port), timeout=timeout).close()
        return True
    except OSError:
        return False


def check(info):
    """info: the server's `endpoints` JSON. Returns [{'name', 'ok', 'text'}]."""
    ip, rows = info['server_ip'], []
    for port in info['proxy_ports']:
        ok = tcp(ip, port)
        rows.append({'name': f'TCP {port}', 'ok': ok, 'text': '代理端口（VLESS）可连接' if ok
                     else '连不上：检查云安全组和本机防火墙'})
    for port in info.get('alt_ports', []):
        ok = tcp(ip, port)
        rows.append({'name': f'TCP {port}', 'ok': ok, 'text': '备用协议端口（AnyTLS）可连接' if ok
                     else '连不上：备用协议不可用，其他协议不受影响；检查安全组'})
    for port in info['subscription_ports']:
        ok = tcp(ip, port)
        rows.append({'name': f'TCP {port}', 'ok': ok, 'text': '订阅端口可连接' if ok
                     else '连不上：客户端无法更新订阅'})
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(info['subscription_url'], timeout=8) as response:
            rows.append({'name': '订阅下载', 'ok': True, 'text': f'成功（HTTP {response.status}）'})
    except Exception as error:
        rows.append({'name': '订阅下载', 'ok': False, 'text': '失败：' + type(error).__name__})
    udp = info['proxy_ports'] + info.get('tuic_ports', [])
    udp = [str(p) for p in udp] + [f'{a}-{b}' for a, b in info.get('hop_ranges', [])]
    rows.append({'name': 'UDP ' + '、'.join(udp), 'ok': None,
                 'text': 'Hy2 / TUIC 无法从外部探测；请确认安全组也放行了这些 UDP 端口。'
                         'UDP 不通时客户端会自动改走 VLESS'})
    return rows


def main(argv):
    rows = check(json.loads(argv[1]))
    for row in rows:
        mark = {True: '✓', False: '✗', None: '·'}[row['ok']]
        print(f"  {mark} {row['name']}：{row['text']}")
    return 0 if all(row['ok'] is not False for row in rows) else 1


if __name__ == '__main__':
    sys.exit(main(sys.argv))
