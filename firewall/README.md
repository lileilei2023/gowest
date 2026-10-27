# UCloud 防火墙导入模板

模板沿用用户导出 JSON 的 `data` 数组和五个中文字段；协议值为 `TCP:22` / `UDP:443` / `ICMP`。仅提供 IPv4 入站允许规则，不调用云 API、不修改 UFW、不更改 SSH 服务。

## 选择一个模板

| 用途 | 文件 | 放行的协议/端口 |
| --- | --- | --- |
| 一个人用，HTTP 订阅（默认） | `Firewall-Rule-Template-json.json` | TCP 22、80、443、2443；UDP 443、2443、24576-24639；ICMP |
| 一个人用，HTTPS 订阅 | `Firewall-Rule-Template-https-json.json` | TCP 22、8443、443、2443；UDP 443、2443、24576-24639；ICMP |
| 多人共用（最多 32 人） | `Firewall-Rule-Template-multi-json.json` | TCP 22、80；TCP/UDP 20001-27000；ICMP |

单人模板按新安装的默认配置生成，包含端口跳跃和备用协议（TUIC、AnyTLS）。安装时如果关掉了这两项，可以在控制台删掉 2443 和 24576-24639 这三条，也可以留着，不影响使用。

多人模板一次放行整段端口，覆盖 32 个订阅链接的主端口（20001 起）、备用端口（21001 起）和跳跃端口（24576-26623），以后加人、开关端口跳跃或备用协议都不用再改安全组。代价是放行的范围比实际用到的大，范围内没有程序监听的端口不会被访问到。想精确放行时，用下面的 `firewall generate` 按实际端口生成。

所有模板都是"接受 / 高优先级 / 0.0.0.0/0"，适合首次部署。它们不包含服务器 IP、节点密钥、订阅令牌或设备地址。HTTPS 模板只放行端口，不负责申请或续期证书。

- TCP 22：SSH。稳定后建议限定为可信管理出口 IP/32 或网段；公网开放并不因免密登录就没有风险。收窄前保留第二个 SSH 会话和云控制台救援入口，使用真实管理出口而不是随意照抄示例 IP。`--ssh-port` 只描述规则，不修改 sshd。
- TCP 443：VLESS/REALITY。它不是 Nginx HTTPS 订阅端口。
- UDP 443：Hy2。不能用 TCP 443 的连通性证明此端口正常。
- TCP / UDP 2443：备用协议 AnyTLS（TCP）和 TUIC（UDP）。
- UDP 24576-24639：Hy2 端口跳跃。这一段没放行时，Hy2 会时通时断，客户端会自动回退到其他协议。
- TCP 80 或 8443：订阅下载。HTTP 会明文传输节点凭据；长期建议 HTTPS。
- ICMP：连通性诊断及路径 MTU 相关反馈；ping 不回应也可能是过滤，不能单独证明断线。
- 移除 TCP 3389：本 Ubuntu VPN 栈不需要 RDP。作为客户端做直播，也不需要在服务器额外开放入站 1935 等推流端口，除非另行部署接收推流的服务。

## 根据参数生成并检查（推荐）

先在工程根目录用 `deploy.sh init` 创建新机参数，然后执行：

```sh
bash deploy.sh firewall generate --settings settings.local.json \
  --ssh-source YOUR_MANAGEMENT_IP/32 --output firewall.local.json
bash deploy.sh firewall check --settings settings.local.json --file firewall.local.json
```

将 `YOUR_MANAGEMENT_IP` 替换成真实可信的管理出口；可重复指定 `--ssh-source`。CIDR 必须无主机位，例如 `198.51.100.7/24` 会报错，不会自动扩大为 `/24`。

确需首次部署时全网 SSH：

```sh
bash deploy.sh firewall generate --settings settings.local.json \
  --allow-world-ssh --output firewall.bootstrap.json
```

生成器从 `settings` 获取订阅端口，自动包含 TCP/UDP 443；拒绝与 SSH 冲突的端口、重复规则、已有输出文件和符号链接。生成文件权限为 0600。检查成功退出 0，规则不符合退出非零。

检查器只校验本工程的窄范围模板：字段、协议/端口、来源、重复项和必需规则。它不解释任意云 ACL；额外业务规则、拒绝规则、端口范围或刻意收窄订阅来源需单独审查，不能当作本模板直接通过。

## 导入前后必查

1. 确认选中的是**新服务器**的防火墙。先导出原规则备份，并核实控制台“导入”是追加还是替换，避免误删已有业务/SSH 规则。
2. 确认未匹配入站的实际默认动作是拒绝，检查是否残留“允许全部”的旧规则。本文件没有添加兜底拒绝，也不能仅靠这五条白名单证明其他端口已关闭。
3. 默认出站应允许代理访问互联网、DNS、TLS 和软件源；若出站受限，另行制定规则，入站模板无法解决。
4. 当前包使用 IPv4 服务器地址；如果以后分配公网 IPv6，单独审核 IPv6 规则。`0.0.0.0/0` 不覆盖 IPv6。
5. 云防火墙和主机 UFW/nftables 是两层。0.5.4 安装器会在安全范围内新增 UFW 规则；已有重叠规则、显式拒绝和自定义防火墙留给人工核对。不启停防火墙、不清空规则、不动 SSH。模板命令本身始终不修改 UFW。
6. 导入后在控制台核对规则，再从外部验证 SSH、订阅、VLESS 和 Hy2。双 HTTP/HTTPS 配置生成六条（多管理来源则更多），不要漏掉额外订阅端口。离线检查不代表云端已生效，也不证明速度或直播稳定性。

只读本机检测：`sudo bash deploy.sh firewall inspect --settings settings.local.json`。云 API 尚未接入，报告明确标记 `cloud_status=unverified`，不会假装已开放云安全组。无论本机是否有规则，都应单独确认云端入站策略。

本版本已通过 JSON/生成器一致性和离线测试；尚未实际上传控制台验证导入器的接受情况。端口段写法（如 `UDP:24576-24639`）尤其需要在导入后核对一次，如果控制台不接受，就在控制台里手动添加这一条。
