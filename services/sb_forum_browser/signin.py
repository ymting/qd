"""烧饼论坛的 Playwright 持久 Profile 登录与签到流程。

这里的浏览器仅用于建立真实登录态并操作站点原生页面。脚本不会读取、打印
或保存 Cookie、CSRF、验证码 Token 和密码，也不会尝试破解 Cap CAPTCHA。
Playwright 在函数调用时才延迟导入，避免 QD 和单元测试被浏览器依赖耦合。
"""

from __future__ import annotations

import asyncio
import contextlib
import html
import inspect
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Sequence
from urllib.parse import urlparse

try:  # Playwright 是可选依赖；离线测试通过 fake 工厂注入浏览器。
    from playwright.async_api import TimeoutError as PlaywrightTimeoutError
    from playwright.async_api import async_playwright
except ImportError:  # pragma: no cover - 由无 Playwright 的 QD/测试环境触发
    async_playwright = None  # type: ignore[assignment]

    class PlaywrightTimeoutError(Exception):
        """Playwright 未安装时供异常映射使用的兼容类型。"""


BASE_URL = os.environ.get("SB_FORUM_BASE_URL", "https://sb.sb").rstrip("/")
LOGIN_URL = f"{BASE_URL}/login/"
SIGNIN_URL = f"{BASE_URL}/signin/"

DEFAULT_TIMEOUT_MS = 15_000
DEFAULT_LOGIN_WAIT_MS = 90_000
MAX_PROFILE_NAME_LENGTH = 64
PROFILE_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

# 这些选择器只用于寻找站点原生登录/签到控件，不构造或伪造站点请求。
USERNAME_SELECTORS = (
    'input[name="username"]',
    'input[name="email"]',
    'input[autocomplete="username"]',
    '#username',
    'input[type="text"]',
)
PASSWORD_SELECTORS = (
    'input[name="password"]',
    'input[autocomplete="current-password"]',
    'input[type="password"]',
)
LOGIN_SUBMIT_SELECTORS = (
    'button[type="submit"]',
    'input[type="submit"]',
    'button:has-text("登录")',
    'button:has-text("登入")',
    'input[type="button"][value*="登录"]',
)
SIGNIN_BUTTON_SELECTORS = (
    'button:has-text("签到")',
    'input[type="submit"][value*="签到"]',
    'input[type="button"][value*="签到"]',
    '[role="button"]:has-text("签到")',
    'a:has-text("签到")',
    '[data-testid*="signin"]',
    '[data-action*="signin"]',
    '[id*="signin"]',
)
TWO_FACTOR_SELECTORS = (
    'input[autocomplete="one-time-code"]',
    'input[name*="otp" i]',
    'input[name*="2fa" i]',
    'input[name*="totp" i]',
    '[data-testid*="two-factor" i]',
    '[data-testid*="otp" i]',
    '[id*="two-factor" i]',
)

CAPTCHA_MARKERS = (
    r"captcha",
    r"cap-widget",
    r"data-cap",
    r"人机验证",
    r"请完成验证",
    r"验证码",
)
TWO_FACTOR_MARKERS = (
    r"二步验证",
    r"双因素",
    r"two[\s-]*factor",
    r"two[\s-]*step",
    r"\b2fa\b",
    r"one[\s-]*time[\s-]*(?:password|code)",
    r"passkey",
    r"安全密钥",
)
SIGNED_MARKERS = (
    r"今日已签到",
    r"今天已签到",
    r"已完成签到",
    r"签到成功",
    r"已签到",
    r"already[\s_-]*signed[\s_-]*in",
    r"signed[\s_-]*in[\s_-]*today",
)
LOGIN_PAGE_MARKERS = (
    r"登录",
    r"登入",
    r"sign[\s_-]*in",
    r"log[\s_-]*in",
)

