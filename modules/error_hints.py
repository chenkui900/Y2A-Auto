# -*- coding: utf-8 -*-
"""
错误信息友好化：把底层原始错误映射为「原因 + 建议操作」。

供 Jinja 过滤器 friendly_error 与前端 JS 使用：
  - Jinja 侧： {{ task.error_message|friendly_error }}
  - JS 侧：   拷贝 ERROR_HINT_PATTERNS 的正则逻辑（见 templates/tasks.html）
"""

import re

# (匹配正则, 简短原因, 建议操作)
ERROR_HINT_PATTERNS = [
    (
        r"137022|投稿过于频繁",
        "B站投稿频率限制",
        "B站对投稿间隔有限制：建议等待 10~30 分钟后再重试；"
        "新账号或低等级账号每日投稿配额较低，可隔天发布。系统已将上传改为串行以降低触发概率。",
    ),
    (
        r"curl:?\s*\(60\)|SSL certificate|certificate has expired",
        "网络证书校验失败",
        "通常是代理/VPN 对 HTTPS 做了拦截，或系统时间不对。"
        "建议：关闭代理后重试；检查系统时间是否准确；或更换网络环境。",
    ),
    (
        r"curl:?\s*\(92\)|HTTP/2 stream|PROTOCOL_ERROR",
        "网络连接不稳定",
        "网络传输中断，系统会自动重试。若反复出现，请检查网络/代理稳定性。",
    ),
    (
        r"curl:?\s*\(28\)|timed? ?out|Timeout",
        "网络请求超时",
        "上传或下载超时，系统会自动指数退避重试。若持续失败，请检查网络带宽与代理。",
    ),
    (
        r"登录|未登录|login|cookie.{0,6}(失效|过期|invalid)|not logged in",
        "平台登录凭据失效",
        "请到「设置」页面对对应平台重新扫码登录，或通过 CookieCloud 同步新的 Cookie。",
    ),
    (
        r"分区|partition.{0,10}(invalid|illegal|错误)|不合法",
        "投稿分区不合法",
        "所选分区与平台规则不匹配。请到「设置」页调整默认分区，或在人工审核页修改后重新上传。",
    ),
    (
        r"标题.{0,6}(长|超过)|title.{0,10}(too long|exceed)",
        "标题超长",
        "平台对标题长度有限制，请缩短标题（或精简AI翻译结果）后重试。",
    ),
    (
        r"风控|审核不通过|违规|敏感",
        "内容未通过平台审核",
        "平台判定内容不适合发布。请检查标题/简介/标签是否含敏感词，或更换视频后重试。",
    ),
    (
        r"磁盘|空间不足|No space left",
        "磁盘空间不足",
        "请清理 D 盘（下载目录/临时目录）空间后重试；也可在「设置」开启下载文件自动清理。",
    ),
    (
        r"用户主动停止",
        "用户手动停止",
        "任务已被手动停止，可在任务列表重新启动。",
    ),
]


def friendly_error(message):
    """把原始错误信息转换为「原因 + 建议」。

    返回拼接后的字符串；无匹配时原样返回。
    """
    if not message:
        return message
    text = str(message)
    for pattern, reason, advice in ERROR_HINT_PATTERNS:
        if re.search(pattern, text, re.IGNORECASE):
            # 避免重复拼接
            if advice[:12] in text:
                return text
            return f"{text}\n💡 {reason}：{advice}"
    return text
