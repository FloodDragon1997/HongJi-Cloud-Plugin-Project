"""压缩包处理能力插件（能力插件形式接入插件注册表）。

职责：对 zip / tar / tar.gz / tar.bz2 / tar.xz 做元数据列表、单条目读取、批量读取，
以及 zip 打包（供在线浏览、按需下载、批量打包下载复用）。

前端预览优化：
- 条目结构带 previewable / preview_type / mime，前端可直接渲染压缩包目录树并选渲染器；
- preview(data, filename, entry_name) 一次调用即可取目录或单条目（文本类强制 text/plain）；
- 嵌套压缩包标记 preview_type=archive，前端可继续展开；
- manifest 的 ui.preview 将 archive 映射到 iframe，ui.actions 在右键菜单提供
  「查看压缩包」；对应动态路由见 routes.py（条目 JSON、单条目内容、无脚本目录页）。

安全边界：不落盘、不调用 extractall，条目名统一清洗（防 Zip Slip），加密 ZIP 明确拒绝；
纯标准库实现，无运行依赖。

业务侧以 registry.create("archive", config) 取实例后调用下列方法；
方法名与返回结构需与平台调用方约定一致（同 OCR / thumbnail / preview）。
"""

from __future__ import annotations

import sys
from pathlib import Path

# 平台按单文件装载 entry，包内多文件需自行把所在目录加入 sys.path
_HERE = str(Path(__file__).resolve().parent)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)


def _evict_stale_core() -> None:
    """清理进程内残留的旧版本同名模块。

    平台热更新时 sys.modules 里可能还缓存着上一版本目录下的 archive_core，
    直接 import 会命中缓存（如新版本新增的符号在旧模块中不存在）。按 __file__
    与本包目录比对，不一致则丢弃后重新按当前目录加载。
    """
    name = "archive_core"
    cached = sys.modules.get(name)
    if cached is None:
        return
    expected = (Path(_HERE) / f"{name}.py").resolve()
    cached_file = getattr(cached, "__file__", None)
    if not cached_file or Path(cached_file).resolve() != expected:
        del sys.modules[name]


_evict_stale_core()

from app.sdk.store_plugin import HealthStatus  # noqa: E402
from archive_core import (  # noqa: E402
    DEFAULT_MAX_ENTRIES,
    DEFAULT_MAX_ENTRY_BYTES,
    DEFAULT_MAX_TOTAL_BYTES,
    ENCODINGS,
    MAX_ENTRIES_LIMIT,
    MAX_ENTRY_BYTES_LIMIT,
    MAX_TOTAL_BYTES_LIMIT,
    PREVIEW_TYPES,
    SUPPORTED_FORMATS,
    clamp_limits,
    list_entries as _list_entries,
    pack_zip as _pack_zip,
    read_entries as _read_entries,
    read_entry as _read_entry,
)

_MB = 1024 * 1024


