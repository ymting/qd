"""烧饼论坛浏览器助手的离线测试。

测试只使用 fake Playwright 页面，不需要安装或启动 Chromium，也不保存真实凭据。
"""

from __future__ import annotations

import http.client
import io
import json
import logging
import os
import stat
import tempfile
import threading
import unittest
from pathlib import Path

from services.sb_forum_browser.server import (
    API_TOKEN_HEADER,
    ApiTokenConfigError,
    LOGGER,
    build_server,
    process_signin,
    validate_payload,
)
from services.sb_forum_browser.signin import (
    BrowserSignin,
    ProfileNameError,
    mask_username,
    resolve_profile_dir,
)


class FakeResponse:
    def __init__(self, status: int = 200):
        self.status = status


class FakeLocator:
    def __init__(self, *, count: int = 0, on_click=None, value_store=None):
        self._count = count
        self._on_click = on_click
        self._value_store = value_store
        self.clicked = False

    async def count(self):
        return self._count

    def nth(self, _index):
        return self

    async def is_visible(self):
        return self._count > 0

    async def is_enabled(self):
        return True

    async def fill(self, value, **_kwargs):
        if self._value_store is not None:
            self._value_store.append(value)

    async def click(self, **_kwargs):
        self.clicked = True
        if self._on_click is not None:
            self._on_click()

    async def inner_text(self, **_kwargs):
        return ""


class FakePage:
    def __init__(self, *, initial_url, contents, redirect_to_login=False):
        self.url = initial_url
        self.contents = contents
        self.redirect_to_login = redirect_to_login
        self.signed = False
        self.goto_urls = []
        self.filled = []
        self.closed = False

    async def goto(self, url, **_kwargs):
        self.goto_urls.append(url)
        if self.redirect_to_login and url.endswith("/signin/"):
            self.url = "https://sb.sb/login/"
        else:
            self.url = url
        return FakeResponse()

    async def content(self):
        if self.signed:
            return self.contents["after"]
        return self.contents["before"]

    def locator(self, selector):
        if selector == "body":
            return FakeLocator(count=1)
        if selector in {
            'input[name="username"]',
            'input[name="email"]',
            'input[autocomplete="username"]',
            '#username',
            'input[type="text"]',
        }:
            return FakeLocator(count=1, value_store=self.filled)
        if selector in {
            'input[name="password"]',
            'input[autocomplete="current-password"]',
            'input[type="password"]',
        }:
            return FakeLocator(count=1, value_store=self.filled)
        if selector == 'button:has-text("签到")':
            return FakeLocator(count=1, on_click=lambda: setattr(self, "signed", True))
        if "two-factor" in selector or "otp" in selector or "2fa" in selector:
            return FakeLocator(count=0)
        if "验证码" in self.contents.get("before", "") and selector.startswith(
            'button[type="submit"]'
        ):
            return FakeLocator(count=0)
        return FakeLocator(count=0)

    async def wait_for_timeout(self, _milliseconds):
        return None


class FakeContext:
    def __init__(self, page):
        self.pages = [page]
        self.closed = False

    async def close(self):
        self.closed = True


