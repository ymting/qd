from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from libs.fetcher import Fetcher


class FetcherRenderedUrlErrorTests(unittest.IsolatedAsyncioTestCase):
    async def test_failed_assert_reports_rendered_request_url(self):
        template_url = (
            "https://api.haokun.cc"
            "{{ '/api/user/self' if login_mode == 'cookie' else '/api/user/login' }}"
        )
        rendered_url = "https://api.haokun.cc/api/user/self"
        env = {"variables": {"login_mode": "cookie"}, "session": []}
        response = SimpleNamespace(request=SimpleNamespace(url=rendered_url))
        fetcher = Fetcher()
        fetcher.fetch = AsyncMock(
            return_value={
                "success": False,
                "response": response,
                "env": env,
                "msg": "Fail assert status 401",
            }
        )
        template = [
            {
                "request": {
                    "method": "GET",
                    "url": template_url,
                    "headers": [],
                    "cookies": [],
                },
                "rule": {},
            }
        ]

        with self.assertRaises(Exception) as raised:
            await fetcher.do_fetch(template, env, proxies=[])

        message = str(raised.exception)
        self.assertIn(f"Request URL: {rendered_url}", message)
        self.assertNotIn(template_url, message)


if __name__ == "__main__":
    unittest.main()