class ArchivePlugin:
    plugin_id = "archive"
    display_name = "压缩包处理插件"
    config_schema = [
        {"key": "max_entries", "label": "最大条目数", "type": "number",
         "placeholder": str(DEFAULT_MAX_ENTRIES),
         "hint": f"超出后列表截断、读取跳过，防压缩炸弹；取值 1–{MAX_ENTRIES_LIMIT}"},
        {"key": "max_entry_size", "label": "单条目上限(MB)", "type": "number",
         "placeholder": str(DEFAULT_MAX_ENTRY_BYTES // _MB),
         "hint": f"单个文件解压后字节上限；取值 1–{MAX_ENTRY_BYTES_LIMIT // _MB}"},
        {"key": "max_total_size", "label": "累计解压上限(MB)", "type": "number",
         "placeholder": str(DEFAULT_MAX_TOTAL_BYTES // _MB),
         "hint": f"一次读取全部条目的累积上限；取值 1–{MAX_TOTAL_BYTES_LIMIT // _MB}"},
        {"key": "filename_encoding", "label": "文件名编码", "type": "select",
         "options": list(ENCODINGS), "placeholder": "auto",
         "hint": "仅对未标记 UTF-8 的旧 ZIP 生效；auto 依次尝试 UTF-8、GBK"},
    ]
    required_permissions = ("fs.read", "net.outbound")

    def __init__(self, config: dict | None = None):
        self.config = config or {}
        entry_mb = self.config.get("max_entry_size")
        total_mb = self.config.get("max_total_size")
        self.max_entries, self.max_entry_bytes, self.max_total_bytes = clamp_limits(
            self.config.get("max_entries"),
            int(entry_mb) * _MB if _is_int(entry_mb) else None,
            int(total_mb) * _MB if _is_int(total_mb) else None,
        )
        encoding = str(self.config.get("filename_encoding") or "auto").lower()
        self.encoding = encoding if encoding in ENCODINGS else "auto"

    def capabilities(self) -> dict:
        return {
            "archive": True,
            "formats": list(SUPPORTED_FORMATS),
            "pack_formats": ["zip"],
            "list": True,
            "read": True,
            "pack": True,
            "preview": True,
            "preview_types": list(PREVIEW_TYPES),
            "preview_url": "/api/archive/{file_id}/view",
            "encrypted": False,
            "max_entries": self.max_entries,
            "max_entry_bytes": self.max_entry_bytes,
            "max_total_bytes": self.max_total_bytes,
            "filename_encoding": self.encoding,
        }

    def health_check(self) -> HealthStatus:
        try:
            packed = _pack_zip({"probe/hello.txt": b"archive-probe"},
                               max_total_bytes=self.max_total_bytes)
            probe = _list_entries(packed["data"], "probe.zip")
            entry = _read_entry(packed["data"], "probe.zip", "probe/hello.txt",
                                max_bytes=self.max_entry_bytes,
                                max_total_bytes=self.max_total_bytes,
                                encoding=self.encoding)
            if entry["data"] != b"archive-probe" or probe["count"] != 1:
                raise RuntimeError("压缩包读写自检结果不一致")
        except Exception as exc:  # noqa: BLE001
            return HealthStatus(False, False, f"archive 自检失败: {exc}")
        return HealthStatus(True, False, "archive ready")

    # --- 自定义能力方法（按平台约定实现） ---

    def list_entries(self, data: bytes, filename: str = "") -> dict:
        """列出条目元数据：{format, count, declared_bytes, truncated, entries}。"""
        return _list_entries(data, filename, max_entries=self.max_entries,
                             encoding=self.encoding)

    def read_entry(self, data: bytes, filename: str = "", entry_name: str = "") -> dict:
        """读取单个条目：{name, size, declared_size, data, mime, previewable,
        preview_type, truncated, format}。"""
        return _read_entry(data, filename, entry_name, max_bytes=self.max_entry_bytes,
                           max_total_bytes=self.max_total_bytes,
                           encoding=self.encoding)

    def preview(self, data: bytes, filename: str = "", entry_name: str | None = None) -> dict:
        """前端预览入口：不传 entry_name 返回压缩包目录树，传入则返回该条目内容。

        目录结果带 preview_type="archive"；条目结果带 previewable/preview_type，
        文本类统一 text/plain，避免 HTML 等内容被同源内联执行。
        """
        if not entry_name:
            listing = self.list_entries(data, filename)
            listing["preview_type"] = "archive"
            listing["previewable"] = True
            return listing
        item = self.read_entry(data, filename, entry_name)
        if item["preview_type"] == "text":
            item["mime"] = "text/plain; charset=utf-8"
        return item

    def read_entries(self, data: bytes, filename: str = "",
                     names: list[str] | None = None) -> dict:
        """批量读取条目（names 为空表示全部），超限条目进 skipped。"""
        return _read_entries(data, filename, names, max_entries=self.max_entries,
                             max_entry_bytes=self.max_entry_bytes,
                             max_total_bytes=self.max_total_bytes,
                             encoding=self.encoding)

    def pack(self, entries, filename: str = "archive.zip") -> dict:
        """打包为 zip：entries 为 {名称: 字节} 或 [(名称, 字节)]。"""
        packed = _pack_zip(entries, max_total_bytes=self.max_total_bytes)
        packed["filename"] = filename or "archive.zip"
        return packed


def _is_int(value) -> bool:
    try:
        int(value)
    except (TypeError, ValueError):
        return False
    return True
