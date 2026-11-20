"""缩略图 provider 协议与内置实现（能力插件模式，与 OCR providers 同构）。

按适配器模式隔离，未获批准前不引入 Pillow 等第三方图像依赖：
- StubThumbnailProvider：本地占位实现，零依赖生成合法 PNG 占位缩略图
  （纯 Python + zlib：基于文件名哈希的纯色块），明确标注 engine="stub"。
  用途：流程打通、契约测试、离线演示。
- PillowThumbnailProvider：真实缩放实现（惰性导入 Pillow），仅在运行环境
  已安装 Pillow 时注册可用；未安装时 build_provider("pillow") 给出明确提示。
  真实 provider 后续实现同一协议注册即可，不改插件/API。
"""

from __future__ import annotations

import hashlib
import struct
import zlib
from typing import Protocol

DEFAULT_SIZES: tuple[int, ...] = (128, 256)


class ThumbnailProvider(Protocol):
    name: str

    def make(self, data: bytes, filename: str, mime: str, size: int) -> dict:
        """返回 {data, width, height, format, engine}；data 为编码后图片字节。"""
        ...


def _png_chunk(ctype: bytes, payload: bytes) -> bytes:
    """构造单个 PNG chunk（长度 + 类型 + 数据 + CRC）。"""
    crc = zlib.crc32(ctype + payload) & 0xFFFFFFFF
    return struct.pack(">I", len(payload)) + ctype + payload + struct.pack(">I", crc)


def placeholder_png(width: int, height: int, rgb: tuple[int, int, int]) -> bytes:
    """零依赖生成合法 PNG（truecolor RGB，纯色占位）。"""
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    # 每行：filter byte 0 + W 像素 × RGB 3 字节；共 H 行
    row = b"\x00" + bytes(rgb) * width
    idat = zlib.compress(row * height, 9)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", ihdr)
        + _png_chunk(b"IDAT", idat)
        + _png_chunk(b"IEND", b"")
    )


class StubThumbnailProvider:
    """占位 provider：按文件名哈希生成纯色 PNG 占位图，不做真实缩放。"""

    name = "stub"

    def make(self, data: bytes, filename: str, mime: str, size: int) -> dict:
        digest = hashlib.md5((filename or "").encode("utf-8")).hexdigest()
        rgb = tuple(int(digest[i:i + 2], 16) for i in (0, 2, 4))
        png = placeholder_png(size, size, rgb)  # type: ignore[arg-type]
        return {
            "data": png,
            "width": size,
            "height": size,
            "format": "png",
            "engine": "stub",
            "notice": "占位引擎：未安装 Pillow，当前生成纯色占位缩略图而非真实缩放",
        }


class PillowThumbnailProvider:
    """真实缩放 provider：惰性导入 Pillow，保持等比缩放并输出 PNG。"""

    name = "pillow"

    def make(self, data: bytes, filename: str, mime: str, size: int) -> dict:
        try:
            from io import BytesIO

            from PIL import Image
        except ImportError as exc:  # noqa: BLE001
            raise RuntimeError(
                "pillow provider 需要安装 Pillow（pip install Pillow）；"
                "当前环境未安装，可改用 stub provider"
            ) from exc

        with Image.open(BytesIO(data)) as img:
            img.thumbnail((size, size))
            out = BytesIO()
            img.save(out, format="PNG")
            return {
                "data": out.getvalue(),
                "width": img.width,
                "height": img.height,
                "format": "png",
                "engine": "pillow",
            }


_PROVIDERS: dict[str, type] = {"stub": StubThumbnailProvider}
try:  # Pillow 可用时才暴露真实 provider（保持零依赖环境可用）
    import PIL  # noqa: F401

    _PROVIDERS["pillow"] = PillowThumbnailProvider
except ImportError:
    pass


def available_providers() -> list[str]:
    return sorted(_PROVIDERS)


def register_provider(name: str, cls: type) -> None:
    _PROVIDERS[name] = cls


def build_provider(name: str = "stub") -> ThumbnailProvider:
    cls = _PROVIDERS.get(name)
    if cls is None:
        raise ValueError(f"未注册的缩略图 provider: {name}（可用: {sorted(_PROVIDERS)}）")
    return cls()
