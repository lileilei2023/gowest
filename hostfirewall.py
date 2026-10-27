"""Narrow, journaled UFW additions. Never enable/disable/flush a firewall."""
import hashlib
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess

from render import subscription_endpoints

UFW_FILES = (Path('/etc/ufw/user.rules'), Path('/etc/ufw/user6.rules'))
CLOUD_WARNING = ('云安全组：未验证（无云平台 API 授权/外部探测）。请在云控制台检查下列入站端口；'
                 '本机放行或监听不代表云端已放行，UDP 无响应也不能证明端口开放。')


def run(args):
    # Terminate the whole subprocess group before a caller attempts rollback.
    import signal
    with subprocess.Popen(args, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          start_new_session=True, env=dict(os.environ, LC_ALL='C')) as process:
        try:
            stdout, stderr = process.communicate(timeout=20)
        except BaseException:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=5)
            raise
    return subprocess.CompletedProcess(args, process.returncode, stdout, stderr)


def inspect_command(args):
    try:
        return run(args)
    except (OSError, subprocess.SubprocessError):
        return subprocess.CompletedProcess(args, 1, '', 'Firewall inspection unavailable')


def required_ports(settings):
    return [('tcp', 443), ('udp', 443)] + [('tcp', x['port']) for x in subscription_endpoints(settings)]


def file_digests():
    result = {}
    for path in UFW_FILES:
        if path.is_symlink() or not path.is_file():
            raise RuntimeError('UFW 规则文件缺失或为符号链接，拒绝自动修改')
        result[str(path)] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def inspect(settings):
    report = {'backend': 'unknown', 'status': 'manual-review', 'required_ports':
              [f'{proto.upper()} {port}' for proto, port in required_ports(settings)],
              'cloud_status': 'unverified', 'public_reachability': 'unverified',
              'warnings': [CLOUD_WARNING]}
    if shutil.which('firewall-cmd'):
        active = inspect_command(['firewall-cmd', '--state'])
        if active.returncode == 0:
            report['warnings'].append('检测到 firewalld；本版不自动修改，请人工核对规则。')
            return report
        if active.returncode != 252:
            report['warnings'].append('安装了 firewalld 且无法确认其管理状态；保持规则，需人工核对。')
            return report
    if shutil.which('ufw'):
        result = inspect_command(['ufw', 'status', 'verbose'])
        if result.returncode:
            report['warnings'].append('无法读取 UFW 状态（需要 root）；不能判定为已开放。')
            return report
        if re.search(r'^Status: active$', result.stdout, re.M):
            report['backend'] = 'ufw'
            # Never insert exceptions ahead of explicit administrator deny rules.
            if (re.search(r'\b(DENY|REJECT)\b', result.stdout)
                    or not re.search(r'Default: (?:deny|reject|allow) \(incoming\), allow \(outgoing\)', result.stdout)):
                report['warnings'].append('UFW 含显式拒绝规则或非标准出站策略；保持原规则，需人工确认。')
                return report
            report['status'] = 'can-add-ufw-rules'
            return report
        if not re.search(r'^Status: inactive$', result.stdout, re.M):
            report['warnings'].append('无法识别 UFW 状态，不能假设已关闭或已放行。')
            return report
    # Disabled/missing UFW does not mean iptables/nftables are absent.
    observed = False
    for command in ('iptables-save', 'ip6tables-save'):
        if not shutil.which(command):
            continue
        result = inspect_command([command])
        if result.returncode:
            report['warnings'].append('无法读取 ' + command + '；防火墙状态未知。')
            return report
        observed = True
        if re.search(r'^-A |^:\S+ (?:DROP|REJECT) ', result.stdout, re.M):
            report['warnings'].append('检测到原生 iptables 规则；不覆盖自定义策略，请人工放行。')
            return report
    if shutil.which('nft'):
        result = inspect_command(['nft', 'list', 'ruleset'])
        if result.returncode:
            report['warnings'].append('无法读取 nftables；防火墙状态未知。')
            return report
        observed = True
        if result.stdout.strip():
            report['warnings'].append('检测到原生 nftables 规则；不覆盖自定义策略，请人工放行。')
            return report
    if observed:
        report.update(backend='none', status='no-filter-rules-observed')
        report['warnings'].append('未发现本机过滤规则；不启用新防火墙，云安全组仍需单独确认。')
    else:
        report['warnings'].append('缺少防火墙检查工具，无法确认本机策略；请人工检查。')
    return report


def rule_args(proto, port, tag):
    return ['allow', 'in', 'proto', proto, 'from', 'any', 'to', 'any',
            'port', str(port), 'comment', tag]


