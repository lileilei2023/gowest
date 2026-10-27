# gowest Windows 客户端助手：环境检测 / 安装 Clash Verge Rev / 导入订阅。
#
# 边界（有意为之）：
#   - 不修改系统代理、DNS、TUN、路由或防火墙；这些在 Clash Verge Rev 里由你手动开关。
#   - 只有安装步骤会弹出一次 UAC（官方安装包按全机安装）；检测和导入不需要管理员。
#   - 订阅地址运行时粘贴，脚本里不保存任何凭据；屏幕上只显示脱敏后的地址。
#   - 安装包固定版本，从 GitHub 官方 Release 下载并校验 SHA-256，不符即中止。
#
# 用法：双击 gowest-client.cmd，或
#   powershell -NoProfile -ExecutionPolicy Bypass -File gowest-client.ps1 [check|install|import|all]
# 兼容 Windows 10/11 自带的 Windows PowerShell 5.1。本文件须保存为带 BOM 的 UTF-8。

param([ValidateSet('all', 'check', 'install', 'import', 'help', 'version')][string]$Command = 'all')

Set-StrictMode -Version 1.0
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
try { [Console]::OutputEncoding = [Text.Encoding]::UTF8 } catch { }
[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12

$Kit = 'gowest 0.5.7'
$CvVersion = '2.5.8'
$CvBaseUrl = "https://github.com/clash-verge-rev/clash-verge-rev/releases/download/v$CvVersion"
# SHA-256 由 2026-10-26 从上述官方 Release 下载的文件计算（发布日 2026-10-09）。
$CvAssets = @{
    'x64'   = @{ Name = "Clash.Verge_${CvVersion}_x64-setup.exe";   Sha = '821c0f5957f84c8f7d14532044840e8280d708af24a6a143db360b49bd1e7302' }
    'arm64' = @{ Name = "Clash.Verge_${CvVersion}_arm64-setup.exe"; Sha = '077efab7874a6be7c48630d19c941ef679fa265f79c243f9d20ce5878eb5a943' }
}
$CvDataDir = Join-Path $env:APPDATA 'io.github.clash-verge-rev.clash-verge-rev'
$CvProfiles = Join-Path $CvDataDir 'profiles.yaml'
$Script:Blockers = 0
$Script:Warnings = 0

function Ok($m)    { Write-Host "  [OK] $m" -ForegroundColor Green }
function Warn($m)  { Write-Host "  [!]  $m" -ForegroundColor Yellow; $Script:Warnings++ }
function Bad($m)   { Write-Host "  [X]  $m" -ForegroundColor Red; $Script:Blockers++ }
function Info($m)  { Write-Host "  ·    $m" }
function Title($m) { Write-Host ''; Write-Host $m -ForegroundColor Cyan }
function Stop-Kit($m) { throw $m }

function Confirm-Kit($question) {
    $answer = Read-Host "$question [y/N]"
    return ($answer -match '^(y|yes)$')
}

function Get-Arch {
    $a = $env:PROCESSOR_ARCHITEW6432
    if (-not $a) { $a = $env:PROCESSOR_ARCHITECTURE }
    switch ($a) { 'AMD64' { return 'x64' } 'ARM64' { return 'arm64' } default { return $a } }
}

function Get-UninstallEntries($pattern) {
    $roots = @('HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall',
               'HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall',
               'HKCU:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall')
    foreach ($root in $roots) {
        if (-not (Test-Path $root)) { continue }
        foreach ($key in Get-ChildItem $root -ErrorAction SilentlyContinue) {
            $p = Get-ItemProperty $key.PSPath -ErrorAction SilentlyContinue
            if ($p -and ($p.PSObject.Properties.Name -contains 'DisplayName') -and $p.DisplayName -match $pattern) { $p }
        }
    }
}

function Get-ClashVerge {
    # Official NSIS key: ...\Uninstall\Clash Verge (perMachine).
    $entry = Get-UninstallEntries '^Clash Verge$' | Select-Object -First 1
    if (-not $entry) { return $null }
    $dir = $null
    if ($entry.PSObject.Properties.Name -contains 'InstallLocation' -and $entry.InstallLocation) { $dir = $entry.InstallLocation.Trim('"') }
    if (-not $dir) { $dir = Join-Path $env:ProgramFiles 'Clash Verge' }
    $version = '0'
    if ($entry.PSObject.Properties.Name -contains 'DisplayVersion' -and $entry.DisplayVersion) { $version = $entry.DisplayVersion }
    return [pscustomobject]@{ Version = $version; Exe = (Join-Path $dir 'clash-verge.exe') }
}

function Test-VersionAtLeast($a, $b) {
    try { return ([version]($a -replace '[^0-9.].*$', '')) -ge [version]$b } catch { return $false }
}

function Test-CvRunning { return [bool](Get-Process -Name 'clash-verge' -ErrorAction SilentlyContinue) }

function Hide-Url($url) { return ($url -replace '(/sub/[0-9a-f]{6})[0-9a-f]+', '$1…') }

# ---------------------------------------------------------------- check

function Invoke-Check {
    $Script:Blockers = 0; $Script:Warnings = 0
    Title "$Kit · Windows 环境检测（只读，不做任何修改）"

    Title '系统'
    $os = Get-CimInstance Win32_OperatingSystem
    $build = [int]$os.BuildNumber
    if ([Environment]::OSVersion.Version.Major -ge 10 -and $build -ge 17763) { Ok "$($os.Caption) (build $build)" }
    else { Bad "$($os.Caption) (build $build)：需要 Windows 10 1809 及以上" }
    $arch = Get-Arch
    if ($CvAssets.ContainsKey($arch)) { Ok "处理器架构：$arch" } else { Bad "不支持的处理器架构：$arch" }
    Info "PowerShell $($PSVersionTable.PSVersion)"
    $principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
    if ($principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        Warn '当前以管理员身份运行；建议普通方式运行，安装时会单独请求一次 UAC'
    } else { Ok '普通权限运行（安装时会弹出一次 UAC）' }
    $drive = Get-PSDrive -Name ($env:SystemDrive.TrimEnd(':')) -ErrorAction SilentlyContinue
    if ($drive -and $drive.Free -gt 1GB) { Ok '系统盘空间充足' } else { Warn '系统盘剩余不足 1 GiB' }

    Title 'Clash Verge Rev'
    $cv = Get-ClashVerge
    if ($cv) {
        if (Test-VersionAtLeast $cv.Version $CvVersion) { Ok "已安装 $($cv.Version)：$($cv.Exe)" }
        else { Warn "已安装 $($cv.Version)，低于脚本锁定的 $CvVersion；install 会升级" }
        if (Test-CvRunning) { Info 'Clash Verge Rev 正在运行' }
    } else { Info "未安装；install 会安装 $CvVersion" }
    if (Test-Path $CvProfiles) {
        $n = @(Select-String -Path $CvProfiles -Pattern '/sub/[0-9a-f]{48}' -ErrorAction SilentlyContinue).Count
        Info "已有订阅配置中，gowest 格式的订阅 $n 条"
    }
    $wv = $null
    foreach ($k in @('HKLM:\SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}',
                     'HKCU:\SOFTWARE\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}')) {
        if (Test-Path $k) { $wv = (Get-ItemProperty $k -ErrorAction SilentlyContinue).pv }
    }
    if ($wv) { Ok "WebView2 运行库 $wv" } else { Info '未检测到 WebView2；官方安装包会自动安装' }

    Title '其他代理客户端'
    $others = @(Get-UninstallEntries 'Clash for Windows|ClashN|Clash Nyanpasu|Mihomo Party|FlClash|v2rayN|NekoRay|Nekobox|Hiddify|sing-box|Shadowsocks|Netch')
    foreach ($o in $others) { Info "已安装：$($o.DisplayName)" }
    if ($others | Where-Object { $_.DisplayName -match 'Clash for Windows' }) {
        Warn 'Clash for Windows 的内核不支持本项目的 VLESS+REALITY / Hysteria2 节点，请改用 Clash Verge Rev，并退出 CFW'
    }
    $procs = @(Get-Process -ErrorAction SilentlyContinue | Where-Object {
        $_.ProcessName -match '^(Clash for Windows|clash-win64|clash|mihomo|clash-meta|v2rayN|xray|v2ray|sing-box|nekoray|nekobox|hiddify|FlClash|Shadowsocks|Netch)' -and
        $_.ProcessName -notmatch '^(clash-verge|verge-mihomo)' } | Select-Object -ExpandProperty ProcessName -Unique)
    if ($procs.Count -gt 0) { Warn "正在运行的其他代理程序：$($procs -join ', ') — 请只保留一个代理客户端运行" }
    elseif ($others.Count -eq 0) { Ok '未发现其他代理客户端' }

    Title '网络现状'
    $inet = Get-ItemProperty 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Internet Settings' -ErrorAction SilentlyContinue
    if ($inet -and ($inet.PSObject.Properties.Name -contains 'ProxyEnable') -and $inet.ProxyEnable -eq 1) {
        Warn "系统代理已开启：$($inet.ProxyServer)"
        Info '若指向已退出的旧客户端，会导致断网；可在“设置 → 网络和 Internet → 代理”关闭'
    } else { Ok '系统代理未开启' }
    if ($inet -and ($inet.PSObject.Properties.Name -contains 'AutoConfigURL') -and $inet.AutoConfigURL) { Warn "已设置 PAC 自动代理：$($inet.AutoConfigURL)" }
    $dns = @(Get-DnsClientServerAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue | ForEach-Object { $_.ServerAddresses } | Sort-Object -Unique)
    $tun = @(Get-NetAdapter -ErrorAction SilentlyContinue | Where-Object { $_.Status -eq 'Up' -and $_.InterfaceDescription -match 'Meta|Mihomo|Clash|wintun|sing-box' })
    if ($dns -match '^198\.18\.') {
        if ($tun.Count -gt 0) { Info "DNS 指向 198.18.x（当前有代理 TUN 在运行）：$($dns -join ' ')" }
        else { Warn "DNS 仍指向 198.18.x 但没有 TUN 在运行，属于旧客户端残留，会导致无法解析：$($dns -join ' ')" }
    } else { Ok "DNS：$($dns -join ' ')" }
    foreach ($t in $tun) { Info "检测到虚拟网卡：$($t.Name)（$($t.InterfaceDescription)）" }
    $listen = Get-NetTCPConnection -State Listen -LocalPort 7897 -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($listen) {
        $owner = (Get-Process -Id $listen.OwningProcess -ErrorAction SilentlyContinue).ProcessName
        if ($owner -match '^(clash-verge|verge-mihomo)') { Info "端口 7897 由 $owner 使用（属正常）" }
        else { Warn "端口 7897 已被 $owner 占用，Clash Verge Rev 的混合端口会冲突" }
    } else { Ok '端口 7897 空闲' }
    if (Get-Service -Name 'Tailscale' -ErrorAction SilentlyContinue) { Info '检测到 Tailscale：订阅已内置 Tailscale 直连例外' }
    try {
        $null = Invoke-WebRequest -Uri 'https://github.com' -Method Head -UseBasicParsing -TimeoutSec 10
        Ok 'GitHub 可访问（用于下载安装包）'
    } catch { Warn 'GitHub 当前不可访问或很慢；安装包下载可能失败，可稍后重试' }

    Title '结论'
    if ($Script:Blockers -gt 0) { Bad "有 $($Script:Blockers) 项阻止安装，请先处理"; return $false }
    if ($Script:Warnings -gt 0) { Warn "有 $($Script:Warnings) 项提示，建议看一下再继续" } else { Ok '环境正常' }
    return $true
}

# ---------------------------------------------------------------- install

function Invoke-Install {
    Title "安装 Clash Verge Rev $CvVersion"
    $arch = Get-Arch
    if (-not $CvAssets.ContainsKey($arch)) { Stop-Kit "不支持的处理器架构：$arch" }
    $cv = Get-ClashVerge
    if ($cv -and (Test-VersionAtLeast $cv.Version $CvVersion)) { Ok "已是 $($cv.Version)，无需安装"; return }
    if (Test-CvRunning) { Stop-Kit '请先在托盘图标里退出 Clash Verge，再重新运行 install' }

    $asset = $CvAssets[$arch]
    $work = Join-Path ([IO.Path]::GetTempPath()) ('gowest-' + [guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Path $work | Out-Null
    try {
        $file = Join-Path $work $asset.Name
        Info "下载 $($asset.Name)（约 60 MB，来自 GitHub 官方 Release）"
        Invoke-WebRequest -Uri "$CvBaseUrl/$($asset.Name)" -OutFile $file -UseBasicParsing -TimeoutSec 600
        $actual = (Get-FileHash -Algorithm SHA256 -Path $file).Hash.ToLowerInvariant()
        if ($actual -ne $asset.Sha) { Stop-Kit "SHA-256 不符（期望 $($asset.Sha)，实际 $actual），已拒绝安装" }
        Ok 'SHA-256 校验通过'
        Info '即将弹出 UAC，请选择“是”（官方安装包为全机安装）'
        $p = Start-Process -FilePath $file -ArgumentList '/S' -Verb RunAs -Wait -PassThru
        if ($p.ExitCode -ne 0) { Stop-Kit "安装程序退出码 $($p.ExitCode)" }
    } finally {
        Remove-Item -LiteralPath $work -Recurse -Force -ErrorAction SilentlyContinue
    }
    $cv = Get-ClashVerge
    if (-not $cv -or -not (Test-VersionAtLeast $cv.Version $CvVersion)) { Stop-Kit '安装后未检测到正确版本，请手动打开安装程序查看' }
    Ok "已安装 $($cv.Version)：$($cv.Exe)"
}

# ---------------------------------------------------------------- import

function Get-Subscription($url) {
    # Direct fetch (no system proxy), bounded size; gowest YAML only.
    Add-Type -AssemblyName System.Net.Http
    $handler = New-Object System.Net.Http.HttpClientHandler
    $handler.UseProxy = $false
    $client = New-Object System.Net.Http.HttpClient($handler)
    $client.Timeout = [TimeSpan]::FromSeconds(20)
    $client.MaxResponseContentBufferSize = 1048576
    try {
        $response = $client.GetAsync($url).GetAwaiter().GetResult()
        if (-not $response.IsSuccessStatusCode) { Stop-Kit "订阅返回 HTTP $([int]$response.StatusCode)" }
        $bytes = $response.Content.ReadAsByteArrayAsync().GetAwaiter().GetResult()
        return [Text.Encoding]::UTF8.GetString($bytes)
    } finally { $client.Dispose() }
}

function Test-ProfileHasUrl($url) {
    if (-not (Test-Path $CvProfiles)) { return $false }
    $pattern = '^\s*url:\s*[''"]?' + [regex]::Escape($url) + '[''"]?\s*$'
    return [bool](Select-String -Path $CvProfiles -Pattern $pattern -ErrorAction SilentlyContinue)
}

function Invoke-Import {
    Title '导入订阅到 Clash Verge Rev'
    $cv = Get-ClashVerge
    if (-not $cv) { Stop-Kit '还没有安装 Clash Verge Rev；先运行 install' }

    Write-Host ''
    Write-Host '在服务器上运行  sudo bash /opt/la-vpn/deploy.sh urls --client windows  查看订阅地址。'
    $line = Read-Host '请粘贴“日常分流订阅”地址（https://…/sub/ 加 48 位令牌）'
    $url = ($line -replace '^\s*(normal:)?\s*', '').Trim()
    if ($url -notmatch '^https?://[A-Za-z0-9.-]+(:[0-9]{1,5})?/sub/[0-9a-f]{48}(-us)?$') {
        Stop-Kit '地址格式不对。请完整粘贴 urls 命令输出的 https://…/sub/<48位令牌> 地址'
    }
    $base = $url -replace '-us$', ''
    if ($base -like 'http://*') { Warn '这是 HTTP 订阅，订阅内容（含节点凭据）会明文传输' }

    Info "检查订阅：$(Hide-Url $base)"
    try { $yaml = Get-Subscription $base } catch { Stop-Kit "无法下载订阅（$($_.Exception.Message)）；请确认地址正确、订阅端口已在云防火墙放行" }
    if ($yaml -notmatch '(?m)^proxies:' -or $yaml -notmatch '出口选择') { Stop-Kit '下载到的内容不是 gowest 订阅' }
    $label = 'gowest'
    $m = [regex]::Match($yaml, '(?m)^- name: ([A-Za-z][A-Za-z0-9-]{0,24})-Hy2\s*$')
    if ($m.Success) { $label = $m.Groups[1].Value }
    Ok "订阅可用，服务器标签：$label"

    $items = @(@{ Url = $base; Name = "gowest-$label-日常" })
    if (Confirm-Kit '同时导入“全公网”版（国内普通公网也走代理）？') {
        try { $null = Get-Subscription "$base-us" } catch { Stop-Kit '全公网订阅下载失败' }
        $items += @{ Url = "$base-us"; Name = "gowest-$label-全公网" }
    }

    if (-not (Test-CvRunning)) {
        Info '启动 Clash Verge Rev'
        if (-not (Test-Path $cv.Exe)) { Stop-Kit "找不到 $($cv.Exe)" }
        Start-Process -FilePath $cv.Exe | Out-Null
        for ($i = 0; $i -lt 15 -and -not (Test-CvRunning); $i++) { Start-Sleep -Seconds 1 }
        Start-Sleep -Seconds 3
    }

    foreach ($item in $items) {
        if (Test-ProfileHasUrl $item.Url) { Ok "$($item.Name) 已存在，跳过（避免重复）"; continue }
        # name must precede url: Clash Verge Rev reads everything after url= as the address.
        $link = 'clash://install-config?name=' + [uri]::EscapeDataString($item.Name) + '&url=' + [uri]::EscapeDataString($item.Url)
        Start-Process $link
        Info "已发送导入请求：$($item.Name)"
        Start-Sleep -Seconds 3
    }
    $imported = $false
    for ($i = 0; $i -lt 10; $i++) { if (Test-ProfileHasUrl $base) { $imported = $true; break }; Start-Sleep -Seconds 2 }
    if ($imported) { Ok '已写入 Clash Verge Rev 的订阅列表' } else { Warn '暂未在订阅列表中看到，请查看 Clash Verge Rev 窗口里的提示' }

    Title '接下来（需要你在 Clash Verge Rev 里手动操作）'
    Info "1. 订阅 页：点选 gowest-$label-日常 使其生效（只有一个订阅时会自动选中）"
    Info '2. 设置 页：打开“系统代理”；需要接管不走系统代理的应用时，再打开“虚拟网卡模式（TUN）”（需安装服务模式）'
    Info '3. 代理 页：出口选择 / AI / YouTube / TikTok 默认“自动回退”，可按需手动固定节点'
    Info '本脚本没有改动系统代理、DNS 或 TUN。'
}

function Show-Usage {
    Write-Host "$Kit Windows 客户端助手"
    Write-Host '用法：gowest-client.cmd [命令]'
    Write-Host '  check    只读检测环境（系统、已装客户端、系统代理/DNS/TUN 残留、端口、GitHub 连通）'
    Write-Host "  install  下载并校验 Clash Verge Rev $CvVersion 并安装（弹出一次 UAC）"
    Write-Host '  import   粘贴订阅地址，校验后自动导入 Clash Verge Rev'
    Write-Host '  all      依次执行 check → install → import（默认）'
}

try {
    switch ($Command) {
        'check'   { if (-not (Invoke-Check)) { exit 1 } }
        'install' { Invoke-Install }
        'import'  { Invoke-Import }
        'help'    { Show-Usage }
        'version' { Write-Host $Kit }
        'all' {
            if (-not (Invoke-Check)) { Stop-Kit '环境检测未通过' }
            if (-not (Confirm-Kit '继续安装/检查 Clash Verge Rev 并导入订阅？')) { Info '已取消，未做任何改动'; exit 0 }
            Invoke-Install
            Invoke-Import
        }
    }
} catch {
    Write-Host ''
    Write-Host "操作停止：$($_.Exception.Message)" -ForegroundColor Red
    exit 1
}
