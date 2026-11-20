"""压缩包解析与打包核心（纯标准库，平台无关）。

支持只读解析 zip / tar / tar.gz / tar.bz2 / tar.xz，写侧仅提供 zip 打包。
安全边界（对齐平台 8MB 流式与安装限制口径）：

- 条目名统一清洗：反斜杠转 /，拒绝绝对路径、盘符、``..`` 与空字节（防 Zip Slip）；
- 解析上限：条目数、单条目字节、累计字节，超限即截断或跳过（防压缩炸弹）；
- 加密 ZIP 不支持：列表标注 encrypted，读取明确报错；
- 全程内存操作，不落盘，不使用 extractall / extract。
"""

from __future__ import annotations

import io
import mimetypes
import re
import tarfile
import zipfile

DEFAULT_MAX_ENTRIES = 2000
DEFAULT_MAX_ENTRY_BYTES = 64 * 1024 * 1024
DEFAULT_MAX_TOTAL_BYTES = 512 * 1024 * 1024

MAX_ENTRIES_LIMIT = 10000
MAX_ENTRY_BYTES_LIMIT = 1024 * 1024 * 1024
MAX_TOTAL_BYTES_LIMIT = 4096 * 1024 * 1024

ENCODINGS = ("auto", "utf-8", "gbk", "cp437")

ZIP_FORMATS = ("zip",)
TAR_FORMATS = ("tar", "tar.gz", "tar.bz2", "tar.xz")
SUPPORTED_FORMATS = ZIP_FORMATS + TAR_FORMATS

# --- 条目预览口径（与 preview 插件的 classify 保持一致；安全红线禁止跨插件导入，故本地维护） ---
IMAGE_EXTS = frozenset({"png", "jpg", "jpeg", "gif", "webp", "bmp", "ico"})
PDF_EXTS = frozenset({"pdf"})
VIDEO_EXTS = frozenset({"mp4", "webm", "mov", "m4v", "ogv"})
AUDIO_EXTS = frozenset({"mp3", "wav", "ogg", "m4a", "aac", "flac", "opus"})
TEXT_EXTS = frozenset({
    "txt", "md", "json", "csv", "log", "xml", "yml", "yaml", "ini", "cfg", "toml",
    "py", "js", "ts", "java", "c", "cpp", "h", "go", "rs", "sh", "sql", "rst",
    "html", "css",
})
OFFICE_EXTS = frozenset({
    "doc", "docx", "xls", "xlsx", "ppt", "pptx", "wps", "odt", "ods", "odp", "rtf",
})
ARCHIVE_EXTS = frozenset({"zip", "tar", "gz", "tgz", "bz2", "xz", "7z"})

PREVIEW_TYPES = ("image", "pdf", "video", "audio", "text", "docx", "office", "archive", "none")

#: 明确识别但暂不支持，用于给出可操作的错误提示
UNSUPPORTED_MAGIC = {
    b"7z\xbc\xaf\x27\x1c": "7z",
    b"Rar!\x1a\x07": "rar",
}

