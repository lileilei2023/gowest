<p align="center">
  <img src="docs/images/promo.webp" alt="gowest：一条命令部署双协议代理，多人共用、流量配额" width="100%">
</p>

<p align="center">
  <img alt="version" src="https://img.shields.io/badge/version-0.5.9-6366f1">
  <img alt="platform" src="https://img.shields.io/badge/server-Ubuntu%2024.04%20amd64%20%7C%20arm64-e95420">
  <img alt="sing-box" src="https://img.shields.io/badge/sing--box-1.14.3-0ea5e9">
  <img alt="python" src="https://img.shields.io/badge/python-3.10%2B-3776ab">
  <img alt="tests" src="https://img.shields.io/badge/tests-232%20passed-22c55e">
  <img alt="clients" src="https://img.shields.io/badge/clients-macOS%20%7C%20Windows%20%7C%20Android%20%7C%20iOS-64748b">
</p>

<p align="center">
  <a href="#-快速开始">快速开始</a> ·
  <a href="#-效果预览">效果预览</a> ·
  <a href="#-多人共用与流量配额">多人共用</a> ·
  <a href="#-架构">架构</a> ·
  <a href="#-客户端下载与导入">客户端下载</a> ·
  <a href="#-使用场景">使用场景</a> ·
  <a href="docs/详细说明.md">详细说明</a> ·
  <a href="docs/快速部署.md">快速部署</a> ·
  <a href="CHANGELOG.md">更新日志</a>
</p>

---

**gowest** 是一个面向个人自建场景的代理部署工具。在一台**空白的 Ubuntu 24.04** 服务器上运行一次中文向导，它会：

- 安装经过 SHA-256 校验的 **sing-box**，同机提供 **Hysteria2（UDP）** 与 **VLESS + REALITY（TCP）** 两条线路（一份时用 443 端口，多份时每个订阅链接一个独立端口）；
- 为每台新服务器生成独立的密钥、证书和随机订阅令牌；
- 一次生成 **macOS / Windows / Android / iOS** 四端可用的订阅，并带好统一的分流代理组；
- 支持**几个人共用一台服务器**：份数由你决定，每份一个订阅链接；带宽按链接分配，忙时均分、闲时可以用满，也可以给单个链接设上限；并按**每月流量上限**提醒或暂停；
- 全程可回滚：冲突检测、安装保护定时器、失败自动回滚、改动指纹校验。

> [!NOTE]
> 本仓库是**通用复建源码**，不包含任何现有节点的密钥或订阅。顶部动画和下文的界面截图均为**示意图**：组名、节点名、向导提示和安装阶段与代码真实输出一致，IP、延迟等数值仅为演示。顶部动画里的带宽均分数字，以及"多人共用"一节的速度图表，都是**真实测量数据**。

## ✨ 特性

| | 功能 | 说明 |
| --- | --- | --- |
| 🚀 | **一条命令部署** | `sudo bash deploy.sh`：自动识别公网 IPv4，带宽只填一次（`30Mbps` / `1Gbps`），输入 `INSTALL` 才开始 |
| 🔀 | **同机双协议 + 自动回退** | Hy2 优先，健康检查失败时切到 VLESS；不会悄悄回落到直连 |
| 📱 | **四端订阅一次生成** | Clash Verge Rev、Clash Meta for Android 一键导入链接；Shadowrocket 原生节点 + 两份 `.conf` |
| 👥 | **按订阅链接分配带宽** | N 份 = N 个订阅链接；每个链接保底 1/N、空闲时借满，可单独设上限；同一设备开多个链接、开再多连接都不影响别人的份额 |
| 📊 | **月流量配额** | 填 `1TB` / `512GB` / `800`（只写数字按 GB），客户端订阅显示已用量；可选超额后自动暂停代理，下月或调高上限后自动恢复 |
| 🎚️ | **带宽在线调整** | `bandwidth --set 200Mbps` 或 `bandwidth --user bob --cap 20Mbps` 立即生效，不改密钥、不换订阅地址 |
| 🧭 | **统一代理组** | `出口选择 / 自动回退 / AI / Claude / YouTube / TikTok`，多台服务器只靠节点前缀（LA、DC…）区分 |
| 🛡️ | **事务式安装** | 只接受全新服务器；30 分钟保护定时器、失败回滚、可恢复归档，SSH 断线不影响安装 |
| ⚡ | **保守的网络优化** | BBR、只替换默认队列的安全 FQ、4/32 MiB 缓冲、TCP MTU 探测 |
| 🔥 | **防火墙辅助** | 安全新增 UFW 规则（不动 SSH、不清空规则），多份时按每个链接的端口放行；`firewall generate` 按实际端口生成云安全组模板，另附一份时可直接用的 HTTP/HTTPS 模板 |
| 🧪 | **可验证** | 离线配置检查、真实内核语法校验、回环协议测试、客户端出口 / STUN / 小流量测速工具 |

