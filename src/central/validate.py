"""
validate.py: idea card 검증 (warning 중심, hard reject 최소화).

Hard: generic title, buyer 블랙리스트, 빈 필드, used_evidence bundle 미매칭 0건
Soft: condition 영역 매핑 실패 시 부합 스킵 + 카드 5장 제한, buyer 분포 warning
"""
from __future__ import annotations

import logging
import re
logger = logging.getLogger(__name__)

_GENERIC_TITLE_TOKENS: frozenset[str] = frozenset({
    "여름건강", "여름 건강", "장마안전", "장마 안전", "겨울건강",
    "봄건강", "가을건강", "계절건강", "건강관리", "건강 관리",
    "여름철 피부 관리", "피부 관리 가이드",
    "여름철 식재료 보관", "식재료 보관",
    "여름철 차량 관리", "차량 관리",
    "여름철 반려동물 관리", "반려동물 관리",
    "여름철 인테리어", "인테리어 가이드",
    "여행", "나들이", "계절", "시즌", "이벤트", "행사",
    "summer", "winter", "spring", "autumn", "health",
    "holiday", "festival", "season",
})

_GENERIC_TITLE_RE = re.compile(
    r"^(여름|겨울|봄|가을|계절|장마)철?\s*(건강|안전|관리|가이드|정보|안내|인테리어|여행|요리|레시피)$"
)

# fallback "{keyword} 관련 ~" 패턴
_TEMPLATE_TITLE_RE = re.compile(r".+\s관련\s.+")

_BUYER_BLACKLIST: frozenset[str] = frozenset({
    "기업", "브랜드", "사람들", "사용자", "소비자",
    "대중", "고객", "회사", "업체", "개인",
    "누구나", "모든이", "모두", "일반인",
})

_VALID_DATE_SPECIFICITY = {"high", "medium", "low"}

_VALID_INDUSTRY = {
    "government_public", "ecommerce", "travel", "education",
    "healthcare", "food_beverage", "fashion", "real_estate",
    "finance", "entertainment", "sports_outdoor", "beauty",
    "pet", "it_tech", "other",
}

# condition 라벨 → 영역 (0개 또는 2개 이상 매칭이면 None)
_AREA_TRIGGERS: dict[str, tuple[str, ...]] = {
    "public": (
        "공공", "공공기관", "지자체", "정부", "보건소", "소방", "교육청", "행정",
        "보건", "재난", "안전",
    ),
    "commercial": ("상업", "광고", "쇼핑", "브랜드", "마케터", "이커머스", "판매"),
    "event": ("이벤트", "행사", "페스티벌", "축제", "팝업", "페어"),
    "education": ("교육", "학원", "학교", "강좌", "수업"),
    "local": ("지역", "상권", "로컬", "동네", "소상공"),
}

# 다른 영역 buyer 시드 (명백 충돌 검출용)
_CONFLICT_SEEDS: dict[str, tuple[str, ...]] = {
    "public": ("쇼핑몰", "이커머스", "MD", "브랜드 마케", "프랜차이즈 본사"),
    "commercial": ("보건소", "구청", "시청", "교육청", "소방서", "공무원"),
    "event": (),
    "education": ("쇼핑몰",),
    "local": (),
}

_JOSA_RE = re.compile(
    r"(은|는|이|가|을|를|의|에|에서|으로|로|와|과|도|만)$"
)


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def _normalize_evidence_token(s: str) -> str:
    t = (s or "").strip().lower()
    t = re.sub(r"\s+", "", t)
    while len(t) >= 2 and _JOSA_RE.search(t):
        t = _JOSA_RE.sub("", t)
    return t


def is_generic(title: str) -> bool:
    normalized = _normalize(title)
    if len(normalized.replace(" ", "")) < 8:
        return True
    for token in _GENERIC_TITLE_TOKENS:
        if normalized == _normalize(token):
            return True
    if _GENERIC_TITLE_RE.match(normalized):
        return True
    if _TEMPLATE_TITLE_RE.match(normalized):
        return True
    return False


def _has_buyer_blacklisted(buyer: str) -> bool:
    normalized = _normalize(buyer)
    bl = {_normalize(b) for b in _BUYER_BLACKLIST}
    if normalized in bl:
        return True
    tokens = normalized.split()
    if tokens and all(t in bl for t in tokens):
        return True
    return False


def map_condition_to_area(label: str) -> str | None:
    """정확히 하나의 영역 트리거에만 매칭될 때만 영역 키 반환."""
    if not label:
        return None
    hits: list[str] = []
    for area, triggers in _AREA_TRIGGERS.items():
        if any(tr in label for tr in triggers):
            hits.append(area)
    if len(hits) != 1:
        return None
    return hits[0]


