"""
evidence.py: 3개 collector 출력을 evidence_bundle JSON으로 합치고
source_signals 테이블에 저장한다.
"""
from __future__ import annotations

from typing import Any

from central import db


def build_bundle(
    market: str,
    target_week: str,
    target_date: str | None,
    calendar_signals: list[dict],
    naver_signals: list[dict],
    pinterest_signals: list[dict],
) -> dict[str, Any]:
    """
    Gemini 입력용 evidence bundle JSON.
    {
      "market": "KR",
      "target_week": "2026-W29",
      "target_date": "2026-07-13",
      "signals": {
        "calendar": [...],
        "naver_datalab": [...],
        "pinterest": [...]
      }
    }
    """
    return {
        "market": market,
        "target_week": target_week,
        "target_date": target_date,
        "signals": {
            "calendar": calendar_signals,
            "naver_datalab": naver_signals,
            "pinterest": pinterest_signals,
        },
    }


def save_signals(
    run_id: str,
    market: str,
    target_week: str,
    calendar_signals: list[dict],
    naver_signals: list[dict],
    pinterest_signals: list[dict],
) -> None:
    """수집된 원신호를 source_signals 테이블에 저장."""
    supabase = db.get_client()
    rows = [
        {
            "run_id": run_id,
            "source": "calendar",
            "market": market,
            "target_week": target_week,
            "raw": {"items": calendar_signals},
        },
        {
            "run_id": run_id,
            "source": "naver_datalab",
            "market": market,
            "target_week": target_week,
            "raw": {"items": naver_signals},
        },
        {
            "run_id": run_id,
            "source": "pinterest",
            "market": market,
            "target_week": target_week,
            "raw": {"items": pinterest_signals},
        },
    ]
    supabase.table("source_signals").insert(rows).execute()
