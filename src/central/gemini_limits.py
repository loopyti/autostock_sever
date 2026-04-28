"""
Gemini API RPM/RPD 공유 한도 (grounding 체인 vs fast 단일 모델).
grounding.py / conditions.py(레거시) / gemini_idea.py / slots.py에서 공통 사용.
"""
from __future__ import annotations

import logging
import time
from datetime import date
from threading import Lock

from central.settings import (
    GEMINI_FAST_MODEL,
    GEMINI_FAST_RPM,
    GEMINI_FAST_RPD,
    GEMINI_GROUNDING_MODELS,
)

logger = logging.getLogger(__name__)


class RPDExhausted(RuntimeError):
    """모델 RPD 소진 — 다음 grounding 모델로 전환 또는 상위에서 처리."""


# ── grounding (모델별 RPM/RPD) ───────────────────────────────────────────────

_grounding_rpm_lock = Lock()
_grounding_rpm_calls: dict[str, list[float]] = {}

_grounding_rpd_lock = Lock()
_grounding_rpd_counts: dict[str, int] = {}
_grounding_rpd_date: str = ""


def _reset_grounding_rpd_if_new_day() -> None:
    global _grounding_rpd_date, _grounding_rpd_counts
    today = date.today().isoformat()
    if _grounding_rpd_date != today:
        _grounding_rpd_date = today
        _grounding_rpd_counts = {}


def acquire_grounding_slot(model: str, rpm: int, rpd: int) -> None:
    now = time.time()
    with _grounding_rpm_lock:
        calls = _grounding_rpm_calls.setdefault(model, [])
        cutoff = now - 60.0
        calls[:] = [t for t in calls if t > cutoff]
        if len(calls) >= rpm:
            sleep_sec = 60.0 - (now - calls[0]) + 1.0
            logger.info("grounding RPM 한도(%d) [%s] — %.1f초 대기", rpm, model, sleep_sec)
            time.sleep(sleep_sec)
        calls.append(time.time())

    with _grounding_rpd_lock:
        _reset_grounding_rpd_if_new_day()
        count = _grounding_rpd_counts.get(model, 0)
        if count >= rpd:
            raise RPDExhausted(f"{model} RPD 한도({rpd}) 소진")
        _grounding_rpd_counts[model] = count + 1


def grounding_model_chain() -> list[dict]:
    return list(GEMINI_GROUNDING_MODELS)


# ── fast (단일 모델) ──────────────────────────────────────────────────────────

_fast_rpm_lock = Lock()
_fast_rpm_calls: list[float] = []

_fast_rpd_lock = Lock()
_fast_rpd_count: int = 0
_fast_rpd_date: str = ""


def _reset_fast_rpd_if_new_day() -> None:
    global _fast_rpd_date, _fast_rpd_count
    today = date.today().isoformat()
    if _fast_rpd_date != today:
        _fast_rpd_date = today
        _fast_rpd_count = 0


def acquire_fast_slot() -> None:
    """GEMINI_FAST_MODEL 단일 풀 RPM/RPD."""
    global _fast_rpd_count, _fast_rpd_date
    model = GEMINI_FAST_MODEL
    rpm = GEMINI_FAST_RPM
    rpd = GEMINI_FAST_RPD
    now = time.time()

    with _fast_rpm_lock:
        cutoff = now - 60.0
        _fast_rpm_calls[:] = [t for t in _fast_rpm_calls if t > cutoff]
        if len(_fast_rpm_calls) >= rpm:
            sleep_sec = 60.0 - (now - _fast_rpm_calls[0]) + 1.0
            logger.info("fast RPM 한도(%d) [%s] — %.1f초 대기", rpm, model, sleep_sec)
            time.sleep(sleep_sec)
        _fast_rpm_calls.append(time.time())

    with _fast_rpd_lock:
        _reset_fast_rpd_if_new_day()
        if _fast_rpd_count >= rpd:
            raise RPDExhausted(f"{model} fast RPD 한도({rpd}) 소진")
        _fast_rpd_count += 1


def fast_model_name() -> str:
    return GEMINI_FAST_MODEL
