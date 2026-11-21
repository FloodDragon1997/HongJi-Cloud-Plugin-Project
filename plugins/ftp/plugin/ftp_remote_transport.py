"""FTP 存储传输层。

插件只依赖 Transport 协议（put/get/stat/stat_size/delete/copy/probe）。
- MemoryTransport：内存实现，用于契约测试与本地演示，零外部依赖。
- FtpTransport：基于标准库 ftplib 的 FTP（STOR/RETR/SIZE/DELE）。

模块名带 ftp_ 前缀，避免与 webdav 插件在同进程装载时发生模块名冲突。
"""

from __future__ import annotations

import hashlib
import io
import threading
from typing import BinaryIO, Protocol, runtime_checkable

#: 真流式读写的单块大小（1MB，远低于 8MB 契约上限，为队列缓冲留余量）
STREAM_CHUNK = 1 * 1024 * 1024


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


class FtpTransport:
    """FTP 传输（标准库 ftplib，被动模式）。

    配置：host、port、username、password、base（远端根目录）、readonly。
    """

    def __init__(self, config: dict):
        self.host = (config.get("host") or "").strip()
        if not self.host:
            raise RemoteUnavailable("FTP 配置缺少 host")
        try:
            self.port = int(config.get("port") or 21)
        except (TypeError, ValueError) as exc:
            raise RemoteUnavailable(f"FTP 端口非法: {config.get('port')!r}") from exc
        self.username = config.get("username") or "anonymous"
        self.password = config.get("password") or ""
        self.base = (config.get("base") or "").strip().rstrip("/")
        self.readonly = bool(config.get("readonly", False))

    def _path(self, key: str) -> str:
        return f"{self.base}/{key.lstrip('/')}".replace("//", "/")

    def _connect(self):
        import ftplib

        try:
            ftp = ftplib.FTP()
            ftp.connect(self.host, self.port, timeout=15)
            ftp.login(self.username, self.password)
            ftp.set_pasv(True)
            return ftp
        except Exception as exc:  # noqa: BLE001
            raise RemoteUnavailable(f"FTP 连接失败: {exc}") from exc

    @staticmethod
    def _quit(ftp) -> None:
        try:
            ftp.quit()
        except Exception:  # noqa: BLE001
            try:
                ftp.close()
            except Exception:  # noqa: BLE001
                pass

    def _mkdirs(self, ftp, path: str) -> None:
        current = ""
        for part in path.split("/")[:-1]:
            if not part:
                continue
            current = f"{current}/{part}" if current else part
            try:
                ftp.cwd(current)
            except Exception:  # noqa: BLE001
                try:
                    ftp.mkd(current)
                except Exception:  # noqa: BLE001
                    pass

    def put(self, key: str, data: bytes, readonly: bool = False) -> None:
        if self.readonly or readonly:
            raise RemoteUnavailable("FTP 存储为只读模式")
        ftp = self._connect()
        try:
            path = self._path(key)
            self._mkdirs(ftp, path)
            ftp.storbinary(f"STOR {path}", io.BytesIO(data))
        except RemoteUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001
            raise RemoteUnavailable(f"FTP 写入失败: {exc}") from exc
        finally:
            self._quit(ftp)

    def get(self, key: str) -> bytes:
        ftp = self._connect()
        try:
            buf = io.BytesIO()
            try:
                ftp.retrbinary(f"RETR {self._path(key)}", buf.write)
            except Exception as exc:  # noqa: BLE001
                raise RemoteObjectNotFound(key) from exc
            return buf.getvalue()
        finally:
            self._quit(ftp)

    def write_stream(self, key: str, fileobj: BinaryIO,
                     readonly: bool = False) -> int:
        """真流式 STOR：fileobj 直接交给 storbinary，按 8KB 块读取发送。"""
        if self.readonly or readonly:
            raise RemoteUnavailable("FTP 存储为只读模式")
        ftp = self._connect()
        size = 0

        def _count(block: bytes) -> None:
            nonlocal size
            size += len(block)

        try:
            path = self._path(key)
            self._mkdirs(ftp, path)
            ftp.storbinary(f"STOR {path}", fileobj, callback=_count)
            return size
        except RemoteUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001
            raise RemoteUnavailable(f"FTP 写入失败: {exc}") from exc
        finally:
            self._quit(ftp)

    def read_into(self, key: str, fileobj: BinaryIO) -> int:
        """真流式 RETR：retrbinary 的块直接写入 fileobj，不整对象入内存。"""
        ftp = self._connect()
        total = 0

        def _write(block: bytes) -> None:
            nonlocal total
            fileobj.write(block)
            total += len(block)

        try:
            try:
                ftp.retrbinary(f"RETR {self._path(key)}", _write)
            except Exception as exc:  # noqa: BLE001
                raise RemoteObjectNotFound(key) from exc
            return total
        finally:
            self._quit(ftp)

    def stat(self, key: str) -> tuple[int, str]:
        """流式计算 size 与 MD5：retrbinary 逐块喂入哈希器，不整对象入内存。"""
        ftp = self._connect()
        md5 = hashlib.md5()
        size = 0

        def _hash(block: bytes) -> None:
            nonlocal size
            md5.update(block)
            size += len(block)

        try:
            try:
                ftp.retrbinary(f"RETR {self._path(key)}", _hash)
            except Exception as exc:  # noqa: BLE001
                raise RemoteObjectNotFound(key) from exc
            return size, md5.hexdigest()
        finally:
            self._quit(ftp)

    def stat_size(self, key: str) -> int:
        """轻量取大小：优先 SIZE 命令，避免为了 size 全量下载对象。"""
        ftp = self._connect()
        try:
            try:
                size = ftp.size(self._path(key))
            except Exception as exc:  # noqa: BLE001
                raise RemoteObjectNotFound(key) from exc
            if size is None:
                raise RemoteObjectNotFound(key)
            return int(size)
        finally:
            self._quit(ftp)

    def exists(self, key: str) -> bool:
        ftp = self._connect()
        try:
            try:
                return ftp.size(self._path(key)) is not None
            except Exception:  # noqa: BLE001
                return False
        finally:
            self._quit(ftp)

    def delete(self, key: str) -> None:
        if self.readonly:
            raise RemoteUnavailable("FTP 存储为只读模式")
        ftp = self._connect()
        try:
            try:
                ftp.delete(self._path(key))
            except Exception:  # noqa: BLE001
                pass  # 幂等
        finally:
            self._quit(ftp)

    def copy(self, src: str, dst: str) -> None:
        """服务端无 COPY 命令：用两条连接做流式中转，内存峰值 ≤ STREAM_CHUNK。"""
        if self.readonly:
            raise RemoteUnavailable("FTP 存储为只读模式")
        src_ftp = self._connect()
        try:
            dst_ftp = self._connect()
        except Exception:
            self._quit(src_ftp)
            raise
        src_conn = None
        dst_conn = None
        try:
            dst_path = self._path(dst)
            self._mkdirs(dst_ftp, dst_path)
            src_ftp.voidcmd("TYPE I")
            try:
                src_conn, _ = src_ftp.transfercmd(f"RETR {self._path(src)}")
            except Exception as exc:  # noqa: BLE001
                raise RemoteObjectNotFound(src) from exc
            dst_ftp.voidcmd("TYPE I")
            dst_conn = dst_ftp.transfercmd(f"STOR {dst_path}")
            while True:
                block = src_conn.recv(STREAM_CHUNK)
                if not block:
                    break
                dst_conn.sendall(block)
            dst_conn.close()
            dst_conn = None
            src_conn.close()
            src_conn = None
            dst_ftp.voidresp()
            src_ftp.voidresp()
        finally:
            for conn in (src_conn, dst_conn):
                if conn is not None:
                    try:
                        conn.close()
                    except Exception:  # noqa: BLE001
                        pass
            for ftp in (src_ftp, dst_ftp):
                self._quit(ftp)

    def probe(self) -> tuple[bool, bool, str]:
        ftp = self._connect()
        try:
            ftp.pwd()
            return True, not self.readonly, "FTP reachable"
        except Exception as exc:  # noqa: BLE001
            return False, False, f"FTP 不可用: {exc}"
        finally:
            self._quit(ftp)
