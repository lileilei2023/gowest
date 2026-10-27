<p align="center">
  <img src="docs/images/promo.webp" alt="gowest：一条命令部署双协议代理，多人共用、流量配额" width="100%">
</p>

<p align="center">
  <img alt="version" src="https://img.shields.io/badge/version-0.6.0-6366f1">
  <img alt="platform" src="https://img.shields.io/badge/server-Ubuntu%2024.04%20amd64%20%7C%20arm64-e95420">
  <img alt="sing-box" src="https://img.shields.io/badge/sing--box-1.14.3-0ea5e9">
  <img alt="tests" src="https://img.shields.io/badge/tests-242%20passed-22c55e">
  <img alt="clients" src="https://img.shields.io/badge/clients-macOS%20%7C%20Windows%20%7C%20Android%20%7C%20iOS-64748b">
</p>

<p align="center">
  <a href="#-快速开始">快速开始</a> ·
  <a href="#-效果预览">效果预览</a> ·
  <a href="#-多人共用与流量配额">多人共用</a> ·
  <a href="#-客户端下载">客户端下载</a> ·
  <a href="docs/详细说明.md">详细说明</a> ·
  <a href="CHANGELOG.md">更新日志</a>
</p>

---

**gowest** 适合**几个人合租一台海外服务器**：在空白的 Ubuntu 24.04 上，用一条命令部署 **Hysteria2 + VLESS REALITY** 双协议代理，每人一个订阅链接，带宽公平分配，按月控制流量，四端订阅都已配好 AI / Claude 分流。一个人用也完全可以。

- **一条命令**：在自己电脑上运行，中文向导，几个问题就装好；失败自动回滚；升级也是一条命令。
- **双线路互备**：Hy2 优先，故障时自动切到 VLESS，不会悄悄走直连。
- **多人共用**：分成几份由你决定，每份一个订阅链接；忙时均分，闲时拉满。
- **月流量配额**：填 `1TB` 或 `512GB`，订阅里显示用量，超额可自动暂停。

## 🚀 快速开始

**1. 准备服务器**：空白 Ubuntu 24.04，在云控制台的安全组放行 **TCP/UDP 443** 和 **TCP 80**（多人共用时端口不同，向导会列出）。

**2. 在你自己的电脑上运行**（会通过 SSH 把代码传到服务器并打开向导，服务器上不需要 git）：

```sh
git clone https://github.com/lileilei2023/gowest.git && cd gowest
./remote.sh root@<服务器IP>
```

**3. 导入客户端**：安装完成后屏幕上会显示订阅地址，也可以随时查看：

```sh
./remote.sh root@<服务器IP> urls --client mac   # 或 windows / android / shadowrocket
```

**以后升级**：拉取最新代码后一条命令，密钥、端口和订阅地址都不变，客户端无需重新导入：

```sh
git pull && ./remote.sh root@<服务器IP> upgrade
```

Windows 电脑没有 `sh` 时，可以 SSH 登录服务器后在服务器上 `git clone`，再运行 `sudo bash deploy.sh`（升级用 `sudo bash deploy.sh upgrade --confirm-server <服务器IP>`）。