class BrowserSigninTests(unittest.IsolatedAsyncioTestCase):
    async def test_already_signed_is_idempotent_and_extracts_public_metrics(self):
        page = FakePage(
            initial_url="https://sb.sb/signin/",
            contents={
                "before": "今日已签到，连续签到 3 天，获得积分 8",
                "after": "今日已签到",
            },
        )
        context = FakeContext(page)
        with tempfile.TemporaryDirectory() as directory:
            result = await BrowserSignin(
                data_dir=directory,
                profile_name="fixture-profile",
                username="fixture-user",
                password="fixture-only",
                context_factory=lambda _manager, _path: context,
                playwright_factory=lambda: object(),
                login_wait_ms=0,
            ).run()
        self.assertEqual(result["status"], "success")
        self.assertTrue(result["signed_in"])
        self.assertTrue(result["already_signed_in"])
        self.assertEqual(result["continuous_days"], 3)
        self.assertEqual(result["points"], 8)
        self.assertNotIn("fixture-only", str(result))

    async def test_native_signin_button_is_clicked_and_rechecked(self):
        page = FakePage(
            initial_url="https://sb.sb/signin/",
            contents={"before": "今日尚未签到 button", "after": "签到成功，连续签到 1 天"},
        )
        context = FakeContext(page)
        with tempfile.TemporaryDirectory() as directory:
            result = await BrowserSignin(
                data_dir=directory,
                profile_name="fixture-profile",
                username="fixture-user",
                password="fixture-only",
                context_factory=lambda _manager, _path: context,
                playwright_factory=lambda: object(),
                login_wait_ms=0,
            ).run()
        self.assertEqual(result["status"], "success")
        self.assertTrue(result["signed_in"])
        self.assertFalse(result["already_signed_in"])
        self.assertGreaterEqual(page.goto_urls.count("https://sb.sb/signin/"), 2)

    async def test_captcha_is_not_submitted_and_returns_login_required(self):
        page = FakePage(
            initial_url="https://sb.sb/login/",
            contents={
                "before": '<form>用户名 密码 Cap CAPTCHA 验证码</form>',
                "after": "",
            },
            redirect_to_login=True,
        )
        context = FakeContext(page)
        with tempfile.TemporaryDirectory() as directory:
            result = await BrowserSignin(
                data_dir=directory,
                profile_name="fixture-profile",
                username="fixture-user",
                password="fixture-only",
                context_factory=lambda _manager, _path: context,
                playwright_factory=lambda: object(),
                login_wait_ms=0,
            ).run()
        self.assertEqual(result["status"], "login_required")
        self.assertEqual(result["reason"], "captcha_required")
        self.assertNotIn("fixture-only", str(result))

    async def test_two_factor_is_mapped_to_login_required(self):
        page = FakePage(
            initial_url="https://sb.sb/login/",
            contents={"before": "登录 二步验证 one-time code", "after": ""},
            redirect_to_login=True,
        )
        context = FakeContext(page)
        with tempfile.TemporaryDirectory() as directory:
            result = await BrowserSignin(
                data_dir=directory,
                profile_name="fixture-profile",
                username="fixture-user",
                password="fixture-only",
                context_factory=lambda _manager, _path: context,
                playwright_factory=lambda: object(),
                login_wait_ms=0,
            ).run()
        self.assertEqual(result["status"], "login_required")
        self.assertEqual(result["reason"], "two_factor_required")


