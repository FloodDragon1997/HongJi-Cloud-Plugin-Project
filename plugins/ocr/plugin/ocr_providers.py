"""OCR provider 协议与内置实现（REQ-BIZ-02）。

按适配器模式隔离，默认 auto：优先本地 Tesseract，其次 RapidOCR（PP-OCR ONNX），
都不可用时降级 stub。
- StubOcrProvider：零依赖占位实现，只从可读文本片段保守提取（engine="stub"）。
- RapidOcrProvider：本地 PP-OCR（rapidocr + onnxruntime），离线可用，默认中英混排。
- TesseractOcrProvider：本地 Tesseract（pytesseract + 可执行程序），可配置语言包。
真实 provider 均惰性导入第三方依赖，未安装时 build_provider 给出明确提示，
不影响插件装载与零依赖环境运行。
"""

from __future__ import annotations

import importlib.util
import re
import shutil
from functools import lru_cache
from typing import Protocol

DEFAULT_MAX_PAGES = 20
MAX_PAGES = 50
MAX_TEXT_CHARS = 20000
#: 占位引擎最多扫描的字节数（避免对超大二进制做全量解码与正则）
MAX_STUB_SCAN = 1 * 1024 * 1024
#: 单次识别最多回报的文本块数（防止超大图分配过多内存）
MAX_BLOCKS = 1000
#: 单页渲染后的像素上限（防止异常 PDF 页面撑爆内存）
MAX_PAGE_PIXELS = 40_000_000


class OcrProvider(Protocol):
    name: str

    def recognize(self, data: bytes, filename: str, mime: str) -> dict:
        """返回 {text, confidence, engine, language, blocks, pages}。"""
        ...


# ------------------------------------------------------------------ 公共工具

def _clamp_pages(value) -> int:
    try:
        pages = int(value)
    except (TypeError, ValueError):
        pages = DEFAULT_MAX_PAGES
    return max(1, min(MAX_PAGES, pages))


def _bbox(box) -> dict | None:
    """把 4 点多边形转为 {x0,y0,x1,y1}；无法解析时返回 None。"""
    try:
        xs = [float(point[0]) for point in box]
        ys = [float(point[1]) for point in box]
    except (TypeError, ValueError, IndexError):
        return None
    return {"x0": int(min(xs)), "y0": int(min(ys)),
            "x1": int(max(xs)), "y1": int(max(ys))}


def _load_pages(data: bytes, filename: str, mime: str, max_pages: int) -> list:
    """把文件字节解码为 PIL 图像页列表：图片取首帧，PDF 逐页渲染。"""
    if not data:
        raise ValueError(f"空文件无法识别: {filename}")
    ext = filename.rsplit(".", 1)[-1].lower() if "." in (filename or "") else ""
    if mime == "application/pdf" or ext == "pdf":
        return _load_pdf_pages(data, max_pages)

    try:
        from io import BytesIO

        from PIL import Image, ImageOps, UnidentifiedImageError
    except ImportError as exc:  # noqa: BLE001
        raise RuntimeError("图片 OCR 需要 Pillow（pip install Pillow）") from exc

    try:
        with Image.open(BytesIO(data)) as img:
            if getattr(img, "is_animated", False):
                img.seek(0)  # 动图只识别首帧
            oriented = ImageOps.exif_transpose(img)
            return [(oriented or img).convert("RGB")]
    except (UnidentifiedImageError, OSError) as exc:
        raise ValueError(f"不支持或已损坏的图片: {filename}") from exc


def _load_pdf_pages(data: bytes, max_pages: int) -> list:
    try:
        import pymupdf as fitz  # PyMuPDF >= 1.24 推荐入口
    except ImportError:
        try:
            import fitz  # 旧版入口
        except ImportError as exc:  # noqa: BLE001
            raise RuntimeError(
                "PDF OCR 需要 PyMuPDF（pip install pymupdf），用于逐页渲染为图像"
            ) from exc
    try:
        from PIL import Image
    except ImportError as exc:  # noqa: BLE001
        raise RuntimeError("PDF OCR 需要 Pillow（pip install Pillow）") from exc

    pages = []
    try:
        with fitz.open(stream=data, filetype="pdf") as doc:
            for index, page in enumerate(doc):
                if index >= max_pages:
                    break
                pix = page.get_pixmap(dpi=200)
                if pix.width * pix.height > MAX_PAGE_PIXELS:
                    raise ValueError(
                        f"PDF 第 {index + 1} 页像素过大（{pix.width}×{pix.height}），"
                        "请降低分辨率或分页识别")
                pages.append(
                    Image.frombytes("RGB", (pix.width, pix.height), pix.samples))
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"PDF 解析失败: {exc}") from exc
    if not pages:
        raise ValueError("PDF 没有可识别的页面")
    return pages


