import importlib.util
import logging
import pathlib
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from modules import youtube_handler


class YouTubeRuntimeEntryTests(unittest.TestCase):
    def test_find_command_uses_python_module_in_development(self):
        completed = subprocess.CompletedProcess([], 0, stdout="2026.07.15", stderr="")
        with mock.patch.object(youtube_handler.sys, "frozen", False, create=True), mock.patch.object(
            youtube_handler.sys, "executable", "python.exe"
        ), mock.patch.object(youtube_handler.subprocess, "run", return_value=completed) as run_mock:
            command = youtube_handler._find_yt_dlp_command(logging.getLogger("test"))

        self.assertEqual(command, ["python.exe", "-m", "yt_dlp"])
        self.assertEqual(run_mock.call_args.args[0], ["python.exe", "-m", "yt_dlp", "--version"])

    def test_find_command_uses_internal_cli_when_frozen(self):
        completed = subprocess.CompletedProcess([], 0, stdout="2026.07.15", stderr="")
        with mock.patch.object(youtube_handler.sys, "frozen", True, create=True), mock.patch.object(
            youtube_handler.sys, "executable", "Y2A-Auto.exe"
        ), mock.patch.object(youtube_handler.subprocess, "run", return_value=completed) as run_mock:
            command = youtube_handler._find_yt_dlp_command(logging.getLogger("test"))

        self.assertEqual(
            command,
            ["Y2A-Auto.exe", youtube_handler._INTERNAL_YT_DLP_FLAG],
        )
        self.assertEqual(
            run_mock.call_args.args[0],
            ["Y2A-Auto.exe", youtube_handler._INTERNAL_YT_DLP_FLAG, "--version"],
        )

    def test_find_command_raises_when_all_entries_are_missing(self):
        failed = subprocess.CompletedProcess([], 1, stdout="", stderr="missing")
        with mock.patch.object(youtube_handler.sys, "frozen", False, create=True), mock.patch.object(
            youtube_handler.sys, "executable", "python.exe"
        ), mock.patch.object(youtube_handler.subprocess, "run", return_value=failed), mock.patch.object(
            youtube_handler, "_which", return_value=None
        ), mock.patch.object(youtube_handler.os.path, "exists", return_value=False):
            with self.assertRaisesRegex(youtube_handler.YtDlpUnavailableError, "本地 yt-dlp 不可用"):
                youtube_handler._find_yt_dlp_command(logging.getLogger("test"))

    def test_video_precheck_does_not_retry_missing_executable(self):
        logger = mock.Mock()
        with mock.patch.object(youtube_handler, "load_config", return_value={}), mock.patch.object(
            youtube_handler, "_append_yt_dlp_network_args", side_effect=lambda command, **_: command
        ), mock.patch.object(
            youtube_handler.subprocess, "run", side_effect=FileNotFoundError(2, "missing")
        ) as run_mock:
            with self.assertRaisesRegex(youtube_handler.YtDlpUnavailableError, "本地 yt-dlp 不可用"):
                youtube_handler.test_video_availability(
                    "https://www.youtube.com/watch?v=test",
                    ["missing-yt-dlp"],
                    logger=logger,
                )

        self.assertEqual(run_mock.call_count, 1)

    def test_download_preserves_local_yt_dlp_unavailable_message(self):
        logger = mock.Mock()
        with tempfile.TemporaryDirectory() as temp_dir, mock.patch.object(
            youtube_handler, "get_app_subdir", side_effect=lambda name: str(pathlib.Path(temp_dir) / name)
        ), mock.patch.object(
            youtube_handler, "setup_task_logger", return_value=logger
        ), mock.patch.object(
            youtube_handler,
            "_find_yt_dlp_command",
            side_effect=youtube_handler.YtDlpUnavailableError(
                youtube_handler._YT_DLP_UNAVAILABLE_MESSAGE
            ),
        ):
            success, error = youtube_handler.download_video_data(
                "https://www.youtube.com/watch?v=test",
                task_id="missing-runtime",
                skip_download=True,
            )

        self.assertFalse(success)
        self.assertEqual(error, youtube_handler._YT_DLP_UNAVAILABLE_MESSAGE)
        self.assertNotIn("视频不可用", error)

    def test_internal_dispatch_only_handles_reserved_flag(self):
        setup_path = pathlib.Path(__file__).resolve().parents[1] / "build-tools" / "setup_app.py"
        spec = importlib.util.spec_from_file_location("y2a_setup_app_test", setup_path)
        module = importlib.util.module_from_spec(spec)
        assert spec and spec.loader
        spec.loader.exec_module(module)

        self.assertIsNone(module.run_internal_yt_dlp_cli(["--version"]))

        fake_yt_dlp = SimpleNamespace(main=mock.Mock(return_value=None))
        with mock.patch.dict(sys.modules, {"yt_dlp": fake_yt_dlp}):
            exit_code = module.run_internal_yt_dlp_cli(
                [module.INTERNAL_YT_DLP_FLAG, "--version"]
            )

        self.assertEqual(exit_code, 0)
        fake_yt_dlp.main.assert_called_once_with(["--version"])

    def test_pyinstaller_spec_generator_collects_all_yt_dlp_submodules(self):
        root = pathlib.Path(__file__).resolve().parents[1]
        generator = (root / "build-tools" / "build_exe.py").read_text(encoding="utf-8")

        self.assertIn("collect_submodules('yt_dlp')", generator)


if __name__ == "__main__":
    unittest.main()
