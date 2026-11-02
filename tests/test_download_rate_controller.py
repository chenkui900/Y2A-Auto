# -*- coding: utf-8 -*-
"""download_rate_controller 的单元测试 + yt-dlp 动态重拉集成测试。

覆盖：
1. register/unregister 的活动计数与 generation/rebalance 信号；
2. per_task_rate_bytes 的动态分配数学（1 个活动任务吃满、多个均分）；
3. _run_yt_dlp_dynamic 在限速/活动数变化时，自动以新速率重新拉起 yt-dlp
   （断点续传），全程无需重启服务。
"""
import subprocess
import threading
import time
import unittest
from unittest import mock

from modules import youtube_handler
from modules.download_rate_controller import (
    DownloadRateController,
    compute_per_task_rate,
)

MIB = 1024 * 1024


class ControllerTrackingTests(unittest.TestCase):
    def test_register_unregister_tracks_active_count(self):
        controller = DownloadRateController()
        self.assertEqual(controller.active_count(), 0)
        controller.register('a')
        controller.register('b')
        self.assertEqual(controller.active_count(), 2)
        # 重复注册同一任务只算一次
        controller.register('a')
        self.assertEqual(controller.active_count(), 2)
        controller.unregister('a')
        self.assertEqual(controller.active_count(), 1)
        controller.unregister('not-registered')  # 注销不存在的任务不应报错
        self.assertEqual(controller.active_count(), 1)

    def test_generation_and_event_bump_on_changes(self):
        controller = DownloadRateController()
        controller.rebalance_event.clear()
        gen0 = controller.generation
        controller.register('a')
        self.assertEqual(controller.generation, gen0 + 1)
        self.assertTrue(controller.rebalance_event.is_set())
        gen1 = controller.generation
        controller.unregister('a')
        self.assertEqual(controller.generation, gen1 + 1)
        self.assertTrue(controller.rebalance_event.is_set())

    def test_per_task_rate_dynamic_allocation(self):
        controller = DownloadRateController()
        controller._read_total = mock.Mock(return_value=4 * MIB)
        # 没有活动任务时按 1 个算 → 全额
        self.assertEqual(controller.per_task_rate_bytes(), 4 * MIB)
        controller.register('a')
        self.assertEqual(controller.per_task_rate_bytes(), 4 * MIB)
        controller.register('b')
        self.assertEqual(controller.per_task_rate_bytes(), 2 * MIB)

    def test_per_task_rate_no_limit(self):
        controller = DownloadRateController()
        controller._read_total = mock.Mock(return_value=None)
        controller.register('a')
        self.assertIsNone(controller.per_task_rate_bytes())

    def test_notify_config_changed_bumps_generation(self):
        controller = DownloadRateController()
        controller.rebalance_event.clear()
        gen0 = controller.generation
        controller.notify_config_changed()
        self.assertEqual(controller.generation, gen0 + 1)
        self.assertTrue(controller.rebalance_event.is_set())

    def test_compute_per_task_rate_edge_cases(self):
        self.assertIsNone(compute_per_task_rate(0, 1))
        self.assertIsNone(compute_per_task_rate(None, 5))
        self.assertEqual(compute_per_task_rate(100, 0), 100)  # 无活动按 1 个算
        self.assertEqual(compute_per_task_rate(100, 3), 33)  # 向下取整，不超标


class _FakeDownloadProcess:
    """模拟 yt-dlp 子进程：stdout 在 release 前阻塞，terminate/kill 后结束。"""

    def __init__(self, pid, release_event):
        self.pid = pid
        self.returncode = None
        self._release = release_event
        self._terminated = threading.Event()
        self.stdout = self._Stdout(self)

    class _Stdout:
        def __init__(self, proc):
            self._proc = proc
            self._sent = False

        def __iter__(self):
            return self

        def __next__(self):
            # 阻塞直到被释放（正常输出一行）或被终止（结束输出）
            while not self._proc._release.is_set():
                if self._proc._terminated.is_set():
                    raise StopIteration
                self._proc._release.wait(timeout=0.02)
            if self._sent:
                raise StopIteration
            self._sent = True
            return '[download]  42.0% of 100.00MiB at 2.00MiB/s ETA 00:30\n'

    def poll(self):
        return self.returncode

    def terminate(self):
        self._terminated.set()
        self.returncode = -15

    def kill(self):
        self._terminated.set()
        self.returncode = -9

    def wait(self, timeout=None):
        # 模拟进程退出：正常释放（未被终止）时视为成功返回 0
        if not self._terminated.is_set():
            self.returncode = 0
        return self.returncode


