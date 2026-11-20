"""在线预览能力插件（REQ-BIZ-01，能力插件形式接入插件注册表）。

职责：按扩展名判定预览类型与是否可原生预览；提供内容类型协商。
- 图片/PDF/主流音视频/纯文本：浏览器原生预览（previewable=True）
- Office：转换服务选型【待确认】，占位实现——不内联，给出下载引导（AIC-013 失败处理口径）
- 其他类型：不支持，给出下载引导
"""

from __future__ import annotations

import mimetypes

from app.sdk.store_plugin import HealthStatus

#: 浏览器可直接渲染的类型集（对齐 openapi PreviewResult.preview_type 枚举）
#: 注意：SVG 刻意不在此集合内——SVG 是可在同源执行脚本的文档，
#: 以内联方式返回会形成存储型 XSS，故按不支持类型给出下载引导。
IMAGE_EXTS = frozenset({"png", "jpg", "jpeg", "gif", "webp", "bmp", "ico"})
PDF_EXTS = frozenset({"pdf"})
VIDEO_EXTS = frozenset({"mp4", "webm", "mov", "m4v", "ogv"})
AUDIO_EXTS = frozenset({"mp3", "wav", "ogg", "m4a", "aac", "flac", "opus"})
TEXT_EXTS = frozenset({
    "txt", "md", "json", "csv", "log", "xml", "yml", "yaml", "ini", "cfg", "toml",
    "py", "js", "ts", "java", "c", "cpp", "h", "go", "rs", "sh", "sql", "rst",
    "html", "css",
})
#: Office 文档：需转换服务（占位，仅下载引导）
OFFICE_EXTS = frozenset({
    "doc", "docx", "xls", "xlsx", "ppt", "pptx", "wps", "odt", "ods", "odp", "rtf",
})


class PreviewPlugin:
    """能力插件（注册进 PluginRegistry，受生命周期守卫约束——禁用即 45002）。"""

    plugin_id = "preview"
    display_name = "在线预览插件"
    config_schema = [
        {"key": "provider", "label": "提供方 Provider", "type": "text", "placeholder": "stub",
         "full": True, "hint": "默认 stub（内置演示）；真实 provider 实现同一协议注册即可"},
    ]
    required_permissions = ("fs.read",)

    def __init__(self, config: dict | None = None):
        self.config = config or {}

    def capabilities(self) -> dict:
        return {"preview": True,
                "types": ["image", "pdf", "video", "audio", "text", "docx"]}

    def health_check(self) -> HealthStatus:
        return HealthStatus(healthy=True, writable=False, message="preview ready")

    def classify(self, ext: str) -> tuple[bool, str]:
        """扩展名 → (previewable, preview_type)。"""
        ext = (ext or "").lower().lstrip(".")
        if ext in IMAGE_EXTS:
            return True, "image"
        if ext in PDF_EXTS:
            return True, "pdf"
        if ext in VIDEO_EXTS:
            return True, "video"
        if ext in AUDIO_EXTS:
            return True, "audio"
        if ext in TEXT_EXTS:
            return True, "text"
        if ext == "docx":
            return True, "docx"  # 零依赖正文抽取（docx_text），完整排版仍建议下载
        if ext in OFFICE_EXTS:
            return False, "office"  # 其余 Office（doc/xls/ppt）转换服务未接入（占位）
        return False, "none"

    def media_type(self, filename: str, preview_type: str) -> str:
        """内容协商：文本/docx 抽取强制 text/plain（防 HTML/脚本内联执行），其余按扩展名猜测。"""
        if preview_type in ("text", "docx"):
            return "text/plain; charset=utf-8"
        guessed, _ = mimetypes.guess_type(filename)
        return guessed or "application/octet-stream"
