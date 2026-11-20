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
from thumbnail_providers import (  # noqa: E402
    DEFAULT_SIZES,
    MAX_SIZE,
    MIN_SIZE,
    available_providers,
    build_provider,
    placeholder_png,
    resolve_provider_name,
)

DEFAULT_SIZE = 128


class ThumbnailPlugin:
    plugin_id = "thumbnail"
    display_name = "缩略图生成插件"
    config_schema = [
        {"key": "provider", "label": "缩略图引擎", "type": "select",
         "options": ["auto", "pillow", "stub"], "placeholder": "auto", "full": True,
         "hint": "auto：优先 Pillow 真实缩放，未安装时自动降级为占位引擎"},
        {"key": "default_size", "label": "默认长边(px)", "type": "number",
         "placeholder": str(DEFAULT_SIZE), "hint": f"等比缩放，只缩不放；取值 {MIN_SIZE}–{MAX_SIZE}"},
    ]
    required_permissions = ("fs.read",)

    def __init__(self, config: dict | None = None):
        self.config = config or {}
        self.provider_name = self.config.get("provider") or "auto"
        try:
            size = int(self.config.get("default_size", DEFAULT_SIZE))
        except (TypeError, ValueError):
            size = DEFAULT_SIZE
        self.default_size = max(MIN_SIZE, min(MAX_SIZE, size))

    @property
    def provider(self):
        return build_provider(self.provider_name)

    def capabilities(self) -> dict:
        return {
            "thumbnail": True,
            "types": ["image"],
            "sizes": list(DEFAULT_SIZES),
            "provider": resolve_provider_name(self.provider_name),
            "requested_provider": self.provider_name,
            "available": available_providers(),
            "default_size": self.default_size,
        }

    def health_check(self) -> HealthStatus:
        try:
            provider = self.provider
            if provider.name == "pillow":
                # 冒烟：用零依赖 PNG 张量做一次真实编解码，确认 Pillow 链路可用
                provider.make(placeholder_png(8, 8, (255, 255, 255)),
                              "health.png", "image/png", 8)
            return HealthStatus(True, False, f"thumbnail engine={provider.name} ready")
        except Exception as exc:  # noqa: BLE001
            return HealthStatus(False, False, f"thumbnail provider 不可用: {exc}")

    def generate(self, data: bytes, filename: str, mime: str, size: int | None = None) -> dict:
        """对单份文件字节生成缩略图，返回 provider 结果 dict。"""
        try:
            target = self.default_size if size is None else int(size)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"非法缩略图尺寸: {size!r}") from exc
        target = max(MIN_SIZE, min(MAX_SIZE, target))
        return self.provider.make(data, filename, mime, target)
