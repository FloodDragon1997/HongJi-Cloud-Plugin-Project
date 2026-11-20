"""FTP 存储插件（AIC-015-2，REQ-STG-04）。

配置：
- transport: memory（默认，契约测试/离线）| ftp（真实服务）
- host, port, username, password, base, readonly（transport=ftp 时）
"""

from __future__ import annotations

import sys
from pathlib import Path

# 平台按单文件装载 entry，包内多文件需自行把所在目录加入 sys.path
_HERE = str(Path(__file__).resolve().parent)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from hj_remote_base import RemoteStoragePluginBase  # noqa: E402


class FtpPlugin(RemoteStoragePluginBase):
    plugin_id = "ftp"
    display_name = "FTP 存储"
    required_permissions = ("net.outbound",)
    transport_kind = "ftp"
    config_schema = [
        {"key": "host", "label": "FTP 主机", "type": "text", "required": True,
         "placeholder": "ftp.example.com"},
        {"key": "port", "label": "端口", "type": "number", "placeholder": "21"},
        {"key": "username", "label": "用户名", "type": "text", "placeholder": "anonymous"},
        {"key": "password", "label": "密码", "type": "password"},
        {"key": "base", "label": "根路径（可选）", "type": "text", "placeholder": "如：/upload", "full": True},
        {"key": "readonly", "label": "只读模式（禁写）", "type": "checkbox"},
    ]
