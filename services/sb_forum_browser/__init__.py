"""烧饼论坛浏览器助手。

该包故意不在导入时启动 Playwright。这样 QD 主进程和离线测试不需要安装
Chromium，也不会因为浏览器依赖缺失而无法加载包。
"""

from .signin import (
    BASE_URL,
    LOGIN_URL,
    SIGNIN_URL,
    BrowserSignin,
    ProfileBusyError,
    ProfileLock,
    ProfileNameError,
    mask_username,
    resolve_profile_dir,
    run_signin,
    validate_profile_name,
)

__all__ = [
    "BASE_URL",
    "LOGIN_URL",
    "SIGNIN_URL",
    "BrowserSignin",
    "ProfileBusyError",
    "ProfileLock",
    "ProfileNameError",
    "mask_username",
    "resolve_profile_dir",
    "run_signin",
    "validate_profile_name",
]
