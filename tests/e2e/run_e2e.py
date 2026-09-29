"""端到端测试：电脑 A（本机）⇄ 虚拟网线 ⇄ 假电脑 B（Ubuntu 20.04 容器）。

需要 root、Docker 和网络命名空间：

    sudo python3 -m tests.e2e.run_e2e --bundle 离线包.tar [--scenario dhcp|static|all]

离线包可以用 tests/fakes/make_fake_rustdesk.py 生成的假 RustDesk deb 制作，
依赖列表与官方包相同，所以会真实地走一遍离线安装。
"""

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
IMAGE = "ard-fakeb:focal"
CONTAINER = "ard-fakeb-e2e"
A_IF, B_IF = "ardA1", "ardB1"
SCENARIOS = ["dhcp", "static", "silent"]
STATIC_IP = {"static": "10.9.8.7", "silent": "192.168.1.50"}


def sh(*cmd, check=True, timeout=600):
    p = subprocess.run(list(cmd), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout)
    out = p.stdout.decode("utf-8", "replace")
    if check and p.returncode != 0:
        raise RuntimeError("%s 失败：\n%s" % (" ".join(cmd), out))
    return out


def dexec(*cmd, check=True, container=CONTAINER):
    return sh("docker", "exec", container, *cmd, check=check)


def setup_b(scenario):
    sh("docker", "rm", "-f", CONTAINER, check=False)
    sh("ip", "link", "del", A_IF, check=False)
    sh("docker", "run", "-d", "--name", CONTAINER, "--network", "none", "--cap-add", "NET_ADMIN",
       IMAGE)
    pid = sh("docker", "inspect", "-f", "{{.State.Pid}}", CONTAINER).strip()
    sh("ip", "link", "add", A_IF, "type", "veth", "peer", "name", B_IF)
    sh("ip", "link", "set", B_IF, "netns", pid)
    dexec("ip", "link", "set", B_IF, "up")
    sh("ip", "link", "set", A_IF, "up")
    if scenario == "dhcp":
        # 和 Ubuntu 桌面版默认一样：网口用 DHCP 自动获取地址
        sh("docker", "exec", "-d", CONTAINER, "dhclient", "-v", B_IF)
    elif scenario == "static":
        # 固定 IP，并模拟 B 平时会发出的流量（访问网关时发 ARP）
        dexec("ip", "addr", "add", STATIC_IP[scenario] + "/24", "dev", B_IF)
        sh("docker", "exec", "-d", CONTAINER, "sh", "-c",
           "while true; do ping -c1 -W1 10.9.8.1 >/dev/null 2>&1; sleep 2; done")
    else:
        # 固定 IP 且完全不发报文：只能靠主动 ARP 扫描常见网段找到
        dexec("ip", "addr", "add", STATIC_IP[scenario] + "/24", "dev", B_IF)


def teardown_b():
    sh("docker", "rm", "-f", CONTAINER, check=False)
    sh("ip", "link", "del", A_IF, check=False)


def run_cli(home, extra):
    env = dict(os.environ, XDG_CONFIG_HOME=os.path.join(home, "config"), PYTHONUNBUFFERED="1")
    cmd = [sys.executable, "-m", "autorustdesk", "connect", "--iface", A_IF, "--user", "robot",
           "--password", "robot123", "--no-launch", "-y", "-v"] + extra
    print("$ " + " ".join(cmd), flush=True)
    p = subprocess.run(cmd, cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                       timeout=3600)
    out = p.stdout.decode("utf-8", "replace")
    print(out, flush=True)
    return p.returncode, out


