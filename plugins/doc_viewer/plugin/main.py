"""文档查看能力插件（type=capability）。

职责：把 Office Open XML 文档（docx / xlsx / pptx）解析为可安全内联的 HTML，
供平台在线预览；旧版二进制 Office 格式给出下载引导。

- 本模块是注册进 PluginRegistry 的能力插件，业务侧以
  ``registry.create("doc_viewer", config)`` 取实例后调用；
- 浏览器侧由 ``routes.py`` 声明的动态端点提供预览页、纯文本与内嵌图片，
  manifest 的 ``ui.preview`` / ``ui.actions`` 负责把入口挂到前端；
- 解析实现位于同目录 ``doc_viewer_core.py``（纯标准库，无第三方依赖）。

安全说明：HTML 由核心统一转义生成，页面端 CSP 禁止脚本；插件不解析宏，
不执行文档中的任何代码，图片与超链接均按白名单过滤。
"""

from __future__ import annotations

import io
import sys
import zipfile
from pathlib import Path

# 平台按单文件装载 entry，包内多文件需自行把所在目录加入 sys.path
_HERE = str(Path(__file__).resolve().parent)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)


def _evict_stale_core() -> None:
    """清理进程内残留的旧版本核心模块（热更新后避免命中旧缓存）。"""
    name = "doc_viewer_core"
    cached = sys.modules.get(name)
    if cached is None:
        return
    expected = (Path(_HERE) / f"{name}.py").resolve()
    cached_file = getattr(cached, "__file__", None)
    if not cached_file or Path(cached_file).resolve() != expected:
        del sys.modules[name]


_evict_stale_core()

from app.sdk.store_plugin import HealthStatus  # noqa: E402
from doc_viewer_core import (  # noqa: E402
    DEFAULT_MAX_DECOMPRESSED,
    DEFAULT_MAX_SLIDES,
    DEFAULT_MAX_TABLE_COLS,
    DEFAULT_MAX_TABLE_ROWS,
    LEGACY_FORMATS,
    MAX_DECOMPRESSED_LIMIT,
    MAX_SLIDES_LIMIT,
    MAX_TABLE_COLS_LIMIT,
    MAX_TABLE_ROWS_LIMIT,
    PREVIEW_TYPES,
    SUPPORTED_FORMATS,
    clamp_limits,
    classify as _classify,
    detect_kind,
    extract_text as _extract_text,
    render_document,
)

_MB = 1024 * 1024
_DOCX_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_XLSX_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_PKG_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
_PPTX_NS = "http://schemas.openxmlformats.org/presentationml/2006/main"
_DRAW_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"