def _build_result(engine: str, language: str, blocks: list,
                  confs: list[tuple[float, int]], pages: int) -> dict:
    """汇总块结果：按文本长度加权平均置信度，文本总量与块数均设上限。"""
    if len(blocks) > MAX_BLOCKS:
        blocks = blocks[:MAX_BLOCKS]
        confs = confs[:MAX_BLOCKS]
    text = "\n".join(block["text"] for block in blocks)[:MAX_TEXT_CHARS]
    total = sum(length for _, length in confs)
    confidence = round(sum(conf * length for conf, length in confs) / total, 4) if total else 0.0
    return {
        "text": text,
        "confidence": confidence,
        "engine": engine,
        "language": language,
        "blocks": blocks,
        "pages": pages,
    }


def _module_available(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def _as_list(value) -> list:
    """None / numpy 数组 / 元组统一转 list（numpy 数组不能直接用 or 判断）。"""
    return [] if value is None else list(value)


# ------------------------------------------------------------------ stub

class StubOcrProvider:
    """占位 provider：从文件的可读文本段保守提取，不做真实图像识别。"""

    name = "stub"

    def __init__(self, config: dict | None = None):
        self.config = config or {}

    def recognize(self, data: bytes, filename: str, mime: str) -> dict:
        # 仅当文件内含可读文本（如文本型 PDF、带说明的图片元数据）时提取
        try:
            decoded = data[:MAX_STUB_SCAN].decode("utf-8", errors="ignore")
        except Exception:  # noqa: BLE001
            decoded = ""
        # 提取连续可打印片段（≥4 字符），过滤纯二进制噪声
        fragments = [s.strip() for s in re.findall(r"[\w\u4e00-\u9fff\s，。,.!?！？:：-]{4,}", decoded)]
        text = "\n".join(f for f in fragments if f and not f.isspace())[:MAX_TEXT_CHARS]
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
            "pages": 1,
            "notice": "占位引擎：未执行真实 OCR 识别，请安装 Tesseract 或 RapidOCR",
        }


# ------------------------------------------------------------------ RapidOCR

class RapidOcrProvider:
    """本地 PP-OCR：rapidocr + onnxruntime，离线可用，默认中英混排。"""

    name = "rapidocr"
    language = "ch"

    def __init__(self, config: dict | None = None):
        self.config = config or {}
        self.max_pages = _clamp_pages(self.config.get("max_pages"))
        self._engine = None

    def _ensure_engine(self):
        if self._engine is None:
            try:
                from rapidocr import RapidOCR
            except ImportError as exc:  # noqa: BLE001
                raise RuntimeError(
                    "rapidocr provider 需要安装 rapidocr 与 onnxruntime"
                    "（pip install rapidocr onnxruntime）"
                ) from exc
            self._engine = RapidOCR()
        return self._engine

    def probe(self) -> None:
        """健康检查：跑一次最小图像推理，确认模型与推理后端可用。"""
        import numpy as np
        from PIL import Image

        self._ensure_engine()(np.array(Image.new("RGB", (64, 64), "white")))

    def recognize(self, data: bytes, filename: str, mime: str) -> dict:
        import numpy as np

        engine = self._ensure_engine()
        pages = _load_pages(data, filename, mime, self.max_pages)
        blocks: list[dict] = []
        confs: list[tuple[float, int]] = []
        for page_no, page in enumerate(pages, 1):
            result = engine(np.array(page))
            texts = _as_list(getattr(result, "txts", None))
            scores = _as_list(getattr(result, "scores", None))
            boxes = _as_list(getattr(result, "boxes", None))
            for index, raw_text in enumerate(texts):
                text = (raw_text or "").strip()
                if not text:
                    continue
                score = scores[index] if index < len(scores) and scores[index] is not None else 0.0
                confidence = round(float(score), 4)
                block = {"text": text, "confidence": confidence, "page": page_no}
                if index < len(boxes):
                    box = _bbox(boxes[index])
                    if box:
                        block["box"] = box
                blocks.append(block)
                confs.append((confidence, len(text)))
        return _build_result(self.name, self.language, blocks, confs, len(pages))


# ------------------------------------------------------------------ Tesseract