class DynamicRelaunchTests(unittest.TestCase):
    """活动下载数变化时，正在跑的 yt-dlp 自动以新速率断点续传重拉。"""

    def test_relaunches_with_new_rate_on_rebalance(self):
        total = 4 * MIB
        controller = DownloadRateController()
        controller._read_total = mock.Mock(return_value=total)
        controller.register('main-task')
        # 清掉 register 置位的事件：本次拉起不应被视为「有变化」
        controller.rebalance_event.clear()

        processes = []
        releases = [threading.Event(), threading.Event()]

        def fake_popen(cmd, **kwargs):
            proc = _FakeDownloadProcess(1000 + len(processes), releases[len(processes)])
            processes.append((cmd, proc))
            return proc

        logger = mock.Mock()
        base_cmd = ['yt-dlp', '--concurrent-fragments', '1']

        def runner_target():
            with mock.patch.object(youtube_handler.subprocess, 'Popen', side_effect=fake_popen):
                youtube_handler._run_yt_dlp_dynamic(
                    base_cmd, logger, None, controller, 'main-task', None,
                )

        runner = threading.Thread(target=runner_target, daemon=True)
        runner.start()

        # 等第一次拉起（4M：只有 main-task 一个活动任务）
        deadline = time.time() + 5
        while len(processes) < 1 and time.time() < deadline:
            time.sleep(0.02)
        self.assertEqual(len(processes), 1, '第一次拉起超时')
        first_cmd = processes[0][0]
        self.assertIn('--limit-rate', first_cmd)
        self.assertEqual(first_cmd[first_cmd.index('--limit-rate') + 1], '4M')

        # 模拟另一个任务开始下载：活动数 1→2，generation 变化触发重平衡
        controller.register('second-task')

        # 等第一次进程被终止并以 2M 重拉
        deadline = time.time() + 8
        while len(processes) < 2 and time.time() < deadline:
            time.sleep(0.02)
        self.assertEqual(len(processes), 2, '未按新速率重新拉起')
        self.assertEqual(processes[0][1].returncode, -15, '旧进程应被 terminate')
        second_cmd = processes[1][0]
        self.assertIn('--limit-rate', second_cmd)
        self.assertEqual(second_cmd[second_cmd.index('--limit-rate') + 1], '2M')

        # 释放第二个进程让它正常跑完，runner 应返回其输出
        releases[1].set()
        runner.join(timeout=10)
        self.assertFalse(runner.is_alive(), 'runner 未在限定时间内结束')

    def test_relaunch_budget_is_capped(self):
        # 重拉次数超过 MAX_RELAUNCH_PER_DOWNLOAD 时应抛错止损，避免死循环
        from modules.download_rate_controller import MAX_RELAUNCH_PER_DOWNLOAD

        controller = DownloadRateController()
        controller._read_total = mock.Mock(return_value=4 * MIB)

        class _InstantProcess:
            def __init__(self):
                self.pid = 1
                self.returncode = None
                self.stdout = iter(())

            def poll(self):
                return self.returncode

            def terminate(self):
                self.returncode = -15

            kill = terminate

            def wait(self, timeout=None):
                return self.returncode

        def endless_rebalance(cmd, **kwargs):
            # 每次拉起都让 generation 变化并置位重平衡事件 → 无限触发重拉
            controller._bump()
            return _InstantProcess()

        with mock.patch.object(
            youtube_handler.subprocess, 'Popen', side_effect=endless_rebalance
        ), mock.patch.object(youtube_handler.time, 'sleep'):
            with self.assertRaises(subprocess.CalledProcessError):
                youtube_handler._run_yt_dlp_dynamic(
                    ['yt-dlp'], mock.Mock(), None, controller, 't', None,
                )
        self.assertGreaterEqual(MAX_RELAUNCH_PER_DOWNLOAD, 1)


if __name__ == '__main__':
    unittest.main()
