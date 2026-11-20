"""本地磁盘存储插件实现。

配置：{"root": "<数据根目录>"}（默认 ./data_store，dev/test 使用，S3 默认在生产 ADR-004）。
对象键安全：所有键解析后必须落在数据根内，目录穿越直接拒绝。
"""

from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
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


class LocalDiskPlugin(StoragePlugin):
    plugin_id = "local_disk"
    display_name = "本地磁盘"
    required_permissions = ("fs.read", "fs.write", "fs.list")
    #: 统一配置 schema（后端为唯一真源，随插件元数据下发，前端按此渲染表单）
    config_schema = [
        {"key": "root", "label": "挂载根目录", "type": "text", "placeholder": "./data_store",
         "full": True, "hint": "服务器上的目录路径，留空使用默认 ./data_store"},
        {"key": "readonly", "label": "只读模式（禁写）", "type": "checkbox"},
    ]

    def __init__(self, config: dict | None = None):
        super().__init__(config)
        root = self.config.get("root") or "./data_store"
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    # --- 路径安全 ---
    def _resolve(self, key: str) -> Path:
        candidate = (self.root / key).resolve()
        if not str(candidate).startswith(str(self.root) + os.sep) and candidate != self.root:
            raise ValueError(f"非法对象键（目录穿越）: {key}")
        return candidate

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
            if not self.root.is_dir():
                return HealthStatus(False, False, f"数据根目录不存在: {self.root}")
            fd, probe = tempfile.mkstemp(prefix=".health_", dir=self.root)
            os.close(fd)
            os.unlink(probe)
        except OSError as exc:
            return HealthStatus(False, False, f"数据根目录不可写: {exc}")
        return HealthStatus(True, True, "ok", int((time.time() - start) * 1000))

    def put(self, key: str, data: bytes) -> PutResult:
        path = self._resolve(key)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        except OSError as exc:
            raise PluginUnavailable(f"本地磁盘写入失败: {exc}") from exc
        return PutResult(key=key, size=len(data), etag=hashlib.md5(data).hexdigest())

    def put_stream(self, key: str, fileobj: BinaryIO) -> PutResult:
        path = self._resolve(key)
        md5 = hashlib.md5()
        size = 0
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "wb") as fh:
                while chunk := fileobj.read(1024 * 1024 * 8):
                    fh.write(chunk)
                    md5.update(chunk)
                    size += len(chunk)
        except OSError as exc:
            raise PluginUnavailable(f"本地磁盘写入失败: {exc}") from exc
        return PutResult(key=key, size=size, etag=md5.hexdigest())

    def get(self, key: str) -> bytes:
        path = self._resolve(key)
        if not path.is_file():
            raise PluginObjectNotFound(key)
        try:
            return path.read_bytes()
        except OSError as exc:
            raise PluginUnavailable(f"本地磁盘读取失败: {exc}") from exc

    def open_read(self, key: str) -> Iterator[bytes]:
        path = self._resolve(key)
        if not path.is_file():
            raise PluginObjectNotFound(key)
        try:
            with open(path, "rb") as fh:
                while chunk := fh.read(1024 * 1024 * 8):
                    yield chunk
        except OSError as exc:
            raise PluginUnavailable(f"本地磁盘读取失败: {exc}") from exc

    def stat(self, key: str) -> dict:
        """完整 stat（含 etag/MD5）。仅复制等确实需要 etag 的路径使用。"""
        path = self._resolve(key)
        if not path.is_file():
            raise PluginObjectNotFound(key)
        md5 = hashlib.md5()
        try:
            with open(path, "rb") as fh:
                while chunk := fh.read(1024 * 1024 * 8):
                    md5.update(chunk)
        except OSError as exc:
            raise PluginUnavailable(f"本地磁盘读取失败: {exc}") from exc
        return {"size": path.stat().st_size, "etag": md5.hexdigest()}

    def stat_size(self, key: str) -> int:
        """轻量 stat：仅 os.stat 取字节数，不读取文件内容/不计算 MD5。

        契约 stat() 必须返回 etag，但下载/预览/对账/zip 预检只需 size，
        走此覆写可避免每个对象都全文件顺序读一遍（大文件场景收益显著）。
        """
        path = self._resolve(key)
        if not path.is_file():
            raise PluginObjectNotFound(key)
        try:
            return path.stat().st_size
        except OSError as exc:
            raise PluginUnavailable(f"本地磁盘读取失败: {exc}") from exc

    def exists(self, key: str) -> bool:
        return self._resolve(key).is_file()

    def delete(self, key: str) -> None:
        path = self._resolve(key)
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            raise PluginUnavailable(f"本地磁盘删除失败: {exc}") from exc

    def copy(self, src_key: str, dst_key: str) -> PutResult:
        src = self._resolve(src_key)
        if not src.is_file():
            raise PluginObjectNotFound(src_key)
        dst = self._resolve(dst_key)
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dst)
        except OSError as exc:
            raise PluginUnavailable(f"本地磁盘复制失败: {exc}") from exc
        stat = self.stat(dst_key)
        return PutResult(key=dst_key, size=stat["size"], etag=stat["etag"])

    def walk_objects(self):
        """遍历数据根目录下全部对象键（对账用扩展，非契约方法）。"""
        for p in sorted(self.root.rglob("*")):
            if p.is_file() and not p.name.startswith(".health_"):
                yield p.relative_to(self.root).as_posix()
