<p align="center">
  <img src="docs/images/promo.webp" alt="gowest：几个人合租一台服务器，一条命令部署，按人管流量，本地网页管理" width="100%">
</p>

<p align="center">
  <img alt="version" src="https://img.shields.io/badge/version-0.9.0-6366f1">
  <img alt="platform" src="https://img.shields.io/badge/server-Ubuntu%2024.04%20amd64%20%7C%20arm64-e95420">
  <img alt="sing-box" src="https://img.shields.io/badge/sing--box-1.14.3-0ea5e9">
  <img alt="tests" src="https://img.shields.io/badge/tests-299%20passed-22c55e">
  <img alt="license" src="https://img.shields.io/badge/license-GPL--3.0-blue">
  <img alt="clients" src="https://img.shields.io/badge/clients-macOS%20%7C%20Windows%20%7C%20Android%20%7C%20iOS-64748b">
</p>

<p align="center">
  <a href="#-快速开始">快速开始</a> ·
  <a href="#-网页管理">网页能做什么</a> ·
  <a href="#-效果预览">效果预览</a> ·
  <a href="#-多人共用与流量配额">多人共用</a> ·
  <a href="#-客户端下载">客户端下载</a> ·
  <a href="docs/详细说明.md">详细说明</a> ·
  <a href="CHANGELOG.md">更新日志</a>
</p>

---

**gowest** 适合**几个人合租一台海外服务器**：在空白的 Ubuntu 24.04 上，用一条命令部署 **Hysteria2（端口跳跃）+ VLESS REALITY + TUIC + AnyTLS** 四协议代理，每人一个订阅链接，带宽公平分配，按月控制流量，四端订阅都已配好 AI / Claude 分流。一个人用也完全可以。

- **一个本地网页搞定**：在自己电脑上打开管理页面，点几下完成安装、导入和日常配置；也可以用一条命令；失败自动回滚。
- **晚高峰更稳**：Hy2 端口跳跃绕开运营商对单个 UDP 端口的限速；同机四种协议（Hy2、VLESS REALITY、TUIC、AnyTLS）自动回退，任何时候都不会悄悄走直连。
- **线路体检**：点一下就能看到你和服务器之间走的是 CN2 GIA、9929 还是普通线路，以及延迟、丢包和下载速度。
- **多人共用**：每人一个订阅链接；忙时均分，闲时拉满；随时加人、删人、换链接，其他人不受影响。
- **按人管流量**：每个链接可以单独设速度上限、月流量额度和到期日，超了只停那一个人。
- **解锁检测**：点一下就能看这台服务器能不能用 Claude、ChatGPT、TikTok、YouTube。

## 🚀 快速开始

**1. 准备服务器**：一台空白的 Ubuntu 24.04，能用 SSH 密钥登录。在云控制台的安全组放行：

| | 需要放行 |
| --- | --- |
| 一个人用 | TCP/UDP 443、TCP/UDP 2443、UDP 24576-24639、TCP 80 |
| 多人共用 | TCP/UDP 20001-27000、TCP 80（一次开好，以后加人不用再改） |

安装时如果关掉"稳定性增强"（端口跳跃和备用协议），只需要 TCP/UDP 443（多人 20001-20032）和 TCP 80。

**2. 在你自己的电脑上打开管理页面**：

```sh
git clone https://github.com/lileilei2023/gowest.git && cd gowest
./remote.sh ui          # Windows：python gowest_ui.py
```

**3. 在页面里点三下**：

1. **添加服务器**：填 `root@服务器IP`。
2. **开始安装**：填套餐带宽和人数（可选：每月流量上限），安装进度会实时显示，大约 2–5 分钟。
3. **导入**：点每个人旁边的二维码，手机对着屏幕扫一下；电脑点"导入日常分流"。

