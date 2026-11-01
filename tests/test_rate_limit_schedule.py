# -*- coding: utf-8 -*-
"""上传/下载限速的「开关 + 生效时间段」测试。

需求：
1. 设置页可以一键开启 / 关闭限速（关闭后保留已填速率，恢复开关即生效）；
2. 可以只在指定时间段内限速（如 22:00 - 06:00），其余时间满速；
3. 时间段支持跨零点，起止相同视为全天；非法时间回退默认值而不是报错。
"""
import datetime as dt
import pathlib
import re
import unittest
from unittest import mock

import app as web_app
from modules import rate_limiter as rl


REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]


def _config(**overrides):
    base = {
        'RATE_LIMIT_ENABLED': True,
        'RATE_LIMIT_TIME_ENABLED': False,
        'RATE_LIMIT_START_TIME': '22:00',
        'RATE_LIMIT_END_TIME': '06:00',
        'DOWNLOAD_RATE_LIMIT': '1M',
        'UPLOAD_RATE_LIMIT': '500K',
    }
    base.update(overrides)
    return base


class TimeWindowTests(unittest.TestCase):
    def test_parse_time_formats(self):
        self.assertEqual(rl.parse_time_of_day('22:00'), 22 * 60)
        self.assertEqual(rl.parse_time_of_day('9:05'), 9 * 60 + 5)
        self.assertEqual(rl.parse_time_of_day('22:00:30'), 22 * 60)
        self.assertEqual(rl.parse_time_of_day('2200'), 22 * 60)
        self.assertEqual(rl.parse_time_of_day(' 6:5 '), 6 * 60 + 5)
        self.assertEqual(rl.parse_time_of_day('00:00'), 0)

    def test_parse_time_invalid(self):
        for value in ('', '  ', '25:00', '12:60', 'abc', '12', None, '12:00:00:00'):
            self.assertIsNone(rl.parse_time_of_day(value), f'{value!r} 应判为非法')

    def test_normalize_time(self):
        self.assertEqual(rl.normalize_time_of_day('7:5'), '07:05')
        self.assertEqual(rl.normalize_time_of_day('2200'), '22:00')
        self.assertEqual(rl.normalize_time_of_day('', '06:00'), '06:00')
        with self.assertRaises(ValueError):
            rl.normalize_time_of_day('25:99')

    def test_window_same_day(self):
        start, end = 8 * 60, 18 * 60
        self.assertFalse(rl.in_time_window(start, end, 7 * 60 + 59))
        self.assertTrue(rl.in_time_window(start, end, 8 * 60))       # 左闭
        self.assertTrue(rl.in_time_window(start, end, 17 * 60 + 59))
        self.assertFalse(rl.in_time_window(start, end, 18 * 60))     # 右开

    def test_window_cross_midnight(self):
        start, end = 22 * 60, 6 * 60
        self.assertTrue(rl.in_time_window(start, end, 23 * 60))
        self.assertTrue(rl.in_time_window(start, end, 0))
        self.assertTrue(rl.in_time_window(start, end, 5 * 60 + 59))
        self.assertFalse(rl.in_time_window(start, end, 6 * 60))
        self.assertFalse(rl.in_time_window(start, end, 12 * 60))

    def test_window_same_start_end_means_all_day(self):
        self.assertTrue(rl.in_time_window(9 * 60, 9 * 60, 3 * 60))
        self.assertTrue(rl.in_time_window(9 * 60, 9 * 60, 9 * 60))

    def test_time_window_falls_back_when_config_broken(self):
        config = _config(RATE_LIMIT_TIME_ENABLED=True,
                         RATE_LIMIT_START_TIME='bad', RATE_LIMIT_END_TIME='')
        self.assertEqual(rl.get_time_window(config), (22 * 60, 6 * 60))
        self.assertEqual(rl.describe_window(config), '22:00 - 次日 06:00')


class ActiveStateTests(unittest.TestCase):
    NOON = dt.datetime(2026, 11, 1, 12, 0)
    LATE = dt.datetime(2026, 11, 1, 23, 30)
    EARLY = dt.datetime(2026, 11, 2, 3, 0)

    def test_disabled_switch_turns_off_everything(self):
        config = _config(RATE_LIMIT_ENABLED=False)
        self.assertFalse(rl.is_rate_limit_active(config, self.NOON))
        self.assertIsNone(rl.get_download_limit(config))
        self.assertIsNone(rl.get_upload_limit(config))
        self.assertIn('限速已关闭', rl.rate_limit_status(config, self.NOON)['text'])

    def test_always_on_when_schedule_disabled(self):
        config = _config(RATE_LIMIT_TIME_ENABLED=False)
        self.assertTrue(rl.is_rate_limit_active(config, self.NOON))
        self.assertEqual(rl.get_download_limit(config), 1024 ** 2)
        self.assertEqual(rl.get_upload_limit(config), 500 * 1024)

    def test_schedule_outside_window_means_unlimited(self):
        config = _config(RATE_LIMIT_TIME_ENABLED=True)
        self.assertFalse(rl.is_rate_limit_active(config, self.NOON))
        # 必须显式传 now：不传会取真实时钟，跑在 22:00-06:00 之间时这条断言会假失败
        self.assertIsNone(rl.get_download_limit(config, now=self.NOON))
        self.assertIsNone(rl.get_upload_limit(config, now=self.NOON))
        status = rl.rate_limit_status(config, self.NOON)
        self.assertFalse(status['active'])
        self.assertIn('不在限速时段', status['text'])

    def test_schedule_inside_window_applies_rate(self):
        config = _config(RATE_LIMIT_TIME_ENABLED=True)
        for moment in (self.LATE, self.EARLY):
            self.assertTrue(rl.is_rate_limit_active(config, moment))
            self.assertEqual(rl.get_download_limit(config, now=moment), 1024 ** 2)
            self.assertEqual(rl.get_upload_limit(config, now=moment), 500 * 1024)
        self.assertIn('处于限速时段', rl.rate_limit_status(config, self.LATE)['text'])

    def test_schedule_same_day_window(self):
        config = _config(RATE_LIMIT_TIME_ENABLED=True,
                         RATE_LIMIT_START_TIME='09:00', RATE_LIMIT_END_TIME='18:00')
        self.assertTrue(rl.is_rate_limit_active(config, self.NOON))
        self.assertIsNone(rl.get_download_limit(config))
        # 凌晨不在 [09:00, 18:00) 内
        self.assertFalse(rl.is_rate_limit_active(config, self.EARLY))