class TesseractOcrProvider:
    """本地 Tesseract：pytesseract + 系统可执行程序，识别语言可配置。"""

    name = "tesseract"

    def __init__(self, config: dict | None = None):
        self.config = config or {}
        self.language = (self.config.get("language") or "chi_sim+eng").strip() or "chi_sim+eng"
        self.max_pages = _clamp_pages(self.config.get("max_pages"))

    def _ensure(self):
        try:
            import pytesseract
        except ImportError as exc:  # noqa: BLE001
            raise RuntimeError(
                "tesseract provider 需要安装 pytesseract 与 Tesseract 可执行程序"
                "（pip install pytesseract，并安装 tesseract-ocr 与语言包）"
            ) from exc
        try:
            pytesseract.get_tesseract_version()
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"未找到可用的 Tesseract 可执行程序: {exc}") from exc
        return pytesseract

    def probe(self) -> None:
        """健康检查：确认可执行程序与语言包可用。"""
        pytesseract = self._ensure()
        from PIL import Image

        pytesseract.image_to_string(Image.new("RGB", (32, 32), "white"), lang=self.language)

    def recognize(self, data: bytes, filename: str, mime: str) -> dict:
        pytesseract = self._ensure()
        pages = _load_pages(data, filename, mime, self.max_pages)
        blocks: list[dict] = []
        confs: list[tuple[float, int]] = []
        for page_no, page in enumerate(pages, 1):
            detail = pytesseract.image_to_data(
                page, lang=self.language, output_type=pytesseract.Output.DICT)
            groups: dict[tuple[int, int, int], list[tuple[str, float]]] = {}
            for index, word in enumerate(detail.get("text", [])):
                word = str(word).strip()
                if not word:
                    continue
                try:
                    raw_conf = float(detail["conf"][index])
                    key = (detail["block_num"][index], detail["par_num"][index],
                           detail["line_num"][index])
                except (KeyError, IndexError, TypeError, ValueError):
                    continue
                if raw_conf < 0:
                    continue
                groups.setdefault(key, []).append((word, raw_conf))
            for words in groups.values():
                # 连续中文不加空格，西文按词以空格拼接
                joiner = "" if any(re.search(r"[\u4e00-\u9fff]", w) for w, _ in words) else " "
                text = joiner.join(w for w, _ in words)
                confidence = round(sum(c for _, c in words) / len(words) / 100.0, 4)
                blocks.append({"text": text, "confidence": confidence, "page": page_no})
                confs.append((confidence, len(text)))
        return _build_result(self.name, self.language, blocks, confs, len(pages))


# ------------------------------------------------------------------ 注册表

@lru_cache(maxsize=8)
def _tesseract_ready() -> bool:
    return _module_available("pytesseract") and shutil.which("tesseract") is not None


@lru_cache(maxsize=8)
def _rapidocr_ready() -> bool:
    return _module_available("rapidocr") and _module_available("onnxruntime")


_PROVIDERS: dict[str, type] = {
    "stub": StubOcrProvider,
    "tesseract": TesseractOcrProvider,
    "rapidocr": RapidOcrProvider,
}
_READINESS = {
    "stub": lambda: True,
    "tesseract": _tesseract_ready,
    "rapidocr": _rapidocr_ready,
}
_DEPENDENCY_HINTS = {
    "tesseract": "需要安装 pytesseract 与 Tesseract 可执行程序及对应语言包",
    "rapidocr": "需要安装 rapidocr 与 onnxruntime（pip install rapidocr onnxruntime）",
}


def available_providers() -> list[str]:
    """当前环境实际可用的引擎（含 auto），用于前端/运维判断。"""
    return ["auto", *(name for name in sorted(_PROVIDERS)
                      if _READINESS.get(name, lambda: False)())]


def register_provider(name: str, cls: type) -> None:
    _PROVIDERS[name] = cls
    _READINESS.setdefault(name, lambda: True)


def resolve_provider_name(name: str | None = "auto") -> str:
    """auto 按环境能力选择（Tesseract 优先），都不可用时降级 stub。"""
    if not name or name == "auto":
        for candidate in ("tesseract", "rapidocr"):
            if _READINESS.get(candidate, lambda: False)():
                return candidate
        return "stub"
    return name


def build_provider(name: str = "auto", config: dict | None = None) -> OcrProvider:
    resolved = resolve_provider_name(name)
    cls = _PROVIDERS.get(resolved)
    if cls is None:
        raise ValueError(f"未注册的 OCR provider: {name}（可用: {available_providers()}）")
    if not _READINESS.get(resolved, lambda: True)():
        raise RuntimeError(
            f"OCR provider {resolved} 依赖未安装：{_DEPENDENCY_HINTS.get(resolved, '')}")
    return cls(config)
