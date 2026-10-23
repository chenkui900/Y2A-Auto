import ast
import hashlib
import pathlib
import re
import secrets
import threading
import unittest
from functools import wraps


def _load_token_helpers(*names):
    app_path = pathlib.Path(__file__).resolve().parents[1] / "app.py"
    source = app_path.read_text(encoding="utf-8")
    module_ast = ast.parse(source, filename=str(app_path))
    requested = set(names)
    if "_verify_tgbot_api_token" in requested:
        requested.update({"_is_valid_tgbot_api_token_format", "_verify_tgbot_api_token_hash"})

    selected = []
    for node in module_ast.body:
        if isinstance(node, ast.Assign):
            if any(
                isinstance(target, ast.Name)
                and target.id in {
                    "TG_BOT_API_TOKEN_PREFIX",
                    "TG_BOT_API_TOKEN_HASH_PREFIX",
                    "TG_BOT_API_TOKEN_HASH_ITERATIONS",
                    "_TG_BOT_API_TOKEN_RANDOM_RE",
                    "_TG_BOT_UPLOAD_RATE_LIMIT_WINDOW_SECONDS",
                    "_TG_BOT_UPLOAD_RATE_LIMIT_MAX_REQUESTS",
                    "_TG_BOT_UPLOAD_RATE_LIMIT_MAX_BUCKETS",
                    "_TG_BOT_UPLOAD_RATE_LIMIT_BUCKETS",
                    "_TG_BOT_UPLOAD_RATE_LIMIT_LOCK",
                }
                for target in node.targets
            ):
                selected.append(node)
        elif isinstance(node, ast.FunctionDef) and node.name in requested:
            selected.append(node)

    isolated_module = ast.Module(body=selected, type_ignores=[])
    namespace = {
        "hashlib": hashlib,
        "re": re,
        "secrets": secrets,
        "threading": threading,
        "wraps": wraps,
        "load_config": lambda: {},
    }
    exec(compile(isolated_module, str(app_path), "exec"), namespace)
    for name in names:
        assert name in namespace, f"{name} was not found in app.py"
    return [namespace[name] for name in names]


class TgbotTokenAuthTests(unittest.TestCase):
    def test_generated_token_has_expected_format_and_hash(self):
        generate_token, is_valid_format, hash_token = _load_token_helpers(
            "_generate_tgbot_api_token",
            "_is_valid_tgbot_api_token_format",
            "_hash_tgbot_api_token",
        )

        token = generate_token()
        token_hash = hash_token(token)

        self.assertTrue(token.startswith("y2a_tgbot_v1_"))
        self.assertTrue(is_valid_format(token))
        self.assertTrue(token_hash.startswith("pbkdf2_sha256:"))
        self.assertNotIn(token, token_hash)
        self.assertNotEqual(hash_token(token), token_hash)

    def test_verify_compares_against_stored_hash(self):
        generate_token, hash_token, verify_token = _load_token_helpers(
            "_generate_tgbot_api_token",
            "_hash_tgbot_api_token",
            "_verify_tgbot_api_token",
        )

        token = generate_token()
        config = {"TG_BOT_API_TOKEN_HASH": hash_token(token)}

        self.assertTrue(verify_token(token, config))
        self.assertFalse(verify_token(token + "x", config))
        self.assertFalse(verify_token("not-a-y2a-token", config))
        self.assertFalse(verify_token(token, {"TG_BOT_API_TOKEN_HASH": ""}))

    def test_extract_bearer_token_boundaries(self):
        extract_bearer_token, = _load_token_helpers("_extract_bearer_token")

        class RequestStub:
            def __init__(self, authorization):
                self.headers = {"Authorization": authorization}

        cases = [
            (None, ""),
            ("", ""),
            ("Basic abc", ""),
            ("Bearer", ""),
            ("Bearer    ", ""),
            ("Bearer abc", "abc"),
            ("bearer   abc  ", "abc"),
        ]
        for header, expected in cases:
            with self.subTest(header=header):
                extract_bearer_token.__globals__["request"] = RequestStub(header)
                self.assertEqual(extract_bearer_token(), expected)

    def test_upload_token_decorator_handles_auth_states(self):
        token_required, = _load_token_helpers("tgbot_upload_token_required")

        class RequestStub:
            method = "POST"

        def jsonify_stub(payload):
            return payload

        def endpoint():
            return {"success": True}

        namespace = token_required.__globals__
        namespace["request"] = RequestStub()
        namespace["jsonify"] = jsonify_stub
        namespace["_is_tgbot_upload_rate_limited"] = lambda: False
        decorated = token_required(endpoint)

        namespace["load_config"] = lambda: {}
        self.assertEqual(decorated()[1], 403)

        namespace["load_config"] = lambda: {"TG_BOT_API_TOKEN_HASH": "configured"}
        namespace["_extract_bearer_token"] = lambda: "invalid"
        namespace["_verify_tgbot_api_token"] = lambda token, config: False
        self.assertEqual(decorated()[1], 401)

        namespace["_verify_tgbot_api_token"] = lambda token, config: True
        self.assertEqual(decorated(), {"success": True})

        namespace["_is_tgbot_upload_rate_limited"] = lambda: True
        self.assertEqual(decorated()[1], 429)

    def test_token_state_does_not_expose_secret(self):
        token_state, = _load_token_helpers("_tgbot_api_token_state")
        state = token_state({
            "TG_BOT_API_TOKEN_HASH": "pbkdf2_sha256:260000$salt$" + "a" * 64,
            "TG_BOT_API_TOKEN_CREATED_AT": "2026-07-08 12:00:00",
            "TG_BOT_API_TOKEN_LAST4": "AbCd",
        })

        self.assertEqual(state, {
            "configured": True,
            "created_at": "2026-07-08 12:00:00",
            "last4": "AbCd",
        })

    def test_upload_rate_limit_buckets_are_bounded(self):
        rate_limited, = _load_token_helpers("_is_tgbot_upload_rate_limited")

        class RequestStub:
            remote_addr = "current"

        class TimeStub:
            @staticmethod
            def time():
                return 1000.0

        namespace = rate_limited.__globals__
        namespace["request"] = RequestStub()
        namespace["time"] = TimeStub
        namespace["_TG_BOT_UPLOAD_RATE_LIMIT_MAX_BUCKETS"] = 3
        buckets = namespace["_TG_BOT_UPLOAD_RATE_LIMIT_BUCKETS"]
        buckets.clear()
        buckets.update({
            "oldest": [990.0],
            "old": [995.0],
            "recent": [999.0],
        })

        self.assertFalse(rate_limited())
        self.assertLessEqual(len(buckets), 3)
        self.assertIn("current", buckets)
        self.assertNotIn("oldest", buckets)


if __name__ == "__main__":
    unittest.main()
