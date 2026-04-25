"""
fallback.py: valid 카드가 0개일 때만 최소 보충용으로 사용.
- status='fallback_review' 로 저장 (pending 아님)
- idea_title에 "{keyword} 관련" 패턴 금지
- source_summary에 "수요 기반 fallback 생성" 문구 금지
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

_CATEGORY_TEMPLATES: list[dict] = [
    {
        "type": "holiday",
        "buyer": "소상공인 자영업자",
        "use_case": "명절·기념일 프로모션 배너 / SNS 광고",
        "intent": "promotional_banner",
        "industry": "ecommerce",
        "date_specificity": "high",
        "asset_hints": ["gift box ribbon", "holiday table flat lay", "sale badge graphic"],
    },
    {
        "type": "shopping",
        "buyer": "온라인 쇼핑몰 MD",
        "use_case": "시즌 세일 메인 배너 / 카테고리 상단 배너",
        "intent": "promotional_banner",
        "industry": "ecommerce",
        "date_specificity": "high",
        "asset_hints": ["shopping bag mockup", "percent discount tag", "summer sale typography"],
    },
    {
        "type": "public_campaigns",
        "buyer": "지자체·공공기관 홍보 담당자",
        "use_case": "공공 캠페인 안내 포스터 / 카드뉴스",
        "intent": "public_health_campaign",
        "industry": "government_public",
        "date_specificity": "medium",
        "asset_hints": ["city hall silhouette", "infographic icons", "public notice layout"],
    },
    {
        "type": "season",
        "buyer": "학원·교육기관 원장",
        "use_case": "방학·학기 특강 모집 배너 / 현수막",
        "intent": "education_promotion",
        "industry": "education",
        "date_specificity": "medium",
        "asset_hints": ["classroom chalkboard", "student silhouette", "registration CTA banner"],
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

        # "{keyword} 관련 ~" 패턴 대신 구체적 제목 조합
        idea_title = f"{event_name} {template['use_case']}"

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
            "reason": f"{event_name} 시기에 {template['buyer']}가 {template['use_case']} 제작",
            "intent": template["intent"],
            "source_keywords": all_keywords,
            "source_summary": f"{event_name} 시기 신호 기반 최소 보충 카드",
            "asset_hints": list(template.get("asset_hints") or [event_name, "banner layout", "icon set"]),
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
