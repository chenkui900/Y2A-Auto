"""上传 / 下载独立限速。

配置项（config.json）：
- ``RATE_LIMIT_ENABLED``：限速总开关，``false`` 时上传下载都不限速；
- ``RATE_LIMIT_TIME_ENABLED``：是否只在指定时间段内限速；
- ``RATE_LIMIT_START_TIME`` / ``RATE_LIMIT_END_TIME``：限速时段（``HH:MM``，
  支持跨零点，如 22:00 - 06:00；起止相同视为全天）；
- ``DOWNLOAD_RATE_LIMIT``：下载限速，交给 yt-dlp 的 ``--limit-rate``；
- ``UPLOAD_RATE_LIMIT``：上传限速，由本模块在分块上传时按块大小记账并等待。

取值格式：``500K`` / ``1M`` / ``2.5M`` / ``1G`` / 纯数字（字节/秒）；
留空、``0``、``-1`` 表示不限制。K/M/G 按 1024 进制。

限速采用「虚拟时钟」调度：每次传输 N 字节，就为它预约 N/rate 秒的通道时间，
返回需要等待的时长。多个并发上传共享同一个 pacer 时，后到的分块会排队，
因此总速率仍然被限制在配置值附近，而不是每个任务各占满带宽。
"""
from __future__ import annotations

import asyncio
import logging
import re
import threading
import time
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# 复用的解析结果缓存
_UNIT_FACTORS = {
    '': 1,
    'B': 1,
    'K': 1024,
    'KB': 1024,
    'M': 1024 ** 2,
    'MB': 1024 ** 2,
    'G': 1024 ** 3,
    'GB': 1024 ** 3,
}

_RATE_RE = re.compile(r'^\s*(\d+(?:\.\d+)?)\s*([a-zA-Z]*)\s*$')


def parse_rate_limit(value: Any) -> Optional[int]:
    """把 ``1M`` / ``500K`` / ``1024`` 解析成 bytes/s；空值或 0 返回 None。

    非法格式抛 ``ValueError``，由调用方决定回退策略。
    """
    if value is None:
        return None
    if isinstance(value, (int, float)):
        number, unit = float(value), ''
    else:
        text = str(value).strip()
        if not text or text in {'0', '-1', '0.0'}:
            return None
        match = _RATE_RE.match(text)
        if not match:
            raise ValueError(f'无法识别的限速格式: {value!r}')
        number, unit = float(match.group(1)), match.group(2).upper()

    if unit not in _UNIT_FACTORS:
        raise ValueError(f'不支持的限速单位: {unit!r}')
    bytes_per_second = int(number * _UNIT_FACTORS[unit])
    if bytes_per_second <= 0:
        return None
    return bytes_per_second


def normalize_rate_limit(value: Any) -> str:
    """把用户输入规范成保存到配置里的字符串；空白/0 统一成 ''。非法值抛 ValueError。"""
    if value is None:
        return ''
    text = str(value).strip()
    if not text:
        return ''
    parsed = parse_rate_limit(text)
    if parsed is None:
        return ''
    return format_rate_limit(parsed)


def format_rate_limit(bytes_per_second: Optional[int]) -> str:
    """1048576 -> '1M'，512000 -> '500K'，1572864 -> '1.5M'，小于 1K 时按字节输出。"""
    if not bytes_per_second or bytes_per_second <= 0:
        return ''
    for unit, factor in (('G', 1024 ** 3), ('M', 1024 ** 2), ('K', 1024)):
        if bytes_per_second >= factor:
            value = bytes_per_second / factor
            text = f'{value:.2f}'.rstrip('0').rstrip('.')
            return f'{text}{unit}'
    return str(int(bytes_per_second))


class Pacer:
    """按字节记账的限速器；`acquire()` 返回调用方应等待的秒数。"""

    def __init__(self, bytes_per_second: Optional[int]):
        self.rate = float(bytes_per_second or 0)
        self._lock = threading.Lock()
        self._next_free = 0.0

    def acquire(self, num_bytes: int) -> float:
        if self.rate <= 0 or not num_bytes or num_bytes <= 0:
            return 0.0
        duration = float(num_bytes) / self.rate
        with self._lock:
            now = time.monotonic()
            start = max(now, self._next_free)
            self._next_free = start + duration
            return start - now

    def reset(self) -> None:
        with self._lock:
            self._next_free = 0.0


# ---------------------------------------------------------------------------
# 配置读取（带短缓存，避免每个分块都读一次 config.json）
# ---------------------------------------------------------------------------

