#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""AutoRustDesk 在电脑 B（Ubuntu 20.04）上执行的脚本。

由电脑 A 通过 SSH 上传后以 sudo 运行；只用标准库，兼容 Python 3.8。

子命令：
  probe          收集环境信息
  install        用离线包安装 RustDesk 及依赖
  configure      配置 RustDesk、登录界面、虚拟显示器、休眠、防火墙
  link-ip        给直连网口临时加一个直连网段地址
  status         快速检查 RustDesk 状态
  display-switch 根据是否接了显示器启用/停用虚拟显示器（开机时由 systemd 调用）
  restart-display 重启显示管理器（登录界面）
  revert         撤销 AutoRustDesk 对系统做的修改（不卸载 RustDesk）

输出约定：以 "ARD:" 开头的行是给电脑 A 解析的：
  ARD:LOG <文本>     日志
  ARD:STEP <文本>    当前步骤
  ARD:RESULT <JSON>  最终结果（最后一行）
其它行是被调用命令的原始输出。
"""

import argparse
import ipaddress
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import time

SCRIPT_VERSION = "1"

STATE_DIR = "/etc/autorustdesk"
STATE_FILE = os.path.join(STATE_DIR, "state.json")
DISPLAY_CONF = os.path.join(STATE_DIR, "display.json")
DUMMY_TEMPLATE = os.path.join(STATE_DIR, "xorg-dummy.conf")
XORG_CONF_D = "/etc/X11/xorg.conf.d"
DUMMY_ACTIVE = os.path.join(XORG_CONF_D, "99-autorustdesk-dummy.conf")
INSTALL_DIR = "/usr/local/lib/autorustdesk"
INSTALLED_SCRIPT = os.path.join(INSTALL_DIR, "ard_remote.py")
DISPLAY_UNIT_NAME = "autorustdesk-display.service"
DISPLAY_UNIT = "/etc/systemd/system/" + DISPLAY_UNIT_NAME
GDM_CONFS = ["/etc/gdm3/custom.conf", "/etc/gdm/custom.conf"]
SLEEP_TARGETS = ["sleep.target", "suspend.target", "hibernate.target", "hybrid-sleep.target"]
DEFAULT_PORT = 21118


class ArdError(Exception):
    pass


# ---------------------------------------------------------------------------
# 输出
# ---------------------------------------------------------------------------


def log(msg):
    for line in str(msg).splitlines() or [""]:
        print("ARD:LOG " + line, flush=True)


def step(msg):
    print("ARD:STEP " + msg, flush=True)


def emit_result(obj):
    print("ARD:RESULT " + json.dumps(obj, ensure_ascii=False, sort_keys=True), flush=True)


# ---------------------------------------------------------------------------
# 执行命令
# ---------------------------------------------------------------------------


def run(cmd, timeout=60, check=False, env=None, stream=False, input_text=None):
    """执行命令，返回 (退出码, 输出)。stream=True 时把输出逐行转发给 A。"""
    full_env = dict(os.environ)
    full_env.setdefault("LC_ALL", "C.UTF-8")
    full_env["LANG"] = "C.UTF-8"
    if env:
        full_env.update(env)
    try:
        if not stream:
            p = subprocess.run(
                cmd,
                stdin=subprocess.DEVNULL if input_text is None else subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=timeout,
                env=full_env,
                input=input_text.encode() if input_text is not None else None,
            )
            out = p.stdout.decode("utf-8", "replace")
            rc = p.returncode
        else:
            p = subprocess.Popen(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                env=full_env,
            )
            chunks = []
            deadline = time.time() + timeout
            assert p.stdout is not None
            for raw in iter(p.stdout.readline, b""):
                line = raw.decode("utf-8", "replace").rstrip("\n")
                chunks.append(line)
                print(line, flush=True)
                if time.time() > deadline:
                    p.kill()
                    raise subprocess.TimeoutExpired(cmd, timeout)
            rc = p.wait()
            out = "\n".join(chunks)
    except FileNotFoundError:
        rc, out = 127, "%s: 命令不存在" % cmd[0]
    except subprocess.TimeoutExpired:
        rc, out = 124, "%s: 超时（%ss）" % (" ".join(cmd), timeout)
    if check and rc != 0:
        raise ArdError("命令失败（%d）：%s\n%s" % (rc, " ".join(cmd), out.strip()[-2000:]))
    return rc, out


def have(cmd):
    return shutil.which(cmd) is not None


def read_file(path, default=None):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return default


def write_file(path, content, mode=0o644):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = path + ".ard-tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(content)
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def backup_once(path):
    """第一次修改前备份原文件。"""
    bak = path + ".autorustdesk.bak"
    if os.path.exists(path) and not os.path.exists(bak):
        shutil.copy2(path, bak)
    return bak


def load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_state(state):
    state["script_version"] = SCRIPT_VERSION
    state["updated"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    write_file(STATE_FILE, json.dumps(state, ensure_ascii=False, indent=2) + "\n", 0o600)


def require_root():
    if os.geteuid() != 0:
        raise ArdError("需要 root 权限（请用 sudo 运行）")


# ---------------------------------------------------------------------------
# 纯函数（便于单元测试）
# ---------------------------------------------------------------------------

_WAYLAND_COMMENTED = re.compile(r"^\s*#\s*WaylandEnable\s*=", re.I)
_WAYLAND_LINE = re.compile(r"^\s*WaylandEnable\s*=\s*(\S*)\s*$", re.I)
_SECTION = re.compile(r"^\s*\[([^\]]+)\]\s*$")


def rustdesk_sees_login_wayland(text):
    """与 RustDesk 源码 is_login_wayland() 的判断一致。"""
    return bool(
        re.search(r"# *WaylandEnable *= *false", text) or re.search(r"WaylandEnable *= *true", text)
    )


def gdm_disable_wayland(text):
    """把 GDM 配置改成登录界面使用 Xorg。返回 (新文本, 是否有变化)。

    RustDesk 用正则 "# *WaylandEnable *= *false" 判断登录界面是 Wayland，
    所以除了设置 WaylandEnable=false，还必须删掉被注释掉的那一行。
    """
    lines = text.splitlines()
    out = []
    section = None
    daemon_idx = None
    found = False
    for line in lines:
        if _WAYLAND_COMMENTED.match(line):
            continue
        m = _SECTION.match(line)
        if m:
            section = m.group(1).strip().lower()
            out.append(line)
            if section == "daemon" and daemon_idx is None:
                daemon_idx = len(out) - 1
            continue
        m = _WAYLAND_LINE.match(line)
        if m:
            if section == "daemon" and not found:
                found = True
                out.append("WaylandEnable=false")
            continue
        out.append(line)
    if not found:
        if daemon_idx is None:
            if out and out[-1].strip():
                out.append("")
            out.extend(["[daemon]", "WaylandEnable=false"])
        else:
            out.insert(daemon_idx + 1, "WaylandEnable=false")
    new = "\n".join(out) + "\n"
    return new, new != text


def gdm_wayland_state(text):
    """返回 'disabled'（登录界面走 Xorg）或 'enabled'。"""
    if text is None:
        return "unknown"
    section = None
    for line in text.splitlines():
        m = _SECTION.match(line)
        if m:
            section = m.group(1).strip().lower()
            continue
        m = _WAYLAND_LINE.match(line)
        if m and section == "daemon":
            return "disabled" if m.group(1).lower() in ("false", "0", "no") else "enabled"
    return "enabled"


def cvt_modeline(width, height, refresh=60.0):
    """按 VESA CVT 计算 Modeline（与 xserver 的 cvt 工具结果一致）。"""
    if width % 8:
        width += 8 - width % 8
    v_display = height
    if not (v_display % 3) and (v_display * 4 // 3) == width:
        vsync = 4
    elif not (v_display % 9) and (v_display * 16 // 9) == width:
        vsync = 5
    elif not (v_display % 10) and (v_display * 16 // 10) == width:
        vsync = 6
    elif not (v_display % 4) and (v_display * 5 // 4) == width:
        vsync = 7
    elif not (v_display % 9) and (v_display * 15 // 9) == width:
        vsync = 7
    else:
        vsync = 10
    min_vsync_bp = 550.0
    min_v_porch = 3
    h_gran = 8
    h_period = (1000000.0 / refresh - min_vsync_bp) / (v_display + min_v_porch)
    if int(min_vsync_bp / h_period) + 1 < vsync + min_v_porch:
        vsync_bp = vsync + min_v_porch
    else:
        vsync_bp = int(min_vsync_bp / h_period) + 1
    v_total = v_display + vsync_bp + min_v_porch
    c_prime = (40 - 20) * 128 // 256 + 20
    m_prime = 600 * 128 // 256
    h_blank_pct = c_prime - m_prime * h_period / 1000.0
    if h_blank_pct < 20:
        h_blank_pct = 20
    h_blank = int(width * h_blank_pct / (100.0 - h_blank_pct))
    h_blank -= h_blank % (2 * h_gran)
    h_total = width + h_blank
    h_sync_end = width + h_blank // 2
    h_sync_start = h_sync_end - (h_total * 8) // 100
    h_sync_start += h_gran - h_sync_start % h_gran
    v_sync_start = v_display + min_v_porch
    v_sync_end = v_sync_start + vsync
    clock = int(h_total * 1000.0 / h_period)
    clock -= clock % 250
    name = "%dx%d" % (width, height)
    return name, '"%s" %.2f %d %d %d %d %d %d %d %d -hsync +vsync' % (
        name, clock / 1000.0, width, h_sync_start, h_sync_end, h_total,
        v_display, v_sync_start, v_sync_end, v_total,
    )


def parse_resolution(text):
    m = re.match(r"^\s*(\d{3,5})\s*[xX*]\s*(\d{3,5})\s*$", text or "")
    if not m:
        raise ArdError("分辨率格式不对：%r（示例：1920x1080）" % text)
    w, h = int(m.group(1)), int(m.group(2))
    if not (640 <= w <= 7680 and 480 <= h <= 4320):
        raise ArdError("分辨率超出范围：%dx%d" % (w, h))
    return w, h


def dummy_xorg_conf(width, height):
    name, modeline = cvt_modeline(width, height)
    return """# 由 AutoRustDesk 生成：没接显示器时使用的虚拟显示器（xserver-xorg-video-dummy）。