def check(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print("  ✔ " + msg, flush=True)


def verify_b(scenario, home):
    opts = json.loads(dexec("cat", "/var/lib/fake-rustdesk/options.json"))
    check(opts.get("direct-server") == "Y", "B 开启了 IP 直连")
    check(opts.get("verification-method") == "use-permanent-password", "B 只使用固定密码")
    check(opts.get("approve-mode") == "password", "B 不需要人工确认连接")
    check(opts.get("whitelist") == "192.168.77.0/24", "B 设置了 IP 白名单")
    devices = json.load(open(os.path.join(home, "config", "autorustdesk", "devices.json")))["devices"]
    dev = list(devices.values())[0]
    pw = dexec("cat", "/var/lib/fake-rustdesk/password.txt").strip()
    check(pw == dev["rustdesk_password"] and len(pw) >= 8, "B 的 RustDesk 密码与 A 记录的一致")
    gdm = dexec("cat", "/etc/gdm3/custom.conf")
    check("WaylandEnable=false" in gdm and "#WaylandEnable" not in gdm, "登录界面改用 Xorg")
    check("dummy" in dexec("cat", "/etc/X11/xorg.conf.d/99-autorustdesk-dummy.conf"),
          "没接显示器，虚拟显示器已启用")
    check(os.path.basename(dexec("ls", "/etc/systemd/system/autorustdesk-display.service").strip())
          == "autorustdesk-display.service", "安装了开机切换虚拟显示器的服务")
    check("mask" in dexec("cat", "/var/lib/fake-systemd/calls.log"), "禁止了自动休眠")
    check(dexec("dpkg-query", "-W", "-f=${Version}", "rustdesk").strip() != "", "RustDesk 已安装")
    check(dexec("dpkg-query", "-W", "-f=${Status}", "xserver-xorg-video-dummy").endswith("installed"),
          "虚拟显示驱动已安装")
    addrs = dexec("ip", "-4", "addr", "show", B_IF)
    check("192.168.77." in addrs, "B 的直连网口有直连网段地址")
    if scenario in STATIC_IP:
        check(STATIC_IP[scenario] in addrs, "B 原来的固定 IP 保持不变")
    check("192.168.77.1/24" not in sh("ip", "-4", "addr", "show", A_IF), "A 的网卡已恢复原状")
    check(not os.path.exists("/tmp/autorustdesk-") and
          dexec("sh", "-c", "ls -d /tmp/autorustdesk-* 2>/dev/null || true").strip() == "",
          "B 上的临时文件已清理")


def swap_scenario(bundle, home):
    """程序不退出，先配置 B1，再拔线换插 B2（不同 MAC）。"""
    from autorustdesk.cli import ConsoleUi
    from autorustdesk.core.devices import DeviceRegistry
    from autorustdesk.core.settings import Settings
    from autorustdesk.core.workflow import Workflow

    os.environ["XDG_CONFIG_HOME"] = os.path.join(home, "config")
    second = CONTAINER + "-2"
    setup_b("dhcp")
    sh("docker", "rm", "-f", second, check=False)
    sh("docker", "run", "-d", "--name", second, "--network", "none", "--cap-add", "NET_ADMIN", IMAGE)
    settings = Settings(iface=A_IF, show_virtual_nics=True, bundle_path=bundle, ssh_user="robot",
                        auto_launch=False)
    wf = Workflow(settings, DeviceRegistry(), ConsoleUi(assume_yes=True, password="robot123"))
    wf.session_ssh_password = "robot123"
    try:
        r1 = wf.run("full")
        check(r1.get("ok") and r1["ip"] == "192.168.77.100", "B1 配置成功（192.168.77.100）")
        # 拔线：B 端网卡 down，移到 B2 的容器里，换一个 MAC，再插上
        pid1 = sh("docker", "inspect", "-f", "{{.State.Pid}}", CONTAINER).strip()
        pid2 = sh("docker", "inspect", "-f", "{{.State.Pid}}", second).strip()
        sh("nsenter", "-t", pid1, "-n", "ip", "link", "set", B_IF, "down")
        time.sleep(3)
        sh("nsenter", "-t", pid1, "-n", "ip", "link", "set", B_IF, "netns", pid2)
        dexec("ip", "link", "set", B_IF, "address", "02:42:ac:11:00:99", container=second)
        dexec("ip", "link", "set", B_IF, "up", container=second)
        sh("docker", "exec", "-d", second, "dhclient", "-v", B_IF)
        r2 = wf.run("full")
        check(r2.get("ok") and r2["mac"] == "02:42:ac:11:00:99", "换插后识别出新设备 B2")
        check(r2["ip"] == "192.168.77.101", "B2 分到不同的地址（B1 的 .100 已保留）")
        check(r1["password"] != r2["password"], "两台设备的 RustDesk 密码不同")
        opts = json.loads(dexec("cat", "/var/lib/fake-rustdesk/options.json", container=second))
        check(opts.get("direct-server") == "Y", "B2 开启了 IP 直连")
        check(len(wf.registry.all()) == 2, "设备记录里有两台设备")
    finally:
        wf.restore_network()
        sh("docker", "rm", "-f", second, check=False)
        teardown_b()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bundle", required=True)
    ap.add_argument("--scenario", choices=SCENARIOS + ["swap", "all"], default="all")
    args = ap.parse_args()
    bundle = os.path.abspath(args.bundle)
    print("构建假电脑 B 的镜像……", flush=True)
    sh("docker", "build", "-q", "-t", IMAGE, os.path.join(HERE, "fakeb"), timeout=1800)
    scenarios = SCENARIOS + ["swap"] if args.scenario == "all" else [args.scenario]
    for scenario in scenarios:
        if scenario == "swap":
            print("\n========== 场景：换插设备 ==========", flush=True)
            swap_scenario(bundle, tempfile.mkdtemp(prefix="ard-e2e-"))
            continue
        print("\n========== 场景：%s ==========" % scenario, flush=True)
        home = tempfile.mkdtemp(prefix="ard-e2e-")
        setup_b(scenario)
        try:
            rc, out = run_cli(home, ["--bundle", bundle])
            check(rc == 0 and "完成：地址 192.168.77." in out, "完整流程成功")
            verify_b(scenario, home)
            rc, out = run_cli(home, ["--quick"])
            check(rc == 0 and "已配置过，直接连接" in out, "快速连接跳过了安装和配置")
            rc, out = run_cli(home, ["--bundle", bundle])
            check(rc == 0 and "已安装 RustDesk" in out, "再次运行完整流程是幂等的")
        finally:
            teardown_b()
    print("\n端到端测试全部通过", flush=True)


if __name__ == "__main__":
    main()
