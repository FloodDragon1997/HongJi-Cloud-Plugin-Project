"""Office 文档查看核心（纯标准库，平台无关）。

支持 WordprocessingML（docx）、SpreadsheetML（xlsx）、PresentationML（pptx）
的只读解析，输出可直接嵌入预览页的安全 HTML 片段与纯文本：

- Word：标题层级、段落/对齐、粗体/斜体/下划线/删除线/字号/颜色、超链接、
  编号与项目符号、表格（跨列）、内嵌图片、页面结构；
- Excel：工作表、共享字符串、单元格样式（粗体/颜色/对齐/百分比/日期）、
  合并单元格、行列上限截断；
- PowerPoint：按放映顺序输出每页文本、缩进层级。

安全边界：
- 所有文本经 ``html.escape`` 转义，不解析文档中的 HTML/宏，不执行任何脚本；
- 图片只允许包内 media 目录且扩展名在白名单内，由路由端点按需取出；
- 超链接只允许 http/https/mailto，并带 ``rel="noopener noreferrer"``；
- zip 解压总量、单条目、行列数、幻灯片数均有上限，防压缩炸弹与内存放大；
- 拒绝 DTD/实体声明（防实体扩展攻击），全程内存操作、不落盘、不使用 extractall。
"""

from __future__ import annotations

import html
import io
import posixpath
import re
import urllib.parse
import zipfile
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta

# ---------------------------------------------------------------- 常量与限制

DEFAULT_MAX_DECOMPRESSED = 64 * 1024 * 1024
MAX_DECOMPRESSED_LIMIT = 256 * 1024 * 1024
MAX_ENTRY_BYTES = 32 * 1024 * 1024
MAX_MEDIA_BYTES = 16 * 1024 * 1024

DEFAULT_MAX_TABLE_ROWS = 500
MAX_TABLE_ROWS_LIMIT = 5000
DEFAULT_MAX_TABLE_COLS = 60
MAX_TABLE_COLS_LIMIT = 200
DEFAULT_MAX_SLIDES = 100
MAX_SLIDES_LIMIT = 500
MAX_SHEETS = 20

DOCX_EXTS = frozenset({"docx"})
XLSX_EXTS = frozenset({"xlsx"})
PPTX_EXTS = frozenset({"pptx"})
SUPPORTED_FORMATS = ("docx", "xlsx", "pptx")

#: 已识别但暂不支持在线渲染的格式（给出下载引导）
LEGACY_FORMATS = {
    "doc": "Word 97-2003 文档",
    "xls": "Excel 97-2003 工作簿",
    "ppt": "PowerPoint 97-2003 演示文稿",
    "wps": "WPS 文档",
    "et": "WPS 表格",
    "dps": "WPS 演示文稿",
    "rtf": "RTF 文档",
    "odt": "OpenDocument 文本文档",
    "ods": "OpenDocument 电子表格",
    "odp": "OpenDocument 演示文稿",
    "ofd": "OFD 版式文档",
    "pages": "Pages 文档",
    "numbers": "Numbers 表格",
    "key": "Keynote 演示文稿",
}

PREVIEW_TYPES = ("docx", "xlsx", "pptx", "office", "none")

#: 允许在线展示的图片扩展名（SVG 刻意排除：同源内联会形成存储型 XSS）
MEDIA_MIMES = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "gif": "image/gif",
    "bmp": "image/bmp",
    "webp": "image/webp",
}
MEDIA_PREFIXES = ("word/media/", "xl/media/", "ppt/media/")

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
_A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
_P = "{http://schemas.openxmlformats.org/presentationml/2006/main}"
_S = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_REL = "{http://schemas.openxmlformats.org/package/2006/relationships}"
_V = "{urn:schemas-microsoft-com:vml}"
_DC_TITLE = "{http://purl.org/dc/elements/1.1/}title"

_CELL_RE = re.compile(r"^([A-Za-z]+)(\d+)$")
_DRIVE_RE = re.compile(r"^[A-Za-z]:")
_HEX_RE = re.compile(r"^[0-9A-Fa-f]{6}$")
_HEADING_RE = re.compile(r"^(?:heading|标题)\s*([1-9])$", re.IGNORECASE)
_SAFE_SCHEMES = ("http://", "https://", "mailto:")

_HIGHLIGHT_COLORS = {
    "yellow": "#fff3a3",
    "green": "#b5f0c0",
    "cyan": "#b3f0f0",
    "magenta": "#f7c6f7",
    "blue": "#c4dcff",
    "red": "#ffc7c7",
    "darkblue": "#9db9e8",
    "darkred": "#f0b0b0",
    "darkgreen": "#a7d8a7",
    "darkcyan": "#9fd8d8",
    "darkmagenta": "#dfb0df",
    "darkyellow": "#e8d59a",
    "gray": "#d9d9d9",
    "lightgray": "#ececec",
}

_DATE_FMT_IDS = frozenset({14, 15, 16, 17, 18, 19, 20, 21, 22, 45, 46, 47})
_PERCENT_FMT_IDS = frozenset({9, 10})


# ---------------------------------------------------------------- 通用工具

def _ext(filename: str) -> str:
    base = (filename or "").replace("\\", "/").rsplit("/", 1)[-1]
    return base.rsplit(".", 1)[-1].lower() if "." in base else ""


def _stem(filename: str) -> str:
    base = (filename or "").replace("\\", "/").rsplit("/", 1)[-1]
    return base.rsplit(".", 1)[0] if "." in base else base


def _flag(el) -> bool | None:
    """Word 三态开关：元素不存在返回 None，``val=0/false/off`` 为 False。"""
    if el is None:
        return None
    value = (el.get(_W + "val") or "true").lower()
    return value not in ("0", "false", "off")


def _clamp_int(value, default: int, low: int, high: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, number))


