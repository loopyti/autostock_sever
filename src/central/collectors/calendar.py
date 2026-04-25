"""
calendar collector: data.go.kr 공휴일 API + seasons_kr.yaml 시즌 사전.
target_week 주변 이벤트 리스트를 반환한다.
"""
from __future__ import annotations

import re
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import httpx
import yaml

from central.settings import DATA_GO_KR_API_KEY

# src/central/collectors/calendar.py → parents[3] = 프로젝트 루트
_SEASONS_PATH = Path(__file__).resolve().parents[3] / "data" / "seasons_kr.yaml"

_PUBLIC_HOLIDAYS_KR: dict[str, str] = {
    "01-01": "신정",
    "03-01": "삼일절",
    "05-05": "어린이날",
    "06-06": "현충일",
    "08-15": "광복절",
    "10-03": "개천절",
    "10-09": "한글날",
    "12-25": "성탄절",
}


def _iso_week_to_dates(target_week: str) -> tuple[date, date]:
    """'2026-W29' → (월요일, 일요일)."""
    match = re.fullmatch(r"(\d{4})-W(\d{2})", target_week)
    if not match:
        raise ValueError(f"잘못된 target_week 형식: {target_week!r}. 예시: '2026-W29'")
    year, week = int(match.group(1)), int(match.group(2))
    monday = date.fromisocalendar(year, week, 1)
    sunday = monday + timedelta(days=6)
    return monday, sunday


def _load_seasons() -> dict:
    if not _SEASONS_PATH.exists():
        return {}
    with open(_SEASONS_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _fetch_datagokr_holidays(year: int) -> list[dict]:
    """data.go.kr 특일정보 API로 해당 연도 공휴일 가져오기."""
    if not DATA_GO_KR_API_KEY:
        return []
    url = "http://apis.data.go.kr/B090041/openapi/service/SpcdeInfoService/getRestDeInfo"
    params = {
        "serviceKey": DATA_GO_KR_API_KEY,
        "solYear": year,
        "numOfRows": 100,
        "_type": "json",
    }
    try:
        resp = httpx.get(url, params=params, timeout=10)
        resp.raise_for_status()
        items = (
            resp.json()
            .get("response", {})
            .get("body", {})
            .get("items", {})
            .get("item", [])
        )
        if isinstance(items, dict):
            items = [items]
        return [
            {"date": str(item["locdate"]), "name": item["dateName"]}
            for item in items
            if isinstance(item, dict)
        ]
    except Exception:
        return []


def collect(target_week: str, market: str = "KR") -> list[dict[str, Any]]:
    """
    Returns:
        list of {
            "type": "holiday" | "season" | "shopping" | "campaign" | "life_event" | "b2b",
            "name": str,
            "date": "YYYY-MM-DD" | None,
            "keywords": list[str],
            "month": int,
            "week": int,
        }
    """
    monday, sunday = _iso_week_to_dates(target_week)
    year = monday.year
    month = monday.month
    week_num = monday.isocalendar()[1]

    results: list[dict] = []

    holidays = _fetch_datagokr_holidays(year)
    holiday_set: set[str] = {h["date"] for h in holidays}

    for delta in range(7):
        d = monday + timedelta(days=delta)
        mmdd = d.strftime("%m-%d")
        date_str = d.isoformat()
        if date_str in {h["date"] for h in holidays if h["date"] == date_str}:
            name = next(h["name"] for h in holidays if h["date"] == date_str)
            results.append({
                "type": "holiday",
                "name": name,
                "date": date_str,
                "keywords": [name],
                "month": d.month,
                "week": week_num,
            })
        elif mmdd in _PUBLIC_HOLIDAYS_KR and date_str not in holiday_set:
            name = _PUBLIC_HOLIDAYS_KR[mmdd]
            results.append({
                "type": "holiday",
                "name": name,
                "date": date_str,
                "keywords": [name],
                "month": d.month,
                "week": week_num,
            })

    seasons_data = _load_seasons()

    for season_name, season in (seasons_data.get("seasons") or {}).items():
        if month in (season.get("months") or []):
            results.append({
                "type": "season",
                "name": season_name,
                "date": None,
                "keywords": season.get("keywords", []),
                "month": month,
                "week": week_num,
            })

    for cat_key, cat_list in seasons_data.items():
        if cat_key == "seasons" or not isinstance(cat_list, list):
            continue
        event_type = cat_key
        for item in cat_list:
            if not isinstance(item, dict):
                continue
            if month in (item.get("months") or []):
                results.append({
                    "type": event_type,
                    "name": item.get("keyword", ""),
                    "date": None,
                    "keywords": [item.get("keyword", "")],
                    "month": month,
                    "week": week_num,
                })

    return results
