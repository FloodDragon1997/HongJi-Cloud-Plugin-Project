#!/usr/bin/env python3
"""插件包静态校验（零依赖，不导入平台代码）。

校验规则与平台安装期保持一致，并补充仓库目录规范：

- manifest.json 存在、为 JSON 对象、必填字段齐全、取值合法；
- permissions ⊆ 平台白名单，sdk_version 为 1.x，entry 为包内相对路径且文件存在；
- 目录名与 manifest.id 一致（templates/ 下的模板除外）；
- 入口模块顶层存在 plugin_id 与 manifest.id 一致的插件类，且唯一；
- storage 插件继承 StoragePlugin 并实现全部抽象方法；
- 类声明的 required_permissions 与 manifest.permissions 一致；
- 包内 Python 文件只允许导入 app.sdk.store_plugin，禁止其他 app.* 与 plugins.*；
- 文件数与解压总大小不超过平台安装限制。

用法：
    python scripts/validate_plugin.py                     # 校验 plugins/ 下全部插件
    python scripts/validate_plugin.py plugins/my_storage  # 校验指定插件
    python scripts/validate_plugin.py --all               # 连同 templates/ 一起校验

退出码：0 全部通过，1 存在错误。
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from pathlib import Path

# --- 平台约束（与 backend/app/plugin_framework 对齐） ---
PERMISSION_WHITELIST = frozenset({"fs.read", "fs.write", "fs.list", "net.outbound", "kv.meta"})
PLUGIN_TYPES = frozenset({"storage", "capability"})
SDK_MAJOR = "1"

REQUIRED_MANIFEST_KEYS = ("id", "name", "version", "type", "entry", "permissions", "sdk_version")
REQUIRED_STORAGE_METHODS = frozenset({
    "capabilities", "health_check", "put", "put_stream", "get",
    "open_read", "stat", "exists", "delete", "copy",
})
REQUIRED_CAPABILITY_METHODS = frozenset({"capabilities", "health_check"})

CONFIG_FIELD_TYPES = frozenset({"text", "password", "number", "checkbox", "select", "textarea"})

MAX_PACKAGE_FILES = 512
MAX_TOTAL_BYTES = 100 * 1024 * 1024
MAX_MANIFEST_BYTES = 1024 * 1024

ID_RE = re.compile(r"^[a-z][a-z0-9_]{1,63}$")
VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")

REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGINS_DIR = REPO_ROOT / "plugins"
TEMPLATES_DIR = REPO_ROOT / "templates"


# ---------------------------------------------------------------- manifest

def _load_manifest(plugin_dir: Path, errors: list[str]) -> dict | None:
    path = plugin_dir / "manifest.json"
    if not path.is_file():
        errors.append("缺少 manifest.json（必须位于插件包根目录）")
        return None
    if path.stat().st_size > MAX_MANIFEST_BYTES:
        errors.append("manifest.json 解压后超过 1MB 上限")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        errors.append(f"manifest.json 解析失败: {exc}")
        return None
    if not isinstance(data, dict):
        errors.append("manifest.json 必须是 JSON 对象")
        return None
    return data


def _check_manifest_fields(plugin_dir: Path, manifest: dict, check_dirname: bool,
                           errors: list[str]) -> None:
    missing = [k for k in REQUIRED_MANIFEST_KEYS if k not in manifest]
    if missing:
        errors.append(f"manifest 缺少必填字段: {', '.join(missing)}")

    plugin_id = manifest.get("id")
    if not isinstance(plugin_id, str) or not ID_RE.match(plugin_id):
        errors.append(f"id 非法（小写字母开头，仅 a-z0-9_，长度 2-64）: {plugin_id!r}")
    elif check_dirname and plugin_dir.name != plugin_id:
        errors.append(f"目录名 {plugin_dir.name!r} 与 manifest.id {plugin_id!r} 不一致")

    name = manifest.get("name")
    if not isinstance(name, str) or not name.strip():
        errors.append("name 必须为非空字符串")

    version = manifest.get("version")
    if not isinstance(version, str) or not VERSION_RE.match(version):
        errors.append(f"version 必须为 SemVer（如 1.0.0）: {version!r}")

    if manifest.get("type") not in PLUGIN_TYPES:
        errors.append(f"type 必须为 storage 或 capability: {manifest.get('type')!r}")

    sdk_version = manifest.get("sdk_version")
    if not isinstance(sdk_version, str) or sdk_version.split(".")[0] != SDK_MAJOR:
        errors.append(f"sdk_version 必须为 {SDK_MAJOR}.x: {sdk_version!r}")

    perms = manifest.get("permissions")
    if not isinstance(perms, list) or not all(isinstance(p, str) for p in perms):
        errors.append("permissions 必须是字符串数组")
    else:
        if len(perms) != len(set(perms)):
            errors.append("permissions 存在重复项")
        extra = sorted(set(perms) - PERMISSION_WHITELIST)
        if extra:
            errors.append(f"permissions 超出平台白名单: {extra}")

    entry = manifest.get("entry")
    if not isinstance(entry, str) or not entry:
        errors.append("entry 必须为非空字符串")
    elif (".." in Path(entry).parts or entry.startswith(("/", "\\"))
          or re.match(r"^[A-Za-z]:", entry)):
        errors.append(f"entry 不允许绝对路径或目录穿越: {entry!r}")
    else:
        entry_path = (plugin_dir / entry).resolve()
        if not entry_path.is_relative_to(plugin_dir.resolve()):
            errors.append(f"entry 越出插件包根目录: {entry!r}")
        elif not entry_path.is_file():
            errors.append(f"entry 文件不存在: {entry!r}")
        elif entry_path.suffix != ".py":
            errors.append(f"entry 必须是 Python 文件: {entry!r}")

    schema = manifest.get("config_schema")
    if schema is not None:
        if not isinstance(schema, list):
            errors.append("config_schema 必须是数组")
        else:
            for i, field in enumerate(schema):
                if not isinstance(field, dict):
                    errors.append(f"config_schema[{i}] 必须是对象")
                    continue
                for key in ("key", "label", "type"):
                    if not isinstance(field.get(key), str) or not field.get(key):
                        errors.append(f"config_schema[{i}] 缺少字符串字段 {key}")
                if field.get("type") not in CONFIG_FIELD_TYPES:
                    errors.append(
                        f"config_schema[{i}] type 不支持: {field.get('type')!r}"
                        f"（可用: {', '.join(sorted(CONFIG_FIELD_TYPES))}）")


def _check_package_limits(plugin_dir: Path, errors: list[str]) -> None:
    files = [p for p in plugin_dir.rglob("*")
             if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc"]
    if len(files) > MAX_PACKAGE_FILES:
        errors.append(f"包内文件数 {len(files)} 超过上限 {MAX_PACKAGE_FILES}")
    total = sum(p.stat().st_size for p in files)
    if total > MAX_TOTAL_BYTES:
        errors.append(f"包内文件总大小 {total} 字节超过上限 {MAX_TOTAL_BYTES} 字节")


# ---------------------------------------------------------------- AST

def _is_forbidden_module(module: str) -> bool:
    """只允许 app.sdk.store_plugin；其余 app.* 与 plugins.* 均禁止。"""
    if module == "app" or module.startswith("app."):
        return not (module == "app.sdk.store_plugin"
                    or module.startswith("app.sdk.store_plugin."))
    return module == "plugins" or module.startswith("plugins.")


def _check_imports(py_file: Path, tree: ast.Module, rel: str, errors: list[str]) -> None:
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if _is_forbidden_module(alias.name):
                    errors.append(f"{rel}:{node.lineno} 禁止导入平台内部模块 {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            if node.level:  # 相对导入属于包内模块，允许
                continue
            module = node.module or ""
            if _is_forbidden_module(module):
                errors.append(f"{rel}:{node.lineno} 禁止导入平台内部模块 {module}")


def _class_attr(cls: ast.ClassDef, name: str):
    """读取类体中的简单字面量赋值（plugin_id / required_permissions）。"""
    for node in cls.body:
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, ast.AnnAssign):
            targets, value = [node.target], node.value
            if value is None:
                continue
        else:
            continue
        if any(isinstance(t, ast.Name) and t.id == name for t in targets):
            try:
                return ast.literal_eval(value)
            except (ValueError, SyntaxError):
                return None
    return None


def _base_names(cls: ast.ClassDef) -> set[str]:
    names: set[str] = set()
    for base in cls.bases:
        if isinstance(base, ast.Name):
            names.add(base.id)
        elif isinstance(base, ast.Attribute):
            names.add(base.attr)
    return names


def _method_names(cls: ast.ClassDef) -> set[str]:
    return {
        node.name for node in cls.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def _collect_methods(cls_name: str, class_map: dict[str, ast.ClassDef],
                     seen: set[str] | None = None) -> set[str]:
    """沿包内继承链收集方法名（基类可定义在包内其他模块）。"""
    seen = set() if seen is None else seen
    if cls_name in seen:
        return set()
    seen.add(cls_name)
    node = class_map.get(cls_name)
    if node is None:
        return set()
    methods = _method_names(node)
    for base in _base_names(node):
        methods |= _collect_methods(base, class_map, seen)
    return methods


def _inherits_storage(cls_name: str, class_map: dict[str, ast.ClassDef],
                      seen: set[str] | None = None) -> bool:
    """沿包内继承链判断最终是否继承 StoragePlugin。"""
    seen = set() if seen is None else seen
    if cls_name in seen:
        return False
    seen.add(cls_name)
    node = class_map.get(cls_name)
    if node is None:
        return False
    for base in _base_names(node):
        if base == "StoragePlugin":
            return True
        if base in class_map and _inherits_storage(base, class_map, seen):
            return True
    return False


def _check_entry(manifest: dict, tree: ast.Module,
                 class_map: dict[str, ast.ClassDef], errors: list[str]) -> None:
    plugin_id = manifest.get("id")
    ptype = manifest.get("type")
    if not isinstance(plugin_id, str):
        return

    # 平台装载只扫描入口模块顶层命名空间（vars(module)）
    classes = [
        node for node in tree.body
        if isinstance(node, ast.ClassDef) and _class_attr(node, "plugin_id") == plugin_id
    ]
    if not classes:
        errors.append(f"entry 顶层未找到 plugin_id={plugin_id!r} 的插件类")
        return
    if len(classes) > 1:
        names = ", ".join(c.name for c in classes)
        errors.append(f"entry 存在多个 plugin_id={plugin_id!r} 的类: {names}")
        return

    cls = classes[0]
    manifest_perms = set(manifest.get("permissions") or [])
    class_perms = _class_attr(cls, "required_permissions")
    if class_perms is None:
        errors.append(f"类 {cls.name} 未声明 required_permissions")
    else:
        class_perms = set(class_perms)
        if class_perms != manifest_perms:
            errors.append(
                f"类 {cls.name}.required_permissions {sorted(class_perms)} "
                f"与 manifest.permissions {sorted(manifest_perms)} 不一致"
            )

    methods = _collect_methods(cls.name, class_map)
    if ptype == "storage":
        if not _inherits_storage(cls.name, class_map):
            errors.append(f"storage 插件类 {cls.name} 必须继承 StoragePlugin")
        missing = sorted(REQUIRED_STORAGE_METHODS - methods)
        if missing:
            errors.append(f"storage 插件类 {cls.name} 缺少契约方法: {', '.join(missing)}")
    elif ptype == "capability":
        missing = sorted(REQUIRED_CAPABILITY_METHODS - methods)
        if missing:
            errors.append(f"capability 插件类 {cls.name} 缺少方法: {', '.join(missing)}")


# ---------------------------------------------------------------- 主流程

def validate_plugin_dir(plugin_dir: Path, check_dirname: bool = True) -> list[str]:
    errors: list[str] = []
    if not plugin_dir.is_dir():
        return [f"目录不存在: {plugin_dir}"]

    manifest = _load_manifest(plugin_dir, errors)
    if manifest is not None:
        _check_manifest_fields(plugin_dir, manifest, check_dirname, errors)

    _check_package_limits(plugin_dir, errors)

    trees: dict[str, ast.Module] = {}
    for py_file in sorted(plugin_dir.rglob("*.py")):
        if "__pycache__" in py_file.parts:
            continue
        rel = py_file.relative_to(plugin_dir).as_posix()
        try:
            tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
        except (OSError, SyntaxError, UnicodeDecodeError) as exc:
            errors.append(f"{rel}: 解析失败: {exc}")
            continue
        trees[rel] = tree
        _check_imports(py_file, tree, rel, errors)

    entry = (manifest or {}).get("entry")
    entry_tree = trees.get(entry) if isinstance(entry, str) else None
    if manifest is not None and entry_tree is not None:
        class_map = {
            node.name: node for tree in trees.values() for node in tree.body
            if isinstance(node, ast.ClassDef)
        }
        _check_entry(manifest, entry_tree, class_map, errors)

    return errors


def _discover_targets() -> list[Path]:
    targets = [
        p for p in sorted(PLUGINS_DIR.iterdir())
        if p.is_dir() and not p.name.startswith(("_", "."))
    ] if PLUGINS_DIR.is_dir() else []
    if TEMPLATES_DIR.is_dir():
        targets += [p for p in sorted(TEMPLATES_DIR.iterdir()) if p.is_dir()]
    return targets


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="鸿迹云插件包静态校验")
    parser.add_argument("paths", nargs="*", help="待校验的插件目录（省略则校验 plugins/ 下全部）")
    parser.add_argument("--all", action="store_true", help="校验 plugins/ 与 templates/ 下的全部目录")
    args = parser.parse_args(argv)

    if args.paths:
        targets = [Path(p) for p in args.paths]
    elif args.all:
        targets = _discover_targets()
    else:
        if not PLUGINS_DIR.is_dir():
            print(f"未找到插件目录: {PLUGINS_DIR}")
            return 1
        targets = [p for p in sorted(PLUGINS_DIR.iterdir())
                   if p.is_dir() and not p.name.startswith(("_", "."))]

    if not targets:
        print("没有待校验的插件目录。")
        return 0

    failed = 0
    for target in targets:
        # templates/<type>/ 的目录名不要求等于 manifest.id
        check_dirname = target.parent.name != "templates"
        errors = validate_plugin_dir(target, check_dirname=check_dirname)
        label = target.relative_to(REPO_ROOT) if target.is_relative_to(REPO_ROOT) else target
        if errors:
            failed += 1
            print(f"[FAIL] {label}")
            for err in errors:
                print(f"  - {err}")
        else:
            print(f"[ OK ] {label}")

    print(f"\n共 {len(targets)} 个插件目录，通过 {len(targets) - failed} 个，失败 {failed} 个。")
    return 1 if failed else 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except AttributeError:  # pragma: no cover  非标准流
        pass
    sys.exit(main())
