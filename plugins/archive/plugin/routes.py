"""压缩包预览动态路由（manifest.routes 声明，安装/热装载时即挂载，无需重启后端）。

端点（由 ui.preview / ui.actions 引用）：
- GET /api/archive/{file_id}/entries          条目元数据 JSON（含预览口径）
- GET /api/archive/{file_id}/entry?name=...   单条目内容，文本类强制 text/plain
- GET /api/archive/{file_id}/view             无脚本目录页，供 iframe 预览

文件字节获取（安全红线：路由模块禁止导入 app.*）：
带平台会话 Cookie 反向调用平台自身的文件下载接口，按 FILE_API_CANDIDATES 依次尝试，
404/405 时回退下一个候选。平台接口路径若不同，只需调整该常量，或用环境变量
HJ_ARCHIVE_FILE_APIS（英文逗号分隔）覆盖；站点地址默认取当前请求的 base_url，
也可用 HJ_ARCHIVE_PLATFORM_BASE 覆盖（反向代理场景）。
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

from fastapi import APIRouter, HTTPException, Query, Request  # noqa: E402
from fastapi.responses import HTMLResponse, Response  # noqa: E402

from archive_core import (  # noqa: E402
    MAX_ENTRIES_LIMIT,
    list_entries,
    read_entry,
)

#: 平台文件下载接口候选（按顺序回退），{file_id} 为占位符
_DEFAULT_FILE_APIS = (
    "/api/files/{file_id}/download",
    "/api/files/{file_id}/content",
    "/api/files/{file_id}/raw",
)
FILE_API_CANDIDATES = tuple(
    item.strip() for item in os.environ.get("HJ_ARCHIVE_FILE_APIS", "").split(",")
    if item.strip()
) or _DEFAULT_FILE_APIS

#: 单次读取压缩包的硬上限（防止把超大文件整体拉进内存）
MAX_ARCHIVE_BYTES = 256 * 1024 * 1024
_CHUNK = 8 * 1024 * 1024
_TIMEOUT = 20

router = APIRouter(prefix="/api/archive", tags=["plugin-archive"])


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """禁止跟随重定向，避免携带平台会话 Cookie 被转发到其他站点。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


# ---------------------------------------------------------------- 平台文件读取

def _platform_base(request: Request) -> str:
    override = os.environ.get("HJ_ARCHIVE_PLATFORM_BASE", "").strip()
    return (override or str(request.base_url)).rstrip("/")


def _read_capped(resp) -> bytes:
    declared = resp.headers.get("Content-Length")
    if declared:
        try:
            if int(declared) > MAX_ARCHIVE_BYTES:
                raise HTTPException(413, f"压缩包超过上限 {MAX_ARCHIVE_BYTES} 字节，请下载后查看")
        except ValueError:
            pass
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = resp.read(_CHUNK)
        if not chunk:
            break
        total += len(chunk)
        if total > MAX_ARCHIVE_BYTES:
            raise HTTPException(413, f"压缩包超过上限 {MAX_ARCHIVE_BYTES} 字节，请下载后查看")
        chunks.append(chunk)
    return b"".join(chunks)


