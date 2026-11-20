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
from ocr_providers import (  # noqa: E402
    DEFAULT_MAX_PAGES,
    MAX_PAGES,
    available_providers,
    build_provider,
    resolve_provider_name,
)


class OcrPlugin:
    plugin_id = "ocr"
    display_name = "OCR 文字识别插件"
    config_schema = [
        {"key": "provider", "label": "识别引擎", "type": "select",
         "options": ["auto", "tesseract", "rapidocr", "stub"], "placeholder": "auto",
         "full": True,
         "hint": "auto：优先 Tesseract，其次 RapidOCR（PP-OCR），均不可用时降级占位"},
        {"key": "language", "label": "识别语言", "type": "text",
         "placeholder": "chi_sim+eng", "hint": "仅 Tesseract 生效；RapidOCR 默认中英混排"},
        {"key": "max_pages", "label": "PDF 最多识别页数", "type": "number",
         "placeholder": str(DEFAULT_MAX_PAGES), "hint": f"取值 1–{MAX_PAGES}"},
    ]
    required_permissions = ("fs.read",)

    def __init__(self, config: dict | None = None):
        self.config = config or {}
        self.provider_name = self.config.get("provider") or "auto"
        self._provider = None

    @property
    def provider(self):
        if self._provider is None:
            self._provider = build_provider(self.provider_name, self.config)
        return self._provider

    def capabilities(self) -> dict:
        return {
            "ocr": True,
            "types": ["image", "pdf"],
            "searchable": True,
            "provider": resolve_provider_name(self.provider_name),
            "requested_provider": self.provider_name,
            "available": available_providers(),
            "accuracy_target": 0.90,  # PRD 假设口径：常用印刷体抽检 ≥90%
        }

    def health_check(self) -> HealthStatus:
        try:
            provider = self.provider
            probe = getattr(provider, "probe", None)
            if probe is not None:
                probe()  # 真实引擎做一次最小推理，占位引擎无副作用
            return HealthStatus(True, False, f"OCR engine={provider.name} ready")
        except Exception as exc:  # noqa: BLE001
            return HealthStatus(False, False, f"OCR provider 不可用: {exc}")

    def recognize(self, data: bytes, filename: str, mime: str) -> dict:
        return self.provider.recognize(data, filename, mime)
