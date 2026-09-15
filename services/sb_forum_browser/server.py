"""烧饼论坛浏览器助手的内部 HTTP API。

服务只接受 Profile 名称和账号密码，且所有错误都映射为固定的脱敏状态。
请求体、异常文本和浏览器页面内容绝不会进入日志或 HTTP 响应。
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import hmac
import json
import logging
import os
import sys
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Mapping

from .signin import (
    BrowserUnavailableError,
    ProfileBusyError,
    ProfileNameError,
    mask_username,
    run_signin,
    validate_profile_name,
)


LOGGER = logging.getLogger("sb_forum_browser")
API_TOKEN_ENV = "SB_FORUM_API_TOKEN"
API_TOKEN_HEADER = "X-SB-Forum-Token"
MIN_API_TOKEN_LENGTH = 24
UNAUTHORIZED_RESPONSE = {"status": "unauthorized"}
ALLOWED_REQUEST_KEYS = frozenset({"profile_name", "username", "password"})
MAX_BODY_BYTES = 32 * 1024
SAFE_STATUSES = frozenset({
    "success",
    "login_required",
    "error",
    "profile_busy",
    "browser_unavailable",
    "invalid_request",
    "timeout",
})
SAFE_REASONS = frozenset({
    "login_page",
    "captcha_required",
    "two_factor_required",
    "manual_login_required",
    "signin_control_not_found",
    "signin_not_confirmed",
    "access_challenge",
    "browser_timeout",
})


class RequestValidationError(ValueError):
    """API 请求不符合固定字段约束。"""


class ApiTokenConfigError(ValueError):
    """内部 API 共享密钥缺失或长度不足。"""


def validate_api_token(api_token: object) -> str:
    """校验启动配置，但绝不在异常文本中回显密钥内容。"""

    if not isinstance(api_token, str) or len(api_token) < MIN_API_TOKEN_LENGTH:
        raise ApiTokenConfigError(
            f"{API_TOKEN_ENV} is required and must be at least "
            f"{MIN_API_TOKEN_LENGTH} characters"
        )
    return api_token


def _resolve_api_token(api_token: str | None) -> str:
    configured = os.environ.get(API_TOKEN_ENV) if api_token is None else api_token
    return validate_api_token(configured)


def _token_matches(expected: str, supplied: object) -> bool:
    """使用固定时间比较共享密钥，且不将密钥放进日志/响应。"""

    if not isinstance(supplied, str):
        return False
    try:
        expected_bytes = expected.encode("utf-8")
        supplied_bytes = supplied.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return hmac.compare_digest(expected_bytes, supplied_bytes)


def validate_payload(payload: object) -> dict[str, str]:
    """验证 API 输入且不在异常中携带用户提交的值。"""

    if not isinstance(payload, dict):
        raise RequestValidationError("invalid request")
    if set(payload) != ALLOWED_REQUEST_KEYS:
        raise RequestValidationError("invalid request")
    profile_name = payload.get("profile_name")
    username = payload.get("username")
    password = payload.get("password")
    try:
        profile_name = validate_profile_name(profile_name)
    except ProfileNameError as exc:
        raise RequestValidationError("invalid request") from exc
    if not isinstance(username, str) or not username.strip() or len(username) > 256:
        raise RequestValidationError("invalid request")
    if not isinstance(password, str) or not password or len(password) > 4096:
        raise RequestValidationError("invalid request")
    if "\x00" in username or "\x00" in password:
        raise RequestValidationError("invalid request")
    return {"profile_name": profile_name, "username": username, "password": password}


def sanitize_result(result: object, *, username: str | None = None) -> dict[str, Any]:
    """只保留协议允许的状态和数值字段，防止 fake/异常结果泄露敏感值。"""

    source = result if isinstance(result, Mapping) else {}
    status = source.get("status")
    if status not in SAFE_STATUSES:
        status = "error"
    signed_in = bool(source.get("signed_in")) if status == "success" else False
    already = bool(source.get("already_signed_in")) if status == "success" else False
    sanitized: dict[str, Any] = {
        "status": status,
        "signed_in": signed_in,
        "already_signed_in": already,
    }
    masked = mask_username(source.get("username")) or mask_username(username)
    if masked:
        sanitized["username"] = masked
    reason = source.get("reason")
    if reason in SAFE_REASONS:
        sanitized["reason"] = reason
    for key, maximum in (("continuous_days", 100_000), ("points", 10_000_000)):
        value = source.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= maximum:
            sanitized[key] = value
    return sanitized


async def process_signin(
    payload: object,
    *,
    data_dir: str | os.PathLike[str] = "/data",
    runner_factory: Callable[..., Any] | None = None,
    headless: bool = False,
    timeout_ms: int = 15_000,
    login_wait_ms: int = 90_000,
) -> tuple[int, dict[str, Any]]:
    """处理一次 API 调用，返回 HTTP 状态码和脱敏 JSON。"""

    try:
        values = validate_payload(payload)
    except RequestValidationError:
        return HTTPStatus.BAD_REQUEST, {
            "status": "invalid_request",
            "signed_in": False,
            "already_signed_in": False,
        }

    try:
        if runner_factory is None:
            result = await run_signin(
                data_dir=data_dir,
                profile_name=values["profile_name"],
                username=values["username"],
                password=values["password"],
                headless=headless,
                timeout_ms=timeout_ms,
                login_wait_ms=login_wait_ms,
            )
        else:
            runner = runner_factory(
                data_dir=data_dir,
                profile_name=values["profile_name"],
                username=values["username"],
                password=values["password"],
                headless=headless,
                timeout_ms=timeout_ms,
                login_wait_ms=login_wait_ms,
            )
            run = getattr(runner, "run", runner)
            result = run()
            if asyncio.iscoroutine(result):
                result = await result
        return HTTPStatus.OK, sanitize_result(result, username=values["username"])
    except ProfileBusyError:
        return HTTPStatus.CONFLICT, {
            "status": "profile_busy",
            "signed_in": False,
            "already_signed_in": False,
        }
    except BrowserUnavailableError:
        return HTTPStatus.SERVICE_UNAVAILABLE, {
            "status": "browser_unavailable",
            "signed_in": False,
            "already_signed_in": False,
        }
    except (asyncio.TimeoutError, TimeoutError):
        return HTTPStatus.GATEWAY_TIMEOUT, {
            "status": "timeout",
            "signed_in": False,
            "already_signed_in": False,
        }
    except Exception:
        # 异常类型/文本可能包含 URL、输入值或 Playwright 请求详情，均不回显。
        return HTTPStatus.INTERNAL_SERVER_ERROR, {
            "status": "error",
            "signed_in": False,
            "already_signed_in": False,
        }


class SBForumHTTPServer(ThreadingHTTPServer):
    """带运行配置的线程 HTTP Server。"""

    allow_reuse_address = True
    daemon_threads = True

    def __init__(
        self,
        server_address: tuple[str, int],
        *,
        data_dir: str | os.PathLike[str] = "/data",
        runner_factory: Callable[..., Any] | None = None,
        api_token: str | None = None,
        headless: bool = False,
        timeout_ms: int = 15_000,
        login_wait_ms: int = 90_000,
    ):
        self.api_token = _resolve_api_token(api_token)
        self.data_dir = Path(data_dir)
        self.runner_factory = runner_factory
        self.headless = headless
        self.timeout_ms = timeout_ms
        self.login_wait_ms = login_wait_ms
        super().__init__(server_address, SigninRequestHandler)


class SigninRequestHandler(BaseHTTPRequestHandler):
    """仅实现内部 POST /api/signin，默认不记录请求头和请求体。"""

    server: SBForumHTTPServer
    server_version = "SBForumBrowser/1"
    sys_version = ""

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        if self.path != "/api/signin":
            self._write_json(HTTPStatus.NOT_FOUND, {"status": "not_found"})
            return
        # 先鉴权再读取正文，避免未授权请求把账号、密码或超大正文送入解析器。
        if not self._authorized():
            self._write_json(HTTPStatus.UNAUTHORIZED, UNAUTHORIZED_RESPONSE)
            return
        content_length = self.headers.get("Content-Length")
        try:
            length = int(content_length or "-1")
        except ValueError:
            length = -1
        if length < 0 or length > MAX_BODY_BYTES:
            self._write_json(HTTPStatus.BAD_REQUEST, {"status": "invalid_request"})
            return
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._write_json(HTTPStatus.BAD_REQUEST, {"status": "invalid_request"})
            return
        status, result = asyncio.run(
            process_signin(
                payload,
                data_dir=self.server.data_dir,
                runner_factory=self.server.runner_factory,
                headless=self.server.headless,
                timeout_ms=self.server.timeout_ms,
                login_wait_ms=self.server.login_wait_ms,
            )
        )
        self._write_json(status, result)

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        self._write_json(HTTPStatus.NOT_FOUND, {"status": "not_found"})

    def do_PUT(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        self._write_json(HTTPStatus.METHOD_NOT_ALLOWED, {"status": "method_not_allowed"})

    def do_DELETE(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        self._write_json(HTTPStatus.METHOD_NOT_ALLOWED, {"status": "method_not_allowed"})

    def log_message(self, format: str, *args: Any) -> None:
        # 只记录方法和路径，避免 BaseHTTPRequestHandler 的默认日志带出敏感 URL。
        with contextlib.suppress(Exception):
            LOGGER.info("request method=%s path=%s", self.command, self.path)

    def _authorized(self) -> bool:
        supplied = self.headers.get(API_TOKEN_HEADER)
        return _token_matches(self.server.api_token, supplied)

    def _write_json(self, status: int | HTTPStatus, value: Mapping[str, Any]) -> None:
        body = json.dumps(dict(value), ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8"
        )
        self.send_response(int(status))
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


def build_server(
    *,
    host: str = "0.0.0.0",
    port: int = 8766,
    data_dir: str | os.PathLike[str] = "/data",
    runner_factory: Callable[..., Any] | None = None,
    api_token: str | None = None,
    headless: bool = False,
    timeout_ms: int = 15_000,
    login_wait_ms: int = 90_000,
) -> SBForumHTTPServer:
    return SBForumHTTPServer(
        (host, int(port)),
        data_dir=data_dir,
        runner_factory=runner_factory,
        api_token=api_token,
        headless=headless,
        timeout_ms=timeout_ms,
        login_wait_ms=login_wait_ms,
    )


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except ValueError:
        return default


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="SB forum persistent browser helper")
    parser.add_argument("--host", default=os.environ.get("SB_FORUM_HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=_env_int("SB_FORUM_PORT", 8766))
    parser.add_argument("--data-dir", default=os.environ.get("SB_FORUM_DATA_DIR", "/data"))
    parser.add_argument("--headless", action="store_true", default=False)
    parser.add_argument(
        "--timeout-ms",
        type=int,
        default=_env_int("SB_FORUM_TIMEOUT_MS", 15_000),
    )
    parser.add_argument(
        "--login-wait-ms",
        type=int,
        default=_env_int("SB_FORUM_LOGIN_WAIT_MS", 90_000),
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=os.environ.get("SB_FORUM_LOG_LEVEL", "INFO"))
    try:
        server = build_server(
            host=args.host,
            port=args.port,
            data_dir=args.data_dir,
            headless=args.headless,
            timeout_ms=max(1, args.timeout_ms),
            login_wait_ms=max(0, args.login_wait_ms),
        )
    except ApiTokenConfigError as exc:
        # 配置错误必须清晰，但不能回显用户可能误填的共享密钥。
        print(str(exc), file=sys.stderr)
        return 2
    LOGGER.info("SB forum browser API listening on %s:%s", args.host, args.port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 0
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":  # pragma: no cover - 进程入口
    raise SystemExit(main())


__all__ = [
    "ALLOWED_REQUEST_KEYS",
    "API_TOKEN_ENV",
    "API_TOKEN_HEADER",
    "ApiTokenConfigError",
    "MAX_BODY_BYTES",
    "RequestValidationError",
    "SBForumHTTPServer",
    "SigninRequestHandler",
    "build_server",
    "main",
    "process_signin",
    "sanitize_result",
    "validate_api_token",
    "validate_payload",
]