# 开机时 autorustdesk-display.service 会根据是否接了显示器自动启用或移除本文件。
Section "ServerLayout"
    Identifier "AutoRustDeskDummyLayout"
    Screen     "AutoRustDeskDummyScreen"
EndSection

Section "Device"
    Identifier "AutoRustDeskDummyDevice"
    Driver     "dummy"
    VideoRam   256000
EndSection

Section "Monitor"
    Identifier  "AutoRustDeskDummyMonitor"
    HorizSync   5.0 - 1000.0
    VertRefresh 5.0 - 200.0
    Modeline    %s
EndSection

Section "Screen"
    Identifier   "AutoRustDeskDummyScreen"
    Device       "AutoRustDeskDummyDevice"
    Monitor      "AutoRustDeskDummyMonitor"
    DefaultDepth 24
    SubSection "Display"
        Depth   24
        Modes   "%s"
        Virtual %d %d
    EndSubSection
EndSection
""" % (modeline, name, int(name.split("x")[0]), height)


def display_unit_text(python="/usr/bin/python3"):
    return """[Unit]
Description=AutoRustDesk: use a virtual display when no monitor is connected
After=systemd-udev-settle.service systemd-modules-load.service
Before=display-manager.service gdm.service gdm3.service lightdm.service

[Service]
Type=oneshot
ExecStart=%s %s display-switch --boot
RemainAfterExit=yes

