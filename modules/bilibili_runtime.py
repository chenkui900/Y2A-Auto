#!/usr/bin/env python
# -*- coding: utf-8 -*-

import logging
import os
from typing import Optional

logger = logging.getLogger("bilibili_runtime")

_INITIALIZED = False
_LAST_ERROR: Optional[str] = None


def configure_bilibili_runtime() -> bool:
    """Configure the internal Bilibili SDK network runtime once per process."""
    global _INITIALIZED, _LAST_ERROR
    if _INITIALIZED:
        return True

    try:
        from .bili_sdk import request_settings

        impersonate = os.environ.get("BILIBILI_IMPERSONATE", "chrome131").strip()
        if impersonate:
            request_settings.set("impersonate", impersonate)
        _INITIALIZED = True
        _LAST_ERROR = None
        return True
    except Exception as exc:
        _LAST_ERROR = str(exc)
        logger.warning("配置 bilibili-api 网络运行时失败: %s", exc)
        return False


def get_bilibili_runtime_error() -> Optional[str]:
    return _LAST_ERROR
