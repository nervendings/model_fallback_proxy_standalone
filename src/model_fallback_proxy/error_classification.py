"""模型容量与远程 compact 瞬时故障判定。"""
from __future__ import annotations

import re
from typing import Any


CAPACITY_MARKERS = (
    "selected model is at capacity",
    "model is at capacity",
    "server_overloaded",
    "server overloaded",
    "our servers are currently overloaded",
    "an error occurred while processing your request",
    "upstream service temporarily unavailable",
    "no available accounts",
    "capacity exceeded",
    "capacity_exceeded",
)
REMOTE_COMPACT_TRANSIENT_MARKERS = (
    "bad gateway",
    "upstream request failed",
    "upstream service temporarily unavailable",
    "stream disconnected before completion",
    "temporarily unavailable",
)
REMOTE_COMPACT_TRANSIENT_STATUS_RE = re.compile(r"(?:unexpected\s+status\s+)?(?:429|502|503|529)\b")
# 会话历史损坏类错误：重试、换模型或“继续”都无法恢复，必须人工处理。
# 典型：某条历史 item 的 encrypted_content 不是可解密内容（被明文/损坏内容占据），
# 服务端每次 hydrate 该 item 都失败，导致此后每个回合都以同一错误失败（实测 77 连败）。
CORRUPTED_HISTORY_MARKERS = (
    "could not be verified",
    "hydration failed",
    "could not be decrypted or parsed",
    "encrypted content hydration",
)


def normalizeErrorText(raw_value: Any) -> str:
    """扁平化结构化错误，并收敛终端换行和多余空白。"""
    if raw_value is None:
        return ""
    if isinstance(raw_value, bytes):
        text_value = raw_value.decode("utf-8", "ignore")
    elif isinstance(raw_value, dict):
        text_value = " ".join(
            normalizeErrorText(item)
            for key, value in raw_value.items()
            for item in (key, value)
        )
    elif isinstance(raw_value, (list, tuple, set)):
        text_value = " ".join(normalizeErrorText(item) for item in raw_value)
    else:
        text_value = str(raw_value)
    return " ".join(text_value.casefold().split())


def isCapacityText(raw_value: Any) -> bool:
    normalized = normalizeErrorText(raw_value)
    return any(marker in normalized for marker in CAPACITY_MARKERS)


def isRemoteCompactTransientError(raw_value: Any) -> bool:
    """识别远程 compact 的可恢复上游故障，避免只依赖单一 502 正文。"""
    normalized = normalizeErrorText(raw_value)
    if "remote compact task" not in normalized:
        return False
    return bool(
        REMOTE_COMPACT_TRANSIENT_STATUS_RE.search(normalized)
        or any(marker in normalized for marker in REMOTE_COMPACT_TRANSIENT_MARKERS)
    )


def getRetryableModelFailureKind(raw_value: Any) -> str:
    if isCapacityText(raw_value):
        return "capacity"
    if isRemoteCompactTransientError(raw_value):
        return "remote_compact_transient"
    return ""


def isCorruptedHistoryError(raw_value: Any) -> bool:
    """历史条目密文损坏，重试无意义；调用方应停止自动续跑并提示人工接管。"""
    normalized = normalizeErrorText(raw_value)
    if not normalized:
        return False
    # 只匹配密文水合失败的明确特征；通用 400002（input.type/tool.type 等）可能可恢复，不能误伤。
    return any(marker in normalized for marker in CORRUPTED_HISTORY_MARKERS)
