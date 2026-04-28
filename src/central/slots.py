"""
고정 5 슬롯: evidence 축으로 직교 분리해 primary_keyword / demand_family(industry)를
코드에서 잠근 뒤, 슬롯별 sub-bundle로 idea 생성을 호출한다.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import yaml

from central.collectors.pinterest import _is_noise

logger = logging.getLogger(__name__)

_ROOT = Path(__file__).resolve().parents[2]
_EVERGREEN_PATH = _ROOT / "data" / "evergreen_seeds.yaml"

_INDUSTRY_FALLBACK: dict[str, tuple[str, ...]] = {
    "calendar_event_slot": ("government_public", "education", "entertainment"),
    "search_trend_slot": ("ecommerce", "healthcare", "food_beverage"),
    "pinterest_visual_slot": ("fashion", "beauty", "food_beverage", "entertainment"),
    "commercial_evergreen_slot": ("ecommerce", "education", "travel"),
    "long_tail_niche_slot": ("other", "travel", "sports_outdoor", "pet"),
}

_VALID_INDUSTRY = {
    "government_public", "ecommerce", "travel", "education",
    "healthcare", "food_beverage", "fashion", "real_estate",
    "finance", "entertainment", "sports_outdoor", "beauty",
    "pet", "it_tech", "other",
}


def _norm_kw(s: str) -> str:
    t = (s or "").strip().lower()
    return re.sub(r"\s+", "", t)


@dataclass
class GenerationSlot:
    slot_id: str
    primary_keyword: str
    demand_family: str
    forbidden_keywords: tuple[str, ...]
    sub_bundle: dict[str, Any] = field(default_factory=dict)


def _parse_target_month(bundle: dict) -> int:
    td = bundle.get("target_date") or ""
    try:
        return date.fromisoformat(str(td)[:10]).month
    except ValueError:
        return date.today().month


def _load_evergreen_seeds(month: int) -> list[dict[str, str]]:
    if not _EVERGREEN_PATH.exists():
        return [{"keyword": "시즌 상업 프로모션", "industry": "ecommerce"}]
    try:
        data = yaml.safe_load(_EVERGREEN_PATH.read_text(encoding="utf-8")) or {}
        rows = (data.get("months") or {}).get(month) or (data.get("months") or {}).get(8) or []
        out: list[dict[str, str]] = []
        for row in rows:
            if isinstance(row, dict) and row.get("keyword"):
                ind = str(row.get("industry") or "ecommerce")
                if ind not in _VALID_INDUSTRY:
                    ind = "ecommerce"
                out.append({"keyword": str(row["keyword"]).strip(), "industry": ind})
        return out or [{"keyword": "시즌 상업", "industry": "ecommerce"}]
    except Exception as e:
        logger.warning("evergreen_seeds 로드 실패: %s", e)
        return [{"keyword": "시즌 상업", "industry": "ecommerce"}]


def _pick_calendar_event(cal: list[dict], target_date: str | None) -> tuple[str, list[dict]]:
    holidays = [
        e for e in cal
        if e.get("type") == "holiday" and e.get("date") and e.get("name")
    ]
    if target_date and holidays:
        try:
            td = date.fromisoformat(str(target_date)[:10])
            holidays.sort(
                key=lambda e: abs((date.fromisoformat(str(e["date"])[:10]) - td).days),
            )
            ev = holidays[0]
            return ev["name"], [ev]
        except ValueError:
            pass
    if holidays:
        ev = sorted(holidays, key=lambda e: str(e.get("date") or ""))[0]
        return str(ev.get("name") or ""), [ev]
    for e in cal:
        nm = (e.get("name") or "").strip()
        if nm:
            return nm, [e]
    return "시즌 이벤트", cal[:5] if cal else []


def _rank_naver(naver: list[dict]) -> list[dict]:
    return sorted(
        naver,
        key=lambda x: (
            0 if x.get("trend") == "rising" else 1,
            -float(x.get("ratio_latest") or 0.0),
        ),
    )


def _naver_primary_and_rest(
    naver: list[dict],
    avoid_norm: set[str],
) -> tuple[str, list[dict], list[str]]:
    """첫 그룹에서 primary, 전체에서 키워드 순서 리스트."""
    ranked = _rank_naver(naver)
    flat: list[str] = []
    for g in ranked:
        for kw in g.get("keywords") or []:
            if isinstance(kw, str) and kw.strip():
                k = kw.strip()
                if k not in flat:
                    flat.append(k)
    primary = ""
    for g in ranked:
        for kw in g.get("keywords") or []:
            if not isinstance(kw, str) or not kw.strip():
                continue
            k = kw.strip()
            if _norm_kw(k) not in avoid_norm:
                primary = k
                break
        if primary:
            break
    if not primary and flat:
        primary = flat[0]
    return primary, ranked, flat


def _pick_pinterest_primary(pin: list[dict], avoid_norm: set[str]) -> str:
    for p in pin:
        kw = str(p.get("keyword") or "").strip()
        if not kw:
            continue
        if _is_noise(kw):
            continue
        if _norm_kw(kw) in avoid_norm:
            continue
        return kw
    for p in pin:
        kw = str(p.get("keyword") or "").strip()
        if kw and _norm_kw(kw) not in avoid_norm:
            return kw
    return ""


def _pick_evergreen(month: int, used_norm: set[str]) -> tuple[str, str]:
    for row in _load_evergreen_seeds(month):
        kw = row["keyword"]
        if _norm_kw(kw) not in used_norm:
            return kw, row["industry"]
    seeds = _load_evergreen_seeds(month)
    return seeds[0]["keyword"], seeds[0]["industry"]


def _pick_long_tail(
    flat_naver: list[str],
    forbidden_norm: set[str],
    cal: list[dict],
) -> str:
    for kw in flat_naver:
        if _norm_kw(kw) not in forbidden_norm:
            return kw
    for ev in cal:
        for kw in ev.get("keywords") or []:
            if isinstance(kw, str) and kw.strip():
                k = kw.strip()
                if _norm_kw(k) not in forbidden_norm:
                    return k
        nm = (ev.get("name") or "").strip()
        if nm and _norm_kw(nm) not in forbidden_norm:
            return nm
    return "틈새 검색 수요"


def _assign_industry(slot_id: str, preferred: str | None, used: set[str]) -> str:
    if preferred and preferred in _VALID_INDUSTRY and preferred not in used:
        return preferred
    for cand in _INDUSTRY_FALLBACK.get(slot_id, ("other",)):
        if cand not in used:
            return cand
    return "other"


def _sub_bundle(
    full: dict[str, Any],
    *,
    calendar: list[dict] | None = None,
    naver: list[dict] | None = None,
    pinterest: list[dict] | None = None,
) -> dict[str, Any]:
    sig = full.get("signals") or {}
    return {
        "market": full.get("market"),
        "target_week": full.get("target_week"),
        "target_date": full.get("target_date"),
        "grounding_evidences": list(full.get("grounding_evidences") or []),
        "signals": {
            "calendar": calendar if calendar is not None else list(sig.get("calendar") or []),
            "naver_datalab": naver if naver is not None else list(sig.get("naver_datalab") or []),
            "pinterest": pinterest if pinterest is not None else list(sig.get("pinterest") or []),
        },
    }


def assign_slots(bundle: dict[str, Any]) -> list[GenerationSlot]:
    """
    bundle에서 5 슬롯을 결정론적으로 구성한다.
    """
    sig = bundle.get("signals") or {}
    cal = list(sig.get("calendar") or [])
    naver_all = list(sig.get("naver_datalab") or [])
    pin_all = list(sig.get("pinterest") or [])
    target_date = bundle.get("target_date")
    month = _parse_target_month(bundle)

    cal_kw, cal_subset = _pick_calendar_event(cal, str(target_date) if target_date else None)
    cal_norm = {_norm_kw(cal_kw)}

    search_primary, naver_ranked, flat_naver = _naver_primary_and_rest(naver_all, cal_norm)
    if not search_primary and flat_naver:
        search_primary = flat_naver[0]
    used_kw_norm = {_norm_kw(cal_kw), _norm_kw(search_primary)}

    pin_primary = _pick_pinterest_primary(pin_all, used_kw_norm)
    if not pin_primary and pin_all:
        pin_primary = str(pin_all[0].get("keyword") or "").strip() or "Pinterest trend"
    if pin_primary.strip():
        used_kw_norm.add(_norm_kw(pin_primary))

    ev_kw, ev_industry = _pick_evergreen(month, used_kw_norm)
    used_kw_norm.add(_norm_kw(ev_kw))

    forbidden_for_tail = set(used_kw_norm)
    top3 = set()
    for kw in flat_naver[:3]:
        top3.add(_norm_kw(kw))
    forbidden_for_tail |= top3

    lt_kw = _pick_long_tail(flat_naver, forbidden_for_tail, cal)

    forbid_others = {_norm_kw(cal_kw), _norm_kw(search_primary), _norm_kw(pin_primary), _norm_kw(ev_kw)}
    if _norm_kw(lt_kw) in forbid_others or not (lt_kw or "").strip():
        lt_kw = _pick_long_tail(flat_naver, forbid_others | top3, cal) or lt_kw
    if _norm_kw(lt_kw) in forbid_others or not (lt_kw or "").strip():
        for g in reversed(naver_ranked):
            for kw in g.get("keywords") or []:
                if not isinstance(kw, str):
                    continue
                k = kw.strip()
                if k and _norm_kw(k) not in forbid_others:
                    lt_kw = k
                    break
            else:
                continue
            break
    if not (lt_kw or "").strip():
        lt_kw = "틈새 검색 수요"

    used_ind: set[str] = set()
    industries: dict[str, str] = {}

    industries["calendar_event_slot"] = _assign_industry(
        "calendar_event_slot",
        "government_public" if any(x in cal_kw for x in ("광복", "삼일", "현충", "개천", "한글")) else None,
        used_ind,
    )
    used_ind.add(industries["calendar_event_slot"])

    industries["search_trend_slot"] = _assign_industry("search_trend_slot", None, used_ind)
    used_ind.add(industries["search_trend_slot"])

    industries["pinterest_visual_slot"] = _assign_industry("pinterest_visual_slot", None, used_ind)
    used_ind.add(industries["pinterest_visual_slot"])

    industries["commercial_evergreen_slot"] = _assign_industry(
        "commercial_evergreen_slot",
        ev_industry if ev_industry in _VALID_INDUSTRY else None,
        used_ind,
    )
    used_ind.add(industries["commercial_evergreen_slot"])

    industries["long_tail_niche_slot"] = _assign_industry("long_tail_niche_slot", None, used_ind)
    used_ind.add(industries["long_tail_niche_slot"])

    all_primaries = (cal_kw, search_primary, pin_primary, ev_kw, lt_kw)
    forbidden_lists: list[tuple[str, ...]] = []
    for i, pk in enumerate(all_primaries):
        others = tuple(p for j, p in enumerate(all_primaries) if j != i and p)
        forbidden_lists.append(others)

    naver_top_group: list[dict] = naver_ranked[:1] if naver_ranked else []

    slots: list[GenerationSlot] = [
        GenerationSlot(
            slot_id="calendar_event_slot",
            primary_keyword=cal_kw,
            demand_family=industries["calendar_event_slot"],
            forbidden_keywords=forbidden_lists[0],
            sub_bundle=_sub_bundle(bundle, calendar=cal_subset, naver=[], pinterest=[]),
        ),
        GenerationSlot(
            slot_id="search_trend_slot",
            primary_keyword=search_primary,
            demand_family=industries["search_trend_slot"],
            forbidden_keywords=forbidden_lists[1],
            sub_bundle=_sub_bundle(bundle, calendar=[], naver=naver_top_group or naver_all[:1], pinterest=[]),
        ),
        GenerationSlot(
            slot_id="pinterest_visual_slot",
            primary_keyword=pin_primary,
            demand_family=industries["pinterest_visual_slot"],
            forbidden_keywords=forbidden_lists[2],
            sub_bundle=_sub_bundle(
                bundle,
                calendar=[],
                naver=[],
                pinterest=pin_all[:20],
            ),
        ),
        GenerationSlot(
            slot_id="commercial_evergreen_slot",
            primary_keyword=ev_kw,
            demand_family=industries["commercial_evergreen_slot"],
            forbidden_keywords=forbidden_lists[3],
            sub_bundle=_sub_bundle(
                bundle,
                calendar=cal[:3],
                naver=naver_ranked[:2],
                pinterest=pin_all[:10],
            ),
        ),
        GenerationSlot(
            slot_id="long_tail_niche_slot",
            primary_keyword=lt_kw,
            demand_family=industries["long_tail_niche_slot"],
            forbidden_keywords=forbidden_lists[4],
            sub_bundle=_sub_bundle(
                bundle,
                calendar=cal[:2],
                naver=(
                    [g for g in naver_ranked if any(
                        _norm_kw(k) == _norm_kw(lt_kw) for k in (g.get("keywords") or [])
                    )][:3]
                    or (naver_ranked[1:4] if len(naver_ranked) > 1 else naver_ranked[:1])
                ),
                pinterest=pin_all[:8],
            ),
        ),
    ]

    logger.info(
        "[slots] %s",
        json.dumps(
            [{"id": s.slot_id, "kw": s.primary_keyword, "industry": s.demand_family} for s in slots],
            ensure_ascii=False,
        ),
    )
    return slots
