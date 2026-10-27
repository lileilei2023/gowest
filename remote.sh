#!/bin/sh
# Run from your own computer: copy this kit to a server over SSH, then
# install, upgrade or manage it there. The server needs no git and no
# access to this (private) repository.
#
#   ./remote.sh root@203.0.113.7              首次安装（运行中文向导）
#   ./remote.sh root@203.0.113.7 upgrade      升级为本机这份代码；密钥和订阅地址不变
#   ./remote.sh root@203.0.113.7 status       其他管理命令：status / urls / bandwidth / traffic …
#
# 目标可以是 ~/.ssh/config 里的别名；这时 upgrade 等命令需要用
# GOWEST_IP=<服务器公网IP> 指明要确认的地址。
set -eu
kit_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
remote_dir=${GOWEST_REMOTE_DIR:-gowest}

if [ $# -lt 1 ] || [ "$1" = -h ] || [ "$1" = --help ]; then
    sed -n '2,11p' "$0" | sed 's/^# \{0,1\}//'
    exit 2
fi
target=$1
shift
[ $# -gt 0 ] || set -- install

host=${target#*@}
ip=${GOWEST_IP:-}
if [ -z "$ip" ] && printf '%s' "$host" | grep -Eq '^([0-9]{1,3}\.){3}[0-9]{1,3}$'; then
    ip=$host
fi

upload() {
    echo "→ 上传 gowest 到 $target:$remote_dir"
    # Only what the server runs: no git history, tests, docs or build output.
    COPYFILE_DISABLE=1 tar -C "$kit_dir" -czf - \
        --exclude ./.git --exclude ./tests --exclude ./docs --exclude ./dist --exclude ./clients \
        --exclude ./native --exclude '__pycache__' --exclude '.DS_Store' --exclude './generated*' . |
        ssh "$target" "set -e; rm -rf '$remote_dir.new'; mkdir -p '$remote_dir.new';
            tar -xzf - -C '$remote_dir.new' 2>/dev/null; rm -rf '$remote_dir'; mv '$remote_dir.new' '$remote_dir'"
}

with_ip() {
    if [ -z "$ip" ]; then
        echo "需要服务器公网 IP 做确认：GOWEST_IP=<IP> $0 $target $*" >&2
        exit 2
    fi
    case " $* " in
        *" --confirm-server "*) ;;
        *) set -- "$@" --confirm-server "$ip" ;;
    esac
    args=$*
}

action=$1
case $action in
    install)
        shift
        upload
        exec ssh -t "$target" "cd '$remote_dir' && sudo bash deploy.sh $*" ;;
    upgrade)
        with_ip "$@"
        upload
        # Runs the uploaded (new) kit, which swaps itself into /opt/la-vpn.
        exec ssh -t "$target" "cd '$remote_dir' && sudo bash deploy.sh $args" ;;
    bandwidth|traffic|repair-fq)
        with_ip "$@" ;;
    *)
        args=$* ;;
esac
# Day-to-day commands use the installed version, never a newer uploaded copy.
exec ssh -t "$target" "if [ -x /opt/la-vpn/gowest ]; then sudo /opt/la-vpn/gowest $args; else sudo bash /opt/la-vpn/deploy.sh $args; fi"
