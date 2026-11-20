"""缩略图生成插件（能力插件，模式同 OCR 插件 REQ-BIZ-02）。

普通类，注册到同一 registry，提供 plugin_id/display_name/required_permissions/
capabilities/health_check + generate 自定义方法。
业务层通过 registry.create("thumbnail", config) 取实例（受 AIC-011 生命周期守卫）。
"""

from __future__ import annotations

import sys
from pathlib import Path

# 平台按单文件装载 entry，包内多文件需自行把所在目录加入 sys.path
_HERE = str(Path(__file__).resolve().parent)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from app.sdk.store_plugin import HealthStatus  # noqa: E402
from thumbnail_providers import DEFAULT_SIZES, build_provider  # noqa: E402


class ThumbnailPlugin:
    plugin_id = "thumbnail"
    display_name = "缩略图生成插件"
    config_schema = [
        {"key": "provider", "label": "提供方 Provider", "type": "text", "placeholder": "stub",
         "full": True, "hint": "默认 stub（内置演示）；真实 provider 实现同一协议注册即可"},
    ]
    required_permissions = ("fs.read",)

    def __init__(self, config: dict | None = None):
        self.config = config or {}
        self.provider_name = self.config.get("provider", "stub")

    @property
    def provider(self):
        return build_provider(self.provider_name)

    def capabilities(self) -> dict:
        return {
            "thumbnail": True,
            "types": ["image"],
            "sizes": list(DEFAULT_SIZES),
            "provider": self.provider_name,
        }

    def health_check(self) -> HealthStatus:
        try:
            _ = self.provider
            return HealthStatus(True, False, f"thumbnail provider={self.provider_name} ready")
        except Exception as exc:  # noqa: BLE001
            return HealthStatus(False, False, f"thumbnail provider 不可用: {exc}")

    def generate(self, data: bytes, filename: str, mime: str, size: int = 128) -> dict:
        """对单份文件字节生成缩略图，返回 provider 结果 dict。"""
        return self.provider.make(data, filename, mime, size)
