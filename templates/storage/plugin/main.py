"""存储插件模板（type=storage）。

使用步骤：
1. 复制本目录到 plugins/<plugin_id>/；
2. 修改 manifest.json 的 id/name/version/permissions；
3. 同步本文件的 plugin_id、display_name、required_permissions、config_schema；
4. 将示例的本地目录实现替换为真实后端（对象存储/网络存储等）。

平台约束：
- 只能 import 标准库、第三方依赖与 app.sdk.store_plugin，禁止 import app.* 其他内部模块；
- 对象键 key 由平台生成后传入，插件仍须自行防目录穿越；
- 大对象必须走流式（put_stream/open_read），单块内存上限 8MB；
- 后端不可用/只读时抛 PluginUnavailable，对象不存在抛 PluginObjectNotFound。
"""

from __future__ import annotations

import hashlib
import io
import os
import time
from collections.abc import Iterator
from pathlib import Path
from typing import BinaryIO

from app.sdk.store_plugin import (
    HealthStatus,
    PluginObjectNotFound,
    PluginUnavailable,
    PutResult,
    StoragePlugin,
)

#: 流式读写单块大小（平台约定上限 8MB）
CHUNK = 8 * 1024 * 1024


class MyStoragePlugin(StoragePlugin):
    # 以下三项必须与 manifest.json 保持一致
    plugin_id = "my_storage"
    display_name = "我的存储后端"
    required_permissions = ("fs.read", "fs.write", "fs.list")

    #: 配置表单（后端为唯一真源，随插件元数据下发给前端渲染）
    config_schema = [
        {"key": "root", "label": "挂载根目录", "type": "text", "placeholder": "./data_store",
         "full": True, "hint": "服务器上的目录路径，留空使用默认 ./data_store"},
        {"key": "readonly", "label": "只读模式（禁写）", "type": "checkbox"},
    ]

    def __init__(self, config: dict | None = None):
        super().__init__(config)
        self.root = Path(self.config.get("root") or "./data_store").resolve()
        self.readonly = bool(self.config.get("readonly"))

    # --- 内部：路径安全 ---
    def _resolve(self, key: str) -> Path:
        """把对象键解析为数据根内的绝对路径，越界即拒绝。"""
        candidate = (self.root / key).resolve()
        if candidate != self.root and self.root not in candidate.parents:
            raise ValueError(f"非法对象键（目录穿越）: {key}")
        return candidate

    def _ensure_writable(self) -> None:
        if self.readonly:
            raise PluginUnavailable("插件处于只读模式，禁止写入")

    # --- 契约实现 ---
    def capabilities(self) -> dict:
        return {
            "stream": True,
            "multipart": True,
            "max_object_bytes": 1024 ** 4,
            "server_side_copy": True,
        }

    def health_check(self) -> HealthStatus:
        start = time.time()
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            if not self.readonly:
                probe = self.root / ".health_probe"
                probe.write_bytes(b"")
                probe.unlink()
        except OSError as exc:
            return HealthStatus(False, False, f"数据根目录不可用: {exc}")
        latency = int((time.time() - start) * 1000)
        return HealthStatus(True, not self.readonly, "ok", latency)

    def put(self, key: str, data: bytes) -> PutResult:
        return self.put_stream(key, io.BytesIO(data))

    def put_stream(self, key: str, fileobj: BinaryIO) -> PutResult:
        self._ensure_writable()
        path = self._resolve(key)
        md5 = hashlib.md5()
        size = 0
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "wb") as fh:
                while chunk := fileobj.read(CHUNK):
                    fh.write(chunk)
                    md5.update(chunk)
                    size += len(chunk)
        except OSError as exc:
            raise PluginUnavailable(f"写入失败: {exc}") from exc
        return PutResult(key=key, size=size, etag=md5.hexdigest())

    def get(self, key: str) -> bytes:
        path = self._resolve(key)
        if not path.is_file():
            raise PluginObjectNotFound(key)
        try:
            return path.read_bytes()
        except OSError as exc:
            raise PluginUnavailable(f"读取失败: {exc}") from exc

    def open_read(self, key: str) -> Iterator[bytes]:
        path = self._resolve(key)
        if not path.is_file():
            raise PluginObjectNotFound(key)
        try:
            with open(path, "rb") as fh:
                while chunk := fh.read(CHUNK):
                    yield chunk
        except OSError as exc:
            raise PluginUnavailable(f"读取失败: {exc}") from exc

    def stat(self, key: str) -> dict:
        path = self._resolve(key)
        if not path.is_file():
            raise PluginObjectNotFound(key)
        md5 = hashlib.md5()
        try:
            with open(path, "rb") as fh:
                while chunk := fh.read(CHUNK):
                    md5.update(chunk)
            size = path.stat().st_size
        except OSError as exc:
            raise PluginUnavailable(f"读取失败: {exc}") from exc
        return {"size": size, "etag": md5.hexdigest()}

    def stat_size(self, key: str) -> int:
        """只需字节数的路径（下载/预览/对账）走这里，避免全量读取算 MD5。"""
        path = self._resolve(key)
        if not path.is_file():
            raise PluginObjectNotFound(key)
        try:
            return os.stat(path).st_size
        except OSError as exc:
            raise PluginUnavailable(f"读取失败: {exc}") from exc

    def exists(self, key: str) -> bool:
        return self._resolve(key).is_file()

    def delete(self, key: str) -> None:
        self._ensure_writable()
        try:
            self._resolve(key).unlink(missing_ok=True)
        except OSError as exc:
            raise PluginUnavailable(f"删除失败: {exc}") from exc

    def copy(self, src_key: str, dst_key: str) -> PutResult:
        self._ensure_writable()
        src = self._resolve(src_key)
        if not src.is_file():
            raise PluginObjectNotFound(src_key)
        dst = self._resolve(dst_key)
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            with open(src, "rb") as rf, open(dst, "wb") as wf:
                while chunk := rf.read(CHUNK):
                    wf.write(chunk)
        except OSError as exc:
            raise PluginUnavailable(f"复制失败: {exc}") from exc
        return PutResult(key=dst_key, **self.stat(dst_key))