def overlaps_existing(proto, port):
    """Skip any overlapping UFW tuple, even restricted/IPv6/app rules.

    Re-adding an equivalent rule can replace its comment. Never relabel an
    administrator's rule as ours, because rollback would then delete it.
    """
    for path in UFW_FILES:
        for line in path.read_text().splitlines():
            if not line.startswith('### tuple ### '):
                continue
            fields = line[len('### tuple ### '):].split()
            if len(fields) < 7:
                raise RuntimeError('无法解析已有 UFW 规则；请人工核对')
            if fields[6] != 'in' or fields[1] not in (proto, 'any'):
                continue
            if fields[2] == 'any':
                return True
            for part in fields[2].split(','):
                match = re.fullmatch(r'(\d+)(?::(\d+))?', part)
                if not match:
                    raise RuntimeError('UFW 端口格式未知；拒绝自动修改')
                if int(match[1]) <= port <= int(match[2] or match[1]):
                    return True
    return False


def apply(settings, state, save, mode='auto'):
    report = inspect(settings)
    journal = {'report': report, 'rules': [], 'changed': False}
    state['host_firewall'] = journal
    save(state)
    if report['status'] != 'can-add-ufw-rules' or mode != 'auto':
        if report['status'] == 'can-add-ufw-rules':
            report['status'] = 'check-only-no-changes'
        save(state)
        return report
    journal['before'] = file_digests()
    journal['expected'] = journal['before']
    tag = 'gowest-' + secrets.token_hex(6)
    # Parse every relevant tuple before adding any rules, so unknown syntax
    # yields a warning instead of a partial firewall operation.
    try:
        overlaps = {(proto, port): overlaps_existing(proto, port) for proto, port in required_ports(settings)}
    except RuntimeError as error:
        report['status'] = 'manual-review'
        report['warnings'].append(str(error))
        save(state)
        return report
    for proto, port in required_ports(settings):
        if file_digests() != journal['expected']:
            raise RuntimeError('UFW 规则在部署期间被外部修改；停止自动配置')
        if overlaps[(proto, port)] or overlaps_existing(proto, port):
            report['warnings'].append(f'{proto.upper()} {port} 已有重叠规则，保留原规则；请确认来源范围及 IPv4/IPv6 覆盖。')
            continue
        args = rule_args(proto, port, tag)
        entry = {'args': args, 'status': 'intent'}
        journal['rules'].append(entry)
        save(state)
        result = run(['ufw'] + args)
        # An existing equivalent UFW rule must never be owned/deleted by us.
        after = file_digests()
        if after == journal['expected']:
            entry['status'] = 'unchanged'
        elif result.returncode == 0 and tag in run(['ufw', 'status']).stdout:
            entry['status'] = 'added'
            journal['changed'] = True
        else:
            entry['status'] = 'uncertain'
            save(state)
            raise RuntimeError('UFW 修改结果不明确，需人工核对；保留原始规则摘要和操作记录')
        journal['expected'] = after
        save(state)
        if result.returncode:
            raise RuntimeError('UFW 自动放行失败，请检查规则与权限')
    report['status'] = 'ufw-rules-processed'
    if any(overlaps.values()):
        report['status'] = 'ufw-overlap-needs-review'
    report['warnings'].append('已处理 UFW 放行规则；自定义底层规则和云安全组仍可能阻断。')
    save(state)
    return report


def check_restore(state):
    journal = state.get('host_firewall', {})
    if any(x['status'] in ('intent', 'uncertain') for x in journal.get('rules', [])):
        raise RuntimeError('UFW 上次变更未完整记账；回滚前请人工核对，不能猜测删除规则')
    if journal.get('changed') and file_digests() != journal['expected']:
        raise RuntimeError('UFW 规则被外部修改；拒绝自动删除，避免影响后续规则')


def restore(state, save):
    check_restore(state)
    journal = state.get('host_firewall', {})
    for entry in reversed(journal.get('rules', [])):
        if entry['status'] != 'added':
            continue
        if file_digests() != journal['expected']:
            raise RuntimeError('UFW 规则变化，停止回滚')
        entry['status'] = 'intent'
        save(state)
        result = run(['ufw', '--force', 'delete'] + entry['args'])
        if result.returncode:
            raise RuntimeError('删除本次 UFW 规则失败；保留回滚记录')
        entry['status'] = 'removed'
        journal['expected'] = file_digests()
        save(state)
    journal['changed'] = False
    if journal:
        save(state)


def display(report):
    print('本机防火墙：' + report['status'])
    print('需要的入站端口：' + '、'.join(report['required_ports']))
    for warning in report['warnings']:
        print('注意：' + warning)