class SettingsUiTests(unittest.TestCase):
    def _render(self, config):
        web_app.app.config.update(TESTING=True)
        client = web_app.app.test_client()
        with client.session_transaction() as session:
            session['logged_in'] = True
        with mock.patch.object(web_app, 'load_config', return_value=config):
            response = client.get('/settings')
        self.assertEqual(response.status_code, 200)
        return response.get_data(as_text=True)

    def test_switch_and_time_fields_rendered(self):
        html = self._render(_config())
        for needle in (
            'name="RATE_LIMIT_ENABLED"',
            'name="RATE_LIMIT_TIME_ENABLED"',
            'name="RATE_LIMIT_START_TIME"',
            'name="RATE_LIMIT_END_TIME"',
            'id="rate-limit-enabled"',
            'id="rate-limit-time-enabled"',
            'id="rate-limit-time-settings"',
            'id="rate-limit-status-hint"',
            'type="time"',
        ):
            self.assertIn(needle, html, f'设置页缺少 {needle}')
        self.assertIn('启用限速', html)
        self.assertIn('只在指定时间段内限速', html)

    def test_checkbox_state_follows_config(self):
        html = self._render(_config(RATE_LIMIT_ENABLED=False, RATE_LIMIT_TIME_ENABLED=True,
                                    RATE_LIMIT_START_TIME='09:00', RATE_LIMIT_END_TIME='18:00'))
        enabled_tag = re.search(r'<input[^>]*id="rate-limit-enabled"[^>]*>', html).group(0)
        schedule_tag = re.search(r'<input[^>]*id="rate-limit-time-enabled"[^>]*>', html).group(0)
        self.assertNotIn('checked', enabled_tag)
        self.assertIn('checked', schedule_tag)
        self.assertIn('value="09:00"', html)
        self.assertIn('value="18:00"', html)
        self.assertIn('限速已关闭', html)

    def test_hint_shows_window_status(self):
        html = self._render(_config(RATE_LIMIT_TIME_ENABLED=True,
                                    RATE_LIMIT_START_TIME='22:00', RATE_LIMIT_END_TIME='06:00'))
        self.assertIn('22:00 - 次日 06:00', html)

    def test_checkbox_fields_whitelisted(self):
        # 不进白名单的开关在「取消勾选后保存」时不会生效
        self.assertIn('RATE_LIMIT_ENABLED', web_app.SETTINGS_CHECKBOX_FIELDS)
        self.assertIn('RATE_LIMIT_TIME_ENABLED', web_app.SETTINGS_CHECKBOX_FIELDS)

    def test_save_normalizes_time_values(self):
        source = (REPO_ROOT / 'app.py').read_text(encoding='utf-8')
        self.assertIn('normalize_time_of_day(_submitted_time, _time_default)', source)
        self.assertIn("'RATE_LIMIT_START_TIME', '限速开始时间'", source)
        self.assertIn("'RATE_LIMIT_END_TIME', '限速结束时间'", source)

    def test_defaults_present_in_config(self):
        from modules.config_manager import DEFAULT_CONFIG

        self.assertIs(DEFAULT_CONFIG.get('RATE_LIMIT_ENABLED'), True)
        self.assertIs(DEFAULT_CONFIG.get('RATE_LIMIT_TIME_ENABLED'), False)
        self.assertEqual(DEFAULT_CONFIG.get('RATE_LIMIT_START_TIME'), '22:00')
        self.assertEqual(DEFAULT_CONFIG.get('RATE_LIMIT_END_TIME'), '06:00')

    def test_frontend_js_wired(self):
        source = (REPO_ROOT / 'static' / 'js' / 'main.js').read_text(encoding='utf-8')
        self.assertIn("getElementById('rate-limit-enabled')", source)
        self.assertIn("getElementById('rate-limit-time-enabled')", source)
        self.assertIn('renderRateLimitState', source)


if __name__ == '__main__':
    unittest.main()