class ValidationAndApiTests(unittest.IsolatedAsyncioTestCase):
    def test_profile_name_rejects_path_traversal(self):
        for value in ("../escape", "..", "a/b", "a\\b", "a.b", "", "x" * 65):
            with self.assertRaises(ProfileNameError):
                resolve_profile_dir(tempfile.gettempdir(), value)

    def test_profile_is_created_under_private_profiles_root(self):
        with tempfile.TemporaryDirectory() as directory:
            profile = resolve_profile_dir(directory, "fixture-profile")
            self.assertEqual(profile, Path(directory).resolve() / "profiles" / "fixture-profile")
            self.assertTrue(profile.is_dir())
            self.assertTrue((profile.parent).is_dir())
            if os.name != "nt":
                self.assertEqual(stat.S_IMODE(profile.parent.stat().st_mode), 0o700)
                self.assertEqual(stat.S_IMODE(profile.stat().st_mode), 0o700)

    def test_novnc_container_listener_is_not_loopback_by_default(self):
        start_script = (
            Path(__file__).resolve().parents[1]
            / "services"
            / "sb_forum_browser"
            / "start.sh"
        ).read_text(encoding="utf-8")
        self.assertIn('NOVNC_HOST="${NOVNC_HOST:-0.0.0.0}"', start_script)
        self.assertIn('"$NOVNC_HOST:$NOVNC_PORT" "127.0.0.1:$VNC_PORT"', start_script)

    def test_username_mask_is_not_full_value(self):
        masked = mask_username("fixture-user@example.test")
        self.assertEqual(masked, "f***r@e***t")

    def test_api_rejects_cookie_and_unknown_fields(self):
        with self.assertRaises(ValueError):
            validate_payload(
                {
                    "profile_name": "default",
                    "username": "fixture-user",
                    "password": "fixture-only",
                    "cookie": "fixture-cookie",
                }
            )

    async def test_api_sanitizes_runner_result_and_does_not_echo_secret(self):
        captured = {}

        def runner_factory(**kwargs):
            captured.update(kwargs)

            async def run():
                return {
                    "status": "success",
                    "signed_in": True,
                    "username": "fixture-user",
                    "password": "fixture-only",
                    "cookie": "fixture-cookie",
                    "reason": "unexpected-secret",
                    "points": 4,
                }

            return run

        status, result = await process_signin(
            {
                "profile_name": "default",
                "username": "fixture-user",
                "password": "fixture-only",
            },
            data_dir=tempfile.gettempdir(),
            runner_factory=runner_factory,
        )
        self.assertEqual(status, 200)
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["points"], 4)
        self.assertNotIn("fixture-only", str(result))
        self.assertNotIn("fixture-cookie", str(result))
        self.assertEqual(captured["password"], "fixture-only")

    def test_server_requires_shared_token_at_startup(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ApiTokenConfigError):
                build_server(host="127.0.0.1", port=0, data_dir=directory, api_token="short")

    def test_real_http_layer_requires_token_before_parsing_body(self):
        api_token = "fixture_api_token_for_http_tests_123456"

        def runner_factory(**_kwargs):
            async def run():
                return {
                    "status": "success",
                    "signed_in": True,
                    "already_signed_in": True,
                    "username": "fixture-user",
                    "points": 6,
                }

            return run

        with tempfile.TemporaryDirectory() as directory:
            server = build_server(
                host="127.0.0.1",
                port=0,
                data_dir=directory,
                runner_factory=runner_factory,
                api_token=api_token,
            )
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            logger_stream = io.StringIO()
            log_handler = logging.StreamHandler(logger_stream)
            previous_level = LOGGER.level
            LOGGER.setLevel(logging.INFO)
            LOGGER.addHandler(log_handler)
            try:
                host, port = server.server_address

                def request(token=None, body=b"not-json"):
                    connection = http.client.HTTPConnection(host, port, timeout=5)
                    headers = {"Content-Type": "application/json"}
                    if token is not None:
                        headers[API_TOKEN_HEADER] = token
                    connection.request("POST", "/api/signin", body=body, headers=headers)
                    response = connection.getresponse()
                    response_body = response.read()
                    connection.close()
                    return response.status, response_body

                missing_status, missing_body = request()
                wrong_status, wrong_body = request("fixture_wrong_token")
                self.assertEqual(missing_status, 401)
                self.assertEqual(wrong_status, 401)
                self.assertEqual(missing_body, b'{"status":"unauthorized"}')
                self.assertEqual(missing_body, wrong_body)

                payload = json.dumps(
                    {
                        "profile_name": "fixture-profile",
                        "username": "fixture-user",
                        "password": "fixture-only",
                    }
                ).encode("utf-8")
                success_status, success_body = request(api_token, payload)
                self.assertEqual(success_status, 200)
                success = json.loads(success_body.decode("utf-8"))
                self.assertEqual(success["status"], "success")
                self.assertEqual(success["points"], 6)
                self.assertNotIn(api_token, success_body.decode("utf-8"))
                self.assertNotIn(api_token, logger_stream.getvalue())
            finally:
                LOGGER.removeHandler(log_handler)
                LOGGER.setLevel(previous_level)
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