[Install]
WantedBy=graphical.target
""" % (python, INSTALLED_SCRIPT)


def merge_whitelist(current, cidr):
    items = [x.strip() for x in (current or "").split(",") if x.strip()]
    if cidr not in items:
        items.append(cidr)
    return ",".join(items)


def parse_listen_ports(text):
    """解析 /proc/net/tcp(6)，返回处于 LISTEN 状态的端口集合。"""
    ports = set()
    for line in text.splitlines()[1:]:
        parts = line.split()
        if len(parts) < 4:
            continue
        local, state = parts[1], parts[3]
        if state != "0A":
            continue
        try:
            ports.add(int(local.rsplit(":", 1)[1], 16))
        except (ValueError, IndexError):
            continue
    return ports


def parse_loginctl_show(text):
    props = {}
    for line in text.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            props[k.strip()] = v.strip()
    return props


# ---------------------------------------------------------------------------
# 系统信息
# ---------------------------------------------------------------------------


def os_release():
    info = {}
    for line in (read_file("/etc/os-release", "") or "").splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            info[k] = v.strip().strip('"')
    return info


def dpkg_version(pkg):
    rc, out = run(["dpkg-query", "-W", "-f=${Status}\t${Version}", pkg], timeout=30)
    if rc != 0:
        return None
    status, _, version = out.strip().partition("\t")
    if status.endswith(" installed"):
        return version
    return None


def dpkg_arch():
    rc, out = run(["dpkg", "--print-architecture"], timeout=10)
    return out.strip() if rc == 0 else ""


def version_ge(a, b):
    if not a or not b:
        return False
    rc, _ = run(["dpkg", "--compare-versions", a, "ge", b], timeout=10)
    return rc == 0


def systemctl(*args, timeout=60):
    return run(["systemctl"] + list(args), timeout=timeout)


def unit_state(unit):
    _, active = systemctl("is-active", unit, timeout=15)
    _, enabled = systemctl("is-enabled", unit, timeout=15)
    return active.strip().splitlines()[-1] if active.strip() else "", (
        enabled.strip().splitlines()[-1] if enabled.strip() else ""
    )


def listen_ports():
    ports = set()
    for p in ("/proc/net/tcp", "/proc/net/tcp6"):
        ports |= parse_listen_ports(read_file(p, "") or "")
    return ports


def drm_connectors():
    base = "/sys/class/drm"
    result = []
    try:
        names = sorted(os.listdir(base))
    except OSError:
        return result
    for name in names:
        if not re.match(r"^card\d+-", name):
            continue
        status = (read_file(os.path.join(base, name, "status"), "") or "").strip()
        result.append({"name": name, "status": status})
    return result


def monitors_connected(connectors=None):
    if connectors is None:
        connectors = drm_connectors()
    return [c["name"] for c in connectors if c["status"] == "connected"]


def sessions():
    result = []
    if not have("loginctl"):
        return result
    rc, out = run(["loginctl", "list-sessions", "--no-legend"], timeout=15)
    if rc != 0:
        return result
    for line in out.splitlines():
        parts = line.split()
        if not parts:
            continue
        sid = parts[0]
        _, show = run(
            ["loginctl", "show-session", sid, "-p", "Name", "-p", "Type", "-p", "Class",
             "-p", "State", "-p", "Active", "-p", "Seat", "-p", "Display", "-p", "Remote"],
            timeout=15,
        )
        props = parse_loginctl_show(show)
        props["Id"] = sid
        result.append(props)
    return result


def graphical_user_sessions(sess=None):
    sess = sessions() if sess is None else sess
    return [
        s for s in sess
        if s.get("Class") == "user" and s.get("Type") in ("x11", "wayland")
        and s.get("State") in ("active", "online")
    ]


def greeter_sessions(sess=None):
    sess = sessions() if sess is None else sess
    return [s for s in sess if s.get("Class") == "greeter"]


def ip_json(*args):
    rc, out = run(["ip", "-j"] + list(args), timeout=15)
    if rc != 0:
        return []
    try:
        return json.loads(out)
    except ValueError:
        return []


def interfaces():
    result = []
    for item in ip_json("addr", "show"):
        name = item.get("ifname", "")
        if name == "lo":
            continue
        entry = {
            "name": name,
            "mac": (item.get("address") or "").lower(),
            "state": item.get("operstate", ""),
            "ipv4": [],
            "ipv6": [],
        }
        for a in item.get("addr_info", []):
            cidr = "%s/%s" % (a.get("local"), a.get("prefixlen"))
            if a.get("family") == "inet":
                entry["ipv4"].append(cidr)
            elif a.get("family") == "inet6":
                entry["ipv6"].append(cidr)
        result.append(entry)
    return result


def find_iface_by_mac(mac):
    mac = (mac or "").lower()
    for itf in interfaces():
        if itf["mac"] == mac:
            return itf
    return None


def ufw_active():
    if not have("ufw"):
        return False
    rc, out = run(["ufw", "status"], timeout=20)
    return rc == 0 and "Status: active" in out


def sleep_masked():
    masked = {}
    for t in SLEEP_TARGETS:
        _, enabled = systemctl("is-enabled", t, timeout=15)
        masked[t] = enabled.strip().endswith("masked")
    return masked


def disk_free_mb(path):
    try:
        st = os.statvfs(path)
        return int(st.f_bavail * st.f_frsize / 1048576)
    except OSError:
        return -1


def gdm_conf_path():
    for p in GDM_CONFS:
        if os.path.exists(p):
            return p
    return None


def display_manager():
    dm = (read_file("/etc/X11/default-display-manager", "") or "").strip()
    return os.path.basename(dm) if dm else ""


def has_display_manager_unit():
    return os.path.exists("/etc/systemd/system/display-manager.service")


# ---------------------------------------------------------------------------
# RustDesk
# ---------------------------------------------------------------------------


def rustdesk_bin():
    for p in ("/usr/bin/rustdesk", "/usr/share/rustdesk/rustdesk"):
        if os.path.exists(p):
            return p
    return shutil.which("rustdesk")


def rd(args, timeout=30):
    exe = rustdesk_bin()
    if not exe:
        raise ArdError("B 上没有找到 rustdesk 程序")
    rc, out = run([exe] + list(args), timeout=timeout)
    lines = [ln.strip() for ln in out.splitlines() if ln.strip()]
    return rc, lines


def rd_get_option(key):
    _, lines = rd(["--option", key])
    return lines[-1] if lines else ""


def rd_get_id(timeout=20):
    _, lines = rd(["--get-id"], timeout=timeout)
    for ln in reversed(lines):
        if re.match(r"^[0-9A-Za-z_\-]{3,}$", ln):
            return ln
    return ""


def wait_rustdesk_ipc(timeout=60):
    """等 RustDesk 服务进程就绪（能通过 IPC 取到 ID）。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        rid = rd_get_id()
        if rid:
            return rid
        time.sleep(2)
    return ""


