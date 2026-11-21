"""WebDAV 存储传输层。

插件只依赖 Transport 协议（put/get/stat/stat_size/delete/copy/probe）。
- MemoryTransport：内存实现，用于契约测试与本地演示，零外部依赖。
- WebDavTransport：基于标准库 urllib 的 WebDAV（PUT/GET/HEAD/DELETE/COPY/OPTIONS）。

模块名带 webdav_ 前缀，避免与 ftp 插件在同进程装载时发生模块名冲突。
"""

from __future__ import annotations

import base64
import hashlib
import threading
import urllib.error
import urllib.parse
import urllib.request
from typing import BinaryIO, Protocol, runtime_checkable

#: 真流式读写的单块大小（1MB，远低于 8MB 契约上限，为队列缓冲留余量）
STREAM_CHUNK = 1 * 1024 * 1024
_TIMEOUT = 15


class RemoteObjectNotFound(Exception):
    pass


class RemoteUnavailable(Exception):
    pass


@runtime_checkable
class Transport(Protocol):
    def put(self, key: str, data: bytes, readonly: bool = False) -> None: ...
    def get(self, key: str) -> bytes: ...
    def stat(self, key: str) -> tuple[int, str]: ...  # (size, md5)
    def stat_size(self, key: str) -> int: ...
    def exists(self, key: str) -> bool: ...
    def delete(self, key: str) -> None: ...
    def copy(self, src: str, dst: str) -> None: ...
    def probe(self) -> tuple[bool, bool, str]: ...  # (healthy, writable, message)

    # --- 可选真流式能力 ---
    # 未实现的 Transport 保持旧语义（基类回退到 put()/get()+切片）。
    def write_stream(self, key: str, fileobj: BinaryIO,
                     readonly: bool = False) -> int:
        """从 fileobj 流式写对象，返回写入字节数；不支持请保持未实现。"""
        raise NotImplementedError

    def read_into(self, key: str, fileobj: BinaryIO) -> int:
        """流式读对象并写入 fileobj，返回字节数；不支持请保持未实现。"""
        raise NotImplementedError


class MemoryTransport:
    """内存传输：进程内实现的完整对象语义，供契约测试与离线演示。"""

    def __init__(self, readonly: bool = False):
        self._data: dict[str, bytes] = {}
        self._readonly = readonly
        self._lock = threading.RLock()

    def put(self, key: str, data: bytes, readonly: bool = False) -> None:
        if self._readonly or readonly:
            raise RemoteUnavailable("远程存储为只读模式")
        with self._lock:
            self._data[key] = bytes(data)

    def get(self, key: str) -> bytes:
        with self._lock:
            if key not in self._data:
                raise RemoteObjectNotFound(key)
            return self._data[key]

    def stat(self, key: str) -> tuple[int, str]:
        with self._lock:
            blob = self._data.get(key)
        if blob is None:
            raise RemoteObjectNotFound(key)
        return len(blob), hashlib.md5(blob).hexdigest()

    def stat_size(self, key: str) -> int:
        with self._lock:
            if key not in self._data:
                raise RemoteObjectNotFound(key)
            return len(self._data[key])

    def exists(self, key: str) -> bool:
        with self._lock:
            return key in self._data

    def delete(self, key: str) -> None:
        if self._readonly:
            raise RemoteUnavailable("远程存储为只读模式")
        with self._lock:
            self._data.pop(key, None)

    def copy(self, src: str, dst: str) -> None:
        if self._readonly:
            raise RemoteUnavailable("远程存储为只读模式")
        with self._lock:
            if src not in self._data:
                raise RemoteObjectNotFound(src)
            self._data[dst] = self._data[src]

    def probe(self) -> tuple[bool, bool, str]:
        return True, not self._readonly, "memory transport ready"

    def write_stream(self, key: str, fileobj: BinaryIO,
                     readonly: bool = False) -> int:
        if self._readonly or readonly:
            raise RemoteUnavailable("远程存储为只读模式")
        buf = bytearray()
        while True:
            chunk = fileobj.read(STREAM_CHUNK)
            if not chunk:
                break
            buf.extend(chunk)
        with self._lock:
            self._data[key] = bytes(buf)
        return len(buf)

    def read_into(self, key: str, fileobj: BinaryIO) -> int:
        with self._lock:
            if key not in self._data:
                raise RemoteObjectNotFound(key)
            blob = self._data[key]
        total = 0
        for i in range(0, len(blob), STREAM_CHUNK):
            part = blob[i:i + STREAM_CHUNK]
            fileobj.write(part)
            total += len(part)
        return total


