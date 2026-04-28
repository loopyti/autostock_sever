"""
naver_datalab collector: DataLab Search Trend API.
calendar 이벤트 + seasons_kr 키워드를 시드로 입력해
target_week 주변 상승 비율을 반환한다.
"""
from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Any

import httpx

from central.settings import NAVER_DATALAB_CLIENT_ID, NAVER_DATALAB_CLIENT_SECRET

_API_URL = "https://openapi.naver.com/v1/datalab/search"

# DataLab API 최대 그룹 수 (1회 호출당 최대 5개 키워드 그룹)
_MAX_GROUPS = 5
_MAX_KEYWORDS_PER_GROUP = 5

# calendar와 독립적인 상업 수요 신호 키워드 그룹
# — 한국 B2B 스톡 이미지 발주가 실제로 발생하는 영역들
_INDEPENDENT_GROUPS: list[dict] = [
    {
        "groupName": "공공기관홍보",
        "keywords": ["지자체 홍보", "공공기관 안내", "보건소 홍보", "행정안전부", "복지관"],
    },
    {
        "groupName": "이커머스프로모션",
        "keywords": ["온라인쇼핑 할인", "쇼핑몰 배너", "프로모션 배너", "상세페이지", "이벤트 배너"],
    },
    {
        "groupName": "교육홍보",
        "keywords": ["학원 모집", "학원 광고", "입학 홍보", "교육 안내", "수강 모집"],
    },
    {
        "groupName": "외식업홍보",
        "keywords": ["카페 홍보", "음식점 홍보", "프랜차이즈 홍보", "배달 이벤트", "맛집 홍보"],
    },
    {
        "groupName": "이벤트행사",
        "keywords": ["행사 홍보", "이벤트 포스터", "축제 홍보", "전시 홍보", "공연 홍보"],
    },
]


def _iso_week_to_period(target_week: str) -> tuple[str, str]:
    """
    Naver DataLab은 미래 날짜를 허용하지 않는다.
    target_week가 미래이면 1년 전 같은 주차를 기준으로 조회한다.
    (예: 2026-W30 → 2025-W30 전후 4주)
    """
    match = re.fullmatch(r"(\d{4})-W(\d{2})", target_week)
    if not match:
        raise ValueError(f"잘못된 target_week: {target_week!r}")
    year, week = int(match.group(1)), int(match.group(2))
    week_end = date.fromisocalendar(year, week, 7)

    today = date.today()
    if week_end > today:
        # 미래 주차 → 1년 전 같은 주차로 대체
        prev_year = year - 1
        try:
            week_end = date.fromisocalendar(prev_year, week, 7)
        except ValueError:
            # 윤년 등으로 해당 주차 없는 경우 마지막 주차 사용
            week_end = date.fromisocalendar(prev_year, 52, 7)

    end = week_end
    start = end - timedelta(weeks=4)
    return start.isoformat(), end.isoformat()


def _build_keyword_groups(
    calendar_events: list[dict],
    season_keywords: list[str],
) -> list[dict]:
    """
    DataLab API 키워드 그룹 빌드.
    calendar 이벤트 키워드 + season 키워드를 합쳐 최대 2그룹으로 묶는다.
    나머지 3슬롯은 독립 상업 수요 그룹으로 채운다.
    """
    # calendar 기반 키워드 (최대 2그룹)
    all_keywords: list[str] = []
    for ev in calendar_events:
        all_keywords.extend(ev.get("keywords", []))
    all_keywords.extend(season_keywords)

    seen: set[str] = set()
    deduped: list[str] = []
    for kw in all_keywords:
        if kw and kw not in seen:
            seen.add(kw)
            deduped.append(kw)

    cal_groups = []
    chunk_size = _MAX_KEYWORDS_PER_GROUP
    for i in range(0, min(len(deduped), 2 * chunk_size), chunk_size):
        chunk = deduped[i: i + chunk_size]
        if chunk:
            cal_groups.append({
                "groupName": f"cal_group_{i // chunk_size + 1}",
                "keywords": chunk,
            })

    # 독립 상업 수요 그룹으로 나머지 슬롯 채우기
    remaining = _MAX_GROUPS - len(cal_groups)
    ind_groups = _INDEPENDENT_GROUPS[:remaining]

    return (cal_groups + ind_groups)[:_MAX_GROUPS]


def _call_datalab(groups: list[dict], start_date: str, end_date: str) -> dict:
    headers = {
        "X-Naver-Client-Id": NAVER_DATALAB_CLIENT_ID,
        "X-Naver-Client-Secret": NAVER_DATALAB_CLIENT_SECRET,
        "Content-Type": "application/json",
    }
    body = {
        "startDate": start_date,
        "endDate": end_date,
        "timeUnit": "week",
        "keywordGroups": groups,
    }
    resp = httpx.post(_API_URL, headers=headers, json=body, timeout=15)
    resp.raise_for_status()
    return resp.json()


def collect(
    target_week: str,
    calendar_events: list[dict],
    season_keywords: list[str] | None = None,
    market: str = "KR",
) -> list[dict[str, Any]]:
    """
    Returns:
        list of {
            "group_name": str,
            "keywords": list[str],
            "ratio_latest": float,     # 최근 주 ratio (0~100)
            "ratio_avg": float,
            "trend": "rising" | "stable" | "falling",
            "raw_data": list[{period, ratio}],
        }
    """
    if not NAVER_DATALAB_CLIENT_ID or not NAVER_DATALAB_CLIENT_SECRET:
        return []

    groups = _build_keyword_groups(calendar_events, season_keywords or [])
    if not groups:
        return []

    start_date, end_date = _iso_week_to_period(target_week)

    try:
        raw = _call_datalab(groups, start_date, end_date)
    except Exception:
        return []

    results: list[dict] = []
    for item in raw.get("results", []):
        data = item.get("data", [])
        ratios = [d.get("ratio", 0.0) for d in data]
        if not ratios:
            continue

        latest = ratios[-1]
        avg = sum(ratios) / len(ratios)
        if latest > avg * 1.1:
            trend = "rising"
        elif latest < avg * 0.9:
            trend = "falling"
        else:
            trend = "stable"

        group = next(
            (g for g in groups if g["groupName"] == item.get("title")),
            {},
        )
        results.append({
            "group_name": item.get("title", ""),
            "keywords": group.get("keywords", []),
            "ratio_latest": latest,
            "ratio_avg": avg,
            "trend": trend,
            "raw_data": data,
        })

    results.sort(key=lambda x: x["ratio_latest"], reverse=True)
    return results
