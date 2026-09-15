#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""模板版本管理工具：维护并校验 `templates/manifest.json`。

QD 数据库里保存的是导入当时的 HAR 快照，仓库里的模板文件更新后不会自动同步。
这个工具让「仓库里的模板版本」变成可机检的事实，配合模板日志里的版本号，
就能确认某个任务实际运行的是哪一版模板。

用法（在仓库根目录执行）：

    python tools/template_manifest.py --check
        校验清单与磁盘模板是否一致，不一致时以非零退出码结束。

    python tools/template_manifest.py --write
        重新计算并写回 sha256、条目数、任务变量等可推导字段；
        version / updated / notes 等人工字段保持不变。

    python tools/template_manifest.py --set-version 吾爱破解-签到.har 20260915.2
        升级指定模板的版本号，并把 updated 更新为今天。

清单里的 mirror 路径以仓库根目录为基准，用于记录外层工作区 templates/ 中的副本。
"""

import argparse
import datetime
import hashlib
import json
import re
import sys
from pathlib import Path

QD_ROOT = Path(__file__).resolve().parents[1]
if str(QD_ROOT) not in sys.path:
    sys.path.insert(0, str(QD_ROOT))

TEMPLATE_DIR = QD_ROOT / "templates"
MANIFEST_PATH = TEMPLATE_DIR / "manifest.json"

# 模板版本号与项目发布版本同构：YYYYMMDD.N，同一天多次修订递增 N。
VERSION_RE = re.compile(r"^\d{8}\.\d+$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
VARIABLE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# 可推导字段由 --write 维护，人工字段由维护者填写。
DERIVED_FIELDS = ("sha256", "entries", "required_variables")
HUMAN_FIELDS = ("name", "site", "version", "updated", "qd_min_version", "mirror", "notes")


def load_manifest() -> dict:
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


def template_files() -> list:
    return sorted(path.name for path in TEMPLATE_DIR.glob("*.har"))


def read_entries(filename: str) -> list:
    return json.loads((TEMPLATE_DIR / filename).read_text(encoding="utf-8"))


def file_sha256(filename: str) -> str:
    return hashlib.sha256((TEMPLATE_DIR / filename).read_bytes()).hexdigest()


def task_variables(entries: list) -> list:
    """复用 QD 前端的变量提取逻辑，保证清单与任务表单看到的变量一致。

    模板文件里的控制语句条目（如 {% if %}）可能只保留 request.url，
    这里补齐编辑器会自动补上的空字段后再提取，避免工具因判定 NULL 而中断。
    """
    from web.handlers.har import HARSave

    normalized = []
    for entry in entries:
        request = {"method": "GET", "headers": [], "cookies": []}
        request.update(entry.get("request") or {})
        normalized.append({"request": request, "rule": entry.get("rule") or {}})
    return sorted(HARSave.get_variables(HARSave.env, normalized))


def computed_fields(filename: str) -> dict:
    entries = read_entries(filename)
    return {
        "sha256": file_sha256(filename),
        "entries": len(entries),
        "required_variables": task_variables(entries),
    }


def resolve_mirror(entry: dict):
    """解析 mirror 字段；外层工作区不存在镜像目录时跳过校验。

    仓库单独克隆时看不到外层 templates/，此时镜像检查只是本地维护手段，
    不应让 CI 或独立检出报错。
    """
    mirror = entry.get("mirror")
    if not mirror:
        return None, None
    if not isinstance(mirror, dict) or "path" not in mirror:
        return None, "mirror 必须是包含 path 的对象"
    path = (QD_ROOT / mirror["path"]).resolve()
    state = mirror.get("state", "synced")
    if state not in ("synced", "stale"):
        return None, f"mirror.state 只支持 synced 或 stale，当前为 {state!r}"
    if not path.parent.is_dir():
        return None, None
    return path, None


def check() -> list:
    """返回问题描述列表；空列表表示清单与模板完全一致。"""
    problems = []
    manifest = load_manifest()
    entries = manifest.get("templates") or []
    listed = [entry.get("file") for entry in entries]

    if len(listed) != len(set(listed)):
        problems.append("manifest 中存在重复的 file 字段")

    on_disk = template_files()
    for filename in on_disk:
        if filename not in listed:
            problems.append(f"模板 {filename} 未登记到 manifest")
    for filename in listed:
        if filename not in on_disk:
            problems.append(f"manifest 登记的 {filename} 在 templates/ 中不存在")

    for entry in entries:
        filename = entry.get("file")
        if filename not in on_disk:
            continue
        prefix = f"[{filename}]"
        for field in HUMAN_FIELDS:
            if field == "mirror":
                continue
            if not entry.get(field):
                problems.append(f"{prefix} 缺少人工字段 {field}")
        version = entry.get("version", "")
        if version and not VERSION_RE.match(version):
            problems.append(f"{prefix} version 需为 YYYYMMDD.N，当前为 {version!r}")
        updated = entry.get("updated", "")
        if updated and not DATE_RE.match(updated):
            problems.append(f"{prefix} updated 需为 YYYY-MM-DD，当前为 {updated!r}")

        computed = computed_fields(filename)
        for field, value in computed.items():
            if entry.get(field) != value:
                problems.append(
                    f"{prefix} {field} 与模板不一致，执行 --write 同步"
                    if field != "required_variables"
                    else f"{prefix} required_variables 应为 {value}"
                )

        for variable in computed["required_variables"]:
            if not VARIABLE_RE.match(variable):
                problems.append(
                    f"{prefix} 变量名 {variable!r} 非 ASCII，QD 前端会截断中文键名"
                )

        # 版本号必须出现在末步日志里，用户才能从推送消息确认实际运行的模板版本。
        banner = f"v{version}"
        if version and banner not in json.dumps(read_entries(filename), ensure_ascii=False):
            problems.append(f"{prefix} 模板内容中找不到版本标记 {banner}")

        mirror_path, error = resolve_mirror(entry)
        if error:
            problems.append(f"{prefix} {error}")
        elif mirror_path is not None:
            if not mirror_path.is_file():
                problems.append(f"{prefix} 镜像 {entry['mirror']['path']} 不存在")
            elif entry["mirror"].get("state", "synced") == "synced":
                if mirror_path.read_bytes() != (TEMPLATE_DIR / filename).read_bytes():
                    problems.append(
                        f"{prefix} 镜像 {entry['mirror']['path']} 与模板内容不一致"
                    )
    return problems


def write() -> None:
    manifest = load_manifest()
    for entry in manifest.get("templates") or []:
        filename = entry.get("file")
        if filename not in template_files():
            continue
        entry.update(computed_fields(filename))
    manifest["updated"] = datetime.date.today().isoformat()
    MANIFEST_PATH.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"已更新 {MANIFEST_PATH}")


def set_version(filename: str, version: str) -> None:
    if not VERSION_RE.match(version):
        raise SystemExit(f"版本号需为 YYYYMMDD.N 格式：{version!r}")
    manifest = load_manifest()
    for entry in manifest.get("templates") or []:
        if entry.get("file") == filename:
            entry["version"] = version
            entry["updated"] = datetime.date.today().isoformat()
            break
    else:
        raise SystemExit(f"manifest 中没有登记 {filename}")
    MANIFEST_PATH.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"{filename} 版本已更新为 {version}")


def main() -> int:
    parser = argparse.ArgumentParser(description="校验并维护模板版本清单")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--check", action="store_true", help="校验清单与模板是否一致")
    group.add_argument("--write", action="store_true", help="重新计算并写回可推导字段")
    group.add_argument(
        "--set-version", nargs=2, metavar=("FILE", "VERSION"), help="升级指定模板版本号"
    )
    args = parser.parse_args()

    if args.write:
        write()
        return 0
    if args.set_version:
        set_version(*args.set_version)
        return 0

    problems = check()
    if problems:
        print("模板清单校验失败：")
        for problem in problems:
            print(f"  - {problem}")
        return 1
    print(f"模板清单校验通过，共 {len(template_files())} 个模板")
    return 0


if __name__ == "__main__":
    sys.exit(main())
