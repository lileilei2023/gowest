#!/bin/bash
# gowest macOS 客户端助手：环境检测 / 安装 Clash Verge Rev / 导入订阅。
#
# 边界（有意为之）：
#   - 不修改系统代理、DNS、TUN、路由或防火墙；这些在 Clash Verge Rev 里由你手动开关。
#   - 不需要也不接受 sudo；不会删除任何东西（旧版应用移到废纸篓，可恢复）。
#   - 订阅地址运行时粘贴，脚本里不保存任何凭据；屏幕上只显示脱敏后的地址。
#   - 安装包固定版本，从 GitHub 官方 Release 下载并校验 SHA-256，不符即中止。
#
# 用法：bash gowest-client.sh [check|install|import|all]   （默认 all）
# 兼容 macOS 自带的 bash 3.2。

set -u

KIT='gowest 0.5.7'
CV_VERSION='2.5.8'
CV_BASE_URL="https://github.com/clash-verge-rev/clash-verge-rev/releases/download/v${CV_VERSION}"
# SHA-256 由 2026-10-26 从上述官方 Release 下载的文件计算（发布日 2026-10-09）。
CV_ARM64_DMG="Clash.Verge_${CV_VERSION}_aarch64.dmg"
CV_ARM64_SHA='1f7c3a73970f103fc3cb9944744772fa1540160f05911e033ffc57f7c9f860ec'
CV_X64_DMG="Clash.Verge_${CV_VERSION}_x64.dmg"
CV_X64_SHA='8cdf7a01a1c548a081339aff21b2599d60f67730d05485e90831f4bbdbf3b093'
CV_BUNDLE_ID='io.github.clash-verge-rev.clash-verge-rev'
CV_APP_NAME='Clash Verge.app'
CV_DATA_DIR="$HOME/Library/Application Support/$CV_BUNDLE_ID"
MIN_MACOS='11.0'

WORK_DIR=''
MOUNT_POINT=''
BLOCKERS=0
WARNINGS=0

ok()    { printf '  \033[32m✔\033[0m %s\n' "$*"; }
warn()  { printf '  \033[33m⚠\033[0m %s\n' "$*"; WARNINGS=$((WARNINGS + 1)); }
bad()   { printf '  \033[31m✖\033[0m %s\n' "$*"; BLOCKERS=$((BLOCKERS + 1)); }
info()  { printf '  · %s\n' "$*"; }
title() { printf '\n\033[1m%s\033[0m\n' "$*"; }
die()   { printf '\n\033[31m操作停止：\033[0m%s\n' "$*" >&2; exit 1; }

