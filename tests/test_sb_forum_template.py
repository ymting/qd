# -*- coding: utf-8 -*-
"""烧饼论坛双登录签到 HAR 的离线结构与脱敏回归测试。"""

import asyncio
from copy import deepcopy
import json
import re
import unittest
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs

from jinja2 import Environment, meta
from tornado.httpclient import HTTPResponse
from tornado.httputil import HTTPHeaders

from libs.fetcher import Fetcher
from libs.safe_eval import safe_eval


TEMPLATE_PATH = Path(__file__).resolve().parents[1] / "templates" / "烧饼论坛-签到.har"
CONTROL_PATTERN = re.compile(r"^\{%\s*(?:if|else|endif)\b.*%\}$")
ASCII_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class SbForumTemplateTests(unittest.TestCase):
    """验证两条登录路径的边界，不连接真实论坛，也不伪造签到成功。"""

    @classmethod
    def setUpClass(cls):
        cls.entries = json.loads(TEMPLATE_PATH.read_text(encoding="utf-8"))
        cls.jinja = Environment(autoescape=False)
        cls.request_entries = [
            entry
            for entry in cls.entries
            if not CONTROL_PATTERN.fullmatch(entry["request"].get("url", ""))
        ]
        cls.control_urls = [
            entry["request"]["url"]
            for entry in cls.entries
            if CONTROL_PATTERN.fullmatch(entry["request"].get("url", ""))
        ]

    @staticmethod
    def _render(value, **variables):
        return SbForumTemplateTests.jinja.from_string(value).render(**variables)

    @staticmethod
    def _all_sources(entry):
        request = entry["request"]
        sources = [
            request.get("method", ""),
            request.get("url", ""),
            request.get("data", ""),
        ]
        sources.extend(header.get("name", "") for header in request.get("headers", []))
        sources.extend(header.get("value", "") for header in request.get("headers", []))
        sources.extend(cookie.get("name", "") for cookie in request.get("cookies", []))
        sources.extend(cookie.get("value", "") for cookie in request.get("cookies", []))
        return sources

    @staticmethod
    def _rule(entry):
        return entry.get("rule", {})

    def _mode_validation_request(self):
        return next(
            entry
            for entry in self.request_entries
            if entry["request"]["url"] == "api://util/unicode"
            and "模式校验" in entry["request"].get("data", "")
        )

    @staticmethod
    def _build_request(entry, variables=None):
        fetcher = Fetcher()
        env = {"variables": dict(variables or {}), "session": []}
        request, rule, rendered_env = fetcher.build_request(
            {
                # Fetcher.render 会原地渲染字典；测试必须隔离每个用例，
                # 否则一个 fixture 会把默认请求头泄漏到后续断言。
                "request": deepcopy(entry["request"]),
                "rule": deepcopy(entry.get("rule", {})),
                "env": env,
            },
            proxy={},
        )
        return fetcher, request, rule, rendered_env

    @staticmethod
    def _validation_marker(request):
        body = request.body.decode("utf-8") if isinstance(request.body, bytes) else request.body
        return parse_qs(body or "").get("content", [""])[0]

    @staticmethod
    def _validation_response(request, marker):
        body = json.dumps(
            {"转换后": marker, "状态": "200"}, ensure_ascii=False
        ).encode("utf-8")
        return HTTPResponse(
            request=request,
            code=200,
            reason="OK",
            headers=HTTPHeaders({"Content-Type": "application/json; charset=UTF-8"}),
            buffer=BytesIO(body),
        )

    @staticmethod
    def _html_response(request, body):
        """构造不联网的 HTML 响应，专门验证页面断言边界。"""
        if isinstance(body, str):
            body = body.encode("utf-8")
        return HTTPResponse(
            request=request,
            code=200,
            reason="OK",
            headers=HTTPHeaders({"Content-Type": "text/html; charset=UTF-8"}),
            buffer=BytesIO(body),
        )

    def _password_request(self):
        return next(
            entry
            for entry in self.request_entries
            if entry["request"]["url"] == "http://sb-forum-browser:8766/api/signin"
        )

    def _cookie_get_request(self):
        return next(
            entry
            for entry in self.request_entries
            if "sb.sb/signin/" in entry["request"]["url"]
            and entry["request"]["method"] == "GET"
            and "login_mode" in entry["request"]["url"]
        )

    def _cookie_post_request(self):
        return next(
            entry
            for entry in self.request_entries
            if entry["request"]["url"] == "https://sb.sb/signin/"
            and entry["request"]["method"] == "POST"
        )

    def _cookie_final_request(self):
        return next(
            entry
            for entry in self.request_entries
            if entry["request"]["url"] == "https://sb.sb/signin/"
            and entry["request"]["method"] == "GET"
        )

    def test_template_is_json_and_has_explicit_mode_blocks(self):
        self.assertGreaterEqual(len(self.entries), 8)
        self.assertEqual(
            [
                "{% if str(login_mode).strip().lower() == 'password' %}",
                "{% else %}",
                "{% if already_signed_marker %}",
                "{% else %}",
                "{% endif %}",
                "{% endif %}",
            ],
            self.control_urls,
        )
        self.assertEqual(6, len(self.request_entries))
        self.assertEqual("api://util/unicode", self.entries[0]["request"]["url"])
        for entry in self.entries:
            self.assertNotIn("invalid-login-mode", entry["request"].get("url", ""))

    def test_cookie_default_and_normalized_mode_render_direct_forum_url(self):
        cookie_request = self._cookie_get_request()
        url_template = cookie_request["request"]["url"]

        self.assertEqual(
            "https://sb.sb/signin/",
            self._render(url_template, login_mode=""),
        )
        self.assertEqual(
            "https://sb.sb/signin/",
            self._render(url_template, login_mode="  CoOkIe  "),
        )
        self.assertEqual(
            "https://sb.sb/signin/",
            self._render(url_template),
        )

    def test_invalid_mode_is_validated_locally_before_routing(self):
        validation = self._mode_validation_request()
        fetcher, request, rule, env = self._build_request(
            validation, {"login_mode": "unknown"}
        )
        marker = self._validation_marker(request)
        self.assertEqual(
            "模式校验失败：login_mode 仅支持 cookie 或 password",
            marker,
        )
        self.assertTrue(request.url.endswith("/util/unicode"))
        self.assertNotIn("sb.sb", request.url)
        self.assertNotIn("sb-forum-browser", request.url)

        success, message = fetcher.run_rule(
            self._validation_response(request, marker), rule, env
        )
        self.assertFalse(success)
        self.assertIn("模式校验失败", message)

    def test_fetcher_renders_all_modes_through_local_validation_request(self):
        validation = self._mode_validation_request()
        cases = (
            ({}, "模式校验通过：cookie"),
            ({"login_mode": ""}, "模式校验通过：cookie"),
            ({"login_mode": "  CoOkIe  "}, "模式校验通过：cookie"),
            ({"login_mode": " PASSWORD "}, "模式校验通过：password"),
        )
        for variables, expected_marker in cases:
            fetcher, request, rule, env = self._build_request(validation, variables)
            marker = self._validation_marker(request)
            self.assertEqual(expected_marker, marker)
            self.assertTrue(request.url.endswith("/util/unicode"))
            success, message = fetcher.run_rule(
                self._validation_response(request, marker), rule, env
            )
            self.assertTrue(success, message)

    def test_mode_validation_requires_readable_success_marker(self):
        """本地 util 即使返回 200，也不能在缺少校验结果时静默放行。"""
        validation = self._mode_validation_request()
        fetcher, request, rule, env = self._build_request(
            validation, {"login_mode": "cookie"}
        )
        success, message = fetcher.run_rule(
            self._validation_response(request, ""), rule, env
        )
        self.assertFalse(success)
        self.assertIn("success_asserts", message)

    def test_invalid_mode_stops_before_any_other_request(self):
        entries = json.loads(TEMPLATE_PATH.read_text(encoding="utf-8"))
        fetcher = Fetcher()
        calls = []

        async def fake_fetch(obj, proxy=None):
            calls.append(obj["request"]["url"])
            request, _, _ = fetcher.build_request(obj, proxy={})
            return {
                "success": False,
                "response": SimpleNamespace(
                    request=SimpleNamespace(url=request.url),
                ),
                "env": obj["env"],
                "msg": "模式校验失败：login_mode 仅支持 cookie 或 password",
            }

        fetcher.fetch = fake_fetch
        env = {"variables": {"login_mode": "unknown"}, "session": []}
        with self.assertRaises(Exception) as raised:
            asyncio.run(fetcher.do_fetch(entries, env, proxies=[]))

        self.assertEqual(["api://util/unicode"], calls)
        self.assertIn("模式校验失败", str(raised.exception))
        self.assertIn("/util/unicode", str(raised.exception))

    def test_fetcher_password_mode_routes_to_browser_after_local_validation(self):
        entries = json.loads(TEMPLATE_PATH.read_text(encoding="utf-8"))
        fetcher = Fetcher()
        requests = []

        async def fake_fetch(obj, proxy=None):
            request, _, _ = fetcher.build_request(obj, proxy={})
            requests.append(request)
            return {
                "success": True,
                "response": SimpleNamespace(
                    request=SimpleNamespace(url=request.url),
                ),
                "env": obj["env"],
                "msg": "",
            }

        fetcher.fetch = fake_fetch
        env = {
            "variables": {
                "login_mode": " PASSWORD ",
                "profile_name": "fixture-profile",
                "username": "fixture-user",
                "password": "fixture-password",
                "browser_api_token": "fixture-token",
            },
            "session": [],
        }
        asyncio.run(fetcher.do_fetch(entries, env, proxies=[]))

        self.assertEqual(3, len(requests))
        self.assertTrue(requests[0].url.endswith("/util/unicode"))
        self.assertEqual("http://sb-forum-browser:8766/api/signin", requests[1].url)
        self.assertEqual("POST", requests[1].method)
        self.assertNotIn("Cookie", requests[1].headers)
        password_body = (
            requests[1].body.decode("utf-8")
            if isinstance(requests[1].body, bytes)
            else requests[1].body
        )
        self.assertIn("fixture-user", password_body)
        self.assertIn("fixture-password", password_body)
        self.assertTrue(requests[2].url.endswith("/util/unicode"))

    def test_request_urls_contain_no_markdown_link_syntax(self):
        for entry in self.entries:
            url = entry["request"].get("url", "")
            self.assertNotRegex(url, r"\[[^\]]+\]\(https?://")

    def test_password_request_is_internal_and_does_not_send_cookie(self):
        password_request = self._password_request()
        self.assertEqual(
            "http://sb-forum-browser:8766/api/signin",
            password_request["request"]["url"],
        )
        self.assertEqual("POST", password_request["request"]["method"])
        self.assertNotIn(
            "Cookie",
            {header["name"] for header in password_request["request"]["headers"]},
        )
        token_headers = {
            header["name"]: header["value"]
            for header in password_request["request"]["headers"]
        }
        self.assertEqual("{{browser_api_token}}", token_headers["X-SB-Forum-Token"])
        rendered_body = self._render(
            password_request["request"]["data"],
            profile_name="fixture-profile",
            username="fixture-user",
            password="fixture-password",
        )
        self.assertIn('"profile_name": "fixture-profile"', rendered_body)
        self.assertIn('"username": "fixture-user"', rendered_body)
        self.assertIn('"password": "fixture-password"', rendered_body)

    def test_external_cookie_requests_use_curl_cffi_fingerprint(self):
        external_requests = [
            entry
            for entry in self.request_entries
            if "sb.sb/" in entry["request"]["url"]
        ]
        self.assertEqual(3, len(external_requests))
        for entry in external_requests:
            headers = {
                header["name"].lower(): header["value"]
                for header in entry["request"]["headers"]
            }
            self.assertEqual(
                "{{browser_fingerprint | default('chrome', true)}}",
                headers["x-qd-impersonate"],
            )
            self.assertEqual(
                "{{browser_user_agent | default('auto', true)}}",
                headers["user-agent"],
            )
            self.assertEqual("{{cookie}}", headers["cookie"])

    def test_only_expected_ascii_task_variables_are_referenced(self):
        variables = set()
        extracted = set()

        for entry in self.entries:
            for source in self._all_sources(entry):
                if not source:
                    continue
                if CONTROL_PATTERN.fullmatch(source):
                    continue
                ast = self.jinja.parse(source)
                variables.update(meta.find_undeclared_variables(ast) - extracted)
            extracted.update(
                item["name"] for item in self._rule(entry).get("extract_variables", [])
            )

        user_variables = variables - {"str"}
        self.assertEqual(
            {
                "login_mode",
                "cookie",
                "username",
                "password",
                "profile_name",
                "browser_fingerprint",
                "browser_user_agent",
                "browser_api_token",
            },
            user_variables,
        )
        for variable in user_variables:
            self.assertRegex(variable, ASCII_NAME)

    def test_csrf_and_idempotent_rules_are_conservative(self):
        cookie_get = self._cookie_get_request()
        cookie_post = self._cookie_post_request()
        cookie_final = self._cookie_final_request()

        csrf_pattern = next(
            item["re"]
            for item in self._rule(cookie_get)["extract_variables"]
            if item["name"] == "csrf_token"
        )
        csrf_match = re.search(
            csrf_pattern,
            '<input type="hidden" name="_csrf" value="fixture-csrf">',
        )
        self.assertIsNotNone(csrf_match)
        self.assertEqual("fixture-csrf", csrf_match.group(1))

        post_success = [item["re"] for item in self._rule(cookie_post)["success_asserts"]]
        self.assertTrue(any(re.search(pattern, "今日已签到") for pattern in post_success))
        self.assertTrue(any(re.search(pattern, "签到成功") for pattern in post_success))

        final_success = self._rule(cookie_final)["success_asserts"]
        self.assertEqual("content", final_success[0]["from"])
        self.assertTrue(re.search(final_success[0]["re"], "今日已签到"))
        self.assertFalse(re.search(final_success[0]["re"], "登录页"))

        failure_text = "请先登录"
        failure_patterns = [item["re"] for item in self._rule(cookie_final)["failed_asserts"]]
        self.assertTrue(any(re.search(pattern, failure_text) for pattern in failure_patterns))

    def test_cookie_page_rules_scope_login_failure_to_login_page_signals(self):
        """正常签到页可带登录导航，真正登录表单仍必须失败。"""
        cookie_get = self._cookie_get_request()
        cookie_post = self._cookie_post_request()
        cookie_final = self._cookie_final_request()
        benign_pages = {
            id(cookie_get): (
                '<a href="/login/">登录</a>'
                '<script>const loginPath = "/login/";</script>'
                '<input type="hidden" name="_csrf" value="fixture-csrf">'
                '<div data-checked-in="true">今日已签到</div>'
            ),
            id(cookie_post): (
                '<a href="/login/">login</a>'
                '<div>签到成功</div>'
            ),
            id(cookie_final): (
                '<a href="/login/">登录</a>'
                '<div>今日已签到</div>'
            ),
        }
        login_page = (
            '<form method="post" action="/login/">'
            '<input type="hidden" name="_csrf" value="fixture-csrf">'
            '<input name="username" type="text">'
            '<input name="password" type="password">'
            '</form><div>今日已签到</div>'
        )

        for entry in (cookie_get, cookie_post, cookie_final):
            fetcher, request, rule, env = self._build_request(entry, {"cookie": "fixture"})
            benign_response = self._html_response(request, benign_pages[id(entry)])
            success, message = fetcher.run_rule(benign_response, rule, env)
            self.assertTrue(success, f"benign page rejected by {entry['request']['method']}: {message}")

            login_response = self._html_response(request, login_page)
            success, message = fetcher.run_rule(login_response, rule, env)
            self.assertFalse(success)
            self.assertIn("failed_asserts", message)

    def test_authenticated_signin_page_allows_cloudflare_username_and_native_button(self):
        """签到榜用户名为 Cloudflare 时，不应被当成 Cloudflare 挑战页。"""
        cookie_get = self._cookie_get_request()
        cookie_post = self._cookie_post_request()
        cookie_final = self._cookie_final_request()
        pages = {
            id(cookie_get): (
                '<input type="hidden" name="_csrf" value="fixture-csrf">'
                '<div class="leaderboard">'
                '<a href="/user/1"><img src="/avatar/Cloudflare.png" alt="Cloudflare">'
                'Cloudflare</a></div>'
                '<button class="btn-post" type="submit">\n 立即签到 \n</button>'
            ),
            id(cookie_post): (
                '<img src="/avatar/Cloudflare.png" alt="Cloudflare">'
                '<a href="/user/1">Cloudflare</a><div>签到成功</div>'
            ),
            id(cookie_final): (
                '<img src="/avatar/Cloudflare.png" alt="Cloudflare">'
                '<a href="/user/1">Cloudflare</a><div>今日已签到</div>'
            ),
        }

        for entry in (cookie_get, cookie_post, cookie_final):
            fetcher, request, rule, env = self._build_request(entry, {"cookie": "fixture"})
            response = self._html_response(request, pages[id(entry)])
            success, message = fetcher.run_rule(response, rule, env)
            self.assertTrue(
                success,
                f"authenticated Cloudflare username rejected by {entry['request']['method']}: {message}",
            )

    def test_cookie_get_native_markers_are_order_independent(self):
        """原生按钮标记与文案在 HTML 中顺序变化时仍应识别为签到页。"""
        cookie_get = self._cookie_get_request()
        bodies = (
            '<div>立即签到</div><button class="btn-post"></button>',
            '<button class="btn-post"></button><div>立即签到</div>',
        )
        for body in bodies:
            fetcher, request, rule, env = self._build_request(
                cookie_get, {"cookie": "fixture"}
            )
            response = self._html_response(
                request,
                '<input type="hidden" name="_csrf" value="fixture-csrf">' + body,
            )
            success, message = fetcher.run_rule(response, rule, env)
            self.assertTrue(success, message)

    def test_cookie_get_accepts_already_signed_page_without_button(self):
        """按钮在已签到状态下消失时，CSRF 与已签到标记仍足以识别页面。"""
        cookie_get = self._cookie_get_request()
        fetcher, request, rule, env = self._build_request(cookie_get, {"cookie": "fixture"})
        response = self._html_response(
            request,
            '<input type="hidden" name="_csrf" value="fixture-csrf">'
            '<div class="checkin-state" data-checked-in="true"></div>',
        )
        success, message = fetcher.run_rule(response, rule, env)
        self.assertTrue(success, message)

    def test_cookie_get_rejects_page_without_signin_or_signed_marker(self):
        """仅有 CSRF 的普通 200 页面不能被当作签到页。"""
        cookie_get = self._cookie_get_request()
        fetcher, request, rule, env = self._build_request(cookie_get, {"cookie": "fixture"})
        response = self._html_response(
            request,
            '<input type="hidden" name="_csrf" value="fixture-csrf">'
            '<div class="leaderboard"><a href="/user/1">normal-user</a></div>',
        )
        success, message = fetcher.run_rule(response, rule, env)
        self.assertFalse(success)
        self.assertIn("failed_asserts", message)

    def test_cookie_get_accepts_data_checked_in_marker_without_button(self):
        """签到状态属性即使为 false，也能证明页面是签到页。"""
        cookie_get = self._cookie_get_request()
        fetcher, request, rule, env = self._build_request(cookie_get, {"cookie": "fixture"})
        response = self._html_response(
            request,
            '<input type="hidden" name="_csrf" value="fixture-csrf">'
            '<div class="checkin-state" data-checked-in="false"></div>',
        )
        success, message = fetcher.run_rule(response, rule, env)
        self.assertTrue(success, message)

    def test_cookie_page_rules_keep_explicit_auth_challenge_signals(self):
        """Cap/Cloudflare/明确未登录提示仍应阻止签到页误判成功。"""
        entries = (
            self._cookie_get_request(),
            self._cookie_post_request(),
            self._cookie_final_request(),
        )
        markers = (
            "cap-widget",
            "Just a moment",
            "cf-chl-",
            "cdn-cgi/challenge-platform",
            "请先登录",
            "未登录",
        )
        for marker in markers:
            for entry in entries:
                fetcher, request, rule, env = self._build_request(
                    entry, {"cookie": "fixture"}
                )
                body = (
                    f"<div>{marker}</div>"
                    "<input type=\"hidden\" name=\"_csrf\" value=\"fixture-csrf\">"
                    "<div>今日已签到</div>"
                )
                response = self._html_response(request, body)
                success, message = fetcher.run_rule(response, rule, env)
                self.assertFalse(success, f"marker {marker!r} was accepted: {message}")
                self.assertIn("failed_asserts", message)

    def test_post_303_requires_signin_location_and_login_redirect_fails(self):
        post_rule = self._rule(self._cookie_post_request())
        success_location_patterns = [
            item["re"]
            for item in post_rule["success_asserts"]
            if item["from"] == "header-location"
        ]
        self.assertTrue(
            any(re.search(pattern, "/signin/") for pattern in success_location_patterns)
        )
        self.assertTrue(
            any(
                re.search(pattern, "https://sb.sb/signin/?from=checkin")
                for pattern in success_location_patterns
            )
        )
        self.assertFalse(
            any(
                re.search(pattern, "https://other.example/signin/")
                for pattern in success_location_patterns
            )
        )

        failed_asserts = post_rule["failed_asserts"]
        status_patterns = [item["re"] for item in failed_asserts if item["from"] == "status"]
        location_patterns = [
            item["re"] for item in failed_asserts if item["from"] == "header-location"
        ]

        self.assertFalse(any(re.search(pattern, "303") for pattern in status_patterns))
        self.assertFalse(any(re.search(pattern, "/signin/") for pattern in location_patterns))
        self.assertFalse(
            any(
                re.search(pattern, "https://sb.sb/signin/?from=checkin")
                for pattern in location_patterns
            )
        )
        self.assertTrue(any(re.search(pattern, "/login/") for pattern in location_patterns))
        self.assertTrue(
            any(
                re.search(pattern, "https://other.example/signin/")
                for pattern in location_patterns
            )
        )

    def test_fetcher_parse_and_safe_eval_select_only_the_requested_mode(self):
        """覆盖 QD 控制流，而不是只验证 Jinja 条件字符串。"""
        entries = json.loads(TEMPLATE_PATH.read_text(encoding="utf-8"))
        for index, entry in enumerate(entries, start=1):
            entry["idx"] = index

        blocks = list(Fetcher().parse(entries))
        mode_block = next(block for block in blocks if block["type"] == "if")

        def evaluate_condition(condition, variables):
            try:
                return bool(safe_eval(condition, variables))
            except (NameError, ValueError) as error:
                # Fetcher 将未定义变量按 false 分支处理。
                if "NameError" in str(error):
                    return False
                raise

        def evaluate_mode(variables):
            return evaluate_condition(mode_block["condition"], variables)
        self.assertFalse(evaluate_mode({}))
        self.assertFalse(evaluate_mode({"login_mode": "cookie"}))
        self.assertFalse(evaluate_mode({"login_mode": " Cookie "}))
        self.assertTrue(evaluate_mode({"login_mode": "password"}))
        self.assertTrue(evaluate_mode({"login_mode": " PASSWORD "}))

        password_requests = [
            block["entry"] for block in mode_block["true"] if block["type"] == "request"
        ]
        cookie_direct_requests = [
            block["entry"]
            for block in mode_block["false"]
            if block["type"] == "request"
        ]
        nested_signin_block = next(
            block for block in mode_block["false"] if block["type"] == "if"
        )
        self.assertEqual(1, len(password_requests))
        self.assertEqual(
            "http://sb-forum-browser:8766/api/signin",
            password_requests[0]["request"]["url"],
        )
        self.assertEqual(2, len(cookie_direct_requests))
        self.assertTrue(
            all(
                "sb.sb/signin/" in block["request"]["url"]
                for block in cookie_direct_requests
            )
        )

        # 已提取“今日已签到”时，嵌套 if 的 true 分支为空，POST 被跳过。
        self.assertTrue(
            evaluate_condition(
                nested_signin_block["condition"],
                {"already_signed_marker": "今日已签到"},
            )
        )
        self.assertEqual(0, len(nested_signin_block["true"]))

        # 未提取标记时，Fetcher 对 NameError 按 false 处理，恰好执行一次 POST。
        self.assertFalse(evaluate_condition(nested_signin_block["condition"], {}))
        self.assertEqual(1, len(nested_signin_block["false"]))
        post_entry = nested_signin_block["false"][0]["entry"]
        self.assertEqual("POST", post_entry["request"]["method"])
        self.assertEqual("https://sb.sb/signin/", post_entry["request"]["url"])

        # 两个直连 GET 加上未签到时的一个 POST，Cookie 路径总共三次论坛请求。
        self.assertEqual(3, len(cookie_direct_requests) + len(nested_signin_block["false"]))

    def test_sensitive_values_are_never_in_log_payload(self):
        log_entry = self.entries[-1]
        log_data = log_entry["request"]["data"]
        self.assertNotIn("{{cookie}}", log_data)
        self.assertNotIn("{{password}}", log_data)
        self.assertNotIn("{{csrf_token}}", log_data)
        self.assertNotIn("{{browser_api_token}}", log_data)

        rendered = self._render(
            log_data,
            login_mode="password",
            account_name="masked-user",
            sign_message="今日已签到",
            sign_reward="5",
            streak_days="3",
            cookie="COOKIE_FIXTURE_VALUE",
            password="PASSWORD_FIXTURE_VALUE",
        )
        self.assertNotIn("COOKIE_FIXTURE_VALUE", rendered)
        self.assertNotIn("PASSWORD_FIXTURE_VALUE", rendered)
        self.assertNotIn("fixture-csrf", rendered)
        self.assertIn("masked-user", rendered)

    def test_cookie_mode_uses_fixed_account_label_only(self):
        cookie_get_names = {
            item["name"]
            for item in self._rule(self._cookie_get_request()).get(
                "extract_variables", []
            )
        }
        self.assertNotIn("account_name", cookie_get_names)

        log_data = self.entries[-1]["request"]["data"]
        rendered = self._render(
            log_data,
            login_mode="cookie",
            account_name="raw-page-account",
            sign_message="今日已签到",
            sign_reward="5",
            streak_days="3",
        )
        self.assertIn("账号：已登录用户", rendered)
        self.assertNotIn("raw-page-account", rendered)

    def test_password_service_success_and_login_required_statuses_are_distinct(self):
        password_request = self._password_request()
        rule = self._rule(password_request)
        success_pattern = rule["success_asserts"][0]["re"]
        self.assertRegex('{"status":"already_signed"}', success_pattern)
        self.assertRegex('{"status":"success"}', success_pattern)

        failure_patterns = [item["re"] for item in rule["failed_asserts"]]
        self.assertTrue(any(re.search(pattern, '{"status":"login_required"}') for pattern in failure_patterns))
        self.assertTrue(any(re.search(pattern, "captcha required") for pattern in failure_patterns))


if __name__ == "__main__":
    unittest.main()
