#!/usr/bin/env python3
"""Build on the target OS/architecture; never deploy or alter host services."""
import argparse
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
import xml.etree.ElementTree as ET

import render

ROOT = Path(__file__).resolve().parent
MODULES = ('launcher', 'vpn', 'render', 'routing', 'network', 'subinfo', 'accounting', 'unlock',
           'autoconfig', 'hostfirewall', 'firewall', 'kit_runtime', 'installlog')
ALLOWED_RELEASE_FILES = {'gowest', '使用说明.md', 'THIRD-PARTY-NOTICES.txt',
                         'build-info.json', 'SHA256SUMS'}


def smoke(binary):
    """Run only offline operations, outside the source tree, with isolated env."""
    with tempfile.TemporaryDirectory(prefix='gowest-native-smoke-') as temporary:
        directory = Path(temporary)
        standalone = directory / 'gowest'
        shutil.copy2(binary, standalone)
        env = {k: v for k, v in os.environ.items() if not k.startswith(('PYTHON', 'NUITKA'))}
        def invoke(*args):
            return subprocess.run([str(standalone), *args], cwd=directory, env=env,
                                  check=True, capture_output=True, text=True, timeout=120)
        assert invoke('--version').stdout.strip() == render.PROJECT_NAME + ' ' + render.KIT_VERSION
        invoke('--help')
        invoke('init', '--server-ip', '8.8.4.4', '--bandwidth', '1Gbps', '--accept-http-risk')
        cfg = json.loads((directory / 'settings.local.json').read_text())
        assert cfg['subscription']['scheme'] == 'http'
        assert cfg['hy2_up_mbps'] == cfg['hy2_down_mbps'] == 1000
        plan = json.loads(invoke('plan', '--settings', 'settings.local.json').stdout)
        assert plan['subscription_transports'] == ['http']
        invoke('render', '--settings', 'settings.local.json', '--output', 'generated')
        for name in render.SUBSCRIPTION_FILES.values():
            assert (directory / 'generated' / name).is_file(), name
        assert 'gowest-LA-' in (directory / 'generated/client-import.txt').read_text()
        invoke('firewall', 'generate', '--settings', 'settings.local.json',
               '--ssh-source', '8.8.8.8/32', '--output', 'firewall.json')
        invoke('firewall', 'check', '--settings', 'settings.local.json', '--file', 'firewall.json')
        invoke('_boot-fq', '--help')  # Help only: no networking changes.
    return {'offline_native_smoke': 'passed', 'fresh_server_install': 'not tested'}


def audit_native_dependencies(directory):
    """Reject accidentally retained build-host library paths in a Mac release."""
    if platform.system() != 'Darwin':
        return 'target Linux runtime still requires acceptance testing'
    for binary in directory.rglob('*'):
        if not binary.is_file() or binary.suffix not in ('.so', '.dylib', '.bin'):
            continue
        output = subprocess.run(['otool', '-L', str(binary)], check=True, capture_output=True, text=True).stdout
        identity = ''
        if binary.suffix == '.dylib':
            rows = subprocess.run(['otool', '-D', str(binary)], check=True,
                                  capture_output=True, text=True).stdout.splitlines()
            identity = rows[1].strip() if len(rows) > 1 else ''
        for line in output.splitlines()[1:]:
            dependency = line.strip().split(' (', 1)[0]
            if dependency == identity:
                continue  # LC_ID_DYLIB identifies this library; it is not a dependency.
            if dependency.startswith(('/usr/lib/', '/System/Library/', '@executable_path/', '@loader_path/')):
                continue
            raise RuntimeError('Unbundled library in ' + binary.name + ': ' + dependency)
    return 'no non-system absolute library dependencies'


def audit_project_code(directory):
    # Nuitka freezes standard library bytecode internally, but project modules
    # must all be native compiled and must never be included as data files.
    for path in directory.rglob('*'):
        if path.suffix in ('.py', '.pyc', '.pyo', '.sh', '.ps1'):
            raise RuntimeError('Unexpected source/bytecode file in native payload: ' + path.name)
    report = ET.parse(directory.parent.parent / 'compilation-report.xml')
    modules = {row.attrib['name']: row.attrib['kind'] for row in report.findall('module')}
    for name in MODULES:
        key = '__main__' if name == 'launcher' else name
        if modules.get(key) != ('PythonMainModule' if name == 'launcher' else 'CompiledPythonModule'):
            raise RuntimeError('Project module is not native compiled: ' + name)


def notices():
    sections = ['Third-party notices. gowest application source is not included.\n'
                'sing-box and Ubuntu packages are downloaded at installation, not bundled here.\n'
                'Native code is reversible; this is not a source confidentiality guarantee.']
    # Preserve the actual legal texts supplied with the build environment.
    for package in ('PyYAML', 'Nuitka', 'zstandard'):
        distribution = importlib.metadata.distribution(package)
        legal = [file for file in distribution.files or []
                 if 'license' in file.name.lower() or file.name.upper() == 'COPYING']
        if not legal:
            raise RuntimeError('Missing license metadata: ' + package)
        for file in legal:
            sections.append(f'\n{package}: {file.name}\n' + distribution.locate_file(file).read_text())
    license_file = Path(sys.base_prefix) / 'LICENSE.txt'
    if not license_file.is_file():
        license_file = Path(sys.base_prefix) / f'lib/python{sys.version_info.major}.{sys.version_info.minor}/LICENSE.txt'
    if not license_file.is_file():
        license_file = Path('/usr/share/doc/python3.12/copyright')
    if not license_file.is_file():
        raise RuntimeError('Missing Python license; supply it in the build Python prefix')
    sections.append('\nPython\n' + license_file.read_text())
    if platform.system() == 'Darwin':
        extra = [Path('/opt/homebrew/opt/openssl@3/LICENSE.txt'),
                 Path('/opt/homebrew/opt/xz/COPYING.0BSD')]
    else:
        extra = [Path('/usr/share/doc') / name / 'copyright' for name in
                 ('libssl3t64', 'liblzma5', 'libyaml-0-2', 'zlib1g', 'libbz2-1.0',
                  'libffi8', 'libexpat1')]
    for file in extra:
        if not file.is_file():
            raise RuntimeError('Missing bundled dependency notice: ' + str(file))
        sections.append('\n' + file.parent.name + '\n' + file.read_text())
    return '\n'.join(sections)