def rd_set_option(key, value, attempts=5):
    for _ in range(attempts):
        rd(["--option", key, value])
        if rd_get_option(key) == value:
            return True
        time.sleep(2)
    return False


def rd_set_password(password, timeout=60):
    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        _, lines = rd(["--password", password])
        last = " ".join(lines)
        if any(ln == "Done!" for ln in lines):
            return True, last
        time.sleep(2)
    return False, last


def rustdesk_status(port=DEFAULT_PORT, with_options=True):
    version = dpkg_version("rustdesk")
    active, enabled = unit_state("rustdesk")
    info = {
        "installed": version is not None,
        "version": version,
        "binary": rustdesk_bin(),
        "service_active": active,
        "service_enabled": enabled,
        "listening": port in listen_ports(),
        "port": port,
    }
    if with_options and info["binary"] and os.geteuid() == 0 and active == "active":
        info["id"] = rd_get_id(timeout=10)
        opts = {}
        for key in ("direct-server", "direct-access-port", "verification-method",
                    "approve-mode", "whitelist"):
            try:
                opts[key] = rd_get_option(key)
            except ArdError:
                pass
        info["options"] = opts
    return info


def ensure_rustdesk_service():
    active, enabled = unit_state("rustdesk")
    if enabled not in ("enabled", "static"):
        systemctl("daemon-reload")
        systemctl("enable", "rustdesk")
        log("已设置 RustDesk 服务开机自启")
    if active != "active":
        systemctl("start", "rustdesk")
        log("已启动 RustDesk 服务")


# ---------------------------------------------------------------------------
# 子命令：probe
# ---------------------------------------------------------------------------


def cmd_probe(args):
    rel = os_release()
    sess = sessions()
    gdm = gdm_conf_path()
    gdm_text = read_file(gdm) if gdm else None
    connectors = drm_connectors()
    facts = {
        "script_version": SCRIPT_VERSION,
        "is_root": os.geteuid() == 0,
        "hostname": os.uname()[1],
        "os": {
            "id": rel.get("ID", ""),
            "version": rel.get("VERSION_ID", ""),
            "codename": rel.get("VERSION_CODENAME", ""),
            "pretty": rel.get("PRETTY_NAME", ""),
        },
        "arch": dpkg_arch(),
        "machine": os.uname()[4],
        "python": sys.version.split()[0],
        "rustdesk": rustdesk_status(args.port),
        "display": {
            "manager": display_manager(),
            "manager_active": unit_state("display-manager")[0],
            "default_target": systemctl("get-default", timeout=15)[1].strip(),
            "gdm_conf": gdm,
            "gdm_wayland": gdm_wayland_state(gdm_text),
            "rustdesk_sees_login_wayland": bool(gdm_text) and rustdesk_sees_login_wayland(gdm_text),
            "connectors": connectors,
            "monitors": monitors_connected(connectors),
            "dummy_driver": dpkg_version("xserver-xorg-video-dummy"),
            "dummy_active": os.path.exists(DUMMY_ACTIVE),
            "xorg_conf": os.path.exists("/etc/X11/xorg.conf"),
            "nvidia": os.path.exists("/proc/driver/nvidia"),
            "sessions": sess,
            "user_sessions": len(graphical_user_sessions(sess)),
            "greeter_sessions": len(greeter_sessions(sess)),
        },
        "network": {"interfaces": interfaces()},
        "firewall": {"ufw_active": ufw_active() if os.geteuid() == 0 else None},
        "power": {"sleep_masked": sleep_masked()},
        "disk": {"tmp_mb": disk_free_mb("/tmp"), "root_mb": disk_free_mb("/")},
        "state": load_state() if os.geteuid() == 0 else {},
    }
    if args.link_mac:
        facts["network"]["link"] = find_iface_by_mac(args.link_mac)
    return {"ok": True, "facts": facts}


