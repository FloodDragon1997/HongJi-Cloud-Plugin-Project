"""文档查看动态路由（manifest.routes 声明，安装/热装载时即挂载，无需重启后端）。

端点：
- GET /api/doc_viewer/formats               支持格式与限制（JSON，供插件页展示）
- GET /api/doc_viewer/{file_id}/view        安全 HTML 预览页（ui.preview / actions 使用）
- GET /api/doc_viewer/{file_id}/text        纯文本抽取（text/plain）
- GET /api/doc_viewer/{file_id}/media?name= 文档内嵌图片（仅白名单格式）

鉴权（规范 3.5 / 9.8）：所有端点声明 SDK 导出的 CurrentUser 依赖，未登录直接 401，
与平台会话（Cookie / Bearer）保持一致。

文件字节获取（安全红线：路由模块禁止导入 app.*）：
携带当前请求的会话凭据反向调用平台自身的文件下载接口，按 FILE_API_CANDIDATES
依次尝试，404/405 时回退下一个候选。平台接口路径若不同，只需调整该常量，或用
环境变量 HJ_DOC_VIEWER_FILE_APIS（英文逗号分隔）覆盖；站点地址默认取当前请求的
base_url，也可用 HJ_DOC_VIEWER_PLATFORM_BASE 覆盖（反向代理场景）。
"""

from __future__ import annotations

import html
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

# 平台按单文件方式装载路由模块，包内模块需自行加入 sys.path
_HERE = str(Path(__file__).resolve().parent)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)


def _evict_stale_core() -> None:
    """丢弃上一版本残留的核心模块缓存，避免导入旧实现。"""
    name = "doc_viewer_core"
    cached = sys.modules.get(name)
    if cached is None:
        return
    expected = (Path(_HERE) / f"{name}.py").resolve()
    cached_file = getattr(cached, "__file__", None)
    if not cached_file or Path(cached_file).resolve() != expected:
        del sys.modules[name]


_evict_stale_core()

from fastapi import APIRouter, HTTPException, Query, Request  # noqa: E402
from fastapi.responses import HTMLResponse, Response  # noqa: E402

from app.sdk.store_plugin import CurrentUser  # noqa: E402
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
    MEDIA_MIMES,
    SUPPORTED_FORMATS,
    extract_media,
    render_document,
)

#: 平台文件下载接口候选（按顺序回退），{file_id} 为占位符
_DEFAULT_FILE_APIS = (
    "/api/files/{file_id}/download",
    "/api/files/{file_id}/content",
    "/api/files/{file_id}/raw",
)
FILE_API_CANDIDATES = tuple(
    item.strip() for item in os.environ.get("HJ_DOC_VIEWER_FILE_APIS", "").split(",")
    if item.strip()
) or _DEFAULT_FILE_APIS

#: 单次读取文档的硬上限（防止把超大文件整体拉进内存）
MAX_DOC_BYTES = 128 * 1024 * 1024
_CHUNK = 8 * 1024 * 1024
_TIMEOUT = 10

_KIND_LABELS = {
    "docx": "Word 文档",
    "xlsx": "Excel 工作簿",
    "pptx": "PowerPoint 演示文稿",
}

router = APIRouter(prefix="/api/doc_viewer", tags=["plugin-doc-viewer"])