def _fetch_archive(request: Request, file_id: int) -> bytes:
    cookie = request.headers.get("cookie")
    if not cookie:
        raise HTTPException(401, "缺少平台会话 Cookie，无法读取文件")
    base = _platform_base(request)
    failures: list[str] = []
    for template in FILE_API_CANDIDATES:
        url = f"{base}{template.format(file_id=file_id)}"
        try:
            req = urllib.request.Request(url, headers={"Cookie": cookie, "Accept": "*/*"})
            with _OPENER.open(req, timeout=_TIMEOUT) as resp:
                return _read_capped(resp)
        except urllib.error.HTTPError as exc:
            if exc.code in (404, 405):
                failures.append(f"{url} → {exc.code}")
                continue
            if 300 <= exc.code < 400:
                raise HTTPException(502, "平台文件接口返回重定向，已拒绝跟随") from exc
            status = 401 if exc.code in (401, 403) else 502
            raise HTTPException(status, f"平台文件接口返回 {exc.code}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            failures.append(f"{url} → {exc}")
            continue
    raise HTTPException(502, "无法读取文件内容：" + "；".join(failures))


# ---------------------------------------------------------------- 端点

@router.get("/{file_id}/entries")
def list_entries_endpoint(
    file_id: int,
    request: Request,
    limit: int | None = Query(default=None, ge=1, le=MAX_ENTRIES_LIMIT),
) -> dict:
    """条目元数据列表，供前端渲染压缩包目录树。"""
    data = _fetch_archive(request, file_id)
    try:
        return list_entries(data, str(file_id), max_entries=limit)
    except ValueError as exc:
        raise HTTPException(422, detail=str(exc)) from exc


@router.get("/{file_id}/entry")
def read_entry_endpoint(
    file_id: int,
    request: Request,
    name: str = Query(..., min_length=1, max_length=4096, description="压缩包内条目路径"),
) -> Response:
    """单个条目内容：可预览类型 inline，其余按附件返回；文本类强制 text/plain。"""
    data = _fetch_archive(request, file_id)
    try:
        item = read_entry(data, str(file_id), name)
    except ValueError as exc:
        raise HTTPException(404, detail=str(exc)) from exc

    media = "text/plain; charset=utf-8" if item["preview_type"] == "text" else item["mime"]
    disposition = "inline" if item["previewable"] else "attachment"
    filename = urllib.parse.quote(name.rsplit("/", 1)[-1])
    return Response(
        content=item["data"],
        media_type=media,
        headers={
            "X-Content-Type-Options": "nosniff",
            "Content-Disposition": f"{disposition}; filename*=UTF-8''{filename}",
            "Content-Length": str(len(item["data"])),
        },
    )


@router.get("/{file_id}/view", response_class=HTMLResponse)
def view_endpoint(file_id: int, request: Request) -> HTMLResponse:
    """无脚本目录页：ui.preview 把 preview_type=archive 映射到 iframe 后由它渲染。"""
    data = _fetch_archive(request, file_id)
    try:
        listing = list_entries(data, str(file_id))
    except ValueError as exc:
        raise HTTPException(422, detail=str(exc)) from exc
    return HTMLResponse(
        _render_page(file_id, listing),
        headers={
            "Content-Security-Policy":
                "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'",
            "X-Content-Type-Options": "nosniff",
        },
    )


# ---------------------------------------------------------------- 页面渲染

_STYLE = """
body{margin:0;padding:16px;font:14px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI",
"PingFang SC","Microsoft YaHei",sans-serif;color:#1f2328;background:#fff}
h1{margin:0 0 4px;font-size:16px}
.meta{margin:0 0 12px;color:#656d76;font-size:12px}
table{width:100%;border-collapse:collapse}
th,td{text-align:left;padding:6px 8px;border-bottom:1px solid #eaeef2;font-size:13px}
th{color:#656d76;font-weight:500}
td.name{word-break:break-all}
.badge{display:inline-block;padding:0 6px;border-radius:9px;background:#eff2f5;
color:#57606a;font-size:11px;line-height:18px;margin-right:6px}
.dir{color:#57606a}
a{color:#0969da;text-decoration:none}
a:hover{text-decoration:underline}
.empty{padding:24px;text-align:center;color:#656d76}
"""


def _human_size(size: int | None) -> str:
    if not size:
        return "0 B"
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{int(value)} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{int(size)} B"


def _render_page(file_id: int, listing: dict) -> str:
    rows: list[str] = []
    for entry in listing["entries"]:
        name = html.escape(entry["name"])
        size = "" if entry["is_dir"] else _human_size(entry["size"])
        if entry["is_dir"]:
            label = f'<span class="dir">{name}/</span>'
        elif entry["safe"]:
            href = (f"/api/archive/{file_id}/entry?name="
                    + urllib.parse.quote(entry["name"]))
            label = f'<a href="{html.escape(href, quote=True)}" target="_blank">{name}</a>'
        else:
            label = name
        rows.append(
            f'<tr><td class="name">{label}</td>'
            f'<td><span class="badge">{html.escape(entry["preview_type"])}</span></td>'
            f'<td>{size}</td></tr>'
        )
    if not rows:
        rows.append('<tr><td class="empty" colspan="3">压缩包为空</td></tr>')

    truncated = ('<p class="meta">条目数超过上限，仅显示前 '
                 f'{listing["count"]} 项。</p>') if listing.get("truncated") else ""
    return (
        "<!DOCTYPE html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
        f"<title>压缩包预览</title><style>{_STYLE}</style></head><body>"
        f"<h1>压缩包预览</h1>"
        f'<p class="meta">{html.escape(listing["format"])} · {listing["count"]} 项 · '
        f'{_human_size(listing["declared_bytes"])}（解压后）</p>'
        f"{truncated}"
        "<table><thead><tr><th>名称</th><th>类型</th><th>大小</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table></body></html>"
    )
