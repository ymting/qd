import json
import re
import unittest
from pathlib import Path
from urllib.parse import urlencode

from jinja2 import Environment, meta
from tornado.httputil import HTTPHeaders, parse_body_arguments


TEMPLATE_PATH = (
    Path(__file__).resolve().parents[1] / "templates" / "NodeSeek-可选签到模式.har"
)


class NodeSeekTemplateTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.entries = json.loads(TEMPLATE_PATH.read_text(encoding="utf-8"))
        cls.jinja = Environment(autoescape=False)

    def test_template_has_four_requests(self):
        self.assertEqual(4, len(self.entries))

    def test_user_variables_are_ascii_and_stable(self):
        variables = set()
        extracted = set()

        for entry in self.entries:
            request = entry["request"]
            sources = [
                request.get("method", ""),
                request.get("url", ""),
                request.get("data", ""),
            ]
            sources.extend(header.get("name", "") for header in request["headers"])
            sources.extend(header.get("value", "") for header in request["headers"])
            sources.extend(cookie.get("name", "") for cookie in request["cookies"])
            sources.extend(cookie.get("value", "") for cookie in request["cookies"])

            for source in sources:
                ast = self.jinja.parse(source)
                variables.update(meta.find_undeclared_variables(ast) - extracted)

            extracted.update(
                item["name"] for item in entry["rule"].get("extract_variables", [])
            )

        user_variables = variables - {
            "current_balance",
            "sign_message",
            "sign_reward",
            "sign_time",
        }
        self.assertEqual(
            {"browser_fingerprint", "browser_user_agent", "cookie", "sign_mode"},
            user_variables,
        )
        for variable in user_variables:
            self.assertRegex(variable, re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$"))

    def test_random_mode_renders_random_true(self):
        url = self.entries[1]["request"]["url"]
        rendered = self.jinja.from_string(url).render(sign_mode="random")
        self.assertEqual("https://www.nodeseek.com/api/attendance?random=true", rendered)

    def test_fixed_and_empty_mode_render_random_false(self):
        url = self.entries[1]["request"]["url"]
        template = self.jinja.from_string(url)

        self.assertEqual(
            "https://www.nodeseek.com/api/attendance?random=false",
            template.render(sign_mode="fixed"),
        )
        self.assertEqual(
            "https://www.nodeseek.com/api/attendance?random=false",
            template.render(sign_mode=""),
        )

    def test_mode_is_normalized_before_rendering(self):
        url = self.entries[1]["request"]["url"]
        rendered = self.jinja.from_string(url).render(sign_mode=" Random ")
        self.assertEqual("https://www.nodeseek.com/api/attendance?random=true", rendered)

    def test_ascii_variable_survives_task_form_round_trip(self):
        body = urlencode({"sign_mode": "random"}).encode("ascii")
        arguments = {}
        parse_body_arguments(
            "application/x-www-form-urlencoded",
            body,
            arguments,
            {},
            HTTPHeaders(),
        )

        self.assertEqual([b"random"], arguments["sign_mode"])


if __name__ == "__main__":
    unittest.main()
