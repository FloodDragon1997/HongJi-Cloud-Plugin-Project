"""WebDAV 存储插件（REQ-STG-04）。

配置：
- transport: memory（默认，契约测试/离线）| webdav（真实服务）
- url, username, password, token, readonly（transport=webdav 时）
"""

from __future__ import annotations

import sys
from pathlib import Path

# 平台按单文件装载 entry，包内多文件需自行把所在目录加入 sys.path
_HERE = str(Path(__file__).resolve().parent)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)


def _evict_stale_modules() -> None:
    """热更新后丢弃上一版本目录下的同名模块缓存，避免导入旧实现。"""
    here = Path(_HERE).resolve()
    for name in ("webdav_remote_base", "webdav_remote_transport",
                 "hj_remote_base", "hj_remote_transport"):
        cached = sys.modules.get(name)
        if cached is None:
            continue
        cached_file = getattr(cached, "__file__", None)
        if not cached_file or Path(cached_file).resolve().parent != here:
            del sys.modules[name]


_evict_stale_modules()

from webdav_remote_base import RemoteStoragePluginBase  # noqa: E402


class WebDavPlugin(RemoteStoragePluginBase):
    plugin_id = "webdav"
    display_name = "WebDAV 存储"
    required_permissions = ("net.outbound",)
    transport_kind = "webdav"
    config_schema = [
        {"key": "url", "label": "服务地址", "type": "text", "required": True,
         "placeholder": "https://dav.example.com/dav", "full": True},
        {"key": "username", "label": "用户名", "type": "text"},
        {"key": "password", "label": "密码", "type": "password"},
        {"key": "token", "label": "Bearer Token（可选）", "type": "password", "full": True},
        {"key": "readonly", "label": "只读模式（禁写）", "type": "checkbox"},
    ]
