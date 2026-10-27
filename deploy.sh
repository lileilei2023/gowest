#!/bin/sh
# Local entry point. No curl|sh, no automatic sudo, no implicit remote login.
set -eu
kit_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
if ! command -v python3 >/dev/null 2>&1; then
    echo '需要 Python 3.10+；Ubuntu 24.04 标准镜像通常已提供 python3。' >&2
    exit 1
fi
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' || {
    echo '需要 Python 3.10 或更新版本。' >&2
    exit 1
}
exec python3 -B "$kit_dir/launcher.py" "$@"
