#!/usr/bin/env python
# -*- coding: utf-8 -*-

import logging
from typing import List

from .bilibili_runtime import configure_bilibili_runtime

logger = logging.getLogger("bilibili_zones")


def get_zone_list_sub() -> List[dict]:
    configure_bilibili_runtime()
    try:
        from .bili_sdk import video_zone
        return video_zone.get_zone_list_sub() or []
    except Exception as exc:
        logger.warning("读取bilibili分区数据失败: %s", exc)
        return []


def collect_valid_tids(zone_data=None) -> set:
    valid_tids = set()
    for zone in (zone_data if zone_data is not None else get_zone_list_sub()) or []:
        if not isinstance(zone, dict):
            continue
        tid = zone.get("tid")
        if tid not in (None, "", 0, "0"):
            valid_tids.add(str(tid))
        for sub in zone.get("sub") or []:
            if not isinstance(sub, dict):
                continue
            stid = sub.get("tid")
            if stid not in (None, "", 0, "0"):
                valid_tids.add(str(stid))
    return valid_tids
