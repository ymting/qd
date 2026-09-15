# -*- coding: utf-8 -*-
"""吾爱破解模板离线集成测试：执行真实 Fetcher，仅替换网络响应。

模板结果判定分两层：
1. 第 21 步（领取奖励）只确认 HTTP 200 与登录有效；
2. 只有未取得明确成功提示时，才访问任务详情页复核真实任务状态。
因此「网站返回非进行中任务」不再直接判失败，而由详情页决定是「本期已完成」
还是「本轮领取未生效」。
"""

import asyncio
from copy import deepcopy
from io import BytesIO
import json
from pathlib import Path
import unittest
from urllib.parse import parse_qs, urlsplit

from tornado.httpclient import HTTPResponse
from tornado.httputil import HTTPHeaders

from libs.fetcher import Fetcher
from web.handlers.har import HARSave


ORIGIN = "https://www.52pojie.cn"
HOME = ORIGIN + "/"
APPLY = ORIGIN + "/home.php?mod=task&do=apply&id=2"
VIEW = ORIGIN + "/home.php?mod=task&do=view&id=2"
DRAW = ORIGIN + "/home.php?mod=task&do=draw&id=2"
DETAIL = ORIGIN + "/home.php?mod=task&do=view&id=2"
CREDIT = ORIGIN + "/home.php?mod=spacecp&ac=credit&showcredit=1"

DONE = "恭喜您，任务已成功完成，您将收到奖励通知，请注意查收"
NOT_UNDERWAY = "<p>不是进行中的任务</p>"
# 任务详情页的三种可识别状态。
DETAIL_DONE = "<div>任务状态：已完成</div><div>完成于 2026-09-15 00:05</div><div>1 天后可以再次申请</div>"
DETAIL_CAN_APPLY = '<div>任务状态：未申请</div><button type="submit">立即申请</button>'
DETAIL_UNKNOWN = "<div>任务列表</div>"
DEFAULT_CREDIT = "吾爱币: </em>216 CB"
# 领取奖励接口的传输层失败文案，应给出可执行的排查方向。
TRANSPORT_HINT = "站点返回 5xx 或请求在传输层失败"
TEMPLATE = Path(__file__).resolve().parents[1] / "templates" / "吾爱破解-签到.har"


def page(url, code=200, body="", location=None):
    return url, code, body, location