_DRIVE_RE = re.compile(r"^[A-Za-z]:")
_GZIP_MAGIC = b"\x1f\x8b"
_BZ2_MAGIC = b"BZh"
_XZ_MAGIC = b"\xfd7zXZ\x00"
_ZIP_MAGICS = (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")


# ---------------------------------------------------------------- 基础工具

def detect_format(data: bytes, filename: str = "") -> str:
    """按魔数判定格式，失败时按扩展名兜底；不支持的格式抛 ValueError。"""
    if data[:4] in _ZIP_MAGICS:
        return "zip"
    if data[:2] == _GZIP_MAGIC:
        return "tar.gz"
    if data[:3] == _BZ2_MAGIC:
        return "tar.bz2"
    if data[:6] == _XZ_MAGIC:
        return "tar.xz"
    if len(data) > len(b"ustar") + 257 and data[257:262] == b"ustar":
        return "tar"
    for magic, name in UNSUPPORTED_MAGIC.items():
        if data.startswith(magic):
            raise ValueError(f"暂不支持 {name} 格式，请使用 zip 或 tar 系压缩包")

    lowered = (filename or "").lower()
    if lowered.endswith(".zip"):
        return "zip"
    for fmt in ("tar.gz", "tar.bz2", "tar.xz"):
        if lowered.endswith("." + fmt):
            return fmt
    if lowered.endswith(".tar"):
        return "tar"
    raise ValueError(f"无法识别的压缩包格式: {filename or '未命名'}")


def safe_entry_name(raw: str) -> str:
    """清洗条目名：去掉空段与 ``.``，拒绝穿越；目录尾斜杠会被去掉。"""
    if not raw or "\x00" in raw:
        raise ValueError("压缩包条目名为空或含空字节")
    name = raw.replace("\\", "/")
    if name.startswith("/") or _DRIVE_RE.match(name):
        raise ValueError(f"压缩包条目使用绝对路径，已拒绝: {raw}")
    parts = [part for part in name.split("/") if part not in ("", ".")]
    if any(part == ".." for part in parts):
        raise ValueError(f"压缩包条目存在目录穿越，已拒绝: {raw}")
    if not parts:
        raise ValueError(f"压缩包条目路径非法: {raw}")
    return "/".join(parts)


def guess_mime(name: str) -> str:
    guessed, _ = mimetypes.guess_type(name)
    return guessed or "application/octet-stream"


def _ext(name: str) -> str:
    base = name.rsplit("/", 1)[-1]
    return base.rsplit(".", 1)[-1].lower() if "." in base else ""


def classify_entry(name: str) -> dict:
    """按条目名给出前端预览口径：{mime, previewable, preview_type, hint?}。

    口径与 preview 插件一致：图片/PDF/音视频/文本/docx 可内联；Office 需转换服务；
    SVG 因可执行脚本不内联；嵌套压缩包标记为 archive，前端可继续展开。
    """
    ext = _ext(name)
    mime = guess_mime(name)
    if ext in IMAGE_EXTS:
        return {"mime": mime, "previewable": True, "preview_type": "image"}
    if ext in PDF_EXTS:
        return {"mime": mime, "previewable": True, "preview_type": "pdf"}
    if ext in VIDEO_EXTS:
        return {"mime": mime, "previewable": True, "preview_type": "video"}
    if ext in AUDIO_EXTS:
        return {"mime": mime, "previewable": True, "preview_type": "audio"}
    if ext in TEXT_EXTS:
        return {"mime": mime, "previewable": True, "preview_type": "text"}
    if ext == "docx":
        return {"mime": mime, "previewable": True, "preview_type": "docx"}
    if ext in OFFICE_EXTS:
        return {"mime": mime, "previewable": False, "preview_type": "office",
                "hint": "Office 文档需转换服务，建议下载后查看"}
    if ext in ARCHIVE_EXTS:
        return {"mime": mime, "previewable": True, "preview_type": "archive",
                "hint": "嵌套压缩包，可继续展开"}
    if ext == "svg":
        return {"mime": mime, "previewable": False, "preview_type": "none",
                "hint": "SVG 可在同源执行脚本，不提供内联预览"}
    return {"mime": mime, "previewable": False, "preview_type": "none",
            "hint": "不支持内联预览，建议下载"}


def _clip(value, low: int, high: int, default: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, number))


def clamp_limits(max_entries=None, max_entry_bytes=None, max_total_bytes=None) -> tuple[int, int, int]:
    """把配置值收敛到安全区间，供插件与核心共用。"""
    return (
        _clip(max_entries, 1, MAX_ENTRIES_LIMIT, DEFAULT_MAX_ENTRIES),
        _clip(max_entry_bytes, 1, MAX_ENTRY_BYTES_LIMIT, DEFAULT_MAX_ENTRY_BYTES),
        _clip(max_total_bytes, 1, MAX_TOTAL_BYTES_LIMIT, DEFAULT_MAX_TOTAL_BYTES),
    )


