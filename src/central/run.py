"""
central/run.py: CLI 진입점 + 오케스트레이터.
collect → bundle → grounding(1회) → conditions(3) → idea×3(fast) → enrich → validate → (fallback) → save
"""
from __future__ import annotations

import argparse
import logging
import re
import sys
from datetime import date, timedelta

from central import db
from central.collectors import calendar as cal_collector
from central.collectors import naver as naver_collector
from central.collectors import pinterest as pin_collector
from central import evidence as ev_module
from central import grounding as grounding_module
from central import conditions as conditions_module
from central import gemini_idea
from central import enrich as enrich_module
from central import validate as validate_module
from central import fallback as fallback_module
from central.settings import CARDS_MIN_PER_WEEK, DEFAULT_MARKET, WEEKS_AHEAD

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)
logger = logging.getLogger("central.run")


# ──────────────────────────────────────────────────────────────────────────────
# 주차 유틸
# ──────────────────────────────────────────────────────────────────────────────

def _date_to_iso_week(d: date) -> str:
    iso = d.isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


def _upcoming_weeks(n: int = WEEKS_AHEAD) -> list[str]:
    today = date.today()
    weeks = []
    for i in range(1, n + 1):
        d = today + timedelta(weeks=i)
        weeks.append(_date_to_iso_week(d))
    return weeks


def _week_to_target_date(target_week: str) -> str:
    """'2026-W29' → 해당 주 월요일 날짜."""
    match = re.fullmatch(r"(\d{4})-W(\d{2})", target_week)
    if not match:
        raise ValueError(f"잘못된 target_week 형식: {target_week!r}")
    year, week = int(match.group(1)), int(match.group(2))
    monday = date.fromisocalendar(year, week, 1)
    return monday.isoformat()


# ──────────────────────────────────────────────────────────────────────────────
# 핵심 파이프라인 (단일 흐름)
# ──────────────────────────────────────────────────────────────────────────────

