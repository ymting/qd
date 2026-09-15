"""验证 X-QD-Direct 的内部凭据请求直连边界。"""

import asyncio
from io import BytesIO
from unittest.mock import AsyncMock, Mock, patch

from tornado import httpclient
from tornado.httputil import HTTPHeaders

import config
import libs.fetcher as fetcher_module
from libs.fetcher import Fetcher


PROXY = {
    "scheme": "http",
    "host": "proxy.example",
    "port": 8080,
    "username": "proxy-user",
    "password": "proxy-password",
}


def make_obj(headers):
    return {
        "request": {
            "method": "GET",
            "url": "http://sb-forum-browser:8766/api/signin",
            "headers": headers,
            "cookies": [],
        },
        "rule": {},
        "env": {"variables": {}, "session": []},
    }


def make_response(request):
    return httpclient.HTTPResponse(
        request=request,
        code=200,
        headers=HTTPHeaders({"Content-Type": "application/json"}),
        buffer=BytesIO(b'{"status":"success"}'),
        request_time=0.01,
    )


def make_curl_fetcher(request, response):
    fetcher = Fetcher.__new__(Fetcher)
    fetcher.download_size_limit = 1024
    fetcher.client = Mock(fetch=AsyncMock(return_value=response))
    fetcher.curl_cffi_client = Mock(fetch=AsyncMock(return_value=response))
    fetcher.build_request = Mock(
        return_value=(request, {}, {"variables": {}, "session": []})
    )
    return fetcher


def test_direct_header_is_removed_and_blocks_pycurl_proxy_attributes():
    obj = make_obj(
        [
            {"name": "X-QD-Direct", "value": "1"},
            {"name": "X-SB-Forum-Token", "value": "token-fixture"},
        ]
    )

    with (
        patch.object(fetcher_module, "pycurl", object()),
        patch.object(config, "proxy_direct_mode", ""),
    ):
        request, _, _ = Fetcher().build_request(obj, proxy=PROXY)

    assert request.headers.get("X-QD-Direct") is None
    assert request.headers["X-SB-Forum-Token"] == "token-fixture"
    assert request.force_direct is True
    assert request._qd_force_direct is True
    assert all(
        getattr(request, name, None) is None
        for name in ("proxy_host", "proxy_port", "proxy_username", "proxy_password")
    )


def test_normal_request_still_gets_pycurl_proxy_attributes():
    obj = make_obj([{"name": "Accept", "value": "application/json"}])

    with (
        patch.object(fetcher_module, "pycurl", object()),
        patch.object(config, "proxy_direct_mode", ""),
    ):
        request, _, _ = Fetcher().build_request(obj, proxy=PROXY)

    assert request.force_direct is False
    assert request.proxy_host == "proxy.example"
    assert request.proxy_port == 8080
    assert request.proxy_username == "proxy-user"
    assert request.proxy_password == "proxy-password"


def test_force_direct_curl_cffi_request_receives_no_proxy():
    request = httpclient.HTTPRequest(
        url="http://sb-forum-browser:8766/api/signin",
        method="GET",
        headers={},
        follow_redirects=False,
        max_redirects=0,
        validate_cert=False,
        connect_timeout=4,
        request_timeout=10,
    )
    request._qd_impersonate = "chrome110"
    request.force_direct = True
    response = make_response(request)
    fetcher = make_curl_fetcher(request, response)

    _, _, actual = asyncio.run(fetcher.build_response({}, proxy=PROXY))

    assert actual is response
    fetcher.client.fetch.assert_not_awaited()
    fetcher.curl_cffi_client.fetch.assert_awaited_once_with(
        request,
        impersonate="chrome110",
        proxy=None,
        download_size_limit=1024,
    )


def test_normal_curl_cffi_request_still_receives_task_proxy():
    request = httpclient.HTTPRequest(
        url="https://example.test/api",
        method="GET",
        headers={},
        follow_redirects=False,
        max_redirects=0,
        validate_cert=False,
        connect_timeout=4,
        request_timeout=10,
    )
    request._qd_impersonate = "chrome110"
    request.force_direct = False
    response = make_response(request)
    fetcher = make_curl_fetcher(request, response)

    with patch.object(config, "proxy_direct_mode", ""):
        asyncio.run(fetcher.build_response({}, proxy=PROXY))

    fetcher.curl_cffi_client.fetch.assert_awaited_once_with(
        request,
        impersonate="chrome110",
        proxy=PROXY,
        download_size_limit=1024,
    )
