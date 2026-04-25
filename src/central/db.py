"""Supabase 클라이언트 + runs/source_signals/evidence/idea_cards CRUD 헬퍼."""
from __future__ import annotations

import hashlib
import re
import unicodedata
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from supabase import Client, create_client

from central.settings import SUPABASE_SERVICE_KEY, SUPABASE_URL

# ──────────────────────────────────────────────────────────────────────────────
# 클라이언트 싱글톤
# ──────────────────────────────────────────────────────────────────────────────

_client: Client | None = None


def get_client() -> Client:
    global _client
    if _client is None:
        _client = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)
    return _client


# ──────────────────────────────────────────────────────────────────────────────
# runs
# ──────────────────────────────────────────────────────────────────────────────

def create_run(
    market: str,
    target_week: str,
    target_date: str | None,
    triggered_by: str = "cli",
) -> str:
    """runs 행 생성 → run_id 반환."""
    db = get_client()
    row = {
        "market": market,
        "target_week": target_week,
        "target_date": target_date,
        "triggered_by": triggered_by,
        "status": "running",
    }
    result = db.table("runs").insert(row).execute()
    return result.data[0]["id"]


def finalize_run(
    run_id: str,
    *,
    status: str,
    cards_inserted: int = 0,
    cards_skipped: int = 0,
    cards_invalid: int = 0,
    used_fallback: bool = False,
    raw_response: dict | None = None,
    error: str | None = None,
) -> None:
    db = get_client()
    db.table("runs").update({
        "status": status,
        "cards_inserted": cards_inserted,
        "cards_skipped": cards_skipped,
        "cards_invalid": cards_invalid,
        "used_fallback": used_fallback,
        "raw_response": raw_response,
        "error": error,
        "ended_at": _now_iso(),
    }).eq("id", run_id).execute()


def get_recent_runs(limit: int = 5) -> list[dict]:
    db = get_client()
    return db.table("runs").select("*").order("started_at", desc=True).limit(limit).execute().data


def count_existing_cards(market: str, target_week: str) -> int:
    """해당 주차에 이미 저장된 카드 수."""
    db = get_client()
    result = (
        db.table("idea_cards")
        .select("id", count="exact")
        .eq("market", market)
        .eq("target_week", target_week)
        .execute()
    )
    return result.count or 0


# ──────────────────────────────────────────────────────────────────────────────
# source_signals
# ──────────────────────────────────────────────────────────────────────────────

def save_source_signals(run_id: str, signals: dict[str, Any]) -> None:
    """
    signals: {
      "calendar": [...],
      "naver_datalab": [...],
      "pinterest": [...]
    }
    """
    db = get_client()
    rows = []
    for source, raw in signals.items():
        rows.append({
            "run_id": run_id,
            "source": source,
            "market": signals.get("_market", "KR"),
            "target_week": signals.get("_target_week", ""),
            "raw": raw if isinstance(raw, dict) else {"items": raw},
        })
    if rows:
        db.table("source_signals").insert(rows).execute()


# ──────────────────────────────────────────────────────────────────────────────
# evidence
# ──────────────────────────────────────────────────────────────────────────────

def save_evidence_chunks(run_id: str, chunks: list[dict]) -> None:
    """
    chunks: Gemini grounding_metadata.grounding_chunks 파싱 결과.
    각 chunk: {source, title, url, snippet, raw}
    """
    db = get_client()
    rows = [{"run_id": run_id, **chunk} for chunk in chunks]
    if rows:
        db.table("evidence").insert(rows).execute()


# ──────────────────────────────────────────────────────────────────────────────
# idea_cards
# ──────────────────────────────────────────────────────────────────────────────

def build_dedupe_key(idea_title: str, buyer: str, market: str, target_week: str) -> str:
    normalized = _normalize_text(idea_title) + _normalize_text(buyer) + market + target_week
    return hashlib.sha1(normalized.encode()).hexdigest()


def _normalize_text(text: str) -> str:
    """공백/특수문자 제거 후 소문자화."""
    text = unicodedata.normalize("NFC", text or "")
    text = re.sub(r"\s+", "", text).lower()
    return text


def upsert_idea_cards(
    run_id: str,
    cards: list[dict],
    market: str,
    target_week: str,
    target_date: str | None,
    override_status: str | None = None,
) -> tuple[int, int]:
    """
    idea cards를 upsert. dedupe_key 충돌 시 skip.
    override_status: None이면 'pending', 지정하면 해당 값 사용 (예: 'fallback_review')
    Returns: (inserted, skipped)
    """
    db = get_client()
    inserted = 0
    skipped = 0

    for card in cards:
        dedupe_key = build_dedupe_key(
            card.get("idea_title", ""),
            card.get("buyer", ""),
            market,
            target_week,
        )
        row = {
            "run_id": run_id,
            "market": market,
            "target_week": target_week,
            "target_date": target_date or card.get("event_date"),
            "event_date": card.get("event_date"),
            "lead_start_days": card.get("lead_start_days", 7),
            "lead_end_days": card.get("lead_end_days", 30),
            "idea_title": card["idea_title"],
            "buyer": card["buyer"],
            "use_case": card["use_case"],
            "reason": card.get("reason"),
            "intent": card.get("intent"),
            "source_keywords": card.get("source_keywords", []),
            "source_summary": card.get("source_summary"),
            "asset_hints": card.get("asset_hints", []),
            "used_evidence": card.get("used_evidence", []),
            "industry": card.get("industry"),
            "date_specificity": card.get("date_specificity"),
            "date_weight": card.get("date_weight"),
            "weight": card.get("weight"),
            "status": override_status if override_status else "pending",
            "dedupe_key": dedupe_key,
        }
        try:
            result = (
                db.table("idea_cards")
                .insert(row)
                .execute()
            )
            if result.data:
                inserted += 1
            else:
                skipped += 1
        except Exception as e:
            if "duplicate" in str(e).lower() or "unique" in str(e).lower():
                skipped += 1
            else:
                raise

    return inserted, skipped


def fetch_pending_cards(market: str, target_week: str) -> list[dict]:
    db = get_client()
    return (
        db.table("idea_cards")
        .select("*")
        .eq("market", market)
        .eq("target_week", target_week)
        .eq("status", "pending")
        .order("weight", desc=True)
        .execute()
        .data
    )


def update_card_status(card_id: str, status: str) -> None:
    db = get_client()
    db.table("idea_cards").update({"status": status}).eq("id", card_id).execute()


# ──────────────────────────────────────────────────────────────────────────────
# 유틸
# ──────────────────────────────────────────────────────────────────────────────

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