# ---------------------------------------------------------------------------
# 子命令：install
# ---------------------------------------------------------------------------


def _safe_extract(tar_path, dest):
    """解压离线包，拒绝绝对路径、.. 与链接等可疑成员（Python 3.8 没有 tarfile 过滤器）。"""
    dest = os.path.realpath(dest)
    with tarfile.open(tar_path, "r:*") as tf:
        members = tf.getmembers()
        for m in members:
            target = os.path.realpath(os.path.join(dest, m.name))
            if not (target == dest or target.startswith(dest + os.sep)):
                raise ArdError("离线包包含非法路径：%s" % m.name)
            if not (m.isfile() or m.isdir()):
                raise ArdError("离线包包含不支持的文件类型：%s" % m.name)
        for m in members:
            m.mode = 0o755 if m.isdir() else 0o644
            m.uid = m.gid = 0
        tf.extractall(dest, members=members)


def _find_bundle_root(path):
    for root, _dirs, files in os.walk(path):
        if "manifest.json" in files and os.path.isdir(os.path.join(root, "repo")):
            return root
    raise ArdError("离线包里找不到 manifest.json")


def _apt_opts(work, repo):
    lists = os.path.join(work, "lists")
    archives = os.path.join(work, "archives")
    empty = os.path.join(work, "sources.list.d")
    for d in (os.path.join(lists, "partial"), os.path.join(archives, "partial"), empty):
        os.makedirs(d, exist_ok=True)
    sources = os.path.join(work, "sources.list")
    write_file(sources, "deb [trusted=yes] file:%s ./\n" % repo)
    return [
        "-o", "Dir::Etc::SourceList=%s" % sources,
        "-o", "Dir::Etc::SourceParts=%s" % empty,
        "-o", "Dir::State::Lists=%s" % lists,
        "-o", "Dir::Cache::Archives=%s" % archives,
        "-o", "Dir::Cache::pkgcache=",
        "-o", "Dir::Cache::srcpkgcache=",
        "-o", "Acquire::Languages=none",
        "-o", "DPkg::Lock::Timeout=300",
        "-o", "APT::Sandbox::User=root",
    ]


def cmd_install(args):
    require_root()
    arch = dpkg_arch()
    if arch != "amd64":
        raise ArdError("离线包只支持 x86_64（amd64），B 的架构是 %s" % arch)
    rel = os_release()
    if rel.get("VERSION_CODENAME") != "focal":
        log("警告：离线包是为 Ubuntu 20.04 制作的，B 是 %s" % rel.get("PRETTY_NAME", "?"))

    bundle = os.path.abspath(args.bundle)
    work = args.workdir or os.path.join(os.path.dirname(bundle), "work")
    os.makedirs(work, exist_ok=True)
    os.chmod(work, 0o755)

    step("解压离线包")
    if os.path.isdir(bundle):
        root = _find_bundle_root(bundle)
    else:
        need = os.path.getsize(bundle) // 1048576 + 50
        if disk_free_mb(work) < need:
            raise ArdError("B 上空间不足：需要约 %d MB" % need)
        extract_to = os.path.join(work, "bundle")
        shutil.rmtree(extract_to, ignore_errors=True)
        os.makedirs(extract_to)
        _safe_extract(bundle, extract_to)
        root = _find_bundle_root(extract_to)
    with open(os.path.join(root, "manifest.json"), "r", encoding="utf-8") as f:
        manifest = json.load(f)
    repo = os.path.join(root, "repo")
    packages = args.packages or manifest.get("install_packages") or ["rustdesk"]
    want_version = manifest.get("rustdesk", {}).get("version")
    before = {p: dpkg_version(p) for p in packages}
    log("离线包：RustDesk %s，共 %d 个依赖包" % (want_version, len(manifest.get("packages", []))))

    opts = _apt_opts(work, repo)
    env = {"DEBIAN_FRONTEND": "noninteractive"}
    step("读取本地软件源")
    run(["apt-get"] + opts + ["update"], timeout=300, check=True, env=env, stream=True)

    install_cmd = ["apt-get"] + opts + [
        "install", "-y", "--no-install-recommends", "--no-remove",
        "-o", "Dpkg::Options::=--force-confdef",
        "-o", "Dpkg::Options::=--force-confold",
    ] + packages
    step("安装 %s" % " ".join(packages))
    rc, out = run(install_cmd, timeout=1800, env=env, stream=True)
    if rc != 0 and "dpkg --configure -a" in out:
        log("dpkg 上次被中断，先执行 dpkg --configure -a")
        run(["dpkg", "--configure", "-a"], timeout=900, env=env, stream=True)
        rc, out = run(install_cmd, timeout=1800, env=env, stream=True)
    if rc != 0 and ("Unmet dependencies" in out or "unmet dependencies" in out):
        log("尝试修复未满足的依赖")
        run(["apt-get"] + opts + ["install", "-f", "-y", "--no-remove"], timeout=1800,
            env=env, stream=True)
        rc, out = run(install_cmd, timeout=1800, env=env, stream=True)
    if rc != 0:
        raise ArdError("apt 安装失败（退出码 %d），详见上方输出" % rc)

    after = {p: dpkg_version(p) for p in packages}
    missing = [p for p, v in after.items() if not v]
    if missing:
        raise ArdError("安装后仍缺少：%s" % " ".join(missing))
    if want_version and after.get("rustdesk") and not version_ge(after["rustdesk"], want_version):
        raise ArdError("RustDesk 版本不符：期望 %s，实际 %s" % (want_version, after["rustdesk"]))

    step("启动 RustDesk 服务")
    ensure_rustdesk_service()
    if not args.keep:
        shutil.rmtree(os.path.join(work, "bundle"), ignore_errors=True)
    return {"ok": True, "before": before, "after": after,
            "changed": before != after, "rustdesk_version": after.get("rustdesk")}


