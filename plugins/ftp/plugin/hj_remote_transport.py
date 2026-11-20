"""远程存储传输层（AIC-015-2）：WebDAV / FTP 协议适配。

设计：插件只依赖 Transport 协议（put/get/stat/delete/list/copy/probe）。
- MemoryTransport：内存实现，用于契约测试与本地演示，零外部依赖。
- WebDavTransport：基于标准库 urllib 的 WebDAV（PUT/GET/HEAD/MKCOL/DELETE/COPY）。
- FtpTransport：基于标准库 ftplib 的 FTP（STOR/RETR/SIZE/DELE）。

不引入未批准的大依赖；真实服务连接参数由存储源配置加密落库后注入。
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
    def exists(self, key: str) -> bool: ...
    def delete(self, key: str) -> None: ...
    def copy(self, src: str, dst: str) -> None: ...
    def probe(self) -> tuple[bool, bool, str]: ...  # (healthy, writable, message)

    # --- 可选真流式能力 ---
    # 未实现的 Transport 保持旧语义（基类回退到 put()/get()+切片）。
    # 默认实现抛 NotImplementedError，仅用于显式继承本 Protocol 的场景；
    # 基类用 getattr/hasattr 探测能力，不依赖 isinstance 判定。
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
            if key not in self._data:
                raise RemoteObjectNotFound(key)
            blob = self._data[key]
            return len(blob), hashlib.md5(blob).hexdigest()

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
    """WebDAV 传输（标准库 urllib，支持 Basic/Digest 之外的 Bearer/Basic）。

    配置：url（WebDAV 根）、username、password、token、readonly。
    """

    def __init__(self, config: dict):
        self.base = (config.get("url") or "").rstrip("/")
        if not self.base:
            raise RemoteUnavailable("WebDAV 配置缺少 url")
        self.username = config.get("username") or ""
        self.password = config.get("password") or ""
        self.token = config.get("token") or ""
        self.readonly = bool(config.get("readonly", False))

    def _url(self, key: str) -> str:
        return f"{self.base}/{key.lstrip('/')}"

    def _headers(self) -> dict:
        h = {}
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        elif self.username:
            import base64
            cred = base64.b64encode(f"{self.username}:{self.password}".encode()).decode()
            h["Authorization"] = f"Basic {cred}"
        return h

    def _request(self, method: str, key: str, data: bytes | None = None) -> tuple[int, bytes, dict]:
        import urllib.error
        import urllib.request

        req = urllib.request.Request(self._url(key), data=data, method=method, headers=self._headers())
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                return resp.status, resp.read(), dict(resp.headers)
        except urllib.error.HTTPError as exc:
            if exc.code in (404, 410):
                raise RemoteObjectNotFound(key) from exc
            raise RemoteUnavailable(f"WebDAV {method} 失败: HTTP {exc.code}") from exc
        except Exception as exc:  # noqa: BLE001
            raise RemoteUnavailable(f"WebDAV {method} 失败: {exc}") from exc

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
        import urllib.error
        import urllib.request

        total = 0

        def _body():
            nonlocal total
            while True:
                chunk = fileobj.read(STREAM_CHUNK)
                if not chunk:
                    break
                total += len(chunk)
                yield chunk

        req = urllib.request.Request(
            self._url(key), data=_body(), method="PUT", headers=self._headers())
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                resp.read()
        except urllib.error.HTTPError as exc:
            if exc.code in (404, 410):
                raise RemoteObjectNotFound(key) from exc
            raise RemoteUnavailable(f"WebDAV PUT 失败: HTTP {exc.code}") from exc
        except Exception as exc:  # noqa: BLE001
            raise RemoteUnavailable(f"WebDAV PUT 失败: {exc}") from exc
        return total

    def read_into(self, key: str, fileobj: BinaryIO) -> int:
        """真流式 GET：响应体按 STREAM_CHUNK 逐块写出，不整对象入内存。"""
        import urllib.error
        import urllib.request

        req = urllib.request.Request(self._url(key), method="GET", headers=self._headers())
        total = 0
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                while True:
                    chunk = resp.read(STREAM_CHUNK)
                    if not chunk:
                        break
                    fileobj.write(chunk)
                    total += len(chunk)
        except urllib.error.HTTPError as exc:
            if exc.code in (404, 410):
                raise RemoteObjectNotFound(key) from exc
            raise RemoteUnavailable(f"WebDAV GET 失败: HTTP {exc.code}") from exc
        except Exception as exc:  # noqa: BLE001
            raise RemoteUnavailable(f"WebDAV GET 失败: {exc}") from exc
        return total

    def stat(self, key: str) -> tuple[int, str]:
        """流式计算 size 与 MD5：逐块读响应体，内存峰值 ≤ STREAM_CHUNK。"""
        import urllib.error
        import urllib.request

        req = urllib.request.Request(self._url(key), method="GET", headers=self._headers())
        md5 = hashlib.md5()
        size = 0
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                while True:
                    chunk = resp.read(STREAM_CHUNK)
                    if not chunk:
                        break
                    md5.update(chunk)
                    size += len(chunk)
        except urllib.error.HTTPError as exc:
            if exc.code in (404, 410):
                raise RemoteObjectNotFound(key) from exc
            raise RemoteUnavailable(f"WebDAV GET 失败: HTTP {exc.code}") from exc
        except Exception as exc:  # noqa: BLE001
            raise RemoteUnavailable(f"WebDAV GET 失败: {exc}") from exc
        return size, md5.hexdigest()

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
        import urllib.error
        import urllib.request

        req = urllib.request.Request(self._url(src), method="COPY", headers={
            **self._headers(), "Destination": self._url(dst), "Overwrite": "T",
        })
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                resp.read()
        except urllib.error.HTTPError as exc:
            if exc.code in (404, 409):
                raise RemoteObjectNotFound(src) from exc
            raise RemoteUnavailable(f"WebDAV COPY 失败: HTTP {exc.code}") from exc
        except Exception as exc:  # noqa: BLE001
            raise RemoteUnavailable(f"WebDAV COPY 失败: {exc}") from exc

    def probe(self) -> tuple[bool, bool, str]:
        import urllib.error
        import urllib.request

        req = urllib.request.Request(self.base, method="OPTIONS", headers=self._headers())
        try:
            with urllib.request.urlopen(req, timeout=8) as resp:
                allow = resp.headers.get("DAV", "")
                return bool(allow), not self.readonly, "WebDAV reachable"
        except Exception as exc:  # noqa: BLE001
            return False, False, f"WebDAV 不可达: {exc}"


class FtpTransport:
    """FTP 传输（标准库 ftplib，被动模式）。

    配置：host、port、username、password、base（远端根目录）、readonly。
    """

    def __init__(self, config: dict):
        self.host = config.get("host") or ""
        if not self.host:
            raise RemoteUnavailable("FTP 配置缺少 host")
        self.port = int(config.get("port") or 21)
        self.username = config.get("username") or "anonymous"
        self.password = config.get("password") or ""
        self.base = (config.get("base") or "").rstrip("/")
        self.readonly = bool(config.get("readonly", False))

    def _path(self, key: str) -> str:
        clean = "/".join(seg for seg in key.split("/") if seg not in ("", ".", ".."))
        return f"{self.base}/{clean}".replace("//", "/")

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

    def put(self, key: str, data: bytes, readonly: bool = False) -> None:
        if self.readonly or readonly:
            raise RemoteUnavailable("FTP 存储为只读模式")
        ftp = self._connect()
        try:
            self._mkdirs(ftp, self._path(key))
            with ftp.storbinary(f"STOR {self._path(key)}", io.BytesIO(data)):
                pass
        finally:
            ftp.quit()

    def _mkdirs(self, ftp, path: str) -> None:
        parts = path.split("/")
        current = ""
        for part in parts[:-1]:
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
            ftp.quit()

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
            self._mkdirs(ftp, self._path(key))
            ftp.storbinary(f"STOR {self._path(key)}", fileobj, callback=_count)
            return size
        finally:
            ftp.quit()

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
            ftp.quit()

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
            ftp.quit()

    def exists(self, key: str) -> bool:
        ftp = self._connect()
        try:
            try:
                size = ftp.size(self._path(key))
                return size is not None
            except Exception:  # noqa: BLE001
                return False
        finally:
            ftp.quit()

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
            ftp.quit()

    def copy(self, src: str, dst: str) -> None:
        """服务端无 COPY 命令：用两条连接做流式中转，内存峰值 ≤ STREAM_CHUNK。"""
        if self.readonly:
            raise RemoteUnavailable("FTP 存储为只读模式")
        src_ftp = self._connect()
        dst_ftp = self._connect()
        src_conn = None
        dst_conn = None
        try:
            self._mkdirs(dst_ftp, self._path(dst))
            src_ftp.voidcmd("TYPE I")
            try:
                src_conn, _ = src_ftp.transfercmd(f"RETR {self._path(src)}")
            except Exception as exc:  # noqa: BLE001
                raise RemoteObjectNotFound(src) from exc
            dst_ftp.voidcmd("TYPE I")
            dst_conn = dst_ftp.transfercmd(f"STOR {self._path(dst)}")
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
                try:
                    ftp.quit()
                except Exception:  # noqa: BLE001
                    pass

    def probe(self) -> tuple[bool, bool, str]:
        ftp = self._connect()
        try:
            ftp.pwd()
            return True, not self.readonly, "FTP reachable"
        except Exception as exc:  # noqa: BLE001
            return False, False, f"FTP 不可用: {exc}"
        finally:
            try:
                ftp.quit()
            except Exception:  # noqa: BLE001
                pass