SAFE_REASONS = {
    "login_page",
    "captcha_required",
    "two_factor_required",
    "manual_login_required",
    "signin_control_not_found",
    "signin_not_confirmed",
    "csrf_or_page_changed",
    "access_challenge",
    "browser_timeout",
}


class ProfileNameError(ValueError):
    """Profile 名称不符合安全约束。"""


class ProfileBusyError(RuntimeError):
    """同一个持久 Profile 已被其他浏览器进程占用。"""


class BrowserUnavailableError(RuntimeError):
    """当前运行环境没有可用的 Playwright。"""


class BrowserFlowTimeout(TimeoutError):
    """浏览器页面操作超时。"""


def validate_profile_name(profile_name: object) -> str:
    """严格校验 Profile 名称，拒绝路径分隔符、点段和控制字符。"""

    if not isinstance(profile_name, str):
        raise ProfileNameError("profile_name must be a string")
    if not profile_name or len(profile_name) > MAX_PROFILE_NAME_LENGTH:
        raise ProfileNameError("invalid profile_name")
    if profile_name in {".", ".."} or not PROFILE_NAME_RE.fullmatch(profile_name):
        raise ProfileNameError("invalid profile_name")
    return profile_name


def resolve_profile_dir(data_dir: str | os.PathLike[str], profile_name: str) -> Path:
    """将安全名称映射到 data_dir/profiles 内，并再次检查路径边界。

    第二次边界检查用于阻止已有的同名符号链接把 Chromium Profile 导向
    data_dir 之外；Profile 内可能保存认证状态，因此不能只依赖正则校验。
    """

    name = validate_profile_name(profile_name)
    data_root = Path(data_dir).expanduser().resolve(strict=False)
    root = (data_root / "profiles").resolve(strict=False)
    try:
        root.relative_to(data_root)
    except ValueError as exc:
        raise ProfileNameError("profiles path escapes data directory") from exc
    # Profile 保存认证态；根目录和单个账号目录均尽量收紧到仅当前用户可读写。
    root.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        os.chmod(root, 0o700)
    candidate = (root / name).resolve(strict=False)
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ProfileNameError("profile path escapes data directory") from exc
    if candidate == root:
        raise ProfileNameError("invalid profile path")
    candidate.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        os.chmod(candidate, 0o700)
    return candidate


class ProfileLock:
    """跨进程、单 Profile 文件锁。

    锁文件会留在 Profile 目录内，但锁本身由打开的文件描述符持有；进程
    异常退出时操作系统会释放锁，因此不会留下需要猜测 PID 的陈旧锁。
    """

    def __init__(self, profile_dir: str | os.PathLike[str]):
        self.profile_dir = Path(profile_dir)
        self.lock_path = self.profile_dir / ".sb-forum-browser.lock"
        self._handle: Any | None = None
        self._windows_lock = False

    def acquire(self) -> "ProfileLock":
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(OSError):
            os.chmod(self.profile_dir, 0o700)
        handle = self.lock_path.open("a+b")
        try:
            # Profile 中可能包含登录态，锁文件也不应对同机其他用户开放。
            with contextlib.suppress(OSError):
                os.chmod(self.lock_path, 0o600)
            if os.name == "nt":
                self._acquire_windows(handle)
            else:
                self._acquire_posix(handle)
        except (BlockingIOError, OSError) as exc:
            handle.close()
            raise ProfileBusyError("profile is already in use") from exc
        self._handle = handle
        return self

    @staticmethod
    def _acquire_posix(handle: Any) -> None:
        try:
            import fcntl
        except ImportError as exc:  # pragma: no cover - 仅无 fcntl 的极少数平台
            raise OSError("file locking is unavailable") from exc
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _acquire_windows(self, handle: Any) -> None:  # pragma: no cover - CI 通常 Linux
        import msvcrt

        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        self._windows_lock = True

    def release(self) -> None:
        handle, self._handle = self._handle, None
        if handle is None:
            return
        try:
            if os.name == "nt" and self._windows_lock:  # pragma: no cover
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                self._windows_lock = False
            elif os.name != "nt":
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()

    def __enter__(self) -> "ProfileLock":
        return self.acquire()

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.release()


