"""WebDAV 存储插件公共基类。

把 Transport 的字节语义映射为 SDK StoragePlugin 的契约方法：
- 统一对象键归一化与目录穿越防护
- etag = 内容 MD5（跨后端一致，供秒传核对）
- 流式读写单块 ≤8MB
- 缺失对象抛 PluginObjectNotFound，传输失败抛 PluginUnavailable
- stat_size 走轻量元信息（HEAD Content-Length），避免为取大小全量下载对象
"""

from __future__ import annotations

import hashlib
import queue
import threading
import time
from collections.abc import Iterator
from typing import BinaryIO

from app.sdk.store_plugin import (
    HealthStatus,
    PluginObjectNotFound,
    PluginUnavailable,
    PutResult,
    StoragePlugin,
)
from webdav_remote_transport import (
    MemoryTransport,
    RemoteObjectNotFound,
    RemoteUnavailable,
    Transport,
    WebDavTransport,
)

CHUNK = 8 * 1024 * 1024
MAX_KEY_LENGTH = 4096


class _HashingReader:
    """包裹上传流：边被读取边累计 MD5 与字节数，单块恒 ≤CHUNK（≤8MB）。"""

    def __init__(self, fileobj: BinaryIO):
        self._src = fileobj
        self.md5 = hashlib.md5()
        self.size = 0

    def read(self, n: int = -1) -> bytes:
        n = CHUNK if n is None or n < 0 else min(n, CHUNK)
        chunk = self._src.read(n)
        if not chunk:
            return chunk
        self.md5.update(chunk)
        self.size += len(chunk)
        return chunk


class _QueueSink:
    """把 read_into 的写入转为有界队列，供 open_read 逐块产出。

    maxsize=2 确保生产线程最多领先消费者两块（每块 ≤1MB），
    消费者提前退出时置 stop 事件，避免生产线程永久阻塞在满队列上。
    """

    def __init__(self, q: queue.Queue, stop: threading.Event):
        self._q = q
        self._stop = stop

    def write(self, data: bytes) -> int:
        if not data:
            return 0
        while not self._stop.is_set():
            try:
                self._q.put(data, timeout=0.05)
                break
            except queue.Full:
                continue
        return len(data)


def _safe_key(key: str) -> str:
    if not key or len(key) > MAX_KEY_LENGTH:
        raise ValueError(f"非法对象键: {key!r}")
    if "\x00" in key or "\\" in key:
        raise ValueError(f"非法对象键: {key!r}")
    if any(ord(ch) < 32 for ch in key):
        raise ValueError(f"非法对象键（含控制字符）: {key!r}")
    parts = [p for p in key.split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts):
        raise ValueError(f"非法对象键（目录穿越）: {key!r}")
    clean = "/".join(parts)
    if not clean:
        raise ValueError(f"非法对象键: {key!r}")
    return clean


def build_transport(config: dict) -> Transport:
    kind = (config.get("transport") or "memory").lower()
    if kind == "memory":
        store = config.get("_memory_store")
        readonly = bool(config.get("readonly", False))
        transport = MemoryTransport(readonly=readonly)
        if isinstance(store, dict):
            transport._data = store
        return transport
    if kind == "webdav":
        return WebDavTransport(config)
    raise PluginUnavailable(f"不支持的远程传输类型: {kind}")


