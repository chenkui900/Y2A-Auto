# -*- coding: utf-8 -*-
"""设置页「线程数 + 限速数值/单位」保存链路的守卫测试。

覆盖：
1. 下载/上传线程数能落盘，且越界会回退到默认值并给出可见提示；
2. 限速按「数值 + 单位（K/M）」提交时拼成 500K / 1.5M 落盘；
3. 数值清空 = 不限速；只提交隐藏域（老页面/无 JS）时按原值保存；
4. 总开关关闭时数值控件被 disabled 不提交，隐藏域保留原值。
"""
import os
import shutil
import tempfile
import unittest
from unittest.mock import patch

import app as web_app
from modules import config_manager as cm
from modules.concurrency import DEFAULT_DOWNLOAD_THREADS, DEFAULT_UPLOAD_THREADS


class _SaveBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix='y2a-threads-')
        self._patchers = []
        for patcher in (
            patch.object(cm, 'get_app_subdir', lambda name: os.path.join(self._tmp, name)),
            patch.object(web_app, 'configure_app', return_value=None),
            patch.object(web_app, '_sync_notification_service', return_value=None),
            patch('modules.task_manager.get_global_task_processor', return_value=None),
            patch.object(web_app.youtube_monitor, 'reload_api_client',
                         return_value=(False, 'missing_api_key')),
            patch.object(web_app.youtube_monitor, 'start_all_schedules', return_value=None),
            patch.object(web_app.youtube_monitor, 'stop_all_schedules', return_value=None),
        ):
            patcher.start()
            self._patchers.append(patcher)

    def tearDown(self):
        for patcher in self._patchers:
            patcher.stop()
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _save(self, form):
        result = web_app._perform_settings_save(dict(form), {})
        self.assertTrue(result['success'], msg=result.get('messages'))
        return result

    def _config(self):
        return cm.load_config()

    @staticmethod
    def _warnings(result):
        return [msg['text'] for msg in result['messages'] if msg['category'] == 'warning']


class ThreadSettingsSaveTests(_SaveBase):
    def test_threads_are_persisted(self):
        self._save({'YOUTUBE_DOWNLOAD_THREADS': '3', 'UPLOAD_THREADS': '2'})
        config = self._config()
        self.assertEqual(int(config['YOUTUBE_DOWNLOAD_THREADS']), 3)
        self.assertEqual(int(config['UPLOAD_THREADS']), 2)

    def test_out_of_range_threads_fall_back_with_warning(self):
        result = self._save({'YOUTUBE_DOWNLOAD_THREADS': '99', 'UPLOAD_THREADS': '0'})
        config = self._config()
        self.assertEqual(int(config['YOUTUBE_DOWNLOAD_THREADS']), DEFAULT_DOWNLOAD_THREADS)
        self.assertEqual(int(config['UPLOAD_THREADS']), DEFAULT_UPLOAD_THREADS)
        warnings = ' '.join(self._warnings(result))
        self.assertIn('下载并发数量', warnings)
        self.assertIn('上传并发数量', warnings)


class RateLimitUnitSaveTests(_SaveBase):
    def test_value_and_unit_compose(self):
        self._save({
            'DOWNLOAD_RATE_LIMIT_VALUE': '1.5', 'DOWNLOAD_RATE_LIMIT_UNIT': 'M',
            'UPLOAD_RATE_LIMIT_VALUE': '800', 'UPLOAD_RATE_LIMIT_UNIT': 'K',
        })
        config = self._config()
        self.assertEqual(config['DOWNLOAD_RATE_LIMIT'], '1.5M')
        self.assertEqual(config['UPLOAD_RATE_LIMIT'], '800K')

    def test_clearing_value_means_unlimited(self):
        self._save({'DOWNLOAD_RATE_LIMIT': '1M', 'UPLOAD_RATE_LIMIT': '500K'})
        self._save({
            'DOWNLOAD_RATE_LIMIT_VALUE': '', 'DOWNLOAD_RATE_LIMIT_UNIT': 'M',
            'UPLOAD_RATE_LIMIT_VALUE': '', 'UPLOAD_RATE_LIMIT_UNIT': 'K',
        })
        config = self._config()
        self.assertEqual(config['DOWNLOAD_RATE_LIMIT'], '')
        self.assertEqual(config['UPLOAD_RATE_LIMIT'], '')

    def test_value_without_unit_defaults_to_k(self):
        self._save({'UPLOAD_RATE_LIMIT_VALUE': '250'})
        self.assertEqual(self._config()['UPLOAD_RATE_LIMIT'], '250K')

    def test_value_with_inline_unit_is_accepted(self):
        # 新版自动单位控件：输入框可直接填 2M 这类带单位的写法
        self._save({'DOWNLOAD_RATE_LIMIT_VALUE': '2M', 'UPLOAD_RATE_LIMIT_VALUE': '1.5M'})
        config = self._config()
        self.assertEqual(config['DOWNLOAD_RATE_LIMIT'], '2M')
        self.assertEqual(config['UPLOAD_RATE_LIMIT'], '1.5M')

    def test_bare_number_is_auto_normalized_to_best_unit(self):
        # 裸数字默认按 KB/s，保存时自动换成合适单位：2048（KB/s）-> 2M
        self._save({'DOWNLOAD_RATE_LIMIT_VALUE': '2048'})
        self.assertEqual(self._config()['DOWNLOAD_RATE_LIMIT'], '2M')

    def test_hidden_only_submission_keeps_value(self):
        # 老页面 / 关掉 JS 时只有隐藏域，不能把已有速率清成「不限速」
        self._save({'DOWNLOAD_RATE_LIMIT': '2M'})
        self.assertEqual(self._config()['DOWNLOAD_RATE_LIMIT'], '2M')

    def test_invalid_value_falls_back_to_unlimited_with_warning(self):
        # 数值框能被手工 POST 成任意字符串，非法时回退「不限速」并提示
        result = self._save({
            'DOWNLOAD_RATE_LIMIT_VALUE': 'abc', 'DOWNLOAD_RATE_LIMIT_UNIT': 'M',
        })
        self.assertEqual(self._config()['DOWNLOAD_RATE_LIMIT'], '')
        self.assertIn('下载限速', ' '.join(self._warnings(result)))

    def test_value_unit_fields_never_persisted(self):
        self._save({
            'DOWNLOAD_RATE_LIMIT_VALUE': '1', 'DOWNLOAD_RATE_LIMIT_UNIT': 'M',
            'UPLOAD_RATE_LIMIT_VALUE': '1', 'UPLOAD_RATE_LIMIT_UNIT': 'M',
        })
        config = self._config()
        self.assertNotIn('DOWNLOAD_RATE_LIMIT_VALUE', config)
        self.assertNotIn('DOWNLOAD_RATE_LIMIT_UNIT', config)
        self.assertNotIn('UPLOAD_RATE_LIMIT_VALUE', config)
        self.assertNotIn('UPLOAD_RATE_LIMIT_UNIT', config)


if __name__ == '__main__':
    unittest.main()
