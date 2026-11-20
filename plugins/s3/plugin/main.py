"""S3 兼容对象存储插件（MinIO / AWS S3 / 云厂商 S3 网关均适用）。

配置：{"endpoint", "bucket", "access_key", "secret_key", "region", "path_style"}。
连接参数由存储源模块加密落库，插件实例只拿解密后的 dict。
"""

from __future__ import annotations

import hashlib
import io
from collections.abc import Iterator
from typing import BinaryIO

from app.sdk.store_plugin import (
    HealthStatus,
    PluginObjectNotFound,
    PluginUnavailable,
    PutResult,
    StoragePlugin,
)


class S3Plugin(StoragePlugin):
    plugin_id = "s3"
    display_name = "S3 对象存储"
    required_permissions = ("net.outbound",)
    config_schema = [
        {"key": "bucket", "label": "存储桶 Bucket", "type": "text", "required": True,
         "placeholder": "如：hjcloud-data", "full": True},
        {"key": "endpoint", "label": "Endpoint 地址", "type": "text",
         "placeholder": "如：https://s3.amazonaws.com", "full": True},
        {"key": "access_key", "label": "Access Key", "type": "text"},
        {"key": "secret_key", "label": "Secret Key", "type": "password"},
        {"key": "region", "label": "区域 Region", "type": "text", "placeholder": "us-east-1"},
        {"key": "path_style", "label": "Path-Style 寻址（MinIO 勾选）", "type": "checkbox"},
        {"key": "readonly", "label": "只读模式（禁写）", "type": "checkbox"},
    ]

    def __init__(self, config: dict | None = None):
        super().__init__(config)
        self.readonly = bool(self.config.get("readonly"))
        self._client = None

    def _ensure_writable(self) -> None:
        if self.readonly:
            raise PluginUnavailable("S3 插件处于只读模式，禁止写入")

    # --- 客户端懒加载 ---
    def client(self):
        if self._client is None:
            try:
                import boto3
                from botocore.config import Config as BotoConfig
            except ImportError as exc:  # pragma: no cover
                raise PluginUnavailable(f"boto3 未安装: {exc}") from exc
            if not self.config.get("bucket"):
                raise PluginUnavailable("S3 配置缺少 bucket")
            self._client = boto3.client(
                "s3",
                endpoint_url=self.config.get("endpoint") or None,
                aws_access_key_id=self.config.get("access_key"),
                aws_secret_access_key=self.config.get("secret_key"),
                region_name=self.config.get("region") or "us-east-1",
                config=BotoConfig(s3={"addressing_style": "path" if self.config.get("path_style", True) else "auto"}),
            )
        return self._client

    def _bucket(self) -> str:
        return self.config["bucket"]

    @staticmethod
    def _map_error(exc: Exception) -> None:
        try:
            from botocore.exceptions import ClientError, EndpointConnectionError, BotoCoreError
        except ImportError:  # pragma: no cover
            raise PluginUnavailable(str(exc)) from exc
        if isinstance(exc, ClientError):
            code = str(exc.response.get("Error", {}).get("Code", ""))
            if code in ("404", "NoSuchKey", "NotFound", "NoSuchBucket"):
                raise PluginObjectNotFound(str(exc)) from exc
            raise PluginUnavailable(f"S3 错误 {code}: {exc}") from exc
        if isinstance(exc, (EndpointConnectionError, BotoCoreError)):
            raise PluginUnavailable(f"S3 不可达: {exc}") from exc
        raise PluginUnavailable(str(exc)) from exc

    # --- 契约实现 ---
    def capabilities(self) -> dict:
        return {
            "stream": True,
            "multipart": True,
            "max_object_bytes": 5 * 1024 ** 4,
            "server_side_copy": True,
            "readonly": self.readonly,
        }

    def health_check(self) -> HealthStatus:
        import time

        start = time.time()
        try:
            self.client().head_bucket(Bucket=self._bucket())
        except Exception as exc:  # noqa: BLE001  健康检查只返回状态，不向上抛异常
            return HealthStatus(False, False, f"S3 不可用: {exc}",
                                int((time.time() - start) * 1000))
        return HealthStatus(True, not self.readonly, "ok",
                            int((time.time() - start) * 1000))

    def put(self, key: str, data: bytes) -> PutResult:
        return self.put_stream(key, io.BytesIO(data))

    #: 分片大小：单块内存上限（与平台流式读写块一致，避免大文件整读入内存）
    PART_SIZE = 8 * 1024 * 1024

    @staticmethod
    def _read_chunk(fileobj: BinaryIO, size: int) -> bytes:
        """读满 size 字节或到 EOF（兼容返回不足额的非阻塞流）。"""
        buf = bytearray()
        while len(buf) < size:
            piece = fileobj.read(size - len(buf))
            if not piece:
                break
            buf.extend(piece)
        return bytes(buf)

    def put_stream(self, key: str, fileobj: BinaryIO) -> PutResult:
        """流式上传：边读边算 MD5，内存峰值恒为单块（≤ PART_SIZE）。

        - 单块内读完（含空文件）→ 单次 put_object
        - 超过单块 → multipart 分片上传，异常时 abort 避免残留分片
        """
        self._ensure_writable()
        client = self.client()
        md5 = hashlib.md5()

        first = self._read_chunk(fileobj, self.PART_SIZE)
        md5.update(first)

        if len(first) < self.PART_SIZE:
            try:
                client.put_object(Bucket=self._bucket(), Key=key, Body=first,
                                  ContentLength=len(first))
            except Exception as exc:
                self._map_error(exc)
                raise  # pragma: no cover
            return PutResult(key=key, size=len(first), etag=md5.hexdigest())

        try:
            upload_id = client.create_multipart_upload(
                Bucket=self._bucket(), Key=key)["UploadId"]
        except Exception as exc:
            self._map_error(exc)
            raise  # pragma: no cover

        parts: list[dict] = []
        total = 0
        try:
            number = 1
            chunk = first
            while chunk:
                resp = client.upload_part(
                    Bucket=self._bucket(), Key=key, UploadId=upload_id,
                    PartNumber=number, Body=chunk)
                parts.append({"ETag": resp["ETag"], "PartNumber": number})
                total += len(chunk)
                number += 1
                chunk = self._read_chunk(fileobj, self.PART_SIZE)
                if chunk:
                    md5.update(chunk)
            client.complete_multipart_upload(
                Bucket=self._bucket(), Key=key, UploadId=upload_id,
                MultipartUpload={"Parts": parts})
        except Exception as exc:
            try:  # 失败即清理已上传分片，避免产生计费的孤儿对象
                client.abort_multipart_upload(
                    Bucket=self._bucket(), Key=key, UploadId=upload_id)
            except Exception:  # pragma: no cover - 清理失败不掩盖原始异常
                pass
            self._map_error(exc)
            raise  # pragma: no cover
        return PutResult(key=key, size=total, etag=md5.hexdigest())

    def get(self, key: str) -> bytes:
        try:
            buf = io.BytesIO()
            self.client().download_fileobj(self._bucket(), key, buf)
            return buf.getvalue()
        except Exception as exc:
            self._map_error(exc)
            raise  # pragma: no cover

    def open_read(self, key: str) -> Iterator[bytes]:
        try:
            resp = self.client().get_object(Bucket=self._bucket(), Key=key)
            body = resp["Body"]
            while chunk := body.read(1024 * 1024 * 8):
                yield chunk
        except Exception as exc:
            self._map_error(exc)
            raise  # pragma: no cover

    def stat(self, key: str) -> dict:
        try:
            head = self.client().head_object(Bucket=self._bucket(), Key=key)
        except Exception as exc:
            self._map_error(exc)
            raise  # pragma: no cover
        etag = head.get("ETag", "").strip('"')
        return {"size": head["ContentLength"], "etag": etag}

    def stat_size(self, key: str) -> int:
        """只需字节数的路径走这里：HEAD 一次即可，避免 GET 全量读对象。"""
        try:
            head = self.client().head_object(Bucket=self._bucket(), Key=key)
        except Exception as exc:
            self._map_error(exc)
            raise  # pragma: no cover
        return int(head["ContentLength"])

    def exists(self, key: str) -> bool:
        try:
            self.client().head_object(Bucket=self._bucket(), Key=key)
            return True
        except Exception as exc:
            try:
                self._map_error(exc)
            except PluginObjectNotFound:
                return False
            raise  # pragma: no cover  后端不可用等异常向上抛，不伪装成"不存在"

    def delete(self, key: str) -> None:
        self._ensure_writable()
        try:
            self.client().delete_object(Bucket=self._bucket(), Key=key)
        except Exception as exc:
            self._map_error(exc)

    def copy(self, src_key: str, dst_key: str) -> PutResult:
        self._ensure_writable()
        try:
            self.client().copy_object(
                Bucket=self._bucket(), CopySource={"Bucket": self._bucket(), "Key": src_key},
                Key=dst_key,
            )
            stat = self.stat(dst_key)
        except Exception as exc:
            self._map_error(exc)
            raise  # pragma: no cover
        return PutResult(key=dst_key, size=stat["size"], etag=stat["etag"])
