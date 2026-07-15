#!/usr/bin/env python
# -*- encoding: utf-8 -*-

from datetime import timedelta
from io import BytesIO
from typing import Dict, Optional
from urllib.parse import quote

from tornado import httpclient
from tornado.httputil import HTTPHeaders

try:
    from curl_cffi.requests import AsyncSession
    from curl_cffi.requests.exceptions import RequestException

    CURL_CFFI_IMPORT_ERROR = None
    CURL_CFFI_REQUEST_ERRORS = (RequestException,)
except ImportError as exc:  # pragma: no cover - 由依赖缺失测试覆盖运行分支
    AsyncSession = None  # type: ignore
    CURL_CFFI_IMPORT_ERROR = exc
    CURL_CFFI_REQUEST_ERRORS = ()


IMPERSONATE_HEADER = "X-QD-Impersonate"
SUPPORTED_IMPERSONATES = frozenset(("chrome110",))


class CurlCffiClient:
    """使用浏览器 TLS 指纹发送请求，并适配为 Tornado 响应。"""

    @staticmethod
    def _build_timeout(request: httpclient.HTTPRequest):
        request_timeout = request.request_timeout
        connect_timeout = request.connect_timeout
        if request_timeout is None:
            return None
        if connect_timeout is None or request_timeout <= 0:
            return request_timeout

        # curl_cffi 的元组超时会将连接和读取时间相加，这里保持 QD 的总超时语义。
        connect_timeout = min(connect_timeout, request_timeout)
        return connect_timeout, max(request_timeout - connect_timeout, 0)

    @staticmethod
    def _build_proxy_url(proxy: Optional[Dict]) -> Optional[str]:
        if not proxy or not proxy.get("host"):
            return None

        scheme = proxy.get("scheme") or "http"
        host = str(proxy["host"])
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"

        username = proxy.get("username")
        password = proxy.get("password")
        auth = ""
        if username:
            auth = quote(str(username), safe="")
            if password is not None:
                auth += ":" + quote(str(password), safe="")
            auth += "@"

        port = f":{proxy['port']}" if proxy.get("port") else ""
        return f"{scheme}://{auth}{host}{port}"

    @staticmethod
    def _build_headers(response) -> HTTPHeaders:
        headers = HTTPHeaders()
        for name, value in response.headers.multi_items():
            headers.add(str(name), "" if value is None else str(value))
        return headers

    async def fetch(
        self,
        request: httpclient.HTTPRequest,
        *,
        impersonate: str,
        proxy: Optional[Dict] = None,
        download_size_limit: int,
    ) -> httpclient.HTTPResponse:
        if impersonate not in SUPPORTED_IMPERSONATES:
            raise httpclient.HTTPError(
                500,
                f'Unsupported X-QD-Impersonate value: "{impersonate}". '
                f"Supported values: {', '.join(sorted(SUPPORTED_IMPERSONATES))}",
            )
        if AsyncSession is None:
            raise httpclient.HTTPError(
                500,
                "curl_cffi transport is unavailable; install curl-cffi==0.15.0",
            ) from CURL_CFFI_IMPORT_ERROR

        body = bytearray()
        size_limit_exceeded = False

        def collect_content(chunk: bytes) -> int:
            nonlocal size_limit_exceeded
            if len(body) + len(chunk) > download_size_limit:
                size_limit_exceeded = True
                return 0
            body.extend(chunk)
            return len(chunk)

        # 二次过滤内部控制头，确保它不会因其他调用路径泄露到目标站点。
        if isinstance(request.headers, HTTPHeaders):
            request_headers = request.headers.get_all()
        else:
            request_headers = request.headers.items()
        headers = [
            (name, value)
            for name, value in request_headers
            if name.lower() != IMPERSONATE_HEADER.lower()
        ]

        try:
            async with AsyncSession(trust_env=False, raise_for_status=False) as session:
                response = await session.request(
                    method=request.method,
                    url=request.url,
                    data=request.body,
                    headers=headers,
                    timeout=self._build_timeout(request),
                    allow_redirects=request.follow_redirects,
                    max_redirects=request.max_redirects,
                    proxy=self._build_proxy_url(proxy),
                    verify=request.validate_cert,
                    impersonate=impersonate,
                    content_callback=collect_content,
                    discard_cookies=True,
                )
        except CURL_CFFI_REQUEST_ERRORS as exc:
            if size_limit_exceeded:
                raise httpclient.HTTPError(
                    599,
                    f"curl_cffi response exceeded download size limit "
                    f"({download_size_limit} bytes)",
                ) from exc
            raise httpclient.HTTPError(
                599, f"curl_cffi request failed: {exc}"
            ) from exc

        if size_limit_exceeded:
            raise httpclient.HTTPError(
                599,
                f"curl_cffi response exceeded download size limit "
                f"({download_size_limit} bytes)",
            )

        content = bytes(body) if body else response.content
        if len(content) > download_size_limit:
            raise httpclient.HTTPError(
                599,
                f"curl_cffi response exceeded download size limit "
                f"({download_size_limit} bytes)",
            )

        elapsed = getattr(response, "elapsed", timedelta())
        request_time = elapsed.total_seconds() if elapsed else 0.0
        return httpclient.HTTPResponse(
            request=request,
            code=response.status_code,
            reason=response.reason,
            headers=self._build_headers(response),
            buffer=BytesIO(content),
            effective_url=str(response.url),
            request_time=request_time,
            time_info={},
        )