def run_pipeline(
    market: str,
    target_week: str,
    target_date: str | None = None,
    triggered_by: str = "cli",
    min_cards: int = CARDS_MIN_PER_WEEK,
) -> dict:
    """
    Returns: {
        "run_id": str,
        "status": str,
        "cards_inserted": int,
        "cards_skipped": int,
        "cards_invalid": int,
        "used_fallback": bool,
    }
    """
    if target_date is None:
        target_date = _week_to_target_date(target_week)

    # 이미 충분한 카드가 있으면 skip (min_cards=0이면 항상 실행)
    existing = db.count_existing_cards(market, target_week)
    if min_cards > 0 and existing >= min_cards:
        logger.info(f"[{target_week}] 이미 {existing}개 카드 있음 → skip")
        return {"run_id": None, "status": "skipped", "cards_inserted": 0,
                "cards_skipped": existing, "cards_invalid": 0, "used_fallback": False}

    run_id = db.create_run(market, target_week, target_date, triggered_by)
    logger.info(f"[{target_week}] run 시작 (run_id={run_id})")

    try:
        # ── 1. 신호 수집 ──────────────────────────────────────────────────────
        logger.info("1/8 calendar 수집...")
        cal_signals = cal_collector.collect(target_week, market)
        logger.info(f"   → {len(cal_signals)}개 이벤트")

        season_kws = [kw for ev in cal_signals for kw in ev.get("keywords", [])]

        logger.info("2/8 naver_datalab 수집...")
        naver_signals = naver_collector.collect(target_week, cal_signals, season_kws, market)
        logger.info(f"   → {len(naver_signals)}개 키워드 그룹")

        logger.info("3/8 pinterest 수집...")
        pin_signals = pin_collector.collect(target_week, market)
        logger.info(f"   → {len(pin_signals)}개 트렌드")

        # ── 2. evidence bundle 빌드 + source_signals 저장 ─────────────────────
        logger.info("4/8 evidence bundle 빌드...")
        ev_module.save_signals(run_id, market, target_week, cal_signals, naver_signals, pin_signals)
        bundle = ev_module.build_bundle(
            market, target_week, target_date,
            cal_signals, naver_signals, pin_signals,
        )

        # ── 3. grounding 1회 (evidence 보강) ──────────────────────────────────
        logger.info("5/8 evidence grounding 1회...")
        bundle = grounding_module.enrich_with_grounding(bundle, run_id)

        # ── 4. generation conditions (정확히 3개) ─────────────────────────────
        logger.info("6/8 generation conditions...")
        conditions = conditions_module.generate_conditions(bundle, run_id)
        logger.info("   → conditions %s", conditions)

        # ── 5. condition별 idea (fast, grounding OFF) ─────────────────────────
        logger.info("7/8 Gemini idea (fast, per-condition)...")
        raw_ideas: list[dict] = []
        for cond in conditions:
            ideas, _ = gemini_idea.generate_idea_cards(
                bundle, run_id, condition=cond, target_count=7,
            )
            for it in ideas:
                it["_condition"] = cond
            raw_ideas.extend(ideas)
        logger.info("   → raw %d개 카드 (conditions=%d)", len(raw_ideas), len(conditions))

        # ── 6. enrich ─────────────────────────────────────────────────────────
        enriched = enrich_module.enrich(raw_ideas, target_date)

        # ── 7. validate (bundle 근거 매칭 + condition 후처리) ─────────────────
        valid, invalid_count = validate_module.validate_ideas(enriched, bundle=bundle)
        logger.info("   → valid %d개 / invalid %d개", len(valid), invalid_count)

        # ── 7-1. 품질 재시도 (valid < 5 or valid_ratio < 0.4) ──────────────────
        valid_ratio = len(valid) / max(len(raw_ideas), 1)
        if len(valid) < 5 or valid_ratio < 0.4:
            logger.warning(
                "valid 비율 낮음 (%d/%d = %.0f%%) → 품질 suffix 재시도 (condition=%r)",
                len(valid), len(raw_ideas), valid_ratio * 100, conditions[0] if conditions else "",
            )
            retry_ideas, _ = gemini_idea.retry_with_quality_suffix(
                bundle, run_id, condition=conditions[0] if conditions else "", target_count=7,
            )
            for it in retry_ideas:
                it["_condition"] = conditions[0] if conditions else ""
            retry_enriched = enrich_module.enrich(retry_ideas, target_date)
            retry_valid, retry_invalid = validate_module.validate_ideas(retry_enriched, bundle=bundle)
            logger.info("   → 재시도 valid %d개 / invalid %d개", len(retry_valid), retry_invalid)

            # 재시도 결과가 더 좋으면 교체, 아니면 합산
            if len(retry_valid) > len(valid):
                valid = retry_valid
                invalid_count += retry_invalid
            else:
                # 재시도 카드 중 중복 아닌 것만 추가
                existing_titles = {v.get("idea_title") for v in valid}
                for card in retry_valid:
                    if card.get("idea_title") not in existing_titles:
                        valid.append(card)
                invalid_count += retry_invalid

            logger.info(f"   → 재시도 후 최종 valid {len(valid)}개")

        # ── 8. fallback (valid == 0 일 때만) ─────────────────────────────────
        used_fallback = False
        fb_cards: list[dict] = []
        if len(valid) == 0:
            logger.warning("valid 카드 0개 → fallback 최소 보충 실행")
            fb_cards = fallback_module.rule_based_generation(
                signals={"calendar": cal_signals, "naver_datalab": naver_signals},
                market=market,
                target_week=target_week,
                target_date=target_date,
            )
            if fb_cards:
                used_fallback = True
        elif len(valid) < 5:
            logger.info("valid 카드 %d개 (5 미만이지만 fallback 실행 안 함)", len(valid))

        # ── 9. idea_cards 저장 (내부 키 제거) ───────────────────────────────────
        logger.info("8/8 idea_cards 저장...")
        for card in valid:
            card.pop("_condition", None)

        # 메인 카드 저장
        inserted, skipped = db.upsert_idea_cards(run_id, valid, market, target_week, target_date)

        # fallback 카드는 status='fallback_review' 로 별도 저장
        fb_inserted = 0
        if fb_cards:
            for card in fb_cards:
                card.pop("_condition", None)
                card.pop("_fallback_status", None)
            fb_inserted, _ = db.upsert_idea_cards(
                run_id, fb_cards, market, target_week, target_date,
                override_status="fallback_review",
            )
            logger.info("   → fallback inserted=%d (fallback_review)", fb_inserted)
        inserted += fb_inserted
        logger.info(f"   → inserted={inserted}, skipped={skipped}")

        status = "succeeded" if inserted > 0 or skipped > 0 else "failed"

    except Exception as e:
        logger.error(f"파이프라인 실패: {e}", exc_info=True)
        db.finalize_run(run_id, status="failed", error=str(e))
        raise

    db.finalize_run(
        run_id,
        status=status,
        cards_inserted=inserted,
        cards_skipped=skipped,
        cards_invalid=invalid_count,
        used_fallback=used_fallback,
    )

    result = {
        "run_id": run_id,
        "status": status,
        "cards_inserted": inserted,
        "cards_skipped": skipped,
        "cards_invalid": invalid_count,
        "used_fallback": used_fallback,
    }
    logger.info(f"[{target_week}] 완료: {result}")
    return result


# ──────────────────────────────────────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────────────────────────────────────

def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Demand Intelligence Server — idea card 생성")
    p.add_argument("--market", default=DEFAULT_MARKET, help="마켓 코드 (default: KR)")
    p.add_argument("--target-week", help="ISO 주차 ex) 2026-W29")
    p.add_argument("--target-date", help="기준 날짜 YYYY-MM-DD (미지정 시 target-week 월요일)")
    p.add_argument("--weeks-ahead", type=int, default=WEEKS_AHEAD,
                   help=f"cron 모드: 향후 N주 생성 (default: {WEEKS_AHEAD})")
    p.add_argument("--force", action="store_true",
                   help="이미 충분한 카드가 있어도 강제 실행")
    return p.parse_args()


def main() -> int:
    args = _parse_args()

    min_cards = 0 if args.force else CARDS_MIN_PER_WEEK

    if args.target_week:
        weeks = [args.target_week]
    else:
        weeks = _upcoming_weeks(args.weeks_ahead)
        logger.info(f"cron 모드: {weeks}")

    exit_code = 0
    for week in weeks:
        try:
            result = run_pipeline(
                market=args.market,
                target_week=week,
                target_date=args.target_date if args.target_week else None,
                triggered_by="cli",
                min_cards=min_cards,
            )
            if result["status"] == "failed":
                exit_code = 1
        except Exception:
            exit_code = 1

        if len(weeks) > 1:
            import time
            time.sleep(5)  # 주차 간 RPM 여유

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