之后加人、删人、改额度、检查连通、解锁检测和升级，都在这个页面里完成。客户端去哪下载，见 [客户端下载](#-客户端下载)。

<p align="center"><img src="docs/images/webui.png" alt="本地网页管理" width="100%"></p>

<details>
<summary><b>更喜欢命令行？</b></summary>

页面上的每个按钮都对应一条命令，效果完全相同：

```sh
./remote.sh root@<服务器IP> quick 100Mbps --users 3   # 零问答安装（一个人用时去掉 --users）
./remote.sh root@<服务器IP>                           # 或者用中文向导逐项确认
./remote.sh root@<服务器IP> urls --client all --qr    # 订阅地址和二维码
./remote.sh root@<服务器IP> check                     # 从这台电脑检查端口和订阅
git pull && ./remote.sh root@<服务器IP> upgrade       # 升级：密钥、端口和订阅地址都不变
```

Windows 上没有 `sh`：用网页，或者 SSH 登录服务器后在服务器上 `git clone`，再运行 `sudo bash deploy.sh`。非交互安装、本机预览配置、安全组模板见 [快速部署](docs/快速部署.md)。

</details>

## 🖥 网页管理

| 你想做的事 | 在网页里 | 对应的命令 |
| --- | --- | --- |
| 管理多台服务器 | 左侧"添加服务器" | — |
| 安装 | 未安装的服务器上"开始安装" | `quick 100Mbps --users 3` |
| 看状态和用量 | 服务器首页：流量、带宽、链接数、服务，每个人的用量 | `status`、`users` |
| 手机、电脑导入 | 每个人旁边的二维码按钮 | `urls --qr` |
| 加人 / 删人 / 换链接 | "添加链接"，每行的"⋯"菜单 | `users add / remove / reset` |
| 每人速度上限、流量额度、到期日 | 每行的编辑按钮 | `users set` |
| 整台服务器的带宽、月流量、计量方式 | 首页卡片右上角的编辑按钮 | `bandwidth`、`traffic` |
| 检查端口、解锁检测 | 右上角按钮 | `check`、`unlock` |
| 线路体检 | 右上角"线路体检" | `linecheck` |
| 端口跳跃、备用协议、Hy2 混淆 | "线路与协议"卡片里的开关 | `options --hopping on --protocols tuic,anytls` |
| 升级 | 右上角"升级"（有新版本时会提示） | `upgrade` |
| 卸载（可恢复） | 右上角"⋯"→ 卸载，需输入 IP 确认 | `rollback` |

只能在命令行里做的：HTTPS 订阅（需要自己的域名证书）、Tailscale 例外等高级安装选项，用 `./remote.sh root@<服务器IP>` 的中文向导。

<p align="center"><img src="docs/images/webui-qr.png" alt="扫码导入" width="72%"></p>

**它和 3x-ui、Marzban 这类面板的区别**：那些面板运行在服务器上，要对公网开一个管理端口。gowest 的页面只运行在你的电脑上，所有操作都通过你自己的 SSH 密钥执行，服务器上不开任何新端口，也不常驻面板程序。页面只接受本机访问，地址里带一次性令牌，关掉终端就停止。

前提是登录用户是 root，或者可以免密码 sudo。服务器列表保存在 `~/.gowest/servers.json`，只有你自己能读。想先看看效果，可以运行 `./remote.sh ui --demo`，用示例数据打开页面，不连接任何服务器。截图中的数据为示意。

## 📸 效果预览

**命令行向导**：不用网页时，一个人用的话"共用份数"和"流量上限"直接回车即可。

<p align="center"><img src="docs/images/wizard.png" alt="部署向导" width="92%"></p>

**桌面客户端**：两份订阅（日常分流 / 全公网），统一的 `AI / Claude / YouTube / TikTok` 代理组。

<p align="center"><img src="docs/images/clash.png" alt="桌面客户端代理组" width="92%"></p>

**手机客户端**：Android 点链接一键导入；iOS 用 Shadowrocket。

<p align="center"><img src="docs/images/mobile.png" alt="Android 与 iOS" width="80%"></p>

## 👥 多人共用与流量配额

向导里的"共用份数"填 `3` 或 `alice,bob,carol`，就会生成 3 个订阅链接，分给 3 个人：

- 每个链接有独立的密钥和端口，泄露了只影响那一份；
- 带宽按链接分配：一个人用时拉满，几个人同时用时均分，开多少连接都抢不走别人的份额；
- 每个链接可以单独设速度上限、月流量额度和到期日，超额或到期只停那一个人，其他人不受影响。

下图是真实内核上的实测结果（总速率 95 Mbps）：

<p align="center"><img src="docs/images/fair-share.png" alt="按订阅链接分配实测" width="100%"></p>

装好之后在网页的"订阅链接"表里随时管理，立即生效，其他人的订阅地址不变。命令行的等价写法：

<details>
<summary><b>命令行</b></summary>

```sh
./remote.sh root@<服务器IP> users                                    # 每个人的端口、上限、本月用量、到期日、状态
./remote.sh root@<服务器IP> users add dave --quota 100 --expire 2026-12-31
./remote.sh root@<服务器IP> users set bob --cap 20Mbps --quota 200    # 速度上限、月流量额度
./remote.sh root@<服务器IP> users reset bob                          # 链接泄露了：只换他一个人的
./remote.sh root@<服务器IP> users remove bob
./remote.sh root@<服务器IP> bandwidth --set 200Mbps                  # 整台服务器的带宽
./remote.sh root@<服务器IP> traffic --quota 1TB --on-exceed stop     # 整台服务器的月流量
```

</details>

- 每人的用量按他自己的上传加下载统计，订阅里显示的是他自己的用量和额度。整台服务器的用量另外统计，大约是所有人用量之和的两倍，因为服务器收发各算一次，云厂商一般按这个计费。
- 额度和到期日用 `0` 或 `none` 取消。超额的人下个月自动恢复；调高额度后几分钟内恢复。
- 只写数字时，流量按 GB、带宽按 Mbps 计算。实现原理见 [详细说明](docs/详细说明.md)。

## 🛡 稳定性与防泄漏

<p align="center"><img src="docs/images/webui-line.png" alt="线路与协议、线路体检" width="100%"></p>

- **四种协议、两个端口**：每个订阅链接在主端口上提供 VLESS REALITY（TCP）和 Hysteria2（UDP），在第二个端口上提供 AnyTLS（TCP）和 TUIC（UDP）。客户端的"自动回退"按 Hy2 → VLESS → TUIC → AnyTLS 的顺序选第一个可用的。
- **端口跳跃**：Hy2 每 30 秒换一个 UDP 端口（每人 64 个端口一组），运营商针对单个端口的晚高峰限速就失效了。跳跃的流量照样计入这个人的用量，也照样参与带宽均分。
- **绝不直连**：自动回退组和 AI、Claude、YouTube、TikTok 组里都没有 DIRECT。所有节点都不可用时，请求会被拒绝，不会悄悄改走你自己的网络。DIRECT 只出现在最顶层的"出口选择"里，由你自己手动选择。
- **证书固定**：Hy2、TUIC、AnyTLS 都固定了服务器证书的指纹，证书被替换就拒绝连接，不存在"跳过校验"带来的中间人风险。小火箭不支持固定 TUIC 和 AnyTLS 的证书，所以小火箭只使用 Hy2 和 VLESS。
- **DNS 不泄漏**：除国内域名外，DNS 查询都通过代理走加密 DoH。
- **服务器端只动自己的东西**：端口跳跃的转发规则和按人统计都放在 gowest 自己的 nftables 表里，不改 UFW 或你自己的规则，卸载时一起删除。

## 🧭 Claude / TikTok 能不能用

在网页右上角点"解锁检测"，或运行 `./remote.sh root@<服务器IP> unlock`。在服务器上检测出口 IP 的国家和运营商，以及 Claude、ChatGPT、TikTok、YouTube 能否正常使用。所有客户端都从这个 IP 出去，所以结果对每个人都一样。建议买服务器后先测一下再装。检测依据是各网站公开的返回特征，网站调整后可能失准。

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
./remote.sh root@<服务器IP> status     # 版本、运行状态、队列、本月流量、各链接用量
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
├── gowest_ui.py, ui/         # 本地网页管理（只在自己电脑上运行，经 SSH 操作服务器）
├── localcheck.py             # 从自己电脑检查端口和订阅是否通
├── deploy.sh / launcher.py   # 中文向导与统一入口
├── vpn.py                    # plan / install / upgrade / status / verify / urls / users / unlock / bandwidth / traffic / rollback
├── accounting.py             # 每个链接的流量计数、超额或到期暂停（nftables）
├── unlock.py                 # Claude / ChatGPT / TikTok / YouTube 解锁检测
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
- 每个链接的用量按端口统计，服务器重启后的头一两分钟不计入。整台服务器的流量上限和暂停作用于所有人。
- 一个人用的安装（端口 443）不能事后加人；要多人共用请在安装时就选 2 份或以上。
- 本仓库不含任何节点的密钥或订阅。顶部动画和界面截图为示意图，IP、延迟等数值仅作演示；带宽实测图是真实数据。

## 📄 许可证

[GPL-3.0](LICENSE)：可以自由使用、修改和分发；分发修改后的版本时，也需要以 GPL-3.0 开源。网页里内置的二维码库 [qrcode-generator](https://github.com/kazuhikoarase/qrcode-generator) 使用 MIT 许可证。

本项目仅供学习与研究网络技术使用，请遵守当地法律法规和服务条款。