cleanup() {
    if [ -n "$MOUNT_POINT" ] && [ -d "$MOUNT_POINT" ]; then
        hdiutil detach "$MOUNT_POINT" -quiet >/dev/null 2>&1 || hdiutil detach "$MOUNT_POINT" -force -quiet >/dev/null 2>&1
    fi
    # Only our own mktemp directory under $TMPDIR is removed.
    if [ -n "$WORK_DIR" ] && [ -d "$WORK_DIR" ]; then
        case "$WORK_DIR" in
            "${TMPDIR:-/tmp}"*|/tmp/*|/private/var/folders/*) rm -rf "$WORK_DIR" ;;
        esac
    fi
}
trap cleanup EXIT
trap 'exit 130' INT TERM

confirm() {
    # confirm "问题" -> 0 when the user types y/yes
    local answer
    printf '%s [y/N]：' "$1"
    read -r answer || return 1
    case "$answer" in y|Y|yes|YES|Yes) return 0 ;; *) return 1 ;; esac
}

# version_ge A B: A >= B for dotted numeric versions (no sort -V on macOS).
version_ge() {
    awk -v a="$1" -v b="$2" 'BEGIN {
        na = split(a, x, "."); nb = split(b, y, "."); n = (na > nb) ? na : nb
        for (i = 1; i <= n; i++) { p = x[i] + 0; q = y[i] + 0
            if (p > q) exit 0; if (p < q) exit 1 }
        exit 0 }'
}

urlencode() {
    # Byte-wise RFC 3986 encoding (UTF-8 names included); od avoids bash locale quirks.
    printf '%s' "$1" | od -An -v -tx1 | tr -s ' \n' '\n\n' | sed '/^$/d' | while read -r hex; do
        case "$hex" in
            3[0-9]|4[1-9a-f]|5[0-9a]|6[1-9a-f]|7[0-9a]|2d|2e|5f|7e) printf "\\x$hex" ;;
            *) printf '%%%s' "$(printf '%s' "$hex" | tr 'a-f' 'A-F')" ;;
        esac
    done
}

profile_has_url() {
    # profile_has_url FILE URL — exact match of a saved subscription address.
    [ -f "$1" ] || return 1
    local escaped
    escaped=$(printf '%s' "$2" | sed 's/[.]/\\./g')
    grep -qE "^[[:space:]]*url:[[:space:]]*[\"']?${escaped}[\"']?[[:space:]]*$" "$1"
}

mask_url() {
    # https://host:port/sub/abcdef…-us  — never echo the full token.
    printf '%s' "$1" | sed -E 's#(/sub/[0-9a-f]{6})[0-9a-f]+#\1…#'
}

installed_app() {
    local dir
    for dir in "/Applications" "$HOME/Applications"; do
        if [ -d "$dir/$CV_APP_NAME" ]; then
            printf '%s' "$dir/$CV_APP_NAME"
            return 0
        fi
    done
    return 1
}

plist_value() {
    /usr/libexec/PlistBuddy -c "Print :$2" "$1/Contents/Info.plist" 2>/dev/null
}

app_running() {
    pgrep -x 'clash-verge' >/dev/null 2>&1 || pgrep -f "$CV_APP_NAME/Contents/MacOS" >/dev/null 2>&1
}

# ---------------------------------------------------------------- check

cmd_check() {
    BLOCKERS=0
    WARNINGS=0
    title "$KIT · Mac 环境检测（只读，不做任何修改）"

    title '系统'
    local product arch
    product=$(sw_vers -productVersion 2>/dev/null || echo '0')
    arch=$(uname -m)
    if version_ge "$product" "$MIN_MACOS"; then ok "macOS $product"; else bad "macOS $product，Clash Verge Rev 需要 $MIN_MACOS 及以上"; fi
    case "$arch" in
        arm64)  ok '处理器：Apple 芯片 (arm64)' ;;
        x86_64)
            if [ "$(sysctl -n sysctl.proc_translated 2>/dev/null)" = '1' ]; then
                warn '终端运行在 Rosetta 转译下；将按 Apple 芯片安装 arm64 版本'
            else
                ok '处理器：Intel (x86_64)'
            fi ;;
        *) bad "不支持的处理器架构：$arch" ;;
    esac
    if [ "$(id -u)" = '0' ]; then bad '请不要用 sudo 或 root 运行本脚本'; fi
    if id -Gn | tr ' ' '\n' | grep -qx admin; then ok '当前用户是管理员，可写入 /Applications'; else warn '当前用户不是管理员，将安装到 ~/Applications'; fi
    local free_kb
    free_kb=$(df -Pk "$HOME" | awk 'NR == 2 {print $4}')
    if [ "${free_kb:-0}" -gt 1048576 ]; then ok '磁盘空间充足'; else warn '主目录所在磁盘剩余不足 1 GiB'; fi

    title 'Clash Verge Rev'
    local app version
    if app=$(installed_app); then
        version=$(plist_value "$app" CFBundleShortVersionString)
        if [ "$(plist_value "$app" CFBundleIdentifier)" != "$CV_BUNDLE_ID" ]; then
            warn "$app 的标识不是 Clash Verge Rev（可能是旧版 Clash Verge），建议人工确认"
        elif version_ge "${version:-0}" "$CV_VERSION"; then
            ok "已安装 $version：$app"
        else
            warn "已安装 $version，低于脚本锁定的 $CV_VERSION；install 会升级（旧版移到废纸篓）"
        fi
        if app_running; then info 'Clash Verge Rev 正在运行'; fi
    else
        info "未安装；install 会安装 $CV_VERSION"
    fi
    if [ -f "$CV_DATA_DIR/profiles.yaml" ]; then
        local count
        count=$(grep -c '/sub/[0-9a-f]\{48\}' "$CV_DATA_DIR/profiles.yaml" 2>/dev/null || true)
        info "已有订阅配置中，gowest 格式的订阅 ${count:-0} 条"
    fi

    title '其他代理客户端'
    local found=0 name
    for name in 'ClashX.app' 'ClashX Pro.app' 'ClashX Meta.app' 'Clash for Windows.app' 'Clash Nyanpasu.app' \
                'Mihomo Party.app' 'FlClash.app' 'Surge.app' 'Stash.app' 'V2rayU.app' 'V2RayX.app' \
                'Quantumult X.app' 'Loon.app' 'Shadowrocket.app' 'sing-box.app' 'SFM.app' 'Hiddify.app'; do
        if [ -d "/Applications/$name" ] || [ -d "$HOME/Applications/$name" ]; then
            info "已安装：${name%.app}"
            found=1
        fi
    done
    local procs
    procs=$(ps -Ao comm= | awk -F/ '{print $NF}' | grep -iE '^(clashx|clash for windows|mihomo|verge-mihomo|clash-meta|surge|stash|v2rayu|xray|v2ray|sing-box|hiddify|flclash|quantumult|loon|shadowrocket)' | grep -v '^verge-mihomo' | sort -u)
    if [ -n "$procs" ]; then
        warn "正在运行的其他代理程序：$(printf '%s' "$procs" | tr '\n' ' ')— 导入后请只保留一个代理客户端运行"
        found=1
    fi
    if [ "$found" = '0' ]; then ok '未发现其他代理客户端'; fi
    if [ -d '/Applications/Clash for Windows.app' ]; then
        warn 'Clash for Windows 的内核不支持本项目的 VLESS+REALITY / Hysteria2 节点，请改用 Clash Verge Rev'
    fi

    title '网络现状'
    local proxy
    proxy=$(scutil --proxy 2>/dev/null)
    if printf '%s' "$proxy" | grep -qE '(HTTPEnable|HTTPSEnable|SOCKSEnable) : 1'; then
        warn "系统代理已开启：$(printf '%s' "$proxy" | awk '/HTTPProxy|HTTPPort|SOCKSProxy|SOCKSPort/ {printf "%s=%s ", $1, $3}')"
        info '若指向已退出的旧客户端，会导致断网；可在“系统设置 → 网络 → 详细信息 → 代理”关闭'
    else
        ok '系统代理未开启'
    fi
    if printf '%s' "$proxy" | grep -q 'ProxyAutoConfigEnable : 1'; then warn '已启用 PAC 自动代理配置'; fi
    local dns
    dns=$(scutil --dns 2>/dev/null | awk '/nameserver\[[0-9]+\]/ {print $3}' | sort -u | tr '\n' ' ')
    if printf '%s' "$dns" | grep -q '198\.18\.'; then
        if ifconfig 2>/dev/null | grep -q 'inet 198\.18\.'; then
            info "DNS 指向 198.18.x（当前有代理 TUN 在运行）：$dns"
        else
            warn "DNS 仍指向 198.18.x 但没有 TUN 在运行，属于旧客户端残留，会导致无法解析：$dns"
        fi
    else
        ok "DNS：${dns:-未读取到}"
    fi
    if ifconfig 2>/dev/null | grep -q 'inet 198\.18\.'; then info '检测到 198.18.x 的虚拟网卡：已有代理 TUN 处于开启状态'; fi
    if netstat -anv -p tcp 2>/dev/null | grep -E '[.:]7897 ' | grep -q LISTEN; then
        if app_running; then info '端口 7897 已被占用（Clash Verge Rev 正在运行，属正常）'; else warn '端口 7897 已被其他程序占用，Clash Verge Rev 的混合端口会冲突'; fi
    else
        ok '端口 7897 空闲'
    fi
    if [ -d '/Applications/Tailscale.app' ] || pgrep -x tailscaled >/dev/null 2>&1; then
        info '检测到 Tailscale：订阅已内置 Tailscale 直连例外'
    fi
    local code
    code=$(curl -sS -o /dev/null -I -m 10 -w '%{http_code}' https://github.com 2>/dev/null || true)
    case "$code" in
        2??|3??) ok 'GitHub 可访问（用于下载安装包）' ;;
        *) warn 'GitHub 当前不可访问或很慢；安装包下载可能失败，可稍后重试或先连上其他网络' ;;
    esac

    title '结论'
    if [ "$BLOCKERS" -gt 0 ]; then
        bad "有 $BLOCKERS 项阻止安装，请先处理"
        return 1
    fi
    if [ "$WARNINGS" -gt 0 ]; then warn "有 $WARNINGS 项提示，建议看一下再继续"; else ok '环境正常'; fi
    return 0
}

# ---------------------------------------------------------------- install

cmd_install() {
    title "安装 Clash Verge Rev $CV_VERSION"
    [ "$(id -u)" != '0' ] || die '请不要用 sudo 或 root 运行本脚本'
    version_ge "$(sw_vers -productVersion)" "$MIN_MACOS" || die "需要 macOS $MIN_MACOS 及以上"

    local existing='' existing_version='' asset expected
    if existing=$(installed_app); then
        existing_version=$(plist_value "$existing" CFBundleShortVersionString)
        if [ "$(plist_value "$existing" CFBundleIdentifier)" = "$CV_BUNDLE_ID" ] && version_ge "${existing_version:-0}" "$CV_VERSION"; then
            ok "已是 $existing_version，无需安装"
            return 0
        fi
    else
        existing=''
    fi
    if [ -n "$existing" ] && app_running; then
        die '请先在菜单栏退出 Clash Verge，再重新运行 install（升级时不能替换正在运行的应用）'
    fi

    if [ "$(uname -m)" = 'arm64' ] || [ "$(sysctl -n hw.optional.arm64 2>/dev/null)" = '1' ]; then
        asset=$CV_ARM64_DMG; expected=$CV_ARM64_SHA
    else
        asset=$CV_X64_DMG; expected=$CV_X64_SHA
    fi

    WORK_DIR=$(mktemp -d "${TMPDIR:-/tmp}/gowest.XXXXXX") || die '无法创建临时目录'
    info "下载 $asset（约 80 MB，来自 GitHub 官方 Release）"
    curl --fail --location --proto '=https' --proto-redir '=https' --connect-timeout 20 \
         --retry 2 --progress-bar -o "$WORK_DIR/$asset" "$CV_BASE_URL/$asset" \
        || die '下载失败；网络不通时可稍后重试'
    local actual
    actual=$(shasum -a 256 "$WORK_DIR/$asset" | awk '{print $1}')
    [ "$actual" = "$expected" ] || die "SHA-256 不符（期望 $expected，实际 $actual），已拒绝安装"
    ok 'SHA-256 校验通过'

    MOUNT_POINT="$WORK_DIR/mnt"
    mkdir "$MOUNT_POINT"
    hdiutil attach "$WORK_DIR/$asset" -nobrowse -readonly -noautoopen -mountpoint "$MOUNT_POINT" -quiet \
        || die '无法挂载安装镜像'
    local source="$MOUNT_POINT/$CV_APP_NAME"
    [ -d "$source" ] && [ ! -L "$source" ] || die '安装镜像中没有找到 Clash Verge.app'
    [ "$(plist_value "$source" CFBundleIdentifier)" = "$CV_BUNDLE_ID" ] || die '安装镜像中的应用标识不符'
    [ "$(plist_value "$source" CFBundleShortVersionString)" = "$CV_VERSION" ] || die '安装镜像中的应用版本不符'

    local target_dir='/Applications'
    if [ -n "$existing" ]; then
        target_dir=$(dirname "$existing")
    elif [ ! -w /Applications ]; then
        target_dir="$HOME/Applications"
        mkdir -p "$target_dir" || die "无法创建 $target_dir"
    fi
    [ -w "$target_dir" ] || die "$target_dir 不可写；请用管理员账户运行"

    if [ -n "$existing" ]; then
        local trash="$HOME/.Trash/Clash Verge ${existing_version:-old} $(date +%Y%m%d-%H%M%S).app"
        mv "$existing" "$trash" || die '无法把旧版移到废纸篓；未做任何改动'
        info "旧版 ${existing_version:-未知} 已移到废纸篓，可随时恢复"
    fi
    ditto "$source" "$target_dir/$CV_APP_NAME" || die "复制到 $target_dir 失败"
    # curl does not set quarantine; clear it only if something else added it to our copy.
    xattr -dr com.apple.quarantine "$target_dir/$CV_APP_NAME" 2>/dev/null || true
    /System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister \
        -f "$target_dir/$CV_APP_NAME" >/dev/null 2>&1 || true
    hdiutil detach "$MOUNT_POINT" -quiet >/dev/null 2>&1 && MOUNT_POINT=''
    ok "已安装到 $target_dir/$CV_APP_NAME"
    info '首次打开如提示“无法验证开发者”：系统设置 → 隐私与安全性 → 仍要打开'
    return 0
}

# ---------------------------------------------------------------- import

read_subscription() {
    local line url
    # Prompts go to stderr: stdout is captured by the caller.
    printf '\n在服务器上运行  sudo bash /opt/la-vpn/deploy.sh urls --client mac  查看订阅地址。\n' >&2
    printf '请粘贴“日常分流订阅”地址（https://…/sub/ 加 48 位令牌）：\n> ' >&2
    read -r line || return 1
    url=$(printf '%s' "$line" | sed -E 's/^[[:space:]]*(normal:)?[[:space:]]*//; s/[[:space:]]+$//')
    if ! printf '%s' "$url" | grep -qE '^https?://[A-Za-z0-9.-]+(:[0-9]{1,5})?/sub/[0-9a-f]{48}(-us)?$'; then
        return 2
    fi
    printf '%s' "${url%-us}"
}

