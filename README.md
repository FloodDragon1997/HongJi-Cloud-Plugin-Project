# 鸿迹云插件仓库

本仓库用于开发和维护鸿迹云盘（HongJi Cloud）平台的可插拔存储插件与能力插件，
产出平台可直接上传安装的 zip 插件包。

平台源码未随本仓库公开。下文出现的 `backend/...` 路径指配套平台项目中的位置，
仅用于说明契约来源：

- 契约基类：`backend/app/plugin_framework/interface.py`
- 对外 SDK：`backend/app/sdk/store_plugin`（插件唯一允许依赖的平台模块）
- 安装与装载：`backend/app/plugin_framework/manager/install.py`、`runtime.py`

## 目录规范

```text
HongJi-Cloud-Plugin-Project/
├── plugins/                    # 插件源码，每个插件一个目录，目录名必须等于 manifest.id
│   └── <plugin_id>/
│       ├── manifest.json       # 插件清单（压缩包根目录，必需）
│       ├── plugin/
│       │   ├── main.py         # 入口模块（manifest.entry 指向）
│       │   └── ...             # 可拆分的实现模块
│       └── README.md           # 可选：插件说明、配置项、依赖
├── templates/
│   ├── storage/                # 存储插件模板（type=storage）
│   └── capability/             # 能力插件模板（type=capability）
├── scripts/
│   └── validate_plugin.py      # 静态校验（零依赖，可在提交前/CI 中执行）
└── README.md
```

命名约定：

| 项目 | 规则 |
| --- | --- |
| 插件目录 | 等于 `manifest.id`，例如 `plugins/s3_compatible/` |
| `manifest.id` | 小写字母开头，仅 `a-z0-9_`，长度 2–64 |
| `manifest.version` | SemVer，例如 `1.0.0` |
| 入口 | 默认 `plugin/main.py`，相对包根路径，禁止 `..` 与绝对路径 |
| 权限 | 只能取 `fs.read`、`fs.write`、`fs.list`、`net.outbound`、`kv.meta` |

## 插件包结构

上传到平台的 zip 包，`manifest.json` 必须位于 zip 根目录（不要把父目录打进去）：

```text
my_storage-1.0.0.zip
├── manifest.json
└── plugin/
    └── main.py
```

### 包内多文件

平台按单文件方式装载 `entry`（`spec_from_file_location`），不会自动把插件目录加入 `sys.path`。
若实现拆分为多个文件（如 `ftp`/`webdav` 的公共基类与传输层、`ocr`/`thumbnail` 的 provider），
必须在 `entry` 顶部自行把所在目录加入 `sys.path`：

```python
import sys
from pathlib import Path

_HERE = str(Path(__file__).resolve().parent)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from my_helpers import helper  # noqa: E402
```

包内模块名建议带插件前缀（如 `ocr_providers.py`），避免多个插件在同进程装载时模块名冲突。

## 已收录插件

| plugin_id | 类型 | 说明 | 运行依赖 |
| --- | --- | --- | --- |
| `local_disk` | storage | 本地磁盘存储 | 无 |
| `s3` | storage | S3 / MinIO 等 S3 兼容存储 | `boto3`（平台已内置） |
| `webdav` | storage | WebDAV 存储 | 无（标准库） |
| `ftp` | storage | FTP 存储 | 无（标准库） |
| `ocr` | capability | OCR 文字识别（`auto` 优先 Tesseract，其次 RapidOCR/PP-OCR，缺失时降级占位） | 可选 pytesseract+Tesseract / rapidocr+onnxruntime / PyMuPDF（PDF） |
| `thumbnail` | capability | 缩略图生成（`auto` 优先 Pillow 真实缩放，未安装自动降级占位） | 可选 Pillow |
| `preview` | capability | 在线预览类型判定与内容协商 | 无 |
| `archive` | capability | 压缩包条目浏览、iframe 预览页与 zip 打包（含 `routes` 端点，防 Zip Slip 与压缩炸弹） | 无（标准库） |

> 这些插件与平台内置插件同 `id`。平台启动时会把内置插件同步进插件表，因此直接上传同 `id`
> 的 zip 会因重复安装被拒绝（45002）。本仓库用于统一维护这些插件的源码；需要在平台外置
> 部署或与内置版本并存时，应调整 `id` 后再打包安装。

## 快速开始

1. 复制模板：

   ```powershell
   Copy-Item -Recurse .\templates\storage .\plugins\my_storage
   ```

2. 修改 `plugins/my_storage/manifest.json` 的 `id`、`name`、`version`、`permissions`；
   同步修改 `plugin/main.py` 中插件类的 `plugin_id`（必须与 `manifest.id` 一致）与
   `required_permissions`（必须与 `manifest.permissions` 一致）。

3. 按真实后端实现契约方法，删除模板中不需要的示例逻辑。

4. 静态校验：

   ```powershell
   python .\scripts\validate_plugin.py
   ```

5. 打包：

   ```powershell
   Remove-Item -Recurse -Force .\plugins\my_storage\__pycache__ -ErrorAction SilentlyContinue
   New-Item -ItemType Directory -Force .\dist | Out-Null
   Compress-Archive -Path .\plugins\my_storage\* -DestinationPath .\dist\my_storage-1.0.0.zip -Force
   ```

6. 安装：主平台「系统管理 → 插件管理」上传 zip（`POST /api/plugins`），安装后状态为
   `installed`，需「启用」（通过健康检查后才变为 `enabled`）。代码热装载，无需重启后端。

## manifest 字段

