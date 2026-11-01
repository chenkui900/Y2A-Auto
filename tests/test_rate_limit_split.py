# -*- coding: utf-8 -*-
"""下载限速的摊分守卫测试。

背景：用户反馈「限速后下载速度还是超出限制」。根因是 yt-dlp 的 ``--limit-rate``
按「每条连接」生效，旧实现只按总限速跑、没按并发摊分，多任务并发时实际总速度 =
设定值 × 任务数。

现在改为：总限速只按「下载并发数量」一级摊分，且 ``--concurrent-fragments`` 固定为 1
（单任务只有 1 条连接），于是每个任务的限额 = 每条连接的限额 = 总限速 / 下载并发数量。
N 个任务同时跑时总速率恰好等于设定值，不会超标，且单连接限额足够大、yt-dlp 能稳定约束。

本文件覆盖三件事：
1. 摊分数学：任何并发数量下，总速度都不会超过设定值；
2. 配置边界：不限速 / 限速关闭 / 时段外都不得下发 --limit-rate；
3. 真实命令：download_video_data 拼出来的 yt-dlp 命令确实带上摊分后的值与 -N 1。
"""
import pathlib
import tempfile
import unittest
from unittest import mock

from modules.concurrency import (
    MAX_DOWNLOAD_THREADS,
    split_download_rate,
)
from modules.rate_limiter import format_rate_limit, parse_rate_limit
from modules import youtube_handler

MIB = 1024 * 1024


def _config(**overrides):
    config = {
        'YOUTUBE_DOWNLOAD_THREADS': 4,
        'DOWNLOAD_RATE_LIMIT': '4M',
        'RATE_LIMIT_ENABLED': True,
        'RATE_LIMIT_TIME_ENABLED': False,
    }
    config.update(overrides)
    return config


class SplitMathTests(unittest.TestCase):
    def test_split_by_tasks(self):
        # 4M 总限速 ÷ 4 个下载任务 = 每任务（每连接）1M
        per_task, per_connection = split_download_rate(4 * MIB, _config())
        self.assertEqual(per_task, 1 * MIB)
        self.assertEqual(per_connection, 1 * MIB)

    def test_total_never_exceeds_setting_for_any_thread_count(self):
        total = 8 * MIB
        for threads in range(1, MAX_DOWNLOAD_THREADS + 1):
            config = _config(YOUTUBE_DOWNLOAD_THREADS=threads)
            per_task, per_connection = split_download_rate(total, config)
            # 所有下载任务同时跑满时的总速率 = 每任务限额 × 并发数量
            self.assertLessEqual(per_task * threads, total, f'并发数量 {threads} 的总速率超出设定')
            self.assertEqual(per_connection, per_task, f'并发数量 {threads} 下每连接配额应等于每任务配额')
            self.assertGreaterEqual(per_connection, 1)

    def test_no_limit_cases(self):
        self.assertEqual(split_download_rate(0, _config()), (None, None))
        self.assertEqual(split_download_rate(None, _config()), (None, None))
        self.assertEqual(split_download_rate(-5, _config()), (None, None))

    def test_parse_and_format_round_trip(self):
        self.assertEqual(parse_rate_limit('1.5M'), int(1.5 * MIB))
        self.assertEqual(format_rate_limit(int(1.5 * MIB)), '1.5M')
        self.assertEqual(format_rate_limit(256 * 1024), '256K')


class DownloadPacingTests(unittest.TestCase):
    def test_pacing_returns_per_connection_limit(self):
        per_task, per_connection = youtube_handler._download_pacing(_config())
        self.assertEqual(per_task, 1 * MIB)
        self.assertEqual(per_connection, 1 * MIB)

    def test_pacing_with_single_task(self):
        per_task, per_connection = youtube_handler._download_pacing(
            _config(YOUTUBE_DOWNLOAD_THREADS=1, DOWNLOAD_RATE_LIMIT='800K'))
        self.assertEqual(per_task, 800 * 1024)
        self.assertEqual(per_connection, 800 * 1024)

    def test_disabled_rate_limit_adds_nothing(self):
        per_task, per_connection = youtube_handler._download_pacing(
            _config(RATE_LIMIT_ENABLED=False))
        self.assertIsNone(per_task)
        self.assertIsNone(per_connection)

    def test_outside_time_window_adds_nothing(self):
        config = _config(
            RATE_LIMIT_TIME_ENABLED=True,
            RATE_LIMIT_START_TIME='03:00',
            RATE_LIMIT_END_TIME='04:00',
        )
        with mock.patch('modules.rate_limiter.is_rate_limit_active', return_value=False):
            per_task, per_connection = youtube_handler._download_pacing(config)
        self.assertIsNone(per_task)
        self.assertIsNone(per_connection)


class _FakeProcess:
    def __init__(self, lines=(), returncode=0):
        self.pid = 4321
        self.returncode = returncode
        self.stdout = iter(lines)

    def wait(self, timeout=None):
        return self.returncode

    def poll(self):
        return self.returncode

    def kill(self):
        self.returncode = -9

    def terminate(self):
        self.returncode = -15


class DownloadCommandIntegrationTests(unittest.TestCase):
    """真实走一遍命令拼装，确认 --limit-rate 的值是「每条连接」的配额，且 -N 固定为 1。"""

    def _run(self, config):
        logger = mock.Mock(name='task_logger')
        with tempfile.TemporaryDirectory() as temp_dir:
            with mock.patch.object(youtube_handler, 'load_config', return_value=config), \
                 mock.patch.object(
                     youtube_handler, 'get_app_subdir',
                     side_effect=lambda name: str(pathlib.Path(temp_dir) / name)), \
                 mock.patch.object(youtube_handler, 'setup_task_logger', return_value=logger), \
                 mock.patch.object(youtube_handler, '_find_yt_dlp_command', return_value=['yt-dlp']), \
                 mock.patch.object(
                     youtube_handler, 'test_video_availability',
                     return_value=(True, 'id\ttitle', None)), \
                 mock.patch.object(youtube_handler, 'get_ffmpeg_path', return_value=None), \
                 mock.patch.object(youtube_handler, 'is_docker_env', return_value=False), \
                 mock.patch.object(
                     youtube_handler.subprocess, 'Popen', return_value=_FakeProcess()) as popen_mock:
                youtube_handler.download_video_data(
                    'https://www.youtube.com/watch?v=dQw4w9WgXcQ',
                    task_id='rate-split',
                    progress_callback=lambda info: None,
                )
        self.assertTrue(popen_mock.called, '没有真正启动 yt-dlp 进程')
        return popen_mock.call_args.args[0]

    def test_command_uses_split_limit_and_single_fragment(self):
        command = self._run(_config())
        self.assertIn('--limit-rate', command)
        limit_value = command[command.index('--limit-rate') + 1]
        # 4M 总限速 ÷ 4 个下载任务 = 每条连接 1M（单任务固定 1 条连接）
        self.assertEqual(limit_value, '1M')
        self.assertIn('--concurrent-fragments', command)
        self.assertEqual(command[command.index('--concurrent-fragments') + 1], '1')

    def test_command_without_rate_limit_has_no_limit_flag(self):
        command = self._run(_config(RATE_LIMIT_ENABLED=False))
        self.assertNotIn('--limit-rate', command)


if __name__ == '__main__':
    unittest.main()