fetch_profile() {
    # fetch_profile URL OUTFILE — direct (no proxy), bounded, URL kept out of argv.
    printf 'url = "%s"\n' "$1" | curl --config - --noproxy '*' --fail --silent --show-error \
        --max-time 20 --max-filesize 1048576 -o "$2"
}

cmd_import() {
    title '导入订阅到 Clash Verge Rev'
    [ "$(id -u)" != '0' ] || die '请不要用 sudo 或 root 运行本脚本'
    local app
    app=$(installed_app) || die '还没有安装 Clash Verge Rev；先运行 install'

    local base status
    base=$(read_subscription); status=$?
    [ "$status" = '0' ] || die '地址格式不对。请完整粘贴 urls 命令输出的 https://…/sub/<48位令牌> 地址'
    case "$base" in http://*) warn '这是 HTTP 订阅，订阅内容（含节点凭据）会明文传输' ;; esac

    [ -n "$WORK_DIR" ] || WORK_DIR=$(mktemp -d "${TMPDIR:-/tmp}/gowest.XXXXXX") || die '无法创建临时目录'
    info "检查订阅：$(mask_url "$base")"
    fetch_profile "$base" "$WORK_DIR/normal.yaml" || die '无法下载订阅；请确认地址正确、服务器订阅端口已在云防火墙放行'
    grep -q '^proxies:' "$WORK_DIR/normal.yaml" && grep -q '出口选择' "$WORK_DIR/normal.yaml" \
        || die '下载到的内容不是 gowest 订阅'
    local label
    label=$(sed -nE 's/^- name: ([A-Za-z][A-Za-z0-9-]{0,24})-Hy2$/\1/p' "$WORK_DIR/normal.yaml" | head -n 1)
    [ -n "$label" ] || label='gowest'
    ok "订阅可用，服务器标签：$label"

    local urls names
    urls="$base"
    names="gowest-$label-日常"
    if confirm '同时导入“全公网”版（国内普通公网也走代理）？'; then
        fetch_profile "$base-us" "$WORK_DIR/strict.yaml" || die '全公网订阅下载失败'
        urls="$urls
$base-us"
        names="$names
gowest-$label-全公网"
    fi

    if ! app_running; then
        info '启动 Clash Verge Rev'
        open -a "$app" || die '无法启动 Clash Verge Rev'
        local i
        for i in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15; do app_running && break; sleep 1; done
        sleep 3
    fi

    local profiles="$CV_DATA_DIR/profiles.yaml" url name n=1 link
    while IFS= read -r url; do
        name=$(printf '%s\n' "$names" | sed -n "${n}p")
        n=$((n + 1))
        if profile_has_url "$profiles" "$url"; then
            ok "$name 已存在，跳过（避免重复）"
            continue
        fi
        # name must precede url: Clash Verge Rev reads everything after url= as the address.
        link="clash://install-config?name=$(urlencode "$name")&url=$(urlencode "$url")"
        open "$link" || die '无法唤起 Clash Verge Rev 的导入链接'
        info "已发送导入请求：$name"
        sleep 3
    done <<EOF
$urls
EOF

    local imported=0 i
    for i in 1 2 3 4 5 6 7 8 9 10; do
        if profile_has_url "$profiles" "$base"; then imported=1; break; fi
        sleep 2
    done
    if [ "$imported" = '1' ]; then ok '已写入 Clash Verge Rev 的订阅列表'; else warn '暂未在订阅列表中看到，请查看 Clash Verge Rev 窗口里的提示'; fi

    title '接下来（需要你在 Clash Verge Rev 里手动操作）'
    info "1. 订阅 页：点选 gowest-$label-日常 使其生效（只有一个订阅时会自动选中）"
    info '2. 设置 页：打开“系统代理”；需要接管 WebRTC/不走系统代理的应用时，再打开“虚拟网卡模式（TUN）”'
    info '3. 代理 页：出口选择 / AI / YouTube / TikTok 默认“自动回退”，可按需手动固定节点'
    info '本脚本没有改动系统代理、DNS 或 TUN。'
}

usage() {
    cat <<EOF
$KIT Mac 客户端助手
用法：bash $(basename "$0") [命令]
  check    只读检测环境（系统、已装客户端、系统代理/DNS/TUN 残留、端口、GitHub 连通）
  install  下载并校验 Clash Verge Rev $CV_VERSION，安装到“应用程序”
  import   粘贴订阅地址，校验后自动导入 Clash Verge Rev
  all      依次执行 check → install → import（默认）
EOF
}

main() {
    case "${1:-all}" in
        check) cmd_check ;;
        install) cmd_install ;;
        import) cmd_import ;;
        all)
            cmd_check || die '环境检测未通过'
            confirm '继续安装/检查 Clash Verge Rev 并导入订阅？' || { info '已取消，未做任何改动'; exit 0; }
            cmd_install || exit 1
            cmd_import ;;
        -h|--help|help) usage ;;
        --version) printf '%s\n' "$KIT" ;;
        *) usage; exit 2 ;;
    esac
}

main "$@"