def _zip_entry_name(info: zipfile.ZipInfo, encoding: str) -> str:
    """还原 ZIP 文件名：UTF-8 标记位优先；旧包按配置编码或自动嗅探。"""
    name = info.filename
    if info.flag_bits & 0x800:
        return name
    try:
        raw = name.encode("cp437")
    except UnicodeEncodeError:
        return name
    candidates = [encoding] if encoding and encoding != "auto" else ["utf-8", "gb18030"]
    for candidate in candidates:
        try:
            return raw.decode(candidate)
        except UnicodeDecodeError:
            continue
    return name


def _zip_is_dir(info: zipfile.ZipInfo) -> bool:
    return info.is_dir() or info.filename.endswith("/")


def _zip_encrypted(info: zipfile.ZipInfo) -> bool:
    return bool(info.flag_bits & 0x1)


def _format_mtime_zip(info: zipfile.ZipInfo) -> str | None:
    try:
        year, month, day, hour, minute, second = info.date_time
        return f"{year:04d}-{month:02d}-{day:02d}T{hour:02d}:{minute:02d}:{second:02d}"
    except (TypeError, ValueError):
        return None


def _tar_kind(member: tarfile.TarInfo) -> str:
    if member.isdir():
        return "dir"
    if member.issym() or member.islnk():
        return "link"
    if member.isreg():
        return "file"
    return "other"


def _describe(name_raw: str, size: int, is_dir: bool, mtime, encoded: bool = False) -> dict:
    try:
        name = safe_entry_name(name_raw)
        safe = True
    except ValueError:
        name = name_raw.replace("\\", "/")
        safe = False
    item = {
        "name": name,
        "size": int(size or 0),
        "is_dir": is_dir,
        "safe": safe,
        "mtime": mtime,
        "encrypted": encoded,
    }
    if is_dir:
        item.update({"mime": "inode/directory", "previewable": False, "preview_type": "none"})
    else:
        item.update(classify_entry(name))
    return item


# ---------------------------------------------------------------- 读取

def _zip_members(data: bytes, encoding: str, max_entries: int):
    zf = zipfile.ZipFile(io.BytesIO(data))
    infos = zf.infolist()
    return zf, infos[:max_entries], len(infos) > max_entries


def _tar_members(data: bytes, max_entries: int):
    tf = tarfile.open(fileobj=io.BytesIO(data), mode="r:*")
    members: list[tarfile.TarInfo] = []
    truncated = False
    try:
        while True:
            member = tf.next()
            if member is None:
                break
            if len(members) >= max_entries:
                truncated = True
                break
            members.append(member)
    except Exception:
        tf.close()
        raise
    return tf, members, truncated


def list_entries(data: bytes, filename: str = "", *, max_entries: int | None = None,
                 encoding: str = "auto") -> dict:
    """列出条目元数据（不解压内容），不因单个非法条目而失败。"""
    if not data:
        raise ValueError(f"空文件无法解析: {filename or '未命名'}")
    fmt = detect_format(data, filename)
    max_entries = clamp_limits(max_entries=max_entries)[0]

    if fmt in ZIP_FORMATS:
        zf, infos, truncated = _zip_members(data, encoding, max_entries)
        try:
            total = 0
            entries = []
            for info in infos:
                is_dir = _zip_is_dir(info)
                item = _describe(_zip_entry_name(info, encoding), info.file_size, is_dir,
                                 _format_mtime_zip(info), _zip_encrypted(info))
                item["type"] = "dir" if is_dir else "file"
                item["compressed_size"] = int(info.compress_size or 0)
                total += item["size"]
                entries.append(item)
        finally:
            zf.close()
    else:
        tf, members, truncated = _tar_members(data, max_entries)
        try:
            total = 0
            entries = []
            for member in members:
                kind = _tar_kind(member)
                item = _describe(member.name, member.size, kind == "dir", int(member.mtime or 0))
                item["type"] = kind
                item["compressed_size"] = None
                total += item["size"]
                entries.append(item)
        finally:
            tf.close()

    return {
        "format": fmt,
        "count": len(entries),
        "declared_bytes": total,
        "truncated": truncated,
        "entries": entries,
    }