class _SafeRedirect(urllib.request.HTTPRedirectHandler):
    """跟随重定向，但跨站时丢弃 Cookie，避免平台会话被带到第三方。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if (new is not None
                and urllib.parse.urlsplit(newurl).netloc
                != urllib.parse.urlsplit(req.full_url).netloc):
            new.remove_header("Cookie")
        return new


_OPENER = urllib.request.build_opener(_SafeRedirect)


# ---------------------------------------------------------------- 平台文件读取

def _platform_bases(request: Request) -> list[str]:
    """返回可尝试的平台站点地址，优先回环直连，其次请求来源地址。"""
    override = os.environ.get("HJ_DOC_VIEWER_PLATFORM_BASE", "").strip()
    if override:
        return [override.rstrip("/")]

    bases: list[str] = []
    server = request.scope.get("server") or ()
    if len(server) > 1 and isinstance(server[1], int) and server[1] not in (80, 443):
        bases.append(f"http://127.0.0.1:{server[1]}")

    parts = urllib.parse.urlsplit(str(request.base_url))
    proto = request.headers.get("x-forwarded-proto", "").split(",")[0].strip().lower()
    scheme = proto or parts.scheme
    host = parts.netloc
    if scheme == "http" and not host.startswith(("127.0.0.1", "localhost", "[::1]")):
        scheme = "https"
    origin = f"{scheme}://{host}"
    if origin not in bases:
        bases.append(origin)
    return bases


def _read_capped(resp) -> bytes:
    declared = resp.headers.get("Content-Length")
    if declared:
        try:
            if int(declared) > MAX_DOC_BYTES:
                raise HTTPException(413, f"文档超过上限 {MAX_DOC_BYTES} 字节，请下载后查看")
        except ValueError:
            pass
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = resp.read(_CHUNK)
        if not chunk:
            break
        total += len(chunk)
        if total > MAX_DOC_BYTES:
            raise HTTPException(413, f"文档超过上限 {MAX_DOC_BYTES} 字节，请下载后查看")
        chunks.append(chunk)
    return b"".join(chunks)


def _credential_headers(request: Request) -> dict:
    headers = {"Accept": "*/*"}
    cookie = request.headers.get("cookie")
    if cookie:
        headers["Cookie"] = cookie
    authorization = request.headers.get("authorization")
    if authorization:
        headers["Authorization"] = authorization
    return headers


def _fetch_document(request: Request, file_id: int) -> bytes:
    headers = _credential_headers(request)
    if "Cookie" not in headers and "Authorization" not in headers:
        raise HTTPException(401, "缺少平台会话凭据，无法读取文件")
    failures: list[str] = []
    for base in _platform_bases(request):
        for template in FILE_API_CANDIDATES:
            url = f"{base}{template.format(file_id=file_id)}"
            try:
                req = urllib.request.Request(url, headers=headers)
                with _OPENER.open(req, timeout=_TIMEOUT) as resp:
                    return _read_capped(resp)
            except urllib.error.HTTPError as exc:
                if exc.code in (404, 405):
                    failures.append(f"{url} → {exc.code}")
                    continue
                if 300 <= exc.code < 400:
                    raise HTTPException(502, "平台文件接口重定向次数超限") from exc
                status = 401 if exc.code in (401, 403) else 502
                raise HTTPException(status, f"平台文件接口返回 {exc.code}") from exc
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                failures.append(f"{url} → {exc}")
                break
    raise HTTPException(502, "无法读取文件内容：" + "；".join(failures))


# ---------------------------------------------------------------- 端点

@router.get("/formats")
def formats_endpoint(user: CurrentUser) -> dict:
    """支持格式与限制，供插件页功能入口展示。"""
    return {
        "plugin": "doc_viewer",
        "formats": list(SUPPORTED_FORMATS),
        "legacy_formats": sorted(LEGACY_FORMATS),
        "limits": {
            "max_decompressed_mb": DEFAULT_MAX_DECOMPRESSED // (1024 * 1024),
            "max_decompressed_limit_mb": MAX_DECOMPRESSED_LIMIT // (1024 * 1024),
            "max_table_rows": DEFAULT_MAX_TABLE_ROWS,
            "max_table_rows_limit": MAX_TABLE_ROWS_LIMIT,
            "max_table_cols": DEFAULT_MAX_TABLE_COLS,
            "max_table_cols_limit": MAX_TABLE_COLS_LIMIT,
            "max_slides": DEFAULT_MAX_SLIDES,
            "max_slides_limit": MAX_SLIDES_LIMIT,
        },
        "endpoints": {
            "view": "/api/doc_viewer/{file_id}/view?name={file_name}",
            "text": "/api/doc_viewer/{file_id}/text?name={file_name}",
            "media": "/api/doc_viewer/{file_id}/media?name={media_name}",
        },
    }


@router.get("/{file_id}/view", response_class=HTMLResponse)
def view_endpoint(
    file_id: int,
    request: Request,
    user: CurrentUser,
    name: str | None = Query(default=None, max_length=512, description="文件名（含扩展名）"),
) -> HTMLResponse:
    """安全 HTML 预览页：正文由核心转义生成，CSP 禁止脚本执行。"""
    filename = (name or "").strip()
    data = _fetch_document(request, file_id)
    try:
        result = render_document(
            data, filename,
            media_prefix=f"/api/doc_viewer/{file_id}/media?name=",
        )
    except ValueError as exc:
        return HTMLResponse(_render_error_page(str(exc), filename), status_code=422,
                            headers=_PAGE_HEADERS)
    except Exception as exc:  # noqa: BLE001  损坏文档不应把 500 堆栈暴露给用户
        return HTMLResponse(_render_error_page(f"文档解析失败：{exc}", filename),
                            status_code=422, headers=_PAGE_HEADERS)

    return HTMLResponse(
        _render_page(result, filename),
        headers=_PAGE_HEADERS,
    )


@router.get("/{file_id}/text")
def text_endpoint(
    file_id: int,
    request: Request,
    user: CurrentUser,
    name: str | None = Query(default=None, max_length=512, description="文件名（含扩展名）"),
) -> Response:
    """纯文本抽取：文本类统一 text/plain，避免任何 HTML 被内联执行。"""
    data = _fetch_document(request, file_id)
    try:
        result = render_document(data, (name or "").strip())
    except ValueError as exc:
        raise HTTPException(422, detail=str(exc)) from exc
    if result["kind"] not in SUPPORTED_FORMATS:
        raise HTTPException(422, detail="该格式暂不支持在线查看，请下载后打开")

    download_name = urllib.parse.quote((name or f"document.{result['kind']}").rsplit("/", 1)[-1])
    return Response(
        content=result["text"].encode("utf-8"),
        media_type="text/plain; charset=utf-8",
        headers={
            "X-Content-Type-Options": "nosniff",
            "Content-Disposition": f"inline; filename*=UTF-8''{download_name}",
            "Cache-Control": "private, max-age=60",
        },
    )


@router.get("/{file_id}/media")
def media_endpoint(
    file_id: int,
    request: Request,
    user: CurrentUser,
    name: str = Query(..., min_length=1, max_length=512, description="文档内图片路径"),
) -> Response:
    """文档内嵌图片：只允许包内 media 目录与白名单图片扩展名。"""
    data = _fetch_document(request, file_id)
    try:
        content, media_type = extract_media(data, name)
    except ValueError as exc:
        raise HTTPException(404, detail=str(exc)) from exc

    filename = urllib.parse.quote(name.rsplit("/", 1)[-1])
    return Response(
        content=content,
        media_type=media_type,
        headers={
            "X-Content-Type-Options": "nosniff",
            "Content-Disposition": f"inline; filename*=UTF-8''{filename}",
            "Cache-Control": "private, max-age=600",
        },
    )


# ---------------------------------------------------------------- 页面渲染

_PAGE_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'none'; img-src 'self' data:; style-src 'unsafe-inline'; "
        "base-uri 'none'; form-action 'none'; frame-ancestors 'self'"
    ),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}

_STYLE = """
:root{color-scheme:light}
*{box-sizing:border-box}
body{margin:0;background:#f6f7f9;color:#1f2328;
font:15px/1.75 -apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif}
.shell{max-width:920px;margin:0 auto;padding:24px 20px 64px}
.top{display:flex;flex-wrap:wrap;align-items:center;gap:10px;margin-bottom:14px}
.top h1{margin:0;font-size:16px;font-weight:600;word-break:break-all}
.badge{display:inline-block;padding:1px 8px;border-radius:10px;background:#e8eef7;
color:#3b5b8c;font-size:12px;line-height:20px}
.doc{background:#fff;border:1px solid #e3e6ea;border-radius:12px;
padding:34px 42px;box-shadow:0 1px 2px rgba(15,23,42,.04)}
.doc h1,.doc h2,.doc h3,.doc h4,.doc h5,.doc h6{
margin:1.1em 0 .45em;line-height:1.35;font-weight:600;color:#111827}
.doc h1{font-size:26px}.doc h2{font-size:21px}.doc h3{font-size:18px}
.doc h4{font-size:16px}.doc h5,.doc h6{font-size:15px;color:#374151}
.doc h1:first-child,.doc h2:first-child,.doc h3:first-child{margin-top:0}
.doc p{margin:0 0 .7em;white-space:pre-wrap;overflow-wrap:anywhere}
.doc p.li{display:flex;gap:.45em}
.doc .marker{flex:0 0 auto;color:#57606a}
.doc a{color:#0969da;text-decoration:none}
.doc a:hover{text-decoration:underline}
.doc img.doc-img{display:block;max-width:100%;height:auto;margin:12px auto;border-radius:4px}
.doc table.doc-table{width:100%;border-collapse:collapse;margin:1em 0;font-size:14px}
.doc table.doc-table th,.doc table.doc-table td{
border:1px solid #d8dee4;padding:6px 10px;vertical-align:top;text-align:left}
.doc table.doc-table th{background:#f3f5f7;font-weight:600}
.sheet{margin-bottom:36px}
.sheet:last-child{margin-bottom:0}
.sheet h2{margin:0 0 12px;font-size:16px;color:#1f2328}
.sheet-table{width:100%;border-collapse:collapse;font-size:13px}
.sheet-table td{border:1px solid #e3e6ea;padding:5px 9px;vertical-align:top;
max-width:420px;overflow-wrap:anywhere;white-space:pre-wrap}
.truncate{margin:0 0 10px;padding:6px 10px;border-radius:7px;background:#fff8e6;
color:#8a6100;font-size:12px}
.slide{background:#fbfcfd;border:1px solid #e3e6ea;border-radius:10px;
padding:22px 26px;margin-bottom:18px}
.slide:last-child{margin-bottom:0}
.slide-no{color:#8a94a0;font-size:12px;margin-bottom:8px;letter-spacing:.04em}
.slide h3{margin:0 0 .5em;font-size:19px;color:#111827}
.slide p.pp{margin:0 0 .45em}
.notice{background:#fff;border:1px dashed #cbd5e1;border-radius:12px;padding:40px 28px;text-align:center}
.notice h2{margin:0 0 10px;font-size:18px}
.notice p{margin:0 0 8px;color:#4b5563}
.notice .hint{color:#8a94a0;font-size:13px}
.notice-inline{margin:1em 0;color:#6b7280;text-align:center}
.warnings{margin:0 0 14px;padding:10px 14px;border-radius:8px;background:#fff8e6;
color:#8a6100;font-size:13px}
.warnings ul{margin:0;padding-left:18px}
.foot{margin-top:14px;color:#9aa4af;font-size:12px;text-align:center}
.error h2{color:#b42318}
@media (max-width:640px){
.shell{padding:20px 16px 48px}
.doc{padding:20px 16px;border-radius:10px}
}
@media print{
body{background:#fff}
.shell{max-width:none;padding:0}
.doc{border:none;box-shadow:none;padding:0}
.top,.foot{display:none}
}
"""


def _render_page(result: dict, filename: str) -> str:
    title = html.escape(result["title"] or filename or "文档预览")
    kind = result["kind"]
    label = _KIND_LABELS.get(kind, "文档")
    warnings_html = ""
    if result.get("warnings"):
        items = "".join(f"<li>{html.escape(item)}</li>" for item in result["warnings"])
        warnings_html = f'<div class="warnings"><ul>{items}</ul></div>'

    return (
        '<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>{title} · 文档预览</title><style>{_STYLE}</style></head><body>"
        '<div class="shell">'
        '<div class="top">'
        f"<h1>{title}</h1><span class=\"badge\">{html.escape(label)}</span>"
        "</div>"
        f"{warnings_html}"
        f'<article class="doc">{result["html"]}</article>'
        '<p class="foot">由文档查看插件渲染 · 如需完整排版请下载原文件</p>'
        "</div></body></html>"
    )


def _render_error_page(message: str, filename: str) -> str:
    title = html.escape(filename or "文档预览")
    return (
        '<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>{title} · 无法预览</title><style>{_STYLE}</style></head><body>"
        '<div class="shell"><div class="top"><h1>' + title + "</h1></div>"
        '<div class="notice error"><h2>无法在线预览</h2>'
        f"<p>{html.escape(message)}</p>"
        '<p class="hint">请下载原文件后使用本地应用打开。</p>'
        "</div></div></body></html>"
    )