def _build_reference_strings(bundle: dict | None) -> set[str]:
    refs: set[str] = set()
    if not bundle:
        return refs
    sig = bundle.get("signals") or {}
    for ev in sig.get("calendar") or []:
        name = ev.get("name") or ""
        if name:
            refs.add(_normalize_evidence_token(name))
        for kw in ev.get("keywords") or []:
            if isinstance(kw, str) and kw:
                refs.add(_normalize_evidence_token(kw))
    for n in sig.get("naver_datalab") or []:
        for kw in n.get("keywords") or []:
            if isinstance(kw, str) and kw:
                refs.add(_normalize_evidence_token(kw))
    for p in sig.get("pinterest") or []:
        kw = p.get("keyword") or ""
        if kw:
            refs.add(_normalize_evidence_token(str(kw)))
    for g in bundle.get("grounding_evidences") or []:
        if not isinstance(g, dict):
            continue
        t = g.get("title") or ""
        if t:
            refs.add(_normalize_evidence_token(str(t)))
        rk = g.get("ref_keyword")
        if rk:
            refs.add(_normalize_evidence_token(str(rk)))
    refs.discard("")
    return refs


def _evidence_item_field(ev: dict) -> str:
    for key in ("keyword", "title", "name", "url"):
        v = ev.get(key)
        if v and isinstance(v, str):
            return v
    return ""


def _matches_reference(token: str, refs: set[str]) -> bool:
    if not token or not refs:
        return False
    n = _normalize_evidence_token(token)
    if not n:
        return False
    for r in refs:
        if not r:
            continue
        if n in r or r in n:
            return True
    return False


def _used_evidence_match_count(used_evidence: list, refs: set[str]) -> int:
    if not isinstance(used_evidence, list) or not refs:
        return 0
    n_ok = 0
    for ev in used_evidence:
        if not isinstance(ev, dict):
            continue
        field = _evidence_item_field(ev)
        if field and _matches_reference(field, refs):
            n_ok += 1
    return n_ok


def _buyer_conflicts_area(buyer: str, area: str) -> bool:
    buyer_n = buyer or ""
    seeds = _CONFLICT_SEEDS.get(area, ())
    return any(seed in buyer_n for seed in seeds)


def _apply_condition_postprocess(ideas: list[dict]) -> list[dict]:
    """영역 매핑 실패 시 warning + 카드 5장 제한. 매핑 성공 시 충돌만 reject."""
    labels = {str(idea.get("_condition") or "") for idea in ideas if idea.get("_condition")}
    unmapped: set[str] = set()
    for lab in labels:
        if lab and map_condition_to_area(lab) is None:
            unmapped.add(lab)
            logger.warning(
                "[validate] condition 영역 매핑 실패 %r — 부합 검사 스킵, 해당 condition 카드 최대 5장",
                lab,
            )

    after_conflict: list[dict] = []
    for idea in ideas:
        cond = str(idea.get("_condition") or "")
        area = map_condition_to_area(cond) if cond else None
        if area is not None:
            buyer = idea.get("buyer") or ""
            if _buyer_conflicts_area(buyer, area):
                logger.debug(
                    "condition 충돌 reject: area=%s buyer=%r title=%s",
                    area, buyer, idea.get("idea_title"),
                )
                continue
        after_conflict.append(idea)

    counts: dict[str, int] = {lab: 0 for lab in unmapped}
    result: list[dict] = []
    for idea in after_conflict:
        cond = str(idea.get("_condition") or "")
        if cond in unmapped:
            if counts[cond] >= 5:
                continue
            counts[cond] += 1
        result.append(idea)
    return result


def _warn_buyer_diversity(ideas: list[dict]) -> None:
    buyers = [_normalize(idea.get("buyer") or "") for idea in ideas]
    uniq = len({b for b in buyers if b})
    if uniq < 2:
        logger.warning(
            "[validate] buyer 분포: 서로 다른 buyer %d개 (2 미만 — 데이터는 유지, 다음 튜닝 참고)",
            uniq,
        )


def validate_ideas(
    ideas: list[dict],
    bundle: dict | None = None,
) -> tuple[list[dict], int]:
    refs = _build_reference_strings(bundle) if bundle else set()
    valid: list[dict] = []
    invalid = 0

    for idea in ideas:
        if not isinstance(idea, dict):
            invalid += 1
            continue

        title = idea.get("idea_title", "")
        buyer = idea.get("buyer", "")
        use_case = idea.get("use_case", "")
        used_evidence = idea.get("used_evidence") or []
        intent = (idea.get("intent") or "").strip()
        hints = idea.get("asset_hints") or []

        if is_generic(title):
            invalid += 1
            continue
        if not buyer or _has_buyer_blacklisted(buyer):
            invalid += 1
            continue
        if not use_case:
            invalid += 1
            continue
        if not intent:
            invalid += 1
            continue
        if not isinstance(hints, list) or len(hints) < 2:
            invalid += 1
            continue

        keywords = idea.get("source_keywords") or []
        if len(keywords) < 2:
            invalid += 1
            continue

        if bundle is not None and refs and not idea.get("_is_fallback"):
            if _used_evidence_match_count(used_evidence, refs) < 1:
                logger.debug("evidence 미매칭 reject: title=%s", title)
                invalid += 1
                continue

        if idea.get("date_specificity") not in _VALID_DATE_SPECIFICITY:
            idea["date_specificity"] = None
        if idea.get("industry") not in _VALID_INDUSTRY:
            idea["industry"] = "other"

        valid.append(idea)

    valid = _apply_condition_postprocess(valid)
    _warn_buyer_diversity(valid)
    return valid, invalid
