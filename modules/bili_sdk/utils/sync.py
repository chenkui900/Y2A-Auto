"""
bilibili_api.utils.sync

同步执行异步函数
"""

import asyncio
from typing import Any, TypeVar, Coroutine, Union
from asyncio.futures import Future as AsyncioFuture
from concurrent.futures import Future as ConcurrentFuture, ThreadPoolExecutor

T = TypeVar("T")


def __ensure_event_loop() -> None:
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        # 仅当当前线程没有事件循环时（RuntimeError）才新建，
        # 避免裸 except 吞掉 KeyboardInterrupt/SystemExit 等信号
        asyncio.set_event_loop(asyncio.new_event_loop())
    return asyncio.get_event_loop()


def sync(
    coroutine: Union[Coroutine[Any, Any, T], AsyncioFuture, ConcurrentFuture]
) -> T:
    """
    同步执行异步函数

    Args:
        obj (Coroutine | Future): 异步函数

    Returns:
        Any: 该异步函数的返回值
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return __ensure_event_loop().run_until_complete(coroutine)
    else:
        with ThreadPoolExecutor() as executor:
            return executor.submit(
                lambda x: __ensure_event_loop().run_until_complete(x), coroutine
            ).result()
