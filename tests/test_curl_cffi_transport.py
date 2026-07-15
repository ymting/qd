import asyncio
from datetime import timedelta
from io import BytesIO
from unittest.mock import AsyncMock, Mock, patch

import pytest
from tornado import httpclient
from tornado.httputil import HTTPHeaders

import config
import libs.curl_cffi_client as curl_cffi_client_module
import libs.fetcher as fetcher_module
from libs.curl_cffi_client import CurlCffiClient
from libs.fetcher import Fetcher


class FakeHeaders:
    def __init__(self, items=None):
        self._items = items or []

    def multi_items(self):
        return list(self._items)


class FakeCurlResponse:
    def __init__(
        self,
        *,
        status_code=200,
        reason="OK",
        content=b"response body",
        headers=None,
    ):
        self.status_code = status_code
        self.reason = reason
        self.content = content
        self.headers = FakeHeaders(headers)
        self.elapsed = timedelta(milliseconds=125)
        self.url = "https://www.nodeseek.com/api/attendance?random=false"


class FakeAsyncSession:
    response = FakeCurlResponse()
    error = None
    init_kwargs = None
    request_kwargs = None
    emit_content = True

    def __init__(self, **kwargs):
        type(self).init_kwargs = kwargs

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def request(self, **kwargs):
        type(self).request_kwargs = kwargs
        if type(self).error:
            raise type(self).error
        if type(self).emit_content and self.response.content:
            kwargs["content_callback"](self.response.content)
        return self.response


def make_request(headers=None):
    return httpclient.HTTPRequest(
        url="https://www.nodeseek.com/api/attendance?random=false",
        method="POST",
        headers=headers or {},
        body="",
        follow_redirects=False,
        max_redirects=0,
        validate_cert=False,
        connect_timeout=4,
        request_timeout=10,
    )


def make_tornado_response(request, code=200, body=b"ok"):
    return httpclient.HTTPResponse(
        request=request,
        code=code,
        headers=HTTPHeaders({"Content-Type": "application/json"}),
        buffer=BytesIO(body),
        request_time=0.1,
    )


def make_fetcher_for_response(request, response):
    fetcher = Fetcher.__new__(Fetcher)
    fetcher.download_size_limit = 1024
    fetcher.client = Mock(fetch=AsyncMock(return_value=response))
    fetcher.curl_cffi_client = Mock(fetch=AsyncMock(return_value=response))
    fetcher.build_request = Mock(
        return_value=(request, {}, {"variables": {}, "session": []})
    )
    return fetcher


def test_build_request_removes_internal_header_case_insensitively():
    obj = {
        "request": {
            "method": "GET",
            "url": "https://www.nodeseek.com/",
            "headers": [
                {"name": "x-qd-IMPERSONATE", "value": " chrome110 "},
                {"name": "User-Agent", "value": "test-agent"},
            ],
            "cookies": [],
        },
        "rule": {},
        "env": {"variables": {}, "session": []},
    }

    async def build_request():
        return Fetcher().build_request(obj)

    request, _, _ = asyncio.run(build_request())

    assert getattr(request, "_qd_impersonate") == "chrome110"
    assert request.headers.get("X-QD-Impersonate") is None
    assert request.headers["User-Agent"] == "test-agent"


def test_default_request_keeps_original_tornado_path():
    request = make_request()
    setattr(request, "_qd_impersonate", None)
    response = make_tornado_response(request)
    fetcher = make_fetcher_for_response(request, response)

    _, _, actual = asyncio.run(fetcher.build_response({}))

    assert actual is response
    fetcher.client.fetch.assert_awaited_once_with(request)
    fetcher.curl_cffi_client.fetch.assert_not_awaited()


def test_marked_request_uses_curl_cffi_only():
    request = make_request()
    setattr(request, "_qd_impersonate", "chrome110")
    response = make_tornado_response(request)
    fetcher = make_fetcher_for_response(request, response)

    _, _, actual = asyncio.run(fetcher.build_response({}))

    assert actual is response
    fetcher.client.fetch.assert_not_awaited()
    fetcher.curl_cffi_client.fetch.assert_awaited_once_with(
        request,
        impersonate="chrome110",
        proxy=None,
        download_size_limit=1024,
    )


def test_marked_request_error_never_downgrades_to_plain_tls():
    request = make_request()
    setattr(request, "_qd_impersonate", "chrome110")
    response = make_tornado_response(request)
    fetcher = make_fetcher_for_response(request, response)
    fetcher.curl_cffi_client.fetch.side_effect = httpclient.HTTPError(
        599, "fingerprint transport failed"
    )

    with (
        patch.object(fetcher_module, "pycurl", object()),
        patch.object(config, "allow_retry", True),
        patch.object(
            fetcher_module.simple_httpclient,
            "SimpleAsyncHTTPClient",
            side_effect=AssertionError("must not downgrade transport"),
        ) as simple_client,
    ):
        _, _, actual = asyncio.run(fetcher.build_response({}))

    assert actual.code == 599
    simple_client.assert_not_called()
    fetcher.client.fetch.assert_not_awaited()


