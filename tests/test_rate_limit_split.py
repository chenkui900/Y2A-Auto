# -*- coding: utf-8 -*-
"""下载限速「动态分配」的守卫测试。

背景：用户需求是「并发设置多少，不代表一直留着平分带宽，而是根据实际在下载的
任务数动态计算每个任务的速度」——并发设 4 但只有 1 个任务在下载时，那 1 个任务
应占用全部带宽；同时 2 个下载就按需均分。

实现（见 modules/download_rate_controller 与 youtube_handler._run_yt_dlp_dynamic）：
- ``--concurrent-fragments`` 固定为 1（单任务只有 1 条连接），因此单任务限额
  就是每条连接的限额；
- 单任务限额 = 总限速 / max(1, 实际在下载的任务数)，由控制器实时计算；
- 任务开始/结束或设置变更时控制器置位重平衡信号，正在跑的 yt-dlp 以新速率
  断点续传重拉，无需重启服务。

本文件覆盖三件事：
1. 摊分数学：任何「实际活动下载数」下，总速度都不会超过设定值；
2. 配置边界：不限速 / 限速关闭 / 时段外都不得下发 --limit-rate；
3. 真实命令：download_video_data 拼出来的 yt-dlp 命令带上按实际活动数计算的
   --limit-rate 与 -N 1（单活动任务 = 全额，2 个活动任务 = 各半）。
"""
import pathlib
import tempfile
import unittest
from unittest import mock

from modules.concurrency import (
    MAX_DOWNLOAD_THREADS,
    split_download_rate,
)
from modules.download_rate_controller import (
    DownloadRateController,
    compute_per_task_rate,
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


def _controller_with_total(total, active_tasks=()):
    """构造一个不读真实 config.json 的控制器，总限速固定为 total。"""
    controller = DownloadRateController()
    controller._read_total = mock.Mock(return_value=total)
    for task_id in active_tasks:
        controller.register(task_id)
    return controller


class DynamicSplitMathTests(unittest.TestCase):
    """动态分配数学：按「实际活动下载数」摊分，而不是按配置的并发上限。"""

    def test_single_active_task_gets_full_bandwidth(self):
        # 并发设 4，但只有 1 个任务在下载 → 吃满 4M
        self.assertEqual(compute_per_task_rate(4 * MIB, 1), 4 * MIB)
        self.assertEqual(compute_per_task_rate(4 * MIB, 0), 4 * MIB)

    def test_active_tasks_share_bandwidth(self):
        self.assertEqual(compute_per_task_rate(4 * MIB, 2), 2 * MIB)
        self.assertEqual(compute_per_task_rate(4 * MIB, 3), int(4 * MIB / 3))
        self.assertEqual(compute_per_task_rate(4 * MIB, 4), 1 * MIB)

    def test_total_never_exceeds_setting_for_any_active_count(self):
        total = 8 * MIB
        for active in range(0, MAX_DOWNLOAD_THREADS + 2):
            per_task = compute_per_task_rate(total, active)
            self.assertIsNotNone(per_task)
            # 即便活动数超过并发上限，总速率也不会超过设定值
            self.assertLessEqual(per_task * max(1, active), total)
            self.assertGreaterEqual(per_task, 1)

    def test_no_limit_cases(self):
        self.assertIsNone(compute_per_task_rate(0, 3))
        self.assertIsNone(compute_per_task_rate(None, 3))
        self.assertIsNone(compute_per_task_rate(-5, 3))

    def test_split_download_rate_supports_active_count_override(self):
        # split_download_rate 保留静态摊分（按配置并发），也可显式指定实际活动数
        per_task, per_connection = split_download_rate(4 * MIB, _config(), active_count=1)
        self.assertEqual(per_task, 4 * MIB)
        self.assertEqual(per_connection, per_task)
        per_task, per_connection = split_download_rate(4 * MIB, _config(), active_count=2)
        self.assertEqual(per_task, 2 * MIB)

    def test_parse_and_format_round_trip(self):
        self.assertEqual(parse_rate_limit('1.5M'), int(1.5 * MIB))
        self.assertEqual(format_rate_limit(int(1.5 * MIB)), '1.5M')
        self.assertEqual(format_rate_limit(256 * 1024), '256K')


class StaticPacingFallbackTests(unittest.TestCase):
    """_download_pacing 按配置并发静态摊分（供设置页预览/兼容使用）。"""

    def test_pacing_static_by_configured_concurrency(self):
        per_task, per_connection = youtube_handler._download_pacing(_config())
        self.assertEqual(per_task, 1 * MIB)
        self.assertEqual(per_connection, 1 * MIB)

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
    """真实走一遍命令拼装：--limit-rate 按「实际活动下载数」动态计算，-N 固定为 1。"""

    def _run(self, config, total=4 * MIB, active_tasks=()):
        logger = mock.Mock(name='task_logger')
        controller = _controller_with_total(total, active_tasks=active_tasks)
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
                     youtube_handler, 'get_download_rate_controller', return_value=controller), \
                 mock.patch.object(
                     youtube_handler.subprocess, 'Popen', return_value=_FakeProcess()) as popen_mock:
                youtube_handler.download_video_data(
                    'https://www.youtube.com/watch?v=dQw4w9WgXcQ',
                    task_id='rate-split',
                    progress_callback=lambda info: None,
                )
        self.assertTrue(popen_mock.called, '没有真正启动 yt-dlp 进程')
        return popen_mock.call_args.args[0]

    def _limit_value(self, command):
        self.assertIn('--limit-rate', command)
        return command[command.index('--limit-rate') + 1]

    def test_single_active_download_gets_full_rate(self):
        # 没有其他任务在下载（register 由 task_manager 负责，直调时活动数为 0 按 1 算）
        # → 这 1 个任务吃满全部 4M，而不是旧实现的 4M÷4=1M
        command = self._run(_config())
        self.assertEqual(self._limit_value(command), '4M')
        self.assertIn('--concurrent-fragments', command)
        self.assertEqual(command[command.index('--concurrent-fragments') + 1], '1')

    def test_two_active_downloads_split_rate(self):
        # 控制器里已有 2 个注册中的下载任务（register 由 task_manager 在拉起前完成）
        # → 每个任务各分 2M，总和仍为 4M，不超标
        command = self._run(_config(), active_tasks=('rate-split', 'other-task'))
        self.assertEqual(self._limit_value(command), '2M')

    def test_command_without_rate_limit_has_no_limit_flag(self):
        command = self._run(_config(RATE_LIMIT_ENABLED=False), total=None)
        self.assertNotIn('--limit-rate', command)


if __name__ == '__main__':
    unittest.main()
