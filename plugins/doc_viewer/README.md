# 文档查看插件

在线查看 DOCX（Word）、XLSX（Excel）、PPTX（PowerPoint）文档：解码 Office Open XML
并渲染为安全的 HTML，内嵌图片与纯文本抽取一并支持；旧版二进制 Office 格式给出下载引导。
纯标准库实现，无第三方依赖，不解析宏、不执行文档中的任何脚本。

## 支持格式

| 格式 | 能力 |
| --- | --- |
| `docx` | 标题层级、段落与对齐、粗体/斜体/下划线/删除线/字号/颜色、超链接、编号与项目符号、表格（跨列）、内嵌图片 |
| `xlsx` | 工作表、共享字符串、单元格样式（粗体/颜色/对齐/百分比/日期）、合并单元格、行列上限截断 |
| `pptx` | 按放映顺序输出每页文本与缩进层级 |
| `doc` / `xls` / `ppt` / `wps` / `rtf` / `odt` / `ods` / `odp` / `ofd` 等 | 识别格式并给出下载引导 |

## 端点

所有端点均声明 SDK 导出的 `CurrentUser` 鉴权依赖，未登录返回 401。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/doc_viewer/formats` | 支持格式与限制（JSON） |
| GET | `/api/doc_viewer/{file_id}/view?name=文件名` | 安全 HTML 预览页（`ui.preview` / `ui.actions` 使用） |
| GET | `/api/doc_viewer/{file_id}/text?name=文件名` | 纯文本抽取，`text/plain; charset=utf-8` |
| GET | `/api/doc_viewer/{file_id}/media?name=包内图片路径` | 文档内嵌图片（仅 png/jpg/jpeg/gif/bmp/webp） |

右键菜单的「查看文档」通过 `ui.actions.extensions`（`docx`/`xlsx`/`pptx`）限定，
仅在对应后缀的文件上展示，目录与其他类型文件不显示。

`view` 端点通过当前请求的会话凭据（Cookie / Bearer）反向调用平台文件下载接口获取
文件字节，候选路径为 `/api/files/{file_id}/download`、`/content`、`/raw`，404/405
时依次回退。路径不同时可用环境变量覆盖：

```text
HJ_DOC_VIEWER_FILE_APIS=/api/files/{file_id}/download,...
HJ_DOC_VIEWER_PLATFORM_BASE=https://your-platform.example
```

## 安全边界

- 所有文本统一 `html.escape`，预览页 CSP 为 `default-src 'none'`（仅允许内联样式与同源图片）；
- 超链接仅放行 `http/https/mailto`，并带 `rel="noopener noreferrer"`；
- 图片仅允许包内 `word/media`、`xl/media`、`ppt/media` 目录且扩展名在白名单内，SVG 明确拒绝；
- 拒绝 DTD/实体声明，防实体扩展攻击；拒绝加密文档；
- zip 解压总量、单条目、行列数、幻灯片数均有上限，防压缩炸弹与内存放大。

## 配置项

| key | 默认 | 说明 |
| --- | --- | --- |
| `max_decompressed_mb` | 64 | 文档解压总量上限（MB），超出提示下载查看 |
| `max_table_rows` | 500 | Excel 单表展示行数上限 |
| `max_table_cols` | 60 | Excel 单表展示列数上限 |
| `max_slides` | 100 | PPTX 展示页数上限 |

## 打包与安装

```powershell
Remove-Item -Recurse -Force .\plugins\doc_viewer\__pycache__ -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force .\dist | Out-Null
Compress-Archive -Path .\plugins\doc_viewer\* -DestinationPath .\dist\doc_viewer-1.0.0.zip -Force
```

安装后需「启用」并通过健康检查（自检会构造最小 docx/xlsx/pptx 内存样例）。