# ---------------------------------------------------------------------------
# 显示相关
# ---------------------------------------------------------------------------


def load_display_conf():
    try:
        with open(DISPLAY_CONF, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"mode": "auto", "resolution": "1920x1080"}


def apply_display_switch(boot=False):
    """根据配置和显示器连接状态启用/移除虚拟显示器配置。返回 (是否启用, 是否有变化, 原因)。"""
    conf = load_display_conf()
    mode = conf.get("mode", "auto")
    if boot:
        # 开机时等显卡驱动加载出 DRM 接口，最多 30 秒
        deadline = time.time() + 30
        while time.time() < deadline and not drm_connectors():
            time.sleep(1)
    monitors = monitors_connected()
    if mode == "on":
        want, reason = True, "设置为始终使用虚拟显示器"
    elif mode == "off":
        want, reason = False, "设置为不使用虚拟显示器"
    elif monitors:
        want, reason = False, "检测到显示器：%s" % ", ".join(monitors)
    else:
        want, reason = True, "没有检测到显示器"
    if want and dpkg_version("xserver-xorg-video-dummy") is None:
        want, reason = False, "未安装 xserver-xorg-video-dummy"
    changed = False
    if want:
        template = read_file(DUMMY_TEMPLATE)
        if template is None:
            w, h = parse_resolution(conf.get("resolution", "1920x1080"))
            template = dummy_xorg_conf(w, h)
        if read_file(DUMMY_ACTIVE) != template:
            os.makedirs(XORG_CONF_D, exist_ok=True)
            write_file(DUMMY_ACTIVE, template)
            changed = True
    elif os.path.exists(DUMMY_ACTIVE):
        os.remove(DUMMY_ACTIVE)
        changed = True
    return want, changed, reason


def cmd_display_switch(args):
    require_root()
    active, changed, reason = apply_display_switch(boot=args.boot)
    msg = "虚拟显示器：%s（%s）" % ("启用" if active else "不启用", reason)
    log(msg)
    return {"ok": True, "dummy_active": active, "changed": changed, "reason": reason}


def restart_display_manager(wait=40):
    """重启显示管理器并等待登录界面出现。"""
    log("重启显示管理器（登录界面）")
    systemctl("restart", "display-manager", timeout=90)
    deadline = time.time() + wait
    while time.time() < deadline:
        sess = sessions()
        if greeter_sessions(sess) or graphical_user_sessions(sess):
            log("登录界面已启动")
            return True
        time.sleep(2)
    log("警告：%d 秒内没有看到登录界面会话" % wait)
    return False


def cmd_restart_display(args):
    require_root()
    if graphical_user_sessions() and not args.force:
        raise ArdError("B 上有用户已登录图形界面，重启显示管理器会让其退出登录")
    ok = restart_display_manager()
    systemctl("restart", "rustdesk")
    return {"ok": True, "greeter": ok}


# ---------------------------------------------------------------------------
# 子命令：configure
# ---------------------------------------------------------------------------


def _read_password(path):
    if not path:
        raise ArdError("缺少 --password-file")
    pw = (read_file(path) or "").strip()
    try:
        os.remove(path)
    except OSError:
        pass
    if len(pw) < 6:
        raise ArdError("RustDesk 密码太短（至少 6 位）")
    return pw


def configure_gdm(state, changes):
    path = gdm_conf_path()
    if not path:
        log("没有找到 GDM 配置文件，跳过登录界面设置（显示管理器：%s）" % (display_manager() or "未知"))
        return False
    text = read_file(path, "")
    new, changed = gdm_disable_wayland(text)
    if changed:
        state.setdefault("gdm_backup", backup_once(path))
        write_file(path, new)
        changes.append("GDM 登录界面改用 Xorg（%s：WaylandEnable=false）" % path)
        return True
    return False