class WuaPojieTemplateTests(unittest.TestCase):
    def run_template(self, responses, variables=None, error=None):
        async def run():
            fetcher = Fetcher()
            fixtures = list(responses)
            calls = []
            local_markers = []
            env = {"variables": {"cookie": "auth=offline-fixture", **(variables or {})}, "session": []}

            async def fake_build_response(obj, proxy, *args):
                request, rule, rendered_env = fetcher.build_request({"request": deepcopy(obj["request"]), "rule": deepcopy(obj["rule"]), "env": obj["env"]}, proxy={})
                # 本地 util 沿用真实编码转换，网络步骤仍经 build_request/fetch/run_rule/do_fetch。
                if urlsplit(request.url).hostname in ("localhost", "127.0.0.1"):
                    self.assertTrue(request.url.endswith("/util/unicode"))
                    body = request.body.decode() if isinstance(request.body, bytes) else request.body
                    params = parse_qs(body)
                    marker = params.get("content", [""])[0]
                    local_markers.append(marker)
                    tmp = bytes(marker, "unicode_escape").decode("utf-8").replace(r"\u", r"\\u").replace(r"\\\u", r"\\u")
                    tmp = bytes(tmp, "utf-8").decode("unicode_escape")
                    tmp = tmp.encode("utf-8").replace(b"\xc2\xa0", b"\xa0").decode("unicode_escape")
                    content = json.dumps({"转换后": tmp, "状态": "200"}, ensure_ascii=False, indent=4)
                    status, location = 200, None
                    headers = HTTPHeaders({"Content-Type": "application/json; charset=UTF-8"})
                else:
                    calls.append(request)
                    # 在消费 fixture 前检查源站，任何外站请求都会立刻失败。
                    self.assertEqual("https", urlsplit(request.url).scheme)
                    self.assertEqual("www.52pojie.cn", urlsplit(request.url).netloc)
                    self.assertFalse(request.follow_redirects)
                    self.assertTrue(fixtures, f"多余外部请求: {request.url}")
                    expected_url, status, content, location = fixtures.pop(0)
                    self.assertEqual(expected_url, request.url)
                    headers = HTTPHeaders({"Content-Type": "text/html; charset=UTF-8"})
                if location is not None:
                    headers["Location"] = location
                response = HTTPResponse(request, status, headers=headers, buffer=BytesIO(str(content).encode()), reason="OK" if status == 200 else "Found")
                return rule, rendered_env, response

            fetcher.build_response = fake_build_response
            try:
                entries = json.loads(TEMPLATE.read_text(encoding="utf-8"))
                if error:
                    try:
                        await fetcher.do_fetch(entries, env, proxies=[])
                    except Exception as exc:
                        self.assertRegex(str(exc), error)
                    else:
                        self.fail(f"未按预期失败: {error}")
                else:
                    env, _ = await fetcher.do_fetch(entries, env, proxies=[])
                    self.assertIn("签到结果", env["variables"]["__log__"])
                self.assertFalse(fixtures, "没有执行完预期的外部请求")
                return env, calls, local_markers
            finally:
                fetcher.client.close()

        return asyncio.run(run())

    @staticmethod
    def finish(message=DONE, credit=DEFAULT_CREDIT):
        """领取奖励接口返回明确成功提示：不应再请求任务详情页。"""
        return [page(DRAW, body=f"<p>{message}</p>"), page(CREDIT, body=credit)]

    @staticmethod
    def finish_via_detail(draw_body, detail_body=DETAIL_DONE, credit=DEFAULT_CREDIT):
        """领取奖励接口未给出明确成功提示：必须经过任务详情复核后才能判定。"""
        return [page(DRAW, body=draw_body), page(DETAIL, body=detail_body), page(CREDIT, body=credit)]

    def test_first_apply_302_completes_same_run_for_supported_locations(self):
        for location in (VIEW, "/home.php?mod=task&do=view&id=2", "home.php?mod=task&do=view&id=2", "./home.php?mod=task&do=view&id=2", "//www.52pojie.cn/home.php?mod=task&do=view&id=2", "?mod=task&do=view&id=2"):
            with self.subTest(location=location):
                env, calls, _ = self.run_template([page(HOME), page(APPLY, 302, location=location), page(VIEW), *self.finish()])
                self.assertEqual(DONE, env["variables"]["sign_message"])
                self.assertEqual(5, len(calls))

    def test_original_dynamic_branch_and_302_location(self):
        dynamic = ORIGIN + "/check.php?wzwscspd=MC4wLjAuMA=="
        _, calls, _ = self.run_template([page(HOME), page(APPLY, body="dynamicurl|/check.php|"), page(dynamic, 302, location="/"), page(HOME), *self.finish()])
        self.assertEqual(6, len(calls))

    def test_apply_redirect_then_dynamic_redirect_completes_same_run(self):
        apply_result = ORIGIN + "/task-result.php"
        dynamic = ORIGIN + "/check.php?wzwscspd=MC4wLjAuMA=="
        validation_result = ORIGIN + "/verification-complete.php"
        env, calls, _ = self.run_template(
            [
                page(HOME),
                page(APPLY, 302, location="/task-result.php"),
                page(apply_result, body="dynamicurl|/check.php|"),
                page(dynamic, 302, location="/verification-complete.php"),
                page(validation_result),
                *self.finish(),
            ]
        )
        self.assertEqual(DONE, env["variables"]["sign_message"])
        self.assertEqual(
            [HOME, APPLY, apply_result, dynamic, validation_result, DRAW, CREDIT],
            [request.url for request in calls],
        )

    def test_har_save_exposes_only_expected_user_variables(self):
        entries = json.loads(TEMPLATE.read_text(encoding="utf-8"))
        self.assertEqual(
            {"cookie", "browser_fingerprint", "browser_user_agent"},
            HARSave.get_variables(HARSave.env, entries),
        )

    def test_blank_browser_configuration_uses_default_transport_fields(self):
        _, calls, _ = self.run_template([page(HOME), page(APPLY), *self.finish()])
        for request in calls:
            with self.subTest(url=request.url):
                self.assertEqual("chrome", request._qd_impersonate)
                self.assertEqual("auto", request.headers["User-Agent"])
                self.assertIn("auth=offline-fixture", request.headers["Cookie"])
                self.assertNotIn("X-QD-Impersonate", request.headers)

    def test_dynamic_200_does_not_use_stale_redirect(self):
        dynamic = ORIGIN + "/check.php?wzwscspd=MC4wLjAuMA=="
        self.run_template([page(HOME), page(APPLY, body="dynamicurl|/check.php|"), page(dynamic), *self.finish()], {"redirect_location": "https://evil.example/", "redirect_path": "old.php"})

    def test_missing_dynamic_clears_previous_variables_and_skips_check(self):
        env, calls, _ = self.run_template([page(HOME), page(APPLY), *self.finish()], {"url_key": "/old.php", "check_url": "https://evil.example/", "redirect_location": "https://evil.example/", "sign_message": "旧成功", "coin": "999 CB", "reputation": "999 点"})
        self.assertEqual("", env["variables"]["url_key"])
        self.assertEqual(4, len(calls))
        self.assertNotIn("999", env["variables"]["__log__"])
        self.assertIn("威望：【未返回】", env["variables"]["__log__"])

    # ---- 领取奖励接口的判定链路 -------------------------------------------

    def test_explicit_success_skips_task_detail_verification(self):
        env, calls, _ = self.run_template([page(HOME), page(APPLY), *self.finish()])
        self.assertEqual([HOME, APPLY, DRAW, CREDIT], [request.url for request in calls])
        self.assertEqual(DONE, env["variables"]["sign_message"])
        self.assertIsNone(env["variables"].get("verify_state"))

    def test_explicit_success_wins_over_not_underway_text(self):
        # 站点偶发在同页同时输出成功提示与任务状态说明，明确成功提示优先。
        env, calls, _ = self.run_template(
            [page(HOME), page(APPLY), page(DRAW, body=f"<p>{DONE}</p>{NOT_UNDERWAY}"), page(CREDIT, body=DEFAULT_CREDIT)],
            {"sign_message": DONE},
        )
        self.assertEqual(DONE, env["variables"].get("sign_message"))
        self.assertEqual([HOME, APPLY, DRAW, CREDIT], [request.url for request in calls])
        self.assertIn(DONE, env["variables"]["__log__"])

    def test_not_underway_with_completed_task_reports_already_signed(self):
        # 本轮修复的核心场景：网站返回非进行中任务，但任务详情显示本期已完成。
        env, calls, _ = self.run_template(
            [page(HOME), page(APPLY), *self.finish_via_detail(NOT_UNDERWAY)],
            {"sign_message": DONE},
        )
        self.assertEqual([HOME, APPLY, DRAW, DETAIL, CREDIT], [request.url for request in calls])
        self.assertIsNone(env["variables"].get("sign_message"))
        self.assertEqual("verify_ok", env["variables"].get("verify_state"))
        log = env["variables"]["__log__"]
        self.assertIn("今日已签到，未重复领取奖励", log)
        self.assertIn("（模板 v", log)

    def test_not_underway_with_applicable_task_fails_with_actionable_reason(self):
        # 本轮领取确实没有生效：详情页显示可以立即申请，必须继续失败，不能报成功。
        env, calls, _ = self.run_template(
            [page(HOME), page(APPLY), page(DRAW, body=NOT_UNDERWAY), page(DETAIL, body=DETAIL_CAN_APPLY)],
            {"sign_message": DONE},
            error="本轮领取未生效",
        )
        self.assertEqual([HOME, APPLY, DRAW, DETAIL], [request.url for request in calls])
        self.assertEqual("verify_fail_reapply", env["variables"].get("verify_state"))

    def test_unknown_draw_content_is_verified_and_fails_without_credit(self):
        for body in ("<p>微信签到每天都送论坛币！</p>", "<h2>已完成的任务</h2>", "<p>尚未签到成功</p>", "<p>签到成功攻略</p>", "<p>任务已完成？尚未领取</p>", "<p>今日已签到攻略</p>", "<p>站点新增提示</p>"):
            with self.subTest(body=body):
                env, calls, _ = self.run_template(
                    [page(HOME), page(APPLY), page(DRAW, body=body), page(DETAIL, body=DETAIL_UNKNOWN)],
                    {"sign_message": DONE},
                    error="任务详情未返回可识别的任务状态，未取得明确签到完成状态",
                )
                self.assertEqual([HOME, APPLY, DRAW, DETAIL], [request.url for request in calls])
                self.assertIsNone(env["variables"].get("sign_message"))

    def test_detail_page_login_and_status_failures_are_classified(self):
        self.run_template(
            [page(HOME), page(APPLY), page(DRAW, body=NOT_UNDERWAY), page(DETAIL, body="<p>请先登录</p>")],
            error="网站要求登录，无法复核任务状态",
        )
        self.run_template(
            [page(HOME), page(APPLY), page(DRAW, body=NOT_UNDERWAY), page(DETAIL, 403, "")],
            error="登录或访问受限，无法复核任务状态",
        )
        self.run_template(
            [page(HOME), page(APPLY), page(DRAW, body=NOT_UNDERWAY), page(DETAIL, 404, "")],
            error="任务详情响应不是200，无法复核任务状态",
        )

    def test_draw_transport_failure_is_classified_with_timeout_advice(self):
        # HTTP 599 是 QD 对 curl_cffi 传输异常的合成状态码，日志原文为 curl: (28) 超时。
        for status in (599, 502, 0):
            with self.subTest(status=status):
                self.run_template(
                    [page(HOME), page(APPLY), page(DRAW, status, "")],
                    error=TRANSPORT_HINT,
                )

    def test_transport_failure_is_classified_on_every_external_step(self):
        self.run_template([page(HOME, 599, "")], error=TRANSPORT_HINT)
        self.run_template([page(HOME), page(APPLY, 599, "")], error=TRANSPORT_HINT)
        self.run_template([page(HOME), page(APPLY), page(DRAW, body=NOT_UNDERWAY), page(DETAIL, 599, "")], error=TRANSPORT_HINT)

    def test_explicit_already_signed(self):
        message = "今日已签到。"
        env, _, markers = self.run_template([page(HOME), page(APPLY), *self.finish(message)])
        self.assertIn(message, markers[-1])
        self.assertIn(message, env["variables"]["__log__"])

    def test_final_log_encodes_form_content(self):
        async def run():
            fetcher = Fetcher()
            try:
                entry = json.loads(TEMPLATE.read_text(encoding="utf-8"))[-1]
                request, _, _ = fetcher.build_request({"request": entry["request"], "rule": entry["rule"], "env": {"variables": {"sign_message": "说明 A&B=1"}, "session": []}}, proxy={})
                params = parse_qs(request.body.decode())
                self.assertEqual({"content", "html_unescape"}, set(params))
                self.assertIn("说明 A&B=1", params["content"][0])
            finally:
                fetcher.client.close()
        asyncio.run(run())

    # ---- 跳转与登录约束 ---------------------------------------------------

    def test_rejects_bad_location_without_contacting_target(self):
        for location in (None, "", "/ok\n", "/ok\r", "https://evil.example/", "//evil.example/", "https://www.52pojie.cn.evil.example/", "https://www.52pojie.cn@evil.example/", "http://www.52pojie.cn/", "javascript:alert(1)", r"\evil.example", "https://www.52pojie.cn:443/", "/member.php?mod=logging&action=login", "member.php?mod=logging&action=login"):
            with self.subTest(location=location):
                self.run_template([page(HOME), page(APPLY, 302, location=location)], error="Location或动态地址缺失")

    def test_relative_nested_paths_are_resolved_against_current_page(self):
        self.run_template([page(HOME), page(APPLY, 302, location="/folder/step.php"), page(ORIGIN + "/folder/step.php", 302, location="../home.php?mod=task&do=view&id=2"), page(VIEW), *self.finish()])

    def test_loop_and_redirect_limit_fail(self):
        self.run_template([page(HOME), page(APPLY, 302, location=APPLY)], error="重复跳转或循环")
        self.run_template([page(HOME), page(APPLY, 302, location="/a"), page(ORIGIN + "/a", 302, location="/b"), page(ORIGIN + "/b", 302, location="/c"), page(ORIGIN + "/c", 302, location="/d")], error="跳转超过3次")

    def test_401_403_429_and_login_html_always_fail(self):
        for status in (401, 403, 429):
            for prefix, url in (([], HOME), ([page(HOME)], APPLY), ([page(HOME), page(APPLY)], DRAW)):
                with self.subTest(status=status, url=url):
                    self.run_template([*prefix, page(url, status, f"<p>{DONE}</p>")], error="failed_asserts")
        self.run_template([page(HOME), page(APPLY, body="请先登录")], error="failed_asserts")

    def test_draw_failure_categories_include_chinese_meaning(self):
        cases = (
            (403, "", "登录或访问受限，未取得明确签到完成状态"),
            (200, "请先登录", "网站要求登录，未取得明确签到完成状态"),
        )
        for status, body, category in cases:
            with self.subTest(status=status, body=body):
                self.run_template([page(HOME), page(APPLY), page(DRAW, status, body)], error=category)

    def test_dynamic_external_address_is_rejected_before_request(self):
        self.run_template([page(HOME), page(APPLY, body="dynamicurl|https://evil.example/check.php|")], error="Location或动态地址缺失")


if __name__ == "__main__":
    unittest.main()
