import ast
import logging
import os
import pathlib
import tempfile
import unittest
from unittest import mock

from modules import task_manager


class CookiePathResolutionTests(unittest.TestCase):
    def test_bilibili_cookie_path_uses_application_root(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            expected = pathlib.Path(temp_dir) / "cookies" / "bili_cookies.json"

            with mock.patch.object(task_manager, "get_app_root_dir", return_value=temp_dir):
                resolved = task_manager.resolve_cookie_file_path(
                    "cookies/bili_cookies.json",
                    "cookies/bili_cookies.json",
                    service_name="Bilibili",
                )

            self.assertEqual(os.path.normpath(resolved), os.path.normpath(str(expected)))

    def test_bilibili_call_sites_use_shared_cookie_path_resolver(self):
        root = pathlib.Path(__file__).resolve().parents[1]
        app_tree = ast.parse((root / "app.py").read_text(encoding="utf-8"))
        task_manager_tree = ast.parse(
            (root / "modules" / "task_manager.py").read_text(encoding="utf-8")
        )

        functions = {
            node.name: node
            for tree in (app_tree, task_manager_tree)
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name in {
                "bilibili_qrcode_start",
                "bilibili_qrcode_status",
                "_do_upload_to_bilibili",
            }
        }

        self.assertEqual(
            set(functions),
            {"bilibili_qrcode_start", "bilibili_qrcode_status", "_do_upload_to_bilibili"},
        )
        for function_node in functions.values():
            called_names = {
                node.func.id
                for node in ast.walk(function_node)
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
            }
            referenced_names = {
                node.id for node in ast.walk(function_node) if isinstance(node, ast.Name)
            }
            self.assertIn("resolve_cookie_file_path", called_names)
            self.assertNotIn("__file__", referenced_names)

    def test_configured_youtube_cookie_path_has_priority_over_legacy_file(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = pathlib.Path(temp_dir)
            configured = root / "cookies" / "yt_cookies.txt"
            legacy = root / "config" / "yt_cookies.txt"
            configured.parent.mkdir(parents=True)
            legacy.parent.mkdir(parents=True)
            configured.write_text("configured", encoding="utf-8")
            legacy.write_text("legacy", encoding="utf-8")

            with mock.patch.object(task_manager, "get_app_root_dir", return_value=temp_dir):
                resolved = task_manager.resolve_youtube_cookies_path(
                    {"YOUTUBE_COOKIES_PATH": "cookies/yt_cookies.txt"},
                    logging.getLogger("test"),
                )

            self.assertEqual(os.path.normpath(resolved), os.path.normpath(str(configured)))

    def test_legacy_config_cookie_is_used_only_when_configured_path_is_missing(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = pathlib.Path(temp_dir)
            legacy = root / "config" / "yt_cookies.txt"
            legacy.parent.mkdir(parents=True)
            legacy.write_text("legacy", encoding="utf-8")
            logger = mock.Mock()

            with mock.patch.object(task_manager, "get_app_root_dir", return_value=temp_dir):
                resolved = task_manager.resolve_youtube_cookies_path(
                    {"YOUTUBE_COOKIES_PATH": "cookies/yt_cookies.txt"},
                    logger,
                )

            self.assertEqual(os.path.normpath(resolved), os.path.normpath(str(legacy)))
            logger.warning.assert_called_once()
            self.assertIn("旧版路径", logger.warning.call_args.args[0])

    def test_frozen_internal_cookie_is_supported_as_last_compatibility_fallback(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = pathlib.Path(temp_dir)
            legacy = root / "_internal" / "cookies" / "yt_cookies.txt"
            legacy.parent.mkdir(parents=True)
            legacy.write_text("legacy-internal", encoding="utf-8")

            with mock.patch.object(task_manager, "get_app_root_dir", return_value=temp_dir), mock.patch.object(
                task_manager.sys, "frozen", True, create=True
            ):
                resolved = task_manager.resolve_youtube_cookies_path(
                    {"YOUTUBE_COOKIES_PATH": "cookies/yt_cookies.txt"},
                    logging.getLogger("test"),
                )

            self.assertEqual(os.path.normpath(resolved), os.path.normpath(str(legacy)))

    def test_settings_upload_uses_application_cookie_directory(self):
        app_path = pathlib.Path(__file__).resolve().parents[1] / "app.py"
        source = app_path.read_text(encoding="utf-8")
        module_ast = ast.parse(source, filename=str(app_path))
        function_node = next(
            node for node in module_ast.body
            if isinstance(node, ast.FunctionDef) and node.name == "_persist_settings_uploads"
        )
        namespace = {
            "os": os,
            "get_app_subdir": lambda name: str(pathlib.Path(self.temp_dir) / name),
            "logger": mock.Mock(),
        }
        exec(
            compile(ast.Module(body=[function_node], type_ignores=[]), str(app_path), "exec"),
            namespace,
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            self.temp_dir = temp_dir
            form_data = {}
            namespace["_persist_settings_uploads"](
                form_data,
                {
                    "youtube_cookies_file": {
                        "filename": "cookies.txt",
                        "content": b"# Netscape HTTP Cookie File\n",
                    }
                },
            )

            expected = pathlib.Path(temp_dir) / "cookies" / "yt_cookies.txt"
            self.assertTrue(expected.is_file())
            self.assertEqual(form_data["YOUTUBE_COOKIES_PATH"], "cookies/yt_cookies.txt")

    def test_browser_cookie_endpoints_use_application_cookie_directory(self):
        app_source = (pathlib.Path(__file__).resolve().parents[1] / "app.py").read_text(encoding="utf-8")

        self.assertGreaterEqual(app_source.count("cookies_dir = get_app_subdir('cookies')"), 3)
        self.assertNotIn(
            "cookies_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'cookies')",
            app_source,
        )


if __name__ == "__main__":
    unittest.main()
