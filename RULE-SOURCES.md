# gowest 场景规则来源与取舍（0.5.2）

查阅日期：2026-10-26。以下是公开资料，不包含用户的订阅、密钥或服务器配置。

## 查阅来源

- [SkywalkerJi/Clash-Rules](https://github.com/SkywalkerJi/Clash-Rules)：按服务划分 AI 规则，参考 `AI/Anthropic.yaml`、`AI/OpenAI.yaml`、`AI/Gemini.yaml` 等的服务域名。查阅版本 `de2def5fbe74a122d99e6124160a83d51b99dd12`，上游 GPL-3.0。README 的 AI 综合描述不代表存在 `AI/AI.yaml`；该路径实测 404，不作为下载依赖。
- [blackmatrix7/ios_rule_script](https://github.com/blackmatrix7/ios_rule_script)：核对 `rule/Clash/YouTube/YouTube.list`、`rule/Clash/TikTok/TikTok.list` 的媒体/CDN 域名。查阅版本 `9b29c945b9164a02c7018ed333cb9b5bc1bcb417`，上游 GPL-2.0。
- [Loyalsoldier/clash-rules](https://github.com/Loyalsoldier/clash-rules)：检索到的通用规则集方案；本版没有引入整套远程列表。
- [LOWERTOP/Shadowrocket](https://github.com/LOWERTOP/Shadowrocket)：社区维护的非官方使用手册及 `lazy_group.conf`。核对节点订阅与配置文件的区别、Clash YAML 导入、策略组、DNS-over-PROXY、UDP 失败策略等语法。
  0.5.1 另核对 README 的“全局路由”“场景”“启用回退”“代理分组类型”及 URL-Schemes；读取的 README blob 为 `eeab58d98d60c060eccae68350a1ede168fddb32`。全局路由/场景/默认节点是应用设置，不伪造为 `.conf` 字段；专用跳转不是必需导入路径。
- [Hysteria2 URI 规范](https://v2.hysteria.network/docs/developers/URI-Scheme/)：原生节点链接的百分号编码、salamander、SNI、`insecure`、`pinSHA256`。

## 本工程实现

`routing.py` 是重新组织的、有限范围的服务域名基线；不是上述仓库的配置文件镜像，不包含其脚本、模板或完整规则集合。服务名及域名事实用于独立生成 Clash 与 Shadowrocket 配置。

- AI：OpenAI/ChatGPT、Claude、Gemini/AI Studio/NotebookLM、Copilot、Grok、Perplexity、Cursor、Augment、Windsurf/Codeium、Trae。
- YouTube：页面、主要视频 CDN、图片与 API；另用 YouTube 关键词覆盖部分地区域名。
- TikTok：主站、Shop、视频/API/CDN 域名；默认同服务器自动回退，可手动固定协议，不放 DIRECT、不跨服务器，也不是自动测速选最快。
- 保留 `Claude` 组名以兼容旧使用习惯，默认跟随 `AI`；普通公网兜底是统一命名的「出口选择」，选择「自动回退」。
- 所有场景规则都排在国内直连规则前，避免带国内地址的共享 CDN 先匹配 CN。
- 不把全部 `microsoft.com`、`googleapis.com`、`githubusercontent.com`、共享云 ASN/IP 范围视作 AI 专属。
- 不采用远程脚本、HTTPS 解密、广告注入或第三方订阅转换；不发送用户订阅给这些仓库。
- 规则内嵌，客户端初次加载不依赖 GitHub 可达性。订阅自动刷新**不会自动跟踪上游规则变化**；后续更新需审核 `routing.py`、运行测试并重新发布。

这不是完备的应用识别系统。新增域名、硬编码 IP、APP 自带 DNS/定位、SIM、时区和账号状态均可能影响服务行为。未知公网流量通常仍由 Select 代理，但不一定进入对应的专用分组。TikTok 等重要业务建议使用严格版及固定出口，并实际检查日志。

## 兼容性边界

- Clash Verge / Mihomo：常规 block YAML，完整节点、分组、规则、DNS；可用原普通/严格链接。
- Shadowrocket：可尝试在“配置”页导入上述 YAML；更明确的兼容路径是 `-nodes` 原生节点订阅 + 本工程 `.conf` 分流配置。手机实机导入与连通性仍需验收，不能用桌面内核检查代替。
- 原版 Shadowsocks：不支持当前 Hysteria2/VLESS 服务，不能仅通过转换格式获得兼容性；本版没有擅自新增 SS 服务或开放端口。
