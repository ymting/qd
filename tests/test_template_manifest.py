# -*- coding: utf-8 -*-
"""模板清单与版本管理的回归测试。

清单是模板版本的事实来源，这些用例保证：
1. 清单与磁盘上的每个 HAR 一一对应；
2. 清单里的哈希、条目数、任务变量与实际模板一致；
3. 版本号格式合法，且真的写进了模板日志（用户能从推送消息确认运行版本）；
4. 校验器本身是有效的——被篡改的哈希、非 ASCII 变量名、丢失的版本标记都能被发现。
"""

import importlib.util
import json
import sys
import unittest
from pathlib import Path

QD_DIR = Path(__file__).resolve().parents[1]
TOOL_PATH = QD_DIR / "tools" / "template_manifest.py"

sys.path.insert(0, str(QD_DIR))
spec = importlib.util.spec_from_file_location("template_manifest", TOOL_PATH)
assert spec and spec.loader
template_manifest = importlib.util.module_from_spec(spec)
spec.loader.exec_module(template_manifest)


class TemplateManifestTests(unittest.TestCase):
    def test_manifest_matches_every_template_on_disk(self):
        problems = template_manifest.check()
        self.assertEqual([], problems, "\n".join(problems))

    def test_every_template_declares_a_dated_version(self):
        manifest = template_manifest.load_manifest()
        templates = manifest["templates"]
        self.assertEqual(len(template_manifest.template_files()), len(templates))
        for entry in templates:
            with self.subTest(template=entry["file"]):
                self.assertRegex(entry["version"], r"^\d{8}\.\d+$")

    def test_version_is_visible_in_template_log_output(self):
        # 用户只能从推送日志判断 QD 数据库里实际运行的是哪一版模板。
        for entry in template_manifest.load_manifest()["templates"]:
            with self.subTest(template=entry["file"]):
                entries = template_manifest.read_entries(entry["file"])
                log_data = entries[-1]["request"].get("data") or ""
                self.assertIn(f"v{entry['version']}", log_data)

    def test_checker_detects_tampered_hash(self):
        original = template_manifest.file_sha256
        template_manifest.file_sha256 = lambda filename: "0" * 64
        try:
            problems = template_manifest.check()
        finally:
            template_manifest.file_sha256 = original
        self.assertTrue(any("sha256" in problem for problem in problems), problems)

    def test_checker_rejects_non_ascii_variable_names(self):
        # QD 前端用 JavaScript \w 提取变量名，中文键名会被截断成无效字段。
        original = template_manifest.task_variables
        template_manifest.task_variables = lambda entries: ["签_到"]
        try:
            problems = template_manifest.check()
        finally:
            template_manifest.task_variables = original
        self.assertTrue(
            any("非 ASCII" in problem for problem in problems), problems
        )

    def test_checker_detects_missing_version_banner(self):
        original = template_manifest.read_entries
        template_manifest.read_entries = lambda filename: []
        try:
            problems = template_manifest.check()
        finally:
            template_manifest.read_entries = original
        self.assertTrue(any("版本标记" in problem for problem in problems), problems)

    def test_unmanaged_entries_are_documented(self):
        manifest = template_manifest.load_manifest()
        for item in manifest["unmanaged"]:
            with self.subTest(path=item["path"]):
                self.assertTrue(item["reason"])

    def test_manifest_is_valid_json_with_expected_shape(self):
        raw = (QD_DIR / "templates" / "manifest.json").read_text(encoding="utf-8")
        manifest = json.loads(raw)
        self.assertEqual(1, manifest["schema"])
        self.assertIn("version_format", manifest)
        self.assertIn("mirror_base", manifest)
        for entry in manifest["templates"]:
            self.assertEqual(
                {
                    "file",
                    "name",
                    "site",
                    "version",
                    "updated",
                    "qd_min_version",
                    "mirror",
                    "notes",
                    "sha256",
                    "entries",
                    "required_variables",
                },
                set(entry),
                entry.get("file"),
            )


if __name__ == "__main__":
    unittest.main()
