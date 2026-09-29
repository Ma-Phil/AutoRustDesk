"""助手连回界面的通信方式（macOS/Windows）、ICMPv6 报文、提权脚本。"""

import os
import socket
import struct
import threading

import pytest

from autorustdesk.core.helper_client import applescript_quote, macos_auth_script
from autorustdesk.helper import icmp6
from autorustdesk.helper.transport import Listener, TransportError, connect_back, parse_args


def _connect(listener, token_file, box):
    try:
        box["helper"] = connect_back(listener.address, token_file)
    except Exception as e:  # noqa: BLE001
        box["error"] = e


def test_mutual_auth_and_message_flow():
    listener = Listener()
    try:
        if os.name == "posix":
            assert oct(os.stat(listener.token_file).st_mode & 0o777) == "0o600"
        box = {}
        t = threading.Thread(target=_connect, args=(listener, listener.token_file, box))
        t.start()
        conn, reader = listener.accept(10, lambda: True)
        t.join(5)
        sock, rfile, wfile = box["helper"]
        # 助手 → 界面
        wfile.write('{"type": "ready"}\n')
        wfile.flush()
        assert reader.readline() == b'{"type": "ready"}\n'
        # 界面 → 助手
        conn.sendall(b'{"id": 1, "cmd": "hello"}\n')
        assert rfile.readline() == '{"id": 1, "cmd": "hello"}\n'
        # 界面退出：半关闭连接（界面还有读文件时 close 不会真正关闭套接字）
        conn.shutdown(socket.SHUT_WR)
        assert rfile.readline() == ""  # 助手读到结束，会恢复网卡并退出
        reader.close()
        conn.close()
        sock.close()
    finally:
        listener.close()
    assert not os.path.exists(listener.token_file)


def test_helper_with_wrong_token_is_rejected(tmp_path):
    listener = Listener()
    try:
        bad = tmp_path / "token"
        bad.write_text("0" * 64)
        box = {}
        t = threading.Thread(target=_connect, args=(listener, str(bad), box))
        t.start()
        alive = [True]
        with pytest.raises(TransportError):
            listener.accept(3, lambda: alive[0])
        t.join(5)
        # 助手一方也发现界面的回答对不上（它的令牌是错的），拒绝继续
        assert isinstance(box.get("error"), TransportError)
    finally:
        listener.close()


def test_impostor_gui_cannot_command_helper(tmp_path):
    """冒充界面监听端口的程序不知道令牌，助手会拒绝它。"""
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    token = tmp_path / "token"
    token.write_text("a" * 64)

    def impostor():
        c, _ = srv.accept()
        c.recv(1000)
        c.sendall(b'{"type": "auth", "proof": "0000", "nonce": "x"}\n')
        c.close()

    threading.Thread(target=impostor, daemon=True).start()
    with pytest.raises(TransportError):
        connect_back("127.0.0.1:%d" % srv.getsockname()[1], str(token))
    srv.close()


def test_connect_back_only_to_localhost(tmp_path):
    token = tmp_path / "token"
    token.write_text("a" * 64)
    with pytest.raises(TransportError):
        connect_back("192.0.2.1:1234", str(token))


def test_parse_args():
    assert parse_args(["--connect", "127.0.0.1:5000", "--token-file", "/tmp/x"]) == ("127.0.0.1:5000", "/tmp/x")
    assert parse_args([]) == (None, None)


def test_icmp6_echo_request_checksum():
    pkt = icmp6.echo_request(0x1234, 1, "fe80::1")
    # 按 RFC 4443 验证：连同伪首部求和结果为 0xffff
    pseudo = socket.inet_pton(socket.AF_INET6, "fe80::1") + socket.inet_pton(socket.AF_INET6, "ff02::1")
    pseudo += struct.pack("!I3xB", len(pkt), 58)
    assert icmp6._checksum(pseudo + pkt) == 0
    assert pkt[0] == 128 and struct.unpack("!HH", pkt[4:8]) == (0x1234, 1)
    # 不知道源地址时校验和留给内核填写
    assert icmp6.echo_request(0x1234, 1)[2:4] == b"\x00\x00"


def test_macos_auth_script_quotes_paths_with_spaces_and_quotes():
    cmd = ["/Applications/Auto RustDesk.app/Contents/MacOS/AutoRustDesk", "helper",
           "--connect", "127.0.0.1:5000", "--token-file", '/tmp/a"b/token']
    script = macos_auth_script(cmd)
    assert script.startswith('do shell script "')
    assert script.endswith("with administrator privileges")
    assert applescript_quote('a"b\\c') == '"a\\"b\\\\c"'
    # 解开 AppleScript 字符串后应当是一条合法的 shell 命令，并在后台运行
    inner = script[len("do shell script "):script.index(" with prompt")]
    shell = inner[1:-1].replace('\\"', '"').replace("\\\\", "\\")
    assert shell.endswith(" >/dev/null 2>&1 &")
    import shlex

    assert shlex.split(shell[:-len(" >/dev/null 2>&1 &")]) == cmd