def clamp_limits(max_rows=None, max_cols=None, max_slides=None,
                 max_decompressed=None) -> tuple[int, int, int, int]:
    return (
        _clamp_int(max_rows, DEFAULT_MAX_TABLE_ROWS, 1, MAX_TABLE_ROWS_LIMIT),
        _clamp_int(max_cols, DEFAULT_MAX_TABLE_COLS, 1, MAX_TABLE_COLS_LIMIT),
        _clamp_int(max_slides, DEFAULT_MAX_SLIDES, 1, MAX_SLIDES_LIMIT),
        _clamp_int(max_decompressed, DEFAULT_MAX_DECOMPRESSED, 1024 * 1024,
                   MAX_DECOMPRESSED_LIMIT),
    )


def _open_zip(data: bytes, max_decompressed: int = DEFAULT_MAX_DECOMPRESSED) -> zipfile.ZipFile:
    """打开包内 zip，拒绝加密条目并校验解压总量，防压缩炸弹。"""
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except (zipfile.BadZipFile, ValueError) as exc:
        raise ValueError("不是有效的 Office 文档（zip 结构损坏或格式不符）") from exc

    total = 0
    for info in archive.infolist():
        if info.flag_bits & 0x1:
            archive.close()
            raise ValueError("加密文档不支持在线查看，请下载后打开")
        total += info.file_size
    if total > max_decompressed:
        archive.close()
        raise ValueError(
            f"文档解压后约 {total // (1024 * 1024)}MB，超过上限 "
            f"{max_decompressed // (1024 * 1024)}MB，请下载后查看"
        )
    return archive


def _read_member(archive: zipfile.ZipFile, name: str,
                 max_bytes: int = MAX_ENTRY_BYTES) -> bytes | None:
    try:
        info = archive.getinfo(name)
    except KeyError:
        return None
    if info.is_dir():
        return None
    if info.file_size > max_bytes:
        raise ValueError(f"文档内部条目 {name} 过大，无法在线查看")
    return archive.read(info)


def _load_xml(archive: zipfile.ZipFile, name: str):
    raw = _read_member(archive, name)
    if raw is None:
        return None
    # 拒绝 DTD/实体声明，避免实体扩展攻击
    if b"<!DOCTYPE" in raw or b"<!ENTITY" in raw:
        raise ValueError(f"文档包含不支持的 DTD/实体声明: {name}")
    try:
        return ET.fromstring(raw)
    except ET.ParseError as exc:
        raise ValueError(f"文档内部 XML 结构损坏: {name}") from exc


def _read_rels(archive: zipfile.ZipFile, path: str, base: str) -> dict[str, dict]:
    """读取关系文件：{rId: {"target": 包内绝对路径或外链, "external": bool}}。"""
    root = _load_xml(archive, path)
    if root is None:
        return {}
    rels: dict[str, dict] = {}
    for rel in root.findall(_REL + "Relationship"):
        rid = rel.get("Id")
        target = rel.get("Target") or ""
        if not rid or not target:
            continue
        external = (rel.get("TargetMode") or "").lower() == "external"
        if external:
            rels[rid] = {"target": target, "external": True}
        else:
            # 以 / 开头表示包内绝对路径，否则相对关系文件所在目录解析
            joined = target.lstrip("/") if target.startswith("/") else posixpath.join(base, target)
            normalized = posixpath.normpath(joined)
            if normalized.startswith("../") or normalized == ".." or _DRIVE_RE.match(normalized):
                continue
            rels[rid] = {"target": normalized, "external": False}
    return rels


def detect_kind(data: bytes, filename: str = "") -> str:
    """判定文档类型：docx/xlsx/pptx 或已识别的旧格式扩展名，未知返回 none。"""
    ext = _ext(filename)
    if ext in LEGACY_FORMATS:
        return ext
    if data[:2] == b"PK":
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                names = set(archive.namelist())
        except (zipfile.BadZipFile, ValueError):
            names = set()
        if "word/document.xml" in names:
            return "docx"
        if "xl/workbook.xml" in names:
            return "xlsx"
        if "ppt/presentation.xml" in names:
            return "pptx"
    return ext if ext in SUPPORTED_FORMATS else "none"


def classify(ext: str) -> tuple[bool, str]:
    """扩展名 → (previewable, preview_type)，与平台预览口径保持一致。"""
    ext = (ext or "").lower().lstrip(".")
    if ext in DOCX_EXTS:
        return True, "docx"
    if ext in XLSX_EXTS:
        return True, "xlsx"
    if ext in PPTX_EXTS:
        return True, "pptx"
    if ext in LEGACY_FORMATS:
        return False, "office"
    return False, "none"


def _is_media_target(target: str) -> bool:
    if not target.startswith(MEDIA_PREFIXES):
        return False
    if ".." in target.split("/"):
        return False
    return _ext(target) in MEDIA_MIMES


def extract_media(data: bytes, name: str,
                  max_decompressed: int = DEFAULT_MAX_DECOMPRESSED) -> tuple[bytes, str]:
    """取出文档内嵌图片字节；只允许白名单目录与图片扩展名。"""
    if not _is_media_target(name):
        raise ValueError("图片路径非法或类型不允许")
    with _open_zip(data, max_decompressed) as archive:
        raw = _read_member(archive, name, max_bytes=MAX_MEDIA_BYTES)
    if raw is None:
        raise ValueError("文档内未找到该图片")
    return raw, MEDIA_MIMES[_ext(name)]


# ---------------------------------------------------------------- 结果封装

def _result(kind: str, title: str, body_html: str, text: str,
            counts: dict | None = None, warnings: list[str] | None = None) -> dict:
    return {
        "kind": kind,
        "title": title or "未命名文档",
        "html": body_html,
        "text": text,
        "counts": counts or {},
        "warnings": warnings or [],
    }


def _unsupported_result(kind: str, filename: str) -> dict:
    ext = (kind if kind in LEGACY_FORMATS else _ext(filename)) or "unknown"
    label = LEGACY_FORMATS.get(ext, "该格式")
    safe_name = html.escape(filename or f"未命名.{ext}")
    body = (
        '<div class="notice">'
        f"<h2>暂不支持在线查看</h2>"
        f"<p><strong>{safe_name}</strong></p>"
        f"<p>{html.escape(label)}暂不支持浏览器内渲染，请下载后使用本地应用打开。</p>"
        '<p class="hint">支持在线查看：DOCX（Word）、XLSX（Excel）、PPTX（PowerPoint）。</p>'
        "</div>"
    )
    return _result(ext, _stem(filename) or label, body, "", {"supported": False},
                   [f"{label}暂不支持在线查看"])


