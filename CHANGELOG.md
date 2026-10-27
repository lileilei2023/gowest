# 更新日志

只记录对使用者有影响的变化。服务器内部路径 `/opt/la-vpn`、`/etc/la-vpn`、`/var/lib/la-vpn-deploy` 和 `la-vpn*` systemd 服务名为兼容旧部署而保留，各版本均未改动。

## 0.5.7 · 2026-10-27

- 项目名、CLI、原生可执行文件和包内目录统一为 `gowest`。
- 新生成的配置名为 `gowest-<节点标签>-日常/全公网`；客户端脚本改名为 `gowest-client.*`。
- 不迁移已有服务器，不更换订阅 URL。
- 186 项测试通过。

## 0.5.6 · 2026-10-27

- 向导和 `init` 默认使用 HTTP 订阅，但仍须确认明文风险；四端配置始终全部生成。
- 安装分为八个阶段，显示时间戳和耗时，长任务每 15 秒报告一次进度。
- 新增私有的 `install.log` 和脱敏的 `command-errors.log`。
- 命令超时或被中断时，先终止整个子进程组再回滚。
- 新增 Nuitka 原生编译（`build_native.py`）：原生安装只调用二进制，不把项目源码复制到服务器。目前只构建了 Mac arm64 版，Linux 版尚未构建和验收，见 [原生编译与交付](docs/原生编译与交付.md)。

## 0.5.5 · 2026-10-27

- 带宽通常只需填一次：可以写 `30`、`30Mbps` 或 `1Gbps`（按十进制换算为 1000 Mbps），同一个值同时用作 Hy2 上下行目标。
- 输入 `advanced` 或使用 `--up/--down`，可以分别填写上下行。
- 拒绝 `MB/s` 这类字节单位以及小数 Mbps。

## 0.5.4 · 2026-10-27

- 自动识别公网 IPv4：至少两个来源结果一致才采用，失败或结果冲突时要求手动填写。
- 订阅支持 HTTPS、HTTP 或两者同时提供；`urls --transport all|http|https`。
- 新机安装时会保守地新增 UFW 规则：不动 SSH，不清空规则，不启停防火墙，并且可以回滚。`install --host-firewall check` 只检查不修改，`firewall inspect` 只读查看。
- 云安全组不会自动修改，安装结束时提示"未验证"。
- 修正 Ubuntu 24.04 上 OpenSSL `-checkhost` 匹配失败时仍返回 0 的问题。

## 0.5.3 · 2026-10-26

- 修正 Hy2 带宽格式：`10.0 Mbps` 会被 Mihomo 忽略，现在只输出整数 Mbps。
- 日常分流版 DNS 改为默认走代理 DoH，只有国内域名使用国内 DoH。
- 安装中 SSH 断线不再中断事务，之后的输出写入 root 私有日志。
- 新增四端导入说明：`urls --client mac|windows|android|shadowrocket`，提供 `clash://` 和 `clashmeta://` 一键导入链接。
- 新增 Mac / Windows 一键脚本 `clients/`：检测环境、安装 Clash Verge Rev（固定版本并校验 SHA-256）、导入订阅。
- 补齐 Windows / Android 上的 Tailscale 进程直连例外。

## 0.5.2 · 2026-10-26

- 项目更名（当时名为 GoToWest），新增 `deploy.sh --version`。

## 0.5.1 · 2026-10-26

- 小火箭成为正式支持的目标：原生节点订阅加日常 / 全公网两份 `.conf`。
- 生成时静态检查分组绑定；新增 `urls --client shadowrocket` 和每台服务器专属的导入说明。

## 0.5.0 · 2026-10-26

- 代理组统一为 `出口选择 / 自动回退 / AI / Claude / YouTube / TikTok`，不同服务器只靠节点前缀（`LA-Hy2`、`DC-VLESS`）区分。
- 各组默认使用同机自动回退：Hy2 优先，VLESS 备用，不包含 DIRECT。
- 新增 `standardize.py`，可离线把已有的双协议 YAML 转换为统一格式。

## 0.4.0 · 2026-10-26

- 客户端 YAML 改由 PyYAML 生成常规 block 格式。
- 新增 `-nodes` 原生节点订阅和两份小火箭 `.conf`。
- 新增 AI / YouTube / TikTok 分组及对应的 DNS 策略。域名规则内嵌在代码里，来源见 [RULE-SOURCES.md](RULE-SOURCES.md)。

## 0.3.1 · 2026-10-26

- FQ 生命周期：使用确定句柄、通过回滚凭据授权、校验队列拓扑，自定义队列一律不替换；新增 `repair-fq`。
- `probe.py` 新增上传测速、1～4 路并发下载和目标利用率。

## 0.3.0 · 2026-10-26

- 新增 `firewall.py`：生成和校验云安全组模板，并附带 HTTP 80 / HTTPS 8443 两份现成模板。
- 新增 `init --node-prefix`，可自定义节点标签。
- `probe.py` 支持多轮测试、指定间隔和预期出口 IP 校验。

## 0.2.0 · 2026-10-25

- 新增中文向导 `deploy.sh`、`init` 和 `urls` 命令。
- 修复实装中发现的两个问题：APT 的 needrestart 会中断安装器；Nginx 沙箱内无法创建临时目录。
- 已在一台全新的 Ubuntu 24.04 amd64 服务器上完成真实安装，并验证双协议 HTTP/UDP 正常。

## 验证现状

已经验证过的：
- 离线 / 模拟测试，覆盖安装事务、回滚、防火墙、带宽和订阅生成。
- 回环环境下的真实 sing-box + Mihomo 测试：Hy2 / VLESS 的 HTTP 和 UDP、故障回退、双节点失效时拒绝请求、错误证书指纹时拒绝连接。
- 隔离网络命名空间中的真实 tc / FQ 测试，以及真实 Nginx 双入口测试。

尚未验证的：
- Linux 原生包的构建与安装。
- 真实 UFW 写入和云安全组生效。
- 系统重启后的恢复。
- Windows / Android / iOS 实机导入，以及 Mac 一键脚本的实机运行。
- 长期线路稳定性、IPv6 / WebRTC 是否完整无泄漏。