def package(stage, destination):
    members = {p.name for p in stage.iterdir()}
    if members != ALLOWED_RELEASE_FILES or any(p.is_symlink() or not p.is_file() for p in stage.iterdir()):
        raise RuntimeError('Native package allowlist violation')
    # Exclusive output creation: never overwrite another release.
    with destination.open('xb') as stream, tarfile.open(fileobj=stream, mode='w:gz') as archive:
        for name in sorted(members):
            archive.add(stage / name, arcname='gowest/' + name, recursive=False)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--work', required=True, help='New local build directory (not a network volume)')
    parser.add_argument('--output', default=str(ROOT / 'dist'))
    parser.add_argument('--jobs', type=int, default=2)
    parser.add_argument('--smoke-only', type=Path, help='Validate an existing native executable, without building')
    args = parser.parse_args()
    if args.smoke_only:
        print(json.dumps(smoke(args.smoke_only.resolve())))
        return
    if not 1 <= args.jobs <= 8:
        parser.error('--jobs must be between 1 and 8')
    host = platform.system()
    if host not in ('Linux', 'Darwin'):
        parser.error('Build requires Linux or macOS; Linux deployment binaries must be built on Linux')
    if host == 'Linux':
        release = Path('/etc/os-release').read_text()
        if 'ID=ubuntu\n' not in release or 'VERSION_ID="24.04"' not in release:
            parser.error('Linux release build requires Ubuntu 24.04')
    arch = {'x86_64': 'amd64', 'aarch64': 'arm64', 'arm64': 'arm64'}.get(platform.machine())
    if not arch:
        parser.error('Unsupported architecture')
    for name, version in [('Nuitka', '4.3.1'), ('PyYAML', '6.0.3')]:
        if importlib.metadata.version(name) != version:
            parser.error('Use the pinned requirements-build.txt: ' + name)
    work = Path(args.work).absolute()
    work.mkdir(mode=0o700)  # Never reuse stale output.
    source = work / 'source'
    source.mkdir()
    for module in MODULES:
        shutil.copy2(ROOT / (module + '.py'), source)
    result = work / 'result'
    command = [sys.executable, '-m', 'nuitka', '--mode=onefile', '--output-filename=gowest',
               '--output-dir=' + str(result), '--jobs=' + str(args.jobs), '--lto=no',
               '--python-flag=isolated', '--python-flag=no_site', '--include-package=yaml',
               '--onefile-child-grace-time=infinity',
               '--user-package-configuration-file=' + str(ROOT / 'native/dependencies.nuitka-package.config.yml'),
               '--assume-yes-for-downloads', '--report=' + str(work / 'compilation-report.xml')]
    command += ['--include-module=' + name for name in MODULES if name != 'launcher']
    command += [str(source / 'launcher.py')]
    subprocess.run(command, cwd=source, check=True)
    binary = result / 'gowest'
    report = smoke(binary)
    audit_project_code(result / 'launcher.dist')
    report['dynamic_library_audit'] = audit_native_dependencies(result / 'launcher.dist')
    stage = work / 'release'
    stage.mkdir()
    shutil.copy2(binary, stage / 'gowest')
    shutil.copy2(ROOT / 'native/使用说明.md', stage / '使用说明.md')
    (stage / 'THIRD-PARTY-NOTICES.txt').write_text(notices(), encoding='utf-8')
    metadata = dict(project=render.PROJECT_NAME, version=render.KIT_VERSION,
                    os=host, arch=arch, python=platform.python_version(),
                    nuitka=importlib.metadata.version('Nuitka'), **report)
    metadata['source_sha256'] = {name + '.py': hashlib.sha256((source / (name + '.py')).read_bytes()).hexdigest()
                               for name in MODULES}
    (stage / 'build-info.json').write_text(json.dumps(metadata, indent=2) + '\n')
    checksums = ''.join(hashlib.sha256(p.read_bytes()).hexdigest() + '  ' + p.name + '\n'
                        for p in sorted(stage.iterdir()))
    (stage / 'SHA256SUMS').write_text(checksums)
    output = Path(args.output).absolute()
    output.mkdir(parents=True, exist_ok=True)
    target = output / f'gowest-{render.KIT_VERSION}-{host.lower()}-{arch}-native.tar.gz'
    package(stage, target)
    print('Native release: ' + str(target))
    print('SHA256: ' + hashlib.sha256(target.read_bytes()).hexdigest())
    print('Offline smoke passed; this is NOT a full fresh-server installation acceptance test.')


if __name__ == '__main__':
    main()