# ---------------------------------------------------------------- Word（docx）

def _docx_styles(archive: zipfile.ZipFile) -> dict[str, dict]:
    root = _load_xml(archive, "word/styles.xml")
    if root is None:
        return {}
    styles: dict[str, dict] = {}
    for style in root.findall(_W + "style"):
        sid = style.get(_W + "styleId")
        if not sid:
            continue
        name_el = style.find(_W + "name")
        based_el = style.find(_W + "basedOn")
        rpr = style.find(_W + "rPr")
        ppr = style.find(_W + "pPr")
        entry: dict = {
            "name": name_el.get(_W + "val") if name_el is not None else None,
            "based": based_el.get(_W + "val") if based_el is not None else None,
        }
        if rpr is not None:
            entry["bold"] = _flag(rpr.find(_W + "b"))
            entry["italic"] = _flag(rpr.find(_W + "i"))
            size_el = rpr.find(_W + "sz")
            if size_el is not None:
                try:
                    entry["size"] = int(size_el.get(_W + "val"))
                except (TypeError, ValueError):
                    pass
            color_el = rpr.find(_W + "color")
            if color_el is not None:
                color = color_el.get(_W + "val") or ""
                if _HEX_RE.match(color):
                    entry["color"] = color.upper()
        if ppr is not None:
            jc_el = ppr.find(_W + "jc")
            if jc_el is not None:
                entry["align"] = jc_el.get(_W + "val")
            outline_el = ppr.find(_W + "outlineLvl")
            if outline_el is not None:
                try:
                    entry["outline"] = int(outline_el.get(_W + "val"))
                except (TypeError, ValueError):
                    pass
        styles[sid] = entry
    return styles


def _resolve_style(style_id: str, styles: dict[str, dict],
                   cache: dict[str, dict]) -> dict:
    if not style_id:
        return {}
    if style_id in cache:
        return cache[style_id]
    chain: list[dict] = []
    seen: set[str] = set()
    current = style_id
    while current and current not in seen and current in styles:
        seen.add(current)
        entry = styles[current]
        chain.append(entry)
        current = entry.get("based") or ""
    merged: dict = {}
    for entry in reversed(chain):
        for key, value in entry.items():
            if key != "based" and value is not None:
                merged[key] = value
    cache[style_id] = merged
    return merged


def _docx_numbering(archive: zipfile.ZipFile) -> dict[str, dict[int, dict]]:
    root = _load_xml(archive, "word/numbering.xml")
    if root is None:
        return {}
    abstract: dict[str, dict[int, dict]] = {}
    for item in root.findall(_W + "abstractNum"):
        aid = item.get(_W + "abstractNumId")
        if not aid:
            continue
        levels: dict[int, dict] = {}
        for lvl in item.findall(_W + "lvl"):
            try:
                ilvl = int(lvl.get(_W + "ilvl") or 0)
            except (TypeError, ValueError):
                ilvl = 0
            fmt_el = lvl.find(_W + "numFmt")
            start_el = lvl.find(_W + "start")
            text_el = lvl.find(_W + "lvlText")
            try:
                start = int(start_el.get(_W + "val")) if start_el is not None else 1
            except (TypeError, ValueError):
                start = 1
            levels[ilvl] = {
                "fmt": fmt_el.get(_W + "val") if fmt_el is not None else "decimal",
                "start": start,
                "text": text_el.get(_W + "val") if text_el is not None else "",
            }
        abstract[aid] = levels

    numbering: dict[str, dict[int, dict]] = {}
    for num in root.findall(_W + "num"):
        nid = num.get(_W + "numId")
        ref = num.find(_W + "abstractNumId")
        if not nid or ref is None:
            continue
        numbering[nid] = abstract.get(ref.get(_W + "val") or "", {})
    return numbering


def _list_marker(fmt: str, number: int, level_text: str) -> str:
    fmt = (fmt or "bullet").lower()
    if fmt in ("bullet", "none"):
        return "•" if fmt == "bullet" else ""
    if fmt == "lowerletter":
        return f"{_alpha(number).lower()}."
    if fmt == "upperletter":
        return f"{_alpha(number)}."
    if fmt == "lowerroman":
        return f"{_roman(number).lower()}."
    if fmt == "upperroman":
        return f"{_roman(number)}."
    if fmt == "decimalzero":
        return f"{number:02d}."
    if fmt == "chinese" or fmt == "ideographdigital":
        return f"{number}."
    return f"{number}."


def _alpha(number: int) -> str:
    if number <= 0:
        return "A"
    result = ""
    while number:
        number, rem = divmod(number - 1, 26)
        result = chr(65 + rem) + result
    return result


def _roman(number: int) -> str:
    if number <= 0 or number > 3999:
        return str(number)
    table = ((1000, "M"), (900, "CM"), (500, "D"), (400, "CD"), (100, "C"),
             (90, "XC"), (50, "L"), (40, "XL"), (10, "X"), (9, "IX"),
             (5, "V"), (4, "IV"), (1, "I"))
    out = []
    for value, symbol in table:
        while number >= value:
            out.append(symbol)
            number -= value
    return "".join(out)


def _media_src(target: str, ctx: dict) -> str:
    if not _is_media_target(target):
        return ""
    prefix = ctx.get("media_prefix") or ""
    if not prefix:
        return ""
    return prefix + urllib.parse.quote(target, safe="")


def _image_html(rid: str | None, ctx: dict) -> str:
    rel = ctx["rels"].get(rid or "")
    if not rel or rel.get("external"):
        return ""
    src = _media_src(rel.get("target", ""), ctx)
    if not src:
        return ""
    ctx["images"] += 1
    return ('<img class="doc-img" src="' + html.escape(src, quote=True)
            + '" alt="文档图片" loading="lazy">')


def _drawing_html(drawing, ctx: dict) -> str:
    blip = drawing.find(".//" + _A + "blip")
    rid = blip.get(_R + "embed") if blip is not None else None
    return _image_html(rid, ctx)


