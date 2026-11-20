"""WebDAV 存储插件（AIC-015-2，REQ-STG-04）。

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

from hj_remote_base import RemoteStoragePluginBase  # noqa: E402


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
