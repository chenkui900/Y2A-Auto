# -*- coding: utf-8 -*-
"""并发上限来源的守卫测试：并发跟随「下载并发数量 / 上传并发数量」。

背景：并发上限原本来自 设置 → 运维与安全 → 资源控制 的
MAX_CONCURRENT_TASKS / MAX_CONCURRENT_UPLOADS，和「下载并发数量」是两处独立设置，
改并发数量不影响并发、改并发数又会影响限速摊分。现在资源控制里的任务并发已取消：

- 下载并发数量（YOUTUBE_DOWNLOAD_THREADS）= 同时下载的任务数；
- 上传并发数量（UPLOAD_THREADS）= 同时上传的任务数；
- 旧键即使残留在 config.json 里也必须被忽略。
"""
import os
import shutil
import tempfile
import unittest

import modules.task_manager as tm
from modules import concurrency
from modules.concurrency import (
    get_download_concurrency,
    get_upload_concurrency,
)

import app as web_app


class ConcurrencySourceTests(unittest.TestCase):
    def test_download_concurrency_follows_thread_setting(self):
        self.assertEqual(get_download_concurrency({'YOUTUBE_DOWNLOAD_THREADS': '2'}), 2)
        self.assertEqual(get_download_concurrency({'YOUTUBE_DOWNLOAD_THREADS': 7}), 7)

    def test_download_concurrency_clamped_and_default(self):
        self.assertEqual(
            get_download_concurrency({'YOUTUBE_DOWNLOAD_THREADS': 0}),
            1, '0 会被钳到 1，而不是让调度器完全不启动任务')
        self.assertEqual(
            get_download_concurrency({'YOUTUBE_DOWNLOAD_THREADS': 999}),
            concurrency.MAX_DOWNLOAD_THREADS)
        self.assertEqual(
            get_download_concurrency({'YOUTUBE_DOWNLOAD_THREADS': 'abc'}),
            concurrency.DEFAULT_DOWNLOAD_THREADS)
        # 传空字典表示「本次没有配置」，会回落到 config.json；这里用一个不含该键的配置验证默认值
        self.assertEqual(
            get_download_concurrency({'UPLOAD_THREADS': 1}),
            concurrency.DEFAULT_DOWNLOAD_THREADS)

    def test_upload_concurrency_follows_thread_setting(self):
        self.assertEqual(get_upload_concurrency({'UPLOAD_THREADS': 3}), 3)
        self.assertEqual(get_upload_concurrency({'UPLOAD_THREADS': '4'}), 4)
        self.assertEqual(
            get_upload_concurrency({'UPLOAD_THREADS': 99}), concurrency.MAX_UPLOAD_THREADS)
        self.assertEqual(
            get_upload_concurrency({'YOUTUBE_DOWNLOAD_THREADS': 4}),
            concurrency.DEFAULT_UPLOAD_THREADS)

    def test_legacy_concurrency_keys_are_ignored(self):
        cfg = {
            'MAX_CONCURRENT_TASKS': 99,
            'MAX_CONCURRENT_UPLOADS': 99,
            'YOUTUBE_DOWNLOAD_THREADS': 2,
            'UPLOAD_THREADS': 1,
        }
        self.assertEqual(get_download_concurrency(cfg), 2)
        self.assertEqual(get_upload_concurrency(cfg), 1)


class TaskProcessorWiringTests(unittest.TestCase):
    """真实构造 TaskProcessor，验证配额对象拿到的是线程数。"""

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix='y2a_conc_')
        self._old_db_path = tm.DB_PATH
        tm.DB_PATH = os.path.join(self._tmp, 'tasks.db')
        tm.init_db()
        self.processors = []

    def tearDown(self):
        for processor in self.processors:
            try:
                processor.scheduler.shutdown(wait=False)
            except Exception:
                pass
        tm.DB_PATH = self._old_db_path
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _make(self, config):
        processor = tm.TaskProcessor(dict(config))
        processor.scheduler.remove_all_jobs()
        self.processors.append(processor)
        return processor

    def test_initial_slots_come_from_threads(self):
        processor = self._make({'YOUTUBE_DOWNLOAD_THREADS': 3, 'UPLOAD_THREADS': 2})
        self.assertEqual(processor._current_max_concurrent_tasks, 3)
        self.assertEqual(processor._current_max_concurrent_uploads, 2)
        self.assertEqual(tm.task_semaphore.limit, 3)
        self.assertEqual(tm.upload_slots.limit, 2)

    def test_refresh_reacts_to_thread_change_without_restart(self):
        processor = self._make({'YOUTUBE_DOWNLOAD_THREADS': 1, 'UPLOAD_THREADS': 1})
        self.assertEqual(tm.task_semaphore.limit, 1)
        # 改设置后只更新 config 再刷新，不重建处理器（模拟保存设置页）
        processor.config = {'YOUTUBE_DOWNLOAD_THREADS': 5, 'UPLOAD_THREADS': 4}
        processor._refresh_runtime_limits(force=True)
        self.assertEqual(tm.task_semaphore.limit, 5)
        self.assertEqual(tm.upload_slots.limit, 4)
        self.assertEqual(processor._current_max_concurrent_tasks, 5)
        self.assertEqual(processor._current_max_concurrent_uploads, 4)


class AppSideConcurrencyTests(unittest.TestCase):
    def test_batch_upload_limit_follows_upload_threads(self):
        self.assertEqual(web_app._resolve_upload_concurrency_limit({'UPLOAD_THREADS': 3}), 3)
        # 旧键已无人读取：即使填了 9，也不会改变批量上传的并发
        self.assertEqual(
            web_app._resolve_upload_concurrency_limit(
                {'UPLOAD_THREADS': 2, 'MAX_CONCURRENT_UPLOADS': 9}),
            2,
        )
        self.assertEqual(
            web_app._resolve_upload_concurrency_limit({'MAX_CONCURRENT_UPLOADS': 9}), 1,
            '只剩旧键时按上传线程数的默认值 1 走')

    def test_concurrency_keys_left_settings_whitelist(self):
        self.assertNotIn('MAX_CONCURRENT_TASKS', web_app.SETTINGS_INT_FIELDS)
        self.assertNotIn('MAX_CONCURRENT_UPLOADS', web_app.SETTINGS_INT_FIELDS)
        self.assertIn('YOUTUBE_DOWNLOAD_THREADS', web_app.SETTINGS_INT_FIELDS)
        self.assertIn('UPLOAD_THREADS', web_app.SETTINGS_INT_FIELDS)

    def test_thread_fields_are_range_guarded(self):
        guards = {key: (lo, hi) for key, lo, hi in web_app.SETTINGS_RANGE_GUARDS}
        self.assertEqual(guards['YOUTUBE_DOWNLOAD_THREADS'], (1.0, float(concurrency.MAX_DOWNLOAD_THREADS)))
        self.assertEqual(guards['UPLOAD_THREADS'], (1.0, float(concurrency.MAX_UPLOAD_THREADS)))

    def test_settings_template_drops_concurrency_card(self):
        import pathlib

        template = (pathlib.Path(__file__).resolve().parents[1] / 'templates' / 'settings.html').read_text(encoding='utf-8')
        self.assertNotIn('name="MAX_CONCURRENT_TASKS"', template)
        self.assertNotIn('name="MAX_CONCURRENT_UPLOADS"', template)
        self.assertIn('name="UPLOAD_THREADS"', template)
        # 「资源控制 → 任务并发」卡片整体取消，不再有 MAX_CONCURRENT_* 控件
        self.assertNotIn('section-eyebrow">资源控制', template)


if __name__ == '__main__':
    unittest.main()