def _vml_html(pict, ctx: dict) -> str:
    data = pict.find(".//" + _V + "imagedata")
    rid = data.get(_R + "id") if data is not None else None
    return _image_html(rid, ctx)


def _run_format(run, ctx: dict, inherited: dict) -> dict:
    fmt = dict(inherited)
    rpr = run.find(_W + "rPr")
    if rpr is None:
        return fmt

    style_el = rpr.find(_W + "rStyle")
    if style_el is not None:
        styled = _resolve_style(style_el.get(_W + "val") or "",
                                ctx["styles"], ctx["resolved"])
        for key in ("bold", "italic", "size", "color"):
            if styled.get(key) is not None:
                fmt[key] = styled[key]

    for key, tag in (("bold", "b"), ("italic", "i"), ("strike", "strike")):
        flag = _flag(rpr.find(_W + tag))
        if flag is not None:
            fmt[key] = flag

    underline = rpr.find(_W + "u")
    if underline is not None:
        value = (underline.get(_W + "val") or "single").lower()
        fmt["underline"] = value not in ("none", "0", "false")

    color_el = rpr.find(_W + "color")
    if color_el is not None:
        color = color_el.get(_W + "val") or ""
        if _HEX_RE.match(color):
            fmt["color"] = color.upper()

    size_el = rpr.find(_W + "sz")
    if size_el is not None:
        try:
            fmt["size"] = int(size_el.get(_W + "val"))
        except (TypeError, ValueError):
            pass

    vert = rpr.find(_W + "vertAlign")
    if vert is not None:
        value = (vert.get(_W + "val") or "").lower()
        fmt["vertAlign"] = value or None

    highlight = rpr.find(_W + "highlight")
    if highlight is not None:
        fmt["highlight"] = (highlight.get(_W + "val") or "").lower() or None
    return fmt


def _render_run(run, ctx: dict, inherited: dict) -> str:
    fmt = _run_format(run, ctx, inherited)
    pieces: list[str] = []
    for child in run:
        tag = child.tag
        if tag == _W + "t":
            pieces.append(html.escape(child.text or ""))
        elif tag == _W + "tab":
            pieces.append("\u2003")
        elif tag in (_W + "br", _W + "cr"):
            pieces.append("<br>")
        elif tag == _W + "drawing":
            pieces.append(_drawing_html(child, ctx))
        elif tag == _W + "pict":
            pieces.append(_vml_html(child, ctx))
        elif tag == _W + "noBreakHyphen":
            pieces.append("-")
        elif tag == _W + "softHyphen":
            pieces.append("&shy;")
    content = "".join(pieces)
    if not content:
        return ""

    if fmt.get("size"):
        content = f'<span style="font-size:{fmt["size"] / 2:g}pt">{content}</span>'
    if fmt.get("color"):
        content = f'<span style="color:#{fmt["color"]}">{content}</span>'
    highlight = _HIGHLIGHT_COLORS.get(fmt.get("highlight") or "")
    if highlight:
        content = f'<span style="background-color:{highlight}">{content}</span>'
    if fmt.get("strike"):
        content = f"<s>{content}</s>"
    if fmt.get("underline"):
        content = f"<u>{content}</u>"
    if fmt.get("italic"):
        content = f"<em>{content}</em>"
    if fmt.get("bold"):
        content = f"<strong>{content}</strong>"
    if (fmt.get("vertAlign") or "").lower() == "superscript":
        content = f"<sup>{content}</sup>"
    elif (fmt.get("vertAlign") or "").lower() == "subscript":
        content = f"<sub>{content}</sub>"
    return content


def _render_inline(container, ctx: dict, inherited: dict, out: list[str]) -> None:
    for child in container:
        tag = child.tag
        if tag == _W + "r":
            out.append(_render_run(child, ctx, inherited))
        elif tag == _W + "hyperlink":
            inner: list[str] = []
            _render_inline(child, ctx, inherited, inner)
            content = "".join(inner)
            if not content:
                continue
            rel = ctx["rels"].get(child.get(_R + "id") or "")
            href = rel.get("target") if rel and rel.get("external") else ""
            if href and href.lower().startswith(_SAFE_SCHEMES):
                out.append('<a href="' + html.escape(href, quote=True)
                           + '" target="_blank" rel="noopener noreferrer">'
                           + content + "</a>")
            else:
                out.append(content)
        elif tag in (_W + "ins", _W + "smartTag", _W + "fldSimple"):
            _render_inline(child, ctx, inherited, out)
        elif tag == _W + "sdt":
            block = child.find(_W + "sdtContent")
            if block is not None:
                _render_inline(block, ctx, inherited, out)
        elif tag == _W + "del":
            continue


def _paragraph_props(p, ctx: dict) -> dict:
    ppr = p.find(_W + "pPr")
    props: dict = {"style": "", "align": None, "left": 0, "first": 0,
                   "numid": None, "ilvl": 0, "outline": None}
    if ppr is None:
        return props
    style_el = ppr.find(_W + "pStyle")
    if style_el is not None:
        props["style"] = style_el.get(_W + "val") or ""
    jc = ppr.find(_W + "jc")
    if jc is not None:
        props["align"] = (jc.get(_W + "val") or "").lower() or None
    ind = ppr.find(_W + "ind")
    if ind is not None:
        for key, tag in (("left", "left"), ("left", "start")):
            value = ind.get(_W + tag)
            if value:
                try:
                    props["left"] = int(value)
                    break
                except (TypeError, ValueError):
                    pass
        first = ind.get(_W + "firstLine")
        if first:
            try:
                props["first"] = int(first)
            except (TypeError, ValueError):
                pass
    numpr = ppr.find(_W + "numPr")
    if numpr is not None:
        numid_el = numpr.find(_W + "numId")
        ilvl_el = numpr.find(_W + "ilvl")
        if numid_el is not None:
            props["numid"] = numid_el.get(_W + "val")
        if ilvl_el is not None:
            try:
                props["ilvl"] = int(ilvl_el.get(_W + "val") or 0)
            except (TypeError, ValueError):
                props["ilvl"] = 0
    outline = ppr.find(_W + "outlineLvl")
    if outline is not None:
        try:
            props["outline"] = int(outline.get(_W + "val"))
        except (TypeError, ValueError):
            pass
    return props