@dataclass(frozen=True)
class SigninResult:
    """浏览器状态机对外暴露的脱敏结果。"""

    status: str
    signed_in: bool = False
    already_signed_in: bool = False
    username: str | None = None
    reason: str | None = None
    continuous_days: int | None = None
    points: int | None = None

    def as_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "status": self.status,
            "signed_in": self.signed_in,
            "already_signed_in": self.already_signed_in,
        }
        if self.username:
            result["username"] = self.username
        if self.reason:
            result["reason"] = self.reason
        if self.continuous_days is not None:
            result["continuous_days"] = self.continuous_days
        if self.points is not None:
            result["points"] = self.points
        return result


def mask_username(username: object) -> str | None:
    """返回适合日志/API 的用户名掩码，不回显完整账号。"""

    if not isinstance(username, str):
        return None
    value = username.strip()
    if not value:
        return None
    if "@" in value:
        local, domain = value.split("@", 1)
        masked_local = _mask_part(local)
        masked_domain = _mask_part(domain)
        return f"{masked_local}@{masked_domain}"
    return _mask_part(value)


def _mask_part(value: str) -> str:
    if len(value) <= 1:
        return "*"
    if len(value) == 2:
        return f"{value[0]}*"
    return f"{value[0]}***{value[-1]}"


def _safe_reason(reason: str | None) -> str | None:
    return reason if reason in SAFE_REASONS else None


def _bounded_int(value: object, maximum: int = 10_000_000) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and 0 <= value <= maximum:
        return value
    return None


def _metrics_from_text(raw_text: str) -> tuple[int | None, int | None]:
    """只从页面可公开文案提取有限数值，不返回原始 HTML/文本。"""

    text = html.unescape(re.sub(r"<[^>]+>", " ", raw_text or ""))
    text = re.sub(r"\s+", " ", text)
    continuous_days: int | None = None
    points: int | None = None
    continuous_match = re.search(
        r"(?:连续签到|连续|streak)[^0-9]{0,24}(\d{1,6})\s*(?:天|day|days)?",
        text,
        re.IGNORECASE,
    )
    if continuous_match:
        continuous_days = _bounded_int(int(continuous_match.group(1)), 100_000)
    points_match = re.search(
        r"(?:奖励|获得|积分|points?)[^0-9]{0,24}(\d{1,9})",
        text,
        re.IGNORECASE,
    )
    if points_match:
        points = _bounded_int(int(points_match.group(1)))
    return continuous_days, points


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


