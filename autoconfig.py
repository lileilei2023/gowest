"""Bounded public IPv4 discovery, without proxy settings or cloud credentials."""
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
import ipaddress
import json
import platform
import subprocess
import urllib.request

IP_SOURCES = ('https://api4.ipify.org', 'https://ipv4.icanhazip.com',
              'https://checkip.amazonaws.com')


def public_ipv4(value):
    ip = ipaddress.IPv4Address(value.strip())
    if not ip.is_global:
        raise ValueError('Not a public IPv4')
    return str(ip)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def query_ip(url):
    # Do not mistake a proxy exit address for this server's address.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    with opener.open(url, timeout=4) as response:
        return public_ipv4(response.read(128).decode('ascii').strip())


def local_public_ips():
    try:
        result = subprocess.run(['ip', '-j', '-4', 'addr', 'show', 'scope', 'global'],
                                capture_output=True, text=True, check=True, timeout=5)
        ips = set()
        for interface in json.loads(result.stdout):
            for address in interface.get('addr_info', []):
                try:
                    ips.add(public_ipv4(address['local']))
                except (ValueError, KeyError):
                    pass
        return ips
    except (OSError, ValueError, subprocess.SubprocessError):
        return set()


def detect_public_ipv4():
    if platform.system() != 'Linux':
        raise ValueError('自动识别需在目标 Linux 服务器执行；本地准备参数请明确填写新服务器 IP。')
    def safe_query(url):
        try:
            return query_ip(url)
        except Exception:
            return None
    with ThreadPoolExecutor(max_workers=len(IP_SOURCES)) as executor:
        counts = Counter(ip for ip in executor.map(safe_query, IP_SOURCES) if ip)
    agreed = [ip for ip, count in counts.items() if count >= 2]
    if len(agreed) == 1:
        return {'address': agreed[0], 'source': '至少两个 HTTPS 公网出口查询一致；NAT 入站映射仍需确认'}
    local = local_public_ips()
    if len(local) == 1 and (not counts or set(counts) == local):
        return {'address': next(iter(local)), 'source': '唯一公网 IPv4 网卡地址'}
    raise ValueError('公网 IP 自动识别失败或结果冲突，请人工填写目标服务器公网 IPv4。')