def _heading_level(name: str | None, outline: int | None) -> int:
    if outline is not None:
        return max(1, min(6, outline + 1))
    if name:
        match = _HEADING_RE.match(name.strip())
        if match:
            return int(match.group(1))
    return 0


def _next_list_marker(props: dict, ctx: dict) -> str:
    numid, ilvl = props["numid"], props["ilvl"]
    if not numid or numid == "0":
        return ""
    levels = ctx["numbering"].get(numid) or {}
    level = levels.get(ilvl) or {}
    fmt = level.get("fmt") or "bullet"
    key = (numid, ilvl)
    start = level.get("start", 1)
    number = ctx["counters"].get(key, start - 1) + 1
    ctx["counters"][key] = number
    for other in [k for k in ctx["counters"] if k[0] == numid and k[1] > ilvl]:
        del ctx["counters"][other]
    return _list_marker(fmt, number, level.get("text") or "")


def _render_paragraph(p, ctx: dict) -> str:
    props = _paragraph_props(p, ctx)
    styled = _resolve_style(props["style"], ctx["styles"], ctx["resolved"]) if props["style"] else {}
    heading = _heading_level(styled.get("name") or "",
                             props["outline"] if props["outline"] is not None
                             else styled.get("outline"))
    # 标题的字号/加粗交给 hN 的样式，避免重复包裹行内标签
    inherited = {} if heading else {
        "bold": styled.get("bold"),
        "italic": styled.get("italic"),
        "size": styled.get("size"),
        "color": styled.get("color"),
    }
    parts: list[str] = []
    _render_inline(p, ctx, inherited, parts)
    inner = "".join(parts)
    if not inner.strip():
        return ""

    if heading:
        ctx["headings"] += 1
        return f"<h{heading}>{inner}</h{heading}>"

    styles: list[str] = []
    align = props["align"] or (styled.get("align") or "").lower()
    align_map = {"center": "center", "right": "right", "end": "right",
                 "both": "justify", "justify": "justify", "distribute": "justify"}
    if align in align_map:
        styles.append(f"text-align:{align_map[align]}")

    marker = _next_list_marker(props, ctx)
    left = props["left"]
    if marker:
        left += props["ilvl"] * 360
    if left > 0:
        styles.append(f"margin-left:{min(left / 20, 216):g}pt")
    if props["first"] > 0:
        styles.append(f"text-indent:{min(props['first'] / 20, 216):g}pt")

    style_attr = f' style="{";".join(styles)}"' if styles else ""
    if marker:
        return (f'<p class="li"{style_attr}><span class="marker">{html.escape(marker)}</span>'
                f"{inner}</p>")
    return f"<p{style_attr}>{inner}</p>"


def _render_table(table, ctx: dict) -> str:
    ctx["tables"] += 1
    rows: list[str] = []
    for tr in table.findall(_W + "tr"):
        trpr = tr.find(_W + "trPr")
        is_header = trpr is not None and trpr.find(_W + "tblHeader") is not None
        cells: list[str] = []
        for tc in tr.findall(_W + "tc"):
            tcpr = tc.find(_W + "tcPr")
            colspan = 1
            vmerge = None
            if tcpr is not None:
                span_el = tcpr.find(_W + "gridSpan")
                if span_el is not None:
                    try:
                        colspan = max(1, int(span_el.get(_W + "val") or 1))
                    except (TypeError, ValueError):
                        colspan = 1
                merge_el = tcpr.find(_W + "vMerge")
                if merge_el is not None:
                    vmerge = (merge_el.get(_W + "val") or "continue").lower()
            if vmerge == "continue":
                # 纵向合并的续接单元格：内容已由首格覆盖，跳过避免重复
                continue
            content: list[str] = []
            for child in tc:
                if child.tag == _W + "p":
                    rendered = _render_paragraph(child, ctx)
                    if rendered:
                        content.append(rendered)
                elif child.tag == _W + "tbl":
                    content.append(_render_table(child, ctx))
                elif child.tag == _W + "sdt":
                    block = child.find(_W + "sdtContent")
                    if block is not None:
                        for sub in block:
                            if sub.tag == _W + "p":
                                rendered = _render_paragraph(sub, ctx)
                                if rendered:
                                    content.append(rendered)
            tag = "th" if is_header else "td"
            span_attr = f' colspan="{colspan}"' if colspan > 1 else ""
            cells.append(f"<{tag}{span_attr}>{''.join(content) or '&nbsp;'}</{tag}>")
        if cells:
            rows.append("<tr>" + "".join(cells) + "</tr>")
    if not rows:
        return ""
    return '<table class="doc-table"><tbody>' + "".join(rows) + "</tbody></table>"


def _walk_docx_body(container, ctx: dict, blocks: list[str]) -> None:
    for child in container:
        if child.tag == _W + "p":
            rendered = _render_paragraph(child, ctx)
            if rendered:
                blocks.append(rendered)
        elif child.tag == _W + "tbl":
            rendered = _render_table(child, ctx)
            if rendered:
                blocks.append(rendered)
        elif child.tag == _W + "sdt":
            block = child.find(_W + "sdtContent")
            if block is not None:
                _walk_docx_body(block, ctx, blocks)


def _docx_text(body) -> str:
    lines: list[str] = []
    for p in body.iter(_W + "p"):
        parts: list[str] = []
        for node in p.iter():
            if node.tag == _W + "t":
                parts.append(node.text or "")
            elif node.tag == _W + "tab":
                parts.append("\t")
            elif node.tag in (_W + "br", _W + "cr"):
                parts.append("\n")
        line = "".join(parts)
        if line.strip():
            lines.append(line)
    return "\n".join(lines)


def _docx_title(archive: zipfile.ZipFile) -> str:
    root = _load_xml(archive, "docProps/core.xml")
    if root is None:
        return ""
    title_el = root.find(_DC_TITLE)
    return (title_el.text or "").strip() if title_el is not None else ""