_CONFIG_CACHE_TTL = 3.0
_config_cache: Dict[str, Any] = {'config': None, 'at': 0.0, 'lock': threading.Lock()}
_pacers: Dict[str, Any] = {'upload': None, 'upload_rate': None, 'lock': threading.Lock()}


def _load_config_cached() -> Dict[str, Any]:
    now = time.monotonic()
    with _config_cache['lock']:
        cached = _config_cache['config']
        if cached is not None and now - _config_cache['at'] < _CONFIG_CACHE_TTL:
            return cached
    try:
        from .config_manager import load_config

        config = load_config() or {}
    except Exception as exc:  # pragma: no cover - 配置损坏时不应影响传输
        logger.debug('读取限速配置失败（忽略）：%s', exc)
        config = {}
    with _config_cache['lock']:
        _config_cache['config'] = config
        _config_cache['at'] = now
    return config


def invalidate_cache() -> None:
    """设置保存后调用，让下一次传输立刻用上新值。"""
    with _config_cache['lock']:
        _config_cache['config'] = None
        _config_cache['at'] = 0.0
    with _pacers['lock']:
        _pacers['upload'] = None
        _pacers['upload_rate'] = None


# ---------------------------------------------------------------------------
# 限速开关与生效时间段
# ---------------------------------------------------------------------------
DEFAULT_WINDOW_START = '22:00'
DEFAULT_WINDOW_END = '06:00'

_TIME_RE = re.compile(r'^\s*(\d{1,2})\s*[:：]\s*(\d{1,2})(?:\s*[:：]\s*\d{1,2})?\s*$')
_COMPACT_TIME_RE = re.compile(r'^\s*(\d{1,2})(\d{2})\s*$')