```json
{
  "id": "my_storage",
  "name": "我的存储后端",
  "version": "1.0.0",
  "type": "storage",
  "entry": "plugin/main.py",
  "permissions": ["fs.read", "fs.write", "fs.list"],
  "sdk_version": "1.0",
  "config_schema": [
    {"key": "root", "label": "挂载根目录", "type": "text", "placeholder": "./data_store", "full": true}
  ]
}
```

| 字段 | 必需 | 说明 |
| --- | --- | --- |
| `id` | 是 | 插件唯一标识，注册表主键，也是 zip 内插件类的 `plugin_id` |
| `name` | 是 | 中文展示名 |
| `version` | 是 | SemVer；同一 `id` 已安装时不能重复安装 |
| `type` | 是 | `storage`（存储源）或 `capability`（能力扩展） |
| `entry` | 是 | 入口文件相对路径，装载时在此模块内查找匹配的插件类 |
| `permissions` | 是 | 平台权限白名单子集，安装期校验，超出直接拒绝（45003） |
| `sdk_version` | 是 | 契约版本，当前平台只接受 `1.x` |
| `config_schema` | 否 | 配置表单描述，随插件元数据下发给前端渲染（见下节） |

`config_schema` 每项字段：`key`（配置键）、`label`（中文标签）、`type`
（`text` / `password` / `number` / `checkbox` / `select` / `textarea`）、
可选 `placeholder`、`hint`、`required`、`full`（独占整行）、`options`（select 选项）。

## 存储插件契约（type=storage）

入口模块中定义 `plugin_id` 等于 `manifest.id`、且继承 `StoragePlugin` 的类，平台装载时自动发现并注册：

```python
from app.sdk.store_plugin import StoragePlugin


class MyStoragePlugin(StoragePlugin):
    plugin_id = "my_storage"
    display_name = "我的存储后端"
    required_permissions = ("fs.read", "fs.write", "fs.list")
    config_schema = [...]
```

必须实现的方法：

| 方法 | 约定 |
| --- | --- |
| `capabilities() -> dict` | 能力声明：`stream`、`multipart`、`max_object_bytes`、`server_side_copy` 等 |
| `health_check() -> HealthStatus` | 可重复调用、无副作用；`writable=False` 表示只读（平台判定「暂不可写」） |
| `put(key, data) -> PutResult` | 小对象写入（≤8MB），返回 `size` 与实际内容 MD5 的 `etag` |
| `put_stream(key, fileobj) -> PutResult` | 流式写入，边读边写边算 MD5，内存峰值不超过单块 |
| `get(key) -> bytes` | 完整读取；不存在抛 `PluginObjectNotFound` |
| `open_read(key) -> Iterator[bytes]` | 流式读取，单块 ≤8MB |
| `stat(key) -> dict` | 返回 `{"size": int, "etag": str}`；不存在抛 `PluginObjectNotFound` |
| `exists(key) -> bool` | 对象是否存在 |
| `delete(key) -> None` | 删除，对象不存在时静默幂等 |
| `copy(src_key, dst_key) -> PutResult` | 服务端复制（秒传复用、版本快照），不支持时可通过流式读写实现 |

可选覆写 `stat_size(key) -> int`：下载、预览、对账等只需字节数的路径会调用它，
能廉价取大小（如 `os.stat`）的后端应覆写，避免为了 etag 全量读文件。

实现约束：

- 对象键由平台生成后传入，插件仍须自行校验，禁止拼接用户输入造成目录穿越；
- 大对象必须流式处理，单次内存驻留上限 8MB；
- 后端不可用/配额不足/只读时抛 `PluginUnavailable`，平台映射为 45001（存储源暂不可用）；
- 只允许 `import app.sdk.store_plugin`，禁止导入 `app.*` 其他内部模块或平台内置插件。

## 能力插件契约（type=capability）

能力插件是普通类（不继承 `StoragePlugin`），通过同一注册表管理，提供
`plugin_id`、`display_name`、`required_permissions`、`capabilities()`、`health_check()`
以及平台业务约定的自定义方法（例如 OCR 的 `recognize`）。业务侧以
`registry.create(plugin_id, config)` 获取实例，调用受生命周期守卫与权限校验约束。

能力插件与平台业务接口一一对应，开发前请先与平台确认调用协议。

## 校验脚本

`scripts/validate_plugin.py` 为纯标准库实现的静态校验，不依赖平台代码，检查项：

- `manifest.json` 存在、为 JSON 对象、必填字段齐全、字段取值合法；
- 权限均在白名单内，`sdk_version` 为 1.x，`entry` 合法且文件存在；
- 入口模块中存在 `plugin_id` 与 manifest 一致的插件类；
- storage 插件继承 `StoragePlugin` 并实现全部抽象方法，`required_permissions` 与 manifest 一致；
- 包内 Python 文件未导入 `app.*` 内部模块或平台内置 `plugins.*`；
- 包内文件数、总大小符合平台安装限制。

用法：

```powershell
python .\scripts\validate_plugin.py                      # 校验 plugins/ 下全部插件
python .\scripts\validate_plugin.py plugins/my_storage   # 校验指定插件
python .\scripts\validate_plugin.py --all                # 连同 templates/ 一起校验
```

校验通过退出码为 0，失败为 1，适合直接接入 CI。

## 平台安装限制

| 限制 | 取值 |
| --- | --- |
| 上传 zip 大小 | ≤ 20MB |
| 解压后总大小 | ≤ 100MB |
| 包内条目数 | ≤ 512 |
| `manifest.json` 解压后大小 | ≤ 1MB |
| zip 内路径 | 禁止绝对路径、盘符与 `..` 穿越 |

安装期校验失败返回 40001（清单非法）或 45003（权限超出白名单）；启用时健康检查失败
返回 45004，连续失败达到阈值后平台自动禁用插件。

## 许可证

[MIT](LICENSE)