def _render_docx(archive: zipfile.ZipFile, filename: str, media_prefix: str) -> dict:
    root = _load_xml(archive, "word/document.xml")
    if root is None:
        raise ValueError("文档缺少正文（word/document.xml）")
    body = root.find(_W + "body")
    if body is None:
        raise ValueError("文档正文结构损坏")

    ctx: dict = {
        "rels": _read_rels(archive, "word/_rels/document.xml.rels", "word"),
        "styles": _docx_styles(archive),
        "numbering": _docx_numbering(archive),
        "resolved": {},
        "counters": {},
        "media_prefix": media_prefix,
        "images": 0,
        "tables": 0,
        "headings": 0,
    }
    blocks: list[str] = []
    _walk_docx_body(body, ctx, blocks)
    text = _docx_text(body)
    paragraphs = sum(1 for _ in body.iter(_W + "p"))
    title = _docx_title(archive) or _stem(filename) or "Word 文档"
    counts = {
        "paragraphs": paragraphs,
        "tables": ctx["tables"],
        "headings": ctx["headings"],
        "images": ctx["images"],
    }
    if not blocks:
        blocks.append('<p class="notice-inline">文档正文为空。</p>')
    return _result("docx", title, "".join(blocks), text, counts)


# ---------------------------------------------------------------- Excel（xlsx）

def _si_text(node) -> str:
    if node.tag == _S + "rPh":
        return ""
    if node.tag == _S + "t":
        return node.text or ""
    return "".join(_si_text(child) for child in node)


def _xlsx_shared_strings(archive: zipfile.ZipFile) -> list[str]:
    root = _load_xml(archive, "xl/sharedStrings.xml")
    if root is None:
        return []
    return [_si_text(si) for si in root.findall(_S + "si")]


def _xlsx_styles(archive: zipfile.ZipFile) -> list[dict]:
    root = _load_xml(archive, "xl/styles.xml")
    if root is None:
        return []

    fonts: list[dict] = []
    fonts_el = root.find(_S + "fonts")
    if fonts_el is not None:
        for font in fonts_el.findall(_S + "font"):
            bold = font.find(_S + "b") is not None
            italic = font.find(_S + "i") is not None
            color_el = font.find(_S + "color")
            color = (color_el.get("rgb") or "") if color_el is not None else ""
            size_el = font.find(_S + "sz")
            size = size_el.get("val") if size_el is not None else None
            fonts.append({
                "bold": bold,
                "italic": italic,
                "color": color.upper() if _HEX_RE.match(color) else None,
                "size": size,
            })

    custom_formats: dict[int, str] = {}
    numfmts = root.find(_S + "numFmts")
    if numfmts is not None:
        for fmt in numfmts.findall(_S + "numFmt"):
            try:
                custom_formats[int(fmt.get("numFmtId") or 0)] = fmt.get("formatCode") or ""
            except (TypeError, ValueError):
                continue

    styles: list[dict] = []
    cellxfs = root.find(_S + "cellXfs")
    if cellxfs is not None:
        for xf in cellxfs.findall(_S + "xf"):
            font_id = int(xf.get("fontId") or 0)
            num_fmt = int(xf.get("numFmtId") or 0)
            font = fonts[font_id] if 0 <= font_id < len(fonts) else {}
            align_el = xf.find(_S + "alignment")
            align = (align_el.get("horizontal") or "").lower() if align_el is not None else ""
            code = custom_formats.get(num_fmt, "")
            styles.append({
                **font,
                "num_fmt": num_fmt,
                "align": align,
                "percent": num_fmt in _PERCENT_FMT_IDS or "%" in code,
                "date": num_fmt in _DATE_FMT_IDS,
            })
    return styles


def _xlsx_sheets(archive: zipfile.ZipFile) -> list[dict]:
    workbook = _load_xml(archive, "xl/workbook.xml")
    if workbook is None:
        raise ValueError("工作簿缺少 xl/workbook.xml")
    rels = _read_rels(archive, "xl/_rels/workbook.xml.rels", "xl")
    sheets_el = workbook.find(_S + "sheets")
    if sheets_el is None:
        return []
    names = set(archive.namelist())
    sheets: list[dict] = []
    for sheet in sheets_el.findall(_S + "sheet"):
        rel = rels.get(sheet.get(_R + "id") or "")
        target = rel.get("target") if rel else ""
        if not target or target not in names:
            continue
        sheets.append({
            "name": sheet.get("name") or "Sheet",
            "path": target,
            "state": sheet.get("state") or "visible",
        })
    return sheets


def _ref_cell(ref: str) -> tuple[int, int] | None:
    match = _CELL_RE.match(ref or "")
    if not match:
        return None
    letters, row = match.group(1).upper(), int(match.group(2))
    col = 0
    for ch in letters:
        col = col * 26 + (ord(ch) - 64)
    return row, col


def _format_number(value: float, style: dict) -> str:
    if style.get("percent"):
        return f"{value * 100:g}%"
    if style.get("date") and 0 < value < 100000:
        moment = datetime(1899, 12, 30) + timedelta(days=value)
        if abs(value - int(value)) < 1e-6:
            return moment.strftime("%Y-%m-%d")
        return moment.strftime("%Y-%m-%d %H:%M")
    if float(value).is_integer():
        return str(int(value))
    return f"{value:.10g}"


def _xlsx_cell(cell, shared: list[str], styles: list[dict]) -> tuple[str, dict, bool]:
    """返回 (显示文本, 样式, 是否数值)。"""
    try:
        style_index = int(cell.get("s") or 0)
    except (TypeError, ValueError):
        style_index = 0
    style = styles[style_index] if 0 <= style_index < len(styles) else {}

    cell_type = (cell.get("t") or "").lower()
    if cell_type == "inlinestr":
        inline = cell.find(_S + "is")
        return (_si_text(inline) if inline is not None else ""), style, False

    value_el = cell.find(_S + "v")
    raw = value_el.text if value_el is not None and value_el.text is not None else ""

    if cell_type == "s":
        try:
            index = int(raw)
            return (shared[index] if 0 <= index < len(shared) else ""), style, False
        except (TypeError, ValueError):
            return "", style, False
    if cell_type == "str":
        return raw, style, False
    if cell_type == "b":
        return ("TRUE" if raw.strip() == "1" else "FALSE"), style, False
    if cell_type == "e":
        return (raw or "#ERR"), style, False
    if raw == "":
        return "", style, False
    try:
        number = float(raw)
    except ValueError:
        return raw, style, False
    return _format_number(number, style), style, True


