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
    "연례 시즌 소비·선물 수요",
    "시즌말 상업 재고 압박 수요",
    "계절 전환기 보건·환경 리스크 수요",
]

_ABSTRACT_SUBSTRINGS: frozenset[str] = frozenset({
    "콘텐츠", "마케팅", "일반", "기타",
})

# 넓은 카테고리·결과물 암시 라벨 금지 (시기×토픽 niche 수요만)
_BROAD_LABEL_SUBSTRINGS: frozenset[str] = frozenset({
    "프로모션", "캠페인", "안내", "홍보", "광고",
})

_MAX_LABEL_LEN = 40

_CONDITIONS_PROMPT = """너는 스톡 이미지 시장 수요 분석가다.

TASK:
입력된 evidence bundle(calendar / naver_datalab / pinterest / grounding_evidences)을 분석해,
이번 주차에 서로 다른 "시기 × 토픽 결합 niche 수요"를 나타내는 한국어 라벨을 정확히 3개 출력하라.
각 라벨은 Phase1에 넘길 generation condition으로만 쓰이며 DB에 저장되지 않는다.

핵심 원칙:
1. 라벨은 반드시 "…수요"로 끝낸다 (예: 폭염기 취약계층 생활 안전 수요).
2. 넓은 카테고리·결과물 암시 라벨 금지
   나쁜 예: 시즌 상업 프로모션, 공공 안내, 환경·보건 캠페인, 명절 홍보, SNS 광고
   좋은 예: 폭염 취약계층 생활 안전 수요, 하반기 채용 준비 자료 수요, 여름 재고 소진 소비 자극 수요
3. 라벨에 포함 금지 단어(부분 일치도 금지): 프로모션, 캠페인, 안내, 홍보, 광고
4. 라벨에 포함 금지 단어: 콘텐츠, 마케팅, 일반, 기타
5. buyer 이름·직업으로 나누는 것 금지
   금지: "병원 원장 대상", "학원 원장 대상", "쇼핑몰 MD 대상"
6. 동일 상업 토픽 안에서 표현만 다르게 나누는 것 금지
7. 각 라벨 40자 이하, 짧고 구체적으로 (시기 또는 이벤트 + 구체 토픽 + 수요)

Few-shot (참고만, 신호에 맞게 새로 쓸 것, 복사 금지):
- [2026-W29 여름·폭염·장마] → ["폭염기 취약계층 온열질환 예방 수요", "여름철 실내 냉방 건강 리스크 수요", "장마철 도시 침수·안전 정보 수요"]
- [2026-W33 광복절·8월] → ["광복절 기념 독립운동 역사 교육 수요", "8월 여름휴가철 가족 돌봄 공백 수요", "하반기 채용 시즌 인재 유치 수요"]
- [2026-W37 추석 직전] → ["추석 명절 이동·교통 혼잡 정보 수요", "명절 선물·소비 트렌드 시각 수요", "가을 전환기 건강·식생활 리스크 수요"]

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
        if not lab.endswith("수요"):
            return False, "수요접미"
        for bad in _ABSTRACT_SUBSTRINGS:
            if bad in lab:
                return False, f"추상:{bad}"
        for broad in _BROAD_LABEL_SUBSTRINGS:
            if broad in lab:
                return False, f"넓은라벨:{broad}"
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
            abstract = ", ".join(sorted(_ABSTRACT_SUBSTRINGS))
            broad = ", ".join(sorted(_BROAD_LABEL_SUBSTRINGS))
            suffix = (
                "\n\n[RETRY] 이전 출력이 규칙 위반이었다. "
                "서로 다른 niche 수요 라벨 3개, 각 라벨은 반드시 '수요'로 끝낼 것, 40자 이하. "
                f"포함 금지(추상): {abstract}. "
                f"포함 금지(넓은 카테고리·결과물 암시): {broad}. "
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
