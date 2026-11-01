# -*- coding: utf-8 -*-
"""上传 / 下载独立限速的单元测试。

覆盖：
1. 速率写法解析（500K / 1M / 2.5M / 纯数字 / 0 / 非法值）；
2. 下载限速配置读取（新键优先、旧键 YOUTUBE_THROTTLED_RATE 兼容回退）；
3. 上传限速配置读取；
4. Pacer 的排队节流：同速率下连续预约会累加等待时间；
5. 三个链路的接线：yt-dlp 用 --limit-rate（不是 --throttled-rate）、
   AcFun 分块前节流且可取消、bili_sdk 分块前 await 节流。
"""
import pathlib
import time
import unittest
from unittest import mock

from modules import rate_limiter as rl


REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]


class RateParseTests(unittest.TestCase):
    def test_parse_units(self):
        self.assertEqual(rl.parse_rate_limit('500K'), 500 * 1024)
        self.assertEqual(rl.parse_rate_limit('1M'), 1024 ** 2)
        self.assertEqual(rl.parse_rate_limit('2.5M'), int(2.5 * 1024 ** 2))
        self.assertEqual(rl.parse_rate_limit('1G'), 1024 ** 3)
        self.assertEqual(rl.parse_rate_limit('1024'), 1024)
        self.assertEqual(rl.parse_rate_limit('1mb'), 1024 ** 2)

    def test_parse_unlimited(self):
        for value in ('', None, '0', 0, '  '):
            self.assertIsNone(rl.parse_rate_limit(value))

    def test_parse_invalid_raises(self):
        for value in ('abc', '1T', '-5M', '1.2.3M'):
            with self.assertRaises(ValueError):
                rl.parse_rate_limit(value)

    def test_normalize(self):
        self.assertEqual(rl.normalize_rate_limit('1m'), '1M')
        self.assertEqual(rl.normalize_rate_limit('2048K'), '2M')
        self.assertEqual(rl.normalize_rate_limit('  '), '')
        with self.assertRaises(ValueError):
            rl.normalize_rate_limit('fast')

    def test_format_roundtrip(self):
        self.assertEqual(rl.format_rate_limit(1024 ** 2), '1M')
        self.assertEqual(rl.format_rate_limit(500 * 1024), '500K')
        self.assertEqual(rl.format_rate_limit(1536 * 1024), '1.5M')
        self.assertEqual(rl.format_rate_limit(0), '')


class ConfigLimitTests(unittest.TestCase):
    def test_download_new_key(self):
        self.assertEqual(rl.get_download_limit({'DOWNLOAD_RATE_LIMIT': '4M'}), 4 * 1024 ** 2)

    def test_download_legacy_fallback(self):
        self.assertEqual(rl.get_download_limit({'YOUTUBE_THROTTLED_RATE': '700K'}), 700 * 1024)

    def test_download_new_key_wins_and_invalid_falls_back(self):
        both = {'DOWNLOAD_RATE_LIMIT': '2M', 'YOUTUBE_THROTTLED_RATE': '700K'}
        self.assertEqual(rl.get_download_limit(both), 2 * 1024 ** 2)
        broken = {'DOWNLOAD_RATE_LIMIT': 'oops', 'YOUTUBE_THROTTLED_RATE': '700K'}
        self.assertEqual(rl.get_download_limit(broken), 700 * 1024)

    def test_upload_limit(self):
        self.assertEqual(rl.get_upload_limit({'UPLOAD_RATE_LIMIT': '800K'}), 800 * 1024)
        self.assertIsNone(rl.get_upload_limit({'UPLOAD_RATE_LIMIT': ''}))
        self.assertIsNone(rl.get_upload_limit({}))


class PacerTests(unittest.TestCase):
    def test_unlimited_never_waits(self):
        pacer = rl.Pacer(None)
        self.assertEqual(pacer.acquire(10 * 1024 * 1024), 0.0)

    def test_second_reservation_waits_for_budget(self):
        pacer = rl.Pacer(1000)  # 1000 B/s
        self.assertEqual(pacer.acquire(500), 0.0)
        delay = pacer.acquire(500)
        self.assertGreater(delay, 0.35)
        self.assertLess(delay, 0.7)

    def test_delay_shrinks_when_idle(self):
        pacer = rl.Pacer(1000)
        pacer.acquire(1000)
        time.sleep(0.25)
        delay = pacer.acquire(100)
        self.assertLessEqual(delay, 0.78)
        self.assertGreater(delay, 0.5)


class WiringTests(unittest.TestCase):
    def test_youtube_handler_uses_limit_rate(self):
        source = (REPO_ROOT / 'modules' / 'youtube_handler.py').read_text(encoding='utf-8')
        self.assertIn("'--limit-rate'", source)
        self.assertIn('get_download_limit(config)', source)
        # --throttled-rate 是「低于该速度就重连」的阈值，不是限速，不能再用它冒充限速
        self.assertNotIn("'--throttled-rate'", source)

    def test_acfun_chunk_throttled(self):
        source = (REPO_ROOT / 'modules' / 'acfun_uploader.py').read_text(encoding='utf-8')
        self.assertIn('throttle_upload_cancellable(len(block)', source)
        self.assertIn('from .rate_limiter import throttle_upload_cancellable', source)

    def test_bilibili_sdk_paces_chunks(self):
        source = (REPO_ROOT / 'modules' / 'bili_sdk' / 'video_uploader.py').read_text(encoding='utf-8')
        self.assertIn('_pace_async_upload', source)
        self.assertIn('await _pace_async_upload(real_chunk_size)', source)

    def test_cancellable_throttle_returns_false_when_cancelled(self):
        class Evt:
            def is_set(self):
                return True

        rl.invalidate_cache()
        # 先预约掉一个分块的通道时间，下一次预约才会有等待，从而走到取消检查
        pacer = rl.Pacer(1024)
        pacer.acquire(10 * 1024)
        with mock.patch.object(rl, '_upload_pacer', return_value=pacer):
            self.assertTrue(rl.throttle_upload_cancellable(0, Evt()))  # 0 字节不需要等待
            self.assertFalse(rl.throttle_upload_cancellable(10 * 1024, Evt()))


if __name__ == '__main__':
    unittest.main()
