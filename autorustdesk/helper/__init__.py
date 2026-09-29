"""以 root 运行的网络助手：配置直连网卡、运行 DHCP 服务、监听链路发现电脑 B。

图形界面以普通用户运行，通过 pkexec 启动本助手，用 stdin/stdout 上的 JSON 行通信。
本包只使用标准库。
"""

HELPER_PROTOCOL = 1
