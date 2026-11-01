"""下载带宽「动态分配」控制器（进程内单例）。

设计目标（对应需求：并发数量只是「最多同时下载几个」，不代表带宽被永久平分；
真正的单任务速度应当按「此刻实际正在下载的任务数」实时计算）：

- 每个真正在传输视频的任务，在拉起 yt-dlp 前 ``register(task_id)``，结束后
  ``unregister(task_id)``；控制器据此知道「此刻实际有几个下载在跑」。
- 总下载限速（``DOWNLOAD_RATE_LIMIT``）由 ``rate_limiter.get_download_limit``
  实时读取（配置有 3 秒缓存，保存设置时已失效，所以改完立即生效）。
- 单个任务应得速率 = 总限速 / max(1, 实际在下载的任务数)：
  - 只有 1 个任务在下载 -> 吃掉全部带宽；
  - 同时 2 个 -> 各半；4 个 -> 各 1/4；多任务总速率始终≈设定值，不会超标。
- 当「实际下载数」变化（任务开始/结束）或用户在设置页改了限速/并发，
  ``generation`` 自增并置位 ``rebalance_event``；正在跑的 yt-dlp 进程检测到后，
  以新速率重新拉起（断点续传），**无需重启服务**。

对比旧实现：旧代码按「配置的并发上限」两级摊分，导致「并发设 4 但只有 1 个在下载」
时那个任务也只能用 1/4 带宽，看似「限速没生效」。新方案让带宽随实际负载浮动。
"""
from __future__ import annotations

import logging
import threading
from typing import Any, Dict, Optional, Set

logger = logging.getLogger(__name__)

# 重新拉起之间的最小静默窗口：多个任务几乎同时开始/结束时，让 generation 先稳定，
# 避免每一次变化都触发一次重启。单位：秒。
REBALANCE_DEBOUNCE_SECONDS = 2.0
# 单个任务在一次下载生命周期内，因限速变化而重新拉起次数的安全上限，防止异常死循环。
MAX_RELAUNCH_PER_DOWNLOAD = 64


def compute_per_task_rate(
    total_bytes_per_second: Optional[int],
    active_count: int,
) -> Optional[int]:
    """实际动态分配：总限速按「当前实际在下载的任务数」一级摊分。

    返回每个任务应得的 bytes/s；不限速（total 为空/0）或没有活动任务时返回 None。
    与旧实现不同：这里用的是「实际活动任务数」，而不是配置里的并发上限，
    因此「并发设 4 但只有 1 个在下载」时，那 1 个任务能占满全部带宽。
    """
    if not total_bytes_per_second or int(total_bytes_per_second) <= 0:
        return None
    active = max(1, int(active_count))
    total = int(total_bytes_per_second)
    return max(1, total // active)


class DownloadRateController:
    """进程内单例：跟踪实际活动下载数，并对外提供「单任务应得速率」与重平衡信号。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._active: Set[str] = set()
        self.generation = 0
        # 重平衡唤醒事件：被置位后，正在跑的下载进程应检查 generation 并视情况以新速率重拉。
        self.rebalance_event = threading.Event()

    # --- 实际活动下载数 ---
    def register(self, task_id: str) -> None:
        with self._lock:
            self._active.add(str(task_id))
        self._bump()

    def unregister(self, task_id: str) -> None:
        with self._lock:
            self._active.discard(str(task_id))
        self._bump()

    def active_count(self) -> int:
        with self._lock:
            return len(self._active)

    # --- 总限速（实时读配置，带 3 秒缓存由 rate_limiter 负责）---
    def _read_total(self) -> Optional[int]:
        try:
            from .rate_limiter import get_download_limit

            return get_download_limit()
        except Exception as exc:  # pragma: no cover - 配置读取异常不应阻断下载
            logger.warning("读取下载限速失败，本次按不限速处理: %s", exc)
            return None

    def notify_config_changed(self) -> None:
        """设置页改了限速/并发后调用：触发在跑任务以新速率重拉（无需重启服务）。"""
        self._bump()

    def _bump(self) -> None:
        with self._lock:
            self.generation += 1
            gen = self.generation
        # 在锁外置位事件，避免持锁时阻塞等待
        self.rebalance_event.set()

    # --- 给下载进程用的速率查询 ---
    def per_task_rate_bytes(self) -> Optional[int]:
        total = self._read_total()
        with self._lock:
            active = len(self._active)
        return compute_per_task_rate(total, active)

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return {
                'active_count': len(self._active),
                'active_tasks': sorted(self._active),
                'generation': self.generation,
            }


_controller = DownloadRateController()


def get_download_rate_controller() -> DownloadRateController:
    """返回进程内唯一的下载限速控制器。"""
    return _controller
