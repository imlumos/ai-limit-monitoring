"""从默认 PNG 生成 Windows 多尺寸 ICO,仅做等比缩放与格式转换。"""
from pathlib import Path
import struct
import sys

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, Qt
from PySide6.QtGui import QImage

ROOT = Path(__file__).resolve().parents[1]
SIZES = (16, 24, 32, 48, 64, 128, 256)


def build_icon(source: Path, target: Path):
    image = QImage(str(source))
    if image.isNull() or image.width() != image.height():
        raise ValueError("默认图标需为可读取的正方形图片")
    frames = []
    for size in SIZES:
        scaled = image.scaled(size, size, Qt.AspectRatioMode.KeepAspectRatio,
                              Qt.TransformationMode.SmoothTransformation)
        encoded = QByteArray()
        buffer = QBuffer(encoded)
        buffer.open(QIODevice.OpenModeFlag.WriteOnly)
        if not scaled.save(buffer, "PNG"):
            raise ValueError("无法编码图标")
        frames.append(bytes(encoded))
    offset = 6 + 16 * len(SIZES)
    header = bytearray(struct.pack("<HHH", 0, 1, len(SIZES)))
    for size, frame in zip(SIZES, frames):
        header.extend(struct.pack("<BBBBHHII", size % 256, size % 256, 0, 0,
                                  1, 32, len(frame), offset))
        offset += len(frame)
    target.write_bytes(bytes(header) + b"".join(frames))


if __name__ == "__main__":
    source = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "faviconV2.png"
    target = Path(sys.argv[2]) if len(sys.argv) > 2 else ROOT / "favicon.ico"
    build_icon(source, target)
    print(f"Created {target.name}: {', '.join(str(size) for size in SIZES)} px")
