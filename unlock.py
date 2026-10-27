"""Can this server's exit IP use Claude, ChatGPT, TikTok and YouTube?

Run on the server: proxied traffic leaves from this same address, so what the
sites answer here is what every client sees. The signals are public heuristics
(redirect targets, region fields in the page) that a site may change at any
time, so results are a strong hint, not a guarantee. Read-only: nothing is
installed or changed.
"""
import json
import re
import subprocess

UA = ('Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/124.0 Safari/537.36')


def fetch(url, follow=False):
    """(status, final or redirect URL, body). Status 0 means no answer."""
    args = ['curl', '--silent', '--compressed', '--max-time', '12', '--noproxy', '*', '--user-agent', UA,
            '--write-out', '\n%{http_code} %{url_effective} %{redirect_url}', url]
    if follow:
        args.insert(1, '--location')
    try:
        out = subprocess.run(args, capture_output=True, text=True, timeout=20).stdout
    except (OSError, subprocess.SubprocessError):
        return 0, '', ''
    body, _, tail = out.rpartition('\n')
    parts = tail.split(' ')
    status = int(parts[0]) if parts and parts[0].isdigit() else 0
    where = parts[2] if len(parts) > 2 and parts[2] else (parts[1] if len(parts) > 1 else '')
    return status, where, body


def ip_info():
    status, _, body = fetch('https://ipinfo.io/json')
    try:
        data = json.loads(body) if status == 200 else {}
    except ValueError:
        data = {}
    return {'ip': data.get('ip', '?'), 'country': data.get('country', '?'),
            'org': data.get('org', '?'), 'city': data.get('city', '?')}


def claude():
    status, where, _ = fetch('https://claude.ai/')
    if status == 0:
        return 'fail', '无法连接'
    if 'unavailable-in-region' in where:
        return 'fail', '所在地区不支持'
    if status == 403:
        return 'warn', '被拒绝（403），IP 可能被风控'
    return 'ok', '可用'


def chatgpt():
    _, _, rules = fetch('https://api.openai.com/compliance/cookie_requirements')
    status, _, page = fetch('https://ios.chat.openai.com/')
    if 'unsupported_country' in rules:
        return 'fail', '所在地区不支持'
    if status == 0 and not rules:
        return 'fail', '无法连接'
    if 'VPN' in page:
        return 'warn', '网页可用，App 会提示 VPN（IP 被识别为代理）'
    return 'ok', '可用'


def tiktok():
    status, _, body = fetch('https://www.tiktok.com/', follow=True)
    if status == 0:
        return 'fail', '无法连接'
    match = re.search(r'"region":"([A-Z]{2})"', body)
    if match:
        return 'ok', '可用，识别地区 ' + match.group(1)
    return 'warn', f'能打开（HTTP {status}），但没识别到地区，可能受限'


def youtube():
    status, where, body = fetch('https://www.youtube.com/premium', follow=True)
    if status == 0:
        return 'fail', '无法连接'
    if 'google.cn' in where:
        return 'fail', '被识别为中国大陆'
    match = re.search(r'"countryCode":"([A-Z]{2})"', body) or re.search(r'"GL":"([A-Z]{2})"', body)
    region = match.group(1) if match else '?'
    if 'Premium is not available in your country' in body:
        return 'warn', f'可用，地区 {region}，但 Premium 不支持'
    return 'ok', '可用，地区 ' + region


CHECKS = (('Claude', claude), ('ChatGPT', chatgpt), ('TikTok', tiktok), ('YouTube', youtube))
MARK = {'ok': '✓', 'warn': '!', 'fail': '✗'}


def report():
    return {'ip': ip_info(), 'checks': [dict(zip(('name', 'level', 'text'), (name, *check())))
                                        for name, check in CHECKS]}


def main():
    info = ip_info()
    print(f"出口 IP：{info['ip']}  国家/地区：{info['country']}  城市：{info['city']}  运营商：{info['org']}")
    results = {}
    for name, check in CHECKS:
        level, text = check()
        results[name] = level
        print(f'  {MARK[level]} {name:<8} {text}')
    print('说明：检测基于各网站公开的返回特征，网站调整后可能失准；结果代表这台服务器出口，所有客户端都相同。')
    if results['Claude'] != 'ok' or results['ChatGPT'] != 'ok':
        print('提示：AI 服务受限时，常见做法是换一个 IP 更"干净"的服务器，或让 AI 流量经 Cloudflare WARP 出站。')
    return results


if __name__ == '__main__':
    main()