def _collect_zip(data: bytes, filename: str, names: list[str] | None, max_entries: int,
                 max_entry_bytes: int, max_total_bytes: int, encoding: str) -> dict:
    zf, infos, truncated = _zip_members(data, encoding, max_entries)
    wanted = set(names) if names is not None else None
    entries: list[dict] = []
    skipped: list[dict] = []
    total = 0
    try:
        for info in infos:
            raw_name = _zip_entry_name(info, encoding)
            if _zip_is_dir(info):
                continue
            try:
                name = safe_entry_name(raw_name)
            except ValueError as exc:
                if wanted is None:  # 只读全部时才回报非法条目，单条读取不产生噪声
                    skipped.append({"name": raw_name, "reason": str(exc)})
                continue
            if wanted is not None and name not in wanted:
                continue
            if _zip_encrypted(info):
                skipped.append({"name": name, "reason": "加密条目暂不支持"})
                continue
            if info.file_size > max_entry_bytes:
                skipped.append({"name": name, "reason": f"单条目超过上限 {max_entry_bytes} 字节"})
                continue
            if total + info.file_size > max_total_bytes:
                skipped.append({"name": name, "reason": f"累计超过上限 {max_total_bytes} 字节"})
                truncated = True
                continue
            with zf.open(info) as stream:
                payload = stream.read(max_entry_bytes)
            payload = payload[:max_total_bytes - total]
            total += len(payload)
            preview = classify_entry(name)
            entries.append({
                "name": name,
                "size": len(payload),
                "declared_size": int(info.file_size or 0),
                "data": payload,
                "mime": preview["mime"],
                "previewable": preview["previewable"],
                "preview_type": preview["preview_type"],
                "truncated": len(payload) < info.file_size,
            })
    finally:
        zf.close()
    return {
        "format": "zip",
        "count": len(entries),
        "total_bytes": total,
        "truncated": truncated,
        "entries": entries,
        "skipped": skipped,
    }


def _collect_tar(data: bytes, filename: str, names: list[str] | None, max_entries: int,
                 max_entry_bytes: int, max_total_bytes: int) -> dict:
    tf, members, truncated = _tar_members(data, max_entries)
    wanted = set(names) if names is not None else None
    entries: list[dict] = []
    skipped: list[dict] = []
    total = 0
    try:
        for member in members:
            kind = _tar_kind(member)
            if kind == "dir":
                continue
            try:
                name = safe_entry_name(member.name)
            except ValueError as exc:
                if wanted is None:
                    skipped.append({"name": member.name, "reason": str(exc)})
                continue
            if wanted is not None and name not in wanted:
                continue
            if kind != "file":
                skipped.append({"name": name, "reason": "仅支持常规文件，链接与设备文件已跳过"})
                continue
            if member.size > max_entry_bytes:
                skipped.append({"name": name, "reason": f"单条目超过上限 {max_entry_bytes} 字节"})
                continue
            if total + member.size > max_total_bytes:
                skipped.append({"name": name, "reason": f"累计超过上限 {max_total_bytes} 字节"})
                truncated = True
                continue
            stream = tf.extractfile(member)
            if stream is None:
                skipped.append({"name": name, "reason": "条目内容不可读"})
                continue
            with stream:
                payload = stream.read(max_entry_bytes)
            payload = payload[:max_total_bytes - total]
            total += len(payload)
            preview = classify_entry(name)
            entries.append({
                "name": name,
                "size": len(payload),
                "declared_size": int(member.size or 0),
                "data": payload,
                "mime": preview["mime"],
                "previewable": preview["previewable"],
                "preview_type": preview["preview_type"],
                "truncated": len(payload) < member.size,
            })
    finally:
        tf.close()
    return {
        "format": detect_format(data, filename),
        "count": len(entries),
        "total_bytes": total,
        "truncated": truncated,
        "entries": entries,
        "skipped": skipped,
    }