def _xlsx_merges(sheet_root, max_rows: int, max_cols: int):
    anchors: dict[tuple[int, int], tuple[int, int]] = {}
    covered: set[tuple[int, int]] = set()
    merges = sheet_root.find(_S + "mergeCells")
    if merges is None:
        return anchors, covered
    for merge in merges.findall(_S + "mergeCell"):
        ref = merge.get("ref") or ""
        if ":" not in ref:
            continue
        start_ref, end_ref = ref.split(":", 1)
        start, end = _ref_cell(start_ref), _ref_cell(end_ref)
        if not start or not end:
            continue
        row_a, col_a = start
        row_b, col_b = end
        if row_a > row_b:
            row_a, row_b = row_b, row_a
        if col_a > col_b:
            col_a, col_b = col_b, col_a
        if row_a > max_rows or col_a > max_cols:
            continue
        row_b, col_b = min(row_b, max_rows), min(col_b, max_cols)
        anchors[(row_a, col_a)] = (row_b - row_a + 1, col_b - col_a + 1)
        for row in range(row_a, row_b + 1):
            for col in range(col_a, col_b + 1):
                if (row, col) != (row_a, col_a):
                    covered.add((row, col))
    return anchors, covered


def _xlsx_grid(sheet_root, shared: list[str], styles: list[dict],
               max_rows: int, max_cols: int) -> tuple[dict, int, int, bool, bool]:
    grid: dict[tuple[int, int], tuple[str, dict, bool]] = {}
    max_row = max_col = 0
    truncated_rows = truncated_cols = False
    sheet_data = sheet_root.find(_S + "sheetData")
    if sheet_data is None:
        return grid, 0, 0, False, False

    previous_row = 0
    for row in sheet_data.findall(_S + "row"):
        try:
            row_index = int(row.get("r") or previous_row + 1)
        except (TypeError, ValueError):
            row_index = previous_row + 1
        previous_row = row_index
        if row_index > max_rows:
            truncated_rows = True
            break
        previous_col = 0
        for cell in row.findall(_S + "c"):
            ref = _ref_cell(cell.get("r") or "")
            col_index = ref[1] if ref else previous_col + 1
            previous_col = col_index
            if col_index > max_cols:
                truncated_cols = True
                continue
            text, style, is_number = _xlsx_cell(cell, shared, styles)
            if text == "" and not style:
                continue
            grid[(row_index, col_index)] = (text, style, is_number)
            max_row = max(max_row, row_index)
            max_col = max(max_col, col_index)
    return grid, max_row, max_col, truncated_rows, truncated_cols


def _xlsx_cell_html(text: str, style: dict, is_number: bool) -> str:
    styles: list[str] = []
    if style.get("bold"):
        styles.append("font-weight:600")
    if style.get("italic"):
        styles.append("font-style:italic")
    if style.get("color"):
        styles.append(f"color:#{style['color']}")
    if style.get("size"):
        styles.append(f"font-size:{style['size']}pt")
    align = style.get("align") or ("right" if is_number else "")
    if align in ("center", "right", "left"):
        styles.append(f"text-align:{align}")
    style_attr = f' style="{";".join(styles)}"' if styles else ""
    return f"<td{style_attr}>{html.escape(text)}</td>"


def _render_xlsx(archive: zipfile.ZipFile, filename: str,
                 max_rows: int, max_cols: int) -> dict:
    sheets = _xlsx_sheets(archive)
    shared = _xlsx_shared_strings(archive)
    styles = _xlsx_styles(archive)
    warnings: list[str] = []
    if not sheets:
        raise ValueError("工作簿中没有可读取的工作表")
    if len(sheets) > MAX_SHEETS:
        warnings.append(f"工作表数量超过 {MAX_SHEETS}，仅展示前 {MAX_SHEETS} 个")
        sheets = sheets[:MAX_SHEETS]

    sections: list[str] = []
    text_lines: list[str] = []
    total_rows = 0
    for sheet in sheets:
        sheet_root = _load_xml(archive, sheet["path"])
        if sheet_root is None:
            continue
        grid, max_row, max_col, truncated_rows, truncated_cols = _xlsx_grid(
            sheet_root, shared, styles, max_rows, max_cols)
        anchors, covered = _xlsx_merges(sheet_root, max_rows, max_cols)
        total_rows += max_row

        rows_html: list[str] = []
        for row in range(1, max_row + 1):
            cells: list[str] = []
            for col in range(1, max_col + 1):
                if (row, col) in covered:
                    continue
                text, style, is_number = grid.get((row, col), ("", {}, False))
                rowspan, colspan = anchors.get((row, col), (1, 1))
                attrs = ""
                if rowspan > 1:
                    attrs += f' rowspan="{rowspan}"'
                if colspan > 1:
                    attrs += f' colspan="{colspan}"'
                if attrs:
                    styles_attr = []
                    if style.get("bold"):
                        styles_attr.append("font-weight:600")
                    if style.get("color"):
                        styles_attr.append(f"color:#{style['color']}")
                    if style.get("align") in ("center", "right", "left"):
                        styles_attr.append(f"text-align:{style['align']}")
                    if styles_attr:
                        attrs += f' style="{";".join(styles_attr)}"'
                    cells.append(f"<td{attrs}>{html.escape(text)}</td>")
                else:
                    cells.append(_xlsx_cell_html(text, style, is_number))
            if cells:
                rows_html.append("<tr>" + "".join(cells) + "</tr>")

        notes: list[str] = []
        if truncated_rows:
            notes.append(f"行数超过上限 {max_rows}，已截断")
        if truncated_cols:
            notes.append(f"列数超过上限 {max_cols}，已截断")
        note_html = ("".join(f'<p class="truncate">{html.escape(n)}</p>' for n in notes))
        if notes:
            warnings.extend(f"工作表「{sheet['name']}」{n}" for n in notes)

        table = ('<table class="sheet-table"><tbody>' + "".join(rows_html) + "</tbody></table>"
                 if rows_html else '<p class="notice-inline">工作表为空。</p>')
        sections.append(
            f'<section class="sheet"><h2>{html.escape(sheet["name"])}</h2>'
            f"{note_html}{table}</section>"
        )

        text_lines.append(f"# {sheet['name']}")
        for row in range(1, max_row + 1):
            values = [grid.get((row, col), ("", {}, False))[0]
                      for col in range(1, max_col + 1)]
            while values and values[-1] == "":
                values.pop()
            if any(values):
                text_lines.append("\t".join(values))
        text_lines.append("")

    counts = {"sheets": len(sections), "rows": total_rows}
    title = _stem(filename) or "Excel 工作簿"
    body = "".join(sections) or '<p class="notice-inline">工作簿为空。</p>'
    return _result("xlsx", title, body, "\n".join(text_lines), counts, warnings)


