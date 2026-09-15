# -*- coding: utf-8 -*-
"""safe_eval 在新版解释器上的条件语句回归测试。

Python 3.13 把 `not x` 的真值判断改用 TO_BOOL 生成字节码，白名单只放行 UNARY_NOT
会让模板里合法的 `{% if not 变量 %}` 直接报 forbidden opcode。
"""

import unittest

from libs.safe_eval import safe_eval


class SafeEvalConditionTests(unittest.TestCase):
    def test_not_operator_is_allowed(self):
        self.assertTrue(safe_eval("not sign_message", {}, {"sign_message": None}))
        self.assertTrue(safe_eval("not sign_message", {}, {"sign_message": ""}))
        self.assertFalse(safe_eval("not sign_message", {}, {"sign_message": "已签到"}))

    def test_boolean_operators_are_allowed(self):
        self.assertTrue(safe_eval("a and b", {}, {"a": "1", "b": "2"}))
        self.assertTrue(safe_eval("a or b", {}, {"a": "", "b": "2"}))
        self.assertFalse(safe_eval("a and not b", {}, {"a": "1", "b": "2"}))

    def test_undefined_name_is_still_rejected(self):
        # QD 依赖 NameError 把未定义变量当作 False，这个行为不能被改坏。
        with self.assertRaises(ValueError):
            safe_eval("missing == '1'", {}, {})


if __name__ == "__main__":
    unittest.main()