def configure_headless(args, state, changes):
    mode = args.headless
    w, h = parse_resolution(args.resolution)
    conf = {"mode": mode, "resolution": "%dx%d" % (w, h)}
    template = dummy_xorg_conf(w, h)
    os.makedirs(STATE_DIR, exist_ok=True)
    if read_file(DISPLAY_CONF) != json.dumps(conf):
        write_file(DISPLAY_CONF, json.dumps(conf))
    if read_file(DUMMY_TEMPLATE) != template:
        write_file(DUMMY_TEMPLATE, template)

    # 安装开机自动切换服务
    me = os.path.realpath(__file__)
    os.makedirs(INSTALL_DIR, exist_ok=True)
    if os.path.realpath(INSTALLED_SCRIPT) != me:
        shutil.copyfile(me, INSTALLED_SCRIPT)
        os.chmod(INSTALLED_SCRIPT, 0o755)
    unit = display_unit_text(sys.executable if sys.executable else "/usr/bin/python3")
    if read_file(DISPLAY_UNIT) != unit:
        write_file(DISPLAY_UNIT, unit)
        systemctl("daemon-reload")
    if mode == "off":
        systemctl("disable", DISPLAY_UNIT_NAME)
    else:
        _, enabled = unit_state(DISPLAY_UNIT_NAME)
        if enabled != "enabled":
            systemctl("enable", DISPLAY_UNIT_NAME)
            changes.append("已安装开机自动切换虚拟显示器的服务（%s）" % DISPLAY_UNIT_NAME)
    state["display"] = conf
    active, changed, reason = apply_display_switch()
    if changed:
        changes.append("虚拟显示器%s（%s）" % ("已启用 %dx%d" % (w, h) if active else "已停用", reason))
    else:
        log("虚拟显示器：%s（%s）" % ("启用" if active else "不启用", reason))
    return changed, active


def configure_sleep(state, changes):
    masked = sleep_masked()
    todo = [t for t, m in masked.items() if not m]
    if todo:
        # 只在第一次修改时记录原状态，供 revert 恢复
        state.setdefault("sleep_masked_before", masked)
        systemctl("mask", *todo)
        changes.append("已禁止自动休眠（mask %s）" % " ".join(todo))


def configure_firewall(args, changes):
    if not args.firewall_subnet or not ufw_active():
        return
    rc, out = run(["ufw", "allow", "proto", "tcp", "from", args.firewall_subnet, "to", "any",
                   "port", str(args.port), "comment", "AutoRustDesk"], timeout=30)
    if rc == 0 and "Skipping" not in out:
        changes.append("防火墙已放行 %s 访问 %d/tcp" % (args.firewall_subnet, args.port))
    elif rc != 0:
        log("警告：添加防火墙规则失败：%s" % out.strip())


def configure_rustdesk(args, password, changes, warnings):
    step("等待 RustDesk 服务就绪")
    rid = wait_rustdesk_ipc(timeout=args.ipc_timeout)
    if not rid:
        warnings.append("RustDesk 服务没有就绪（取不到 ID），可能是没有图形会话；继续尝试设置")
    wanted = [
        ("direct-server", "Y"),
        ("direct-access-port", str(args.port)),
        ("verification-method", "use-permanent-password"),
        ("approve-mode", "password"),
    ]
    if args.whitelist:
        wanted.append(("whitelist", merge_whitelist(rd_get_option("whitelist"), args.whitelist)))
    step("设置 RustDesk 选项")
    for key, value in wanted:
        old = rd_get_option(key)
        if old == value:
            continue
        if not rd_set_option(key, value):
            raise ArdError("设置 RustDesk 选项失败：%s=%s" % (key, value))
        changes.append("RustDesk %s：%s → %s" % (key, old or "（空）", value))
    step("设置 RustDesk 固定密码")
    ok, msg = rd_set_password(password, timeout=args.ipc_timeout)
    if not ok:
        raise ArdError("设置 RustDesk 密码失败：%s" % msg)
    log("RustDesk 固定密码已设置")
    return rid


def wait_listening(port, timeout=30):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if port in listen_ports():
            return True
        time.sleep(1)
    return False


