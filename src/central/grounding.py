"""
grounding.py: evidence bundle에 Google Search grounding을 1회만 적용해
grounding_evidences를 채운다. idea 생성 단계에서는 grounding을 호출하지 않는다.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any

from google import genai
from google.genai import types

from central import db
from central import gemini_limits as gl
from central.settings import GOOGLE_AI_STUDIO_API_KEY

logger = logging.getLogger(__name__)

_GROUNDING_PROMPT = """너는 한국 시장 스톡 이미지 수요 분석가다.

TASK:
아래 신호 묶음(calendar / naver_datalab / pinterest)을 보고,
Google Search를 통해 이 신호들이 실제 어떤 뉴스·정책·상업 활동과 연결되는지 검증하라.

검색 우선순위:
1. calendar 이벤트/기념일 → 정부·지자체 정책 발표, 공식 행사 공고, 법정 기념일 연계 캠페인
2. naver_datalab 상위·상승 키워드 → 최근 뉴스 보도, 산업 동향, 기업 홍보 캠페인
3. pinterest 트렌드 키워드 → 글로벌 비주얼 트렌드, 한국 상업 적용 사례

검색 목적:
스톡 이미지 구매로 이어지는 실제 "디자인 발주 수요"를 유발하는 팩트를 확인한다.
- 정책 시행 → 공공기관 안내물 수요
- 시즌 행사 공고 → 행사 홍보물 수요
- 유통·쇼핑 이벤트 → 프로모션 배너 수요
- 사회 이슈 뉴스 → 관련 단체·기관의 안내 콘텐츠 수요

출력 (JSON만, 다른 텍스트 없이):
{
  "summary": "이번 주차 핵심 상업 수요 배경 1~2문단",
  "notes": ["확인된 이벤트/정책/캠페인 3~7개 (각 1줄)"]
}
"""


def _backoff_retry(fn, max_retries: int = 3) -> Any:
    delays = [2, 8, 30]
    last_exc = None
    for attempt, delay in enumerate(delays[:max_retries]):
        try:
            return fn()
        except gl.RPDExhausted:
            raise
        except Exception as e:
            msg = str(e).lower()
            is_retryable = any(x in msg for x in (
                "429", "503", "resource_exhausted", "quota",
                "unavailable", "high demand", "overloaded",
            ))
            if is_retryable:
                logger.warning(
                    "grounding 일시 오류 (시도 %d/%d) — %ds 대기: %s",
                    attempt + 1, max_retries, delay, e,
                )
                time.sleep(delay)
                last_exc = e
            else:
                raise
    raise RuntimeError("grounding 호출 최대 재시도 초과") from last_exc


def _parse_grounding_chunks(response) -> list[dict]:
    chunks: list[dict] = []
    try:
        gm = response.candidates[0].grounding_metadata
        if not gm:
            return []
        for chunk in (gm.grounding_chunks or []):
            web = getattr(chunk, "web", None)
            if web:
                chunks.append({
                    "source": "google_grounding",
                    "title": getattr(web, "title", None),
                    "url": getattr(web, "uri", None),
                    "snippet": None,
                    "raw": {"title": getattr(web, "title", None), "uri": getattr(web, "uri", None)},
                })
    except Exception:
        pass
    return chunks


def _call_grounding_with_fallback(client: genai.Client, prompt: str) -> Any:
    last_exc: Exception | None = None
    for cfg in gl.grounding_model_chain():
        model = cfg["name"]
        rpm = int(cfg["rpm"])
        rpd = int(cfg["rpd"])
        logger.info("grounding 모델 시도: %s (RPM %d, RPD %d)", model, rpm, rpd)

        def _call(m=model, r=rpm, d=rpd):
            gl.acquire_grounding_slot(m, r, d)
            return client.models.generate_content(
                model=m,
                contents=prompt,
                config=types.GenerateContentConfig(
                    tools=[types.Tool(google_search=types.GoogleSearch())],
                    temperature=0.2,
                ),
            )

        try:
            return _backoff_retry(_call)
        except gl.RPDExhausted as e:
            logger.warning("grounding RPD 소진 — 다음 모델: %s", e)
            last_exc = e
        except RuntimeError as e:
            logger.warning("grounding 모델 %s 실패 — 다음: %s", model, e)
            last_exc = e

    raise RuntimeError("모든 grounding 모델 소진 또는 실패") from last_exc


def enrich_with_grounding(bundle: dict, run_id: str) -> dict:
    """
    bundle에 grounding_evidences(list)를 주입해 반환.
    실패 시 빈 리스트 + 경고 로그 (파이프라인은 계속).
    """
    out = dict(bundle)
    out.setdefault("grounding_evidences", [])

    client = genai.Client(api_key=GOOGLE_AI_STUDIO_API_KEY)
    signals_json = json.dumps(bundle, ensure_ascii=False, indent=2)
    prompt = f"{_GROUNDING_PROMPT}\n---\nSIGNALS JSON\n{signals_json}"

    try:
        response = _call_grounding_with_fallback(client, prompt)
    except Exception as e:
        logger.warning("grounding 호출 실패 (빈 근거로 진행): %s", e)
        return out

    chunks = _parse_grounding_chunks(response)
    if chunks:
        try:
            db.save_evidence_chunks(run_id, chunks)
        except Exception as e:
            logger.warning("evidence DB 저장 실패 (무시): %s", e)

    evidences: list[dict] = []
    for c in chunks:
        evidences.append({
            "title": c.get("title") or "",
            "url": c.get("url") or "",
            "summary": (c.get("snippet") or "") or "",
            "ref_keyword": None,
        })
    out["grounding_evidences"] = evidences
    logger.info("grounding 완료: chunks=%d", len(evidences))
    return out