def read_entries(data: bytes, filename: str = "", names: list[str] | None = None, *,
                 max_entries: int | None = None, max_entry_bytes: int | None = None,
                 max_total_bytes: int | None = None, encoding: str = "auto") -> dict:
    """批量读取条目字节；超限条目进 skipped，不中断其余条目。"""
    if not data:
        raise ValueError(f"空文件无法解析: {filename or '未命名'}")
    fmt = detect_format(data, filename)
    max_entries, max_entry_bytes, max_total_bytes = clamp_limits(
        max_entries, max_entry_bytes, max_total_bytes)
    if fmt in ZIP_FORMATS:
        return _collect_zip(data, filename, names, max_entries, max_entry_bytes,
                            max_total_bytes, encoding)
    return _collect_tar(data, filename, names, max_entries, max_entry_bytes, max_total_bytes)


def read_entry(data: bytes, filename: str = "", entry_name: str = "", *,
               max_bytes: int | None = None, max_total_bytes: int | None = None,
               encoding: str = "auto") -> dict:
    """按名称读取单个条目；不存在抛 ValueError，超过上限抛 ValueError。"""
    if not entry_name:
        raise ValueError("缺少条目名 entry_name")
    total_limit = clamp_limits(max_total_bytes=max_total_bytes)[2]
    limit = clamp_limits(max_entry_bytes=max_bytes)[1]
    result = read_entries(data, filename, [safe_entry_name(entry_name)],
                          max_entries=MAX_ENTRIES_LIMIT, max_entry_bytes=limit,
                          max_total_bytes=min(limit, total_limit), encoding=encoding)
    if result["entries"]:
        item = result["entries"][0]
        return {
            "name": item["name"],
            "size": item["size"],
            "declared_size": item["declared_size"],
            "data": item["data"],
            "mime": item["mime"],
            "previewable": item["previewable"],
            "preview_type": item["preview_type"],
            "truncated": item["truncated"],
            "format": result["format"],
        }
    reason = "；".join(f"{s['name']}: {s['reason']}" for s in result["skipped"]) or "条目不存在"
    raise ValueError(f"读取条目失败: {reason}")


# ---------------------------------------------------------------- 打包

def pack_zip(entries, *, max_total_bytes: int | None = None) -> dict:
    """把 {名称: 字节} 或 [(名称, 字节)] 打成 zip（内存内，UTF-8 文件名）。"""
    if isinstance(entries, dict):
        items = list(entries.items())
    else:
        items = list(entries)
    if not items:
        raise ValueError("打包内容为空")
    total_limit = clamp_limits(max_total_bytes=max_total_bytes)[2]

    buffer = io.BytesIO()
    total = 0
    with zipfile.ZipFile(buffer, mode="w", compression=zipfile.ZIP_DEFLATED,
                         compresslevel=6, allowZip64=True) as zf:
        for raw_name, payload in items:
            name = safe_entry_name(raw_name)
            if not isinstance(payload, (bytes, bytearray, memoryview)):
                raise ValueError(f"条目内容必须是字节: {name}")
            blob = bytes(payload)
            total += len(blob)
            if total > total_limit:
                raise ValueError(f"打包内容累计超过上限 {total_limit} 字节")
            zf.writestr(name, blob)
    data = buffer.getvalue()
    return {
        "data": data,
        "format": "zip",
        "count": len(items),
        "size": len(data),
        "uncompressed_bytes": total,
    }
