"""OCR 能力插件（AIC-015-3，REQ-BIZ-02）。

能力插件（非存储插件）：普通类，注册到同一 registry，提供
plugin_id/display_name/required_permissions/capabilities/health_check + 自定义方法。
业务层通过 registry.create("ocr", config) 取实例（受 AIC-011 生命周期守卫）。
"""

from __future__ import annotations

import sys
from pathlib import Path

# 平台按单文件装载 entry，包内多文件需自行把所在目录加入 sys.path
_HERE = str(Path(__file__).resolve().parent)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from app.sdk.store_plugin import HealthStatus  # noqa: E402
from ocr_providers import build_provider  # noqa: E402


class OcrPlugin:
    plugin_id = "ocr"
    display_name = "OCR 文字识别插件"
    config_schema = [
        {"key": "provider", "label": "提供方 Provider", "type": "text", "placeholder": "stub",
         "full": True, "hint": "默认 stub（内置演示）；真实 provider 实现同一协议注册即可"},
    ]
    required_permissions = ("fs.read",)

    def __init__(self, config: dict | None = None):
        self.config = config or {}
        self.provider_name = self.config.get("provider", "stub")
        self._provider = None

    @property
    def provider(self):
        if self._provider is None:
            self._provider = build_provider(self.provider_name)
        return self._provider

    def capabilities(self) -> dict:
        return {
            "ocr": True,
            "types": ["image", "pdf"],
            "searchable": True,
            "provider": self.provider_name,
            "accuracy_target": 0.90,  # PRD 假设口径：常用印刷体抽检 ≥90%
        }

    def health_check(self) -> HealthStatus:
        try:
            _ = self.provider
            return HealthStatus(True, False, f"OCR provider={self.provider_name} ready")
        except Exception as exc:  # noqa: BLE001
            return HealthStatus(False, False, f"OCR provider 不可用: {exc}")

    def recognize(self, data: bytes, filename: str, mime: str) -> dict:
        return self.provider.recognize(data, filename, mime)
