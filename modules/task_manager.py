#!/usr/bin/env python
# -*- coding: utf-8 -*-

import os
import sys
import time
import json
import uuid
import sqlite3
import html
import logging
import shutil
import threading
import gc
import shlex
from datetime import datetime, timedelta
from logging.handlers import RotatingFileHandler
import re
import math
import unicodedata
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.executors.pool import ThreadPoolExecutor as APSchedulerThreadPoolExecutor
from apscheduler.schedulers.base import SchedulerNotRunningError
import queue
from .utils import get_app_root_dir, get_app_subdir
from .ffmpeg_manager import get_ffmpeg_path, get_ffprobe_path
from .subtitle_style import (
    apply_style_overrides,
    parse_style_overrides,
    diagnose_style,
)
from .video_encoder_params import (
    VUI_REPORT_NOTHING,
    VUI_REPORT_USER_NO_VUI,
    VUI_REPORT_USER_OWNS_CODEC,
    VUI_REPORT_USER_VUI,
    VUI_REPORT_WRITTEN,
    build_audio_params,
    build_encoder_params,
    custom_params_vui_report,
    format_quality_value,
    normalize_color_metadata,
    normalize_cpu_codec,
    parse_encoder_config,
    private_param_option_name,
    recommend_quality,
    resolve_color_metadata,
)
from .notifications import (
    EVENT_TASK_ADDED,
    EVENT_TASK_COMPLETED,
    EVENT_TASK_FAILED,
    NotificationEvent,
    emit_notification_event,
)
from .tag_presets import (
    BILIBILI_TAG_LIMIT,
    PLATFORM_ACFUN,
    PLATFORM_BILIBILI,
    is_preset_tags_effective,
    merge_tags,
    parse_preset_tags,
    resolve_upload_tags,
)
import subprocess
from typing import Any, Dict
from werkzeug.security import safe_join

# --- subprocess 句柄管理（yt-dlp / ffmpeg 用）---
_ACTIVE_PROCESSES = {}
_ACTIVE_PROCESSES_LOCK = threading.Lock()

def register_active_process(task_id, process):
    with _ACTIVE_PROCESSES_LOCK:
        _ACTIVE_PROCESSES[str(task_id)] = process

def unregister_active_process(task_id, process=None):
    with _ACTIVE_PROCESSES_LOCK:
        current = _ACTIVE_PROCESSES.get(str(task_id))
        if current is not None and (process is None or current is process):
            _ACTIVE_PROCESSES.pop(str(task_id), None)

def terminate_active_process(task_id):
    with _ACTIVE_PROCESSES_LOCK:
        proc = _ACTIVE_PROCESSES.get(str(task_id))
    if proc is None or proc.poll() is not None:
        return False
    try:
        proc.terminate()
        return True
    except Exception:
        return False

def _get_memory_usage_percent():
    try:
        try:
            import psutil
        except Exception:
            return 50.0
        memory = psutil.virtual_memory()
        return memory.percent
    except ImportError:
        return 50.0
    except Exception:
        return 50.0

def _should_reduce_concurrency():
    memory_percent = _get_memory_usage_percent()
    return memory_percent > 80.0

DB_DIR = get_app_subdir('db')
os.makedirs(DB_DIR, exist_ok=True)
DB_PATH = os.path.join(DB_DIR, 'tasks.db')
LOGS_DIR = get_app_subdir('logs')
DOWNLOADS_DIR = get_app_subdir('downloads')
PROCESS_TERMINATE_WAIT_SECONDS = 5
DB_CONNECT_TIMEOUT_SECONDS = 10
DB_BUSY_TIMEOUT_MS = 30000
DB_WRITE_RETRY_TIMES = 5
DB_WRITE_RETRY_SLEEP_SECONDS = 0.2
METADATA_TRANSLATION_ERROR_CATEGORY = 'metadata_translation_failed'
CONTENT_MODERATION_ERROR_CATEGORY = 'content_moderation_failed'

EMBED_TIMEOUT_MIN_SECONDS = 1800
EMBED_TIMEOUT_MAX_SECONDS = 10800
EMBED_TIMEOUT_X265_FACTOR = 5.0
EMBED_TIMEOUT_MAX_SECONDS_X265 = 28800

def _convert_vtt_text_to_srt_text(vtt_content: str) -> str:
    def _normalize_newlines(text: str) -> str:
        normalized = str(text or '').replace('\r\n', '\n').replace('\r', '\n')
        if normalized.startswith('\ufeff'):
            normalized = normalized[1:]
        return normalized

    def _time_to_ms(value: str) -> int:
        hours, minutes, seconds = str(value).split(':')
        whole_seconds, milliseconds = str(seconds).split('.')
        return (int(hours) * 3600000 + int(minutes) * 60000 + int(whole_seconds) * 1000 + int(milliseconds))

    def _clean_text_line(line: str) -> str:
        text = html.unescape(str(line or ''))
        text = re.sub(r'<\d{2}:\d{2}:\d{2}\.\d{3}>', '', text)
        text = re.sub(r'</?[^>]+>', '', text)
        text = re.sub(r'{[^}]*}', '', text)
        text = re.sub(r'\s+', ' ', text).strip()
        return text

    def _dedupe_lines(lines):
        result, seen = [], set()
        for line in lines:
            normalized = str(line or '').strip()
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            result.append(normalized)
        return result

    def _select_cue_text(cleaned_lines):
        unique_lines = _dedupe_lines(cleaned_lines)
        if not unique_lines:
            return ''
        if len(unique_lines) == 1:
            return unique_lines[0]
        longest_line = max(unique_lines, key=lambda item: (len(item), item))
        if all(line in longest_line for line in unique_lines):
            return longest_line
        return '\n'.join(unique_lines)

    content = _normalize_newlines(vtt_content)
    blocks = re.split(r'\n\s*\n+', content.strip())
    cues = []

    for block in blocks:
        lines = [line.rstrip() for line in block.split('\n')]
        if not lines:
            continue
        first_line = lines[0].strip()
        upper_first_line = first_line.upper()
        if (upper_first_line.startswith('WEBVTT') or first_line.startswith('Kind:')
            or first_line.startswith('Language:') or first_line.startswith('X-TIMESTAMP-MAP=')
            or upper_first_line.startswith('NOTE') or upper_first_line == 'STYLE' or upper_first_line == 'REGION'):
            continue
        time_line_index = None
        for index, line in enumerate(lines):
            if '-->' in line:
                time_line_index = index
                break
        if time_line_index is None:
            continue
        time_match = re.search(r'(?P<start>\d{2}:\d{2}:\d{2}\.\d{3})\s*-->\s*(?P<end>\d{2}:\d{2}:\d{2}\.\d{3})', lines[time_line_index])
        if not time_match:
            continue
        payload_lines = lines[time_line_index + 1:]
        has_inline_timestamps = any(re.search(r'<\d{2}:\d{2}:\d{2}\.\d{3}>', raw_line or '') for raw_line in payload_lines)
        cleaned_lines = [_clean_text_line(line) for line in payload_lines]
        text = _select_cue_text(cleaned_lines)
        if not text:
            continue
        start = time_match.group('start')
        end = time_match.group('end')
        cues.append({'start': start, 'end': end, 'text': text, 'has_inline_timestamps': has_inline_timestamps, 'duration_ms': max(0, _time_to_ms(end) - _time_to_ms(start))})

    if not cues:
        return ''
    optimized_cues = []
    consumed_indices = set()
    youtube_like = any(cue['has_inline_timestamps'] for cue in cues)
    if youtube_like:
        for index, cue in enumerate(cues):
            if cue['has_inline_timestamps'] or cue['duration_ms'] > 120 or index == 0:
                continue
            previous_cue = cues[index - 1]
            if not previous_cue['has_inline_timestamps']:
                continue
            optimized_cues.append({'start': previous_cue['start'], 'end': cue['end'], 'text': cue['text']})
            consumed_indices.add(index - 1)
            consumed_indices.add(index)
        for index, cue in enumerate(cues):
            if index in consumed_indices or not cue['text']:
                continue
            optimized_cues.append({'start': cue['start'], 'end': cue['end'], 'text': cue['text']})
    else:
        optimized_cues = [{'start': cue['start'], 'end': cue['end'], 'text': cue['text']} for cue in cues if cue['text']]

    optimized_cues.sort(key=lambda cue: (_time_to_ms(cue['start']), _time_to_ms(cue['end'])))
    deduped_cues = []
    for cue in optimized_cues:
        if deduped_cues and deduped_cues[-1]['text'] == cue['text']:
            if _time_to_ms(cue['end']) > _time_to_ms(deduped_cues[-1]['end']):
                deduped_cues[-1]['end'] = cue['end']
            continue
        deduped_cues.append(cue)

    srt_blocks = []
    for index, cue in enumerate(deduped_cues, 1):
        srt_blocks.append(f"{index}\n{cue['start'].replace('.', ',')} --> {cue['end'].replace('.', ',')}\n{cue['text']}")
    return '\n\n'.join(srt_blocks).strip()


class TaskCancelledError(Exception):
    pass

_TASK_CANCEL_FLAGS = {}
_TASK_CANCEL_LOCK = threading.Lock()
_ACTIVE_TASK_IDS = set()
_ACTIVE_TASKS_LOCK = threading.Lock()
_TASK_SCHEDULING_LOCK = threading.RLock()

def get_task_cancel_event(task_id):
    if not task_id:
        return None
    with _TASK_CANCEL_LOCK:
        event = _TASK_CANCEL_FLAGS.get(task_id)
        if not event:
            event = threading.Event()
            _TASK_CANCEL_FLAGS[task_id] = event
        return event

def request_task_cancel(task_id):
    event = get_task_cancel_event(task_id)
    if event:
        event.set()
    return event

def is_task_cancelled(task_id):
    if not task_id:
        return False
    with _TASK_CANCEL_LOCK:
        event = _TASK_CANCEL_FLAGS.get(task_id)
        return bool(event and event.is_set())

def clear_task_cancel(task_id, clear_flag=True):
    if not task_id or not clear_flag:
        return
    with _TASK_CANCEL_LOCK:
        _TASK_CANCEL_FLAGS.pop(task_id, None)

def _mark_task_active(task_id: str) -> bool:
    if not task_id:
        return False
    with _ACTIVE_TASKS_LOCK:
        if task_id in _ACTIVE_TASK_IDS:
            return False
        _ACTIVE_TASK_IDS.add(task_id)
        return True

def _mark_task_inactive(task_id: str) -> None:
    if not task_id:
        return
    with _ACTIVE_TASKS_LOCK:
        _ACTIVE_TASK_IDS.discard(task_id)

def _is_task_active(task_id: str) -> bool:
    if not task_id:
        return False
    with _ACTIVE_TASKS_LOCK:
        return task_id in _ACTIVE_TASK_IDS

def is_task_active(task_id: str) -> bool:
    """公开接口：任务线程是否正在运行（含排队等待上传配额的阶段）。"""
    return _is_task_active(task_id)

def _get_active_task_ids() -> set:
    with _ACTIVE_TASKS_LOCK:
        return set(_ACTIVE_TASK_IDS)

def _raise_if_cancelled(task_id, task_logger=None):
    if is_task_cancelled(task_id):
        if task_logger:
            task_logger.info("检测到任务取消请求，终止任务执行")
        raise TaskCancelledError("任务已取消")

raise_if_cancelled = _raise_if_cancelled
is_task_cancel_requested = is_task_cancelled

os.makedirs(LOGS_DIR, exist_ok=True)
os.makedirs(DOWNLOADS_DIR, exist_ok=True)

TASK_STATES = {
    'PENDING': 'pending',
    'DOWNLOADING': 'downloading',
    'DOWNLOADED': 'downloaded',
    'ASR_TRANSCRIBING': 'asr_transcribing',
    'TRANSLATING_SUBTITLE': 'translating_subtitle',
    'ENCODING_VIDEO': 'encoding_video',
    'TRANSLATING': 'translating',
    'TAGGING': 'tagging',
    'PARTITIONING': 'partitioning',
    'MODERATING': 'moderating',
    'AWAITING_REVIEW': 'awaiting_manual_review',
    'SCHEDULED_UPLOAD': 'scheduled_upload',
    'READY_FOR_UPLOAD': 'ready_for_upload',
    'UPLOADING': 'uploading',
    'COMPLETED': 'completed',
    'FAILED': 'failed'
}

PROCESSING_STATES = [
    'fetching_info', 'info_fetched', TASK_STATES['TRANSLATING'], TASK_STATES['TAGGING'],
    TASK_STATES['PARTITIONING'], TASK_STATES['MODERATING'], TASK_STATES['DOWNLOADING'],
    TASK_STATES['DOWNLOADED'], TASK_STATES['ASR_TRANSCRIBING'], TASK_STATES['TRANSLATING_SUBTITLE'],
    TASK_STATES['ENCODING_VIDEO'], TASK_STATES['UPLOADING'],
]

PIPELINE_CHECKPOINT_FIELD = 'pipeline_checkpoint'
PIPELINE_STAGE_FETCH_INFO = 'fetch_info'
PIPELINE_STAGE_TRANSLATE_CONTENT = 'translate_content'
PIPELINE_STAGE_GENERATE_TAGS = 'generate_tags'
PIPELINE_STAGE_RECOMMEND_PARTITION = 'recommend_partition'
PIPELINE_STAGE_MODERATE_CONTENT = 'moderate_content'
PIPELINE_STAGE_DOWNLOAD_VIDEO = 'download_video'
PIPELINE_STAGE_TRANSLATE_SUBTITLE = 'translate_subtitle'
PIPELINE_STAGE_UPLOAD_TO_ACFUN = 'upload_to_acfun'

PIPELINE_STAGE_ORDER = [
    PIPELINE_STAGE_FETCH_INFO, PIPELINE_STAGE_TRANSLATE_CONTENT, PIPELINE_STAGE_GENERATE_TAGS,
    PIPELINE_STAGE_RECOMMEND_PARTITION, PIPELINE_STAGE_MODERATE_CONTENT, PIPELINE_STAGE_DOWNLOAD_VIDEO,
    PIPELINE_STAGE_TRANSLATE_SUBTITLE, PIPELINE_STAGE_UPLOAD_TO_ACFUN,
]

UPLOAD_TARGET_ACFUN = 'acfun'
UPLOAD_TARGET_BILIBILI = 'bilibili'
UPLOAD_TARGET_BOTH = 'both'
VALID_UPLOAD_TARGETS = {UPLOAD_TARGET_ACFUN, UPLOAD_TARGET_BILIBILI, UPLOAD_TARGET_BOTH}

PARTITION_FIELD_MAP = {
    UPLOAD_TARGET_ACFUN: {'recommended': 'recommended_partition_id_acfun', 'selected': 'selected_partition_id_acfun'},
    UPLOAD_TARGET_BILIBILI: {'recommended': 'recommended_partition_id_bilibili', 'selected': 'selected_partition_id_bilibili'},
}

_RESUME_RECOVERY_DONE = False

def normalize_upload_target(upload_target):
    target = str(upload_target or '').strip().lower()
    if target not in VALID_UPLOAD_TARGETS:
        return UPLOAD_TARGET_ACFUN
    return target

def resolve_cookie_file_path(path_value, default_relative_path, service_name="Cookie", logger_obj=None, allow_json_txt_fallback=False):
    raw_path = str(path_value or default_relative_path or '').strip()
    if not raw_path:
        return ''
    app_root = get_app_root_dir()
    resolved_path = raw_path if os.path.isabs(raw_path) else os.path.join(app_root, raw_path)
    resolved_path = os.path.normpath(resolved_path)
    if os.path.exists(resolved_path):
        return resolved_path
    if not allow_json_txt_fallback:
        return resolved_path
    base, ext = os.path.splitext(resolved_path)
    ext_lower = ext.lower()
    if ext_lower == '.json':
        alt_candidates = [base + '.txt']
    elif ext_lower == '.txt':
        alt_candidates = [base + '.json']
    else:
        alt_candidates = [resolved_path + '.json', resolved_path + '.txt']
    for alt_path in alt_candidates:
        if not os.path.exists(alt_path):
            continue
        target_logger = logger_obj if logger_obj is not None else logger
        try:
            target_logger.warning(f"{service_name} Cookies路径 {resolved_path} 不存在，自动回退到兼容路径: {alt_path}")
        except Exception:
            pass
        return alt_path
    return resolved_path

def resolve_youtube_cookies_path(config, logger_obj=None):
    target_logger = logger_obj if logger_obj is not None else logger
    configured_path = str((config or {}).get('YOUTUBE_COOKIES_PATH', 'cookies/yt_cookies.txt') or '').strip()
    resolved_path = resolve_cookie_file_path(configured_path, 'cookies/yt_cookies.txt', service_name='YouTube', logger_obj=target_logger)
    if os.path.isfile(resolved_path):
        return resolved_path
    app_root = get_app_root_dir()
    file_name = os.path.basename(configured_path or 'yt_cookies.txt')
    legacy_candidates = [os.path.join(app_root, 'config', file_name)]
    if getattr(sys, 'frozen', False) and configured_path and not os.path.isabs(configured_path):
        legacy_candidates.append(os.path.join(app_root, '_internal', configured_path))
    normalized_target = os.path.normcase(os.path.normpath(resolved_path))
    seen = {normalized_target}
    for candidate in legacy_candidates:
        normalized_candidate = os.path.normcase(os.path.normpath(candidate))
        if normalized_candidate in seen:
            continue
        seen.add(normalized_candidate)
        if not os.path.isfile(candidate):
            continue
        target_logger.warning("配置的YouTube Cookies文件不存在，临时回退到旧版路径: %s；请重新上传或同步 Cookies 以迁移到配置路径。", candidate)
        return candidate
    target_logger.warning("指定的YouTube Cookies文件不存在: %s", resolved_path)
    return None

def _get_task_upload_target(task, fallback=UPLOAD_TARGET_ACFUN):
    if not task:
        return normalize_upload_target(fallback)
    task_target = task.get('upload_target')
    if not task_target:
        return normalize_upload_target(fallback)
    return normalize_upload_target(task_target)

def _task_has_upload_response(task, upload_target=None):
    if not task:
        return False
    target = normalize_upload_target(upload_target or task.get('upload_target'))
    if target == UPLOAD_TARGET_BOTH:
        return bool(task.get('acfun_upload_response')) and bool(task.get('bilibili_upload_response'))
    if target == UPLOAD_TARGET_BILIBILI:
        return bool(task.get('bilibili_upload_response'))
    return bool(task.get('acfun_upload_response'))

def _task_has_platform_upload_response(task, platform):
    if not task:
        return False
    p = normalize_upload_target(platform)
    if p == UPLOAD_TARGET_BILIBILI:
        return bool(task.get('bilibili_upload_response'))
    return bool(task.get('acfun_upload_response'))

def _build_task_notification_payload(task, overrides=None) -> dict:
    merged_task = dict(task or {})
    if overrides:
        merged_task.update(overrides)
    return {
        'task_id': str(merged_task.get('id') or '').strip(),
        'youtube_url': str(merged_task.get('youtube_url') or '').strip(),
        'upload_target': normalize_upload_target(merged_task.get('upload_target')),
        'status': str(merged_task.get('status') or '').strip(),
        'video_title_original': str(merged_task.get('video_title_original') or '').strip(),
        'video_title_translated': str(merged_task.get('video_title_translated') or '').strip(),
        'error_message': str(merged_task.get('error_message') or '').strip(),
        'asr_warning_message': str(merged_task.get('asr_warning_message') or '').strip(),
        'subtitle_warning_message': str(merged_task.get('subtitle_warning_message') or '').strip(),
        'acfun_uploaded': bool(merged_task.get('acfun_upload_response')),
        'bilibili_uploaded': bool(merged_task.get('bilibili_upload_response')),
    }

def _get_upload_platforms_for_target(upload_target):
    target = normalize_upload_target(upload_target)
    if target == UPLOAD_TARGET_BOTH:
        return [UPLOAD_TARGET_ACFUN, UPLOAD_TARGET_BILIBILI]
    if target == UPLOAD_TARGET_BILIBILI:
        return [UPLOAD_TARGET_BILIBILI]
    return [UPLOAD_TARGET_ACFUN]

def _get_effective_metadata_limits(upload_target):
    target = normalize_upload_target(upload_target)
    if target == UPLOAD_TARGET_BILIBILI:
        return {'title_limit': 80, 'description_limit': 2000}
    return {'title_limit': 50, 'description_limit': 1000}

def _get_pending_upload_platforms(task, upload_target=None):
    if not task:
        return []
    target = normalize_upload_target(upload_target or task.get('upload_target'))
    platforms = _get_upload_platforms_for_target(target)
    return [p for p in platforms if not _task_has_platform_upload_response(task, p)]

def _has_partial_upload_success(task, upload_target=None):
    if not task:
        return False
    target = normalize_upload_target(upload_target or task.get('upload_target'))
    platforms = _get_upload_platforms_for_target(target)
    if len(platforms) <= 1:
        return False
    has_success = any(_task_has_platform_upload_response(task, p) for p in platforms)
    has_pending = any(not _task_has_platform_upload_response(task, p) for p in platforms)
    return has_success and has_pending

def _get_partition_field_name(platform: str, field_type: str) -> str:
    p = normalize_upload_target(platform)
    return PARTITION_FIELD_MAP.get(p, PARTITION_FIELD_MAP[UPLOAD_TARGET_ACFUN]).get(field_type, '')

def _get_task_partition_id(task, platform, prefer_selected=True):
    if not task:
        return ''
    selected_field = _get_partition_field_name(platform, 'selected')
    recommended_field = _get_partition_field_name(platform, 'recommended')
    selected_value = str(task.get(selected_field) or '').strip()
    recommended_value = str(task.get(recommended_field) or '').strip()
    if prefer_selected:
        if selected_value: return selected_value
        if recommended_value: return recommended_value
    else:
        if recommended_value: return recommended_value
        if selected_value: return selected_value
    return ''

def _safe_json_loads(value, default):
    try:
        if value is None: return default
        if isinstance(value, (dict, list)): return value
        s = str(value).strip()
        if not s: return default
        return json.loads(s)
    except Exception:
        return default

def _normalize_tags_list(value):
    parsed = _safe_json_loads(value, [])
    if not isinstance(parsed, list): return []
    tags = []
    for tag in parsed:
        tag_text = str(tag or '').strip()
        if tag_text: tags.append(tag_text)
    return tags

def _as_bool(value):
    if isinstance(value, bool): return value
    if isinstance(value, (int, float)): return value != 0
    return str(value).strip().lower() in ('true', '1', 'on', 'yes')

def _as_int(value, default, minimum=None):
    try:
        result = int(str(value).strip()) if value is not None else int(default)
    except Exception:
        result = int(default)
    if minimum is not None: result = max(int(minimum), result)
    return result

def _normalize_task_text(value) -> str:
    return str(value or '').strip()

def _get_missing_required_translation_fields(task, config) -> list:
    if not task: return []
    missing_fields = []
    if _as_bool(config.get('TRANSLATE_TITLE', True)) and _normalize_task_text(task.get('video_title_original')):
        if not _normalize_task_text(task.get('video_title_translated')):
            missing_fields.append('title')
    if _as_bool(config.get('TRANSLATE_DESCRIPTION', True)) and _normalize_task_text(task.get('description_original')):
        if not _normalize_task_text(task.get('description_translated')):
            missing_fields.append('description')
    return missing_fields

def _build_missing_translation_review_message(field_names) -> str:
    labels = {'title': '标题', 'description': '简介'}
    normalized_fields = [str(f).strip() for f in (field_names or []) if f is not None and str(f).strip()]
    readable = [labels.get(f, f) for f in normalized_fields]
    if not readable: return "自动翻译未完成，任务已转入人工审核。"
    return f"自动翻译未完成：{'、'.join(readable)}仍缺少有效译文，任务已转入人工审核。"

SUBTITLE_QC_DISABLED = 'disabled'

def _is_asr_subtitle_artifact(task_id, subtitle_path) -> bool:
    try:
        name = os.path.basename(str(subtitle_path or '')).lower()
    except Exception:
        return False
    if not name: return False
    if name.startswith('asr_'): return True
    return name == f"asr_{str(task_id or '').lower()}.srt"

def _candidate_subtitle_paths(task, task_dir: str) -> list:
    paths = []
    if isinstance(task, dict):
        for key in ('subtitle_path_original', 'subtitle_path_translated'):
            value = str(task.get(key) or '').strip()
            if value: paths.append(value)
    for ext in ('.srt', '.vtt'):
        paths.append(os.path.join(task_dir, f"asr_{os.path.basename(task_dir)}{ext}"))
    try:
        for name in os.listdir(task_dir):
            if isinstance(name, str) and name.lower().endswith(('.srt', '.vtt')):
                paths.append(os.path.join(task_dir, name))
    except Exception:
        pass
    seen = set()
    unique_paths = []
    for path in paths:
        if path and path not in seen:
            seen.add(path)
            unique_paths.append(path)
    return unique_paths

def _is_asr_enabled(config: dict) -> bool:
    return _as_bool(config.get('SPEECH_RECOGNITION_ENABLED', False))

def _subtitle_block_reasons(config: dict, quality_state, qc_failed, *, qc_cleared: bool = False, qc_enabled=None, qc_reason=None) -> list:
    asr_blocks = _as_bool(config.get('ASR_FAILURE_BLOCKS_EMBED', True))
    if qc_enabled is None:
        qc_enabled = _as_bool(config.get('SUBTITLE_QC_ENABLED', True))
    qc_active = bool(qc_enabled)
    state = str(quality_state or '').strip().lower()
    qc_unavailable = str(qc_reason or '').strip().lower() == 'qc_unavailable'
    reasons = []
    if state == 'failed' and asr_blocks: reasons.append('asr_quality_failed')
    if qc_active and not qc_cleared:
        if qc_unavailable:
            if asr_blocks: reasons.append('subtitle_qc_rejected')
        elif _as_bool(qc_failed):
            reasons.append('subtitle_qc_rejected')
    if state == 'degraded' and asr_blocks and qc_active and not qc_cleared:
        reasons.append('asr_quality_degraded_without_strict_qc')
    return reasons

SUBTITLE_BLOCK_WARNING_MESSAGES = {
    'asr_quality_failed': 'asr_failed_block_embed',
    'asr_quality_degraded_without_strict_qc': 'asr_degraded_block_embed',
    'subtitle_qc_rejected': 'subtitle_qc_rejected',
    'qc_unavailable': 'qc_unavailable',
}

def _block_reason_to_warning(reason) -> str:
    return SUBTITLE_BLOCK_WARNING_MESSAGES.get(str(reason or '').strip(), 'asr_failed_block_embed')

SUBTITLE_BLOCK_WARNING_VALUES = frozenset(SUBTITLE_BLOCK_WARNING_MESSAGES.values())

def _is_subtitle_stage_rejected(task, config=None) -> bool:
    if not task: return False
    if config is None: return _subtitle_rejection_recorded_on_task(task)
    return not _subtitle_embed_allowed(config, task.get('subtitle_quality_state'), task.get('subtitle_qc_failed'), qc_reason=task.get('subtitle_qc_reason'))

def _subtitle_rejection_recorded_on_task(task) -> bool:
    if not task: return False
    if task.get('subtitle_qc_failed') == 1: return True
    if str(task.get('subtitle_quality_state') or '').strip().lower() == 'failed': return True
    warning = str(task.get('subtitle_warning_message') or '').strip().lower()
    return warning in SUBTITLE_BLOCK_WARNING_VALUES

def _subtitle_embed_allowed(config: dict, quality_state, qc_failed, *, qc_cleared: bool = False, qc_enabled=None, qc_reason=None, task_logger=None) -> bool:
    reasons = _subtitle_block_reasons(config, quality_state, qc_failed, qc_cleared=qc_cleared, qc_enabled=qc_enabled, qc_reason=qc_reason)
    if not reasons: return True
    if task_logger is not None:
        try:
            task_logger.warning("字幕烧录被门控拦截: quality_state=%s, qc_failed=%s, qc_cleared=%s, asr_blocks=%s, qc_enabled=%s, reasons=%s",
                str(quality_state or '').strip().lower() or 'ok', bool(_as_bool(qc_failed)), bool(qc_cleared),
                bool(_as_bool(config.get('ASR_FAILURE_BLOCKS_EMBED', True))), bool(_as_bool(config.get('SUBTITLE_QC_ENABLED', True)) if qc_enabled is None else qc_enabled), reasons)
        except Exception:
            pass
    return False

def _qc_result_allows_embed(qc_result, config) -> bool:
    if qc_result is True or qc_result is SUBTITLE_QC_DISABLED: return True
    if qc_result is None: return not _as_bool((config or {}).get('ASR_FAILURE_BLOCKS_EMBED', True))
    return False

def _parse_pipeline_checkpoint(raw_value):
    data = _safe_json_loads(raw_value, {})
    if not isinstance(data, dict): data = {}
    completed = data.get('completed', [])
    if not isinstance(completed, list): completed = []
    completed_set = {str(x) for x in completed if x and str(x) in set(PIPELINE_STAGE_ORDER)}
    return {'version': int(data.get('version', 1) or 1), 'completed': sorted(completed_set, key=lambda s: PIPELINE_STAGE_ORDER.index(s)), 'updated_at': data.get('updated_at')}

def _infer_completed_stages_from_task(task, config=None):
    if not task: return set()
    completed = set()
    if task.get('metadata_json_path_local') or task.get('video_title_original') or task.get('description_original'):
        completed.add(PIPELINE_STAGE_FETCH_INFO)
    if task.get('video_title_translated') or task.get('description_translated'):
        completed.add(PIPELINE_STAGE_TRANSLATE_CONTENT)
    if task.get('tags_generated') is not None:
        completed.add(PIPELINE_STAGE_GENERATE_TAGS)
    if (task.get('recommended_partition_id_acfun') or task.get('selected_partition_id_acfun') or task.get('recommended_partition_id_bilibili') or task.get('selected_partition_id_bilibili')):
        completed.add(PIPELINE_STAGE_RECOMMEND_PARTITION)
    if task.get('moderation_result') or task.get('status') == TASK_STATES['AWAITING_REVIEW']:
        completed.add(PIPELINE_STAGE_MODERATE_CONTENT)
    video_path = task.get('video_path_local')
    if task.get('status') == TASK_STATES['DOWNLOADED']:
        completed.add(PIPELINE_STAGE_DOWNLOAD_VIDEO)
    elif isinstance(video_path, str) and video_path and os.path.exists(video_path):
        completed.add(PIPELINE_STAGE_DOWNLOAD_VIDEO)
    subtitle_rejected = _is_subtitle_stage_rejected(task, config)
    subtitle_keys = ('subtitle_path_translated',) if subtitle_rejected else ('subtitle_path_original', 'subtitle_path_translated')
    for key in subtitle_keys:
        p = task.get(key)
        if isinstance(p, str) and p and os.path.exists(p):
            completed.add(PIPELINE_STAGE_TRANSLATE_SUBTITLE)
            break
    upload_target = _get_task_upload_target(task)
    if _task_has_upload_response(task, upload_target) or task.get('status') == TASK_STATES['COMPLETED']:
        completed.add(PIPELINE_STAGE_UPLOAD_TO_ACFUN)
    return completed

def _get_completed_stages(task, config=None):
    cp = _parse_pipeline_checkpoint(task.get(PIPELINE_CHECKPOINT_FIELD) if task else None)
    completed = set(cp.get('completed', []) or [])
    completed |= _infer_completed_stages_from_task(task, config)
    if _is_subtitle_stage_rejected(task, config):
        completed.discard(PIPELINE_STAGE_TRANSLATE_SUBTITLE)
    return completed