客户端去哪下载，见 [客户端下载](#-客户端下载)。更多方式（非交互安装、本机预览配置、安全组模板）见 [快速部署](docs/快速部署.md)。

## 📸 效果预览

**部署向导**：一个人用时，"共用份数"和"流量上限"直接回车即可。

<p align="center"><img src="docs/images/wizard.png" alt="部署向导" width="92%"></p>

**桌面客户端**：两份订阅（日常分流 / 全公网），统一的 `AI / Claude / YouTube / TikTok` 代理组。

<p align="center"><img src="docs/images/clash.png" alt="桌面客户端代理组" width="92%"></p>

**手机客户端**：Android 点链接一键导入；iOS 用 Shadowrocket。

<p align="center"><img src="docs/images/mobile.png" alt="Android 与 iOS" width="80%"></p>

## 👥 多人共用与流量配额

向导里的"共用份数"填 `3` 或 `alice,bob,carol`，就会生成 3 个订阅链接，分给 3 个人：

- 每个链接有独立的密钥和端口，泄露了只影响那一份；
- 带宽按链接分配：一个人用时拉满，几个人同时用时均分，开多少连接都抢不走别人的份额；
- 可以给某个链接单独设上限，例如 `bob:20Mbps`。

下图是真实内核上的实测结果（总速率 95 Mbps）：

<p align="center"><img src="docs/images/fair-share.png" alt="按订阅链接分配实测" width="100%"></p>

装好之后也可以随时调整，立即生效，订阅地址不变：

```sh
./remote.sh root@<服务器IP> bandwidth --set 200Mbps
./remote.sh root@<服务器IP> bandwidth --user bob --cap 20Mbps
./remote.sh root@<服务器IP> traffic --quota 1TB --on-exceed stop
```

只写数字时，流量按 GB、带宽按 Mbps 计算。实现原理和计量口径见 [详细说明](docs/详细说明.md)。

## 📥 客户端下载

Mac / Windows 也可以直接运行 `clients/` 里的一键脚本，自动下载、校验并导入订阅。

| 平台 | 客户端 | 官方下载 |
| --- | --- | --- |
| macOS | Clash Verge Rev 2.5.8 | [Apple 芯片](https://github.com/clash-verge-rev/clash-verge-rev/releases/download/v2.5.8/Clash.Verge_2.5.8_aarch64.dmg) · [Intel](https://github.com/clash-verge-rev/clash-verge-rev/releases/download/v2.5.8/Clash.Verge_2.5.8_x64.dmg) |
| Windows | Clash Verge Rev 2.5.8 | [x64](https://github.com/clash-verge-rev/clash-verge-rev/releases/download/v2.5.8/Clash.Verge_2.5.8_x64-setup.exe) · [ARM64](https://github.com/clash-verge-rev/clash-verge-rev/releases/download/v2.5.8/Clash.Verge_2.5.8_arm64-setup.exe) |
| Android | Clash Meta for Android | [GitHub Releases](https://github.com/MetaCubeX/ClashMetaForAndroid/releases/latest)（一般选 `arm64-v8a`，不确定选 `universal`） |
| iOS | Shadowrocket（付费） | [App Store](https://apps.apple.com/us/app/shadowrocket/id932747118)（需非中国大陆区 Apple ID），见 [小火箭使用指南](docs/小火箭使用指南.md) |

> [!WARNING]
> Clash for Windows、ClashX 和旧版 Clash for Android 不支持这两种协议，无法使用。请只从上面的官方地址下载。

## 🎯 使用场景

两份订阅加上客户端开关，可以组合出四种常用用法（详见 [桌面使用指南](docs/桌面使用指南.md)）：

<p align="center"><img src="docs/images/scenes.png" alt="四种使用场景" width="100%"></p>

## 🏗 架构

<p align="center"><img src="docs/images/arch.png" alt="架构" width="100%"></p>

<details>
<summary><b>常用运维命令</b></summary>

```sh
./remote.sh root@<服务器IP> status     # 版本、运行状态、队列、本月流量
./remote.sh root@<服务器IP> verify     # 本机验收
./remote.sh root@<服务器IP> rollback   # 回滚（可恢复）
```

在服务器上直接操作时，把 `./remote.sh root@<服务器IP>` 换成 `sudo bash /opt/la-vpn/deploy.sh`；改带宽、流量和升级还要加 `--confirm-server <服务器IP>`。

</details>

<details>
<summary><b>开发与测试</b></summary>

```text
gowest/
├── remote.sh                 # 在自己电脑上运行：上传、安装、升级、管理
├── deploy.sh / launcher.py   # 中文向导与统一入口
├── vpn.py                    # plan / install / upgrade / status / verify / urls / bandwidth / traffic / rollback
├── render.py                 # 密钥、端口、Clash YAML、小火箭配置
├── network.py                # BBR / FQ 与按链接分配的 HTB 队列
├── subinfo.py                # 月流量统计、超额暂停与恢复
├── hostfirewall.py           # UFW 规则（可回滚）
├── firewall.py, firewall/    # 云安全组模板
├── check.py / probe.py       # 离线一致性检查；客户端测速
├── clients/                  # Mac / Windows 一键脚本
├── native/, build_native.py  # Nuitka 原生编译
├── tests/                    # 离线测试与隔离集成测试
└── docs/                     # 详细说明与使用指南
```

```sh
python3 -B -m unittest discover -s tests -v
```

已验证和未验证的范围见 [更新日志 · 验证现状](CHANGELOG.md#验证现状)。

</details>

## ⚠️ 须知

- 只支持**全新服务器**，检测到已有代理或网站时会拒绝安装。
- HTTP 订阅会明文传输凭据，安装时需要确认；不要把订阅地址贴到公开的地方。
- 云安全组需要你自己放行，工具无法替你验证公网是否可达。
- 流量统计和超额暂停作用于整台服务器，不区分链接。
- 本仓库不含任何节点的密钥或订阅。顶部动画和界面截图为示意图，IP、延迟等数值仅作演示；带宽实测图是真实数据。

本项目仅供学习与研究网络技术使用，请遵守当地法律法规和服务条款。