async def _invoke(function: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """调用真实 Playwright 或精简 fake，并兼容 fake 不接收 timeout 参数。"""

    try:
        result = function(*args, **kwargs)
    except TypeError:
        if not kwargs:
            raise
        result = function(*args)
    return await _maybe_await(result)


async def _attribute(value: Any, name: str, default: Any = None) -> Any:
    if value is None:
        return default
    attr = getattr(value, name, default)
    if callable(attr):
        try:
            return await _maybe_await(attr())
        except TypeError:
            return default
    return attr


async def _locator(page: Any, selector: str) -> Any | None:
    try:
        return await _maybe_await(page.locator(selector))
    except Exception:
        return None


async def _locator_count(locator: Any) -> int:
    if locator is None:
        return 0
    count = getattr(locator, "count", None)
    if count is None:
        return 1
    try:
        value = await _maybe_await(count())
    except Exception:
        return 0
    return value if isinstance(value, int) and value > 0 else 0


async def _locator_item(locator: Any, index: int) -> Any:
    nth = getattr(locator, "nth", None)
    if nth is None:
        return locator
    try:
        return await _maybe_await(nth(index))
    except Exception:
        return locator


async def _is_visible(locator: Any) -> bool:
    visible = getattr(locator, "is_visible", None)
    if visible is None:
        return True
    try:
        value = await _maybe_await(visible())
    except Exception:
        return False
    return bool(value)


async def _is_enabled(locator: Any) -> bool:
    enabled = getattr(locator, "is_enabled", None)
    if enabled is None:
        return True
    try:
        value = await _maybe_await(enabled())
    except Exception:
        return False
    return bool(value)


class BrowserSignin:
    """烧饼论坛的浏览器状态机。

    ``playwright_factory`` 可在测试中替换为 fake；生产环境不传该参数时才
    加载 Playwright。``login_wait_ms`` 为人工完成 CAPTCHA/二步验证预留等待
    时间，设为 0 可在离线测试中立即返回 ``login_required``。
    """

    def __init__(
        self,
        *,
        data_dir: str | os.PathLike[str] = "/data",
        profile_name: str = "default",
        username: str = "",
        password: str = "",
        headless: bool = False,
        timeout_ms: int = DEFAULT_TIMEOUT_MS,
        login_wait_ms: int = DEFAULT_LOGIN_WAIT_MS,
        playwright_factory: Callable[[], Any] | None = None,
        context_factory: Callable[[Any, Path], Any] | None = None,
    ):
        self.data_dir = Path(data_dir)
        self.profile_name = validate_profile_name(profile_name)
        self.username = username
        self.password = password
        self.headless = bool(headless)
        self.timeout_ms = max(1, int(timeout_ms))
        self.login_wait_ms = max(0, int(login_wait_ms))
        self.playwright_factory = playwright_factory
        self.context_factory = context_factory

    async def run(self) -> dict[str, Any]:
        """执行一次登录态检查与原生签到，并返回脱敏字典。"""

        profile_dir = resolve_profile_dir(self.data_dir, self.profile_name)
        try:
            # 锁覆盖整个 context 生命周期，防止同一 Profile 被并发启动。
            with ProfileLock(profile_dir):
                return await self._run_locked(profile_dir)
        except ProfileBusyError:
            raise
        except (BrowserFlowTimeout, PlaywrightTimeoutError, asyncio.TimeoutError):
            return self._result("error", reason="browser_timeout").as_dict()

    async def _run_locked(self, profile_dir: Path) -> dict[str, Any]:
        manager: Any | None = None
        context: Any | None = None
        try:
            manager, context = await self._open_context(profile_dir)
            page = await self._get_page(context)

            # 第一阶段：先打开签到页验证现有 Profile，未登录才进入登录页。
            response = await self._goto(page, SIGNIN_URL)
            response_result = await self._classify_response(response)
            if response_result is not None:
                return response_result
            if self._is_login_url(await self._page_url(page)):
                login_result = await self._login(page)
                if login_result is not None:
                    return login_result
                # 登录完成后必须重新打开签到页，不能把登录页当签到成功。
                response = await self._goto(page, SIGNIN_URL)
                response_result = await self._classify_response(response)
                if response_result is not None:
                    return response_result

            if self._is_login_url(await self._page_url(page)):
                return self._result("login_required", reason="login_page").as_dict()

            before_text = await self._page_snapshot(page)
            if self._looks_like_login_page(before_text):
                return self._result("login_required", reason="login_page").as_dict()
            if self._is_signed(before_text):
                return self._success(before_text, already=True)

            # 第二阶段：仅点击站点原生签到控件，不构造 POST、不伪造 CSRF。
            clicked = await self._click_signin_control(page)
            if not clicked:
                return self._result("error", reason="signin_control_not_found").as_dict()

            await self._settle_page(page)
            # 第三阶段：点击后重新 GET 并复查登录态与签到状态。
            response = await self._goto(page, SIGNIN_URL)
            response_result = await self._classify_response(response)
            if response_result is not None:
                return response_result
            if self._is_login_url(await self._page_url(page)):
                return self._result("login_required", reason="login_page").as_dict()
            after_text = await self._page_snapshot(page)
            if self._looks_like_login_page(after_text):
                return self._result("login_required", reason="login_page").as_dict()
            if self._is_signed(after_text):
                return self._success(after_text, already=False)
            return self._result("error", reason="signin_not_confirmed").as_dict()
        finally:
            await self._close_context(context)
            await self._stop_manager(manager)

    async def _open_context(self, profile_dir: Path) -> tuple[Any, Any]:
        factory = self.playwright_factory
        if factory is None:
            factory = async_playwright
        if factory is None:
            raise BrowserUnavailableError("Playwright is not installed")

        manager = factory()
        # 真实 async_playwright() 返回带 start() 的 context manager；fake 可以
        # 直接返回 manager 或直接提供 chromium。
        if hasattr(manager, "start"):
            manager = await _maybe_await(manager.start())
        if self.context_factory is not None:
            context = await _maybe_await(self.context_factory(manager, profile_dir))
            return manager, context
        chromium = getattr(manager, "chromium", None)
        launcher = getattr(chromium, "launch_persistent_context", None)
        if launcher is None:
            raise BrowserUnavailableError("Playwright Chromium is unavailable")
        context = await _invoke(
            launcher,
            str(profile_dir),
            headless=self.headless,
            timeout=self.timeout_ms,
        )
        return manager, context

    async def _get_page(self, context: Any) -> Any:
        pages = getattr(context, "pages", None)
        if callable(pages):
            pages = await _maybe_await(pages())
        if pages:
            return pages[0]
        new_page = getattr(context, "new_page", None)
        if new_page is None:
            raise BrowserUnavailableError("browser context has no page")
        return await _maybe_await(new_page())

    async def _close_context(self, context: Any | None) -> None:
        if context is None:
            return
        close = getattr(context, "close", None)
        if close is not None:
            with contextlib.suppress(Exception):
                await _maybe_await(close())

    async def _stop_manager(self, manager: Any | None) -> None:
        if manager is None:
            return
        stop = getattr(manager, "stop", None)
        if stop is not None:
            with contextlib.suppress(Exception):
                await _maybe_await(stop())

    async def _goto(self, page: Any, url: str) -> Any:
        goto = getattr(page, "goto", None)
        if goto is None:
            raise BrowserUnavailableError("browser page has no navigation method")
        try:
            response = await _invoke(
                goto,
                url,
                wait_until="domcontentloaded",
                timeout=self.timeout_ms,
            )
        except (PlaywrightTimeoutError, asyncio.TimeoutError) as exc:
            raise BrowserFlowTimeout from exc
        return response

    async def _classify_response(self, response: Any) -> dict[str, Any] | None:
        status = await _attribute(response, "status")
        if not isinstance(status, int):
            return None
        if status in {401}:
            return self._result("login_required", reason="login_page").as_dict()
        if status in {403, 429}:
            return self._result("error", reason="access_challenge").as_dict()
        if status >= 500:
            return self._result("error", reason="access_challenge").as_dict()
        return None

    async def _login(self, page: Any) -> dict[str, Any] | None:
        """填写凭据但不处理 CAPTCHA；交互状态统一映射为 login_required。"""

        if not isinstance(self.username, str) or not self.username.strip():
            return self._result("login_required", reason="login_page").as_dict()
        if not isinstance(self.password, str) or not self.password:
            return self._result("login_required", reason="login_page").as_dict()
        user_filled = await self._fill_first(page, USERNAME_SELECTORS, self.username)
        password_filled = await self._fill_first(page, PASSWORD_SELECTORS, self.password)
        if not user_filled or not password_filled:
            return self._result("login_required", reason="login_page").as_dict()

        snapshot = await self._page_snapshot(page)
        if await self._has_any_selector(page, TWO_FACTOR_SELECTORS) or self._has_marker(
            snapshot, TWO_FACTOR_MARKERS
        ):
            return await self._await_manual_login(page, "two_factor_required")
        if self._has_marker(snapshot, CAPTCHA_MARKERS):
            return await self._await_manual_login(page, "captcha_required")

        # 没有检测到验证码时才尝试站点原生提交；一旦出现交互挑战立即停下。
        submitted = await self._click_first(page, LOGIN_SUBMIT_SELECTORS)
        if not submitted:
            return self._result("login_required", reason="manual_login_required").as_dict()
        completed = await self._wait_for_authenticated(page)
        if completed:
            return None
        snapshot = await self._page_snapshot(page)
        if await self._has_any_selector(page, TWO_FACTOR_SELECTORS) or self._has_marker(
            snapshot, TWO_FACTOR_MARKERS
        ):
            return await self._await_manual_login(page, "two_factor_required")
        if self._has_marker(snapshot, CAPTCHA_MARKERS):
            return await self._await_manual_login(page, "captcha_required")
        return self._result("login_required", reason="manual_login_required").as_dict()

    async def _await_manual_login(self, page: Any, reason: str) -> dict[str, Any] | None:
        """等待用户在可见 noVNC 浏览器中完成挑战；不读取挑战答案。"""

        if self.login_wait_ms > 0:
            completed = await self._wait_for_authenticated(page)
            if completed:
                return None
        return self._result("login_required", reason=reason).as_dict()

    async def _wait_for_authenticated(self, page: Any) -> bool:
        if self.login_wait_ms <= 0:
            return not self._is_login_url(await self._page_url(page))
        wait_for_timeout = getattr(page, "wait_for_timeout", None)
        if wait_for_timeout is None:
            return not self._is_login_url(await self._page_url(page))
        deadline = time.monotonic() + self.login_wait_ms / 1000
        while time.monotonic() < deadline:
            if not self._is_login_url(await self._page_url(page)):
                snapshot = await self._page_snapshot(page)
                if not self._has_marker(snapshot, CAPTCHA_MARKERS + TWO_FACTOR_MARKERS):
                    return True
            remaining = max(50, min(500, int((deadline - time.monotonic()) * 1000)))
            try:
                await _invoke(wait_for_timeout, remaining)
            except (PlaywrightTimeoutError, asyncio.TimeoutError):
                break
        return False

    async def _fill_first(self, page: Any, selectors: Sequence[str], value: str) -> bool:
        for selector in selectors:
            target = await _locator(page, selector)
            count = await _locator_count(target)
            for index in range(min(count, 3)):
                item = await _locator_item(target, index)
                if not await _is_visible(item):
                    continue
                fill = getattr(item, "fill", None)
                if fill is None:
                    continue
                try:
                    await _invoke(fill, value, timeout=self.timeout_ms)
                except (PlaywrightTimeoutError, asyncio.TimeoutError) as exc:
                    raise BrowserFlowTimeout from exc
                except Exception:
                    continue
                return True
        return False

    async def _click_first(self, page: Any, selectors: Sequence[str]) -> bool:
        for selector in selectors:
            target = await _locator(page, selector)
            count = await _locator_count(target)
            for index in range(min(count, 5)):
                item = await _locator_item(target, index)
                if not await _is_visible(item) or not await _is_enabled(item):
                    continue
                click = getattr(item, "click", None)
                if click is None:
                    continue
                try:
                    await _invoke(click, timeout=self.timeout_ms)
                except (PlaywrightTimeoutError, asyncio.TimeoutError) as exc:
                    raise BrowserFlowTimeout from exc
                except Exception:
                    continue
                return True
        return False

    async def _click_signin_control(self, page: Any) -> bool:
        return await self._click_first(page, SIGNIN_BUTTON_SELECTORS)

    async def _settle_page(self, page: Any) -> None:
        wait_for_timeout = getattr(page, "wait_for_timeout", None)
        if wait_for_timeout is not None:
            with contextlib.suppress(Exception):
                await _invoke(wait_for_timeout, min(750, self.timeout_ms))

    async def _has_any_selector(self, page: Any, selectors: Sequence[str]) -> bool:
        for selector in selectors:
            target = await _locator(page, selector)
            if await _locator_count(target):
                return True
        return False

    async def _page_snapshot(self, page: Any) -> str:
        """获取页面文案用于状态判断；原文只在内存中短暂存在。"""

        chunks: list[str] = []
        content = getattr(page, "content", None)
        if content is not None:
            with contextlib.suppress(Exception):
                value = await _invoke(content)
                if isinstance(value, str):
                    chunks.append(value)
        body = await _locator(page, "body")
        inner_text = getattr(body, "inner_text", None)
        if inner_text is not None:
            with contextlib.suppress(Exception):
                value = await _invoke(inner_text, timeout=self.timeout_ms)
                if isinstance(value, str):
                    chunks.append(value)
        return "\n".join(chunks)

    async def _page_url(self, page: Any) -> str:
        value = getattr(page, "url", "")
        if callable(value):
            value = await _maybe_await(value())
        return value if isinstance(value, str) else ""

    @staticmethod
    def _is_login_url(url: str) -> bool:
        path = urlparse(url).path.lower()
        return path.rstrip("/") in {"/login", "/signin/login"} or "/login/" in path

    @staticmethod
    def _has_marker(text: str, markers: Sequence[str]) -> bool:
        return any(re.search(marker, text or "", re.IGNORECASE) for marker in markers)

    def _looks_like_login_page(self, text: str) -> bool:
        # 仅当页面同时像登录页且出现密码输入控件时判定，避免签到页上的“登录”导航
        # 被误判；URL 判定在调用方始终优先执行。
        return self._has_marker(text, LOGIN_PAGE_MARKERS) and bool(
            re.search(r"(?:password|密码|passwd)", text or "", re.IGNORECASE)
        )

    def _is_signed(self, text: str) -> bool:
        return self._has_marker(text, SIGNED_MARKERS)

    def _success(self, text: str, *, already: bool) -> dict[str, Any]:
        continuous_days, points = _metrics_from_text(text)
        return self._result(
            "success",
            already=already,
            signed_in=True,
            continuous_days=continuous_days,
            points=points,
        ).as_dict()

    def _result(
        self,
        status: str,
        *,
        reason: str | None = None,
        signed_in: bool = False,
        already: bool = False,
        continuous_days: int | None = None,
        points: int | None = None,
    ) -> SigninResult:
        return SigninResult(
            status=status,
            signed_in=signed_in,
            already_signed_in=already,
            username=mask_username(self.username),
            reason=_safe_reason(reason),
            continuous_days=_bounded_int(continuous_days, 100_000),
            points=_bounded_int(points),
        )


async def run_signin(
    *,
    data_dir: str | os.PathLike[str] = "/data",
    profile_name: str = "default",
    username: str = "",
    password: str = "",
    headless: bool = False,
    timeout_ms: int = DEFAULT_TIMEOUT_MS,
    login_wait_ms: int = DEFAULT_LOGIN_WAIT_MS,
    playwright_factory: Callable[[], Any] | None = None,
    context_factory: Callable[[Any, Path], Any] | None = None,
) -> dict[str, Any]:
    """函数式入口，便于内部 API 和测试调用。"""

    runner = BrowserSignin(
        data_dir=data_dir,
        profile_name=profile_name,
        username=username,
        password=password,
        headless=headless,
        timeout_ms=timeout_ms,
        login_wait_ms=login_wait_ms,
        playwright_factory=playwright_factory,
        context_factory=context_factory,
    )
    return await runner.run()


__all__ = [
    "BASE_URL",
    "LOGIN_URL",
    "SIGNIN_URL",
    "BrowserFlowTimeout",
    "BrowserSignin",
    "BrowserUnavailableError",
    "ProfileBusyError",
    "ProfileLock",
    "ProfileNameError",
    "SigninResult",
    "mask_username",
    "resolve_profile_dir",
    "run_signin",
    "validate_profile_name",
]