def parse_time_of_day(value: Any) -> Optional[int]:
    """把 ``22:00`` / ``22:00:00`` / ``2200`` / ``9:05`` 解析成当天分钟数。

    空值或非法格式返回 None（由调用方回退默认值），不做抛错——
    时间配置坏掉不应该让限速把整个传输卡死。
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    match = _TIME_RE.match(text) or _COMPACT_TIME_RE.match(text)
    if not match:
        return None
    hour, minute = int(match.group(1)), int(match.group(2))
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return hour * 60 + minute


def format_time_of_day(minutes: Optional[int]) -> str:
    if minutes is None:
        return ''
    minutes = int(minutes) % (24 * 60)
    return f'{minutes // 60:02d}:{minutes % 60:02d}'


def normalize_time_of_day(value: Any, default: str = '') -> str:
    """规范成 ``HH:MM``；空值返回 ``default``，非法值抛 ``ValueError``。"""
    text = '' if value is None else str(value).strip()
    if not text:
        return default
    parsed = parse_time_of_day(text)
    if parsed is None:
        raise ValueError(f'无法识别的时间格式: {value!r}')
    return format_time_of_day(parsed)


def _as_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    text = str(value).strip().lower()
    if text in {'1', 'true', 'yes', 'on', 'enabled'}:
        return True
    if text in {'0', 'false', 'no', 'off', 'disabled', ''}:
        return False
    return default


def is_rate_limit_enabled(config: Optional[Dict[str, Any]] = None) -> bool:
    """限速总开关；配置缺失时默认开启（老配置无此键也要保持原有行为）。"""
    config = config if config is not None else _load_config_cached()
    return _as_bool(config.get('RATE_LIMIT_ENABLED'), True)


def is_schedule_enabled(config: Optional[Dict[str, Any]] = None) -> bool:
    config = config if config is not None else _load_config_cached()
    return _as_bool(config.get('RATE_LIMIT_TIME_ENABLED'), False)


def get_time_window(config: Optional[Dict[str, Any]] = None) -> Optional[tuple]:
    """返回 ``(start_minute, end_minute)``；未启用时间段时返回 None。"""
    config = config if config is not None else _load_config_cached()
    if not is_schedule_enabled(config):
        return None
    start = parse_time_of_day(config.get('RATE_LIMIT_START_TIME'))
    end = parse_time_of_day(config.get('RATE_LIMIT_END_TIME'))
    if start is None:
        start = parse_time_of_day(DEFAULT_WINDOW_START)
    if end is None:
        end = parse_time_of_day(DEFAULT_WINDOW_END)
    return int(start), int(end)


def in_time_window(start_minute: int, end_minute: int, minute: int) -> bool:
    """判断某一分钟是否落在区间内：左闭右开，支持跨零点（22:00-06:00）。

    起止相同视为「全天生效」，避免用户填成一样时出现「永远不限速」的困惑。
    """
    if start_minute == end_minute:
        return True
    if start_minute < end_minute:
        return start_minute <= minute < end_minute
    return minute >= start_minute or minute < end_minute


def describe_window(config: Optional[Dict[str, Any]] = None) -> str:
    window = get_time_window(config)
    if not window:
        return ''
    start, end = window
    if start > end:
        # 跨零点用「次日」描述，避免和状态文案里的括号嵌套
        return f'{format_time_of_day(start)} - 次日 {format_time_of_day(end)}'
    return f'{format_time_of_day(start)} - {format_time_of_day(end)}'


def is_rate_limit_active(config: Optional[Dict[str, Any]] = None, now: Any = None) -> bool:
    """当前时刻是否应该限速：总开关开着，且（未启用时间段 或 落在时间段内）。"""
    config = config if config is not None else _load_config_cached()
    if not is_rate_limit_enabled(config):
        return False
    window = get_time_window(config)
    if window is None:
        return True
    moment = now
    if moment is None:
        from datetime import datetime

        moment = datetime.now()
    return in_time_window(window[0], window[1], moment.hour * 60 + moment.minute)


def rate_limit_status(config: Optional[Dict[str, Any]] = None, now: Any = None) -> Dict[str, Any]:
    """给设置页展示用的状态摘要。"""
    config = config if config is not None else _load_config_cached()
    enabled = is_rate_limit_enabled(config)
    scheduled = is_schedule_enabled(config)
    window_text = describe_window(config)
    active = is_rate_limit_active(config, now)
    if not enabled:
        text = '限速已关闭：上传和下载都按满速传输。'
    elif not scheduled:
        text = '全天限速：不限时间段，上传和下载始终按设定速率传输。'
    elif active:
        text = f'当前处于限速时段（{window_text}），按设定速率传输。'
    else:
        text = f'当前不在限速时段（{window_text}），按满速传输；进入时段后自动限速。'
    return {
        'enabled': enabled,
        'scheduled': scheduled,
        'active': active,
        'window': window_text,
        'text': text,
    }


def get_download_limit(config: Optional[Dict[str, Any]] = None,
                       now: Any = None) -> Optional[int]:
    """下载限速（bytes/s）。新键优先，旧键 YOUTUBE_THROTTLED_RATE 作为兼容回退。"""
    config = config if config is not None else _load_config_cached()
    if not is_rate_limit_active(config, now):
        return None
    for key in ('DOWNLOAD_RATE_LIMIT', 'YOUTUBE_THROTTLED_RATE'):
        raw = config.get(key)
        try:
            parsed = parse_rate_limit(raw)
        except ValueError:
            logger.warning('下载限速配置 %s=%r 无法解析，已忽略', key, raw)
            continue
        if parsed:
            return parsed
    return None


def get_upload_limit(config: Optional[Dict[str, Any]] = None,
                     now: Any = None) -> Optional[int]:
    """上传限速（bytes/s），没有配置时返回 None。"""
    config = config if config is not None else _load_config_cached()
    if not is_rate_limit_active(config, now):
        return None
    raw = config.get('UPLOAD_RATE_LIMIT')
    try:
        return parse_rate_limit(raw)
    except ValueError:
        logger.warning('上传限速配置 UPLOAD_RATE_LIMIT=%r 无法解析，已忽略', raw)
        return None


def _upload_pacer() -> Pacer:
    rate = get_upload_limit()
    with _pacers['lock']:
        if _pacers['upload'] is None or _pacers['upload_rate'] != rate:
            _pacers['upload'] = Pacer(rate)
            _pacers['upload_rate'] = rate
        return _pacers['upload']


def acquire_upload_delay(num_bytes: int) -> float:
    """同步链路使用：拿到应等待的秒数，调用方自行 sleep（便于取消检测）。"""
    return _upload_pacer().acquire(num_bytes)


def throttle_upload(num_bytes: int) -> None:
    """同步节流：立即等待（acfun 分块上传用）。"""
    delay = acquire_upload_delay(num_bytes)
    if delay > 0:
        time.sleep(delay)


def throttle_upload_cancellable(num_bytes: int, cancel_event: Any = None) -> bool:
    """同步节流，但每 0.2 秒检查一次取消标志；被取消时返回 False。"""
    delay = acquire_upload_delay(num_bytes)
    if delay <= 0:
        return True
    deadline = time.monotonic() + delay
    while True:
        if cancel_event is not None and getattr(cancel_event, 'is_set', None):
            try:
                if cancel_event.is_set():
                    return False
            except Exception:
                pass
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return True
        time.sleep(min(0.2, remaining))


async def pace_async_upload(num_bytes: int) -> None:
    """异步节流：bili_sdk 分块上传用，不阻塞事件循环。"""
    delay = acquire_upload_delay(num_bytes)
    if delay > 0:
        await asyncio.sleep(delay)
