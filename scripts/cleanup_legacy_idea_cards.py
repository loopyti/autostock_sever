#!/usr/bin/env python3
"""
구 파이프라인(Phase B 이전) idea_cards 정리.

삭제 대상 (market=KR, status=pending 만):
- idea_title 또는 use_case에 최종 포맷 단어 포함 (포스터/배너/카드뉴스 등)
- source_keywords 개수 < 2
- idea_title이 \"… 관련 …\" 템플릿 패턴 (구 fallback 스타일)

사용:
  uv run python scripts/cleanup_legacy_idea_cards.py          # dry-run (삭제 안 함)
  uv run python scripts/cleanup_legacy_idea_cards.py --execute
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

# 프로젝트 루트에서 .env 로드
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(_ROOT / "src"))


def _load_dotenv() -> None:
    env_path = _ROOT / ".env"
    if not env_path.is_file():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("----"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            continue
        k, _, v = line.partition("=")
        k, v = k.strip(), v.strip().strip('"').strip("'")
        if k and k not in os.environ:
            os.environ[k] = v


_TEMPLATE_TITLE_RE = re.compile(r".+\s관련\s.+")

_FORBIDDEN_KO = (
    "포스터",
    "배너",
    "카드뉴스",
    "안내물",
    "홍보물",
    "현수막",
    "전단",
    "상세페이지",
    "썸네일",
    "프로모션 배너",  # 복합
    "SNS 광고",
)

_FORBIDDEN_EN = ("banner", "poster", "flyer", "notice", "ooh")


def _is_legacy(card: dict) -> tuple[bool, str]:
    title = (card.get("idea_title") or "").strip()
    use = (card.get("use_case") or "").strip()
    blob = f"{title} {use}"
    blob_lower = blob.lower()

    if _TEMPLATE_TITLE_RE.match(title):
        return True, "template_관련"

    for w in _FORBIDDEN_KO:
        if w in blob:
            return True, f"ko:{w}"

    for w in _FORBIDDEN_EN:
        if w in blob_lower:
            return True, f"en:{w}"

    sk = card.get("source_keywords") or []
    if not isinstance(sk, list):
        sk = []
    if len(sk) < 2:
        return True, "keywords<2"

    return False, ""


def main() -> int:
    _load_dotenv()
    parser = argparse.ArgumentParser()
    parser.add_argument("--execute", action="store_true", help="실제 삭제 수행")
    parser.add_argument("--market", default="KR")
    args = parser.parse_args()

    from central import db

    client = db.get_client()
    market = args.market

    # pending 전부 페이지네이션 (range)
    page_size = 500
    offset = 0
    all_rows: list[dict] = []
    while True:
        q = (
            client.table("idea_cards")
            .select("id,idea_title,use_case,source_keywords,status,target_week")
            .eq("market", market)
            .eq("status", "pending")
            .order("created_at", desc=True)
        )
        res = q.range(offset, offset + page_size - 1).execute()
        batch = res.data or []
        if not batch:
            break
        all_rows.extend(batch)
        if len(batch) < page_size:
            break
        offset += page_size

    legacy: list[tuple[str, str, str]] = []  # (id, reason, title_snip)
    for row in all_rows:
        ok, reason = _is_legacy(row)
        if ok:
            title = (row.get("idea_title") or "")[:60]
            legacy.append((row["id"], reason, title))

    print(f"market={market} pending 총 {len(all_rows)}건")
    print(f"삭제 대상(구 스타일): {len(legacy)}건")

    by_reason: dict[str, int] = {}
    for _, reason, _ in legacy:
        by_reason[reason] = by_reason.get(reason, 0) + 1
    for r, n in sorted(by_reason.items(), key=lambda x: -x[1]):
        print(f"  {r}: {n}")

    for i, (_, _, t) in enumerate(legacy[:15]):
        print(f"  예시: {t}...")

    if not args.execute:
        print("\n[DRY-RUN] 삭제 안 함. 실행하려면: --execute")
        return 0

    ids = [x[0] for x in legacy]
    deleted = 0
    batch = 80
    for i in range(0, len(ids), batch):
        chunk = ids[i : i + batch]
        client.table("idea_cards").delete().in_("id", chunk).execute()
        deleted += len(chunk)
        print(f"삭제 진행: {deleted}/{len(ids)}")

    print(f"완료: 삭제 {deleted}건")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
