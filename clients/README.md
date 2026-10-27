# 客户端一键脚本（Mac / Windows）

这两份脚本在**你自己的电脑**上运行，用来检测环境、安装 Clash Verge Rev，并导入 gowest 订阅。不要拿到服务器上跑。

| 平台 | 运行方式 | 客户端 |
| --- | --- | --- |
| macOS 11+（Apple 芯片 / Intel） | 双击 `macos/gowest-client.command`，或 `bash macos/gowest-client.sh` | Clash Verge Rev 2.5.8 |
| Windows 10 1809+ / 11（x64 / ARM64） | 双击 `windows/gowest-client.cmd` | Clash Verge Rev 2.5.8 |

两份脚本的命令相同：

- `check`：只读检测。检查系统版本和架构、是否已装 Clash Verge Rev 及版本、其他代理客户端（含 Clash for Windows）、系统代理/PAC 是否开着、DNS 是否残留 198.18.x、虚拟网卡、7897 端口占用、Tailscale、GitHub 能否访问。
- `install`：从 GitHub 官方 Release 下载**固定版本** 2.5.8，校验 SHA-256，不符就中止。Mac 上旧版会移到废纸篓，Windows 会弹出一次 UAC。
- `import`：粘贴订阅地址后，脚本先直连下载确认是 gowest 订阅，再通过 `clash://install-config` 链接导入；已导入过的地址会跳过。可选同时导入“全公网”版。
- `all`（默认）：依次执行 check → 确认 → install → import。

订阅地址在服务器上查看：

```sh
sudo bash /opt/la-vpn/deploy.sh urls --client mac       # 或 windows / android / shadowrocket
```

## 脚本不会做的事

- 不改系统代理、DNS、TUN、路由、防火墙或系统执行策略。系统代理和 TUN 请导入后在 Clash Verge Rev 的“设置”里手动打开。
- 脚本里不写死、不保存订阅地址；屏幕上只显示脱敏后的地址。
- 不删除文件，也不卸载其他代理客户端，只提示冲突。

## 注意

- **Clash for Windows 用不了本项目的节点**：它的内核不支持 VLESS+REALITY 和 Hysteria2，Windows 请改用 Clash Verge Rev。
- 升级 Clash Verge Rev 时要先在菜单栏/托盘里退出它。
- macOS 首次打开如提示“无法验证开发者”：系统设置 → 隐私与安全性 → 仍要打开。
- Windows 的 `.cmd` 只对这一次 PowerShell 进程使用 `-ExecutionPolicy Bypass`，不改系统策略；`.ps1` 必须保持“带 BOM 的 UTF-8”编码，否则 Windows PowerShell 5.1 会读错中文。
- 下载 GitHub 时需要能访问 GitHub；在国内可能很慢或失败，可稍后重试。
- 升级锁定版本时，需同时修改两个脚本中的版本号和四个 SHA-256。

## 手机端

- **iOS**：Shadowrocket，见 `docs/小火箭使用指南.md` 和 `urls --client shadowrocket` 的输出。
- **Android**：Clash Meta for Android（mihomo 内核），用 `urls --client android` 输出的 `clashmeta://` 链接在手机浏览器里打开，或手动粘贴订阅地址。旧版 Clash for Android 不支持本项目的节点。
