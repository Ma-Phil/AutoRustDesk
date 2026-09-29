"""生成程序图标：蓝色圆角方块上一个白色网线水晶头。

输出：
    autorustdesk/gui/autorustdesk.png   256×256，窗口图标、Linux 菜单图标
    packaging/icons/autorustdesk.ico    Windows（16~256）
    packaging/icons/autorustdesk.icns   macOS（16~1024）

用 PySide6 绘制，.ico / .icns 直接按格式写 PNG 数据，不需要其它工具：
    QT_QPA_PLATFORM=offscreen python packaging/make_icons.py
"""

import os
import struct
import sys

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QRectF, Qt
from PySide6.QtGui import QColor, QGuiApplication, QImage, QLinearGradient, QPainter, QPainterPath

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BLUE_TOP = QColor("#2F80ED")
BLUE_BOTTOM = QColor("#1A5DC2")
WHITE = QColor("#FFFFFF")


def draw(size: int) -> QImage:
    img = QImage(size, size, QImage.Format_ARGB32_Premultiplied)
    img.fill(Qt.transparent)
    p = QPainter(img)
    p.setRenderHint(QPainter.Antialiasing)
    p.scale(size / 1024.0, size / 1024.0)
    p.setPen(Qt.NoPen)

    # 背景：macOS 图标的标准留白（824×824，圆角 185）
    bg = QPainterPath()
    bg.addRoundedRect(QRectF(100, 100, 824, 824), 185, 185)
    grad = QLinearGradient(0, 100, 0, 924)
    grad.setColorAt(0, BLUE_TOP)
    grad.setColorAt(1, BLUE_BOTTOM)
    p.fillPath(bg, grad)

    # 水晶头：卡扣、插头主体、网线
    plug = QPainterPath()
    plug.addRoundedRect(QRectF(427, 262, 170, 110), 18, 18)  # 卡扣
    plug.addRoundedRect(QRectF(322, 340, 380, 330), 40, 40)  # 主体
    plug.addRoundedRect(QRectF(452, 640, 120, 230), 26, 26)  # 网线
    plug.setFillRule(Qt.WindingFill)
    p.fillPath(plug.simplified(), WHITE)

    # 金属触点
    for i in range(6):
        x = 512 - 5 * 26 + i * 52 - 11
        p.fillRect(QRectF(x, 390, 22, 110), BLUE_TOP)
    p.end()
    return img


def png_bytes(img: QImage) -> bytes:
    data = QByteArray()
    buf = QBuffer(data)
    buf.open(QIODevice.WriteOnly)
    img.save(buf, "PNG")
    buf.close()
    return bytes(data)


def write_ico(path: str, sizes) -> None:
    """ICO：目录项 + PNG 数据（Windows Vista 起支持 PNG 压缩的图标）。"""
    pngs = [(s, png_bytes(draw(s))) for s in sizes]
    offset = 6 + 16 * len(pngs)
    entries = b""
    blobs = b""
    for s, png in pngs:
        w = 0 if s >= 256 else s  # 0 表示 256
        entries += struct.pack("<BBBBHHII", w, w, 0, 0, 1, 32, len(png), offset + len(blobs))
        blobs += png
    with open(path, "wb") as f:
        f.write(struct.pack("<HHH", 0, 1, len(pngs)) + entries + blobs)


ICNS_TYPES = {16: b"icp4", 32: b"icp5", 64: b"icp6", 128: b"ic07", 256: b"ic08", 512: b"ic09", 1024: b"ic10"}


def write_icns(path: str) -> None:
    """ICNS：每个尺寸一段（类型 + 长度 + PNG 数据）。"""
    chunks = b""
    for s, kind in sorted(ICNS_TYPES.items()):
        png = png_bytes(draw(s))
        chunks += kind + struct.pack(">I", len(png) + 8) + png
    with open(path, "wb") as f:
        f.write(b"icns" + struct.pack(">I", len(chunks) + 8) + chunks)


_APP = None  # 绘图需要 QGuiApplication，要一直持有


def main() -> int:
    global _APP
    _APP = QGuiApplication.instance() or QGuiApplication(sys.argv[:1])
    icons = os.path.join(ROOT, "packaging", "icons")
    os.makedirs(icons, exist_ok=True)
    with open(os.path.join(ROOT, "autorustdesk", "gui", "autorustdesk.png"), "wb") as f:
        f.write(png_bytes(draw(256)))
    write_ico(os.path.join(icons, "autorustdesk.ico"), [16, 24, 32, 48, 64, 128, 256])
    write_icns(os.path.join(icons, "autorustdesk.icns"))
    print("已生成图标")
    return 0


if __name__ == "__main__":
    sys.exit(main())
