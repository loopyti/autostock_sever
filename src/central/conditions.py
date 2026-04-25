"""conditions.py: fast 모델로 generation condition 정확히 3개 생성."""
from __future__ import annotations

import json
import logging
import re
import time
from typing import Any

from google import genai
from google.genai import types

from central import gemini_limits as gl
from central.settings import GOOGLE_AI_STUDIO_API_KEY

logger = logging.getLogger(__name__)

_DEFAULT_CONDITIONS: list[str] = [
    "공공기관 중심",
    "상업 광고 중심",
    "이벤트·행사 중심",
]

_ABSTRACT_SUBSTRINGS: frozenset[str] = frozenset({
    "콘텐츠", "마케팅", "일반", "기타",
})

_MAX_LABEL_LEN = 40

_CONDITIONS_PROMPT = """너는 스톡 이미지 시장 수요 분석가다.

TASK:
입력된 evidence bundle(calendar / naver_datalab / pinterest)을 분석해,
이번 주차의 디자인 발주 수요를 서로 다른 "수요 영역"으로 분리하는 짧은 한국어 라벨을 정확히 3개 출력하라.

핵심 원칙:
1. "수요 영역" 기준으로 분리한다
   좋은 예: 공공안전 캠페인 / 상업 리테일 프로모션 / 교육·학원 홍보 / 이벤트·행사 홍보 / 의료·보건 안내 / 지역 상권 홍보
2. buyer 이름·직업으로 나누는 것은 금지
   금지: "병원 원장 대상", "학원 원장 대상", "쇼핑몰 MD 대상" — 이건 buyer, 영역이 아님
3. 동일 상업 영역 안에서 buyer만 다르게 나누는 것 금지
   금지 예: ["쇼핑몰 광고", "브랜드 배너", "이커머스 프로모"] → 셋 다 상업 영역, 의미 없는 분리
4. 라벨에 포함 금지 단어: 콘텐츠, 마케팅, 일반, 기타
5. 각 라벨은 40자 이하, 짧고 명확하게

Few-shot 예시 (참고용 — 신호가 다르면 다른 라벨을 만들어야 함, 그대로 복사 금지):
- [2026-W29 여름·폭염·장마] → ["공공 안전·보건 캠페인", "여름 상업 프로모션", "레저·관광 이벤트"]
- [2026-W37 추석 직전] → ["명절 기념·공공 홍보", "추석 상업 프로모션", "외식·여행 행사"]
- [2026-W50 크리스마스·연말] → ["연말 공공·사회 캠페인", "홀리데이 상업 프로모션", "문화·엔터 행사"]
- [2026-W05 설날 직전] → ["명절 공공 안내", "설 상업 프로모션", "신학기·교육 홍보"]

출력 (JSON만, 설명·텍스트 없이):
{"conditions": ["라벨1", "라벨2", "라벨3"]}
"""


_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*([\s\S]*?)```", re.IGNORECASE)


def _extract_json(text: str) -> dict:
    m = _JSON_FENCE_RE.search(text)
    if m:
        return json.loads(m.group(1).strip())
    text = text.strip()
    if text.startswith("{"):
        return json.loads(text)
    raise ValueError("JSON 없음")


def _tokenize_label(s: str) -> set[str]:
    parts = re.split(r"[/·,\s]+", (s or "").strip())
    return {p for p in parts if len(p) >= 2}


def _pairwise_max_jaccard(labels: list[str]) -> float:
    if len(labels) < 2:
        return 0.0
    mx = 0.0
    for i in range(len(labels)):
        a = _tokenize_label(labels[i])
        if not a:
            continue
        for j in range(i + 1, len(labels)):
            b = _tokenize_label(labels[j])
            if not b:
                continue
            inter = len(a & b)
            union = len(a | b) or 1
            mx = max(mx, inter / union)
    return mx


def _validate_labels(labels: list[str]) -> tuple[bool, str]:
    if len(labels) != 3:
        return False, "개수!=3"
    seen: set[str] = set()
    for lab in labels:
        if not lab or len(lab) > _MAX_LABEL_LEN:
            return False, "빈라벨/길이"
        if lab in seen:
            return False, "중복"
        seen.add(lab)
        for bad in _ABSTRACT_SUBSTRINGS:
            if bad in lab:
                return False, f"추상:{bad}"
    if _pairwise_max_jaccard(labels) > 0.5:
        return False, "overlap>0.5"
    return True, ""


def _backoff_retry_fast(fn, max_retries: int = 3) -> Any:
    delays = [2, 8, 30]
    for attempt, delay in enumerate(delays[:max_retries]):
        try:
            return fn()
        except gl.RPDExhausted:
            raise
        except Exception as e:
            msg = str(e).lower()
            if any(x in msg for x in ("429", "503", "resource_exhausted", "quota", "unavailable")):
                logger.warning("conditions fast 오류 (%d/%d) %ds: %s", attempt + 1, max_retries, delay, e)
                time.sleep(delay)
            else:
                raise
    raise RuntimeError("conditions fast 재시도 초과")


def _call_fast(client: genai.Client, prompt: str) -> str:
    model = gl.fast_model_name()

    def _call():
        gl.acquire_fast_slot()
        resp = client.models.generate_content(
            model=model,
            contents=prompt,
            config=types.GenerateContentConfig(temperature=0.3),
        )
        return resp.text or ""

    return _backoff_retry_fast(_call)


def generate_conditions(bundle: dict, run_id: str) -> list[str]:
    """
    정확히 3개 condition 문자열 반환. run_id는 로깅/추적용(현재 미사용).
    """
    _ = run_id
    client = genai.Client(api_key=GOOGLE_AI_STUDIO_API_KEY)
    base = (
        f"{_CONDITIONS_PROMPT}\n---\nBUNDLE JSON\n"
        f"{json.dumps(bundle, ensure_ascii=False, indent=2)}"
    )

    def _parse_list(text: str) -> list[str]:
        data = _extract_json(text)
        raw = data.get("conditions") or data.get("labels") or data.get("generation_conditions")
        if not isinstance(raw, list):
            return []
        out: list[str] = []
        for x in raw:
            if isinstance(x, str) and x.strip():
                out.append(x.strip())
        return out[:5]

    for attempt in range(2):
        suffix = ""
        if attempt == 1:
            suffix = (
                "\n\n[RETRY] 이전 출력이 규칙 위반이었다. "
                "서로 다른 수요 영역 3개, 각 라벨 40자 이하, "
                f"다음 단어를 라벨에 포함하지 마라: {', '.join(sorted(_ABSTRACT_SUBSTRINGS))}. "
                "토큰 겹침이 큰 라벨 금지."
            )
        try:
            text = _call_fast(client, base + suffix)
            labels = _parse_list(text)
            if len(labels) > 3:
                labels = labels[:3]
            while len(labels) < 3:
                added = False
                for d in _DEFAULT_CONDITIONS:
                    if d not in labels:
                        labels.append(d)
                        added = True
                        break
                if not added:
                    break
            labels = labels[:3]
            ok, reason = _validate_labels(labels)
            if ok:
                logger.info("[conditions] %s", labels)
                return labels
            logger.warning("[conditions] 검증 실패 (%s): %s", reason, labels)
        except Exception as e:
            logger.warning("[conditions] 파싱/호출 실패: %s", e)

    logger.warning("[conditions] 기본 라벨 사용: %s", _DEFAULT_CONDITIONS)
    return list(_DEFAULT_CONDITIONS)