def cmd_configure(args):
    require_root()
    password = _read_password(args.password_file)
    if not rustdesk_bin():
        raise ArdError("B 上还没有安装 RustDesk")
    state = load_state()
    changes = []
    warnings = []

    step("配置登录界面与显示")
    display_changed = configure_gdm(state, changes)
    if args.headless != "skip":
        changed, _active = configure_headless(args, state, changes)
        display_changed = display_changed or changed
    if args.prevent_sleep:
        configure_sleep(state, changes)
    configure_firewall(args, changes)

    step("启动 RustDesk 服务")
    ensure_rustdesk_service()

    sess = sessions()
    user_sessions = graphical_user_sessions(sess)
    need_restart = display_changed or (not greeter_sessions(sess) and not user_sessions)
    restarted = False
    if need_restart and not has_display_manager_unit():
        warnings.append("B 上没有启用显示管理器（display-manager.service），RustDesk 可能没有画面")
        need_restart = False
    if need_restart:
        if args.restart_dm == "yes" or (args.restart_dm == "auto" and not user_sessions):
            step("重启登录界面使设置生效")
            restart_display_manager()
            systemctl("restart", "rustdesk")
            restarted = True
        elif user_sessions:
            warnings.append("B 上有用户已登录，显示设置要重启登录界面或重启 B 后才生效")

    rid = configure_rustdesk(args, password, changes, warnings)

    step("检查 RustDesk 直连端口")
    listening = wait_listening(args.port, timeout=30)
    if not listening:
        log("直连端口还没有打开，重启 RustDesk 服务后再检查")
        systemctl("restart", "rustdesk")
        wait_rustdesk_ipc(timeout=args.ipc_timeout)
        listening = wait_listening(args.port, timeout=30)
    if not listening:
        warnings.append("RustDesk 没有在 %d 端口监听" % args.port)

    state["configured"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    state["port"] = args.port
    save_state(state)
    return {
        "ok": True,
        "changes": changes,
        "warnings": warnings,
        "needs_restart": need_restart and not restarted,
        "restarted_display": restarted,
        "listening": listening,
        "id": rid or rd_get_id(timeout=10),
        "rustdesk_version": dpkg_version("rustdesk"),
    }


# ---------------------------------------------------------------------------
# 子命令：link-ip / status / revert
# ---------------------------------------------------------------------------


def cmd_link_ip(args):
    require_root()
    want = ipaddress.ip_interface(args.cidr)
    net = want.network
    itf = find_iface_by_mac(args.mac)
    if not itf:
        raise ArdError("B 上找不到 MAC 为 %s 的网口" % args.mac)
    for other in interfaces():
        if other["name"] == itf["name"]:
            continue
        for cidr in other["ipv4"]:
            if ipaddress.ip_interface(cidr).network.overlaps(net):
                raise ArdError(
                    "直连网段 %s 与 B 的网口 %s（%s）冲突，请在设置里换一个直连网段" % (net, other["name"], cidr)
                )
    for cidr in itf["ipv4"]:
        if ipaddress.ip_interface(cidr).ip in net:
            return {"ok": True, "iface": itf["name"], "address": cidr, "added": False}
    run(["ip", "link", "set", itf["name"], "up"], timeout=15)
    run(["ip", "addr", "add", str(want), "dev", itf["name"]], timeout=15, check=True)
    log("已给 %s 临时添加地址 %s（重启后消失）" % (itf["name"], want))
    return {"ok": True, "iface": itf["name"], "address": str(want), "added": True}


def cmd_status(args):
    return {"ok": True, "rustdesk": rustdesk_status(args.port)}


def cmd_revert(args):
    require_root()
    state = load_state()
    changes = []
    if os.path.exists(DUMMY_ACTIVE):
        os.remove(DUMMY_ACTIVE)
        changes.append("移除虚拟显示器配置")
    if os.path.exists(DISPLAY_UNIT):
        systemctl("disable", DISPLAY_UNIT_NAME)
        os.remove(DISPLAY_UNIT)
        systemctl("daemon-reload")
        changes.append("移除开机切换服务")
    bak = state.get("gdm_backup")
    if bak and os.path.exists(bak):
        shutil.copy2(bak, bak[: -len(".autorustdesk.bak")])
        changes.append("恢复 GDM 配置")
    before = state.get("sleep_masked_before") or {}
    to_unmask = [t for t, was in before.items() if not was]
    if to_unmask:
        systemctl("unmask", *to_unmask)
        changes.append("恢复休眠设置")
    return {"ok": True, "changes": changes, "needs_restart": bool(changes)}


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def build_parser():
    p = argparse.ArgumentParser(prog="ard_remote.py")
    sub = p.add_subparsers(dest="cmd")

    s = sub.add_parser("probe")
    s.add_argument("--link-mac")
    s.add_argument("--port", type=int, default=DEFAULT_PORT)
    s.set_defaults(func=cmd_probe)

    s = sub.add_parser("install")
    s.add_argument("--bundle", required=True)
    s.add_argument("--workdir")
    s.add_argument("--packages", nargs="*")
    s.add_argument("--keep", action="store_true")
    s.set_defaults(func=cmd_install)

    s = sub.add_parser("configure")
    s.add_argument("--password-file", required=True)
    s.add_argument("--port", type=int, default=DEFAULT_PORT)
    s.add_argument("--whitelist", help="允许连接的网段，如 192.168.77.0/24")
    s.add_argument("--firewall-subnet", help="ufw 启用时放行的网段")
    s.add_argument("--headless", choices=["auto", "on", "off", "skip"], default="auto")
    s.add_argument("--resolution", default="1920x1080")
    s.add_argument("--prevent-sleep", action="store_true")
    s.add_argument("--restart-dm", choices=["auto", "yes", "no"], default="auto")
    s.add_argument("--ipc-timeout", type=int, default=60)
    s.set_defaults(func=cmd_configure)

    s = sub.add_parser("link-ip")
    s.add_argument("--mac", required=True)
    s.add_argument("--cidr", required=True)
    s.set_defaults(func=cmd_link_ip)

    s = sub.add_parser("status")
    s.add_argument("--port", type=int, default=DEFAULT_PORT)
    s.set_defaults(func=cmd_status)

    s = sub.add_parser("display-switch")
    s.add_argument("--boot", action="store_true")
    s.set_defaults(func=cmd_display_switch)

    s = sub.add_parser("restart-display")
    s.add_argument("--force", action="store_true")
    s.set_defaults(func=cmd_restart_display)

    s = sub.add_parser("revert")
    s.set_defaults(func=cmd_revert)
    return p


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 2
    try:
        result = args.func(args)
    except ArdError as e:
        emit_result({"ok": False, "error": str(e)})
        return 1
    except Exception as e:  # noqa: BLE001 - 任何意外都要把结果告诉 A
        emit_result({"ok": False, "error": "%s: %s" % (type(e).__name__, e)})
        return 1
    emit_result(result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