class DocViewerPlugin:
    plugin_id = "doc_viewer"
    display_name = "文档查看插件"
    required_permissions = ("fs.read", "net.outbound")
    config_schema = [
        {"key": "max_decompressed_mb", "label": "解压后上限(MB)", "type": "number",
         "placeholder": str(DEFAULT_MAX_DECOMPRESSED // _MB),
         "hint": f"文档 zip 解压总量上限；取值 1–{MAX_DECOMPRESSED_LIMIT // _MB}"},
        {"key": "max_table_rows", "label": "表格最大行数", "type": "number",
         "placeholder": str(DEFAULT_MAX_TABLE_ROWS),
         "hint": f"Excel 单表展示行数上限；取值 1–{MAX_TABLE_ROWS_LIMIT}"},
        {"key": "max_table_cols", "label": "表格最大列数", "type": "number",
         "placeholder": str(DEFAULT_MAX_TABLE_COLS),
         "hint": f"Excel 单表展示列数上限；取值 1–{MAX_TABLE_COLS_LIMIT}"},
        {"key": "max_slides", "label": "最大幻灯片数", "type": "number",
         "placeholder": str(DEFAULT_MAX_SLIDES),
         "hint": f"PPTX 展示页数上限；取值 1–{MAX_SLIDES_LIMIT}"},
    ]

    def __init__(self, config: dict | None = None):
        self.config = config or {}
        decompressed_mb = self.config.get("max_decompressed_mb")
        self.max_rows, self.max_cols, self.max_slides, self.max_decompressed = clamp_limits(
            self.config.get("max_table_rows"),
            self.config.get("max_table_cols"),
            self.config.get("max_slides"),
            int(decompressed_mb) * _MB if _is_int(decompressed_mb) else None,
        )

    def capabilities(self) -> dict:
        return {
            "document_viewer": True,
            "formats": list(SUPPORTED_FORMATS),
            "legacy_formats": sorted(LEGACY_FORMATS),
            "preview": True,
            "preview_types": list(PREVIEW_TYPES),
            "preview_url": "/api/doc_viewer/{file_id}/view",
            "text": True,
            "media": True,
            "macros": False,
            "max_table_rows": self.max_rows,
            "max_table_cols": self.max_cols,
            "max_slides": self.max_slides,
            "max_decompressed": self.max_decompressed,
        }

    def health_check(self) -> HealthStatus:
        """构造最小 docx / xlsx / pptx 内存样例，自检解析链路（无副作用）。"""
        probes = (
            ("docx", _probe_docx(), "文档查看自检"),
            ("xlsx", _probe_xlsx(), "表格自检"),
            ("pptx", _probe_pptx(), "演示自检"),
        )
        for kind, data, expected in probes:
            try:
                result = render_document(
                    data, f"probe.{kind}",
                    max_table_rows=self.max_rows,
                    max_table_cols=self.max_cols,
                    max_slides=self.max_slides,
                    max_decompressed=self.max_decompressed,
                )
            except Exception as exc:  # noqa: BLE001
                return HealthStatus(False, False, f"{kind} 自检失败: {exc}")
            if result["kind"] != kind or expected not in result["text"]:
                return HealthStatus(False, False, f"{kind} 自检结果不一致")
        return HealthStatus(True, False, "doc viewer ready")

    # --- 自定义能力方法（按平台调用方约定） ---

    def classify(self, ext: str) -> tuple[bool, str]:
        """扩展名 → (previewable, preview_type)。"""
        return _classify(ext)

    def detect_kind(self, data: bytes, filename: str = "") -> str:
        """按包结构/扩展名判定文档类型。"""
        return detect_kind(data, filename)

    def render(self, data: bytes, filename: str = "", media_prefix: str = "") -> dict:
        """解析为 {kind, title, html, text, counts, warnings}。"""
        return render_document(
            data, filename, media_prefix,
            max_table_rows=self.max_rows,
            max_table_cols=self.max_cols,
            max_slides=self.max_slides,
            max_decompressed=self.max_decompressed,
        )

    def extract_text(self, data: bytes, filename: str = "") -> str:
        """抽取纯文本。"""
        return _extract_text(
            data, filename,
            max_table_rows=self.max_rows,
            max_table_cols=self.max_cols,
            max_slides=self.max_slides,
            max_decompressed=self.max_decompressed,
        )


def _is_int(value) -> bool:
    try:
        int(value)
    except (TypeError, ValueError):
        return False
    return True


# ---------------------------------------------------------------- 自检样例

def _pack(members: dict[str, str]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in members.items():
            archive.writestr(name, content)
    return buffer.getvalue()


def _probe_docx() -> bytes:
    styles = (
        f'<?xml version="1.0" encoding="UTF-8"?>'
        f'<w:styles xmlns:w="{_DOCX_NS}">'
        '<w:style w:type="paragraph" w:styleId="Heading1">'
        '<w:name w:val="heading 1"/><w:pPr><w:outlineLvl w:val="0"/></w:pPr>'
        "</w:style></w:styles>"
    )
    document = (
        f'<?xml version="1.0" encoding="UTF-8"?>'
        f'<w:document xmlns:w="{_DOCX_NS}"><w:body>'
        '<w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr>'
        "<w:r><w:t>文档查看自检</w:t></w:r></w:p>"
        "<w:p><w:r><w:t>doc viewer probe</w:t></w:r></w:p>"
        "</w:body></w:document>"
    )
    return _pack({
        "[Content_Types].xml":
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>',
        "word/document.xml": document,
        "word/styles.xml": styles,
    })


def _probe_xlsx() -> bytes:
    workbook = (
        f'<?xml version="1.0" encoding="UTF-8"?>'
        f'<workbook xmlns="{_XLSX_NS}" xmlns:r="{_REL_NS}">'
        '<sheets><sheet name="自检" sheetId="1" r:id="rId1"/></sheets></workbook>'
    )
    rels = (
        f'<?xml version="1.0" encoding="UTF-8"?>'
        f'<Relationships xmlns="{_PKG_REL_NS}">'
        '<Relationship Id="rId1" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
        'Target="worksheets/sheet1.xml"/></Relationships>'
    )
    sheet = (
        f'<?xml version="1.0" encoding="UTF-8"?>'
        f'<worksheet xmlns="{_XLSX_NS}"><sheetData>'
        '<row r="1"><c r="A1" t="inlineStr"><is><t>表格自检</t></is></c>'
        '<c r="B1"><v>42</v></c></row>'
        "</sheetData></worksheet>"
    )
    return _pack({
        "xl/workbook.xml": workbook,
        "xl/_rels/workbook.xml.rels": rels,
        "xl/worksheets/sheet1.xml": sheet,
    })


def _probe_pptx() -> bytes:
    presentation = (
        f'<?xml version="1.0" encoding="UTF-8"?>'
        f'<p:presentation xmlns:p="{_PPTX_NS}" xmlns:r="{_REL_NS}">'
        '<p:sldIdLst><p:sldId id="256" r:id="rId1"/></p:sldIdLst></p:presentation>'
    )
    rels = (
        f'<?xml version="1.0" encoding="UTF-8"?>'
        f'<Relationships xmlns="{_PKG_REL_NS}">'
        '<Relationship Id="rId1" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slide" '
        'Target="slides/slide1.xml"/></Relationships>'
    )
    slide = (
        f'<?xml version="1.0" encoding="UTF-8"?>'
        f'<p:sld xmlns:p="{_PPTX_NS}" xmlns:a="{_DRAW_NS}"><p:cSld><p:spTree>'
        "<p:sp><p:txBody><a:p><a:r><a:t>演示自检</a:t></a:r></a:p></p:txBody></p:sp>"
        "</p:spTree></p:cSld></p:sld>"
    )
    return _pack({
        "ppt/presentation.xml": presentation,
        "ppt/_rels/presentation.xml.rels": rels,
        "ppt/slides/slide1.xml": slide,
    })
