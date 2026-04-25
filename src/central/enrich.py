"""
enrich.py: 서버 계산 보강 — date_weight, weight, dedupe_key.
Gemini를 호출하지 않는다.
"""
from __future__ import annotations

import math
from datetime import date, timedelta
from typing import Any


def _parse_date(s: str | None) -> date | None:
    if not s:
        return None
    try:
        return date.fromisoformat(s)
    except ValueError:
        return None


def _calc_date_weight(
    target_date: date | None,
    event_date: date | None,
    lead_start_days: int = 7,
    lead_end_days: int = 30,
) -> float:
    """
    target_date와 event_date 거리 + lead 윈도우 기반 0~1 가중치.
    - 윈도우 안이면 1.0 → 거리가 멀어질수록 감쇠
    - event_date 없으면 0.5 (neutral)
    """
    if target_date is None or event_date is None:
        return 0.5

    days_until = (event_date - target_date).days

    if -lead_end_days <= days_until <= lead_start_days:
        return 1.0

    outside = max(0, abs(days_until) - max(lead_start_days, lead_end_days))
    decay = math.exp(-outside / 14.0)
    return round(max(0.0, min(1.0, decay)), 3)


def _calc_weight(
    date_weight: float,
    used_evidence: list[dict],
    trend: str | None = None,
) -> float:
    """
    weight = date_weight × demand_strength
    demand_strength: used_evidence 항목 수 + naver rising 보정
    """
    ev_count = len(used_evidence) if used_evidence else 0
    ev_score = min(1.0, ev_count / 3.0)

    trend_bonus = 0.1 if trend == "rising" else 0.0

    strength = min(1.0, ev_score + trend_bonus)
    return round(date_weight * 0.6 + strength * 0.4, 3)


def enrich(
    ideas: list[dict],
    target_date_str: str | None,
) -> list[dict]:
    """
    idea card 목록에 date_weight, weight 계산 결과를 주입한다.
    """
    target_date = _parse_date(target_date_str)

    for idea in ideas:
        event_date = _parse_date(idea.get("event_date"))
        lead_start = int(idea.get("lead_start_days") or 7)
        lead_end = int(idea.get("lead_end_days") or 30)

        dw = _calc_date_weight(target_date, event_date, lead_start, lead_end)
        idea["date_weight"] = dw

        used_ev = idea.get("used_evidence") or []
        trend = None
        for ev in used_ev:
            if ev.get("source") == "naver_datalab" and ev.get("trend") == "rising":
                trend = "rising"
                break

        idea["weight"] = _calc_weight(dw, used_ev, trend)

    return ideas