# ---------------------------------------------------------------- PowerPoint（pptx）

def _pptx_slides(archive: zipfile.ZipFile) -> list[str]:
    presentation = _load_xml(archive, "ppt/presentation.xml")
    if presentation is None:
        raise ValueError("演示文稿缺少 ppt/presentation.xml")
    rels = _read_rels(archive, "ppt/_rels/presentation.xml.rels", "ppt")
    id_list = presentation.find(_P + "sldIdLst")
    if id_list is None:
        return []
    names = set(archive.namelist())
    slides: list[str] = []
    for slide_id in id_list.findall(_P + "sldId"):
        rel = rels.get(slide_id.get(_R + "id") or "")
        target = rel.get("target") if rel else ""
        if target and target in names:
            slides.append(target)
    return slides


def _slide_paragraphs(slide_root) -> list[tuple[int, str]]:
    items: list[tuple[int, str]] = []
    for paragraph in slide_root.iter(_A + "p"):
        level = 0
        ppr = paragraph.find(_A + "pPr")
        if ppr is not None:
            try:
                level = max(0, int(ppr.get("lvl") or 0))
            except (TypeError, ValueError):
                level = 0
        pieces: list[str] = []
        for node in paragraph:
            if node.tag == _A + "r":
                for text in node.iter(_A + "t"):
                    pieces.append(text.text or "")
            elif node.tag == _A + "br":
                pieces.append("\n")
            elif node.tag == _A + "fld":
                for text in node.iter(_A + "t"):
                    pieces.append(text.text or "")
        content = "".join(pieces).strip()
        if content:
            items.append((min(level, 6), content))
    return items


def _render_pptx(archive: zipfile.ZipFile, filename: str, max_slides: int) -> dict:
    slides = _pptx_slides(archive)
    warnings: list[str] = []
    if not slides:
        raise ValueError("演示文稿中没有可读取的幻灯片")
    truncated = len(slides) > max_slides
    if truncated:
        warnings.append(f"幻灯片数量超过 {max_slides}，仅展示前 {max_slides} 页")
        slides = slides[:max_slides]

    sections: list[str] = []
    text_lines: list[str] = []
    for index, path in enumerate(slides, start=1):
        slide_root = _load_xml(archive, path)
        if slide_root is None:
            continue
        items = _slide_paragraphs(slide_root)
        if not items:
            body = '<p class="notice-inline">本页没有文本内容（可能是纯图片页）。</p>'
        else:
            chunks: list[str] = []
            for order, (level, content) in enumerate(items):
                safe = html.escape(content).replace("\n", "<br>")
                if order == 0 and level == 0:
                    chunks.append(f"<h3>{safe}</h3>")
                elif level > 0:
                    chunks.append(
                        f'<p class="pp bullet" style="margin-left:{level * 1.4:g}em">'
                        f'<span class="marker">•</span>{safe}</p>')
                else:
                    chunks.append(f'<p class="pp">{safe}</p>')
            body = "".join(chunks)
        sections.append(
            f'<section class="slide"><div class="slide-no">第 {index} 页</div>{body}</section>'
        )
        text_lines.append(f"# 第 {index} 页")
        text_lines.extend(content for _, content in items)
        text_lines.append("")

    counts = {"slides": len(sections)}
    title = _stem(filename) or "PowerPoint 演示文稿"
    return _result("pptx", title, "".join(sections), "\n".join(text_lines),
                   counts, warnings)


# ---------------------------------------------------------------- 对外入口

def render_document(data: bytes, filename: str = "", media_prefix: str = "",
                    max_table_rows=None, max_table_cols=None,
                    max_slides=None, max_decompressed=None) -> dict:
    """解析文档并返回 {kind, title, html, text, counts, warnings}。"""
    if not data:
        raise ValueError("文件内容为空")
    rows, cols, slides_limit, decompressed = clamp_limits(
        max_table_rows, max_table_cols, max_slides, max_decompressed)

    kind = detect_kind(data, filename)
    if kind in LEGACY_FORMATS:
        return _unsupported_result(kind, filename)
    if kind not in SUPPORTED_FORMATS:
        return _unsupported_result("none", filename)

    with _open_zip(data, decompressed) as archive:
        if kind == "docx":
            return _render_docx(archive, filename, media_prefix)
        if kind == "xlsx":
            return _render_xlsx(archive, filename, rows, cols)
        return _render_pptx(archive, filename, slides_limit)


def extract_text(data: bytes, filename: str = "",
                 max_table_rows=None, max_table_cols=None,
                 max_slides=None, max_decompressed=None) -> str:
    """只抽取纯文本（用于复制/检索/降级展示）。"""
    result = render_document(data, filename, "", max_table_rows, max_table_cols,
                             max_slides, max_decompressed)
    if not result["text"] and result["kind"] not in SUPPORTED_FORMATS:
        raise ValueError("该格式暂不支持在线查看，请下载后打开")
    return result["text"]
