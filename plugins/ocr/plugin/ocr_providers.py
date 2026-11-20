"""OCR provider 协议与内置实现（AIC-015-3，REQ-BIZ-02）。

供应商/模型未定（PRD 待确认），按适配器模式隔离：
- StubOcrProvider：本地占位实现，不做真实识别，从图片/PDF 的可见 ASCII/UTF-8
  文本片段做保守提取，明确标注 engine="stub"、confidence 为启发值。
  用途：流程打通、契约测试、离线演示。
- 真实 provider（云 OCR / 本地模型）后续实现同一协议注册即可，不改插件/API。

严禁在未获批准前引入云端 SDK 或大模型依赖。
"""

from __future__ import annotations

import re
from typing import Protocol


class OcrProvider(Protocol):
    name: str

    def recognize(self, data: bytes, filename: str, mime: str) -> dict:
        """返回 {text, confidence, engine, language, blocks}。"""
        ...


class StubOcrProvider:
    """占位 provider：从文件的可读文本段保守提取，不做真实图像识别。"""

    name = "stub"

    def recognize(self, data: bytes, filename: str, mime: str) -> dict:
        # 仅当文件内含可读文本（如文本型 PDF、带说明的图片元数据）时提取
        try:
            decoded = data.decode("utf-8", errors="ignore")
        except Exception:  # noqa: BLE001
            decoded = ""
        # 提取连续可打印片段（≥4 字符），过滤纯二进制噪声
        fragments = [s.strip() for s in re.findall(r"[\w\u4e00-\u9fff\s，。,.!?！？:：-]{4,}", decoded)]
        text = "\n".join(f for f in fragments if f and not f.isspace())[:20000]
        confidence = 0.0
        if text:
            # 占位置信度：可读文本占比的启发值，不代表真实识别准确率
            readable = sum(1 for c in decoded if c.isprintable())
            confidence = round(min(0.6, readable / max(1, len(decoded)) * 0.6), 2)
        return {
            "text": text,
            "confidence": confidence,
            "engine": "stub",
            "language": "auto",
            "blocks": [{"text": f, "confidence": confidence} for f in fragments[:50]],
            "notice": "占位引擎：供应商/模型未定，当前未执行真实 OCR 识别",
        }


_PROVIDERS: dict[str, type] = {"stub": StubOcrProvider}


def register_provider(name: str, cls: type) -> None:
    _PROVIDERS[name] = cls


def build_provider(name: str = "stub") -> OcrProvider:
    cls = _PROVIDERS.get(name)
    if cls is None:
        raise ValueError(f"未注册的 OCR provider: {name}")
    return cls()
