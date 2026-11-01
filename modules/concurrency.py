"""并发上限的唯一来源：下载并发数量 / 上传并发数量。

以前并发上限来自 设置 → 运维与安全 → 资源控制 的「最大并发任务数 /
最大并发上传数」，和「下载并发数量」是两处独立设置，很容易互相打架：

- 改大下载并发数量，同时下载的任务数并不会变（还是并发设置那个值）；
- 两处都填了以后，限速应该按哪个数摊分也没人说得清。

现在统一由本模块决定，两个旋钮各自管一件事：

- ``YOUTUBE_DOWNLOAD_THREADS``：同时下载的任务数（下载并发数量）；
- ``UPLOAD_THREADS``：同时上传的任务数（上传并发数量）。

这两个值被三处共用：任务调度配额（TaskSlotLimiter）、上传队列配额
（UploadSlotLimiter）、下载限速摊分（youtube_handler 的 --limit-rate）。
改完设置后 TaskProcessor._refresh_runtime_limits 会立即同步到运行中的配额对象，
不需要重启服务。

关于「单任务分片连接数」：历史上为了把总限速摊到每条连接，曾引入一个独立的
「分片连接数」设置，再按「任务数 × 分片连接数」两级摊分。但分片连接数本身也是一个
需要用户理解的新旋钮，而且把每条连接的限额摊得太小（例如 200K ÷ 4 任务 ÷ 4 连接 =
每条仅 12.5K），yt-dlp 的软限速对过小的单连接限额约束不可靠，反而容易总速度超标。

现在的做法更直接：``--concurrent-fragments`` 固定为 1（单任务只有 1 条连接），
总下载限速按「**当前实际在下载的任务数**」动态摊分（见 ``download_rate_controller``）——
每个任务分到 ``总限速 / 实际活动下载数``，所以并发设 4 但只有 1 个在下载时那 1 个任务
能吃满全部带宽，多个任务同时下载时总速率恰好等于设定值，且单连接限额足够大、yt-dlp 能稳定约束。
"""
from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

# 缺省值：下载 4 个任务、上传 1 个任务，与历史行为接近
DEFAULT_DOWNLOAD_THREADS = 4
DEFAULT_UPLOAD_THREADS = 1

# 上下限：并发数量会直接变成同时进行的任务数，放太大容易把内存和出口连接打满
MAX_DOWNLOAD_THREADS = 16
MAX_UPLOAD_THREADS = 8


def _config_dict(config: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """拿到配置字典；没传或传空时回落到 config.json。"""
    if isinstance(config, dict) and config:
        return config
    try:
        from .config_manager import load_config

        loaded = load_config()
        if isinstance(loaded, dict):
            return loaded
    except Exception:
        pass
    return {}


def _as_thread_count(value: Any, default: int, maximum: int) -> int:
    """把配置里的值收敛到 [1, maximum]；非数字回退默认值。"""
    if isinstance(value, bool):
        return default
    if value is None:
        return default
    try:
        number = int(float(str(value).strip()))
    except (TypeError, ValueError):
        return default
    if number < 1:
        return 1
    return min(number, maximum)


def get_download_threads(config: Optional[Dict[str, Any]] = None) -> int:
    """下载并发数量（= 同时下载的任务数）。"""
    cfg = _config_dict(config)
    return _as_thread_count(
        cfg.get('YOUTUBE_DOWNLOAD_THREADS'), DEFAULT_DOWNLOAD_THREADS, MAX_DOWNLOAD_THREADS
    )


def get_upload_threads(config: Optional[Dict[str, Any]] = None) -> int:
    """上传并发数量（= 同时上传的任务数）。"""
    cfg = _config_dict(config)
    return _as_thread_count(
        cfg.get('UPLOAD_THREADS'), DEFAULT_UPLOAD_THREADS, MAX_UPLOAD_THREADS
    )


def get_download_concurrency(config: Optional[Dict[str, Any]] = None) -> int:
    """同时处理（下载）的任务配额，即下载并发数量。"""
    return get_download_threads(config)


def get_upload_concurrency(config: Optional[Dict[str, Any]] = None) -> int:
    """同时上传的任务配额，即上传并发数量。"""
    return get_upload_threads(config)


def split_download_rate(
    total_bytes_per_second: Optional[int],
    config: Optional[Dict[str, Any]] = None,
    active_count: Optional[int] = None,
) -> Tuple[Optional[int], Optional[int]]:
    """把总下载限速摊分到每个任务。

    返回 ``(per_task, per_connection)``；总限速为 0/None 时返回 ``(None, None)``。

    默认按「下载并发数量」（配置上限）摊分，以兼容旧调用与单元测试；但实际下载限速
    现在由 ``modules/download_rate_controller`` 按「**当前实际在下载的任务数**」动态摊分
    （见该模块的 ``compute_per_task_rate``）——并发设 4 但只有 1 个在下载时，那 1 个任务
    能吃满全部带宽；同时 2 个下载就各半。传入 ``active_count`` 可直接指定实际活动数。

    yt-dlp 的 ``--limit-rate`` 是按每条连接生效的，而我们把 ``--concurrent-fragments``
    固定为 1（单任务只有 1 条连接），因此「每任务配额」与「每条连接配额」是同一个值：
    ``总限速 / 实际活动下载数``。

    这样 N 个下载任务同时跑时，总速率 = N × (总限速 / N) = 总限速，不会超标；
    又因为单连接限额足够大，yt-dlp 的软限速能稳定约束。
    """
    if not total_bytes_per_second or int(total_bytes_per_second) <= 0:
        return None, None
    total = int(total_bytes_per_second)
    if active_count is None:
        active_count = get_download_concurrency(config)
    tasks = max(1, int(active_count))
    per_task = max(1, total // tasks)
    # --concurrent-fragments 固定为 1，单任务只有 1 条连接，故两者相等
    return per_task, per_task


def describe_concurrency(config: Optional[Dict[str, Any]] = None) -> str:
    """给日志/提示用的摘要。"""
    return (
        f"同时下载 {get_download_concurrency(config)} 个任务，"
        f"同时上传 {get_upload_concurrency(config)} 个任务"
    )
