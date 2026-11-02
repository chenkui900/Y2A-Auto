# -*- coding: utf-8 -*-
"""设置页限速开关 / 速率单位 / 时间段的运行期测试。

用 node + 最小 DOM 桩真实执行 static/js/main.js 中「限速开关与生效时间段」
这一段，验证提示文案、控件禁用状态、隐藏域拼装与限速摊分提示，
而不是只做字符串匹配。
"""
import json
import pathlib
import subprocess
import tempfile
import unittest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
MAIN_JS = REPO_ROOT / 'static' / 'js' / 'main.js'
HARNESS = REPO_ROOT / 'tests' / 'js' / 'rate_limit_settings_toggle.js'

SEGMENT_START = '// 设置页：上传/下载限速的总开关与生效时间段'
SEGMENT_END = '// 通用提示对话框'


def _extract_segment(source: str) -> str:
    start = source.index(SEGMENT_START)
    end = source.index(SEGMENT_END, start)
    # 去掉「通用提示对话框」上方那条分隔注释开头
    return source[start:end]


@unittest.skipUnless(HARNESS.exists(), '缺少 JS 测试桩')
class RateLimitSettingsRuntimeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        segment = _extract_segment(MAIN_JS.read_text(encoding='utf-8'))
        cls._tmp = tempfile.NamedTemporaryFile('w', suffix='.js', delete=False, encoding='utf-8')
        cls._tmp.write(segment)
        cls._tmp.close()
        result = subprocess.run(
            ['node', str(HARNESS), cls._tmp.name],
            capture_output=True, text=True, timeout=60,
        )
        if result.returncode != 0:
            raise AssertionError(f'node 执行失败: {result.stderr[:800]}')
        cls.report = json.loads(result.stdout or '{}')
        cls.scenarios = cls.report.get('scenarios', {})

    @classmethod
    def tearDownClass(cls):
        pathlib.Path(cls._tmp.name).unlink(missing_ok=True)

    def test_no_runtime_errors(self):
        self.assertEqual(self.report.get('errors'), [], '页面脚本执行报错')

    def test_disabled_switch_disables_all_inputs(self):
        case = self.scenarios['disabled']
        self.assertTrue(case['downloadDisabled'])
        self.assertTrue(case['uploadDisabled'])
        self.assertTrue(case['scheduleDisabled'])
        self.assertTrue(case['startDisabled'])
        self.assertTrue(case['endDisabled'])
        self.assertTrue(case['timeDimmed'])
        self.assertIn('限速已关闭', case['hint'])

    def test_inside_cross_midnight_window(self):
        case = self.scenarios['insideWindow']
        self.assertFalse(case['downloadDisabled'])
        self.assertFalse(case['startDisabled'])
        self.assertIn('处于限速时段', case['hint'])
        self.assertIn('22:00 - 06:00', case['hint'])
        self.assertIn('alert-success', case['classes'])

    def test_outside_window_means_unlimited(self):
        case = self.scenarios['outsideWindow']
        self.assertIn('不在限速时段', case['hint'])
        self.assertIn('22:00 - 06:00', case['hint'])

    def test_schedule_off_means_always_on(self):
        case = self.scenarios['alwaysOn']
        self.assertIn('全天限速', case['hint'])
        self.assertTrue(case['startDisabled'])
        self.assertTrue(case['timeDimmed'])

    def test_end_time_is_exclusive(self):
        self.assertIn('不在限速时段', self.scenarios['boundaryEndExclusive']['hint'])

    def test_start_time_is_inclusive(self):
        self.assertIn('处于限速时段', self.scenarios['boundaryStartInclusive']['hint'])

    def test_empty_rate_warns_about_full_speed(self):
        case = self.scenarios['emptyRate']
        self.assertIn('实际仍是满速', case['hint'])
        self.assertIn('alert-warning', case['classes'])

    def test_same_start_and_end_means_all_day(self):
        self.assertIn('视为全天限速', self.scenarios['sameStartEnd']['hint'])

    def test_unit_toggle_rebuilds_hidden_rate(self):
        # 裸数字 1024 默认按 KB/s：隐藏域 "1024K"；标签自动切到 1 MB/s
        case = self.scenarios['unitK']
        self.assertEqual(case['downloadHidden'], '1024K')
        self.assertEqual(case['downloadUnitLabel'], '1 MB/s')

    def test_auto_unit_switches_label_by_value(self):
        # 裸数字 2048（KB/s）= 2 MB/s：隐藏域 "2048K"，标签自动切到 2 MB/s
        case = self.scenarios['autoUnitSwitch']
        self.assertEqual(case['downloadHidden'], '2048K')
        self.assertEqual(case['downloadUnitLabel'], '2 MB/s')

    def test_download_split_uses_dynamic_allocation(self):
        # 下载限速按「实际在下载的任务数」动态分配：不按并发数量死板平分，
        # 单任务吃满、多任务均分，改限速/并发无需重启（运行中自动以新速率续传）
        case = self.scenarios['threadsChanged']
        # dataset 里写的是数字，JSON 序列化后可能带引号也可能不带，统一按整数比
        self.assertEqual(int(case['downloadSplitTasks']), 2)
        self.assertIn('动态分配', case['downloadSplit'])
        self.assertIn('实际在下载', case['downloadSplit'])
        self.assertIn('吃满全部带宽', case['downloadSplit'])
        self.assertIn('自动均分', case['downloadSplit'])
        self.assertIn('不会超过设定值', case['downloadSplit'])
        self.assertIn('无需重启', case['downloadSplit'])

    def test_upload_unit_and_shared_budget_hint(self):
        case = self.scenarios['uploadUnitM']
        self.assertEqual(case['uploadHidden'], '2M')
        self.assertIn('2 MB/s', case['uploadSplit'])
        self.assertIn('共享', case['uploadSplit'])
        self.assertEqual(case['uploadUnitLabel'], '2 MB/s')

    def test_disabled_switch_also_disables_unit_select(self):
        case = self.scenarios['disabled']
        self.assertTrue(case['downloadUnitDisabled'])
        self.assertTrue(case['uploadUnitDisabled'])

    def test_empty_value_label_shows_auto(self):
        case = self.scenarios['emptyRate']
        self.assertEqual(case['downloadUnitLabel'], '自动单位')
        self.assertEqual(case['uploadUnitLabel'], '自动单位')


if __name__ == '__main__':
    unittest.main()