class RemoteStoragePluginBase(StoragePlugin):
    """WebDAV 存储共享实现；子类仅声明元信息。"""

    transport_kind = "webdav"

    def __init__(self, config: dict | None = None):
        super().__init__(config)
        cfg = dict(self.config or {})
        cfg.setdefault("transport", self.transport_kind)
        self.readonly = bool(cfg.get("readonly", False))
        self._transport: Transport | None = None
        self._cfg = cfg

    @property
    def transport(self) -> Transport:
        if self._transport is None:
            try:
                self._transport = build_transport(self._cfg)
            except (RemoteUnavailable, PluginUnavailable) as exc:
                raise PluginUnavailable(str(exc)) from exc
        return self._transport

    def _ensure_writable(self) -> None:
        """只读配置统一映射为 45001（PluginUnavailable），不依赖传输层各自实现。"""
        if self.readonly:
            raise PluginUnavailable("远程存储插件处于只读模式，禁止写入")

    def capabilities(self) -> dict:
        return {
            "stream": True,
            "multipart": False,
            "max_object_bytes": 5 * 1024 ** 3,
            "server_side_copy": True,
            "readonly": self.readonly,
        }

    def health_check(self) -> HealthStatus:
        start = time.time()
        try:
            healthy, writable, message = self.transport.probe()
        except (RemoteUnavailable, PluginUnavailable) as exc:
            return HealthStatus(False, False, str(exc))
        except Exception as exc:  # noqa: BLE001
            return HealthStatus(False, False, f"健康检查异常: {exc}")
        return HealthStatus(bool(healthy), bool(writable) and not self.readonly,
                            message or "ok", int((time.time() - start) * 1000))

    def _map_not_found(self, exc: Exception, key: str) -> PluginObjectNotFound:
        return PluginObjectNotFound(f"{key}: {exc}")

    def put(self, key: str, data: bytes) -> PutResult:
        k = _safe_key(key)
        self._ensure_writable()
        try:
            self.transport.put(k, data, self.readonly)
        except RemoteObjectNotFound as exc:
            raise self._map_not_found(exc, k) from exc
        except RemoteUnavailable as exc:
            raise PluginUnavailable(f"写入失败: {exc}") from exc
        return PutResult(key=k, size=len(data), etag=hashlib.md5(data).hexdigest())

    def put_stream(self, key: str, fileobj: BinaryIO) -> PutResult:
        k = _safe_key(key)
        self._ensure_writable()
        write_stream = getattr(self.transport, "write_stream", None)
        if callable(write_stream):
            reader = _HashingReader(fileobj)
            try:
                write_stream(k, reader, self.readonly)
            except RemoteUnavailable as exc:
                raise PluginUnavailable(f"流式写入失败: {exc}") from exc
            return PutResult(key=k, size=reader.size, etag=reader.md5.hexdigest())

        # 回退：整对象缓存后一次写入（仅用于未实现真流式的 Transport）
        buf = bytearray()
        md5 = hashlib.md5()
        while chunk := fileobj.read(CHUNK):
            buf.extend(chunk)
            md5.update(chunk)
        try:
            self.transport.put(k, bytes(buf), self.readonly)
        except RemoteUnavailable as exc:
            raise PluginUnavailable(f"流式写入失败: {exc}") from exc
        return PutResult(key=k, size=len(buf), etag=md5.hexdigest())

    def get(self, key: str) -> bytes:
        k = _safe_key(key)
        try:
            return self.transport.get(k)
        except RemoteObjectNotFound as exc:
            raise self._map_not_found(exc, k) from exc
        except RemoteUnavailable as exc:
            raise PluginUnavailable(f"读取失败: {exc}") from exc

    def open_read(self, key: str) -> Iterator[bytes]:
        """流式读取：优先用 read_into 边下边产（单块 ≤1MB），不再整对象 get()。"""
        k = _safe_key(key)
        read_into = getattr(self.transport, "read_into", None)
        if not callable(read_into):
            blob = self.get(key)
            for i in range(0, len(blob), CHUNK):
                yield blob[i:i + CHUNK]
            return

        q: queue.Queue[bytes | None] = queue.Queue(maxsize=2)
        stop = threading.Event()
        sink = _QueueSink(q, stop)
        errors: list[BaseException] = []

        def _produce() -> None:
            try:
                read_into(k, sink)
            except BaseException as exc:  # noqa: BLE001  统一带到消费端抛出
                errors.append(exc)
            finally:
                while not stop.is_set():
                    try:
                        q.put(None, timeout=0.05)
                        break
                    except queue.Full:
                        continue

        threading.Thread(target=_produce, daemon=True).start()
        try:
            while True:
                chunk = q.get()
                if chunk is None:
                    break
                yield chunk
        finally:
            stop.set()

        if errors:
            exc = errors[0]
            if isinstance(exc, RemoteObjectNotFound):
                raise self._map_not_found(exc, k) from exc
            if isinstance(exc, RemoteUnavailable):
                raise PluginUnavailable(f"读取失败: {exc}") from exc
            raise exc

    def stat(self, key: str) -> dict:
        k = _safe_key(key)
        try:
            size, etag = self.transport.stat(k)
        except RemoteObjectNotFound as exc:
            raise self._map_not_found(exc, k) from exc
        except RemoteUnavailable as exc:
            raise PluginUnavailable(f"读取元信息失败: {exc}") from exc
        return {"size": int(size), "etag": etag}

    def stat_size(self, key: str) -> int:
        """轻量取大小：优先 SIZE/HEAD，避免为取字节数全量读取对象。"""
        k = _safe_key(key)
        stat_size = getattr(self.transport, "stat_size", None)
        if callable(stat_size):
            try:
                return int(stat_size(k))
            except RemoteObjectNotFound as exc:
                raise self._map_not_found(exc, k) from exc
            except RemoteUnavailable as exc:
                raise PluginUnavailable(f"读取元信息失败: {exc}") from exc
        return int(self.stat(k)["size"])

    def exists(self, key: str) -> bool:
        k = _safe_key(key)
        try:
            return self.transport.exists(k)
        except RemoteUnavailable as exc:
            raise PluginUnavailable(f"读取元信息失败: {exc}") from exc

    def delete(self, key: str) -> None:
        k = _safe_key(key)
        self._ensure_writable()
        try:
            self.transport.delete(k)
        except RemoteUnavailable as exc:
            raise PluginUnavailable(f"删除失败: {exc}") from exc

    def copy(self, src_key: str, dst_key: str) -> PutResult:
        src, dst = _safe_key(src_key), _safe_key(dst_key)
        self._ensure_writable()
        try:
            self.transport.copy(src, dst)
        except RemoteObjectNotFound as exc:
            raise self._map_not_found(exc, src) from exc
        except RemoteUnavailable as exc:
            raise PluginUnavailable(f"复制失败: {exc}") from exc
        stat = self.stat(dst)
        return PutResult(key=dst, size=stat["size"], etag=stat["etag"])