def test_adapter_maps_request_and_preserves_duplicate_headers():
    request = make_request(
        {
            "X-QD-Impersonate": "chrome110",
            "Cookie": "session=test",
            "User-Agent": "test-agent",
        }
    )
    FakeAsyncSession.response = FakeCurlResponse(
        status_code=500,
        reason="Internal Server Error",
        content=b'{"success":false}',
        headers=[
            ("Set-Cookie", "first=1; Path=/"),
            ("Set-Cookie", "second=2; Path=/"),
            ("Content-Type", "application/json"),
        ],
    )
    FakeAsyncSession.error = None
    FakeAsyncSession.emit_content = True

    with patch.object(curl_cffi_client_module, "AsyncSession", FakeAsyncSession):
        response = asyncio.run(
            CurlCffiClient().fetch(
                request,
                impersonate="chrome110",
                proxy={
                    "scheme": "http",
                    "host": "proxy.example",
                    "port": 8080,
                    "username": "user@example",
                    "password": "p/ass",
                },
                download_size_limit=1024,
            )
        )

    assert FakeAsyncSession.init_kwargs == {
        "trust_env": False,
        "raise_for_status": False,
    }
    sent_headers = dict(FakeAsyncSession.request_kwargs["headers"])
    assert all(name.lower() != "x-qd-impersonate" for name in sent_headers)
    assert sent_headers["Cookie"] == "session=test"
    assert FakeAsyncSession.request_kwargs["impersonate"] == "chrome110"
    assert FakeAsyncSession.request_kwargs["allow_redirects"] is False
    assert FakeAsyncSession.request_kwargs["verify"] is False
    assert FakeAsyncSession.request_kwargs["timeout"] == (4, 6)
    assert (
        FakeAsyncSession.request_kwargs["proxy"]
        == "http://user%40example:p%2Fass@proxy.example:8080"
    )
    assert response.code == 500
    assert response.body == b'{"success":false}'
    assert response.error is not None
    assert response.headers.get_list("Set-Cookie") == [
        "first=1; Path=/",
        "second=2; Path=/",
    ]
    assert response.request_time == pytest.approx(0.125)


def test_unknown_impersonate_is_rejected_before_request():
    with pytest.raises(httpclient.HTTPError, match="Unsupported") as exc_info:
        asyncio.run(
            CurlCffiClient().fetch(
                make_request(),
                impersonate="chrome999",
                download_size_limit=1024,
            )
        )

    assert exc_info.value.code == 500


def test_missing_dependency_has_clear_error():
    with patch.object(curl_cffi_client_module, "AsyncSession", None):
        with pytest.raises(httpclient.HTTPError, match="install curl-cffi==0.15.0"):
            asyncio.run(
                CurlCffiClient().fetch(
                    make_request(),
                    impersonate="chrome110",
                    download_size_limit=1024,
                )
            )


def test_network_error_becomes_tornado_599():
    request_error = curl_cffi_client_module.CURL_CFFI_REQUEST_ERRORS[0]
    FakeAsyncSession.error = request_error("network down", 35)
    FakeAsyncSession.emit_content = False

    with patch.object(curl_cffi_client_module, "AsyncSession", FakeAsyncSession):
        with pytest.raises(httpclient.HTTPError, match="network down") as exc_info:
            asyncio.run(
                CurlCffiClient().fetch(
                    make_request(),
                    impersonate="chrome110",
                    download_size_limit=1024,
                )
            )

    assert exc_info.value.code == 599
    FakeAsyncSession.error = None


def test_download_size_limit_becomes_tornado_599():
    FakeAsyncSession.response = FakeCurlResponse(content=b"12345")
    FakeAsyncSession.error = None
    FakeAsyncSession.emit_content = True

    with patch.object(curl_cffi_client_module, "AsyncSession", FakeAsyncSession):
        with pytest.raises(httpclient.HTTPError, match="download size limit") as exc_info:
            asyncio.run(
                CurlCffiClient().fetch(
                    make_request(),
                    impersonate="chrome110",
                    download_size_limit=4,
                )
            )

    assert exc_info.value.code == 599


def test_curl_cffi_proxy_respects_existing_direct_rules():
    proxy = {"scheme": "http", "host": "proxy.example", "port": 8080}

    with (
        patch.object(config, "proxy_direct_mode", "regexp"),
        patch.object(config, "proxy_direct", r"nodeseek\.com"),
    ):
        assert Fetcher._get_curl_cffi_proxy("https://www.nodeseek.com/", proxy) is None
        assert Fetcher._get_curl_cffi_proxy("https://example.com/", proxy) is proxy
