"""
fallback.py: valid 카드가 0개일 때만 최소 보충용으로 사용.
- status='fallback_review' 로 저장 (pending 아님)
- idea_title에 "{keyword} 관련" 패턴 금지
- 수요 방향 카드: 최종 포맷(배너/포스터 등) 단어 없이 활동 맥락·추상 asset 방향만
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

_CATEGORY_TEMPLATES: list[dict] = [
    {
        "type": "holiday",
        "buyer": "소상공인 자영업자, 동네 카페 사장, 학교 교사",
        "use_case": "명절 소비 자극 커뮤니케이션, 가족·선물 토픽",
        "intent": "seasonal_gift_demand",
        "industry": "ecommerce",
        "date_specificity": "high",
        "asset_hints": ["gift giving", "holiday season", "family gathering"],
    },
    {
        "type": "shopping",
        "buyer": "온라인 쇼핑몰 MD, 오픈마켓 판매자",
        "use_case": "시즌말 재고 압박, 소비 자극 시각자료",
        "intent": "seasonal_inventory_demand",
        "industry": "ecommerce",
        "date_specificity": "high",
        "asset_hints": ["discount urgency", "seasonal retail", "clearance mood"],
    },
    {
        "type": "public_campaigns",
        "buyer": "지자체·공공기관 홍보 담당자, 보건소 담당자",
        "use_case": "시민 안전·보건 리스크 커뮤니케이션, 계절성 공공토픽",
        "intent": "public_seasonal_risk_demand",
        "industry": "government_public",
        "date_specificity": "medium",
        "asset_hints": ["public safety", "seasonal health risk", "civic seasonal messaging"],
    },
    {
        "type": "season",
        "buyer": "학원·교육기관 원장, 교육기업 마케터",
        "use_case": "방학·학기 전환기 모집·학습 토픽, 학부모 대상 정보",
        "intent": "education_season_demand",
        "industry": "education",
        "date_specificity": "medium",
        "asset_hints": ["academic calendar", "student life", "enrollment season"],
    },
]


def rule_based_generation(
    signals: dict[str, list],
    market: str = "KR",
    target_week: str = "",
    target_date: str | None = None,
) -> list[dict[str, Any]]:
    calendar = signals.get("calendar", [])
    naver = signals.get("naver_datalab", [])

    if not calendar and not naver:
        logger.warning("fallback: 신호가 없어 카드를 만들 수 없습니다.")
        return []

    results: list[dict] = []

    for event in calendar:
        event_type = event.get("type", "season")
        event_name = event.get("name", "")
        event_keywords = event.get("keywords", [])
        event_date = event.get("date")

        if not event_name:
            continue

        template = next(
            (t for t in _CATEGORY_TEMPLATES if t["type"] == event_type),
            _CATEGORY_TEMPLATES[0],
        )

        naver_top = naver[:2]
        naver_keywords = [kw for n in naver_top for kw in (n.get("keywords") or [])]
        all_keywords = list(dict.fromkeys(event_keywords + naver_keywords))[:5]

        if len(all_keywords) < 2:
            continue

        idea_title = f"{event_name} 시기 콘텐츠 수요"

        if len(idea_title.replace(" ", "")) < 8:
            continue

        used_evidence = [{"source": "calendar", "keyword": event_name}]
        if naver_top:
            used_evidence.append({
                "source": "naver_datalab",
                "keyword": naver_top[0].get("keywords", [""])[0],
            })

        results.append({
            "idea_title": idea_title,
            "buyer": template["buyer"],
            "use_case": template["use_case"],
            "reason": (
                f"{event_name} 시기에 {template['buyer']} 쪽에서 "
                f"{template['use_case']} 방향의 시각·토픽 수요가 생길 수 있다."
            ),
            "intent": template["intent"],
            "source_keywords": all_keywords,
            "source_summary": f"{event_name} 시기 신호 기반 최소 보충 카드",
            "asset_hints": list(template.get("asset_hints") or [event_name, "seasonal topic", "visual direction"]),
            "used_evidence": used_evidence,
            "event_date": event_date,
            "lead_start_days": 7,
            "lead_end_days": 30,
            "industry": template["industry"],
            "date_specificity": template["date_specificity"],
            "_is_fallback": True,
            "_fallback_status": "fallback_review",
        })

        if len(results) >= 5:
            break

    logger.info("fallback: %d개 카드 생성", len(results))
    return results