def _persist_pipeline_checkpoint(task_id, completed_stages):
    stages = [s for s in PIPELINE_STAGE_ORDER if s in set(completed_stages or set())]
    payload = {'version': 1, 'completed': stages, 'updated_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
    update_task(task_id, silent=True, **{PIPELINE_CHECKPOINT_FIELD: json.dumps(payload, ensure_ascii=False)})

def _mark_stage_done(task_id, completed_stages, stage):
    if stage in completed_stages: return completed_stages
    completed_stages = set(completed_stages or set())
    completed_stages.add(stage)
    _persist_pipeline_checkpoint(task_id, completed_stages)
    return completed_stages

def recover_interrupted_tasks_to_pending():
    processing_states = PROCESSING_STATES
    conn = get_db_connection()
    try:
        placeholders = ','.join(['?'] * len(processing_states))
        cursor = conn.execute(f'SELECT id, status, upload_target, acfun_upload_response, bilibili_upload_response FROM tasks WHERE status IN ({placeholders})', tuple(processing_states))
        rows = cursor.fetchall() or []
        if not rows: return 0
        now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        recovered = 0
        for row in rows:
            task_id = row['id']
            upload_target = normalize_upload_target(row['upload_target'])
            has_acfun_resp = bool(row['acfun_upload_response'])
            has_bilibili_resp = bool(row['bilibili_upload_response'])
            if upload_target == UPLOAD_TARGET_BOTH:
                has_upload_resp = has_acfun_resp and has_bilibili_resp
                has_partial_upload_resp = has_acfun_resp != has_bilibili_resp
            elif upload_target == UPLOAD_TARGET_BILIBILI:
                has_upload_resp = has_bilibili_resp
                has_partial_upload_resp = False
            else:
                has_upload_resp = has_acfun_resp
                has_partial_upload_resp = False
            if has_upload_resp:
                conn.execute('UPDATE tasks SET status = ?, updated_at = ? WHERE id = ?', (TASK_STATES['COMPLETED'], now_str, task_id))
                recovered += 1
                continue
            if has_partial_upload_resp:
                conn.execute('UPDATE tasks SET status = ?, updated_at = ? WHERE id = ?', (TASK_STATES['FAILED'], now_str, task_id))
                recovered += 1
                continue
            conn.execute('''UPDATE tasks SET status = ?, updated_at = ?, subtitle_qc_failed = 0, subtitle_qc_reason = NULL, subtitle_qc_score = NULL, subtitle_qc_checked_at = NULL, subtitle_quality_state = NULL, asr_warning_message = NULL, subtitle_warning_message = NULL WHERE id = ?''', (TASK_STATES['PENDING'], now_str, task_id))
            recovered += 1
        conn.commit()
        if recovered: logger.info(f"断点续跑：已恢复 {recovered} 个处理中任务为 pending")
        return recovered
    except Exception as e:
        logger.warning(f"断点续跑：恢复处理中任务失败（忽略）：{e}")
        return 0
    finally:
        conn.close()

def setup_logger(name):
    logger = logging.getLogger(name)
    if not logger.handlers:
        logger.setLevel(logging.INFO)
        log_file = os.path.join(LOGS_DIR, f'{name}.log')
        file_handler = RotatingFileHandler(log_file, maxBytes=10485760, backupCount=5, encoding='utf-8')
        file_formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
        file_handler.setFormatter(file_formatter)
        file_handler.setLevel(logging.INFO)
        logger.addHandler(file_handler)
        console_handler = logging.StreamHandler()
        console_handler.setFormatter(file_formatter)
        console_handler.setLevel(logging.INFO)
        logger.addHandler(console_handler)
    return logger

logger = setup_logger('task_manager')

_TASK_EVENT_SUBSCRIBERS = set()
_TASK_EVENT_LOCK = threading.Lock()
_TASK_EVENT_QUEUE_SIZE = 128

def register_task_updates_listener():
    listener_queue = queue.Queue(maxsize=_TASK_EVENT_QUEUE_SIZE)
    with _TASK_EVENT_LOCK: _TASK_EVENT_SUBSCRIBERS.add(listener_queue)
    return listener_queue

def unregister_task_updates_listener(listener_queue):
    with _TASK_EVENT_LOCK: _TASK_EVENT_SUBSCRIBERS.discard(listener_queue)

def _emit_task_event(event):
    with _TASK_EVENT_LOCK: subscribers = list(_TASK_EVENT_SUBSCRIBERS)
    for listener in subscribers:
        try: listener.put_nowait(event)
        except queue.Full:
            try: listener.get_nowait()
            except queue.Empty: pass
            try: listener.put_nowait(event)
            except queue.Full: continue

def publish_task_event(event_type='refresh', data=None):
    event_payload = {'type': event_type, 'data': data or {}, 'timestamp': datetime.utcnow().isoformat()}
    _emit_task_event(event_payload)

def setup_task_logger(task_id):
    log_file = os.path.join(LOGS_DIR, f'task_{task_id}.log')
    logger = logging.getLogger(f'task_{task_id}')
    if not logger.handlers:
        logger.setLevel(logging.INFO)
        file_handler = RotatingFileHandler(log_file, maxBytes=5242880, backupCount=3, encoding='utf-8')
        file_formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
        file_handler.setFormatter(file_formatter)
        file_handler.setLevel(logging.INFO)
        logger.addHandler(file_handler)
        logger.propagate = False
    return logger

def init_db():
    conn = sqlite3.connect(DB_PATH, timeout=DB_CONNECT_TIMEOUT_SECONDS)
    try:
        conn.execute(f'PRAGMA busy_timeout = {DB_BUSY_TIMEOUT_MS}')
        conn.execute('PRAGMA journal_mode = WAL')
        conn.execute('PRAGMA synchronous = NORMAL')
    except Exception: pass
    cursor = conn.cursor()
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS tasks (
        id TEXT PRIMARY KEY,
        youtube_url TEXT NOT NULL,
        upload_target TEXT DEFAULT 'acfun',
        status TEXT NOT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        completed_at TIMESTAMP,
        video_title_original TEXT,
        video_title_translated TEXT,
        description_original TEXT,
        description_translated TEXT,
        tags_generated TEXT,
        recommended_partition_id TEXT,
        selected_partition_id TEXT,
        recommended_partition_id_acfun TEXT,
        selected_partition_id_acfun TEXT,
        recommended_partition_id_bilibili TEXT,
        selected_partition_id_bilibili TEXT,
        cover_path_local TEXT,
        video_path_local TEXT,
        subtitle_path_original TEXT,
        subtitle_path_translated TEXT,
        subtitle_language_detected TEXT,
        subtitle_qc_failed INTEGER DEFAULT 0,
        subtitle_qc_reason TEXT,
        subtitle_qc_score REAL,
        subtitle_qc_checked_at TIMESTAMP,
        metadata_json_path_local TEXT,
        moderation_result TEXT,
        error_message TEXT,
        error_category TEXT,
        pipeline_checkpoint TEXT,
        upload_progress TEXT,
        acfun_upload_response TEXT,
        bilibili_upload_response TEXT,
        asr_warning_message TEXT,
        subtitle_warning_message TEXT,
        subtitle_quality_state TEXT
    )
    ''')
    conn.commit()
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS schema_migrations (
        migration_key TEXT PRIMARY KEY,
        applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    ''')
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS app_meta (
        key TEXT PRIMARY KEY,
        value TEXT
    )
    ''')
    conn.commit()
    try:
        cursor.execute("PRAGMA table_info(tasks)")
        columns = [row[1] for row in cursor.fetchall()]
        
        if 'completed_at' not in columns:
            cursor.execute("ALTER TABLE tasks ADD COLUMN completed_at TIMESTAMP")
            logger.info("数据库升级：添加completed_at字段")
            conn.commit()

        if 'upload_progress' not in columns:
            cursor.execute("ALTER TABLE tasks ADD COLUMN upload_progress TEXT")
            conn.commit()
        if 'subtitle_qc_failed' not in columns:
            cursor.execute("ALTER TABLE tasks ADD COLUMN subtitle_qc_failed INTEGER DEFAULT 0")
            conn.commit()
        if 'subtitle_qc_reason' not in columns:
            cursor.execute("ALTER TABLE tasks ADD COLUMN subtitle_qc_reason TEXT")
            conn.commit()
        if 'subtitle_qc_score' not in columns:
            cursor.execute("ALTER TABLE tasks ADD COLUMN subtitle_qc_score REAL")
            conn.commit()
        if 'subtitle_qc_checked_at' not in columns:
            cursor.execute("ALTER TABLE tasks ADD COLUMN subtitle_qc_checked_at TIMESTAMP")
            conn.commit()
        if 'pipeline_checkpoint' not in columns:
            cursor.execute("ALTER TABLE tasks ADD COLUMN pipeline_checkpoint TEXT")
            conn.commit()
        if 'error_category' not in columns:
            cursor.execute("ALTER TABLE tasks ADD COLUMN error_category TEXT")
            cursor.execute("""UPDATE tasks SET error_category = ? WHERE status = ? AND error_category IS NULL AND (error_message LIKE '自动翻译失败%' OR error_message LIKE '自动翻译未完成%')""", (METADATA_TRANSLATION_ERROR_CATEGORY, TASK_STATES['AWAITING_REVIEW']))
            conn.commit()
        if 'upload_target' not in columns:
            cursor.execute("ALTER TABLE tasks ADD COLUMN upload_target TEXT DEFAULT 'acfun'")
            conn.commit()
        if 'bilibili_upload_response' not in columns:
            cursor.execute("ALTER TABLE tasks ADD COLUMN bilibili_upload_response TEXT")
            conn.commit()
        if 'recommended_partition_id_acfun' not in columns:
            cursor.execute("ALTER TABLE tasks ADD COLUMN recommended_partition_id_acfun TEXT")
            conn.commit()
        if 'selected_partition_id_acfun' not in columns:
            cursor.execute("ALTER TABLE tasks ADD COLUMN selected_partition_id_acfun TEXT")
            conn.commit()
        if 'recommended_partition_id_bilibili' not in columns:
            cursor.execute("ALTER TABLE tasks ADD COLUMN recommended_partition_id_bilibili TEXT")
            conn.commit()
        if 'selected_partition_id_bilibili' not in columns:
            cursor.execute("ALTER TABLE tasks ADD COLUMN selected_partition_id_bilibili TEXT")
            conn.commit()
        if 'scheduled_upload_at' not in columns:
            cursor.execute("ALTER TABLE tasks ADD COLUMN scheduled_upload_at TEXT")
            logger.info("数据库升级：添加scheduled_upload_at字段（定时上传）")
            conn.commit()
        if 'queue_position' not in columns:
            cursor.execute("ALTER TABLE tasks ADD COLUMN queue_position INTEGER")
            logger.info("数据库升级：添加queue_position字段（上传队列位次排序）")
            conn.commit()
        cursor.execute("PRAGMA table_info(tasks)")
        columns = [row[1] for row in cursor.fetchall()]
        cursor.execute("SELECT 1 FROM schema_migrations WHERE migration_key = ? LIMIT 1", ('tasks_partition_backfill_v1',))
        partition_backfill_done = cursor.fetchone() is not None
        if not partition_backfill_done:
            try:
                if 'selected_partition_id' in columns and 'selected_partition_id_acfun' in columns:
                    cursor.execute("""UPDATE tasks SET selected_partition_id_acfun = selected_partition_id WHERE (upload_target = 'acfun' OR upload_target IS NULL OR TRIM(upload_target) = '') AND (selected_partition_id_acfun IS NULL OR TRIM(selected_partition_id_acfun) = '') AND selected_partition_id IS NOT NULL AND TRIM(selected_partition_id) <> ''""")
                if 'recommended_partition_id' in columns and 'recommended_partition_id_acfun' in columns:
                    cursor.execute("""UPDATE tasks SET recommended_partition_id_acfun = recommended_partition_id WHERE (upload_target = 'acfun' OR upload_target IS NULL OR TRIM(upload_target) = '') AND (recommended_partition_id_acfun IS NULL OR TRIM(recommended_partition_id_acfun) = '') AND recommended_partition_id IS NOT NULL AND TRIM(recommended_partition_id) <> ''""")
                if 'selected_partition_id' in columns and 'selected_partition_id_bilibili' in columns:
                    cursor.execute("""UPDATE tasks SET selected_partition_id_bilibili = selected_partition_id WHERE upload_target = 'bilibili' AND (selected_partition_id_bilibili IS NULL OR TRIM(selected_partition_id_bilibili) = '') AND selected_partition_id IS NOT NULL AND TRIM(selected_partition_id) <> ''""")
                if 'selected_partition_id' in columns:
                    if 'selected_partition_id_acfun' in columns:
                        cursor.execute("""UPDATE tasks SET selected_partition_id_acfun = selected_partition_id WHERE upload_target = 'both' AND (selected_partition_id_acfun IS NULL OR TRIM(selected_partition_id_acfun) = '') AND selected_partition_id IS NOT NULL AND TRIM(selected_partition_id) <> ''""")
                    if 'selected_partition_id_bilibili' in columns:
                        cursor.execute("""UPDATE tasks SET selected_partition_id_bilibili = selected_partition_id WHERE upload_target = 'both' AND (selected_partition_id_bilibili IS NULL OR TRIM(selected_partition_id_bilibili) = '') AND selected_partition_id IS NOT NULL AND TRIM(selected_partition_id) <> ''""")
                if 'recommended_partition_id' in columns and 'recommended_partition_id_bilibili' in columns:
                    cursor.execute("""UPDATE tasks SET recommended_partition_id_bilibili = recommended_partition_id WHERE upload_target = 'bilibili' AND (recommended_partition_id_bilibili IS NULL OR TRIM(recommended_partition_id_bilibili) = '') AND recommended_partition_id IS NOT NULL AND TRIM(recommended_partition_id) <> ''""")
                if 'recommended_partition_id' in columns:
                    if 'recommended_partition_id_acfun' in columns:
                        cursor.execute("""UPDATE tasks SET recommended_partition_id_acfun = recommended_partition_id WHERE upload_target = 'both' AND (recommended_partition_id_acfun IS NULL OR TRIM(recommended_partition_id_acfun) = '') AND recommended_partition_id IS NOT NULL AND TRIM(recommended_partition_id) <> ''""")
                    if 'recommended_partition_id_bilibili' in columns:
                        cursor.execute("""UPDATE tasks SET recommended_partition_id_bilibili = recommended_partition_id WHERE upload_target = 'both' AND (recommended_partition_id_bilibili IS NULL OR TRIM(recommended_partition_id_bilibili) = '') AND recommended_partition_id IS NOT NULL AND TRIM(recommended_partition_id) <> ''""")
                cursor.execute("INSERT OR IGNORE INTO schema_migrations (migration_key) VALUES (?)", ('tasks_partition_backfill_v1',))
                conn.commit()
                logger.info("数据库升级：历史任务分区字段回填迁移完成")
            except Exception as e2:
                conn.rollback()
                logger.warning("数据库升级：历史任务分区字段回填迁移失败，将在下次启动重试: %s", e2)
        else:
            logger.info("数据库升级：历史任务分区字段回填迁移已执行，跳过")
        if 'asr_warning_message' not in columns:
            cursor.execute("ALTER TABLE tasks ADD COLUMN asr_warning_message TEXT")
            conn.commit()
        cursor.execute("PRAGMA table_info(tasks)")
        columns = [row[1] for row in cursor.fetchall()]
        if 'subtitle_warning_message' not in columns:
            cursor.execute("ALTER TABLE tasks ADD COLUMN subtitle_warning_message TEXT")
            conn.commit()
        cursor.execute("PRAGMA table_info(tasks)")
        columns = [row[1] for row in cursor.fetchall()]
        if 'subtitle_quality_state' not in columns:
            cursor.execute("ALTER TABLE tasks ADD COLUMN subtitle_quality_state TEXT")
            conn.commit()
        cursor.execute("SELECT 1 FROM schema_migrations WHERE migration_key = ? LIMIT 1", ('asr_warning_message_migrate_v1',))
        if cursor.fetchone() is None:
            try:
                _asr_warning_tokens = ('vad_low_coverage', 'vad_no_speech', 'vad_failed', 'asr_no_timestamps', 'asr_failed', 'vad_no_usable_window')
                for _token in _asr_warning_tokens:
                    cursor.execute("""UPDATE tasks SET asr_warning_message = TRIM(error_message), error_message = NULL WHERE (asr_warning_message IS NULL OR TRIM(asr_warning_message) = '') AND TRIM(error_message) = ?""", (_token,))
                cursor.execute("INSERT OR IGNORE INTO schema_migrations (migration_key) VALUES (?)", ('asr_warning_message_migrate_v1',))
                conn.commit()
                logger.info("数据库升级：ASR警告字段数据迁移完成")
            except Exception as _em:
                conn.rollback()
                logger.warning("数据库升级：ASR警告字段数据迁移失败: %s", _em)
        cursor.execute("UPDATE tasks SET upload_target = 'acfun' WHERE upload_target IS NULL OR TRIM(upload_target) = ''")
        conn.commit()
    except Exception as e:
        logger.warning(f"数据库升级检查失败（可能已是最新版本）: {e}")
    conn.close()
    logger.info("数据库初始化完成")

def get_db_path():
    return DB_PATH

def get_db_connection():
    conn = sqlite3.connect(DB_PATH, timeout=DB_CONNECT_TIMEOUT_SECONDS)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute(f'PRAGMA busy_timeout = {DB_BUSY_TIMEOUT_MS}')
        conn.execute('PRAGMA journal_mode = WAL')
        conn.execute('PRAGMA synchronous = NORMAL')
    except Exception as e:
        logger.debug(f"设置SQLite连接参数失败，将使用默认参数: {e}")
    return conn

def add_task(youtube_url, upload_target=None):
    task_id = str(uuid.uuid4())
    normalized_target = normalize_upload_target(upload_target)
    conn = get_db_connection()
    try:
        if not upload_target:
            try:
                from modules.config_manager import load_config
                cfg = load_config()
                normalized_target = normalize_upload_target(cfg.get('UPLOAD_TARGET_DEFAULT', UPLOAD_TARGET_ACFUN))
            except Exception:
                normalized_target = UPLOAD_TARGET_ACFUN
        conn.execute('INSERT INTO tasks (id, youtube_url, upload_target, status) VALUES (?, ?, ?, ?)', (task_id, youtube_url, normalized_target, TASK_STATES['PENDING']))
        conn.commit()
        logger.info(f"新任务添加成功, ID: {task_id}, URL: {youtube_url}, 平台: {normalized_target}")
        try:
            from modules.config_manager import load_config
            config = load_config()
            processor = get_global_task_processor(config)
            if processor:
                import threading, time
                def delayed_trigger():
                    time.sleep(0.5)
                    processor._check_and_start_next_pending_task()
                threading.Thread(target=delayed_trigger, daemon=True).start()
                logger.info(f"已触发检查pending任务: {task_id}")
        except Exception as e:
            logger.warning(f"触发任务检查失败，但任务已成功添加: {str(e)}")
    except Exception as e:
        logger.error(f"添加任务失败: {str(e)}")
        task_id = None
    finally:
        conn.close()
    if task_id:
        publish_task_event('task_added', {'task_id': task_id})
        emit_notification_event(NotificationEvent(event_type=EVENT_TASK_ADDED, payload={'task_id': task_id, 'youtube_url': youtube_url, 'upload_target': normalized_target}))
    return task_id

def update_task(task_id, silent=False, **kwargs):
    if not kwargs:
        return False
    status_changed = 'status' in kwargs
    new_status = kwargs.get('status') if status_changed else None
    kwargs['updated_at'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    
    if kwargs.get('status') == TASK_STATES['COMPLETED']:
        kwargs['completed_at'] = kwargs['updated_at']

    ALLOWED_COLUMNS = {
        'youtube_url': 'youtube_url = ?',
        'upload_target': 'upload_target = ?',
        'status': 'status = ?',
        'created_at': 'created_at = ?',
        'updated_at': 'updated_at = ?',
        'completed_at': 'completed_at = ?',
        'video_title_original': 'video_title_original = ?',
        'video_title_translated': 'video_title_translated = ?',
        'description_original': 'description_original = ?',
        'description_translated': 'description_translated = ?',
        'tags_generated': 'tags_generated = ?',
        'recommended_partition_id': 'recommended_partition_id = ?',
        'selected_partition_id': 'selected_partition_id = ?',
        'recommended_partition_id_acfun': 'recommended_partition_id_acfun = ?',
        'selected_partition_id_acfun': 'selected_partition_id_acfun = ?',
        'recommended_partition_id_bilibili': 'recommended_partition_id_bilibili = ?',
        'selected_partition_id_bilibili': 'selected_partition_id_bilibili = ?',
        'cover_path_local': 'cover_path_local = ?',
        'video_path_local': 'video_path_local = ?',
        'subtitle_path_original': 'subtitle_path_original = ?',
        'subtitle_path_translated': 'subtitle_path_translated = ?',
        'subtitle_language_detected': 'subtitle_language_detected = ?',
        'subtitle_qc_failed': 'subtitle_qc_failed = ?',
        'subtitle_qc_reason': 'subtitle_qc_reason = ?',
        'subtitle_qc_score': 'subtitle_qc_score = ?',
        'subtitle_qc_checked_at': 'subtitle_qc_checked_at = ?',
        'metadata_json_path_local': 'metadata_json_path_local = ?',
        'moderation_result': 'moderation_result = ?',
        'error_message': 'error_message = ?',
        'error_category': 'error_category = ?',
        'pipeline_checkpoint': 'pipeline_checkpoint = ?',
        'upload_progress': 'upload_progress = ?',
        'scheduled_upload_at': 'scheduled_upload_at = ?',
        'acfun_upload_response': 'acfun_upload_response = ?',
        'bilibili_upload_response': 'bilibili_upload_response = ?',
        'asr_warning_message': 'asr_warning_message = ?',
        'subtitle_warning_message': 'subtitle_warning_message = ?',
        'subtitle_quality_state': 'subtitle_quality_state = ?',
    }
    filtered_items = [(k, v) for k, v in kwargs.items() if k in ALLOWED_COLUMNS]
    filtered_kwargs = dict(filtered_items)
    if not filtered_kwargs:
        return False
    set_clause = ', '.join(ALLOWED_COLUMNS[key] for key, _ in filtered_items)
    values = [value for _, value in filtered_items]
    values.append(task_id)
    conn = get_db_connection()
    try:
        for attempt in range(1, DB_WRITE_RETRY_TIMES + 1):
            try:
                existing_row = conn.execute('SELECT * FROM tasks WHERE id = ?', (task_id,)).fetchone()
                existing_task = dict(existing_row) if existing_row else None
                previous_status = existing_task.get('status') if existing_task else None
                conn.execute(f'UPDATE tasks SET {set_clause} WHERE id = ?', values)
                conn.commit()
                event_type = 'task_updated'
                event_payload = {'task_id': task_id, 'status': filtered_kwargs.get('status')}
                changed_fields = {key for key in filtered_kwargs.keys() if key != 'updated_at'}
                if changed_fields == {'upload_progress'}:
                    current_status = filtered_kwargs.get('status')
                    if current_status is None:
                        try:
                            row = conn.execute('SELECT status FROM tasks WHERE id = ?', (task_id,)).fetchone()
                            current_status = row['status'] if row else None
                        except Exception: current_status = None
                    event_type = 'task_progress'
                    event_payload = {'task_id': task_id, 'status': current_status, 'upload_progress': filtered_kwargs.get('upload_progress'), 'updated_at': filtered_kwargs.get('updated_at')}
                publish_task_event(event_type, event_payload)
                if status_changed and previous_status != filtered_kwargs.get('status') and filtered_kwargs.get('status') in (TASK_STATES['COMPLETED'], TASK_STATES['FAILED']):
                    merged_task = dict(existing_task or {})
                    merged_task.update(filtered_kwargs)
                    merged_task['id'] = task_id
                    emit_notification_event(NotificationEvent(event_type=(EVENT_TASK_COMPLETED if filtered_kwargs.get('status') == TASK_STATES['COMPLETED'] else EVENT_TASK_FAILED), payload=_build_task_notification_payload(merged_task, overrides={'previous_status': previous_status})))
                if not silent:
                    logger.info(f"任务 {task_id} 更新成功: {filtered_kwargs}")
                return True
            except sqlite3.OperationalError as e:
                err_msg = str(e).lower()
                is_locked = ('database is locked' in err_msg or 'database is busy' in err_msg or 'locked' in err_msg or 'busy' in err_msg)
                if is_locked and attempt < DB_WRITE_RETRY_TIMES:
                    try: conn.rollback()
                    except Exception: pass
                    time.sleep(DB_WRITE_RETRY_SLEEP_SECONDS * attempt)
                    continue
                logger.error(f"更新任务 {task_id} 失败 (attempt={attempt}/{DB_WRITE_RETRY_TIMES}): {str(e)}")
                return False
            except Exception as e:
                logger.error(f"更新任务 {task_id} 失败: {str(e)}")
                return False
    finally:
        conn.close()

def get_task(task_id):
    conn = get_db_connection()
    try:
        cursor = conn.execute('SELECT * FROM tasks WHERE id = ?', (task_id,))
        task = cursor.fetchone()
        return dict(task) if task else None
    except Exception as e:
        logger.error(f"获取任务 {task_id} 失败: {str(e)}")
        return None
    finally:
        conn.close()

def get_all_tasks():
    conn = get_db_connection()
    try:
        cursor = conn.execute('SELECT * FROM tasks ORDER BY created_at DESC')
        return [dict(row) for row in cursor.fetchall()]
    except Exception as e:
        logger.error(f"获取所有任务失败: {str(e)}")
        return []
    finally:
        conn.close()

def get_tasks_paginated(page=1, per_page=20, status=None, search=None):
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        conditions = []
        params = []
        if status:
            status_list = status.split(',')
            placeholders = ",".join(["?"] * len(status_list))
            conditions.append(f"status IN ({placeholders})")
            params.extend(status_list)
        if search and search.strip():
            # 关键词搜索：标题/URL/任务ID/上传结果（AcFun、B站投稿响应里的 BV 号、链接等）
            like = f"%{search.strip()}%"
            conditions.append(
                "(id LIKE ? OR youtube_url LIKE ? OR video_title_original LIKE ? "
                "OR video_title_translated LIKE ? OR acfun_upload_response LIKE ? "
                "OR bilibili_upload_response LIKE ?)"
            )
            params.extend([like] * 6)
        where_clause = ("WHERE " + " AND ".join(conditions)) if conditions else ""
        cursor.execute(f"SELECT COUNT(*) FROM tasks {where_clause}", params)
        total = cursor.fetchone()[0] or 0
        total_pages = math.ceil(total / per_page) if per_page > 0 else 0
        offset = (page - 1) * per_page
        # 分层排序：上传中永远置顶 → 正在启动上传 → 排队任务按队列位次从小到大 → 其余按更新时间倒序
        order_clause = f"""
            ORDER BY
                CASE
                    WHEN status = '{TASK_STATES['UPLOADING']}' THEN 0
                    WHEN status = '{TASK_STATES['READY_FOR_UPLOAD']}' AND upload_progress LIKE '{QUEUE_STARTING_MARK}%' THEN 1
                    WHEN status = '{TASK_STATES['READY_FOR_UPLOAD']}' THEN 2
                    ELSE 3
                END ASC,
                CASE
                    WHEN status = '{TASK_STATES['READY_FOR_UPLOAD']}' AND upload_progress NOT LIKE '{QUEUE_STARTING_MARK}%'
                        THEN COALESCE(queue_position, 999999)
                    ELSE 0
                END ASC,
                updated_at DESC
        """
        cursor.execute(f"SELECT * FROM tasks {where_clause} {order_clause} LIMIT ? OFFSET ?", params + [per_page, offset])
        rows = cursor.fetchall()
        tasks = [dict(row) for row in rows]
        return {'tasks': tasks, 'total': total, 'page': page, 'per_page': per_page, 'total_pages': total_pages, 'has_prev': page > 1, 'has_next': page < total_pages, 'prev_page': page - 1, 'next_page': page + 1}
    except Exception as e:
        logger.error(f"获取分页任务列表失败: {e}")
        return {'tasks': [], 'total': 0, 'page': 1, 'per_page': per_page, 'total_pages': 0, 'has_prev': False, 'has_next': False, 'prev_page': 0, 'next_page': 2}
    finally:
        if conn: conn.close()

def get_tasks_by_status(status):
    conn = get_db_connection()
    try:
        cursor = conn.execute('SELECT * FROM tasks WHERE status = ? ORDER BY created_at DESC', (status,))
        return [dict(row) for row in cursor.fetchall()]
    except Exception as e:
        logger.error(f"获取{status}状态任务失败: {str(e)}")
        return []
    finally:
        conn.close()

def delete_task(task_id, delete_files=True):
    task = get_task(task_id)
    if not task: return False
    request_task_cancel(task_id)
    if delete_files: delete_task_files(task_id)
    conn = get_db_connection()
    try:
        conn.execute('DELETE FROM tasks WHERE id = ?', (task_id,))
        conn.commit()
        publish_task_event('task_deleted', {'task_id': task_id})
        return True
    except Exception as e:
        logger.error(f"删除任务 {task_id} 失败: {str(e)}")
        return False
    finally:
        conn.close()

def clear_all_tasks(delete_files=True):
    tasks = get_all_tasks()
    for task in tasks: request_task_cancel(task['id'])
    if delete_files:
        for task in tasks: delete_task_files(task['id'])
    conn = get_db_connection()
    try:
        conn.execute('DELETE FROM tasks')
        conn.commit()
        publish_task_event('tasks_cleared', {})
        return True
    except Exception as e:
        logger.error(f"清空任务失败: {str(e)}")
        return False
    finally:
        conn.close()

def _get_task_download_dir_real(task_id):
    downloads_dir_real = os.path.realpath(DOWNLOADS_DIR)
    try:
        normalized_task_id = str(uuid.UUID(str(task_id or '').strip()))
    except (ValueError, AttributeError, TypeError) as exc:
        raise ValueError("任务ID格式无效") from exc
    safe_task_dir = safe_join(downloads_dir_real, normalized_task_id)
    if not safe_task_dir: raise ValueError("任务目录非法")
    task_dir_real = os.path.realpath(safe_task_dir)
    if os.path.commonpath([task_dir_real, downloads_dir_real]) != downloads_dir_real:
        raise ValueError("任务目录非法")
    return task_dir_real

def _is_upload_stage_failure(task, config=None):
    if not task: return False
    upload_target = _get_task_upload_target(task)
    if not _get_pending_upload_platforms(task, upload_target): return False
    video_path = task.get('video_path_local')
    if not (isinstance(video_path, str) and video_path and os.path.exists(video_path)): return False
    if _task_has_upload_response(task, upload_target): return _has_partial_upload_success(task, upload_target)
    error_text = str(task.get('error_message') or '').lower()
    upload_markers = ('上传', 'upload', 'bilibili', 'acfun', '账号未登录', 'preupload')
    if any(marker in error_text for marker in upload_markers): return True
    completed = _get_completed_stages(task, config)
    return PIPELINE_STAGE_DOWNLOAD_VIDEO in completed and PIPELINE_STAGE_TRANSLATE_SUBTITLE in completed

def _start_background_upload_retry(task_id, config):
    def run_upload_retry():
        try: force_upload_task(task_id, config)
        except Exception as exc: logger.error(f"后台上传重试任务 {task_id} 出错: {exc}")
    threading.Thread(target=run_upload_retry, daemon=True).start()

def retry_failed_tasks(config=None):
    failed_tasks = get_tasks_by_status(TASK_STATES['FAILED'])
    total = len(failed_tasks)
    if total == 0: return {'total': 0, 'scheduled': 0, 'failed_ids': []}
    if config is None:
        try:
            from modules.config_manager import load_config
            config = load_config()
        except Exception as e:
            logger.warning(f"加载配置失败，将使用默认配置重试失败任务: {e}")
            config = {}
    scheduled = 0
    failed_ids = []
    for task in failed_tasks:
        task_id = task['id']
        original_error = task.get('error_message')
        upload_target = _get_task_upload_target(task)
        clear_task_cancel(task_id)
        if _task_has_upload_response(task, upload_target):
            update_task(task_id, silent=True, status=TASK_STATES['COMPLETED'], error_message=None, upload_progress=None)
            continue
        if _is_upload_stage_failure(task, config):
            update_task(task_id, silent=True, status=TASK_STATES['READY_FOR_UPLOAD'], error_message=None, upload_progress=None)
            _start_background_upload_retry(task_id, config)
            scheduled += 1
            continue
        next_status = TASK_STATES['FAILED'] if _has_partial_upload_success(task, upload_target) else TASK_STATES['PENDING']
        update_task(task_id, silent=True, status=next_status, error_message=None, upload_progress=None)
        if start_task(task_id, config):
            scheduled += 1
        else:
            failed_ids.append(task_id)
            update_task(task_id, silent=True, status=TASK_STATES['FAILED'], error_message=original_error or '批量重试调度失败，请稍后重试。')
    return {'total': total, 'scheduled': scheduled, 'failed_ids': failed_ids}

# ==================== 每天首次上传前自动激活失败任务 ====================
_DAILY_FAILED_ACTIVATE_KEY = 'failed_tasks_activated_date'
_daily_activate_lock = threading.Lock()
# 进程内缓存：当天已检查过就不再查库（上传队列补位调用很频繁）
_daily_activate_checked_date = None


def get_app_meta(key, default=None):
    """读取 app_meta 键值（失败时返回默认值，不抛出）。"""
    try:
        conn = get_db_connection()
        try:
            row = conn.execute('SELECT value FROM app_meta WHERE key = ?', (key,)).fetchone()
        finally:
            conn.close()
        return row['value'] if row else default
    except Exception as e:
        logger.debug(f"读取 app_meta[{key}] 失败: {e}")
        return default


def set_app_meta(key, value):
    """写入 app_meta 键值。"""
    try:
        conn = get_db_connection()
        try:
            conn.execute(
                'INSERT OR REPLACE INTO app_meta (key, value) VALUES (?, ?)',
                (key, str(value)),
            )
            conn.commit()
        finally:
            conn.close()
        return True
    except Exception as e:
        logger.warning(f"写入 app_meta[{key}] 失败: {e}")
        return False


def activate_failed_tasks_for_today_once(config=None):
    """每天第一次上传开始前，自动激活失败任务（等同界面上的「重试失败任务」）。

    - 以 app_meta 记录日期，同一天只会执行一次（进程重启也不会重复触发）；
    - 当天没有失败任务时同样记账，避免反复查询；
    - 任何异常都吞掉，绝不影响正常上传流程。
    """
    global _daily_activate_checked_date
    today = datetime.now().strftime('%Y-%m-%d')
    if _daily_activate_checked_date == today:
        return None
    if get_app_meta(_DAILY_FAILED_ACTIVATE_KEY) == today:
        _daily_activate_checked_date = today
        return None
    if not _daily_activate_lock.acquire(blocking=False):
        return None
    try:
        # 拿到锁后二次确认，避免并发下重复激活
        if get_app_meta(_DAILY_FAILED_ACTIVATE_KEY) == today:
            _daily_activate_checked_date = today
            return None
        try:
            failed_count = len(get_tasks_by_status(TASK_STATES['FAILED']))
        except Exception as e:
            logger.warning(f"[每日激活] 统计失败任务失败（忽略）: {e}")
            return None
        if failed_count == 0:
            set_app_meta(_DAILY_FAILED_ACTIVATE_KEY, today)
            _daily_activate_checked_date = today
            logger.info(f"[每日激活] {today} 首次上传前无失败任务，无需激活")
            return {'total': 0, 'scheduled': 0}
        # 先记账再执行：即使激活过程异常，也不要在同一天反复重试，避免刷屏/反复拉库
        set_app_meta(_DAILY_FAILED_ACTIVATE_KEY, today)
        _daily_activate_checked_date = today
        result = retry_failed_tasks(config)
        logger.info(
            f"[每日激活] {today} 首次上传前自动激活失败任务："
            f"共 {result.get('total', 0)} 个，重新调度 {result.get('scheduled', 0)} 个"
        )
        if result.get('failed_ids'):
            logger.warning(f"[每日激活] 以下失败任务重新调度失败: {result['failed_ids']}")
        return result
    except Exception as e:
        logger.warning(f"[每日激活] 自动激活失败任务异常（忽略）: {e}")
        return None
    finally:
        _daily_activate_lock.release()


def is_metadata_translation_retryable(task):
    if not task or task.get('status') != TASK_STATES['AWAITING_REVIEW']: return False
    return task.get('error_category') == METADATA_TRANSLATION_ERROR_CATEGORY

def get_metadata_translation_retry_block_reason(task, config) -> str | None:
    config = config or {}
    can_translate_title = (_as_bool(config.get('TRANSLATE_TITLE', True)) and bool(_normalize_task_text(task.get('video_title_original'))))
    can_translate_description = (_as_bool(config.get('TRANSLATE_DESCRIPTION', True)) and bool(_normalize_task_text(task.get('description_original'))))
    if not can_translate_title and not can_translate_description: return '当前未启用可用的标题或简介自动翻译，无法重新翻译。'
    if not _normalize_task_text(config.get('OPENAI_API_KEY')): return '未配置 OpenAI API Key，无法重新翻译。'
    return None

def retry_metadata_translation_task(task_id, config=None):
    task = get_task(task_id)
    if not is_metadata_translation_retryable(task): return False
    if get_metadata_translation_retry_block_reason(task, config): return False
    if _is_task_active(task_id):
        logger.warning(f"任务 {task_id} 仍在运行，拒绝重新调度元数据翻译")
        return False
    clear_task_cancel(task_id)
    reset_stages = {PIPELINE_STAGE_TRANSLATE_CONTENT, PIPELINE_STAGE_GENERATE_TAGS, PIPELINE_STAGE_RECOMMEND_PARTITION, PIPELINE_STAGE_MODERATE_CONTENT}
    checkpoint = _parse_pipeline_checkpoint(task.get(PIPELINE_CHECKPOINT_FIELD))
    remaining_stages = [stage for stage in checkpoint.get('completed', []) if stage not in reset_stages]
    reset_checkpoint = json.dumps({'version': checkpoint.get('version', 1), 'completed': remaining_stages, 'updated_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S')}, ensure_ascii=False)
    reset_values = {
        'status': TASK_STATES['PENDING'], 'video_title_translated': '', 'description_translated': '',
        'tags_generated': None, 'recommended_partition_id': None, 'recommended_partition_id_acfun': None,
        'recommended_partition_id_bilibili': None, 'selected_partition_id': None, 'selected_partition_id_acfun': None,
        'selected_partition_id_bilibili': None, 'moderation_result': None, 'error_message': None,
        'error_category': None, 'pipeline_checkpoint': reset_checkpoint, 'upload_progress': None
    }
    rollback_values = {key: task.get(key) for key in reset_values if key != 'status'}
    rollback_values['status'] = task.get('status')
    if not update_task(task_id, silent=True, **reset_values): return False
    try:
        if start_task(task_id, config): return True
    except Exception as exc:
        logger.error(f"重新调度元数据翻译任务 {task_id} 出错: {exc}")
    if not update_task(task_id, silent=True, **rollback_values):
        logger.warning(f"重新调度元数据翻译任务 {task_id} 失败，且状态回滚失败")
    return False

def delete_task_files(task_id):
    try:
        task_dir_real = _get_task_download_dir_real(task_id)
    except (ValueError, OSError) as e:
        logger.error(f"验证任务 {task_id} 路径失败: {str(e)}")
        return False
    if os.path.exists(task_dir_real):
        try:
            shutil.rmtree(task_dir_real)
            logger.info(f"任务 {task_id} 的下载目录已删除: {task_dir_real}")
        except Exception as e:
            logger.error(f"删除任务 {task_id} 的下载目录失败: {str(e)}")
            return False
    return True

upload_queue_lock = threading.Lock()
task_semaphore = None


class UploadSlotLimiter:
    """上传并发配额（以「上传任务」为单位，1 个任务占 1 个配额）。

    相比 threading.Semaphore 的改动：
    1. 上限可原地调整，不再重建对象。重建对象会让正在等待旧对象的线程
       永远等不到唤醒，同时旧的持锁线程与新对象的持锁线程叠加，
       出现「设置是 1、实际却在上传 2 个」的假象。
    2. 每次获取配额前（最多 5 秒一次）从 config.json 重读
       MAX_CONCURRENT_UPLOADS，改完设置无需重启即生效。
    3. 同一线程可重入（批量队列先占配额、内部上传再占一次不会死锁）。
    4. 记录当前占用数，日志里能直接看到 占用/上限。
    """

    RELOAD_INTERVAL = 5.0

    def __init__(self, limit=1):
        self._limit = max(1, int(limit or 1))
        self._active = 0
        self._owners = {}          # thread_id -> 重入计数
        self._force_released = set()  # 已被强制交还配额的线程（其后续 release 不再重复扣减）
        self._cond = threading.Condition()
        self._last_reload = 0.0

    @property
    def limit(self):
        return self._limit

    @property
    def active(self):
        return self._active

    def set_limit(self, limit):
        new_limit = max(1, int(limit or 1))
        with self._cond:
            changed = new_limit != self._limit
            self._limit = new_limit
            self._cond.notify_all()
        if changed:
            logger.info(f"上传并发上限已更新为 {self._limit}")

    def _reload_limit_from_config(self):
        now = time.time()
        if now - self._last_reload < self.RELOAD_INTERVAL:
            return
        self._last_reload = now
        try:
            from modules.config_manager import load_config
            latest = _as_int(load_config().get('MAX_CONCURRENT_UPLOADS', self._limit), self._limit, minimum=1)
            if latest != self._limit:
                logger.info(f"检测到「最大并发上传数」设置为 {latest}，立即生效（原 {self._limit}）")
                self.set_limit(latest)
        except Exception as e:
            logger.debug(f"读取「最大并发上传数」失败，沿用 {self._limit}: {e}")

    def acquire(self, label='上传任务', cancel_check=None, blocking=True):
        ident = threading.get_ident()
        self._reload_limit_from_config()
        with self._cond:
            if self._owners.get(ident):
                self._owners[ident] += 1
                return True
            if self._active >= self._limit:
                if not blocking:
                    return False
                logger.info(f"[上传并发] {label} 排队等待配额（当前 {self._active}/{self._limit}）")
                while self._active >= self._limit:
                    if cancel_check is not None:
                        try:
                            if cancel_check():
                                logger.info(f"[上传并发] {label} 在排队等待配额时被取消，退出队列")
                                raise TaskCancelledError("任务已取消")
                        except TaskCancelledError:
                            raise
                        except Exception:
                            pass
                    self._cond.wait(timeout=2.0)
                    self._reload_limit_from_config_unlocked()
            self._active += 1
            self._owners[ident] = 1
            logger.info(f"[上传并发] {label} 获得配额（{self._active}/{self._limit}）")
        return True

    def _reload_limit_from_config_unlocked(self):
        """等待期间也定期重读设置，避免用户调大上限后仍被卡住。"""
        now = time.time()
        if now - self._last_reload < self.RELOAD_INTERVAL:
            return
        self._last_reload = now
        try:
            from modules.config_manager import load_config
            latest = _as_int(load_config().get('MAX_CONCURRENT_UPLOADS', self._limit), self._limit, minimum=1)
            if latest != self._limit:
                self._limit = latest
                if latest > self._active:
                    self._cond.notify_all()
                logger.info(f"上传并发上限已更新为 {latest}（等待中重读设置）")
        except Exception:
            pass

    def release(self):
        ident = threading.get_ident()
        with self._cond:
            if ident in self._force_released:
                # 该线程的配额已被强制交还（停止任务时上传线程没能及时退出），
                # 这里只清标记，不能重复扣减，否则会把别的任务的配额错误释放。
                self._force_released.discard(ident)
                return
            depth = self._owners.get(ident, 0)
            if depth > 1:
                self._owners[ident] = depth - 1
                return
            self._owners.pop(ident, None)
            if self._active > 0:
                self._active -= 1
            self._cond.notify_all()
            logger.info(f"[上传并发] 释放配额（{self._active}/{self._limit}）")

    def is_owner(self, ident=None):
        ident = threading.get_ident() if ident is None else ident
        with self._cond:
            return bool(self._owners.get(ident))

    def force_release_owner(self, ident, reason=''):
        """强制把某个线程占用的配额交还（用于已停止但上传调用无法中断的任务）。"""
        with self._cond:
            if not self._owners.get(ident):
                return False
            self._owners.pop(ident, None)
            self._force_released.add(ident)
            if self._active > 0:
                self._active -= 1
            self._cond.notify_all()
            logger.warning(f"[上传并发] 强制交还配额（{self._active}/{self._limit}）：{reason}")
            return True

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.release()
        return False


upload_slots = UploadSlotLimiter(1)
# 兼容旧引用（其它模块可能 import upload_semaphore）
upload_semaphore = upload_slots


def init_upload_semaphore(max_concurrent_uploads=1):
    """保留原函数名：现在的语义是「调整上限」，不再重建对象。"""
    logger.info(f"初始化上传并发配额，最大并发上传数: {max_concurrent_uploads}")
    upload_slots.set_limit(max_concurrent_uploads)


class _UploadSlot:
    """`with upload_slot('任务 xxxx'): ...` —— 占用 1 个上传配额（同线程可重入）。

    cancel_check: 可选回调，排队等待配额期间周期性调用；返回 True 抛出
    TaskCancelledError，避免停止任务后仍在队列里无限等待。
    """

    def __init__(self, label='上传任务', cancel_check=None):
        self.label = label
        self.cancel_check = cancel_check

    def __enter__(self):
        upload_slots.acquire(self.label, cancel_check=self.cancel_check)
        return self

    def __exit__(self, exc_type, exc, tb):
        upload_slots.release()
        return False


def upload_slot(label='上传任务', cancel_check=None):
    return _UploadSlot(label, cancel_check=cancel_check)


def get_upload_concurrency_status():
    """给页面/日志用：当前上传并发占用与上限。"""
    return {'active': upload_slots.active, 'limit': upload_slots.limit}


QUEUE_WAITING_MARK = '排队等待上传配额'
QUEUE_STARTING_MARK = '正在启动上传…'
QUEUE_RETIRED_MARK = '已退出上传队列'
# 点了「停止」后，给上传线程多少秒自行退出；超时则强制交还配额让队列继续
STOP_RELEASE_GRACE_SECONDS = 60
_QUEUE_PUMP_LOCK = threading.Lock()


_VIDEO_FILE_EXTS = frozenset({'.mp4', '.webm', '.mkv', '.mov', '.avi', '.flv', '.m4v'})
# 有下载数据任务的调度优先级：2=已下载完（有成品视频）> 1=有残留分片（续传快）> 0=无数据
def _download_data_priority(task_id):
    """检查任务下载目录：返回 2/1/0，用于「有下载数据优先」调度。"""
    if not task_id:
        return 0
    try:
        task_dir = os.path.join(DOWNLOADS_DIR, str(task_id))
        if not os.path.isdir(task_dir):
            return 0
        has_other = False
        for name in os.listdir(task_dir):
            if name.startswith('.') or name == 'metadata.json':
                continue
            ext = os.path.splitext(name)[1].lower()
            if ext in _VIDEO_FILE_EXTS:
                return 2
            if name in ('video.info.json', 'video.webp'):
                continue
            has_other = True  # .part 分片、fragment 残留等
        return 1 if has_other else 0
    except Exception:
        return 0


def _list_queued_upload_tasks(limit=200):
    """数据库里仍在「上传队列」中的任务：状态=准备上传（除「已退出队列」外）。

    排队状态必须落库，不能只存在内存线程里：进程重启或线程异常退出后，
    内存里的等待队列会全部消失，任务就永远停在「准备上传」不再继续。
    不强制要求进度带排队标记：线程异常死亡的任务可能残留 None / 0.0% 等进度，
    只要没有活跃线程在跑（由补位器过滤），就应该被接上继续传。
    """
    conn = get_db_connection()
    try:
        rows = conn.execute(
            "SELECT id, created_at FROM tasks WHERE status = ? AND (upload_progress IS NULL OR upload_progress = '' OR upload_progress NOT LIKE ?) LIMIT ?",
            (TASK_STATES['READY_FOR_UPLOAD'], QUEUE_RETIRED_MARK + '%', int(limit) * 2)
        ).fetchall()
        # 有下载数据（成品视频/残留分片）的任务优先上传，其余按创建时间
        ordered = sorted(rows, key=lambda r: (-_download_data_priority(r['id']), r['created_at'] or ''))
        return [row['id'] for row in ordered[:int(limit)]]
    except Exception as e:
        logger.debug(f"读取上传队列失败（忽略）: {e}")
        return []
    finally:
        conn.close()


def _refresh_queue_positions():
    """给所有排队中的任务刷新「第 N 位」，让界面能看出队列在动。

    跳过「正在启动上传…」的任务——那是有线程正在认领执行的，不能被改回排队。
    """
    conn = get_db_connection()
    try:
        now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        # 先清掉本状态下的旧位次，避免任务退出队列后残留
        conn.execute("UPDATE tasks SET queue_position = NULL WHERE status = ?", (TASK_STATES['READY_FOR_UPLOAD'],))
        rows = conn.execute(
            "SELECT id, created_at FROM tasks WHERE status = ? AND (upload_progress IS NULL OR upload_progress = '' OR (upload_progress NOT LIKE ? AND upload_progress != ?))",
            (TASK_STATES['READY_FOR_UPLOAD'], QUEUE_RETIRED_MARK + '%', QUEUE_STARTING_MARK)
        ).fetchall()
        # 位次与实际取任务顺序一致：有下载数据优先，其余按创建时间
        ordered = sorted(rows, key=lambda r: (-_download_data_priority(r['id']), r['created_at'] or ''))
        for idx, row in enumerate(ordered, 1):
            conn.execute(
                "UPDATE tasks SET upload_progress = ?, queue_position = ?, updated_at = ? WHERE id = ?",
                (f"{QUEUE_WAITING_MARK}（第 {idx} 位）…", idx, now_str, row['id'])
            )
        conn.commit()
    except Exception as e:
        logger.debug(f"刷新队列位置失败（忽略）: {e}")
    finally:
        conn.close()


def pump_upload_queue(reason='触发'):
    """把上传队列里「没有线程在跑」的任务补起来（有空闲配额时才补）。

    调用时机：上传结束释放配额后、任务线程收尾后、每 30 秒扫描、进程启动后。
    这样第一个任务完成或终止后，后面的任务会自动接着传，不需要再点一次。
    """
    if not _QUEUE_PUMP_LOCK.acquire(blocking=False):
        return 0
    try:
        queued = _list_queued_upload_tasks()
        if not queued:
            return 0
        active_ids = _get_active_task_ids()
        idle = [tid for tid in queued if tid not in active_ids]
        if not idle:
            return 0
        # 无论是否补位都刷新排队位次：队列满员时也要让界面看到「第 N 位」
        try:
            _refresh_queue_positions()
        except Exception:
            pass
        slots_free = upload_slots.limit - upload_slots.active
        if slots_free <= 0:
            logger.debug(f"[上传队列] {reason}：{len(idle)} 个任务排队中，配额已满（{upload_slots.active}/{upload_slots.limit}），等前一个结束后自动继续")
            return 0
        # 每天第一次上传前：自动激活失败任务（当天只做一次，失败不影响上传）
        try:
            activated = activate_failed_tasks_for_today_once()
            if activated and activated.get('scheduled'):
                # 激活可能把任务改成 ready_for_upload，重新取一次队列
                queued = _list_queued_upload_tasks()
                active_ids = _get_active_task_ids()
                idle = [tid for tid in queued if tid not in active_ids]
                if not idle:
                    return 0
        except Exception as e:
            logger.debug(f"[每日激活] 触发异常（忽略）: {e}")
        started = 0
        for tid in idle[:slots_free]:
            if is_task_cancelled(tid):
                continue
            if not _claim_queued_task(tid):
                # 已被别的实例/线程认领（数据库原子更新，只有一个能抢到）
                continue
            if not _mark_task_active(tid):
                continue
            threading.Thread(target=_run_queued_upload_job, args=(tid,), name=f"upload_queue_{tid[:8]}", daemon=True).start()
            started += 1
            logger.info(f"[上传队列] {reason}：启动排队任务 {tid[:8]}，共补位 {started} 个（空闲配额 {slots_free}）")
        if started:
            try:
                _refresh_queue_positions()
            except Exception:
                pass
        return started
    except Exception as e:
        logger.warning(f"[上传队列] 补位失败（忽略）: {e}")
        return 0
    finally:
        _QUEUE_PUMP_LOCK.release()


def poll_scheduled_uploads():
    """把到点的「定时上传」任务转入上传队列。

    由后台 30 秒扫描（_check_and_start_next_pending_task）周期调用：
    找出 scheduled_upload 状态且计划时间已到的任务，置为 ready_for_upload
    并触发上传队列补位，到点即传，无需人工干预。
    """
    try:
        conn = get_db_connection()
        try:
            now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            rows = conn.execute(
                "SELECT id FROM tasks WHERE status = ? AND scheduled_upload_at IS NOT NULL AND scheduled_upload_at != '' AND scheduled_upload_at <= ?",
                (TASK_STATES['SCHEDULED_UPLOAD'], now_str)
            ).fetchall()
        finally:
            conn.close()
        due_ids = [row['id'] for row in rows]
        if not due_ids:
            return 0
        for tid in due_ids:
            update_task(tid, silent=True,
                        status=TASK_STATES['READY_FOR_UPLOAD'],
                        upload_progress=QUEUE_WAITING_MARK)
        logger.info(f"[定时上传] {len(due_ids)} 个任务到达计划时间，已转入上传队列: {[t[:8] for t in due_ids]}")
        pump_upload_queue(reason='定时上传到点')
        return len(due_ids)
    except Exception as e:
        logger.warning(f"[定时上传] 到点扫描失败（忽略）: {e}")
        return 0


def _claim_queued_task(task_id):
    """数据库原子认领排队任务：把排队标记改成「正在启动上传…」。

    多条线程/多个进程同时补位时，只有一条 UPDATE 能命中排队标记，
    其余影响行数为 0，从根上避免同一个任务被并发上传两次。
    """
    conn = get_db_connection()
    try:
        now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        cur = conn.execute(
            "UPDATE tasks SET upload_progress = ?, updated_at = ? WHERE id = ? AND status = ? AND upload_progress LIKE ?",
            (QUEUE_STARTING_MARK, now_str, task_id, TASK_STATES['READY_FOR_UPLOAD'], QUEUE_WAITING_MARK + '%')
        )
        conn.commit()
        return cur.rowcount == 1
    except Exception as e:
        logger.debug(f"认领排队任务失败: {e}")
        return False
    finally:
        conn.close()


def _watch_stopped_upload(task_id, owner_ident):
    """点「停止」后，B站/AcFun 的上传调用无法中途打断（SDK 内部阻塞）。

    如果一直等它自己超时，配额会被一个已经被停止的任务白占好几分钟，
    后面的排队任务全部都动不了。这里在宽限期后强制交还配额，让队列继续。
    """
    cancel_since = None
    while True:
        time.sleep(1.0)
        try:
            if not upload_slots.is_owner(owner_ident):
                return
            if is_task_cancelled(task_id):
                if cancel_since is None:
                    cancel_since = time.time()
                elif time.time() - cancel_since >= STOP_RELEASE_GRACE_SECONDS:
                    if upload_slots.force_release_owner(owner_ident, f"任务 {str(task_id)[:8]} 已停止 {int(STOP_RELEASE_GRACE_SECONDS)} 秒但上传调用未退出"):
                        try:
                            pump_upload_queue('停止任务强制交还配额')
                        except Exception:
                            pass
                    return
            else:
                cancel_since = None
        except Exception:
            return


def _run_queued_upload_job(task_id):
    """队列补位线程：只跑上传链，不重跑整条处理流程。"""
    task_logger = setup_task_logger(task_id)
    try:
        task_logger.info("上传队列：已轮到本任务，开始上传")
        processor = get_global_task_processor()
        processor._upload_to_target(task_id, task_logger, allow_missing_translations=True)
    except TaskCancelledError:
        task_logger.info("上传队列：任务已取消，退出队列")
        update_task(task_id, status=TASK_STATES['FAILED'], error_message="任务已取消")
    except Exception as e:
        task_logger.error(f"上传队列执行失败: {e}")
        update_task(task_id, status=TASK_STATES['FAILED'], error_message=f"上传失败: {e}")
    finally:
        _mark_task_inactive(task_id)
        try:
            latest = get_task(task_id)
            progress = str(latest.get('upload_progress') or '') if latest else ''
            # 本次没真正开始上传（前置条件不满足 / 被中断）：打上「已退出队列」标记，
            # 补位器会跳过它，避免每隔 30 秒无限重试同一个任务。
            if latest and latest.get('status') == TASK_STATES['READY_FOR_UPLOAD'] and (progress.startswith(QUEUE_WAITING_MARK) or progress == QUEUE_STARTING_MARK):
                update_task(task_id, upload_progress=QUEUE_RETIRED_MARK, silent=True)
                task_logger.info("上传队列：本次未能开始上传，已退出队列（避免反复重试）")
        except Exception:
            pass
        try:
            pump_upload_queue('队列任务结束')
        except Exception:
            pass


def init_task_semaphore(max_concurrent_tasks=3):
    global task_semaphore
    logger.info(f"初始化任务信号量，最大并发任务数: {max_concurrent_tasks}")
    task_semaphore = threading.Semaphore(max_concurrent_tasks)
    logger.info(f"任务信号量初始化完成: {task_semaphore}")

def reset_stuck_tasks(skip_active=False, cancel_active=False):
    conn = get_db_connection()
    try:
        cursor = conn.execute('''SELECT id, status, updated_at FROM tasks WHERE status IN ({}) AND datetime(updated_at) < datetime('now', '-30 minutes')'''.format(','.join(['?'] * len(PROCESSING_STATES))), PROCESSING_STATES)
        stuck_tasks = cursor.fetchall()
        if stuck_tasks:
            logger.warning(f"发现 {len(stuck_tasks)} 个可能卡住的任务，正在重置...")
            reset_count = 0
            for task in stuck_tasks:
                task_id = task[0]
                old_status = task[1]
                if skip_active and _is_task_active(task_id):
                    if cancel_active: request_task_cancel(task_id)
                    logger.warning(f"任务 {task_id[:8]}... 仍处于活动线程中，已跳过自动重置")
                    continue
                conn.execute('''UPDATE tasks SET status = ?, error_message = ?, updated_at = ?, subtitle_qc_failed = 0, subtitle_qc_reason = NULL, subtitle_qc_score = NULL, subtitle_qc_checked_at = NULL, subtitle_quality_state = NULL, asr_warning_message = NULL, subtitle_warning_message = NULL WHERE id = ?''', (TASK_STATES['FAILED'], f"任务超时重置 (原状态: {old_status})", datetime.now().strftime('%Y-%m-%d %H:%M:%S'), task_id))
                reset_count += 1
                logger.info(f"重置任务 {task_id[:8]}... 从 {old_status} 到 failed")
            conn.commit()
            return reset_count
        else:
            logger.info("没有发现卡住的任务")
            return 0
    except Exception as e:
        logger.error(f"重置卡住任务时出错: {str(e)}")
        return 0
    finally:
        conn.close()

def validate_cookies(cookies_path, service_name="Unknown"):
    if not cookies_path or not os.path.exists(cookies_path):
        logger.warning(f"{service_name} Cookies文件不存在: {cookies_path}")
        return False, f"Cookies文件不存在: {cookies_path}"
    try:
        with open(cookies_path, 'r', encoding='utf-8') as f: content = f.read().strip()
        if not content:
            logger.warning(f"{service_name} Cookies文件为空")
            return False, "Cookies文件为空"
        if content.startswith('# Netscape HTTP Cookie File') or '\t' in content:
            lines = [line for line in content.split('\n') if line.strip() and (not line.startswith('#') or line.startswith('#HttpOnly_'))]
            if not lines: return False, "Netscape格式cookies文件没有有效的cookie条目"
            logger.debug(f"{service_name} Netscape格式cookies, {len(lines)} 个条目")
        elif content.startswith('[') or content.startswith('{'):
            import json
            cookies_data = json.loads(content)
            if not cookies_data: return False, "JSON格式cookies文件为空数组"
            logger.debug(f"{service_name} JSON格式cookies, {len(cookies_data)} 个条目")
        else:
            if service_name == "YouTube":
                try:
                    from modules.tools.convert_cookies import convert_to_netscape_lines
                    if convert_to_netscape_lines(content) is not None:
                        logger.debug(f"{service_name} 文本表格格式cookies可自动转换为Netscape")
                        return True, "Cookies文件格式正确"
                except Exception: pass
            logger.warning(f"{service_name} Cookies文件格式不明")
            return False, "Cookies文件格式不明"
        return True, "Cookies文件格式正确"
    except Exception:
        logger.exception("验证%s Cookies文件时出错", service_name)
        return False, "验证cookies文件出错，请查看服务日志。"

class TaskProcessor:
    def __init__(self, config=None):
        self.config = dict(config or {})
        self._current_max_concurrent_tasks = _as_int(self.config.get('MAX_CONCURRENT_TASKS', 2), 2, minimum=1)
        self._current_max_concurrent_uploads = _as_int(self.config.get('MAX_CONCURRENT_UPLOADS', 1), 1, minimum=1)
        self._runtime_limit_refresh_pending = False
        self._last_deferred_limit_signature = None
        init_upload_semaphore(self._current_max_concurrent_uploads)
        init_task_semaphore(self._current_max_concurrent_tasks)
        self.scheduler = BackgroundScheduler(executors={'default': APSchedulerThreadPoolExecutor(max_workers=max(2, self._current_max_concurrent_tasks))}, job_defaults={'coalesce': False, 'max_instances': 1})
        self.scheduler.start()
        logger.info(f"任务处理器初始化完成 - 最大并发任务: {self._current_max_concurrent_tasks}, 最大并发上传: {self._current_max_concurrent_uploads}")
        global _RESUME_RECOVERY_DONE
        if not _RESUME_RECOVERY_DONE:
            try: recover_interrupted_tasks_to_pending()
            finally: _RESUME_RECOVERY_DONE = True
        self._register_periodic_jobs()

    def _register_periodic_jobs(self):
        try:
            scan_interval = _as_int(self.config.get('PENDING_SCAN_INTERVAL_SECONDS', 30), 30, minimum=5)
            self.scheduler.add_job(self._check_and_start_next_pending_task, 'interval', seconds=scan_interval, id='pending_scanner', replace_existing=True)
            logger.info(f"已启动定时扫描pending任务：每 {scan_interval} 秒")
        except Exception as e: logger.warning(f"注册定时扫描pending任务失败（不影响主流程）：{e}")
        try:
            stuck_check_interval = _as_int(self.config.get('STUCK_TASK_CHECK_INTERVAL_SECONDS', 300), 300, minimum=30)
            self.scheduler.add_job(self._recover_stuck_tasks, 'interval', seconds=stuck_check_interval, id='stuck_task_recovery', replace_existing=True)
            logger.info(f"已启动卡住任务扫描：每 {stuck_check_interval} 秒")
        except Exception as e: logger.warning(f"注册卡住任务扫描失败（不影响主流程）：{e}")

    def _refresh_runtime_limits(self, force=False):
        desired_tasks = _as_int(self.config.get('MAX_CONCURRENT_TASKS', 2), 2, minimum=1)
        desired_uploads = _as_int(self.config.get('MAX_CONCURRENT_UPLOADS', 1), 1, minimum=1)
        if desired_tasks == self._current_max_concurrent_tasks and desired_uploads == self._current_max_concurrent_uploads and not self._runtime_limit_refresh_pending: return True
        active_task_count = len(_get_active_task_ids())
        if active_task_count > 0 and not force:
            signature = (desired_tasks, desired_uploads)
            if signature != self._last_deferred_limit_signature:
                logger.info(f"检测到并发配置变更，但当前仍有活动任务运行；新的任务/上传并发上限将延后到空闲时生效: {signature}")
                self._last_deferred_limit_signature = signature
            self._runtime_limit_refresh_pending = True
            return False
        with _TASK_SCHEDULING_LOCK:
            init_task_semaphore(desired_tasks)
            init_upload_semaphore(desired_uploads)
            self._current_max_concurrent_tasks = desired_tasks
            self._current_max_concurrent_uploads = desired_uploads
            self._runtime_limit_refresh_pending = False
            self._last_deferred_limit_signature = None
        logger.info(f"并发配置已生效 - 最大并发任务: {desired_tasks}, 最大并发上传: {desired_uploads}")
        return True

    def refresh_config(self, config=None):
        self.config = dict(config or {})
        self._register_periodic_jobs()
        self._refresh_runtime_limits(force=False)

    def _enqueue_task_retry(self, task_id, delay_seconds=2):
        try:
            self.scheduler.add_job(self.schedule_task, 'date', run_date=datetime.now() + timedelta(seconds=max(1, int(delay_seconds))), id=f'queued_retry_{task_id}', replace_existing=True, args=[task_id])
        except Exception as e: logger.debug(f"为任务 {task_id} 注册延迟重试失败，将依赖定时扫描器: {e}")

    def _recover_stuck_tasks(self):
        try:
            reset_count = reset_stuck_tasks(skip_active=True, cancel_active=True)
            if reset_count > 0:
                logger.warning(f"自动恢复了 {reset_count} 个卡住任务，准备继续调度 pending 队列")
                self._check_and_start_next_pending_task()
        except Exception as e: logger.warning(f"自动恢复卡住任务失败（忽略）：{e}")

    def shutdown(self):
        try:
            if self.scheduler: self.scheduler.shutdown(wait=False)
        except SchedulerNotRunningError: pass
        except Exception as e: logger.warning(f"关闭任务处理器时发生异常: {e}")
        finally: logger.info("任务处理器已关闭")

    def schedule_task(self, task_id):
        try:
            with _TASK_SCHEDULING_LOCK:
                self._refresh_runtime_limits(force=False)
                if _is_task_active(task_id):
                    logger.warning(f"任务 {task_id} 已有活动线程，跳过重复调度")
                    return f"thread_{task_id}"
                queued_job_id = f"queued_retry_{task_id}"
                try: existing_retry_job = self.scheduler.get_job(queued_job_id)
                except Exception: existing_retry_job = None
                task = get_task(task_id)
                if not task:
                    logger.error(f"任务 {task_id} 不存在")
                    return None
                if task['status'] not in [TASK_STATES['PENDING'], TASK_STATES['FAILED']]:
                    logger.warning(f"任务 {task_id} 状态为 {task['status']}，不能调度")
                    return None
                global task_semaphore
                if task_semaphore is None: init_task_semaphore(self._current_max_concurrent_tasks)
                assert task_semaphore is not None, "task_semaphore 应该已经初始化"
                local_task_semaphore = task_semaphore
                slot_acquired = local_task_semaphore.acquire(blocking=False)
                if not slot_acquired:
                    if existing_retry_job is None: self._enqueue_task_retry(task_id, delay_seconds=2)
                    logger.info(f"任务 {task_id} 当前达到并发上限，保留原状态等待后续调度，不再创建等待线程")
                    return queued_job_id
                import threading
                release_slot_on_failure = True
                task_marked_active = False
                def run_task_wrapper():
                    try:
                        logger.info(f"任务 {task_id} 开始在线程中执行")
                        self.process_task(task_id, slot_already_acquired=True, acquired_task_semaphore=local_task_semaphore)
                    except Exception as e:
                        logger.error(f"任务 {task_id} 执行出错: {str(e)}")
                        import traceback
                        logger.error(traceback.format_exc())
                        update_task(task_id, status=TASK_STATES['FAILED'], error_message=f"执行出错: {str(e)}")
                    finally:
                        _mark_task_inactive(task_id)
                try:
                    if not _mark_task_active(task_id):
                        logger.warning(f"任务 {task_id} 在线程启动前检测到重复调度，已跳过")
                        return f"thread_{task_id}"
                    task_marked_active = True
                    thread = threading.Thread(target=run_task_wrapper, name=f"task_{task_id}", daemon=True)
                    thread.start()
                    release_slot_on_failure = False
                except Exception: raise
                finally:
                    if release_slot_on_failure:
                        if task_marked_active: _mark_task_inactive(task_id)
                        local_task_semaphore.release()
                logger.info(f"任务 {task_id} 已在后台线程启动")
                return f"thread_{task_id}"
        except Exception as e:
            logger.error(f"调度任务 {task_id} 失败: {str(e)}")
            import traceback
            logger.error(traceback.format_exc())
            update_task(task_id, status=TASK_STATES['FAILED'], error_message=f"调度失败: {str(e)}")
            return None

    def process_task(self, task_id, slot_already_acquired=False, acquired_task_semaphore=None):
        task_logger = setup_task_logger(task_id)
        task_logger.info(f"开始处理任务 {task_id}")
        slot_acquired = bool(slot_already_acquired)
        active_task_semaphore = acquired_task_semaphore
        if slot_already_acquired and active_task_semaphore is None:
            task_logger.warning("slot_already_acquired=True 但未提供信号量实例，改为重新获取并发配额")
            slot_acquired = False
        if active_task_semaphore is None:
            global task_semaphore
            with _TASK_SCHEDULING_LOCK:
                active_task_semaphore = task_semaphore
                if active_task_semaphore is None:
                    task_logger.warning("task_semaphore 为 None，正在重新初始化...")
                    init_task_semaphore(self._current_max_concurrent_tasks)
                    active_task_semaphore = task_semaphore
            if active_task_semaphore is None:
                task_logger.error("task_semaphore 初始化失败，无法继续执行任务")
                return
        try:
            task = get_task(task_id)
            if not task: return
            if not slot_acquired:
                task_logger.info("等待获取任务并发配额...")
                try:
                    while True:
                        if is_task_cancelled(task_id): raise TaskCancelledError("任务已取消")
                        if active_task_semaphore.acquire(timeout=0.5):
                            slot_acquired = True
                            break
                    task_logger.info("获得任务并发配额，开始执行任务")
                except TaskCancelledError:
                    task_logger.info("任务在等待并发配额时已取消")
                    update_task(task_id, status=TASK_STATES['FAILED'], error_message="任务已取消")
                    return
                except Exception as _e:
                    task_logger.error(f"获取任务并发配额失败: {_e}")
                    return
            else:
                task_logger.info("任务已在调度阶段获得任务并发配额")
            _raise_if_cancelled(task_id, task_logger)
            completed_stages = _get_completed_stages(task, getattr(self, 'config', None))
            try: _persist_pipeline_checkpoint(task_id, completed_stages)
            except Exception: pass
            task = get_task(task_id) or task
            if task is None: return
            upload_target = _get_task_upload_target(task)
            pending_platforms = _get_pending_upload_platforms(task, upload_target)
            if task.get('status') == TASK_STATES['FAILED'] and not pending_platforms and _task_has_upload_response(task, upload_target):
                task_logger.info("检测到目标平台已全部上传成功，跳过重复流程并标记完成")
                completed_stages = _mark_stage_done(task_id, completed_stages, PIPELINE_STAGE_UPLOAD_TO_ACFUN)
                update_task(task_id, status=TASK_STATES['COMPLETED'], error_message=None, upload_progress=None)
                return
            if task.get('status') == TASK_STATES['FAILED'] and _has_partial_upload_success(task, upload_target):
                done_platforms = [p for p in _get_upload_platforms_for_target(upload_target) if _task_has_platform_upload_response(task, p)]
                task_logger.info(f"检测到部分平台已上传成功，进入失败点续传模式。已完成平台: {done_platforms}，待重试平台: {pending_platforms}")
                self._upload_to_target(task_id, task_logger)
                task = get_task(task_id)
                if task and _task_has_upload_response(task, upload_target):
                    completed_stages = _mark_stage_done(task_id, completed_stages, PIPELINE_STAGE_UPLOAD_TO_ACFUN)
                    if task.get('status') != TASK_STATES['COMPLETED']:
                        update_task(task_id, status=TASK_STATES['COMPLETED'], error_message=None, upload_progress=None)
                return
            task_logger.info(f"开始处理任务，当前task对象: {task}")
            if task is None: return
            if PIPELINE_STAGE_FETCH_INFO in completed_stages:
                task_logger.info("跳过采集视频信息（checkpoint已完成）")
            else:
                self._fetch_video_info(task_id, task['youtube_url'], task_logger)
                _raise_if_cancelled(task_id, task_logger)
                task = get_task(task_id)
                if task is None or task['status'] == TASK_STATES['FAILED']: return
                completed_stages = _mark_stage_done(task_id, completed_stages, PIPELINE_STAGE_FETCH_INFO)
            if self.config.get('TRANSLATE_TITLE', True) or self.config.get('TRANSLATE_DESCRIPTION', True):
                if PIPELINE_STAGE_TRANSLATE_CONTENT in completed_stages:
                    task_logger.info("跳过标题/描述翻译（checkpoint已完成）")
                else:
                    ok = self._translate_content(task_id, task_logger)
                    task = get_task(task_id)
                    if ok: completed_stages = _mark_stage_done(task_id, completed_stages, PIPELINE_STAGE_TRANSLATE_CONTENT)
                    elif task is not None and task['status'] == TASK_STATES['AWAITING_REVIEW']: return
                    elif task is not None and task['status'] == TASK_STATES['FAILED']: return
                _raise_if_cancelled(task_id, task_logger)
            if self.config.get('GENERATE_TAGS', True) or is_preset_tags_effective(self.config):
                if PIPELINE_STAGE_GENERATE_TAGS in completed_stages:
                    task_logger.info("跳过标签生成（checkpoint已完成）")
                else:
                    ok = self._generate_tags(task_id, task_logger)
                    if ok: completed_stages = _mark_stage_done(task_id, completed_stages, PIPELINE_STAGE_GENERATE_TAGS)
                _raise_if_cancelled(task_id, task_logger)
            if self.config.get('RECOMMEND_PARTITION', False):
                if PIPELINE_STAGE_RECOMMEND_PARTITION in completed_stages:
                    task_logger.info("跳过分区推荐（checkpoint已完成）")
                else:
                    ok = self._recommend_partition(task_id, task_logger)
                    if ok: completed_stages = _mark_stage_done(task_id, completed_stages, PIPELINE_STAGE_RECOMMEND_PARTITION)
                task = get_task(task_id)
                _raise_if_cancelled(task_id, task_logger)
            if self.config.get('CONTENT_MODERATION_ENABLED', False):
                if PIPELINE_STAGE_MODERATE_CONTENT in completed_stages:
                    task_logger.info("跳过内容审核（checkpoint已完成）")
                else:
                    self._moderate_content(task_id, task_logger)
                    task = get_task(task_id)
                    if task is not None and task['status'] == TASK_STATES['AWAITING_REVIEW']:
                        completed_stages = _mark_stage_done(task_id, completed_stages, PIPELINE_STAGE_MODERATE_CONTENT)
                        return
                    completed_stages = _mark_stage_done(task_id, completed_stages, PIPELINE_STAGE_MODERATE_CONTENT)
                _raise_if_cancelled(task_id, task_logger)
            task = get_task(task_id)
            if task is None: return
            if PIPELINE_STAGE_DOWNLOAD_VIDEO in completed_stages:
                task_logger.info("跳过下载视频文件（checkpoint已完成）")
            else:
                self._download_video_file(task_id, task['youtube_url'], task_logger)
                _raise_if_cancelled(task_id, task_logger)
                task = get_task(task_id)
                if task is not None and task['status'] == TASK_STATES['FAILED']: return
                completed_stages = _mark_stage_done(task_id, completed_stages, PIPELINE_STAGE_DOWNLOAD_VIDEO)
            subtitle_translation_enabled = _as_bool(self.config.get('SUBTITLE_TRANSLATION_ENABLED', False))
            subtitle_embed_enabled = _as_bool(self.config.get('SUBTITLE_EMBED_IN_VIDEO', True))
            if subtitle_translation_enabled or subtitle_embed_enabled:
                if PIPELINE_STAGE_TRANSLATE_SUBTITLE in completed_stages:
                    task_logger.info("跳过字幕处理（checkpoint已完成）")
                else:
                    ok = self._translate_subtitle(task_id, task_logger)
                    task = get_task(task_id)
                    if ok: completed_stages = _mark_stage_done(task_id, completed_stages, PIPELINE_STAGE_TRANSLATE_SUBTITLE)
                _raise_if_cancelled(task_id, task_logger)
            if self.config.get('AUTO_MODE_ENABLED', False):
                task = get_task(task_id)
                upload_target = _get_task_upload_target(task)
                if task and _task_has_upload_response(task, upload_target):
                    task_logger.info("检测到已有上传响应，跳过上传（避免重复上传）")
                    completed_stages = _mark_stage_done(task_id, completed_stages, PIPELINE_STAGE_UPLOAD_TO_ACFUN)
                elif PIPELINE_STAGE_UPLOAD_TO_ACFUN in completed_stages:
                    task_logger.info("跳过上传（checkpoint已完成）")
                else:
                    self._upload_to_target(task_id, task_logger)
                    _raise_if_cancelled(task_id, task_logger)
                    task = get_task(task_id)
                    upload_target = _get_task_upload_target(task)
                    if task and task.get('status') == TASK_STATES['AWAITING_REVIEW']: return
                    if task and (_task_has_upload_response(task, upload_target) or task.get('status') == TASK_STATES['COMPLETED']):
                        completed_stages = _mark_stage_done(task_id, completed_stages, PIPELINE_STAGE_UPLOAD_TO_ACFUN)
            task = get_task(task_id)
            if task is not None:
                upload_target = _get_task_upload_target(task)
                if task['status'] == TASK_STATES['AWAITING_REVIEW']:
                    task_logger.info("任务当前处于人工审核状态，保留待审核状态")
                elif task['status'] != TASK_STATES['COMPLETED'] and task['status'] != TASK_STATES['FAILED']:
                    if not self.config.get('AUTO_MODE_ENABLED', False) or not _task_has_upload_response(task, upload_target):
                        update_task(task_id, status=TASK_STATES['READY_FOR_UPLOAD'])
                        task_logger.info("任务处理完成，标记为准备上传")
                    else:
                        update_task(task_id, status=TASK_STATES['COMPLETED'])
                        task_logger.info("任务处理并上传完成")
            else:
                update_task(task_id, status=TASK_STATES['READY_FOR_UPLOAD'])
        except TaskCancelledError:
            task_logger.info("任务已取消，停止后续处理")
            update_task(task_id, status=TASK_STATES['FAILED'], error_message="任务已取消")
        except Exception as e:
            task_logger.error(f"任务处理过程中发生错误: {str(e)}")
            import traceback
            task_logger.error(traceback.format_exc())
            update_task(task_id, status=TASK_STATES['FAILED'], error_message=str(e))
        finally:
            clear_task_cancel(task_id, clear_flag=True)
            if slot_acquired:
                try: active_task_semaphore.release()
                except Exception: pass
            try: gc.collect()
            except Exception: pass
            import threading, time
            def delayed_check():
                time.sleep(1)
                self._check_and_start_next_pending_task()
            threading.Thread(target=delayed_check, daemon=True).start()

    def _check_and_start_next_pending_task(self):
        # 上传队列补位：进程启动恢复后、以及每 30 秒扫描时，把排队的上传任务接上
        try:
            pump_upload_queue('定时扫描')
        except Exception:
            pass
        # 定时上传：把到达计划时间的任务转入上传队列
        try:
            poll_scheduled_uploads()
        except Exception:
            pass
        try:
            from modules.config_manager import load_config
            latest_config = load_config()
            self.max_concurrent_tasks = int(latest_config.get('MAX_CONCURRENT_TASKS', self.max_concurrent_tasks))
        except Exception as e:
            logger.debug(f"读取最新配置失败，沿用当前配置: {e}")
        
        self._refresh_runtime_limits(force=False)
        pending_tasks = get_tasks_by_status(TASK_STATES['PENDING'])
        active_task_ids = _get_active_task_ids()
        pending_tasks = [task for task in pending_tasks if task.get('id') not in active_task_ids and not is_task_cancelled(task.get('id'))]
        if not pending_tasks:
            if active_task_ids: logger.info(f"当前没有可启动的pending任务（{len(active_task_ids)} 个任务线程处于活动状态）")
            else: logger.info("没有pending任务需要启动")
            return
        processing_states = ['fetching_info', 'info_fetched', TASK_STATES['TRANSLATING'], TASK_STATES['TAGGING'], TASK_STATES['PARTITIONING'], TASK_STATES['MODERATING'], TASK_STATES['DOWNLOADING'], TASK_STATES['DOWNLOADED'], TASK_STATES['ASR_TRANSCRIBING'], TASK_STATES['TRANSLATING_SUBTITLE'], TASK_STATES['ENCODING_VIDEO'], TASK_STATES['UPLOADING']]
        running_tasks = []
        for state in processing_states: running_tasks.extend(get_tasks_by_status(state))
        running_task_ids = {task.get('id') for task in running_tasks if task.get('id')}
        effective_running_count = len(running_task_ids.union(active_task_ids))
        max_concurrent = _as_int(self.config.get('MAX_CONCURRENT_TASKS', 2), 2, minimum=1)
        if _should_reduce_concurrency():
            max_concurrent = max(1, max_concurrent // 2)
            logger.info(f"检测到高内存使用，降低并发数至 {max_concurrent}")
        if effective_running_count >= max_concurrent:
            logger.info(f"当前有效运行任务数 {effective_running_count}（DB运行中 {len(running_task_ids)}，活动线程 {len(active_task_ids)}），达到并发限制 {max_concurrent}，暂不启动新任务")
            return
        # 有下载数据的任务优先（已下载完 > 有残留分片 > 无数据），同优先级按创建时间
        pending_tasks.sort(key=lambda x: (-_download_data_priority(x.get('id')), x['created_at']))
        next_task = pending_tasks[0]
        logger.info(f"发现pending任务，准备启动: {next_task['id'][:8]}... ({next_task.get('youtube_url', 'Unknown URL')[-30:]})")
        job_id = self.schedule_task(next_task['id'])
        if job_id: logger.info(f"下一个pending任务已自动启动: {next_task['id'][:8]}...")
        else: logger.error(f"启动下一个pending任务失败: {next_task['id'][:8]}...")

    def _fetch_video_info(self, task_id, youtube_url, task_logger):
        from modules.youtube_handler import download_video_data
        task_logger.info(f"采集视频信息: {youtube_url}")
        update_task(task_id, status='fetching_info')
        _raise_if_cancelled(task_id, task_logger)
        cookies_path = resolve_youtube_cookies_path(self.config, task_logger)
        if cookies_path:
            is_valid, error_msg = validate_cookies(cookies_path, "YouTube")
            if not is_valid:
                task_logger.error(f"YouTube Cookies验证失败: {error_msg}")
                cookies_path = None
        try:
            cancel_event = get_task_cancel_event(task_id)
            success, result = download_video_data(youtube_url, task_id, cookies_path, skip_download=True, cancel_event=cancel_event)
        except Exception as e:
            if is_task_cancelled(task_id): raise TaskCancelledError("任务已取消")
            task_logger.error(f"采集视频信息时发生异常: {str(e)}")
            update_task(task_id, status=TASK_STATES['FAILED'], error_message=f"采集信息异常: {str(e)}")
            return
        if success:
            task_logger.info("视频信息采集成功")
            metadata_path = result.get('metadata_path')
            video_title = ""
            video_description = ""
            if metadata_path and os.path.exists(metadata_path):
                try:
                    with open(metadata_path, 'r', encoding='utf-8') as f:
                        metadata = json.load(f)
                        video_title = metadata.get('title', '')
                        video_description = metadata.get('description', '')
                except Exception as e: task_logger.error(f"读取视频元数据失败: {str(e)}")
            update_task(task_id, status='info_fetched', video_title_original=video_title, description_original=video_description, cover_path_local=result.get('cover_path', ''), metadata_json_path_local=metadata_path)
        else:
            task_logger.error(f"视频信息采集失败: {result}")
            update_task(task_id, status=TASK_STATES['FAILED'], error_message=f"采集信息失败: {result}")

    def _clean_partial_downloads(self, task_id, task_logger=None):
        """清理任务目录里残留的半成品下载文件（*.part / *.ytdl / *.temp）。

        中断过的下载会留下碎片和 .ytdl 状态文件，续传时若碎片已被破坏（例如多个进程同时
        写同一个任务目录），yt-dlp 会报 "Unable to download video: [Errno 2] No such file
        或 directory: ...part-FragNNN"。这种情况下重新下载前必须先清掉这些残留。
        只删碎片，保留 video.mp4 / video.webp / metadata.json 等成品文件。
        """
        removed = 0
        task_dir = os.path.join(DOWNLOADS_DIR, task_id)
        if not os.path.isdir(task_dir):
            return 0
        suffixes = ('.part', '.ytdl', '.temp')
        try:
            with os.scandir(task_dir) as entries:
                for entry in entries:
                    try:
                        if not entry.is_file():
                            continue
                        name = entry.name.lower()
                        if any(s in name for s in suffixes):
                            os.remove(entry.path)
                            removed += 1
                    except OSError:
                        continue
        except OSError:
            return removed
        if removed and task_logger:
            task_logger.info(f"已清理 {removed} 个未完成的下载碎片文件，准备重新下载")
        return removed

    def _download_video_file(self, task_id, youtube_url, task_logger):
        """下载视频本体。返回 True 表示视频已就绪；False 表示失败（状态与原因已写入任务）。"""
        from modules.youtube_handler import download_video_data
        task_logger.info(f"审核通过，开始下载视频文件: {youtube_url}")
        update_task(task_id, status=TASK_STATES['DOWNLOADING'])
        _raise_if_cancelled(task_id, task_logger)
        cookies_path = resolve_youtube_cookies_path(self.config, task_logger)
        if cookies_path:
            is_valid, error_msg = validate_cookies(cookies_path, "YouTube")
            if not is_valid:
                task_logger.error(f"YouTube Cookies验证失败: {error_msg}")
                cookies_path = None
        def progress_callback(progress_info):
            percent = progress_info.get('percent', 0)
            progress_msg = f"{percent:.1f}%"
            task_logger.debug(f"下载进度: {progress_msg}")
            update_task(task_id, upload_progress=progress_msg, silent=True)

        last_error = ''
        for attempt in (1, 2):
            try:
                cancel_event = get_task_cancel_event(task_id)
                success, result = download_video_data(youtube_url, task_id, cookies_path, only_video=True, progress_callback=progress_callback, cancel_event=cancel_event)
            except Exception as e:
                if is_task_cancelled(task_id): raise TaskCancelledError("任务已取消")
                task_logger.error(f"下载视频文件时发生异常: {str(e)}")
                success, result = False, str(e)
            if is_task_cancelled(task_id): raise TaskCancelledError("任务已取消")
            if success:
                task_logger.info("视频文件下载成功")
                task = get_task(task_id)
                update_data = {'status': TASK_STATES['DOWNLOADED'], 'video_path_local': result.get('video_path', ''), 'upload_progress': None}
                if task is not None:
                    if result.get('metadata_path') and not task.get('metadata_json_path_local'): update_data['metadata_json_path_local'] = result.get('metadata_path')
                    if result.get('cover_path') and not task.get('cover_path_local'): update_data['cover_path_local'] = result.get('cover_path')
                update_task(task_id, **update_data)
                return True
            last_error = str(result)
            task_logger.error(f"视频文件下载失败（第 {attempt} 次）: {last_error}")
            if attempt == 1:
                # 续传失败多半是碎片损坏，清掉碎片再整段重下一次
                if self._clean_partial_downloads(task_id, task_logger) > 0:
                    update_task(task_id, status=TASK_STATES['DOWNLOADING'], upload_progress='0.0%')
                    continue
            break
        update_task(task_id, status=TASK_STATES['FAILED'], error_message=f"下载视频失败: {last_error}")
        return False

    def _recover_cover_path(self, task_id, cover_path, task_logger):
        downloads_dir_real = os.path.realpath(DOWNLOADS_DIR)
        def _is_within_downloads(path):
            path_real = os.path.realpath(path)
            try: return os.path.commonpath([downloads_dir_real, path_real]) == downloads_dir_real, path_real
            except ValueError: return False, path_real
        if cover_path:
            cover_ok, cover_real = _is_within_downloads(cover_path)
            if cover_ok and os.path.isfile(cover_real): return cover_real
        task_dir_ok, task_dir = _is_within_downloads(os.path.join(DOWNLOADS_DIR, task_id))
        if not task_dir_ok: task_dir = ''
        if task_dir and os.path.isdir(task_dir):
            cover_candidates = ['cover.jpg', 'cover.png', 'cover.webp', 'thumbnail.jpg', 'thumbnail.png', 'thumbnail.webp']
            for name in cover_candidates:
                candidate_ok, candidate = _is_within_downloads(os.path.join(task_dir, name))
                if candidate_ok and os.path.isfile(candidate):
                    update_task(task_id, cover_path_local=candidate, silent=True)
                    return candidate
            try:
                with os.scandir(task_dir) as entries:
                    for entry in entries:
                        entry_ok, entry_path = _is_within_downloads(entry.path)
                        if entry_ok and entry.is_file() and entry.name.lower().endswith(('.jpg', '.jpeg', '.png', '.webp')) and os.path.isfile(entry_path):
                            update_task(task_id, cover_path_local=entry_path, silent=True)
                            return entry_path
            except OSError: pass
        return ''

    def _ensure_upload_cover(self, task_id, task, task_logger):
        """上传前确保封面可用：本地恢复失败时，自动从源视频重新抓取封面。"""
        cover_path = task.get('cover_path_local', '') if task else ''
        cover_path = self._recover_cover_path(task_id, cover_path, task_logger)
        if cover_path:
            return cover_path
        youtube_url = task.get('youtube_url', '') if task else ''
        if not youtube_url:
            return ''
        task_logger.info(f"本地封面缺失，尝试重新抓取封面: {youtube_url}")
        try:
            from modules.youtube_handler import download_video_data
            cookies_path = resolve_youtube_cookies_path(self.config, task_logger)
            if cookies_path:
                is_valid, _ = validate_cookies(cookies_path, "YouTube")
                if not is_valid:
                    cookies_path = None
            # 注意：必须同时传 only_video=True，否则 download_video_data 会清空任务目录、连已下载的视频一起删除
            success, result = download_video_data(youtube_url, task_id, cookies_path, skip_download=True, only_video=True, cancel_event=get_task_cancel_event(task_id))
            if is_task_cancelled(task_id): raise TaskCancelledError("任务已取消")
            if success and isinstance(result, dict) and result.get('cover_path') and os.path.isfile(result.get('cover_path')):
                cover_path = result.get('cover_path')
                # 安全校验：确认视频文件没有被误删
                video_local = task.get('video_path_local', '') if task else ''
                if video_local and not os.path.exists(video_local):
                    task_logger.error(f"封面重抓后视频文件丢失: {video_local}")
                    return ''
                update_task(task_id, cover_path_local=cover_path, silent=True)
                task_logger.info(f"封面重新抓取成功: {cover_path}")
                return cover_path
            task_logger.warning(f"封面重新抓取失败: {result}")
        except TaskCancelledError:
            raise
        except Exception as e:
            task_logger.warning(f"封面重新抓取异常: {str(e)}")
        return ''

    def _translate_content(self, task_id, task_logger):
        from modules.ai_enhancer import translate_video_metadata
        task = get_task(task_id)
        if not task: return
        update_task(task_id, status=TASK_STATES['TRANSLATING'])
        upload_target = _get_task_upload_target(task)
        effective_limits = _get_effective_metadata_limits(upload_target)
        openai_config = {'OPENAI_API_KEY': self.config.get('OPENAI_API_KEY', ''), 'OPENAI_BASE_URL': self.config.get('OPENAI_BASE_URL', ''), 'OPENAI_MODEL_NAME': self.config.get('OPENAI_MODEL_NAME', 'gpt-3.5-turbo'), 'OPENAI_THINKING_ENABLED': self.config.get('OPENAI_THINKING_ENABLED', False), 'FALLBACK_OPENAI_API_KEY': self.config.get('FALLBACK_OPENAI_API_KEY', ''), 'FALLBACK_OPENAI_BASE_URL': self.config.get('FALLBACK_OPENAI_BASE_URL', ''), 'FALLBACK_OPENAI_MODEL_NAME': self.config.get('FALLBACK_OPENAI_MODEL_NAME', ''), 'FIXED_PARTITION_ID': self.config.get('FIXED_PARTITION_ID', '')}
        for prompt_key in ('METADATA_TRANSLATE_MODE', 'METADATA_TRANSLATE_TEXT', 'METADATA_DESC_RETRY_MODE', 'METADATA_DESC_RETRY_TEXT'):
            if prompt_key in self.config: openai_config[prompt_key] = self.config[prompt_key]
        translate_title = bool(self.config.get('TRANSLATE_TITLE', True) and task.get('video_title_original'))
        translate_description = bool(self.config.get('TRANSLATE_DESCRIPTION', True) and task.get('description_original'))
        if not translate_title and not translate_description: return True
        translated = translate_video_metadata(task.get('video_title_original', ''), task.get('description_original', ''), target_language="zh-CN", openai_config=openai_config, task_id=task_id, translate_title=translate_title, translate_description=translate_description, title_limit=effective_limits['title_limit'], description_limit=effective_limits['description_limit'])
        updates = {}
        requested_fields = set(translated.get('requested_fields') or [])
        if translate_title and 'title' in requested_fields: updates['video_title_translated'] = translated.get('title', '')
        if translate_description and 'description' in requested_fields: updates['description_translated'] = translated.get('description', '')
        if translated.get('success'):
            if updates:
                updates['error_message'] = None
                updates['error_category'] = None
                update_task(task_id, **updates)
            return True
        review_message = translated.get('error_message') or _build_missing_translation_review_message(requested_fields)
        updates.update({'status': TASK_STATES['AWAITING_REVIEW'], 'error_message': review_message, 'error_category': METADATA_TRANSLATION_ERROR_CATEGORY})
        update_task(task_id, **updates)
        return False

    def _ensure_required_translations_ready(self, task_id, task, task_logger, allow_missing_translations=False):
        missing_fields = _get_missing_required_translation_fields(task, self.config)
        if not missing_fields: return True
        missing_message = _build_missing_translation_review_message(missing_fields)
        if allow_missing_translations: return True
        update_task(task_id, status=TASK_STATES['AWAITING_REVIEW'], error_message=missing_message, error_category=METADATA_TRANSLATION_ERROR_CATEGORY, upload_progress=None)
        return False

    def _translate_subtitle(self, task_id, task_logger, embed_in_video_override=None):
        task = get_task(task_id)
        if not task: return False
        translation_enabled = _as_bool(self.config.get('SUBTITLE_TRANSLATION_ENABLED', False))
        config_embed_enabled = _as_bool(self.config.get('SUBTITLE_EMBED_IN_VIDEO', True))
        should_embed_subtitle = bool(embed_in_video_override) if embed_in_video_override is not None else config_embed_enabled
        embed_quality_state = str(task.get('subtitle_quality_state') or '').strip().lower() or 'ok'
        qc_failed = task.get('subtitle_qc_failed') == 1
        qc_reason = task.get('subtitle_qc_reason')
        qc_cleared = False
        escape_hatch_active = False
        block_reason = None
        def _embed_guard(subtitle_file, *, asr_artifact=True):
            nonlocal block_reason
            state = embed_quality_state if asr_artifact else 'ok'
            allowed = _subtitle_embed_allowed(self.config, state, qc_failed, qc_cleared=qc_cleared, qc_reason=qc_reason, task_logger=task_logger)
            if not allowed and block_reason is None:
                reasons = _subtitle_block_reasons(self.config, state, qc_failed, qc_cleared=qc_cleared, qc_reason=qc_reason)
                if reasons: block_reason = reasons[0]
            return allowed
        def _ensure_asr_qc(subtitle_file):
            nonlocal qc_cleared, qc_failed, block_reason
            if qc_cleared: return True
            allowed, cleared, reason = self._ensure_asr_subtitle_qc(task_id, subtitle_file, task_logger, embed_quality_state)
            if cleared:
                qc_cleared = True
                qc_failed = False
            if not allowed and block_reason is None: block_reason = reason or 'subtitle_qc_rejected'
            return allowed
        def _quarantine_if_asr_artifact(subtitle_file):
            if _is_asr_subtitle_artifact(task_id, subtitle_file): self._quarantine_rejected_subtitle(subtitle_file, task_logger)
        def _block_warning(fallback='asr_failed_block_embed'):
            return _block_reason_to_warning(block_reason) if block_reason else fallback
        update_task(task_id, status=TASK_STATES['TRANSLATING_SUBTITLE'])
        try:
            task_dir = os.path.join(DOWNLOADS_DIR, task_id)
            subtitle_files = []
            try:
                for name in os.listdir(task_dir):
                    if not isinstance(name, str): continue
                    lower = name.lower()
                    if lower.endswith('.srt'): subtitle_files.append(os.path.join(task_dir, name))
                    elif lower.endswith('.vtt'):
                        vtt_path = os.path.join(task_dir, name)
                        srt_path = self._convert_vtt_to_srt(vtt_path, task_logger)
                        if srt_path and os.path.exists(srt_path): subtitle_files.append(srt_path)
                        else: subtitle_files.append(vtt_path)
            except Exception: pass
            if not subtitle_files:
                if _is_asr_enabled(self.config):
                    out_path = None
                    try:
                        from modules.speech_recognition import create_speech_recognizer_from_config
                        recognizer = create_speech_recognizer_from_config(self.config, task_id)
                    except Exception as e:
                        task_logger.error(f"创建语音识别器失败: {e}")
                        recognizer = None
                    if recognizer and task is not None:
                        video_path = task.get('video_path_local')
                        if video_path and os.path.exists(video_path):
                            prev_status = TASK_STATES['TRANSLATING_SUBTITLE']
                            try:
                                _t = get_task(task_id)
                                prev_status = _t['status'] if _t else prev_status
                                update_task(task_id, status=TASK_STATES['ASR_TRANSCRIBING'])
                                asr_subtitle_path = os.path.join(task_dir, f"asr_{task_id}.srt")
                                out_path = None
                                update_task(task_id, asr_warning_message=None, subtitle_qc_failed=0, subtitle_qc_reason=None, subtitle_qc_score=None, subtitle_qc_checked_at=None, subtitle_warning_message=None, subtitle_quality_state=None)
                                qc_failed = False
                                qc_cleared = False
                                embed_quality_state = 'ok'
                                out_path = recognizer.transcribe_video_to_subtitles(video_path, asr_subtitle_path)
                                update_task(task_id, status=prev_status)
                            except Exception as e:
                                task_logger.error(f"语音识别过程中发生错误: {e}")
                                update_task(task_id, status=prev_status if 'prev_status' in locals() else TASK_STATES['TRANSLATING_SUBTITLE'])
                                out_path = None
                        if out_path and os.path.exists(out_path):
                            subtitle_files = [out_path]
                            asr_generated = True
                            embed_quality_state = str(getattr(recognizer, 'last_quality_state', '') or 'ok').strip().lower() or 'ok'
                            update_task(task_id, asr_warning_message=(getattr(recognizer, 'last_warning_message', '') or None), subtitle_quality_state=embed_quality_state)
                        else:
                            update_task(task_id, asr_warning_message=(getattr(recognizer, 'last_warning_message', '') or None), subtitle_quality_state=(str(getattr(recognizer, 'last_quality_state', '') or 'failed').strip().lower() or 'failed'))
                            if getattr(recognizer, 'last_error_message', ''):
                                _t = get_task(task_id)
                                prev_error = _t.get('error_message') if _t else None
                                merged_error = f"{prev_error}\n{recognizer.last_error_message}" if prev_error else recognizer.last_error_message
                                update_task(task_id, error_message=merged_error)
                            self._mark_subtitle_issue(task_id, 'asr_no_subtitle')
                            return True
                    else: return True
                else: return True
            asr_generated = len(subtitle_files) == 1 and _is_asr_subtitle_artifact(task_id, subtitle_files[0])
            if asr_generated:
                asr_subtitle_path = subtitle_files[0]
                if embed_quality_state == 'failed':
                    block_embed = _as_bool(self.config.get('ASR_FAILURE_BLOCKS_EMBED', True))
                    if block_embed:
                        self._quarantine_rejected_subtitle(asr_subtitle_path, task_logger)
                        update_task(task_id, subtitle_warning_message='asr_failed_block_embed', upload_progress=None)
                        return True
                    escape_hatch_active = True
                if asr_subtitle_path and os.path.exists(asr_subtitle_path):
                    strict_qc = embed_quality_state == 'degraded'
                    qc_result = self._run_subtitle_qc(task_id, asr_subtitle_path, task_logger, strict=strict_qc)
                    if qc_result is SUBTITLE_QC_DISABLED:
                        qc_cleared = True
                        qc_failed = False
                    else:
                        if not _qc_result_allows_embed(qc_result, self.config):
                            reason_text = 'qc_unavailable' if qc_result is None else 'subtitle_qc_rejected'
                            self._quarantine_rejected_subtitle(asr_subtitle_path, task_logger)
                            update_task(task_id, subtitle_warning_message=reason_text, upload_progress=None)
                            return True
                        qc_cleared = True
                        qc_failed = False
            detected_list = []
            for f in subtitle_files:
                lang = self._detect_subtitle_language(f)
                detected_list.append((f, lang))
            zh_candidates = [f for f, lang in detected_list if str(lang).lower().startswith('zh')]
            en_candidates = [f for f, lang in detected_list if str(lang).lower().startswith('en')]
            if zh_candidates:
                subtitle_file = zh_candidates[0]
                subtitle_lang = 'zh'
                if should_embed_subtitle and _embed_guard(subtitle_file, asr_artifact=asr_generated) and _ensure_asr_qc(subtitle_file):
                    embedded_video_path = self._embed_subtitle_in_video(task_id, task['video_path_local'], subtitle_file, task_logger)
                    if embedded_video_path:
                        update_task(task_id, video_path_local=embedded_video_path, subtitle_path_original=subtitle_file, subtitle_path_translated=None, subtitle_language_detected=subtitle_lang, subtitle_warning_message=None)
                        return True
                    else:
                        update_task(task_id, subtitle_path_original=subtitle_file, subtitle_path_translated=None, subtitle_language_detected=subtitle_lang, subtitle_warning_message='subtitle_embed_failed')
                        return False
                elif should_embed_subtitle:
                    _quarantine_if_asr_artifact(subtitle_file)
                    update_task(task_id, subtitle_path_original=subtitle_file, subtitle_path_translated=None, subtitle_language_detected=subtitle_lang, subtitle_warning_message=_block_warning())
                    return True
                else:
                    update_task(task_id, subtitle_path_original=subtitle_file, subtitle_path_translated=None, subtitle_language_detected=subtitle_lang)
                    return True
            if not translation_enabled:
                subtitle_file = zh_candidates[0] if zh_candidates else (en_candidates[0] if en_candidates else subtitle_files[0])
                subtitle_lang = self._detect_subtitle_language(subtitle_file)
                if should_embed_subtitle and _embed_guard(subtitle_file, asr_artifact=asr_generated) and _ensure_asr_qc(subtitle_file):
                    embedded_video_path = self._embed_subtitle_in_video(task_id, task['video_path_local'], subtitle_file, task_logger)
                    if embedded_video_path:
                        update_task(task_id, video_path_local=embedded_video_path, subtitle_path_original=subtitle_file, subtitle_path_translated=None, subtitle_language_detected=subtitle_lang, subtitle_warning_message=None)
                        return True
                    update_task(task_id, subtitle_path_original=subtitle_file, subtitle_path_translated=None, subtitle_language_detected=subtitle_lang, subtitle_warning_message='subtitle_embed_failed')
                    return False
                elif should_embed_subtitle:
                    _quarantine_if_asr_artifact(subtitle_file)
                    update_task(task_id, subtitle_path_original=subtitle_file, subtitle_path_translated=None, subtitle_language_detected=subtitle_lang, subtitle_warning_message=_block_warning())
                    return True
                update_task(task_id, subtitle_path_original=subtitle_file, subtitle_path_translated=None, subtitle_language_detected=subtitle_lang)
                return True
            subtitle_file = en_candidates[0] if en_candidates else subtitle_files[0]
            subtitle_lang = self._detect_subtitle_language(subtitle_file)
            from modules.subtitle_translator import create_translator_from_config
            translator = create_translator_from_config(self.config, task_id)
            if not translator:
                update_task(task_id, subtitle_path_original=subtitle_file, subtitle_path_translated=None, subtitle_language_detected=subtitle_lang, upload_progress=None, silent=True)
                return False
            translated_subtitle_path = os.path.join(task_dir, f"translated_{task_id}.srt")
            def progress_callback(progress, current, total):
                if is_task_cancelled(task_id): raise TaskCancelledError("任务已取消")
                update_task(task_id, upload_progress=f"{progress:.1f}%", silent=True)
            cancel_event = get_task_cancel_event(task_id)
            success = translator.translate_file(subtitle_file, translated_subtitle_path, progress_callback=progress_callback, cancel_event=cancel_event)
            if success:
                update_task(task_id, upload_progress=None, silent=True)
                if should_embed_subtitle and _embed_guard(translated_subtitle_path, asr_artifact=asr_generated) and _ensure_asr_qc(subtitle_file):
                    embedded_video_path = self._embed_subtitle_in_video(task_id, task['video_path_local'], translated_subtitle_path, task_logger)
                    if embedded_video_path:
                        update_task(task_id, video_path_local=embedded_video_path, subtitle_path_original=subtitle_file, subtitle_path_translated=translated_subtitle_path, subtitle_language_detected=subtitle_lang, subtitle_warning_message=None)
                    else:
                        update_task(task_id, subtitle_path_original=subtitle_file, subtitle_path_translated=translated_subtitle_path, subtitle_language_detected=subtitle_lang, subtitle_warning_message='subtitle_embed_failed')
                elif should_embed_subtitle:
                    _quarantine_if_asr_artifact(subtitle_file)
                    update_task(task_id, subtitle_path_original=subtitle_file, subtitle_path_translated=translated_subtitle_path, subtitle_language_detected=subtitle_lang, subtitle_warning_message=_block_warning())
                else:
                    update_task(task_id, subtitle_path_original=subtitle_file, subtitle_path_translated=translated_subtitle_path, subtitle_language_detected=subtitle_lang)
                return True
            else:
                update_task(task_id, subtitle_path_original=subtitle_file, subtitle_path_translated=None, subtitle_language_detected=subtitle_lang, upload_progress=None, silent=True)
                return False
        except TaskCancelledError:
            update_task(task_id, upload_progress=None, silent=True)
            raise
        except Exception as e:
            task_logger.error(f"字幕翻译过程中发生错误: {str(e)}")
            update_task(task_id, subtitle_path_translated=None, upload_progress=None, silent=True)
            return False

    def _ensure_asr_subtitle_qc(self, task_id, subtitle_file, task_logger, quality_state, *, force_asr=None):
        is_asr_artifact = _is_asr_subtitle_artifact(task_id, subtitle_file)
        if force_asr is not None: is_asr_artifact = bool(force_asr)
        if not is_asr_artifact: return True, False, ''
        qc_result = self._run_subtitle_qc(task_id, subtitle_file, task_logger, strict=(str(quality_state or '').strip().lower() == 'degraded'))
        if qc_result is True: return True, True, ''
        if qc_result is SUBTITLE_QC_DISABLED: return True, False, ''
        if _qc_result_allows_embed(qc_result, self.config): return True, False, ''
        reason_text = 'qc_unavailable' if qc_result is None else 'subtitle_qc_rejected'
        self._quarantine_rejected_subtitle(subtitle_file, task_logger, force=bool(force_asr))
        update_task(task_id, subtitle_warning_message=reason_text, upload_progress=None)
        return False, False, reason_text

    def _quarantine_rejected_subtitle(self, subtitle_path, task_logger=None, *, force=False) -> str:
        if not force and not _is_asr_subtitle_artifact('', subtitle_path): return ''
        try:
            source = str(subtitle_path or '').strip()
            if not source or not os.path.exists(source): return ''
            target = f"{os.path.splitext(source)[0]}.rejected.txt"
            index = 1
            while os.path.exists(target):
                target = f"{os.path.splitext(source)[0]}.rejected.{index}.txt"
                index += 1
            os.replace(source, target)
            return target
        except Exception: return ''

    def _mark_subtitle_qc_unavailable(self, task_id, task_logger=None, detail='', blocks=None):
        if blocks is None: blocks = _as_bool(self.config.get('ASR_FAILURE_BLOCKS_EMBED', True))
        try:
            fields = {'subtitle_qc_reason': 'qc_unavailable', 'subtitle_qc_checked_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'), 'upload_progress': None}
            if blocks: fields['subtitle_qc_failed'] = 1
            else:
                current = get_task(task_id) or {}
                prior_failed = current.get('subtitle_qc_failed') == 1
                fields['subtitle_qc_failed'] = 1 if prior_failed else 0
                if prior_failed: fields.pop('subtitle_qc_reason', None)
            update_task(task_id, **fields)
            return True
        except Exception: return False

    def _persist_qc_unavailable(self, task_id, task_logger=None, detail=''):
        persisted = self._mark_subtitle_qc_unavailable(task_id, task_logger, detail=detail)
        return persisted

    def _run_subtitle_qc(self, task_id: str, srt_path: str, task_logger, strict: bool = False):
        enabled_raw = self.config.get('SUBTITLE_QC_ENABLED', True)
        if not _as_bool(enabled_raw): return SUBTITLE_QC_DISABLED
        if not srt_path or not os.path.exists(srt_path):
            self._persist_qc_unavailable(task_id, task_logger, detail='missing_file')
            return None
        try:
            from modules.subtitle_qc import run_subtitle_qc
            result = run_subtitle_qc(srt_path, self.config, total_duration_s=self._resolve_qc_total_duration(task_id, task_logger), strict=bool(strict))
            update_task(task_id, subtitle_qc_failed=0 if result.passed else 1, subtitle_qc_reason=result.reason, subtitle_qc_score=float(result.score), subtitle_qc_checked_at=datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
            return True if result.passed else False
        except Exception as e:
            self._persist_qc_unavailable(task_id, task_logger, detail=f'exception: {e}')
            return None

    def _resolve_qc_total_duration(self, task_id: str, task_logger=None):
        try:
            task = get_task(task_id)
            video_path = str(task.get('video_path_local') or '') if task else ''
            if not video_path or not os.path.exists(video_path): return None
            duration = self._get_video_duration(video_path, task_logger or logger)
            duration_value = float(duration or 0.0)
            return duration_value if duration_value > 0 else None
        except Exception: return None

    def _mark_subtitle_issue(self, task_id: str, reason: str, score=None):
        update_task(task_id, subtitle_qc_failed=1, subtitle_qc_reason=reason, subtitle_qc_score=score, subtitle_qc_checked_at=datetime.now().strftime('%Y-%m-%d %H:%M:%S'))

    def _detect_subtitle_language(self, subtitle_path):
        try:
            filename = os.path.basename(subtitle_path)
            name_without_ext = os.path.splitext(filename)[0]
            parts = name_without_ext.split('.')
            if len(parts) >= 2:
                lang_code = parts[-1].lower()
                if '-' in lang_code: lang_code = lang_code.split('-')[0]
                return lang_code
            return "auto"
        except Exception: return "auto"

    _KNOWN_HW_ENCODER_ERROR_PATTERNS = (
        'Unknown encoder "libx265"', "Unknown encoder 'libx265'", 'Unknown encoder "h264_nvenc"', 'Unknown encoder "hevc_nvenc"',
        "Unknown encoder 'h264_nvenc'", "Unknown encoder 'hevc_nvenc'", 'No NVENC capable devices found', 'Device not present',
        'No such filter: "subtitles"', "No such filter: 'subtitles'", 'Cannot load libnvidia-encode', 'Failed to query maximum',
        'Cannot find a CUDA capable', 'CUDA_ERROR_', 'nvcuda.dll', 'libnvcuvid', 'nvenc_load_functions', 'Could not initialize',
        'OpenEncodeSession failed', 'out of memory', 'InitializeEncoder failed', 'GPU is not in the list', 'Unknown encoder "h264_qsv"',
        'Unknown encoder "hevc_qsv"', "Unknown encoder 'h264_qsv'", "Unknown encoder 'hevc_qsv'", 'MFXInit', 'Error initializing an internal MFX session',
        'Error creating a MFX session', 'Error initializing the MFX video session', 'Unable to find', 'No device', 'Device creation failed',
        'iHD_drv_video.so', 'i965_drv_video.so', 'Unknown encoder "h264_amf"', 'Unknown encoder "hevc_amf"', "Unknown encoder 'h264_amf'",
        "Unknown encoder 'hevc_amf'", 'AMF initialization failed', 'AMFContext', 'AMF failed', 'amfrt64.dll', 'Unknown encoder "h264_vaapi"',
        'Unknown encoder "hevc_vaapi"', "Unknown encoder 'h264_vaapi'", "Unknown encoder 'hevc_vaapi'", 'Error creating a VAAPI device',
        'Failed to create VAAPI device', 'Failed to initialise VAAPI connection', 'No VA display found for device', 'vaInitialize',
        'vaDeriveImage', '/dev/dri/renderD128', 'libva', 'hwupload', 'A hardware device reference is required to upload frames to.',
        'Failed to configure output pad on Parsed_hwupload', 'Driver does not support the required nvenc API version',
        'The minimum required Nvidia driver for nvenc is', 'libamfrt64.so', "failed to load library 'libX11.so.6'",
        'cannot open shared object file', 'Assertion in generated code', 'Unrecognized option', 'Option not found', 'Error setting option',
        'Error applying encoder options', 'Error parsing options', 'Invalid option',
    )
    _HW_OPTION_ERROR_PATTERNS = ('Unrecognized option', 'Option not found', 'Error setting option', 'Error applying encoder options', 'Error parsing options', 'Invalid option')
    _HW_PROBE_DEFAULT_SIZE = '640x480'
    _HW_PROBE_RETRY_SIZE = '1280x720'
    _HW_PROBE_DURATION = '0.1'
    _HW_PROBE_DIMENSION_ERROR_PATTERNS = ('frame dimension less than the minimum supported value', 'invalid param (8)')
    _ASS_PLAY_RES_X = 1920
    _ASS_PLAY_RES_Y = 1080
    _FORCE_STYLE_APPEARANCE_KEYS = ('PrimaryColour', 'OutlineColour', 'BackColour', 'BorderStyle', 'Bold')
    _ASS_STYLE_BASE = {
        'FontSize': 56.0, 'Outline': 2.0, 'Shadow': 1.0, 'MarginV': 40.0, 'MarginL': 96.0, 'MarginR': 96.0, 'Alignment': 2,
        'BorderStyle': 1, 'Bold': 1, 'PrimaryColour': '&H00FFFFFF', 'SecondaryColour': '&H00FFFFFF', 'OutlineColour': '&HB2000000', 'BackColour': '&H00000000',
    }
    _ASS_LANDSCAPE_FONT_SIZE_ANCHORS = ((720.0, 48.0), (1080.0, 54.0), (1440.0, 68.0), (2160.0, 102.0))
    _ASS_PORTRAIT_FONT_SIZE_ANCHORS = ((720.0, 44.0), (1280.0, 54.0), (1920.0, 68.0), (2560.0, 76.0))
    _ASS_LANDSCAPE_MARGIN_V_ANCHORS = ((720.0, 56.0), (1080.0, 62.0), (1440.0, 82.0), (2160.0, 124.0))
    _ASS_PORTRAIT_MARGIN_V_ANCHORS = ((720.0, 120.0), (1280.0, 156.0), (1920.0, 220.0), (2560.0, 292.0))
    _ASS_LANDSCAPE_SIDE_MARGIN_RATIO = 0.025
    _ASS_LANDSCAPE_SIDE_MARGIN_MIN = 32.0
    _ASS_LANDSCAPE_SIDE_MARGIN_MAX = 80.0
    _ASS_PORTRAIT_SIDE_MARGIN_RATIO = 0.095
    _ASS_PORTRAIT_SIDE_MARGIN_MIN = 82.0
    _ASS_PORTRAIT_SIDE_MARGIN_MAX = 156.0
    _ASS_LANDSCAPE_LAYOUT_DENSITY = 0.93
    _ASS_LANDSCAPE_SINGLE_LINE_DENSITY = 1.04
    _ASS_LANDSCAPE_SINGLE_LINE_LIMIT_MIN = 28.0
    _ASS_LANDSCAPE_SINGLE_LINE_LIMIT_MAX = 38.0
    _ASS_PORTRAIT_LAYOUT_DENSITY = 0.92
    _ASS_SAFE_WIDTH_RATIO = 0.98
    _ASS_OVERRIDE_FONT_SIZE_RATIO_MIN = 0.60
    _ASS_SINGLE_LINE_FONT_SCALE_MIN = 0.60
    _ASS_OVERRIDE_FONT_SIZE_MIN = 32.0
    _ASS_HARD_WRAP_MIN_LINE_LENGTH = 8
    _ASS_PORTRAIT_RESCUE_LINE_LENGTH_MAX = 16.0
    _ASS_OUTLINE_RATIO = 0.075
    _ASS_OUTLINE_MIN = 2.2
    _ASS_OUTLINE_MAX = 5.5
    _ASS_SHADOW_RATIO = 0.025
    _ASS_SHADOW_MIN = 1.0
    _ASS_SHADOW_MAX = 2.2
    _ASS_OVERRIDE_OUTLINE_RATIO = 0.045
    _ASS_OVERRIDE_OUTLINE_MIN = 1.5
    _ASS_OVERRIDE_OUTLINE_MAX = 3.0
    _STREAMING_SRT_TEMPLATE_HEIGHTS = (720, 1080, 1440, 2160)
    _STREAMING_SRT_STYLE_TEMPLATES = {
        720: {'FontSize': 18.0, 'Outline': 0.4, 'Shadow': 0.6, 'MarginL': 42, 'MarginR': 42, 'MarginV': 16},
        1080: {'FontSize': 18.0, 'Outline': 0.4, 'Shadow': 0.6, 'MarginL': 42, 'MarginR': 42, 'MarginV': 16},
        1440: {'FontSize': 18.0, 'Outline': 0.4, 'Shadow': 0.6, 'MarginL': 42, 'MarginR': 42, 'MarginV': 16},
        2160: {'FontSize': 18.0, 'Outline': 0.4, 'Shadow': 0.6, 'MarginL': 42, 'MarginR': 42, 'MarginV': 16},
    }
    _STREAMING_SRT_PORTRAIT_FONT_SIZE_ANCHORS = ((1280.0, 12.5), (1920.0, 12.5), (2560.0, 13.5))
    _STREAMING_SRT_PORTRAIT_MARGIN_V_ANCHORS = ((1280.0, 18.0), (1920.0, 28.0), (2560.0, 40.0))
    _STREAMING_SRT_PORTRAIT_SIDE_MARGIN_RATIO = 0.08
    _STREAMING_SRT_LANDSCAPE_LAYOUT_DENSITY = 0.72
    _STREAMING_SRT_PORTRAIT_LAYOUT_DENSITY = 0.96
    _BUNDLED_FONT_EXTENSIONS = ('.otf', '.ttf', '.ttc', '.otc')

    @staticmethod
    def _short_error_text(error_output, limit=240):
        text = str(error_output or '').replace('\n', ' ').strip()
        return text[:limit] if text else ''

    @staticmethod
    def _summarize_cmd(cmd, limit=300):
        summary = ' '.join(str(x) for x in (cmd or []))
        return summary[:limit] if len(summary) > limit else summary

    @classmethod
    def _is_probe_dimension_error(cls, error_output):
        if not error_output: return False
        text = str(error_output).lower()
        return any(pattern in text for pattern in cls._HW_PROBE_DIMENSION_ERROR_PATTERNS)

    @classmethod
    def _should_keep_nvidia_preference_on_probe_failure(cls, nvenc_listed, nvidia_device_visible, detect_error):
        return bool(nvenc_listed and nvidia_device_visible and cls._is_probe_dimension_error(detect_error))

    @classmethod
    def _build_hw_probe_cmd(cls, ffmpeg_bin, encoder_name, probe_size=None):
        size = probe_size or cls._HW_PROBE_DEFAULT_SIZE
        color_src = f"color=c=black:s={size}:d={cls._HW_PROBE_DURATION}"
        encoder_name_lower = str(encoder_name or '').lower().strip()
        test_cmd = [ffmpeg_bin, '-hide_banner', '-loglevel', 'error']
        if encoder_name_lower == 'h264_vaapi':
            test_cmd.extend(['-vaapi_device', '/dev/dri/renderD128', '-f', 'lavfi', '-i', color_src, '-vf', 'format=nv12,hwupload', '-frames:v', '1', '-c:v', 'h264_vaapi', '-qp', '23'])
        elif encoder_name_lower == 'hevc_vaapi':
            test_cmd.extend(['-vaapi_device', '/dev/dri/renderD128', '-f', 'lavfi', '-i', color_src, '-vf', 'format=nv12,hwupload', '-frames:v', '1', '-c:v', 'hevc_vaapi', '-qp', '23', '-profile:v', 'main'])
        elif encoder_name_lower == 'h264_qsv':
            test_cmd.extend(['-f', 'lavfi', '-i', color_src, '-frames:v', '1', '-c:v', 'h264_qsv', '-global_quality', '23', '-look_ahead', '0', '-pix_fmt', 'nv12'])
        elif encoder_name_lower == 'hevc_qsv':
            test_cmd.extend(['-f', 'lavfi', '-i', color_src, '-frames:v', '1', '-c:v', 'hevc_qsv', '-global_quality', '23', '-look_ahead', '0', '-profile:v', 'main', '-pix_fmt', 'nv12'])
        elif encoder_name_lower == 'h264_amf':
            test_cmd.extend(['-f', 'lavfi', '-i', color_src, '-frames:v', '1', '-c:v', encoder_name, '-usage', 'transcoding', '-quality', 'balanced', '-rc', 'cqp', '-qp_i', '23', '-qp_p', '23', '-qp_b', '23'])
        elif encoder_name_lower == 'hevc_amf':
            test_cmd.extend(['-f', 'lavfi', '-i', color_src, '-frames:v', '1', '-c:v', encoder_name, '-usage', 'transcoding', '-quality', 'balanced', '-rc', 'qvbr', '-qvbr_quality_level', '24', '-profile:v', 'main'])
        elif encoder_name_lower == 'hevc_nvenc':
            test_cmd.extend(['-f', 'lavfi', '-i', color_src, '-frames:v', '1', '-c:v', encoder_name, '-preset', 'p4', '-rc:v', 'vbr', '-cq:v', '23', '-profile:v', 'main'])
        elif 'nvenc' in encoder_name_lower:
            test_cmd.extend(['-f', 'lavfi', '-i', color_src, '-frames:v', '1', '-c:v', encoder_name, '-preset', 'p4', '-rc:v', 'vbr', '-cq:v', '23'])
        else:
            test_cmd.extend(['-f', 'lavfi', '-i', color_src, '-frames:v', '1', '-c:v', encoder_name])
        test_cmd.extend(['-f', 'null', '-'])
        return test_cmd

    @classmethod
    def _probe_hw_encoder_availability(cls, ffmpeg_bin, encoder_name):
        attempts = [cls._HW_PROBE_DEFAULT_SIZE]
        last_meta = None
        last_error = ''
        for idx, probe_size in enumerate(attempts):
            probe_retry = idx > 0
            test_cmd = cls._build_hw_probe_cmd(ffmpeg_bin=ffmpeg_bin, encoder_name=encoder_name, probe_size=probe_size)
            test_result = subprocess.run(test_cmd, capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=15)
            err_msg = (test_result.stderr or test_result.stdout or '').strip()
            if not err_msg and test_result.returncode != 0: err_msg = '未知错误'
            last_error = err_msg
            last_meta = {'probe_size': probe_size, 'probe_retry': probe_retry, 'probe_returncode': test_result.returncode, 'probe_error_short': cls._short_error_text(err_msg), 'probe_cmd_summary': cls._summarize_cmd(test_cmd)}
            if test_result.returncode == 0: return True, '', last_meta
            if idx == 0 and cls._is_probe_dimension_error(err_msg): attempts.append(cls._HW_PROBE_RETRY_SIZE)
        if not last_meta:
            last_meta = {'probe_size': cls._HW_PROBE_DEFAULT_SIZE, 'probe_retry': False, 'probe_returncode': -1, 'probe_error_short': '未知错误', 'probe_cmd_summary': ''}
        return False, last_error or '未知错误', last_meta

    def _parse_custom_video_params(self, task_logger):
        if not _as_bool(self.config.get('VIDEO_CUSTOM_PARAMS_ENABLED', False)): return None
        custom_params = str(self.config.get('VIDEO_CUSTOM_PARAMS', '')).strip()
        if not custom_params: return None
        try: return shlex.split(custom_params) or None
        except ValueError as e:
            task_logger.warning(f"自定义视频参数解析失败，回退默认编码参数: {e}")
            return None

    @staticmethod
    def _coerce_int(value):
        try: return int(value) if value is not None and value != '' else None
        except (TypeError, ValueError): return None

    @classmethod
    def _select_audio_target_bitrate(cls, input_bit_rate):
        bit_rate = cls._coerce_int(input_bit_rate)
        if bit_rate is None or bit_rate <= 0: return '128k'
        if bit_rate >= 192000: return '192k'
        if bit_rate >= 160000: return '160k'
        if bit_rate >= 128000: return '128k'
        if bit_rate >= 96000: return '96k'
        return '64k'

    @classmethod
    def _build_audio_transcode_params(cls, audio_info):
        return build_audio_params(audio_info)

    @staticmethod
    def _estimate_embed_timeout(video_duration, cpu_codec=None):
        if video_duration:
            rate = 3 if video_duration < 1800 else 2
            estimated_time = video_duration * rate
        else: estimated_time = 3600
        if normalize_cpu_codec(cpu_codec) == 'x265':
            estimated_time *= EMBED_TIMEOUT_X265_FACTOR
            ceiling = EMBED_TIMEOUT_MAX_SECONDS_X265
        else: ceiling = EMBED_TIMEOUT_MAX_SECONDS
        return int(max(EMBED_TIMEOUT_MIN_SECONDS, min(estimated_time, ceiling)))

    @staticmethod
    def _build_embed_ffmpeg_cmd(ffmpeg_bin, input_video, vf_filter, vparams, aparams, output_video, color_params=None):
        return [ffmpeg_bin, '-y', '-i', input_video, '-vf', vf_filter, *vparams, *(color_params or []), *aparams, '-movflags', '+faststart', '-progress', 'pipe:1', output_video]

    @classmethod
    def _is_known_hw_encoder_error(cls, error_output):
        if not error_output: return False
        error_lower = error_output.lower()
        return any(err.lower() in error_lower for err in cls._KNOWN_HW_ENCODER_ERROR_PATTERNS)

    @classmethod
    def _is_hw_option_error(cls, error_output):
        if not error_output: return False
        error_lower = error_output.lower()
        return any(err.lower() in error_lower for err in cls._HW_OPTION_ERROR_PATTERNS)

    @staticmethod
    def _resolve_embed_retry_stages(actual_encoder, hw_quality_boost, hw_error_detected, hw_option_error=None, cpu_codec=None):
        encoder = str(actual_encoder or '').strip().lower()
        codec = normalize_cpu_codec(cpu_codec)
        if encoder not in ('nvidia', 'intel', 'amd'):
            if encoder == 'cpu' and codec == 'x265': return ['cpu_x264']
            return []
        stages = []
        if hw_quality_boost:
            if hw_option_error is None: worth_disabling_boost = not bool(hw_error_detected)
            else: worth_disabling_boost = bool(hw_option_error) or not bool(hw_error_detected)
            if worth_disabling_boost: stages.append('hw_no_boost')
        stages.append('cpu')
        if codec == 'x265': stages.append('cpu_x264')
        return stages

    @classmethod
    def _embed_stage_budget(cls, video_duration, cpu_codec, stage, first_stage_budget):
        if stage == 'cpu': codec = cpu_codec
        elif stage == 'cpu_x264': codec = 'x264'
        else: return max(300.0, float(first_stage_budget))
        return max(300.0, float(cls._estimate_embed_timeout(video_duration, codec)))

    @staticmethod
    def _drop_same_encoder_stage_after_timeout(retry_stages):
        try: return [str(stage) for stage in (retry_stages or []) if str(stage) != 'hw_no_boost']
        except Exception: return []

    @staticmethod
    def _describe_hw_failure(actual_encoder, *, timed_out=False, hw_option_error=False, hw_error_detected=False, boost_enabled=True, returncode=None):
        if timed_out: return 'timeout'
        if hw_option_error: return 'option_error' if boost_enabled else 'option_error_no_boost'
        if hw_error_detected: return 'device_error'
        return 'unknown'

    @staticmethod
    def _finalize_embedded_video_output(temp_output_path, final_output_path):
        try: os.replace(temp_output_path, final_output_path)
        except OSError:
            shutil.copy2(temp_output_path, final_output_path)
            try: os.remove(temp_output_path)
            except Exception: pass

    def _convert_vtt_to_srt(self, vtt_path, task_logger):
        try:
            base_path, _ = os.path.splitext(vtt_path)
            srt_path = f"{base_path}.srt"
            with open(vtt_path, 'r', encoding='utf-8') as vtt_file: vtt_content = vtt_file.read()
            srt_content = _convert_vtt_text_to_srt_text(vtt_content)
            if not srt_content: return None
            with open(srt_path, 'w', encoding='utf-8') as srt_file: srt_file.write(srt_content)
            return srt_path
        except Exception: return None

    @staticmethod
    def _clamp(value, minimum, maximum): return max(minimum, min(maximum, value))

    @staticmethod
    def _interpolate_anchor_value(position, anchors):
        normalized_anchors = []
        for anchor_position, anchor_value in anchors or ():
            try: normalized_anchors.append((float(anchor_position), float(anchor_value)))
            except Exception: continue
        if not normalized_anchors: return 0.0
        normalized_anchors.sort(key=lambda item: item[0])
        if position <= normalized_anchors[0][0]: return normalized_anchors[0][1]
        for idx in range(1, len(normalized_anchors)):
            left_position, left_value = normalized_anchors[idx - 1]
            right_position, right_value = normalized_anchors[idx]
            if position <= right_position:
                if right_position <= left_position: return right_value
                ratio = (position - left_position) / (right_position - left_position)
                return left_value + (right_value - left_value) * ratio
        return normalized_anchors[-1][1]

    @classmethod
    def _resolve_ass_dimensions(cls, video_width, video_height):
        try: width = int(video_width)
        except Exception: width = 0
        try: height = int(video_height)
        except Exception: height = 0
        if width <= 0: width = cls._ASS_PLAY_RES_X
        if height <= 0: height = cls._ASS_PLAY_RES_Y
        return width, height

    @staticmethod
    def _format_ass_number(value):
        try: value_f = float(value)
        except Exception: return str(value)
        if value_f.is_integer(): return str(int(value_f))
        return f"{value_f:.2f}".rstrip('0').rstrip('.')

    @staticmethod
    def _seconds_to_ass_timestamp(seconds):
        try: total_cs = max(0, int(round(float(seconds) * 100)))
        except Exception: total_cs = 0
        hours = total_cs // 360000
        total_cs %= 360000
        minutes = total_cs // 6000
        total_cs %= 6000
        secs = total_cs // 100
        centis = total_cs % 100
        return f"{hours}:{minutes:02d}:{secs:02d}.{centis:02d}"

    @staticmethod
    def _escape_ass_text_line(text): return str(text or '').replace('\\', r'\\').replace('{', r'\{').replace('}', r'\}')

    @staticmethod
    def _escape_ass_text(text):
        normalized = str(text or '').replace('\r\n', '\n').replace('\r', '\n')
        return r'\N'.join(TaskProcessor._escape_ass_text_line(line) for line in normalized.split('\n'))

    @classmethod
    def _compose_ass_dialogue_text(cls, lines, override_font_size=None):
        escaped_lines = [cls._escape_ass_text_line(line) for line in (lines or []) if str(line or '').strip()]
        if not escaped_lines: return ''
        payload = r'\N'.join(escaped_lines)
        if override_font_size is None: return payload
        try: override_font_size = int(round(float(override_font_size)))
        except Exception: override_font_size = 0
        if override_font_size <= 0: return payload
        return f"{{\\fs{override_font_size}}}{payload}"

    @classmethod
    def _build_streaming_ass_style(cls, video_width, video_height, style_overrides=None):
        width, height = cls._resolve_ass_dimensions(video_width, video_height)
        style = dict(cls._ASS_STYLE_BASE)
        style.update({'PlayResX': width, 'PlayResY': height})
        is_portrait = height > width
        if is_portrait:
            font_size = cls._clamp(cls._interpolate_anchor_value(height, cls._ASS_PORTRAIT_FONT_SIZE_ANCHORS), 58.0, 80.0)
            margin_v = cls._clamp(cls._interpolate_anchor_value(height, cls._ASS_PORTRAIT_MARGIN_V_ANCHORS), 150.0, 320.0)
            side_margin = cls._clamp(width * cls._ASS_PORTRAIT_SIDE_MARGIN_RATIO, cls._ASS_PORTRAIT_SIDE_MARGIN_MIN, cls._ASS_PORTRAIT_SIDE_MARGIN_MAX)
        else:
            font_size = cls._clamp(cls._interpolate_anchor_value(height, cls._ASS_LANDSCAPE_FONT_SIZE_ANCHORS), 46.0, 132.0)
            margin_v = cls._clamp(cls._interpolate_anchor_value(height, cls._ASS_LANDSCAPE_MARGIN_V_ANCHORS), 46.0, 156.0)
            side_margin = cls._clamp(width * cls._ASS_LANDSCAPE_SIDE_MARGIN_RATIO, cls._ASS_LANDSCAPE_SIDE_MARGIN_MIN, cls._ASS_LANDSCAPE_SIDE_MARGIN_MAX)
        style.update({'FontSize': font_size, 'Outline': cls._clamp(font_size * cls._ASS_OUTLINE_RATIO, cls._ASS_OUTLINE_MIN, cls._ASS_OUTLINE_MAX), 'Shadow': cls._clamp(font_size * cls._ASS_SHADOW_RATIO, cls._ASS_SHADOW_MIN, cls._ASS_SHADOW_MAX), 'MarginV': margin_v, 'MarginL': side_margin, 'MarginR': side_margin})
        return style if not style_overrides else apply_style_overrides(style, style_overrides)

    @staticmethod
    def _sanitize_ass_font_name(font_family):
        return str(font_family or 'Arial').replace('\r', ' ').replace('\n', ' ').strip() or 'Arial'

    @classmethod
    def _resolve_streaming_srt_template_height(cls, video_width, video_height):
        _, height = cls._resolve_ass_dimensions(video_width, video_height)
        for template_height in cls._STREAMING_SRT_TEMPLATE_HEIGHTS:
            if height <= template_height: return template_height
        return cls._STREAMING_SRT_TEMPLATE_HEIGHTS[-1]

    @classmethod
    def _build_streaming_srt_style_description(cls, font_family, video_width, video_height):
        width, height = cls._resolve_ass_dimensions(video_width, video_height)
        template_height = cls._resolve_streaming_srt_template_height(width, height)
        template = dict(cls._STREAMING_SRT_STYLE_TEMPLATES[template_height])
        is_portrait = height > width
        if is_portrait:
            template.update({'FontSize': cls._clamp(cls._interpolate_anchor_value(height, cls._STREAMING_SRT_PORTRAIT_FONT_SIZE_ANCHORS), 12.5, 13.5), 'MarginL': int(round(cls._clamp(width * cls._STREAMING_SRT_PORTRAIT_SIDE_MARGIN_RATIO, 40.0, 88.0))), 'MarginR': int(round(cls._clamp(width * cls._STREAMING_SRT_PORTRAIT_SIDE_MARGIN_RATIO, 40.0, 88.0))), 'MarginV': int(round(cls._clamp(cls._interpolate_anchor_value(height, cls._STREAMING_SRT_PORTRAIT_MARGIN_V_ANCHORS), 16.0, 44.0)))})
        template.update({'FontName': cls._sanitize_ass_font_name(font_family), 'Alignment': 2, 'BorderStyle': 1, 'OriginalSize': f"{width}x{height}", 'TemplateHeight': template_height})
        return template

    @classmethod
    def _build_streaming_srt_force_style(cls, font_family, video_width, video_height):
        style = cls._build_streaming_srt_style_description(font_family, video_width, video_height)
        entries = [f"FontName={style['FontName']}", f"FontSize={cls._format_ass_number(style['FontSize'])}", f"Outline={cls._format_ass_number(style['Outline'])}", f"Shadow={cls._format_ass_number(style['Shadow'])}", f"MarginL={int(style['MarginL'])}", f"MarginR={int(style['MarginR'])}", f"MarginV={int(style['MarginV'])}", f"Alignment={style['Alignment']}", f"BorderStyle={style['BorderStyle']}"]
        payload = ','.join(entries).replace("'", r"\'")
        return f"force_style='{payload}'"

    @classmethod
    def _build_streaming_srt_filter(cls, render_subtitle_name, font_family, video_width, video_height):
        style = cls._build_streaming_srt_style_description(font_family, video_width, video_height)
        filter_segments = [f"subtitles={render_subtitle_name}", f"original_size={style['OriginalSize']}", "wrap_unicode=1", "fontsdir=fonts", "charenc=UTF-8", cls._build_streaming_srt_force_style(font_family, video_width, video_height)]
        return ':'.join(filter_segments)

    @classmethod
    def _estimate_streaming_srt_layout_limits(cls, video_width, video_height):
        style = cls._build_streaming_srt_style_description('Arial', video_width, video_height)
        width, height = cls._resolve_ass_dimensions(video_width, video_height)
        is_portrait = height > width
        usable_width = max(120.0, float(width) - float(style['MarginL']) - float(style['MarginR']))
        font_size = max(1.0, float(style['FontSize']))
        density = cls._STREAMING_SRT_PORTRAIT_LAYOUT_DENSITY if is_portrait else cls._STREAMING_SRT_LANDSCAPE_LAYOUT_DENSITY
        max_line_length = int(round(usable_width / font_size * density))
        if is_portrait:
            max_line_length = int(cls._clamp(max_line_length, 12.0, 18.0))
            max_lines = 3
        else:
            max_line_length = int(cls._clamp(max_line_length, 20.0, 26.0))
            max_lines = 1
        return max_line_length, max_lines

    @classmethod
    def _create_streaming_srt_engine(cls, video_width=None, video_height=None):
        from .srt_transform_engine import SrtTransformConfig, SrtTransformEngine
        max_line_length, max_lines = cls._estimate_streaming_srt_layout_limits(video_width, video_height)
        return SrtTransformEngine(SrtTransformConfig(max_line_length=max_line_length, max_lines=max_lines, split_long_cues=False, preserve_line_breaks=False, normalize_punctuation=False, filter_filler_words=False), logger=logger)

    @classmethod
    def _wrap_streaming_srt_text(cls, text, video_width, video_height):
        normalized = cls._merge_subtitle_text_parts(str(text or '').replace('\r\n', '\n').replace('\r', '\n').split('\n'))
        if not normalized: return ''
        width, height = cls._resolve_ass_dimensions(video_width, video_height)
        is_portrait = height > width
        style = cls._build_streaming_srt_style_description('Arial', width, height)
        max_line_length, max_lines = cls._estimate_streaming_srt_layout_limits(width, height)
        usable_width = max(120.0, float(width) - float(style['MarginL']) - float(style['MarginR']))
        font_size = max(1.0, float(style['FontSize']))
        outline = float(style['Outline'])
        shadow = float(style['Shadow'])
        single_line_limit = int(cls._clamp(round(usable_width / font_size * cls._ASS_LANDSCAPE_SINGLE_LINE_DENSITY), 18.0, 28.0))
        wrapped_lines = cls._build_wrapped_lines_for_ass(normalized, is_portrait=is_portrait, max_line_length=max_line_length, max_lines=max_lines, single_line_limit=single_line_limit, aggressive=False)
        fits, _, _, _ = cls._check_ass_lines_width_safety(wrapped_lines, usable_width, font_size, outline, shadow)
        if not fits:
            hard_wrap_lines, _ = cls._find_safe_hard_wrap_lines(normalized, max_line_length=max_line_length, max_lines=max_lines, usable_width=usable_width, font_size=font_size, outline=outline, shadow=shadow)
            if hard_wrap_lines: wrapped_lines = hard_wrap_lines
        return '\n'.join(line for line in wrapped_lines if line).strip() or normalized

    @classmethod
    def _prepare_streaming_srt_cues(cls, subtitle_text, video_width=None, video_height=None):
        engine = cls._create_streaming_srt_engine(video_width, video_height)
        cues = engine.parse_srt(subtitle_text or '')
        if not cues: return []
        total_duration = max(float(cue.get('end', 0.0) or 0.0) for cue in cues)
        cues = engine.resolve_overlaps(cues, total_duration)
        cues = engine.apply_text_processing(cues)
        cues = engine.finalize_cues(cues, total_duration)
        for cue in cues: cue['text'] = cls._wrap_streaming_srt_text(cue.get('text', ''), video_width, video_height)
        return cues

    def _build_streaming_srt_file(self, subtitle_path, srt_output_path, task_logger, *, video_width=None, video_height=None):
        try:
            source_path = subtitle_path
            subtitle_ext = os.path.splitext(subtitle_path)[1].lower()
            if subtitle_ext == '.vtt':
                converted_srt = self._convert_vtt_to_srt(subtitle_path, task_logger)
                if not converted_srt or not os.path.exists(converted_srt):
                    task_logger.error("VTT转SRT失败，无法生成流媒体SRT字幕")
                    return False
                source_path = converted_srt
            with open(source_path, 'r', encoding='utf-8-sig', errors='replace') as subtitle_file: subtitle_content = subtitle_file.read()
            cues = self._prepare_streaming_srt_cues(subtitle_content, video_width=video_width, video_height=video_height)
            if not cues: return False
            srt_text = self._create_streaming_srt_engine(video_width, video_height).render_srt(cues)
            if not srt_text: return False
            with open(srt_output_path, 'w', encoding='utf-8') as srt_file: srt_file.write(srt_text)
            return True
        except Exception as e:
            task_logger.error(f"生成流媒体SRT失败: {str(e)}")
            return False

    @classmethod
    def _build_subtitle_style_description(cls, font_family, video_width, video_height, style_overrides=None):
        style = cls._build_streaming_ass_style(video_width, video_height, style_overrides)
        font_name = cls._sanitize_ass_font_name(font_family)
        force_style = {'FontName': font_name, 'FontSize': cls._format_ass_number(style['FontSize']), 'Outline': cls._format_ass_number(style['Outline']), 'Shadow': cls._format_ass_number(style['Shadow']), 'MarginL': str(int(round(style['MarginL']))), 'MarginR': str(int(round(style['MarginR']))), 'MarginV': str(int(round(style['MarginV']))), 'Alignment': str(style['Alignment'])}
        for key in cls._FORCE_STYLE_APPEARANCE_KEYS:
            if key in style: force_style[key] = cls._format_ass_number(style[key])
        return style, force_style

    @classmethod
    def _build_subtitle_force_style(cls, font_family, video_width, video_height, style_overrides=None):
        _, force_style = cls._build_subtitle_style_description(font_family, video_width, video_height, style_overrides)
        entries = [f"{key}={value}" for key, value in force_style.items()]
        payload = ','.join(entries).replace("'", r"\'")
        return f"force_style='{payload}'"

    @classmethod
    def _estimate_subtitle_layout_limits(cls, video_width, video_height, style_overrides=None):
        style = cls._build_streaming_ass_style(video_width, video_height, style_overrides)
        is_portrait = float(style['PlayResY']) > float(style['PlayResX'])
        usable_width = max(120.0, float(style['PlayResX']) - float(style['MarginL']) - float(style['MarginR']))
        font_size = max(1.0, float(style['FontSize']))
        density = cls._ASS_PORTRAIT_LAYOUT_DENSITY if is_portrait else cls._ASS_LANDSCAPE_LAYOUT_DENSITY
        max_line_length = int(round(usable_width / font_size * density))
        if is_portrait:
            max_line_length = int(cls._clamp(max_line_length, 7.0, 14.0))
            max_lines = 5
        else:
            max_line_length = int(cls._clamp(max_line_length, 18.0, 22.0))
            max_lines = 1
        return max_line_length, max_lines

    @staticmethod
    def _is_cjk_like_char(char):
        if not char: return False
        return unicodedata.east_asian_width(char) in {'W', 'F'}

    @classmethod
    def _merge_subtitle_text_parts(cls, parts):
        merged = ''
        trailing_no_space = '([{\u3008\u300a\u300c\u300e\u3010'
        leading_no_space = '.,!?;:)]}，。！？；：、…】）》」』'
        for part in parts or []:
            piece = str(part or '').strip()
            if not piece: continue
            if not merged:
                merged = piece
                continue
            prev_char = merged[-1]
            curr_char = piece[0]
            if (prev_char.isspace() or curr_char.isspace() or prev_char in trailing_no_space or curr_char in leading_no_space or (cls._is_cjk_like_char(prev_char) and cls._is_cjk_like_char(curr_char))): merged += piece
            else: merged += f" {piece}"
        return merged.strip()

    @classmethod
    def _limit_wrapped_lines(cls, lines, max_lines):
        filtered_lines = [str(line or '').strip() for line in (lines or []) if str(line or '').strip()]
        if len(filtered_lines) <= max_lines: return filtered_lines
        if max_lines >= 4:
            visible_lines = filtered_lines[:max_lines - 1]
            remainder = cls._merge_subtitle_text_parts(filtered_lines[max_lines - 1:])
            if remainder: visible_lines.append(remainder)
            return [line for line in visible_lines if line]
        merged_text = cls._merge_subtitle_text_parts(filtered_lines)
        if not merged_text or max_lines <= 1: return filtered_lines[:max_lines]
        rebalanced_lines = []
        remaining_text = merged_text
        remaining_slots = int(max_lines)
        while remaining_slots > 1 and remaining_text:
            remaining_units = cls._estimate_subtitle_text_units(remaining_text)
            target_line_length = max(6.0, remaining_units / remaining_slots)
            split_index = cls._find_balanced_wrap_index(remaining_text, target_line_length)
            if split_index <= 0:
                fallback_lines = cls._wrap_subtitle_segment_greedily(remaining_text, target_line_length)
                if len(fallback_lines) <= 1: break
                current_line = str(fallback_lines[0] or '').strip()
                next_text = cls._merge_subtitle_text_parts(fallback_lines[1:])
            else:
                current_line = remaining_text[:split_index].strip()
                next_text = remaining_text[split_index:].strip()
            if not current_line or not next_text: break
            rebalanced_lines.append(current_line)
            remaining_text = next_text
            remaining_slots -= 1
        if remaining_text: rebalanced_lines.append(remaining_text.strip())
        return [line for line in rebalanced_lines[:max_lines] if line]

    @classmethod
    def _build_wrapped_lines_for_ass(cls, normalized, *, is_portrait, max_line_length, max_lines, single_line_limit, aggressive=False):
        raw_segments = [segment.strip() for segment in str(normalized or '').split('\n') if segment.strip()]
        wrapped_lines = []
        for segment in raw_segments:
            if is_portrait or aggressive:
                max_lines = max(1, int(max_lines))
                candidate_lines = cls._build_optimal_multiline_partition(segment, max_line_length=max_line_length, min_lines=1, max_lines=max_lines)
                if candidate_lines: wrapped_lines.extend(candidate_lines)
                else: wrapped_lines.extend(cls._wrap_subtitle_segment_greedily(segment, max_line_length))
            elif int(max_lines) <= 1: wrapped_lines.append(segment)
            else: wrapped_lines.extend(cls._wrap_landscape_segment_for_ass(segment, single_line_limit=single_line_limit, max_line_length=max_line_length))
        return cls._limit_wrapped_lines(wrapped_lines, max_lines)

    @classmethod
    def _estimate_ass_line_render_width(cls, line, font_size, outline, shadow):
        text_units = cls._estimate_subtitle_text_units(str(line or ''))
        if text_units <= 0: return 0.0
        padding = max(8.0, float(outline) * 2.8 + float(shadow) * 1.8 + float(font_size) * 0.14)
        return text_units * float(font_size) + padding

    @classmethod
    def _check_ass_lines_width_safety(cls, lines, usable_width, font_size, outline, shadow):
        safe_width = max(1.0, float(usable_width) * cls._ASS_SAFE_WIDTH_RATIO)
        line_widths = [cls._estimate_ass_line_render_width(line, font_size, outline, shadow) for line in (lines or []) if str(line or '').strip()]
        if not line_widths: return True, 0.0, safe_width, []
        max_width = max(line_widths)
        return max_width <= safe_width, max_width, safe_width, line_widths

    @classmethod
    def _estimate_safe_line_units(cls, usable_width, font_size, outline, shadow):
        safe_width = max(1.0, float(usable_width) * cls._ASS_SAFE_WIDTH_RATIO)
        padding = max(6.0, float(outline) * 4.0 + float(shadow) * 2.0 + float(font_size) * 0.08)
        return max(float(cls._ASS_HARD_WRAP_MIN_LINE_LENGTH), (safe_width - padding) / max(1.0, float(font_size)))

    @classmethod
    def _can_lines_fit_with_font_override(cls, lines, usable_width, font_size, outline, shadow):
        fits, _, _, _ = cls._check_ass_lines_width_safety(lines, usable_width, font_size, outline, shadow)
        if fits: return True
        _, override_fits = cls._resolve_safe_override_font_size(lines, usable_width, font_size, outline, shadow)
        return override_fits

    @classmethod
    def _score_partition_line(cls, line, target_units, max_line_length, *, is_last):
        stripped = str(line or '').strip()
        if not stripped: return float('inf')
        units = cls._estimate_subtitle_text_units(stripped)
        hard_limit = max(float(max_line_length) * 1.5, float(max_line_length) + 8.0)
        if units > hard_limit: return float('inf')
        score = (abs(units - float(target_units)) ** 2) * 1.3
        if units > float(max_line_length): score += ((units - float(max_line_length)) ** 2) * 36.0
        minimum_units = max(4.5, float(target_units) * (0.60 if is_last else 0.72))
        if units < minimum_units: score += (minimum_units - units) ** 2 * (8.0 if is_last else 16.0)
        if stripped[0] in '.,!?;:，。！？；：、)]}】）》」』': score += 25.0
        if stripped[-1] in '([{【（《“‘': score += 25.0
        if not is_last and cls._is_short_orphan_tail(stripped): score += 10.0
        return score

    @classmethod
    def _build_optimal_multiline_partition(cls, normalized, *, max_line_length, min_lines, max_lines):
        from functools import lru_cache
        raw_segments = [segment.strip() for segment in str(normalized or '').split('\n') if segment.strip()]
        merged_text = cls._merge_subtitle_text_parts(raw_segments)
        if not merged_text: return []
        minimum_lines = max(1, int(min_lines))
        maximum_lines = max(minimum_lines, int(max_lines))
        preferred_points = cls._collect_candidate_wrap_indices(merged_text, include_fallback=False)
        all_points = cls._collect_candidate_wrap_indices(merged_text, include_fallback=True)
        total_units = cls._estimate_subtitle_text_units(merged_text)
        best_score = None
        best_lines = []
        for point_source in (preferred_points, all_points):
            candidate_points = point_source
            if not candidate_points: continue
            points = [0] + sorted(set(idx for idx in candidate_points if 0 < idx < len(merged_text))) + [len(merged_text)]
            if len(points) < minimum_lines + 1: continue
            if len(points) < 2: return [merged_text]
            for line_count in range(minimum_lines, maximum_lines + 1):
                target_units = max(6.0, total_units / max(1, line_count))
                @lru_cache(maxsize=None)
                def solve(start_idx, lines_left):
                    remaining_text = merged_text[points[start_idx]:].strip()
                    if not remaining_text: return float('inf'), tuple()
                    if lines_left == 1:
                        line_score = cls._score_partition_line(remaining_text, target_units, max_line_length, is_last=True)
                        if line_score == float('inf'): return float('inf'), tuple()
                        return line_score, (remaining_text,)
                    best_local = float('inf'), tuple()
                    max_next_index = len(points) - lines_left
                    for next_idx in range(start_idx + 1, max_next_index + 1):
                        line = merged_text[points[start_idx]:points[next_idx]].strip()
                        if not line: continue
                        current_score = cls._score_partition_line(line, target_units, max_line_length, is_last=False)
                        if current_score == float('inf'): continue
                        tail_score, tail_lines = solve(next_idx, lines_left - 1)
                        total_score = current_score + tail_score
                        if total_score < best_local[0]: best_local = total_score, (line,) + tail_lines
                    return best_local
                score, lines = solve(0, line_count)
                if not lines: continue
                score += max(0, line_count - minimum_lines) * 3.0
                if best_score is None or score < best_score:
                    best_score = score
                    best_lines = list(lines)
            if best_lines: break
        return best_lines

    @classmethod
    def _find_portrait_rescue_lines(cls, normalized, *, max_line_length, usable_width, font_size, outline, shadow):
        rescue_font_size = max(float(cls._ASS_OVERRIDE_FONT_SIZE_MIN), float(font_size) * float(cls._ASS_OVERRIDE_FONT_SIZE_RATIO_MIN))
        rescue_outline = cls._clamp(rescue_font_size * cls._ASS_OVERRIDE_OUTLINE_RATIO, cls._ASS_OVERRIDE_OUTLINE_MIN, cls._ASS_OVERRIDE_OUTLINE_MAX)
        rescue_shadow = cls._clamp(rescue_font_size * 0.016, 0.7, 1.35)
        rescue_line_length = int(round(cls._estimate_safe_line_units(usable_width, rescue_font_size, rescue_outline, rescue_shadow)))
        rescue_line_length = int(cls._clamp(rescue_line_length, max_line_length + 1, cls._ASS_PORTRAIT_RESCUE_LINE_LENGTH_MAX))
        best_lines = []
        for line_count in (4, 5):
            candidate_lines = cls._build_optimal_multiline_partition(normalized, max_line_length=rescue_line_length, min_lines=line_count, max_lines=line_count)
            if not candidate_lines: continue
            if cls._can_lines_fit_with_font_override(candidate_lines, usable_width, font_size, outline, shadow): return candidate_lines, True
            if not best_lines: best_lines = candidate_lines
        return best_lines, False

    @classmethod
    def _find_landscape_rescue_lines(cls, normalized, *, max_line_length, usable_width, font_size, outline, shadow):
        rescue_line_length = int(round(cls._estimate_safe_line_units(usable_width, font_size, outline, shadow)))
        rescue_line_length = int(cls._clamp(rescue_line_length, max_line_length, cls._ASS_LANDSCAPE_SINGLE_LINE_LIMIT_MAX))
        best_lines = []
        for line_count in (3, 4):
            candidate_lines = cls._build_optimal_multiline_partition(normalized, max_line_length=rescue_line_length, min_lines=line_count, max_lines=line_count)
            if not candidate_lines: continue
            if cls._can_lines_fit_with_font_override(candidate_lines, usable_width, font_size, outline, shadow): return candidate_lines, True
            if not best_lines: best_lines = candidate_lines
        return best_lines, False

    @classmethod
    def _find_safe_hard_wrap_lines(cls, normalized, *, max_line_length, max_lines, usable_width, font_size, outline, shadow):
        best_lines = []
        best_width = None
        for line_limit in range(int(max_line_length), int(cls._ASS_HARD_WRAP_MIN_LINE_LENGTH) - 1, -1):
            candidate_lines = cls._build_aggressive_two_line_candidate(normalized, max_line_length=line_limit, max_lines=max_lines)
            fits, max_width, _, _ = cls._check_ass_lines_width_safety(candidate_lines, usable_width, font_size, outline, shadow)
            if best_width is None or max_width < best_width:
                best_lines = candidate_lines
                best_width = max_width
            if fits: return candidate_lines, True
        return best_lines, False

    @classmethod
    def _build_aggressive_two_line_candidate(cls, normalized, *, max_line_length, max_lines):
        raw_segments = [segment.strip() for segment in str(normalized or '').split('\n') if segment.strip()]
        merged_text = cls._merge_subtitle_text_parts(raw_segments)
        if not merged_text: return []
        if max_lines <= 1 or cls._estimate_subtitle_text_units(merged_text) <= max_line_length: return [merged_text]
        if max_lines > 2: return cls._limit_wrapped_lines(cls._wrap_subtitle_segment_greedily(merged_text, max_line_length), max_lines)
        split_index = cls._find_balanced_wrap_index(merged_text, max_line_length)
        if split_index > 0: return cls._limit_wrapped_lines([merged_text[:split_index].strip(), merged_text[split_index:].strip()], max_lines)
        return cls._limit_wrapped_lines(cls._wrap_subtitle_segment_greedily(merged_text, max_line_length), max_lines)

    @classmethod
    def _resolve_safe_override_font_size(cls, lines, usable_width, font_size, outline, shadow):
        fits, max_width, safe_width, _ = cls._check_ass_lines_width_safety(lines, usable_width, font_size, outline, shadow)
        if fits or max_width <= 0: return None, True
        min_font_size = max(float(cls._ASS_OVERRIDE_FONT_SIZE_MIN), float(font_size) * float(cls._ASS_OVERRIDE_FONT_SIZE_RATIO_MIN))
        target_font_size = max(min_font_size, float(font_size) * (safe_width / max_width))
        target_font_size = min(float(font_size), target_font_size)
        target_font_size = float(int(max(1, round(target_font_size))))
        if target_font_size >= float(font_size): return None, False
        adjusted_outline = cls._clamp(target_font_size * cls._ASS_OVERRIDE_OUTLINE_RATIO, cls._ASS_OVERRIDE_OUTLINE_MIN, cls._ASS_OVERRIDE_OUTLINE_MAX)
        adjusted_shadow = cls._clamp(target_font_size * 0.016, 0.7, 1.35)
        adjusted_fits, _, _, _ = cls._check_ass_lines_width_safety(lines, usable_width, target_font_size, adjusted_outline, adjusted_shadow)
        return target_font_size, adjusted_fits

    @classmethod
    def _resolve_single_line_scaled_font(cls, text, usable_width, font_size, outline, shadow, *, min_scale):
        single_line = [str(text or '').strip()]
        if not single_line[0]: return None, None, None
        min_allowed_font = max(float(cls._ASS_OVERRIDE_FONT_SIZE_MIN), float(font_size) * float(cls._ASS_OVERRIDE_FONT_SIZE_RATIO_MIN), float(font_size) * float(min_scale))
        if min_allowed_font >= float(font_size): return None, None, None
        def _fits_at_size(size):
            test_outline = cls._clamp(size * cls._ASS_OUTLINE_RATIO, cls._ASS_OUTLINE_MIN, cls._ASS_OUTLINE_MAX)
            test_shadow = cls._clamp(size * cls._ASS_SHADOW_RATIO, cls._ASS_SHADOW_MIN, cls._ASS_SHADOW_MAX)
            fits, _, _, _ = cls._check_ass_lines_width_safety(single_line, usable_width, size, test_outline, test_shadow)
            return fits
        lo = min_allowed_font
        hi = float(font_size)
        if _fits_at_size(hi): return None, None, None
        if not _fits_at_size(lo): return None, None, None
        best_size = lo
        for _ in range(12):
            mid = (lo + hi) / 2.0
            if _fits_at_size(mid):
                best_size = mid
                lo = mid
            else: hi = mid
        best_size = float(int(round(best_size)))
        best_size = min(best_size, font_size - 1.0)
        if best_size < min_allowed_font: return None, None, None
        best_outline = cls._clamp(best_size * cls._ASS_OUTLINE_RATIO, cls._ASS_OUTLINE_MIN, cls._ASS_OUTLINE_MAX)
        best_shadow = cls._clamp(best_size * cls._ASS_SHADOW_RATIO, cls._ASS_SHADOW_MIN, cls._ASS_SHADOW_MAX)
        return best_size, best_outline, best_shadow

    @staticmethod
    def _is_preferred_wrap_boundary(char):
        if not char: return False
        return char.isspace() or char in '.,!?;:，。！？；：、)]}】）》」』'

    @staticmethod
    def _is_latin_word_char(char):
        if not char: return False
        return char.isascii() and (char.isalnum() or char in "'#&+_./-")

    @classmethod
    def _is_disallowed_wrap_pair(cls, left_char, right_char):
        if not left_char or not right_char: return False
        if cls._is_latin_word_char(left_char) and cls._is_latin_word_char(right_char): return True
        if left_char in '([{【（《“‘' or right_char in ')]}】）》」』”’': return True
        if left_char.isdigit() and right_char.isascii() and right_char.isalpha(): return True
        if cls._is_latin_word_char(left_char) and right_char == '%': return True
        return False

    @classmethod
    def _is_cjk_cjk_split(cls, text, split_index):
        segment = str(text or '')
        if split_index <= 0 or split_index >= len(segment): return False
        return cls._is_cjk_like_char(segment[split_index - 1]) and cls._is_cjk_like_char(segment[split_index])

    @classmethod
    def _should_keep_ascii_phrase_together(cls, text, split_index):
        segment = str(text or '')
        if split_index <= 0 or split_index >= len(segment): return False
        if not segment[split_index - 1].isspace(): return False
        left_end = split_index - 1
        left_start = left_end - 1
        while left_start >= 0 and cls._is_latin_word_char(segment[left_start]): left_start -= 1
        left_token = segment[left_start + 1:left_end].strip()
        right_end = split_index
        while right_end < len(segment) and cls._is_latin_word_char(segment[right_end]): right_end += 1
        right_token = segment[split_index:right_end].strip()
        if not left_token or not right_token: return False
        combined_length = len(left_token) + len(right_token) + 1
        if combined_length <= 15: return True
        if left_token.isupper() and len(left_token) <= 4: return True
        return False

    @classmethod
    def _collect_candidate_wrap_indices(cls, segment, include_fallback=True):
        preferred = []
        fallback = []
        for idx in range(1, len(segment)):
            left_char = segment[idx - 1]
            right_char = segment[idx]
            if cls._is_disallowed_wrap_pair(left_char, right_char): continue
            if left_char.isspace() and cls._should_keep_ascii_phrase_together(segment, idx): continue
            right_text = segment[idx:].strip()
            if cls._is_preferred_wrap_boundary(left_char) or cls._semantic_wrap_bonus(right_text) > 0.0: preferred.append(idx)
            else: fallback.append(idx)
        return preferred if not include_fallback else preferred + fallback

    @classmethod
    def _find_wrap_boundary(cls, chars):
        joined_chars = ''.join(chars)
        for idx in range(len(chars) - 1, -1, -1):
            right_char = chars[idx + 1] if idx + 1 < len(chars) else ''
            if chars[idx].isspace() and cls._should_keep_ascii_phrase_together(joined_chars, idx + 1): continue
            if cls._is_preferred_wrap_boundary(chars[idx]) and not cls._is_disallowed_wrap_pair(chars[idx], right_char): return idx
        return -1

    @staticmethod
    def _estimate_subtitle_char_units(char):
        if not char: return 0.0
        if char.isspace(): return 0.35
        if unicodedata.east_asian_width(char) in {'W', 'F'}: return 0.7 if unicodedata.category(char).startswith('P') else 1.0
        if char.isascii(): return 0.6 if char.isalnum() else 0.45
        if unicodedata.category(char).startswith('P'): return 0.6
        return 0.8

    @classmethod
    def _estimate_subtitle_text_units(cls, text):
        return sum(cls._estimate_subtitle_char_units(char) for char in str(text or ''))

    @staticmethod
    def _is_punctuation_only_text(text):
        stripped = str(text or '').strip()
        if not stripped: return False
        return all(char.isspace() or unicodedata.category(char).startswith('P') for char in stripped)

    @classmethod
    def _is_short_orphan_tail(cls, text):
        stripped = str(text or '').strip()
        if not stripped: return False
        if cls._is_punctuation_only_text(stripped): return True
        tail_core = stripped.rstrip('.,!?;:，。！？；：、… ')
        if not tail_core: return True
        tail_units = cls._estimate_subtitle_text_units(tail_core)
        if tail_units <= 3.0: return True
        return tail_core in {'吗', '呢', '啊', '吧', '呀', '了', '嘛', '么', '呗', '哇', '哦', '喔'}

    @classmethod
    def _semantic_wrap_bonus(cls, text):
        stripped = str(text or '').strip()
        if not stripped: return 0.0
        strong_prefixes = ('而不是', '而非', '并不是', '不是', '但是', '不过', '然而', '因此', '所以', '因为', '如果', '虽然', '并且', '或者', '还是', '以及', '然后', '兼顾', '保持', '避免', '确保', '否则', 'rather than', 'instead of', 'because', 'however', 'therefore', 'although')
        weak_prefixes = ('而', '但', '却', '并', '或', 'and', 'but', 'or', 'if', 'when', 'while', 'that', 'which')
        lowered = stripped.lower()
        if lowered.startswith(strong_prefixes): return 18.0
        if lowered.startswith(weak_prefixes): return 8.0
        return 0.0

    @classmethod
    def _is_broken_compound_wrap(cls, left_text, right_text):
        left = str(left_text or '').strip().lower()
        right = str(right_text or '').strip().lower()
        if not left or not right: return False
        broken_pairs = (('而', '不是'), ('并', '不是'), ('rather', 'than'), ('instead', 'of'))
        return any(left.endswith(prefix) and right.startswith(suffix) for prefix, suffix in broken_pairs)

    @classmethod
    def _wrap_subtitle_segment_greedily(cls, segment, max_line_length):
        wrapped_lines = []
        chars = []
        current_units = 0.0
        idx = 0
        while idx < len(segment):
            char = segment[idx]
            char_units = cls._estimate_subtitle_char_units(char)
            if chars and current_units + char_units > max_line_length:
                break_at = cls._find_wrap_boundary(chars)
                if break_at >= 0:
                    line_chars = chars[:break_at + 1]
                    remainder = ''.join(chars[break_at + 1:]).strip()
                else:
                    tail_start = len(chars)
                    while tail_start > 0 and cls._is_latin_word_char(chars[tail_start - 1]): tail_start -= 1
                    if 0 < tail_start < len(chars):
                        line_chars = chars[:tail_start]
                        remainder = ''.join(chars[tail_start:]).strip()
                    elif tail_start == 0 and all(cls._is_latin_word_char(c) for c in chars):
                        line_chars = chars
                        remainder = ''
                    else:
                        line_chars = chars
                        remainder = ''
                line = ''.join(line_chars).strip()
                if line: wrapped_lines.append(line)
                chars = list(remainder)
                current_units = cls._estimate_subtitle_text_units(remainder)
                continue
            chars.append(char)
            current_units += char_units
            idx += 1
        line = ''.join(chars).strip()
        if line: wrapped_lines.append(line)
        return wrapped_lines

    @classmethod
    def _find_balanced_wrap_index(cls, segment, max_line_length):
        best_index = -1
        best_score = None
        candidate_indices = cls._collect_candidate_wrap_indices(segment)
        if not candidate_indices: return -1
        for idx in candidate_indices:
            left = segment[:idx].strip()
            right = segment[idx:].strip()
            if not left or not right: continue
            left_units = cls._estimate_subtitle_text_units(left)
            right_units = cls._estimate_subtitle_text_units(right)
            boundary_char = segment[idx - 1]
            overflow = max(0.0, left_units - max_line_length) + max(0.0, right_units - max_line_length)
            score = abs(left_units - right_units) + overflow * 8.0
            if not cls._is_preferred_wrap_boundary(boundary_char): score += 4.5
            elif boundary_char.isspace(): score -= 1.5
            else: score -= 4.0
            if cls._is_short_orphan_tail(right): score += 15.0
            if left_units <= 3.0: score += 8.0
            shorter = min(left_units, right_units)
            longer = max(left_units, right_units)
            if shorter > 0 and longer / shorter > 3.0: score += 6.0
            score -= cls._semantic_wrap_bonus(right)
            if cls._is_broken_compound_wrap(left, right): score += 12.0
            if cls._is_cjk_cjk_split(segment, idx): score += 4.0
            if best_score is None or score < best_score:
                best_score = score
                best_index = idx
        return best_index

    _SINGLE_LINE_TOLERANCE = 3.5

    @classmethod
    def _wrap_landscape_segment_for_ass(cls, segment, single_line_limit, max_line_length):
        total_units = cls._estimate_subtitle_text_units(segment)
        if total_units <= single_line_limit: return [segment]
        tolerance = cls._SINGLE_LINE_TOLERANCE
        split_index = cls._find_balanced_wrap_index(segment, max_line_length)
        if split_index <= 0:
            if total_units <= single_line_limit + tolerance: return [segment]
            return cls._wrap_subtitle_segment_greedily(segment, max_line_length)
        first_line = segment[:split_index].strip()
        second_line = segment[split_index:].strip()
        if not first_line or not second_line:
            if total_units <= single_line_limit + tolerance: return [segment]
            return cls._wrap_subtitle_segment_greedily(segment, max_line_length)
        if cls._is_short_orphan_tail(second_line) and total_units <= single_line_limit + tolerance: return [segment]
        first_units = cls._estimate_subtitle_text_units(first_line)
        second_units = cls._estimate_subtitle_text_units(second_line)
        shorter = min(first_units, second_units)
        longer = max(first_units, second_units)
        if shorter > 0 and longer / shorter > 3.0 and total_units <= single_line_limit + tolerance: return [segment]
        if first_units > max_line_length * 1.35 or second_units > max_line_length * 1.35:
            fallback_lines = cls._wrap_subtitle_segment_greedily(segment, max_line_length)
            if len(fallback_lines) == 2 and not cls._is_short_orphan_tail(fallback_lines[1]): return fallback_lines
            if total_units <= single_line_limit + tolerance: return [segment]
            return fallback_lines
        return [first_line, second_line]

    @classmethod
    def _wrap_subtitle_text_for_ass(cls, text, video_width, video_height, return_meta=False, *, prefer_single_line=True, single_line_min_font_scale=None, style_overrides=None):
        normalized = cls._merge_subtitle_text_parts(str(text or '').replace('\r\n', '\n').replace('\r', '\n').split('\n'))
        wrap_meta = {'forced_wrap': False, 'font_override': None, 'overflow_warning': False}
        if not normalized: return ('', wrap_meta) if return_meta else ''
        max_line_length, max_lines = cls._estimate_subtitle_layout_limits(video_width, video_height, style_overrides)
        style = cls._build_streaming_ass_style(video_width, video_height, style_overrides)
        is_portrait = float(style['PlayResY']) > float(style['PlayResX'])
        usable_width = max(120.0, float(style['PlayResX']) - float(style['MarginL']) - float(style['MarginR']))
        font_size = max(1.0, float(style['FontSize']))
        outline = float(style['Outline'])
        shadow = float(style['Shadow'])
        single_line_limit = int(cls._clamp(round(usable_width / font_size * cls._ASS_LANDSCAPE_SINGLE_LINE_DENSITY), cls._ASS_LANDSCAPE_SINGLE_LINE_LIMIT_MIN, cls._ASS_LANDSCAPE_SINGLE_LINE_LIMIT_MAX))
        if prefer_single_line:
            min_scale = max(cls._ASS_SINGLE_LINE_FONT_SCALE_MIN, float(single_line_min_font_scale or cls._ASS_SINGLE_LINE_FONT_SCALE_MIN))
            single_line = [normalized]
            single_fits, _, _, _ = cls._check_ass_lines_width_safety(single_line, usable_width, font_size, outline, shadow)
            if single_fits: wrapped_lines = single_line
            else:
                scaled_font_size, scaled_outline, scaled_shadow = cls._resolve_single_line_scaled_font(normalized, usable_width, font_size, outline, shadow, min_scale=min_scale)
                if scaled_font_size is not None and scaled_font_size < font_size:
                    wrapped_lines = single_line
                    wrap_meta['font_override'] = int(round(scaled_font_size))
                    ass_text = cls._compose_ass_dialogue_text(wrapped_lines, override_font_size=wrap_meta['font_override'])
                    return (ass_text, wrap_meta) if return_meta else ass_text
                wrapped_lines = cls._build_wrapped_lines_for_ass(normalized, is_portrait=is_portrait, max_line_length=max_line_length, max_lines=max_lines, single_line_limit=single_line_limit, aggressive=False)
        else: wrapped_lines = cls._build_wrapped_lines_for_ass(normalized, is_portrait=is_portrait, max_line_length=max_line_length, max_lines=max_lines, single_line_limit=single_line_limit, aggressive=False)
        fits, _, _, _ = cls._check_ass_lines_width_safety(wrapped_lines, usable_width, font_size, outline, shadow)
        candidate_lines = wrapped_lines
        if not fits:
            hard_wrap_lines, hard_wrap_fits = cls._find_safe_hard_wrap_lines(normalized, max_line_length=max_line_length, max_lines=max_lines, usable_width=usable_width, font_size=font_size, outline=outline, shadow=shadow)
            if hard_wrap_lines:
                candidate_lines = hard_wrap_lines
                wrap_meta['forced_wrap'] = candidate_lines != wrapped_lines
            fits = hard_wrap_fits
        if not fits and is_portrait and max_lines < 5:
            portrait_rescue_lines, portrait_rescue_fits = cls._find_portrait_rescue_lines(normalized, max_line_length=max_line_length, usable_width=usable_width, font_size=font_size, outline=outline, shadow=shadow)
            if portrait_rescue_lines:
                candidate_lines = portrait_rescue_lines
                wrap_meta['forced_wrap'] = True
                fits = portrait_rescue_fits
            elif not portrait_rescue_lines:
                portrait_fallback_lines, portrait_fallback_fits = cls._find_safe_hard_wrap_lines(normalized, max_line_length=max(max_line_length, cls._ASS_HARD_WRAP_MIN_LINE_LENGTH), max_lines=5, usable_width=usable_width, font_size=font_size, outline=outline, shadow=shadow)
                if portrait_fallback_lines:
                    candidate_lines = portrait_fallback_lines
                    wrap_meta['forced_wrap'] = True
                    fits = portrait_fallback_fits
        if not fits and not is_portrait and 1 < max_lines < 4:
            landscape_rescue_lines, landscape_rescue_fits = cls._find_landscape_rescue_lines(normalized, max_line_length=max_line_length, usable_width=usable_width, font_size=font_size, outline=outline, shadow=shadow)
            if landscape_rescue_lines:
                candidate_lines = landscape_rescue_lines
                wrap_meta['forced_wrap'] = True
                fits = landscape_rescue_fits
            elif not landscape_rescue_lines:
                rescue_lines, rescue_fits = cls._find_safe_hard_wrap_lines(normalized, max_line_length=max(max_line_length - 1, cls._ASS_HARD_WRAP_MIN_LINE_LENGTH), max_lines=3, usable_width=usable_width, font_size=font_size, outline=outline, shadow=shadow)
                if rescue_lines:
                    candidate_lines = rescue_lines
                    wrap_meta['forced_wrap'] = True
                    fits = rescue_fits
            if not fits and not landscape_rescue_lines:
                deep_rescue_lines, deep_rescue_fits = cls._find_safe_hard_wrap_lines(normalized, max_line_length=max(max_line_length - 2, cls._ASS_HARD_WRAP_MIN_LINE_LENGTH), max_lines=4, usable_width=usable_width, font_size=font_size, outline=outline, shadow=shadow)
                if deep_rescue_lines:
                    candidate_lines = deep_rescue_lines
                    wrap_meta['forced_wrap'] = True
                    fits = deep_rescue_fits
        override_font_size = None
        if not fits and candidate_lines:
            override_font_size, override_fits = cls._resolve_safe_override_font_size(candidate_lines, usable_width, font_size, outline, shadow)
            if override_font_size is not None:
                wrap_meta['font_override'] = int(round(override_font_size))
                fits = override_fits
        if not fits and candidate_lines: wrap_meta['overflow_warning'] = True
        ass_text = cls._compose_ass_dialogue_text(candidate_lines, override_font_size=override_font_size)
        return (ass_text, wrap_meta) if return_meta else ass_text

    @classmethod
    def _rebalance_split_cue_durations(cls, cues):
        if not cues: return []
        fixed_cues = [dict(cue or {}) for cue in cues]
        minimum_visible = 0.35
        preferred_duration = 0.6
        for idx, cue in enumerate(fixed_cues):
            start = float(cue.get('start', 0.0) or 0.0)
            end = float(cue.get('end', 0.0) or 0.0)
            if end - start >= 0.05: continue
            if idx > 0:
                prev = fixed_cues[idx - 1]
                prev_start = float(prev.get('start', 0.0) or 0.0)
                prev_end = float(prev.get('end', 0.0) or 0.0)
                target_start = max(prev_start + minimum_visible, end - preferred_duration)
                if target_start < end:
                    prev['end'] = target_start
                    cue['start'] = target_start
                    cue['end'] = end
                    continue
            if idx + 1 < len(fixed_cues):
                nxt = fixed_cues[idx + 1]
                next_start = float(nxt.get('start', 0.0) or 0.0)
                next_end = float(nxt.get('end', 0.0) or 0.0)
                target_end = min(next_end - minimum_visible, start + preferred_duration)
                if target_end > start:
                    cue['start'] = start
                    cue['end'] = target_end
                    nxt['start'] = target_end
        return fixed_cues

    @classmethod
    def _parse_subtitle_text_to_cues(cls, subtitle_text, video_width=None, video_height=None):
        from .srt_transform_engine import SrtTransformConfig, SrtTransformEngine
        engine = SrtTransformEngine(SrtTransformConfig(max_line_length=999, max_lines=99, normalize_punctuation=False, filter_filler_words=False))
        cues = engine.parse_srt(subtitle_text or '')
        if not cues: return []
        processed = engine.apply_text_processing(cues)
        return cls._rebalance_split_cue_durations(processed)

    @staticmethod
    def _normalize_font_match_name(font_name): return ' '.join(str(font_name or '').strip().split()).casefold()

    @staticmethod
    def _normalize_font_filename(font_name): return os.path.basename(str(font_name or '').strip()).casefold()

    @classmethod
    def _iter_bundled_font_paths(cls):
        fonts_dir = get_app_subdir('fonts')
        if not os.path.isdir(fonts_dir): return []
        font_paths = []
        for entry in os.listdir(fonts_dir):
            if os.path.splitext(entry)[1].lower() in cls._BUNDLED_FONT_EXTENSIONS: font_paths.append(os.path.join(fonts_dir, entry))
        return sorted(font_paths)

    @classmethod
    def _read_font_display_names(cls, font_path):
        from PIL import ImageFont
        pil_font = ImageFont.truetype(font_path, size=18)
        family_name, style_name = pil_font.getname()
        family_name = str(family_name or '').strip()
        style_name = str(style_name or '').strip()
        candidates = []
        if family_name and style_name: candidates.append(f"{family_name} {style_name}")
        if family_name: candidates.append(family_name)
        return tuple(dict.fromkeys(name for name in candidates if name))

    @classmethod
    def _find_bundled_font_by_name(cls, font_name, task_logger=None):
        normalized_target = cls._normalize_font_match_name(font_name)
        normalized_filename = cls._normalize_font_filename(font_name)
        if not normalized_target: return None
        for font_path in cls._iter_bundled_font_paths():
            if normalized_filename and os.path.basename(font_path).casefold() == normalized_filename:
                try: display_names = cls._read_font_display_names(font_path)
                except Exception: continue
                primary_name = display_names[0] if display_names else os.path.basename(font_path)
                family_name = display_names[1] if len(display_names) > 1 else primary_name
                return {'font_path': font_path, 'font_name': primary_name, 'font_family': family_name}
            try: display_names = cls._read_font_display_names(font_path)
            except Exception: continue
            for display_name in display_names:
                if cls._normalize_font_match_name(display_name) == normalized_target:
                    primary_name = display_names[0] if display_names else str(font_name).strip()
                    family_name = display_names[1] if len(display_names) > 1 else primary_name
                    return {'font_path': font_path, 'font_name': primary_name, 'font_family': family_name}
        return None

    def _resolve_subtitle_font(self, task_logger, temp_fonts_dir):
        config = getattr(self, 'config', {}) or {}
        configured_font_name = str(config.get('SUBTITLE_FONT_NAME') or 'NotoSansCJKsc-Regular.otf').strip()
        if not configured_font_name: configured_font_name = 'NotoSansCJKsc-Regular.otf'
        resolved_font = {'configured_font_name': configured_font_name, 'font_name': configured_font_name, 'font_family': configured_font_name, 'matched_font_name': None, 'font_path': None, 'temp_font_path': None}
        matched_font = self._find_bundled_font_by_name(configured_font_name, task_logger)
        if not matched_font:
            for fallback_path in self._iter_bundled_font_paths():
                try: display_names = self._read_font_display_names(fallback_path)
                except Exception: continue
                primary_name = display_names[0] if display_names else os.path.basename(fallback_path)
                family_name = display_names[1] if len(display_names) > 1 else primary_name
                matched_font = {'font_path': fallback_path, 'font_name': primary_name, 'font_family': family_name}
                break
        if not matched_font: return resolved_font
        resolved_font['font_path'] = matched_font['font_path']
        resolved_font['matched_font_name'] = matched_font['font_name']
        resolved_font['font_family'] = matched_font['font_family']
        try:
            temp_font_path = os.path.join(temp_fonts_dir, os.path.basename(matched_font['font_path']))
            shutil.copy2(matched_font['font_path'], temp_font_path)
            resolved_font['temp_font_path'] = temp_font_path
        except Exception: pass
        return resolved_font

    def _resolve_subtitle_style_overrides(self, task_logger=None):
        try: return parse_style_overrides(getattr(self, 'config', {}) or {})
        except Exception: return None

    @staticmethod
    def _style_overrides_differ_from_defaults(style_overrides) -> bool:
        if not isinstance(style_overrides, dict): return False
        try: defaults = parse_style_overrides({})
        except Exception: return True
        for key, value in style_overrides.items():
            if key not in defaults: return True
            if value != defaults[key]: return True
        return False

    @classmethod
    def _build_default_ass_document(cls, cues, font_family, video_width, video_height, *, prefer_single_line=True, single_line_min_font_scale=None, style_overrides=None):
        style, force_style = cls._build_subtitle_style_description(font_family, video_width, video_height, style_overrides)
        font_name = force_style['FontName']
        ass_header = (
            "[Script Info]\nTitle: Streaming Subtitle\nScriptType: v4.00+\n"
            f"PlayResX: {style['PlayResX']}\nPlayResY: {style['PlayResY']}\n"
            "WrapStyle: 0\nScaledBorderAndShadow: yes\nCollisions: Normal\n\n"
            "[V4+ Styles]\nFormat: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
            "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
            "Alignment, MarginL, MarginR, MarginV, Encoding\n"
            f"Style: Default,{font_name},{force_style['FontSize']},{style['PrimaryColour']},{style['SecondaryColour']},{style['OutlineColour']},{style['BackColour']},"
            f"{style['Bold']},0,0,0,100,100,0,0,{style['BorderStyle']},{force_style['Outline']},{force_style['Shadow']},{force_style['Alignment']},{force_style['MarginL']},{force_style['MarginR']},{force_style['MarginV']},1\n\n"
            "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
        )
        ass_lines = []
        forced_wrap_count = 0
        font_override_count = 0
        overflow_warning_count = 0
        for cue in cues or []:
            cue_dict = cue if isinstance(cue, dict) else {}
            wrapped_result = cls._wrap_subtitle_text_for_ass(cue_dict.get('text', ''), video_width, video_height, return_meta=True, prefer_single_line=prefer_single_line, single_line_min_font_scale=single_line_min_font_scale, style_overrides=style_overrides)
            if isinstance(wrapped_result, tuple): text, wrap_meta = wrapped_result
            else:
                text = wrapped_result
                wrap_meta = {}
            if not text: continue
            if wrap_meta.get('forced_wrap'): forced_wrap_count += 1
            if wrap_meta.get('font_override'): font_override_count += 1
            if wrap_meta.get('overflow_warning'): overflow_warning_count += 1
            ass_lines.append(f"Dialogue: 0,{cls._seconds_to_ass_timestamp(cue_dict.get('start', 0.0))},{cls._seconds_to_ass_timestamp(cue_dict.get('end', 0.0))},Default,,0,0,0,,{text}")
        body = '\n'.join(ass_lines)
        if body: body += '\n'
        return ass_header + body

    def _convert_srt_to_ass(self, subtitle_path, ass_path, task_logger, *, video_width=None, video_height=None, font_family=None, prefer_single_line=True, single_line_min_font_scale=None, style_overrides=None):
        try:
            source_path = subtitle_path
            subtitle_ext = os.path.splitext(subtitle_path)[1].lower()
            if subtitle_ext == '.vtt':
                converted_srt = self._convert_vtt_to_srt(subtitle_path, task_logger)
                if not converted_srt or not os.path.exists(converted_srt): return False
                source_path = converted_srt
            with open(source_path, 'r', encoding='utf-8-sig', errors='replace') as subtitle_file: subtitle_content = subtitle_file.read()
            cues = self._parse_subtitle_text_to_cues(subtitle_content, video_width=video_width, video_height=video_height)
            if not cues: return False
            config = getattr(self, 'config', {}) or {}
            if style_overrides is None: style_overrides = self._resolve_subtitle_style_overrides(task_logger)
            ass_content = self._build_default_ass_document(cues, font_family=font_family, video_width=video_width, video_height=video_height, prefer_single_line=prefer_single_line, single_line_min_font_scale=(single_line_min_font_scale if single_line_min_font_scale is not None else config.get('SUBTITLE_SINGLE_LINE_MIN_FONT_SCALE', self._ASS_SINGLE_LINE_FONT_SCALE_MIN)), style_overrides=style_overrides)
            with open(ass_path, 'w', encoding='utf-8') as ass_file: ass_file.write(ass_content)
            return True
        except Exception: return False

    def _embed_subtitle_in_video(self, task_id, video_path, subtitle_path, task_logger):
        task_before_encoding = get_task(task_id)
        previous_status = task_before_encoding['status'] if task_before_encoding else TASK_STATES['TRANSLATING_SUBTITLE']
        try:
            import os, tempfile, shutil, re, time, threading, queue
            ffmpeg_bin = get_ffmpeg_path(logger=task_logger)
            if not ffmpeg_bin or not os.path.exists(ffmpeg_bin):
                update_task(task_id, upload_progress=None, status=previous_status, silent=True)
                return None
            ffprobe_bin = get_ffprobe_path(ffmpeg_path=ffmpeg_bin, logger=task_logger)
            if not ffprobe_bin:
                update_task(task_id, upload_progress=None, status=previous_status, silent=True)
                return None
            video_dir = os.path.dirname(video_path)
            video_name = os.path.splitext(os.path.basename(video_path))[0]
            embedded_video_path = os.path.join(video_dir, f"{video_name}_with_subtitle.mp4")
            update_task(task_id, status=TASK_STATES['ENCODING_VIDEO'])
            _raise_if_cancelled(task_id, task_logger)
            video_duration = self._get_video_duration(video_path, task_logger)
            stream_info = self._get_video_stream_info(video_path, task_logger)
            input_width = stream_info.get('width') or 0
            input_height = stream_info.get('height') or 0
            input_fps = stream_info.get('fps') or 30
            audio_info = self._get_audio_stream_info(video_path, task_logger)
            gop = max(24, int(round(2 * input_fps)))
            gop_hevc = max(48, int(round(4 * input_fps)))
            subtitle_ext = os.path.splitext(subtitle_path)[1].lower()
            if subtitle_ext not in ('.srt', '.ass', '.ssa', '.vtt'): subtitle_ext = '.srt'
            temp_dir = tempfile.mkdtemp(prefix='subtitle_embed_')
            simple_video = os.path.abspath(video_path)
            simple_output = os.path.join(video_dir, f".{video_name}_with_subtitle.tmp.mp4")
            temp_fonts_dir = os.path.join(temp_dir, "fonts")
            os.makedirs(temp_fonts_dir, exist_ok=True)
            try:
                if os.path.exists(simple_output): os.remove(simple_output)
                style_overrides = self._resolve_subtitle_style_overrides(task_logger)
                subtitle_font = self._resolve_subtitle_font(task_logger, temp_fonts_dir)
                font_family = subtitle_font.get('font_family') or subtitle_font.get('configured_font_name') or 'Noto Sans CJK SC'
                render_subtitle_ext = subtitle_ext
                render_subtitle_name = f"sub{render_subtitle_ext}"
                render_subtitle_path = os.path.join(temp_dir, render_subtitle_name)
                if subtitle_ext in ('.ass', '.ssa'):
                    shutil.copy2(subtitle_path, render_subtitle_path)
                    filter_segments = [f"subtitles={render_subtitle_name}", "fontsdir=fonts", "charenc=UTF-8"]
                    if self._style_overrides_differ_from_defaults(style_overrides):
                        force_style = self._build_subtitle_force_style(font_family, input_width, input_height, style_overrides)
                        if force_style: filter_segments.append(force_style)
                else:
                    render_subtitle_ext = '.ass'
                    render_subtitle_name = "sub.ass"
                    render_subtitle_path = os.path.join(temp_dir, render_subtitle_name)
                    config = getattr(self, 'config', {}) or {}
                    if not self._convert_srt_to_ass(subtitle_path, render_subtitle_path, task_logger, video_width=input_width, video_height=input_height, font_family=font_family, prefer_single_line=config.get('SUBTITLE_PREFER_SINGLE_LINE', True), single_line_min_font_scale=config.get('SUBTITLE_SINGLE_LINE_MIN_FONT_SCALE', self._ASS_SINGLE_LINE_FONT_SCALE_MIN), style_overrides=style_overrides):
                        update_task(task_id, upload_progress=None, status=previous_status, silent=True)
                        return None
                    filter_segments = [f"subtitles={render_subtitle_name}", "fontsdir=fonts", "charenc=UTF-8"]
                vf_filter = ':'.join(filter_segments)
                _hw_encoder_cache = {}
                _hw_encoder_error_cache = {}
                _hw_encoder_probe_meta_cache = {}
                amd_backend_cache = None
                def _is_encoder_listed(encoder_name: str) -> bool:
                    try:
                        list_result = subprocess.run([ffmpeg_bin, '-hide_banner', '-encoders'], capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=10)
                        return list_result.returncode == 0 and encoder_name in f"{list_result.stdout or ''}\n{list_result.stderr or ''}"
                    except Exception: return False
                def _detect_hw_encoder(encoder_name: str) -> bool:
                    if encoder_name in _hw_encoder_cache: return _hw_encoder_cache[encoder_name]
                    try:
                        available, probe_err, probe_meta = self._probe_hw_encoder_availability(ffmpeg_bin=ffmpeg_bin, encoder_name=encoder_name)
                        _hw_encoder_probe_meta_cache[encoder_name] = dict(probe_meta or {})
                        _hw_encoder_cache[encoder_name] = available
                        if not available: _hw_encoder_error_cache[encoder_name] = probe_err or '未知错误'
                        else: _hw_encoder_error_cache[encoder_name] = ''
                        return available
                    except Exception as e:
                        _hw_encoder_cache[encoder_name] = False
                        _hw_encoder_error_cache[encoder_name] = self._short_error_text(str(e))
                        return False
                def _detect_amd_backend() -> str:
                    nonlocal amd_backend_cache
                    if amd_backend_cache is not None: return amd_backend_cache
                    if _detect_hw_encoder('hevc_amf'): amd_backend_cache = 'amf'
                    elif _detect_hw_encoder('hevc_vaapi'): amd_backend_cache = 'vaapi'
                    else: amd_backend_cache = 'none'
                    return amd_backend_cache
                def _detect_nvidia() -> bool: return _detect_hw_encoder('hevc_nvenc')
                def _detect_intel() -> bool: return _detect_hw_encoder('hevc_qsv')
                def _detect_amd() -> bool: return _detect_amd_backend() != 'none'
                def _get_best_encoder() -> str:
                    if _detect_nvidia(): return 'nvidia'
                    if _detect_intel(): return 'intel'
                    if _detect_amd(): return 'amd'
                    return 'cpu'
                custom_video_params = self._parse_custom_video_params(task_logger)
                encoder_settings = parse_encoder_config(getattr(self, 'config', {}) or {})
                cpu_codec = normalize_cpu_codec(encoder_settings.get('cpu_codec'))
                target_quality = recommend_quality(input_height)
                effective_quality = target_quality
                if encoder_settings.get('quality_mode') == 'manual' and encoder_settings.get('quality_value') is not None:
                    effective_quality = float(encoder_settings['quality_value'])
                target_quality_str = format_quality_value(effective_quality)
                color_params = []
                color_map = {}
                try:
                    color_map = normalize_color_metadata(encoder_settings.get('color_metadata_mode'), stream_info)
                    color_params = resolve_color_metadata(encoder_settings.get('color_metadata_mode'), stream_info, logger=task_logger)
                except Exception: pass
                def _video_param_ctx(amd_backend=None, cpu_codec_override=None, hw_quality_boost=None):
                    ctx = {'height': input_height, 'gop': gop, 'gop_hevc': gop_hevc, 'quality_mode': encoder_settings.get('quality_mode'), 'quality_value': encoder_settings.get('quality_value'), 'cpu_preset': encoder_settings.get('cpu_preset'), 'cpu_preset_hd': encoder_settings.get('cpu_preset_hd'), 'duration_s': video_duration, 'hw_quality_boost': (encoder_settings.get('hw_quality_boost') if hw_quality_boost is None else hw_quality_boost), 'hw_quality_level': encoder_settings.get('hw_quality_level'), 'cpu_codec': cpu_codec_override or encoder_settings.get('cpu_codec'), 'software_tune': encoder_settings.get('software_tune'), 'color_map': color_map, 'custom_params': custom_video_params}
                    if amd_backend is not None: ctx['amd_backend'] = amd_backend
                    return ctx
                def build_cpu_params(cpu_codec_override=None, hw_quality_boost=None): return build_encoder_params('cpu', _video_param_ctx(cpu_codec_override=cpu_codec_override, hw_quality_boost=hw_quality_boost))
                def build_nvidia_params(hw_quality_boost=None): return build_encoder_params('nvidia', _video_param_ctx(hw_quality_boost=hw_quality_boost))
                def build_intel_params(hw_quality_boost=None): return build_encoder_params('intel', _video_param_ctx(hw_quality_boost=hw_quality_boost))
                def build_amd_params(hw_quality_boost=None): return build_encoder_params('amd', _video_param_ctx(_detect_amd_backend(), hw_quality_boost=hw_quality_boost))
                def is_vaapi_encoder() -> bool: return actual_encoder == 'amd' and _detect_amd_backend() == 'vaapi'
                encoder_pref = str(self.config.get('VIDEO_ENCODER', 'auto')).lower().strip()
                actual_encoder = encoder_pref
                if encoder_pref == 'auto': actual_encoder = _get_best_encoder()
                elif encoder_pref == 'nvidia':
                    if not _detect_nvidia(): actual_encoder = _get_best_encoder()
                elif encoder_pref == 'intel':
                    if not _detect_intel(): actual_encoder = _get_best_encoder()
                elif encoder_pref == 'amd':
                    if not _detect_amd(): actual_encoder = _get_best_encoder()
                if actual_encoder == 'nvidia': vparams = build_nvidia_params()
                elif actual_encoder == 'intel': vparams = build_intel_params()
                elif actual_encoder == 'amd':
                    if _detect_amd_backend() == 'none':
                        actual_encoder = 'cpu'
                        vparams = build_cpu_params()
                    else: vparams = build_amd_params()
                else: vparams = build_cpu_params()
                aparams = self._build_audio_transcode_params(audio_info)
                main_color_params = list(color_params)
                if is_vaapi_encoder(): final_vf = f"{vf_filter},format=nv12,hwupload"
                else: final_vf = vf_filter
                cmd = self._build_embed_ffmpeg_cmd(ffmpeg_bin=ffmpeg_bin, input_video=simple_video, vf_filter=final_vf, vparams=vparams, aparams=aparams, output_video=simple_output, color_params=main_color_params)
                timeout = self._estimate_embed_timeout(video_duration, cpu_codec if actual_encoder == 'cpu' else None)
                def _execute_embed(embed_cmd, run_timeout, stage_label=''):
                    stage_suffix = f"[{stage_label}] " if stage_label else ''
                    proc = subprocess.Popen(embed_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, cwd=temp_dir, encoding='utf-8', errors='replace')
                    register_active_process(task_id, proc)
                    try:
                        stage_output_queue = queue.Queue()
                        stage_error_queue = queue.Queue()
                        def read_stream(stream, target_queue, stream_name):
                            try:
                                if stream is not None:
                                    for raw_line in stream: target_queue.put(raw_line.strip())
                            except Exception: pass
                            finally: target_queue.put(None)
                        output_thread = threading.Thread(target=read_stream, args=(proc.stdout, stage_output_queue, 'stdout'), daemon=True)
                        error_thread = threading.Thread(target=read_stream, args=(proc.stderr, stage_error_queue, 'stderr'), daemon=True)
                        output_thread.start()
                        error_thread.start()
                        stage_last_time = 0
                        stage_start_time = time.time()
                        stage_last_progress_time = stage_start_time
                        stage_error_messages = []
                        stage_timed_out = False
                        while True:
                            if is_task_cancelled(task_id):
                                proc.terminate()
                                try: proc.wait(timeout=PROCESS_TERMINATE_WAIT_SECONDS)
                                except subprocess.TimeoutExpired:
                                    if proc.poll() is None: proc.kill()
                                raise TaskCancelledError("任务已取消")
                            if time.time() - stage_start_time > run_timeout:
                                stage_timed_out = True
                                proc.terminate()
                                try: proc.wait(timeout=PROCESS_TERMINATE_WAIT_SECONDS)
                                except subprocess.TimeoutExpired:
                                    if proc.poll() is None: proc.kill()
                                break
                            if proc.poll() is not None: break
                            try:
                                line = stage_output_queue.get(timeout=1)
                                if line is None: break
                                if line.startswith('out_time_us='):
                                    try:
                                        time_us = int(line.split('=')[1])
                                        current_time = time_us / 1000000.0
                                        if video_duration and current_time > stage_last_time:
                                            progress = min((current_time / video_duration) * 100, 100)
                                            update_task(task_id, upload_progress=f"{progress:.1f}%", silent=True)
                                            stage_last_time = current_time
                                            stage_last_progress_time = time.time()
                                    except (ValueError, IndexError): continue
                            except queue.Empty: continue
                            try:
                                error_line = stage_error_queue.get_nowait()
                                if error_line:
                                    stage_error_messages.append(error_line)
                                    if len(stage_error_messages) > 50: stage_error_messages.pop(0)
                            except queue.Empty: pass
                        try: proc.wait(timeout=30)
                        except subprocess.TimeoutExpired:
                            proc.kill()
                        for stream_thread in (output_thread, error_thread):
                            try: stream_thread.join(timeout=1.0)
                            except Exception: pass
                        for _ in range(200):
                            try:
                                drained = stage_error_queue.get_nowait()
                            except queue.Empty: break
                            if drained:
                                stage_error_messages.append(drained)
                                if len(stage_error_messages) > 50: stage_error_messages.pop(0)
                        return proc.returncode, '\n'.join(stage_error_messages), os.path.exists(simple_output), stage_timed_out
                    finally: unregister_active_process(task_id, proc)
                embed_started_at = time.monotonic()
                first_stage_budget = max(300.0, float(timeout))
                process_returncode, error_output_full, output_ready, first_timed_out = _execute_embed(cmd, first_stage_budget)
                if process_returncode == 0 and output_ready:
                    self._finalize_embedded_video_output(simple_output, embedded_video_path)
                    update_task(task_id, upload_progress=None, status=previous_status, silent=True)
                    return embedded_video_path
                hw_error_detected = self._is_known_hw_encoder_error(error_output_full)
                hw_option_error = self._is_hw_option_error(error_output_full)
                retry_stages = self._resolve_embed_retry_stages(actual_encoder, encoder_settings.get('hw_quality_boost'), hw_error_detected, hw_option_error, cpu_codec)
                if first_timed_out: retry_stages = self._drop_same_encoder_stage_after_timeout(retry_stages)
                def _stage_budget(stage): return self._embed_stage_budget(video_duration, cpu_codec, stage, first_stage_budget)
                overall_deadline = embed_started_at + first_stage_budget + sum(_stage_budget(stage) for stage in retry_stages)
                for stage in retry_stages:
                    if is_task_cancelled(task_id): raise TaskCancelledError("任务已取消")
                    remaining_s = overall_deadline - time.monotonic()
                    if remaining_s <= 0: break
                    if stage == 'hw_no_boost':
                        stage_label = f"{actual_encoder} 关闭质量增强"
                        stage_vparams = build_nvidia_params(hw_quality_boost=False) if actual_encoder == 'nvidia' else (build_intel_params(hw_quality_boost=False) if actual_encoder == 'intel' else (build_amd_params(hw_quality_boost=False) if actual_encoder == 'amd' else build_cpu_params(hw_quality_boost=False)))
                        stage_filter = final_vf
                        stage_color_params = list(color_params)
                    elif stage == 'cpu_x264':
                        stage_label = 'CPU 软编码 (libx265 回退至 libx264)'
                        stage_vparams = build_cpu_params(cpu_codec_override='x264')
                        stage_filter = vf_filter
                        stage_color_params = list(color_params)
                    else:
                        stage_label = 'CPU 软编码'
                        stage_vparams = build_cpu_params()
                        stage_filter = vf_filter
                        stage_color_params = list(color_params)
                    stage_budget = _stage_budget(stage)
                    stage_timeout = int(max(60.0, min(remaining_s, stage_budget)))
                    cmd_retry = self._build_embed_ffmpeg_cmd(ffmpeg_bin=ffmpeg_bin, input_video=simple_video, vf_filter=stage_filter, vparams=stage_vparams, aparams=aparams, output_video=simple_output, color_params=stage_color_params)
                    retry_returncode, retry_error, retry_ready, _stage_timed_out = _execute_embed(cmd_retry, stage_timeout, stage_label)
                    if retry_returncode == 0 and retry_ready:
                        self._finalize_embedded_video_output(simple_output, embedded_video_path)
                        update_task(task_id, upload_progress=None, status=previous_status, silent=True)
                        return embedded_video_path
                    try:
                        if os.path.exists(simple_output): os.remove(simple_output)
                    except Exception: pass
                update_task(task_id, upload_progress=None, status=previous_status, silent=True)
                return None
            finally:
                try:
                    if simple_output and os.path.exists(simple_output): os.remove(simple_output)
                except Exception: pass
                try: shutil.rmtree(temp_dir)
                except Exception: pass
        except TaskCancelledError:
            update_task(task_id, upload_progress=None, status=previous_status, silent=True)
            raise
        except Exception:
            update_task(task_id, upload_progress=None, status=previous_status, silent=True)
            return None

    def _get_video_duration(self, video_path, task_logger):
        try:
            ffmpeg_bin = get_ffmpeg_path(logger=task_logger)
            ffprobe_bin = get_ffprobe_path(ffmpeg_path=ffmpeg_bin, logger=task_logger)
            cmd = [ffprobe_bin, '-v', 'quiet', '-print_format', 'json', '-show_format', video_path]
            result = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=60)
            if result.returncode == 0:
                data = json.loads(result.stdout)
                return float(data['format']['duration'])
            return None
        except Exception: return None

    def _get_video_stream_info(self, video_path, task_logger):
        info: dict[str, float | int | str | None] = {"width": None, "height": None, "fps": None, "pix_fmt": None, "codec_name": None, "bit_rate": None, "color_space": None, "color_primaries": None, "color_transfer": None, "color_range": None}
        try:
            ffmpeg_bin = get_ffmpeg_path(logger=task_logger)
            ffprobe_bin = get_ffprobe_path(ffmpeg_path=ffmpeg_bin, logger=task_logger)
            cmd = [ffprobe_bin, '-v', 'quiet', '-print_format', 'json', '-select_streams', 'v:0', '-show_streams', video_path]
            result = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=30)
            if result.returncode == 0:
                data = json.loads(result.stdout)
                streams = data.get('streams', [])
                if streams:
                    s = streams[0]
                    info['width'] = s.get('width')
                    info['height'] = s.get('height')
                    info['pix_fmt'] = s.get('pix_fmt')
                    info['codec_name'] = s.get('codec_name')
                    info['color_space'] = s.get('color_space')
                    info['color_primaries'] = s.get('color_primaries')
                    info['color_transfer'] = s.get('color_transfer')
                    info['color_range'] = s.get('color_range')
                    bit_rate = s.get('bit_rate')
                    if bit_rate:
                        try: info['bit_rate'] = int(bit_rate)
                        except Exception: pass
                    r = s.get('avg_frame_rate') or s.get('r_frame_rate')
                    if r and r != '0/0':
                        try:
                            num, den = r.split('/')
                            fps = float(num) / float(den) if float(den) != 0 else 0
                            if fps > 1: info['fps'] = fps  # type: ignore
                        except Exception: pass
        except Exception: pass
        return info

    def _get_audio_stream_info(self, video_path, task_logger):
        info: dict[str, int | str | None] = {"codec_name": None, "bit_rate": None, "sample_rate": None, "channels": None, "sample_fmt": None}
        try:
            ffmpeg_bin = get_ffmpeg_path(logger=task_logger)
            ffprobe_bin = get_ffprobe_path(ffmpeg_path=ffmpeg_bin, logger=task_logger)
            cmd = [ffprobe_bin, '-v', 'quiet', '-print_format', 'json', '-select_streams', 'a:0', '-show_streams', video_path]
            result = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=30)
            if result.returncode == 0:
                data = json.loads(result.stdout)
                streams = data.get('streams', [])
                if streams:
                    s = streams[0]
                    info['codec_name'] = s.get('codec_name')
                    bit_rate = s.get('bit_rate')
                    if bit_rate:
                        try: info['bit_rate'] = int(bit_rate)
                        except Exception: pass
                    sr = s.get('sample_rate')
                    if sr:
                        try: info['sample_rate'] = int(sr)
                        except Exception: pass
                    info['channels'] = s.get('channels')
                    info['sample_fmt'] = s.get('sample_fmt')
        except Exception: pass
        return info

    def _generate_tags(self, task_id, task_logger):
        from modules.ai_enhancer import generate_acfun_tags
        task = get_task(task_id)
        if not task: return
        update_task(task_id, status=TASK_STATES['TAGGING'])
        from modules.utils import safe_str
        title = safe_str(task.get('video_title_translated') or task.get('video_title_original'))
        description = safe_str(task.get('description_translated') or task.get('description_original'))
        openai_config = {'OPENAI_API_KEY': self.config.get('OPENAI_API_KEY', ''), 'OPENAI_BASE_URL': self.config.get('OPENAI_BASE_URL', ''), 'OPENAI_MODEL_NAME': self.config.get('OPENAI_MODEL_NAME', 'gpt-3.5-turbo'), 'OPENAI_THINKING_ENABLED': self.config.get('OPENAI_THINKING_ENABLED', False), 'FALLBACK_OPENAI_API_KEY': self.config.get('FALLBACK_OPENAI_API_KEY', ''), 'FALLBACK_OPENAI_BASE_URL': self.config.get('FALLBACK_OPENAI_BASE_URL', ''), 'FALLBACK_OPENAI_MODEL_NAME': self.config.get('FALLBACK_OPENAI_MODEL_NAME', ''), 'FIXED_PARTITION_ID': self.config.get('FIXED_PARTITION_ID', '')}
        preset_tags = parse_preset_tags(self.config.get('PRESET_TAGS', ''))
        preset_effective = is_preset_tags_effective(self.config)
        if not preset_effective:
            if self.config.get('GENERATE_TAGS', True) and (title or description):
                tags = generate_acfun_tags(title, description, openai_config=openai_config, task_id=task_id)
                if tags: update_task(task_id, tags_generated=json.dumps(tags[:6], ensure_ascii=False))
                else: update_task(task_id, tags_generated=json.dumps([], ensure_ascii=False))
            else: update_task(task_id, tags_generated=json.dumps([], ensure_ascii=False))
            return True
        existing_tags = _normalize_tags_list(task.get('tags_generated'))
        ai_tags = []
        if self.config.get('GENERATE_TAGS', True) and (title or description):
            raw_tags = generate_acfun_tags(title, description, openai_config=openai_config, task_id=task_id, avoid_tags=preset_tags) or []
            ai_tags = [str(tag).strip() for tag in raw_tags if str(tag or '').strip()]
        merged_tags = merge_tags(preset_tags, existing_tags + ai_tags, limit=BILIBILI_TAG_LIMIT)
        update_task(task_id, tags_generated=json.dumps(merged_tags, ensure_ascii=False))
        return True

    def _recommend_partition(self, task_id, task_logger):
        from modules.ai_enhancer import recommend_acfun_partition, recommend_bilibili_partition, recommend_partitions_aio
        task = get_task(task_id)
        if not task: return
        update_task(task_id, status=TASK_STATES['PARTITIONING'])
        title = task.get('video_title_translated', '') or task.get('video_title_original', '')
        description = task.get('description_translated', '') or task.get('description_original', '')
        title_original = task.get('video_title_original', '') or ''
        description_original = task.get('description_original', '') or ''
        title_translated = task.get('video_title_translated', '') or ''
        description_translated = task.get('description_translated', '') or ''
        tags_generated = _normalize_tags_list(task.get('tags_generated'))
        upload_target = _get_task_upload_target(task)
        openai_config = {'OPENAI_API_KEY': self.config.get('OPENAI_API_KEY', ''), 'OPENAI_BASE_URL': self.config.get('OPENAI_BASE_URL', ''), 'OPENAI_MODEL_NAME': self.config.get('OPENAI_MODEL_NAME', 'gpt-3.5-turbo'), 'OPENAI_THINKING_ENABLED': self.config.get('OPENAI_THINKING_ENABLED', False), 'OPENAI_TIMEOUT_SECONDS': self.config.get('OPENAI_TIMEOUT_SECONDS', 600), 'FALLBACK_OPENAI_API_KEY': self.config.get('FALLBACK_OPENAI_API_KEY', ''), 'FALLBACK_OPENAI_BASE_URL': self.config.get('FALLBACK_OPENAI_BASE_URL', ''), 'FALLBACK_OPENAI_MODEL_NAME': self.config.get('FALLBACK_OPENAI_MODEL_NAME', ''), 'FIXED_PARTITION_ID': self.config.get('FIXED_PARTITION_ID', ''), 'FIXED_PARTITION_ID_BILIBILI': self.config.get('FIXED_PARTITION_ID_BILIBILI', ''), 'RECOMMEND_PARTITION_WITH_COVER': self.config.get('RECOMMEND_PARTITION_WITH_COVER', False)}
        cover_path = task.get('cover_path_local', '')
        if not self.config.get('RECOMMEND_PARTITION', False) or not (title or description): return True
        targets_to_recommend = []
        if upload_target == UPLOAD_TARGET_BOTH: targets_to_recommend = [UPLOAD_TARGET_ACFUN, UPLOAD_TARGET_BILIBILI]
        elif upload_target == UPLOAD_TARGET_BILIBILI: targets_to_recommend = [UPLOAD_TARGET_BILIBILI]
        else: targets_to_recommend = [UPLOAD_TARGET_ACFUN]
        platform_results = {}
        zone_data = []
        id_mapping_data = []
        if UPLOAD_TARGET_BILIBILI in targets_to_recommend:
            try:
                from .bilibili_zones import get_zone_list_sub
                zone_data = get_zone_list_sub()
            except Exception: pass
        if UPLOAD_TARGET_ACFUN in targets_to_recommend:
            from .utils import get_app_subdir
            id_mapping_path = os.path.join(get_app_subdir('acfunid'), 'id_mapping.json')
            try:
                if os.path.exists(id_mapping_path):
                    with open(id_mapping_path, 'r', encoding='utf-8') as f: id_mapping_data = json.load(f)
            except Exception: id_mapping_data = []
        if targets_to_recommend == [UPLOAD_TARGET_ACFUN, UPLOAD_TARGET_BILIBILI]:
            platform_results = recommend_partitions_aio(title, description, acfun_id_mapping_data=id_mapping_data, bilibili_zone_data=zone_data, title_original=title_original, description_original=description_original, title_translated=title_translated, description_translated=description_translated, tags=tags_generated, openai_config=openai_config, task_id=task_id, cover_path=cover_path, include_cover_for_ai=self.config.get('RECOMMEND_PARTITION_WITH_COVER', False))
        else:
            for platform in targets_to_recommend:
                partition_selection = None
                if platform == UPLOAD_TARGET_BILIBILI:
                    if not zone_data:
                        platform_results[platform] = {}
                        continue
                    partition_selection = recommend_bilibili_partition(title, description, zone_data, title_original=title_original, description_original=description_original, title_translated=title_translated, description_translated=description_translated, tags=tags_generated, openai_config=openai_config, task_id=task_id, cover_path=cover_path, include_cover_for_ai=self.config.get('RECOMMEND_PARTITION_WITH_COVER', False))
                else:
                    if not id_mapping_data:
                        platform_results[platform] = {}
                        continue
                    partition_selection = recommend_acfun_partition(title, description, id_mapping_data, title_original=title_original, description_original=description_original, title_translated=title_translated, description_translated=description_translated, tags=tags_generated, openai_config=openai_config, task_id=task_id, cover_path=cover_path, include_cover_for_ai=self.config.get('RECOMMEND_PARTITION_WITH_COVER', False))
                platform_results[platform] = partition_selection or {}
        for platform in targets_to_recommend:
            partition_selection = platform_results.get(platform) or {}
            recommended_partition_id = str(partition_selection.get('id') or '').strip()
            if recommended_partition_id:
                selected_field = _get_partition_field_name(platform, 'selected')
                recommended_field = _get_partition_field_name(platform, 'recommended')
                latest_task = get_task(task_id) or {}
                updates: Dict[str, Any] = {recommended_field: recommended_partition_id}
                if not str(latest_task.get(selected_field) or '').strip(): updates[selected_field] = recommended_partition_id
                update_task(task_id, **updates)
        return True

    def _moderate_content(self, task_id, task_logger):
        from modules.content_moderator import AlibabaCloudModerator
        task = get_task(task_id)
        if not task: return
        update_task(task_id, status=TASK_STATES['MODERATING'])
        title = task.get('video_title_translated', '') or task.get('video_title_original', '')
        description = task.get('description_translated', '') or task.get('description_original', '')
        tags_list = []
        if task.get('tags_generated'):
            try: tags_list = json.loads(task.get('tags_generated', '[]'))
            except json.JSONDecodeError: pass
        url_pattern = r'https?://(?:[-\w.]|(?:%[\da-fA-F]{2}))+'
        email_pattern = r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b'
        filtered_title = re.sub(url_pattern, '', title)
        filtered_title = re.sub(email_pattern, '', filtered_title)
        from modules.utils import safe_str
        filtered_description = re.sub(url_pattern, '', safe_str(description))
        filtered_description = re.sub(email_pattern, '', filtered_description)
        filtered_description = re.sub(r'\\n{3,}', '\\n\\n', filtered_description)
        aliyun_config = {'ALIYUN_ACCESS_KEY_ID': self.config.get('ALIYUN_ACCESS_KEY_ID', ''), 'ALIYUN_ACCESS_KEY_SECRET': self.config.get('ALIYUN_ACCESS_KEY_SECRET', ''), 'ALIYUN_CONTENT_MODERATION_REGION': self.config.get('ALIYUN_CONTENT_MODERATION_REGION', 'cn-shanghai')}
        text_moderation_service = self.config.get('ALIYUN_TEXT_MODERATION_SERVICE', 'comment_detection')
        moderator = AlibabaCloudModerator(aliyun_config, task_id)
        title_result = moderator.moderate_text(filtered_title, service_type=text_moderation_service)
        description_result = moderator.moderate_text(filtered_description, service_type=text_moderation_service)
        tags_for_moderation_string = ""
        if tags_list: tags_for_moderation_string = "，".join(tags_list)
        tags_moderation_result = {"pass": True, "details": [{"label": "skipped", "suggestion": "pass", "reason": "没有生成标签或标签为空"}]}
        if tags_for_moderation_string: tags_moderation_result = moderator.moderate_text(tags_for_moderation_string, service_type=text_moderation_service)
        cover_result = {"pass": True, "details": [{"label": "skipped", "suggestion": "pass", "reason": "封面审核已禁用"}]}
        moderation_result = {"title": title_result, "description": description_result, "tags": tags_moderation_result, "cover": cover_result, "overall_pass": title_result.get("pass", True) and description_result.get("pass", True) and tags_moderation_result.get("pass", True)}
        update_task(task_id, moderation_result=json.dumps(moderation_result, ensure_ascii=False))
        if not moderation_result["overall_pass"]:
            update_task(task_id, status=TASK_STATES['AWAITING_REVIEW'], error_category=CONTENT_MODERATION_ERROR_CATEGORY)

    def _get_embedded_video_candidate(self, video_path: str, subtitle_paths=None) -> str:
        def _is_fresh(candidate: str) -> bool:
            if not candidate or not os.path.exists(candidate): return False
            try: candidate_mtime = os.path.getmtime(candidate)
            except OSError: return False
            for subtitle_path in (subtitle_paths or []):
                if not subtitle_path or not os.path.exists(subtitle_path): continue
                try:
                    if os.path.getmtime(subtitle_path) > candidate_mtime + 1e-6: return False
                except OSError: continue
            return True
        if not video_path: return ''
        if os.path.exists(video_path):
            base_name_no_ext = os.path.splitext(os.path.basename(video_path))[0]
            if base_name_no_ext.endswith('_with_subtitle') and _is_fresh(video_path): return video_path
        video_dir = os.path.dirname(video_path)
        video_name = os.path.splitext(os.path.basename(video_path))[0]
        candidate = os.path.join(video_dir, f"{video_name}_with_subtitle.mp4")
        return candidate if _is_fresh(candidate) else ''

    def _resolve_existing_subtitle_assets(self, task_id: str, task: dict, task_dir: str):
        def _existing_path(value):
            path_value = str(value or '').strip()
            return path_value if path_value and os.path.exists(path_value) else ''
        subtitle_path_original = _existing_path(task.get('subtitle_path_original'))
        subtitle_path_translated = _existing_path(task.get('subtitle_path_translated'))
        subtitle_language = str(task.get('subtitle_language_detected') or '').strip().lower()
        if not subtitle_path_translated:
            translated_candidate = os.path.join(task_dir, f"translated_{task_id}.srt")
            if os.path.exists(translated_candidate): subtitle_path_translated = translated_candidate
        if not subtitle_path_original:
            for ext in ('.srt', '.vtt'):
                asr_candidate = os.path.join(task_dir, f"asr_{task_id}{ext}")
                if os.path.exists(asr_candidate):
                    subtitle_path_original = asr_candidate
                    break
        subtitle_candidates = []
        try:
            for name in os.listdir(task_dir):
                lower = name.lower()
                if lower.endswith('.srt') or lower.endswith('.vtt'): subtitle_candidates.append(os.path.join(task_dir, name))
        except Exception: subtitle_candidates = []
        subtitle_candidates = sorted(set(subtitle_candidates))
        for candidate in subtitle_candidates:
            lower_name = os.path.basename(candidate).lower()
            if lower_name.startswith('translated_'):
                if not subtitle_path_translated: subtitle_path_translated = candidate
                continue
            if not subtitle_path_original: subtitle_path_original = candidate
        return subtitle_path_original, subtitle_path_translated, subtitle_language

    def _prepare_subtitle_for_upload(self, task_id, task_logger):
        task = get_task(task_id)
        if not task: return None
        video_path = task.get('video_path_local', '')
        if not video_path or not os.path.exists(video_path): return get_task(task_id)
        try:
            _raise_if_cancelled(task_id, task_logger)
            task_dir = os.path.dirname(video_path)
            embed_quality_state = str(task.get('subtitle_quality_state') or '').strip().lower() or 'ok'
            qc_failed = task.get('subtitle_qc_failed') == 1
            embed_blocked = not _subtitle_embed_allowed(self.config, embed_quality_state, qc_failed, qc_reason=task.get('subtitle_qc_reason'), task_logger=task_logger)
            config_embed_enabled = _as_bool(self.config.get('SUBTITLE_EMBED_IN_VIDEO', True))
            embed_allowed = config_embed_enabled and not embed_blocked
            reusable_embedded_video = self._get_embedded_video_candidate(video_path, subtitle_paths=_candidate_subtitle_paths(task, task_dir))
            if reusable_embedded_video and not embed_blocked:
                if reusable_embedded_video != video_path: update_task(task_id, video_path_local=reusable_embedded_video)
                return get_task(task_id)
            should_embed_subtitle = embed_allowed
            translation_enabled = _as_bool(self.config.get('SUBTITLE_TRANSLATION_ENABLED', False))
            subtitle_path_original, subtitle_path_translated, subtitle_language = self._resolve_existing_subtitle_assets(task_id, task, task_dir)
            if subtitle_path_original and not subtitle_language: subtitle_language = self._detect_subtitle_language(subtitle_path_original)
            reusable_updates = {}
            if subtitle_path_original and task.get('subtitle_path_original') != subtitle_path_original: reusable_updates['subtitle_path_original'] = subtitle_path_original
            if subtitle_path_translated and task.get('subtitle_path_translated') != subtitle_path_translated: reusable_updates['subtitle_path_translated'] = subtitle_path_translated
            if subtitle_language and task.get('subtitle_language_detected') != subtitle_language: reusable_updates['subtitle_language_detected'] = subtitle_language
            if reusable_updates:
                update_task(task_id, **reusable_updates)
                task = get_task(task_id) or task
            if subtitle_path_translated and os.path.exists(subtitle_path_translated):
                if not should_embed_subtitle: return get_task(task_id)
                recorded_original = str(task.get('subtitle_path_original') or '')
                if subtitle_path_original and os.path.exists(subtitle_path_original):
                    qc_target = subtitle_path_original
                    qc_force_asr = None
                else:
                    qc_target = subtitle_path_translated
                    qc_force_asr = _is_asr_subtitle_artifact(task_id, recorded_original)
                if not self._ensure_asr_subtitle_qc(task_id, qc_target, task_logger, embed_quality_state, force_asr=qc_force_asr)[0]: return get_task(task_id)
                embedded_video_path = self._embed_subtitle_in_video(task_id, video_path, subtitle_path_translated, task_logger)
                if embedded_video_path:
                    update_task(task_id, video_path_local=embedded_video_path, subtitle_path_original=subtitle_path_original, subtitle_path_translated=subtitle_path_translated, subtitle_language_detected=subtitle_language, subtitle_warning_message=None)
                else:
                    update_task(task_id, subtitle_warning_message='subtitle_embed_failed')
                return get_task(task_id)
            if subtitle_path_original and (str(subtitle_language or '').startswith('zh') or not translation_enabled):
                if not should_embed_subtitle: return get_task(task_id)
                if not self._ensure_asr_subtitle_qc(task_id, subtitle_path_original, task_logger, embed_quality_state)[0]: return get_task(task_id)
                embedded_video_path = self._embed_subtitle_in_video(task_id, video_path, subtitle_path_original, task_logger)
                if embedded_video_path:
                    update_task(task_id, video_path_local=embedded_video_path, subtitle_path_original=subtitle_path_original, subtitle_path_translated=None, subtitle_language_detected=subtitle_language or 'zh', subtitle_warning_message=None)
                else:
                    update_task(task_id, subtitle_warning_message='subtitle_embed_failed')
                return get_task(task_id)
            if not translation_enabled and (subtitle_path_original or subtitle_path_translated): return get_task(task_id)
            subtitle_files = []
            try:
                for name in os.listdir(task_dir):
                    lower = name.lower()
                    if lower.endswith('.srt') or lower.endswith('.vtt'): subtitle_files.append(os.path.join(task_dir, name))
            except Exception: pass
            if _is_asr_enabled(self.config):
                if translation_enabled: self._translate_subtitle(task_id, task_logger)
                else:
                    if not subtitle_files:
                        try:
                            from modules.speech_recognition import create_speech_recognizer_from_config
                            recognizer = create_speech_recognizer_from_config(self.config, task_id)
                        except Exception: recognizer = None
                        if recognizer:
                            _t2 = get_task(task_id)
                            prev_status2 = _t2['status'] if _t2 else TASK_STATES['UPLOADING']
                            update_task(task_id, status=TASK_STATES['ASR_TRANSCRIBING'])
                            asr_ext = '.srt' if str(self.config.get('SPEECH_RECOGNITION_OUTPUT_FORMAT', 'srt')).lower() == 'srt' else '.vtt'
                            asr_subtitle_path = os.path.join(task_dir, f"asr_{task_id}{asr_ext}")
                            _raise_if_cancelled(task_id, task_logger)
                            should_embed_subtitle = config_embed_enabled
                            update_task(task_id, asr_warning_message=None, subtitle_qc_failed=0, subtitle_qc_reason=None, subtitle_qc_score=None, subtitle_qc_checked_at=None, subtitle_warning_message=None, subtitle_quality_state=None)
                            out_path = recognizer.transcribe_video_to_subtitles(video_path, asr_subtitle_path)
                            update_task(task_id, status=prev_status2)
                            quality_state = str(getattr(recognizer, 'last_quality_state', '') or 'ok').strip().lower() or 'ok'
                            if out_path and os.path.exists(out_path):
                                detected_lang = self._detect_subtitle_language(out_path)
                                update_task(task_id, asr_warning_message=(getattr(recognizer, 'last_warning_message', '') or None), subtitle_quality_state=quality_state, subtitle_path_original=out_path, subtitle_path_translated=None, subtitle_language_detected=detected_lang)
                                if quality_state == 'failed' and _as_bool(self.config.get('ASR_FAILURE_BLOCKS_EMBED', True)):
                                    self._quarantine_rejected_subtitle(out_path, task_logger)
                                    update_task(task_id, subtitle_warning_message='asr_failed_block_embed')
                                elif should_embed_subtitle:
                                    qc_result = self._run_subtitle_qc(task_id, out_path, task_logger, strict=(quality_state == 'degraded'))
                                    if _qc_result_allows_embed(qc_result, self.config):
                                        embedded_video_path = self._embed_subtitle_in_video(task_id, video_path, out_path, task_logger)
                                        if embedded_video_path:
                                            update_task(task_id, video_path_local=embedded_video_path, subtitle_path_original=out_path, subtitle_path_translated=None, subtitle_language_detected=detected_lang, subtitle_warning_message=None)
                                        else:
                                            update_task(task_id, subtitle_warning_message='subtitle_embed_failed')
                                    else:
                                        reason_text = 'qc_unavailable' if qc_result is None else 'subtitle_qc_rejected'
                                        self._quarantine_rejected_subtitle(out_path, task_logger)
                                        update_task(task_id, subtitle_warning_message=reason_text)
                            else:
                                update_task(task_id, asr_warning_message=(getattr(recognizer, 'last_warning_message', '') or None), subtitle_quality_state='failed')
                                self._mark_subtitle_issue(task_id, 'asr_no_subtitle')
        except TaskCancelledError: raise
        except Exception as e: task_logger.error(f"上传前字幕处理出现异常: {e}")
        return get_task(task_id)

    def _upload_to_target(self, task_id, task_logger, allow_missing_translations=False):
        """上传入口：整条上传链（前置准备 + 双平台投稿）占用 1 个「上传任务」配额。

        配额上限 = 设置 → 运维与安全 → 资源控制 → 最大并发上传数，
        由 UploadSlotLimiter 每次获取前实时重读，改设置即时生效。

        排队等待配额前先把状态置为「准备上传」，让任务列表立刻可见
        （否则点击「开始」后界面长时间毫无反应）；拿到配额真正开始上传后
        由 _do_upload_to_* 置为「上传中」。
        """
        label = f"任务 {str(task_id)[:8]}"
        cancel_check = lambda: is_task_cancelled(task_id)
        acquired = upload_slots.acquire(label, cancel_check=cancel_check, blocking=False)
        if not acquired:
            # 没有空闲配额：先落库为「准备上传 + 排队等待」，界面立刻可见，
            # 且进程重启后能凭这个标记把队列续上（内存里的等待线程重启就没了）。
            try:
                current = get_task(task_id)
                if current and current.get('status') != TASK_STATES['UPLOADING']:
                    update_task(task_id, status=TASK_STATES['READY_FOR_UPLOAD'], upload_progress=QUEUE_WAITING_MARK + '…', silent=True)
                    task_logger.info("任务已加入上传队列：状态置为「准备上传」，等待上传配额后自动开始上传")
            except Exception as exc:
                task_logger.debug(f"标记排队状态失败（忽略）: {exc}")
            upload_slots.acquire(label, cancel_check=cancel_check)
        try:
            # 停止任务时上传调用可能无法立刻中断，交给看门狗在宽限期后强制交还配额
            threading.Thread(
                target=_watch_stopped_upload,
                args=(task_id, threading.get_ident()),
                name=f"upload_watch_{str(task_id)[:8]}",
                daemon=True
            ).start()
            return self._upload_to_target_with_slot(task_id, task_logger, allow_missing_translations)
        finally:
            upload_slots.release()
            # 配额已释放：立刻把队列里下一个任务接上（完成/终止/失败都要接）
            try:
                pump_upload_queue('上传结束释放配额')
            except Exception:
                pass

    def _upload_to_target_with_slot(self, task_id, task_logger, allow_missing_translations=False):
        task = get_task(task_id)
        if not task: return
        if not self._ensure_required_translations_ready(task_id, task, task_logger, allow_missing_translations=allow_missing_translations): return
        if allow_missing_translations:
            task = self._ensure_force_upload_metadata_ready(task_id, task_logger)
            if not task: return
        upload_target = _get_task_upload_target(task)
        pending_platforms = _get_pending_upload_platforms(task, upload_target)
        if not pending_platforms:
            if task.get('status') != TASK_STATES['COMPLETED']: update_task(task_id, status=TASK_STATES['COMPLETED'], error_message=None, upload_progress=None)
            return
        if upload_target == UPLOAD_TARGET_BOTH:
            subtitle_prepared_in_this_round = False
            if UPLOAD_TARGET_ACFUN in pending_platforms:
                self._upload_to_acfun(task_id, task_logger, subtitle_prepared=False)
                subtitle_prepared_in_this_round = True
                task = get_task(task_id)
                if not task or task.get('status') == TASK_STATES['FAILED']: return
            if UPLOAD_TARGET_BILIBILI in pending_platforms: self._upload_to_bilibili(task_id, task_logger, subtitle_prepared=subtitle_prepared_in_this_round)
            return
        if upload_target == UPLOAD_TARGET_BILIBILI:
            if UPLOAD_TARGET_BILIBILI in pending_platforms: self._upload_to_bilibili(task_id, task_logger)
            return
        if UPLOAD_TARGET_ACFUN in pending_platforms: self._upload_to_acfun(task_id, task_logger)

    def _ensure_force_upload_metadata_ready(self, task_id, task_logger):
        task = get_task(task_id)
        if not task: return None
        from modules.utils import safe_str
        completed_stages = _get_completed_stages(task, getattr(self, 'config', None))
        def _has_usable_text(current_task):
            return bool(safe_str(current_task.get('video_title_translated') or current_task.get('video_title_original')) or safe_str(current_task.get('description_translated') or current_task.get('description_original')))
        tags = _normalize_tags_list(task.get('tags_generated'))
        preset_effective = is_preset_tags_effective(self.config)
        if (self.config.get('GENERATE_TAGS', True) or preset_effective) and not tags:
            if _has_usable_text(task):
                self._generate_tags(task_id, task_logger)
                task = get_task(task_id) or task
                completed_stages = _mark_stage_done(task_id, completed_stages, PIPELINE_STAGE_GENERATE_TAGS)
            elif preset_effective:
                preset_only = parse_preset_tags(self.config.get('PRESET_TAGS', ''))
                update_task(task_id, tags_generated=json.dumps(preset_only, ensure_ascii=False), silent=True)
                task = get_task(task_id) or task
                completed_stages = _mark_stage_done(task_id, completed_stages, PIPELINE_STAGE_GENERATE_TAGS)
            else:
                update_task(task_id, tags_generated=json.dumps([], ensure_ascii=False), silent=True)
                task = get_task(task_id) or task
                completed_stages = _mark_stage_done(task_id, completed_stages, PIPELINE_STAGE_GENERATE_TAGS)
        if self.config.get('RECOMMEND_PARTITION', False):
            task = get_task(task_id) or task
            upload_target = _get_task_upload_target(task)
            missing_partition_platforms = [platform for platform in _get_upload_platforms_for_target(upload_target) if not _get_task_partition_id(task, platform, prefer_selected=True)]
            if missing_partition_platforms and _has_usable_text(task):
                self._recommend_partition(task_id, task_logger)
                task = get_task(task_id) or task
                completed_stages = _mark_stage_done(task_id, completed_stages, PIPELINE_STAGE_RECOMMEND_PARTITION)
        if self.config.get('CONTENT_MODERATION_ENABLED', False):
            task = get_task(task_id) or task
            if not task.get('moderation_result'):
                self._moderate_content(task_id, task_logger)
                task = get_task(task_id) or task
                if task.get('status') == TASK_STATES['AWAITING_REVIEW']: return None
                completed_stages = _mark_stage_done(task_id, completed_stages, PIPELINE_STAGE_MODERATE_CONTENT)
        return get_task(task_id) or task

    def _upload_to_bilibili(self, task_id, task_logger, subtitle_prepared=False):
        """平台上传（并发配额已在 _upload_to_target 统一占用，这里不再重复加锁）。"""
        task = get_task(task_id)
        if not task: return
        try:
            self._do_upload_to_bilibili(task_id, task_logger, subtitle_prepared=subtitle_prepared)
        except TaskCancelledError: raise
        except Exception as e:
            update_task(task_id, status=TASK_STATES['FAILED'], error_message=f"上传异常: {str(e)}")

    def _upload_to_acfun(self, task_id, task_logger, subtitle_prepared=False):
        """平台上传（并发配额已在 _upload_to_target 统一占用，这里不再重复加锁）。"""
        task = get_task(task_id)
        if not task: return
        try:
            self._do_upload_to_acfun(task_id, task_logger, subtitle_prepared=subtitle_prepared)
        except TaskCancelledError: raise
        except Exception as e:
            update_task(task_id, status=TASK_STATES['FAILED'], error_message=f"上传异常: {str(e)}")

    def _do_upload_to_acfun(self, task_id, task_logger, subtitle_prepared=False):
        from modules.acfun_uploader import AcfunUploader
        task = get_task(task_id)
        if not task: return
        update_task(task_id, status=TASK_STATES['UPLOADING'])
        video_path = task.get('video_path_local', '') if task else ''
        cover_path = task.get('cover_path_local', '') if task else ''
        title = (task.get('video_title_translated', '') or task.get('video_title_original', '')) if task else ''
        description = (task.get('description_translated', '') or task.get('description_original', '')) if task else ''
        partition_id = _get_task_partition_id(task, UPLOAD_TARGET_ACFUN, prefer_selected=True) if task else ''
        fixed_acfun_pid = str(self.config.get('FIXED_PARTITION_ID', '') or '').strip()
        if fixed_acfun_pid: partition_id = fixed_acfun_pid
        if not video_path or not os.path.exists(video_path):
            youtube_url = task.get('youtube_url', '') if task else ''
            if not youtube_url:
                update_task(task_id, status=TASK_STATES['FAILED'], error_message="无法获取YouTube URL，无法下载视频")
                return
            if not self._download_video_file(task_id, youtube_url, task_logger):
                # 下载失败时状态与真实原因已由 _download_video_file 写入，这里直接结束，
                # 避免被后面的「上传参数不完整，缺少 video_path」覆盖掉真正的原因。
                return
            task = get_task(task_id)
            video_path = task.get('video_path_local', '') if task else ''
            cover_path = task.get('cover_path_local', '') if task else ''
        cover_path = self._ensure_upload_cover(task_id, task, task_logger)
        if not subtitle_prepared:
            task = self._prepare_subtitle_for_upload(task_id, task_logger) or task
            video_path = task.get('video_path_local', '') if task else video_path
        update_task(task_id, status=TASK_STATES['UPLOADING'])
        tags = resolve_upload_tags(self.config, task.get('tags_generated') if task else None, PLATFORM_ACFUN, logger_obj=task_logger)
        metadata_path = task.get('metadata_json_path_local', '') if task else ''
        original_url = ''
        original_uploader = ''
        original_upload_date = ''
        if metadata_path and os.path.exists(metadata_path):
            try:
                with open(metadata_path, 'r', encoding='utf-8') as f:
                    metadata = json.load(f)
                    original_url = metadata.get('webpage_url', '')
                    original_uploader = metadata.get('uploader', '')
                    original_upload_date = metadata.get('upload_date', '')
            except Exception: pass
        if self.config.get('YOUTUBE_UPLOADER_AS_FIRST_TAG', False):
            from modules.utils import safe_str
            uploader_tag = safe_str(original_uploader).strip()
            if uploader_tag:
                normalized_uploader = uploader_tag.lower()
                cleaned_tags = [safe_str(tag).strip() for tag in tags if safe_str(tag).strip() and safe_str(tag).strip().lower() != normalized_uploader]
                upload_tags = [uploader_tag] + cleaned_tags
                tags = upload_tags[:6]
                try:
                    persist_base = resolve_upload_tags(self.config, task.get('tags_generated') if task else None, PLATFORM_BILIBILI)
                    persisted_tags = [uploader_tag] + [safe_str(tag).strip() for tag in persist_base if safe_str(tag).strip() and safe_str(tag).strip().lower() != normalized_uploader]
                    update_task(task_id, tags_generated=json.dumps(persisted_tags[:BILIBILI_TAG_LIMIT], ensure_ascii=False))
                except Exception: pass
        acfun_cookies_path = resolve_cookie_file_path(path_value=self.config.get('ACFUN_COOKIES_PATH', 'cookies/ac_cookies.json'), default_relative_path='cookies/ac_cookies.json', service_name='AcFun', logger_obj=task_logger, allow_json_txt_fallback=True)
        cover_mode = self.config.get('COVER_PROCESSING_MODE', 'crop')
        missing_params = []
        if not video_path or not os.path.exists(video_path): missing_params.append("video_path")
        if not cover_path or not os.path.isfile(cover_path): missing_params.append("cover_path")
        if not title: missing_params.append("title")
        if not partition_id: missing_params.append("partition_id")
        if missing_params:
            update_task(task_id, status=TASK_STATES['FAILED'], error_message=f"上传参数不完整，缺少: {', '.join(missing_params)}")
            return
        if os.path.exists(acfun_cookies_path):
            is_valid, error_msg = validate_cookies(acfun_cookies_path, "AcFun")
            if not is_valid:
                update_task(task_id, status=TASK_STATES['FAILED'], error_message=f"AcFun Cookies无效，请重新登录: {error_msg}")
                return
        try:
            uploader = AcfunUploader(cookie_file=acfun_cookies_path)
            cancel_event = get_task_cancel_event(task_id)
            success, result = uploader.upload_video(video_file_path=video_path, cover_file_path=cover_path, title=title, description=description, tags=tags, partition_id=partition_id, original_url=original_url, original_uploader=original_uploader, original_upload_date=original_upload_date, upload_append_repost_notice=bool(self.config.get('UPLOAD_APPEND_REPOST_NOTICE', True)), task_id=task_id, cover_mode=cover_mode, cancel_event=cancel_event)
            if success:
                update_task(task_id, status=TASK_STATES['COMPLETED'], acfun_upload_response=json.dumps(result, ensure_ascii=False))
                try:
                    if self.config.get('DELETE_DOWNLOAD_FILES_AFTER_UPLOAD', False) and _get_task_upload_target(task) == UPLOAD_TARGET_ACFUN: delete_task_files(task_id)
                except Exception: pass
            else:
                update_task(task_id, status=TASK_STATES['FAILED'], error_message=f"上传失败: {result}")
        except TaskCancelledError: raise
        except Exception as e:
            update_task(task_id, status=TASK_STATES['FAILED'], error_message=f"上传异常: {str(e)}")

    def _do_upload_to_bilibili(self, task_id, task_logger, subtitle_prepared=False):
        from modules.bilibili_uploader import BilibiliUploader
        task = get_task(task_id)
        if not task: return
        update_task(task_id, status=TASK_STATES['UPLOADING'], upload_progress='0.0%')
        video_path = task.get('video_path_local', '') if task else ''
        cover_path = task.get('cover_path_local', '') if task else ''
        title = (task.get('video_title_translated', '') or task.get('video_title_original', '')) if task else ''
        description = (task.get('description_translated', '') or task.get('description_original', '')) if task else ''
        upload_target = _get_task_upload_target(task)
        effective_limits = _get_effective_metadata_limits(upload_target)
        partition_id = ''
        if not video_path or not os.path.exists(video_path):
            youtube_url = task.get('youtube_url', '') if task else ''
            if not youtube_url:
                update_task(task_id, status=TASK_STATES['FAILED'], error_message="无法获取YouTube URL，无法下载视频", upload_progress=None)
                return
            if not self._download_video_file(task_id, youtube_url, task_logger):
                # 下载失败时状态与真实原因已由 _download_video_file 写入，这里直接结束，
                # 避免被后面的「上传参数不完整，缺少 video_path」覆盖掉真正的原因。
                return
            task = get_task(task_id)
            video_path = task.get('video_path_local', '') if task else ''
            cover_path = task.get('cover_path_local', '') if task else ''
        cover_path = self._ensure_upload_cover(task_id, task, task_logger)
        if not subtitle_prepared:
            task = self._prepare_subtitle_for_upload(task_id, task_logger) or task
            video_path = task.get('video_path_local', '') if task else video_path
        update_task(task_id, status=TASK_STATES['UPLOADING'], upload_progress='0.0%')
        tags = resolve_upload_tags(self.config, task.get('tags_generated') if task else None, PLATFORM_BILIBILI, logger_obj=task_logger)
        metadata_path = task.get('metadata_json_path_local', '') if task else ''
        original_url = task.get('youtube_url', '') if task else ''
        original_uploader = ''
        original_upload_date = ''
        if metadata_path and os.path.exists(metadata_path):
            try:
                with open(metadata_path, 'r', encoding='utf-8') as f:
                    metadata = json.load(f)
                    original_url = metadata.get('webpage_url', original_url)
                    original_uploader = metadata.get('uploader', '')
                    original_upload_date = metadata.get('upload_date', '')
            except Exception: pass
        try:
            from modules.bilibili_uploader import format_bilibili_description
            description = format_bilibili_description(base_desc=description, original_url=original_url, original_uploader=original_uploader, original_upload_date=original_upload_date, append_repost_notice=bool(self.config.get('UPLOAD_APPEND_REPOST_NOTICE', True)), max_len=effective_limits['description_limit'])
        except Exception: pass
        if self.config.get('YOUTUBE_UPLOADER_AS_FIRST_TAG', False):
            from modules.utils import safe_str
            uploader_tag = safe_str(original_uploader).strip()
            if uploader_tag:
                normalized_uploader = uploader_tag.lower()
                cleaned_tags = [safe_str(tag).strip() for tag in tags if safe_str(tag).strip() and safe_str(tag).strip().lower() != normalized_uploader]
                tags = [uploader_tag] + cleaned_tags
                if len(tags) > 12: tags = tags[:12]
                try: update_task(task_id, tags_generated=json.dumps(tags, ensure_ascii=False))
                except Exception: pass
        bilibili_cookies_path = resolve_cookie_file_path(path_value=self.config.get('BILIBILI_COOKIES_PATH', 'cookies/bili_cookies.json'), default_relative_path='cookies/bili_cookies.json', service_name='Bilibili', logger_obj=task_logger, allow_json_txt_fallback=False)
        task_partition_id = _get_task_partition_id(task, UPLOAD_TARGET_BILIBILI, prefer_selected=True) if task else ''
        fixed_bili_pid = str(self.config.get('FIXED_PARTITION_ID_BILIBILI', '') or '').strip()
        partition_id = fixed_bili_pid or task_partition_id
        try:
            from .bilibili_zones import collect_valid_tids
            valid_tids = collect_valid_tids()
            if partition_id and str(partition_id) not in valid_tids: partition_id = ''
            if not partition_id and task_partition_id and str(task_partition_id) in valid_tids: partition_id = str(task_partition_id)
        except Exception: pass
        missing_params = []
        if not video_path or not os.path.exists(video_path): missing_params.append("video_path")
        if not cover_path or not os.path.isfile(cover_path): missing_params.append("cover_path")
        if not title: missing_params.append("title")
        if not partition_id: missing_params.append("partition_id")
        if not bilibili_cookies_path: missing_params.append("BILIBILI_COOKIES_PATH")
        if missing_params:
            update_task(task_id, status=TASK_STATES['FAILED'], error_message=f"上传参数不完整，缺少: {', '.join(missing_params)}", upload_progress=None)
            return
        if not os.path.exists(bilibili_cookies_path):
            update_task(task_id, status=TASK_STATES['FAILED'], error_message=f"Bilibili Cookies文件不存在: {bilibili_cookies_path}", upload_progress=None)
            return
        is_valid, error_msg = validate_cookies(bilibili_cookies_path, "Bilibili")
        if not is_valid:
            update_task(task_id, status=TASK_STATES['FAILED'], error_message=f"Bilibili Cookies无效: {error_msg}", upload_progress=None)
            return
        try:
            uploader = BilibiliUploader(cookie_file=bilibili_cookies_path)
            last_progress_text = '0.0%'
            def _on_progress(progress_text):
                if is_task_cancelled(task_id):
                    return  # 任务已停止：僵尸上传的进度不再刷到界面上
                nonlocal last_progress_text
                text = str(progress_text or '').strip()
                if not text: return
                match = re.search(r'(\d+(?:\.\d+)?)\s*%', text)
                if match:
                    percent = float(match.group(1))
                    normalized_text = f"{max(0.0, min(100.0, percent)):.1f}%"
                else:
                    try:
                        numeric = float(text)
                        if 0.0 <= numeric <= 1.0: numeric *= 100.0
                        normalized_text = f"{max(0.0, min(100.0, numeric)):.1f}%"
                    except Exception: return
                if normalized_text == last_progress_text: return
                last_progress_text = normalized_text
                update_task(task_id, upload_progress=normalized_text, silent=True)
            success, result = uploader.upload_video(video_file_path=video_path, cover_file_path=cover_path, title=title, description=description, tags=tags, partition_id=partition_id, youtube_url=original_url, task_id=task_id, progress_callback=_on_progress, title_limit=effective_limits['title_limit'], description_limit=effective_limits['description_limit'])
            if success:
                if is_task_cancelled(task_id):
                    task_logger.warning("任务已被停止，但平台侧上传已完成：保持停止状态，不再标记为已完成")
                    return
                update_task(task_id, status=TASK_STATES['COMPLETED'], bilibili_upload_response=json.dumps(result, ensure_ascii=False), upload_progress=None)
                try:
                    if self.config.get('DELETE_DOWNLOAD_FILES_AFTER_UPLOAD', False) and _get_task_upload_target(task) != UPLOAD_TARGET_ACFUN: delete_task_files(task_id)
                except Exception: pass
            else:
                update_task(task_id, status=TASK_STATES['FAILED'], error_message=f"上传失败: {result}", upload_progress=None)
        except TaskCancelledError: raise
        except Exception as e:
            update_task(task_id, status=TASK_STATES['FAILED'], error_message=f"上传异常: {str(e)}", upload_progress=None)

def start_task(task_id, config=None):
    clear_task_cancel(task_id)
    task = get_task(task_id)
    if not task: return False
    if task['status'] not in [TASK_STATES['PENDING'], TASK_STATES['AWAITING_REVIEW'], TASK_STATES['FAILED']]: return False
    if not config:
        try:
            from flask import current_app
            if 'Y2A_SETTINGS' in current_app.config: config = current_app.config['Y2A_SETTINGS']
        except (ImportError, RuntimeError): config = {}
    processor = get_global_task_processor(config)
    job_id = processor.schedule_task(task_id)
    return job_id is not None

def force_upload_task(task_id, config=None):
    clear_task_cancel(task_id)
    # 每天第一次上传前：自动激活失败任务（当天只做一次；内部记账可防止重试链路递归）
    try:
        activate_failed_tasks_for_today_once(config)
    except Exception:
        pass
    task = get_task(task_id)
    if not task: return False
    allowed_states = [TASK_STATES['AWAITING_REVIEW'], TASK_STATES['COMPLETED'], TASK_STATES['PENDING'], TASK_STATES['READY_FOR_UPLOAD']]
    if task['status'] not in allowed_states: return False
    if not config:
        try:
            from flask import current_app
            if 'Y2A_SETTINGS' in current_app.config: config = current_app.config['Y2A_SETTINGS']
        except (ImportError, RuntimeError): config = {}
    processor = get_global_task_processor(config)
    task_logger = setup_task_logger(task_id)
    try:
        _raise_if_cancelled(task_id, task_logger)
        processor._upload_to_target(task_id, task_logger, allow_missing_translations=True)
        try:
            import threading, time
            def delayed_check():
                time.sleep(1)
                processor._check_and_start_next_pending_task()
            threading.Thread(target=delayed_check, daemon=True).start()
        except Exception: pass
        return True
    except TaskCancelledError:
        return False
    except Exception as e:
        update_task(task_id, status=TASK_STATES['FAILED'], error_message=f"强制上传失败: {str(e)}")
        return False

_global_task_processor = None

def get_global_task_processor(config=None):
    global _global_task_processor
    if _global_task_processor is None: _global_task_processor = TaskProcessor(config)
    elif config: _global_task_processor.refresh_config(config)
    return _global_task_processor

def shutdown_global_task_processor():
    global _global_task_processor
    if _global_task_processor:
        _global_task_processor.shutdown()
        _global_task_processor = None

init_db()