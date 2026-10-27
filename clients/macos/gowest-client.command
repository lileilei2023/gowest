#!/bin/bash
# 双击运行：打开“终端”执行 gowest Mac 客户端助手（检测 → 安装 → 导入）。
cd "$(dirname "$0")" || exit 1
bash ./gowest-client.sh "$@"
echo
read -r -p '按回车键关闭窗口…' _