class WebDavTransport:
    """WebDAV 传输（标准库 urllib，支持 Basic / Bearer）。

    配置：url（WebDAV 根）、username、password、token、readonly。
    对象键按路径段做百分号编码，避免空格、中文、``#``/``?`` 破坏 URL。
    """

    def __init__(self, config: dict):
        self.base = (config.get("url") or "").strip().rstrip("/")
        if not self.base:
            raise RemoteUnavailable("WebDAV 配置缺少 url")
        self.username = config.get("username") or ""
        self.password = config.get("password") or ""
        self.token = config.get("token") or ""
        self.readonly = bool(config.get("readonly", False))

    def _url(self, key: str) -> str:
        return f"{self.base}/{urllib.parse.quote(key.lstrip('/'), safe='/')}"

    def _headers(self) -> dict:
        if self.token:
            return {"Authorization": f"Bearer {self.token}"}
        if self.username:
            cred = base64.b64encode(
                f"{self.username}:{self.password}".encode()).decode()
            return {"Authorization": f"Basic {cred}"}
        return {}

    def _open(self, request: urllib.request.Request, key: str, action: str):
        try:
            return urllib.request.urlopen(request, timeout=_TIMEOUT)
        except urllib.error.HTTPError as exc:
            if exc.code in (404, 410):
                raise RemoteObjectNotFound(key) from exc
            raise RemoteUnavailable(f"WebDAV {action} 失败: HTTP {exc.code}") from exc
        except Exception as exc:  # noqa: BLE001
            raise RemoteUnavailable(f"WebDAV {action} 失败: {exc}") from exc

    def _request(self, method: str, key: str, data: bytes | None = None) -> tuple[int, bytes, dict]:
        request = urllib.request.Request(
            self._url(key), data=data, method=method, headers=self._headers())
        with self._open(request, key, method) as resp:
            return resp.status, resp.read(), dict(resp.headers)

    def put(self, key: str, data: bytes, readonly: bool = False) -> None:
        if self.readonly or readonly:
            raise RemoteUnavailable("WebDAV 存储为只读模式")
        self._request("PUT", key, data)

    def get(self, key: str) -> bytes:
        return self._request("GET", key)[1]

    def write_stream(self, key: str, fileobj: BinaryIO,
                     readonly: bool = False) -> int:
        """真流式 PUT：分块生成器作为请求体（urllib 自动启用 chunked），
        峰值内存恒为单块 STREAM_CHUNK，不整对象入内存。"""
        if self.readonly or readonly:
            raise RemoteUnavailable("WebDAV 存储为只读模式")
        total = 0

        def _body():
            nonlocal total
            while True:
                chunk = fileobj.read(STREAM_CHUNK)
                if not chunk:
                    break
                total += len(chunk)
                yield chunk

        request = urllib.request.Request(
            self._url(key), data=_body(), method="PUT", headers={
                **self._headers(), "Content-Type": "application/octet-stream"})
        with self._open(request, key, "PUT") as resp:
            resp.read()
        return total

    def read_into(self, key: str, fileobj: BinaryIO) -> int:
        """真流式 GET：响应体按 STREAM_CHUNK 逐块写出，不整对象入内存。"""
        request = urllib.request.Request(
            self._url(key), method="GET", headers=self._headers())
        total = 0
        with self._open(request, key, "GET") as resp:
            while True:
                chunk = resp.read(STREAM_CHUNK)
                if not chunk:
                    break
                fileobj.write(chunk)
                total += len(chunk)
        return total

    def stat(self, key: str) -> tuple[int, str]:
        """流式计算 size 与 MD5：逐块读响应体，内存峰值 ≤ STREAM_CHUNK。"""
        request = urllib.request.Request(
            self._url(key), method="GET", headers=self._headers())
        md5 = hashlib.md5()
        size = 0
        with self._open(request, key, "GET") as resp:
            while True:
                chunk = resp.read(STREAM_CHUNK)
                if not chunk:
                    break
                md5.update(chunk)
                size += len(chunk)
        return size, md5.hexdigest()

    def stat_size(self, key: str) -> int:
        """轻量取大小：HEAD 的 Content-Length；缺失时再退回流式统计。"""
        request = urllib.request.Request(
            self._url(key), method="HEAD", headers=self._headers())
        with self._open(request, key, "HEAD") as resp:
            declared = resp.headers.get("Content-Length")
        if declared is not None:
            try:
                return int(declared)
            except ValueError:
                pass
        return self.stat(key)[0]

    def exists(self, key: str) -> bool:
        try:
            self._request("HEAD", key)
            return True
        except RemoteObjectNotFound:
            return False

    def delete(self, key: str) -> None:
        if self.readonly:
            raise RemoteUnavailable("WebDAV 存储为只读模式")
        self._request("DELETE", key)

    def copy(self, src: str, dst: str) -> None:
        if self.readonly:
            raise RemoteUnavailable("WebDAV 存储为只读模式")
        request = urllib.request.Request(self._url(src), method="COPY", headers={
            **self._headers(),
            "Destination": self._url(dst),
            "Overwrite": "T",
        })
        try:
            with urllib.request.urlopen(request, timeout=_TIMEOUT) as resp:
                resp.read()
        except urllib.error.HTTPError as exc:
            if exc.code in (404, 409):
                raise RemoteObjectNotFound(src) from exc
            raise RemoteUnavailable(f"WebDAV COPY 失败: HTTP {exc.code}") from exc
        except Exception as exc:  # noqa: BLE001
            raise RemoteUnavailable(f"WebDAV COPY 失败: {exc}") from exc

    def probe(self) -> tuple[bool, bool, str]:
        request = urllib.request.Request(
            self.base, method="OPTIONS", headers=self._headers())
        try:
            with urllib.request.urlopen(request, timeout=8) as resp:
                dav = resp.headers.get("DAV", "")
                reachable = bool(dav) or resp.status in (200, 204, 207)
                return reachable, not self.readonly, "WebDAV reachable"
        except Exception as exc:  # noqa: BLE001
            return False, False, f"WebDAV 不可达: {exc}"
