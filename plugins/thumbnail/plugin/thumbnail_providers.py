"""缩略图 provider 协议与内置实现（能力插件模式，与 OCR providers 同构）。

按适配器模式隔离，零依赖环境也可运行：
- StubThumbnailProvider：本地占位实现，零依赖生成合法 PNG 占位缩略图
  （纯 Python + zlib：基于文件名哈希的纯色块），明确标注 engine="stub"。
  用途：流程打通、契约测试、离线演示。
- PillowThumbnailProvider：真实缩放实现（惰性导入 Pillow），仅在运行环境
  已安装 Pillow 时注册可用；支持 EXIF 方向校正、动图取首帧、透明图保留 RGBA。
- auto：默认选择，Pillow 可用则真实缩放，否则自动降级 stub 并在结果里带 notice。
真实 provider 后续实现同一协议注册即可，不改插件/API。
"""

from __future__ import annotations

import hashlib
import struct
import zlib
from typing import Protocol

DEFAULT_SIZES: tuple[int, ...] = (128, 256)
MIN_SIZE = 16
MAX_SIZE = 2048


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
            "notice": "占位引擎：未执行真实缩放，输出纯色占位缩略图",
        }


class PillowThumbnailProvider:
    """真实缩放 provider：惰性导入 Pillow，保持等比缩放并输出 PNG。"""

    name = "pillow"

    def make(self, data: bytes, filename: str, mime: str, size: int) -> dict:
        try:
            from io import BytesIO

            from PIL import Image, ImageOps, UnidentifiedImageError
        except ImportError as exc:  # noqa: BLE001
            raise RuntimeError(
                "pillow provider 需要安装 Pillow（pip install Pillow）；"
                "当前环境未安装，可改用 auto/stub provider"
            ) from exc

        if not data:
            raise ValueError(f"空文件无法生成缩略图: {filename}")
        resample = getattr(getattr(Image, "Resampling", Image), "LANCZOS")
        try:
            with Image.open(BytesIO(data)) as img:
                if getattr(img, "is_animated", False):
                    img.seek(0)  # 动图只取首帧，避免逐帧解码
                oriented = ImageOps.exif_transpose(img)
                # 统一转 RGBA：兼容 P / CMYK / 16bit 等模式，并保留透明通道
                canvas = (oriented or img).convert("RGBA")
                canvas.thumbnail((size, size), resample)  # 等比缩放，只缩不放
                out = BytesIO()
                canvas.save(out, format="PNG", optimize=True)
                width, height = canvas.size
        except Image.DecompressionBombError as exc:
            raise ValueError(f"图片像素超出安全上限，已拒绝处理: {filename}") from exc
        except (UnidentifiedImageError, OSError) as exc:
            raise ValueError(f"不支持或已损坏的图片: {filename}") from exc

        return {
            "data": out.getvalue(),
            "width": width,
            "height": height,
            "format": "png",
            "engine": "pillow",
        }


def _pillow_available() -> bool:
    try:
        import PIL.Image  # noqa: F401
    except ImportError:
        return False
    return True


_PROVIDERS: dict[str, type] = {"stub": StubThumbnailProvider}
if _pillow_available():  # Pillow 可用时才暴露真实 provider（保持零依赖环境可用）
    _PROVIDERS["pillow"] = PillowThumbnailProvider


def available_providers() -> list[str]:
    return ["auto", *sorted(_PROVIDERS)]


def register_provider(name: str, cls: type) -> None:
    _PROVIDERS[name] = cls


def resolve_provider_name(name: str | None = "auto") -> str:
    """auto 按环境能力选择；显式指定的引擎原样返回（可用性由 build_provider 校验）。"""
    if not name or name == "auto":
        return "pillow" if "pillow" in _PROVIDERS else "stub"
    return name


def build_provider(name: str = "auto") -> ThumbnailProvider:
    resolved = resolve_provider_name(name)
    cls = _PROVIDERS.get(resolved)
    if cls is None:
        raise ValueError(
            f"未注册的缩略图 provider: {name}（可用: {available_providers()}）")
    return cls()