## 📸 效果预览

### 部署向导

在新服务器上运行向导，填写几项即可完成安装，8 个阶段都会显示耗时。一个人用时，"共用份数"和"流量上限"直接回车即可：

<p align="center"><img src="docs/images/wizard.png" alt="部署向导" width="92%"></p>

### 桌面客户端（Clash Verge Rev / mihomo）

导入后会看到两份订阅（日常分流 / 全公网）和一组统一的代理组。`Claude → AI → 自动回退 → DC-Hy2 / DC-VLESS`：

<p align="center"><img src="docs/images/clash.png" alt="桌面客户端代理组" width="92%"></p>

### 手机客户端

Android 在浏览器中点 `clashmeta://` 链接即可导入；iOS 使用 Shadowrocket 的节点订阅加分流配置：

<p align="center"><img src="docs/images/mobile.png" alt="Android 与 iOS" width="80%"></p>

## 👥 多人共用与流量配额

向导里的"共用份数"由执行部署的人决定：填份数（如 `3`），或填名字（如 `alice,bob:20Mbps,carol`），名字后面可以带上该份的上限。**一份对应一个订阅链接**：

- **每个链接一套独立凭据和独立端口**（默认从 20001 起，TCP 走 VLESS、UDP 走 Hy2）。链接可以单独发给每个人，泄露了也只影响那一份。
- **带宽跟着链接走，不跟设备走**：服务器出口建立一棵 HTB 分层队列（与 [LibreQoS](https://libreqos.readthedocs.io/en/latest/docs/v2.0/htb_fq_codel_cake.html) 相同的思路），每个链接一个类别。保底 `总速率 / N`，空闲时可以借用到总速率，或者借到这个链接自己的上限。类别内部再用 fq_codel 区分各个连接。总速率取套餐带宽的 95%，让排队发生在可控的位置。
- **Hy2 改用 BBR**：多份模式下，Hy2 从固定速率的 Brutal 改为 BBR，否则它会无视排队、挤占别人。
- **内核模块自动补齐**：需要的 `sch_htb` / `sch_fq_codel` / `cls_u32` 通常已在基础内核包里。如果没有，安装时会自动安装 `linux-modules-extra-$(uname -r)`；软件源里找不到这个包时，会添加 Ubuntu 官方源（回滚时一并移除）后再试。仍然不可用时，保持默认队列并在 `status` 里提示。

下面是在真实 Linux 内核里，用 sing-box 服务端和客户端实际跑出来的结果（隔离网络命名空间，总速率 95 Mbps）：

<p align="center"><img src="docs/images/fair-share.png" alt="按订阅链接分配实测" width="100%"></p>

| 场景 | 协议 | 结果（Mbps） |
| --- | --- | --- |
| 一个链接独占 | Hysteria2 | alice 86.2 |
| 三个链接同时下载 | Hysteria2 | alice 29.9 · bob 29.9 · carol 30.0 |
| 同一台设备（同一个 IP）上的两个链接 | VLESS | alice 45.2 · bob 45.2 |
| alice 开 4 个连接、bob 开 1 个，普通限速（对照） | VLESS | alice 89.2 · bob 1.4 |
| 同上，gowest 按链接分配 | VLESS | alice 46.0 · bob 44.4 |
| bob 上限 20 Mbps，单独下载 | Hysteria2 | bob 19.0 |
| bob 上限 20 Mbps，与 alice 同时下载 | Hysteria2 | alice 69.0 · bob 19.0 |
| 总速率在线改为 47 Mbps，三个链接同时下载 | Hysteria2 | alice 14.9 · bob 14.8 · carol 14.8 |
| 47 Mbps 时 carol 单独下载 | Hysteria2 | carol 44.5 |

**月流量配额**对一个人和多人都适用，因为云套餐是按整台服务器计量的：

```sh
# 安装后随时调整（需要确认服务器 IP，避免误操作）
sudo bash /opt/la-vpn/deploy.sh traffic --quota 1TB --on-exceed stop --confirm-server <服务器IP>
sudo bash /opt/la-vpn/deploy.sh bandwidth --set 200Mbps --confirm-server <服务器IP>
sudo bash /opt/la-vpn/deploy.sh bandwidth --user bob --cap 20Mbps --confirm-server <服务器IP>   # none 取消上限
sudo bash /opt/la-vpn/deploy.sh urls --user alice --client mac   # 查看某个链接的导入方式
```

- 单位：流量支持 `GB` / `TB`（十进制，1TB = 1000GB，多数云厂商按此计费）和 `GiB` / `TiB`（二进制），只写数字时按 GB；带宽支持 `Mbps` / `Gbps`（1Gbps = 1000Mbps），只写数字时按 Mbps。`MB/s` 这类字节单位会被拒绝，避免和 `Mbps` 混淆。
- `status` 会以 GB 显示本月用量，例如 `312.4 GB / 1000 GB（31.2%）`。
- 默认把进、出两个方向都算进用量（偏保守）。如果你的套餐只计出方向，加 `--metering outbound`。
- `--on-exceed warn` 只在订阅和 `status` 里显示用量；`stop` 会在达到上限时暂停代理，到下个月或调高上限后自动恢复。你自己手动停掉的服务，它不会去启动。
- 用量按自然月统计（vnStat），是整台服务器的总流量，不区分链接。

## 🏗 架构

<p align="center"><img src="docs/images/arch.png" alt="架构" width="100%"></p>

- 两种协议共用**同一台**服务器并直接出站。自动回退能应对单个协议故障，但不能应对整台服务器宕机。
- 自动回退是**故障切换**，不是挑最低延迟。
- 一份时 Hy2 保持 Brutal（按填写的带宽发送），端口为 443；多份时每个链接用独立端口，Hy2 改为 BBR，出口启用按链接分配的 HTB 队列。只有当出口是系统默认队列时才会替换；遇到你自己配置过的队列会跳过，并在 `status` 中提示。

## 🚀 快速开始

**准备：** 空白 Ubuntu 24.04（amd64/arm64）、systemd、≥ 512 MiB 内存、公网 IPv4。在云安全组放行 SSH、代理端口和订阅端口（HTTP 80 / HTTPS 8443）。代理端口一份时为 **TCP/UDP 443**；多份时为每个链接的端口（默认 **TCP/UDP 20001 起**，`firewall generate` 会生成对应模板）。

```sh
# 1. 把项目上传到新服务器
git clone https://github.com/lileilei2023/gowest.git && cd gowest

# 2. 运行向导（建议在 tmux 中运行）
sudo bash deploy.sh

# 3. 查看各客户端的导入方式
sudo bash /opt/la-vpn/deploy.sh urls --client mac        # 或 windows / android / shadowrocket
sudo bash /opt/la-vpn/deploy.sh urls --user alice --client mac   # 多份时指定是哪个链接
```

<details>
<summary><b>非交互用法 / 在本机预览配置</b></summary>

```sh
# 只生成参数文件，不安装（多人与流量上限可选）
bash deploy.sh init --server-ip <新服务器IP> --node-prefix DC --bandwidth 100Mbps \
  --users alice,bob:20Mbps,carol --monthly-quota 1TB --on-exceed stop \
  --accept-http-risk --output settings.local.json

# 查看计划，不联网、不做改动（会列出需要放行的端口）
bash deploy.sh plan --settings settings.local.json

# 按实际端口生成云安全组模板（SSH 来源换成你的管理出口）
bash deploy.sh firewall generate --settings settings.local.json \
  --ssh-source <你的管理IP>/32 --output firewall.local.json

# 在本地离线生成一套配置并检查（需要 Python 3.10+、OpenSSL、PyYAML）
python3 -B render.py --settings settings.local.json --output generated-review
python3 -B check.py --generated generated-review

# 在新服务器上安装
sudo bash deploy.sh install --settings settings.local.json --confirm-new-server <新服务器IP>
```

</details>

<details>
<summary><b>运维命令</b></summary>

```sh
sudo python3 -B /opt/la-vpn/vpn.py status     # 状态、BBR/FQ、按链接分配队列、本月流量用量
sudo python3 -B /opt/la-vpn/vpn.py verify     # 本机验收
sudo python3 -B /opt/la-vpn/vpn.py rollback   # 回滚（归档到 /var/lib/la-vpn-archives，可恢复）

# 客户端侧测速 / 出口核对
python3 -B probe.py --proxy http://127.0.0.1:7897 --expect-exit-ip <新服务器IP> --download-mib 8 --rounds 3
```

</details>

## 📥 客户端下载与导入

Mac / Windows 用户可以直接运行 `clients/` 里的一键脚本：脚本会下载下表中的同一版本，校验 SHA-256，然后导入订阅。想手动安装时，从下表的官方地址下载：

| 平台 | 客户端 | 下载（官方来源） | 导入方式 |
| --- | --- | --- | --- |
| macOS（Apple 芯片） | Clash Verge Rev 2.5.8 | [Clash.Verge_2.5.8_aarch64.dmg](https://github.com/clash-verge-rev/clash-verge-rev/releases/download/v2.5.8/Clash.Verge_2.5.8_aarch64.dmg) | 一键脚本 `clients/macos/gowest-client.command`，或打开 `urls --client mac` 输出的 `clash://` 链接 |
| macOS（Intel） | Clash Verge Rev 2.5.8 | [Clash.Verge_2.5.8_x64.dmg](https://github.com/clash-verge-rev/clash-verge-rev/releases/download/v2.5.8/Clash.Verge_2.5.8_x64.dmg) | 同上 |
| Windows（x64） | Clash Verge Rev 2.5.8 | [Clash.Verge_2.5.8_x64-setup.exe](https://github.com/clash-verge-rev/clash-verge-rev/releases/download/v2.5.8/Clash.Verge_2.5.8_x64-setup.exe) | 一键脚本 `clients/windows/gowest-client.cmd`，或 `urls --client windows` 输出的 `clash://` 链接 |
| Windows（ARM64） | Clash Verge Rev 2.5.8 | [Clash.Verge_2.5.8_arm64-setup.exe](https://github.com/clash-verge-rev/clash-verge-rev/releases/download/v2.5.8/Clash.Verge_2.5.8_arm64-setup.exe) | 同上 |
| Android | Clash Meta for Android | [GitHub Releases](https://github.com/MetaCubeX/ClashMetaForAndroid/releases/latest)：大多数手机选 `cmfa-*-meta-arm64-v8a-release.apk`，不确定时选 `universal` | 在手机浏览器中打开 `urls --client android` 输出的 `clashmeta://` 链接 |
| iOS | Shadowrocket（付费） | [App Store](https://apps.apple.com/us/app/shadowrocket/id932747118)（中国大陆区未上架，需要使用其他地区的 Apple ID） | 节点订阅 `-nodes`，加上 `-shadowrocket.conf` 或 `-us-shadowrocket.conf`，见 [小火箭使用指南](docs/小火箭使用指南.md) |

其他版本见 [Clash Verge Rev Releases](https://github.com/clash-verge-rev/clash-verge-rev/releases)。一键脚本锁定的是 2.5.8；如果你手动装了更新的版本，脚本会把它视为已安装，不会降级。

> [!WARNING]
> Clash for Windows、ClashX 和旧版 Clash for Android 的内核不支持 VLESS+REALITY 和 Hysteria2，无法使用。请只从上面的官方来源下载，不要用来路不明的"破解版"或镜像站安装包。

## 🎯 使用场景

两份订阅加上客户端开关，可以组合出四种常用场景（详见 [桌面使用指南](docs/桌面使用指南.md)）：

<p align="center"><img src="docs/images/scenes.png" alt="四种使用场景" width="100%"></p>

## 📂 项目结构

```text
gowest/
├── deploy.sh / launcher.py   # 中文向导与统一入口
├── vpn.py                    # plan / install / status / verify / urls / bandwidth / traffic / rollback
├── render.py                 # 每个订阅链接的独立密钥与端口、Clash YAML、小火箭配置
├── network.py                # BBR / FQ / 缓冲，以及按链接分配的 HTB 队列：快照、应用、恢复
├── subinfo.py                # 月流量统计、订阅用量显示、超额暂停与恢复
├── hostfirewall.py           # 保守新增 UFW 规则并支持回滚
├── firewall.py, firewall/    # 云安全组模板的生成与校验
├── check.py / probe.py       # 离线一致性检查；客户端出口与测速
├── clients/                  # Mac / Windows 一键安装与导入脚本
├── native/, build_native.py  # Nuitka 原生编译（交付时不带源码）
├── tests/                    # 232 个离线测试，以及 netns / 回环集成测试
└── docs/                     # 详细说明、快速部署、桌面 / 小火箭使用指南、原生编译
```

## 🧪 测试

```sh
python3 -B -m unittest discover -s tests -v
```

测试全部在离线或隔离环境中运行，不使用任何生产节点凭据。已验证与未验证的范围见 [更新日志 · 验证现状](CHANGELOG.md#验证现状)。

## ⚠️ 边界与安全

- **只支持全新服务器**：检测到已有 sing-box、代理或网站时会拒绝安装，也没有 `--force` 选项。
- **HTTP 订阅会明文传输凭据**，因此必须显式确认；HTTPS 需要自备可信证书（本版不自动申请或续期）。
- **云安全组无法自动验证**：本机能监听，不代表公网已经能访问。
- **分配以订阅链接为单位**：一个链接在多台设备上同时使用，仍然只算一份；想给某人更多份额，就多给他一个链接，或单独调整上限。
- **流量统计是整台服务器的**，不按链接计费；暂停是整台服务器的代理一起停。
- **按链接分配只作用于服务器发往客户端的方向**（也就是下载，这通常就是云套餐限速的方向）。客户端上传经服务器转发到外网的流量，走默认类别。
- 没有系统级 kill switch，也不承诺零泄漏、住宅 IP 或某个服务一定不会触发风控。
- 不要把带令牌的订阅地址贴到公开日志、仓库或在线订阅转换网站。

更多细节（参数说明、DNS/TUN 规则、FQ 与按链接分配策略、回滚语义）见 **[详细说明](docs/详细说明.md)**；各版本的改动见 [CHANGELOG](CHANGELOG.md)。

## 📜 免责声明

本项目仅供学习与研究网络技术使用。请遵守你所在地区的法律法规，以及云服务商和各网站的服务条款。
