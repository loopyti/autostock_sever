"""
gemini_idea.py: fast 모델(GEMINI_FAST_MODEL)로 condition별 idea card 생성.
grounding은 호출하지 않는다 (evidence 단계에서만 수행).
"""
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

_SYSTEM_PROMPT = """너는 스톡 이미지 디자인 발주 상황 추론기다.

━━━ CORE QUESTION ━━━
"이 시기, 이 수요 영역에서 실제로 누가 어떤 디자인을 발주하는가?"

━━━ TASK ━━━
입력된 evidence bundle(calendar / naver_datalab / pinterest / grounding_evidences)을 분석하고,
generation condition에 부합하는 "실제 디자인 발주 수요 카드"를 생성하라.
이미지를 만드는 것이 아니라, 누가 어디에 스톡 이미지를 사는 상황인지를 추론한다.

━━━ STRICT RULES ━━━

[RULE 0] 생성 수량
- condition에 부합하는 카드를 최대한 많이 생성한다 (목표: 10개)
- 품질이 담보되는 경우 10개 이상도 허용
- quality < quantity: 수량 채우기 위해 condition 위반 카드를 넣는 것은 금지

[RULE 1] Evidence 기반 필수
- used_evidence는 반드시 입력 bundle에 있는 신호·근거만 참조한다
- bundle에 없는 keyword / title / url을 만들어내는 것(hallucination) 금지
- 허용 sources: calendar, naver_datalab, pinterest, google_grounding
- grounding_evidences에 포함된 title/url은 반드시 우선 활용한다

[RULE 2] buyer = 실제 발주 주체 (직업 또는 조직으로 구체화)
허용:
  ✅ 시·군·구청 홍보팀, 지자체 보건소 담당자, 공공기관 홍보 담당자
  ✅ 온라인 쇼핑몰 MD, 오픈마켓 판매자, 이커머스 마케터
  ✅ 여행사 마케팅 담당자, 숙박업소 운영자
  ✅ 학원 원장, 교육기업 마케터
  ✅ 프랜차이즈 본사 홍보팀, 외식업 브랜드 담당자, 카페 사장
  ✅ 헬스장 운영자, 스포츠 브랜드 마케터
  ✅ 부동산 중개업소, 인테리어 업체
금지:
  ❌ 기업, 브랜드, 사람들, 사용자, 소비자, 대중, 고객, 회사

[RULE 3] use_case = 구체적 결과물 타입을 반드시 포함
포함 필수 (하나 이상): 배너, 카드뉴스, 포스터, SNS 광고, 홍보물, 안내물, 현수막, 썸네일, OOH, 전단, 상세페이지
금지 단독 사용: "가이드", "정보 제공", "소개", "추천", "정보"

[RULE 4] reason = 발주 트리거 (한 문장)
- "왜 이 시점에 이 디자인 발주가 발생하는가?"
- 반드시 bundle의 신호(이벤트명 / 키워드 / grounding 근거)를 연결해 설명
- 계절 일반론 금지: "여름이라", "겨울이 와서", "계절이 바뀌어"

[RULE 5] asset_hints ≥ 2개 (시각 요소를 구체적으로, 영어 또는 한국어)

[RULE 6] source_keywords ≥ 2개 (bundle에 존재하는 키워드)

━━━ GOOD vs BAD ━━━

❌ BAD (생성 금지):
  idea_title: "여름철 건강 관리 카드뉴스"
  buyer: "기업", use_case: "건강 정보 제공"
  → 추상 buyer, 결과물 타입 없음, 계절 일반론, evidence 없음

❌ BAD:
  idea_title: "여름철 차량 관리 안내"
  → 스톡 이미지 발주 상황 아님, evidence 없음

✅ GOOD:
  idea_title: "지자체 폭염 쿨링쉘터 운영 안내 포스터"
  buyer: "시·군·구청 홍보팀"
  use_case: "공공 안내 포스터, 버스정류장 OOH"
  reason: "행안부 폭염 대응 지침 시행으로 각 지자체가 쿨링쉘터 위치 안내물을 의무 제작"
  used_evidence: [{"source":"calendar","keyword":"폭염"}, {"source":"google_grounding","title":"행안부 폭염 대응 지침","url":"..."}]
  asset_hints: ["cooling shelter signage", "sun heatwave warning icon", "location map pin"]

✅ GOOD:
  idea_title: "학원 여름방학 특강 모집 배너"
  buyer: "학원 원장, 교육기업 마케터"
  use_case: "온라인 배너 광고, SNS 피드 광고"
  reason: "방학 시작 전 4~6주에 학원들이 여름방학 특강 수강생 모집 광고를 집중 집행하는 시기"
  used_evidence: [{"source":"calendar","keyword":"여름방학"}, {"source":"naver_datalab","keyword":"여름방학 특강"}]
  asset_hints: ["classroom chalkboard background", "student studying illustration", "registration CTA badge"]

✅ GOOD:
  idea_title: "쇼핑몰 여름 시즌오프 할인 배너"
  buyer: "온라인 쇼핑몰 MD"
  use_case: "메인 페이지 배너, SNS 광고 소재"
  reason: "7월 말 여름 재고 소진 시즌에 온라인 쇼핑몰들이 일제히 시즌오프 프로모션을 집행하는 시기"
  used_evidence: [{"source":"naver_datalab","keyword":"여름 세일"}, {"source":"pinterest","keyword":"summer sale"}]
  asset_hints: ["sale percentage badge red", "summer fashion product flatlay", "shopping bag vector"]

━━━ ABSOLUTE PROHIBITION ━━━
- image_prompt 생성 금지
- item_title 생성 금지
- style / substyle 선택 금지
- head_noun / cutout_risk 판단 금지
- 계절 일반론("여름 건강", "겨울 대비", "봄맞이") 생성 금지
- bundle에 없는 keyword / title / url 생성 금지

━━━ CLASSIFICATION ━━━
industry:
  government_public | ecommerce | travel | education | healthcare |
  food_beverage | fashion | real_estate | finance | entertainment |
  sports_outdoor | beauty | pet | it_tech | other

date_specificity:
  high   = 특정 날짜/이벤트에 강하게 묶인 수요 (예: 추석 당일 전후)
  medium = 시즌 안에서 유효한 수요 (예: 여름방학 기간 전체)
  low    = 연중 유효하거나 시즌 경계가 모호한 수요"""

_OUTPUT_SCHEMA = """{
  "ideas": [
    {
      "idea_title": "...",
      "buyer": "...",
      "use_case": "...",
      "reason": "...",
      "intent": "...",
      "source_keywords": ["..."],
      "source_summary": "...",
      "asset_hints": ["..."],
      "used_evidence": [
        {"source": "naver_datalab", "keyword": "..."},
        {"source": "calendar", "keyword": "..."},
        {"source": "google_grounding", "title": "...", "url": "..."}
      ],
      "event_date": "YYYY-MM-DD or null",
      "lead_start_days": 7,
      "lead_end_days": 30,
      "industry": "other",
      "date_specificity": "medium"
    }
  ]
}"""

_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*([\s\S]*?)```", re.IGNORECASE)


def _extract_json(text: str) -> dict:
    m = _JSON_FENCE_RE.search(text)
    if m:
        return json.loads(m.group(1).strip())
    text = text.strip()
    if text.startswith("{"):
        return json.loads(text)
    raise ValueError("응답에서 JSON을 찾을 수 없습니다.")


def _build_prompt(bundle: dict, condition: str, target_count: int) -> str:
    signals_json = json.dumps(bundle, ensure_ascii=False, indent=2)
    cond_block = f"""
━━━ GENERATION CONDITION (이번 호출 한정) ━━━
라벨: {condition}
- 이 라벨이 가리키는 수요 영역에 부합하는 idea만 생성한다
- 이 영역과 무관한 buyer / use_case 조합은 절대 포함하지 않는다
  (예: condition="공공 안전·보건 캠페인"이면 → 쇼핑몰 배너·학원 모집 광고 생성 금지)

━━━ SELF-CHECK (출력 직전 반드시 수행) ━━━
각 카드를 JSON에 넣기 전 아래 5가지를 순서대로 점검하라:
1. buyer / use_case가 "{condition}" 수요 영역에 부합하는가?
   → 부합하지 않으면 해당 카드 제거
2. used_evidence의 각 항목(keyword / title / url)이 입력 bundle에 실제로 존재하는가?
   → bundle에 없는 내용이면 실제 bundle 신호로 교체하거나 항목 제거
3. buyer가 "기업", "브랜드", "소비자" 등 단독 추상어인가?
   → 해당하면 구체적 직업/조직명으로 교체하거나 카드 제거
4. asset_hints가 1개 이하인가?
   → 해당하면 시각 요소 1~2개 더 추가
5. 목표 {target_count}개보다 적더라도 condition 위반 카드는 포함하지 않는다 (품질 우선)
"""
    return (
        f"{_SYSTEM_PROMPT}\n\n{cond_block}\n"
        f"---\nEVIDENCE BUNDLE JSON\n{signals_json}\n\n"
        f"---\nOUTPUT JSON SCHEMA (이 스키마로만 출력)\n{_OUTPUT_SCHEMA}"
    )


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
                logger.warning("idea fast 오류 (%d/%d) %ds: %s", attempt + 1, max_retries, delay, e)
                time.sleep(delay)
            else:
                raise
    raise RuntimeError("idea fast 재시도 초과")


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


def generate_idea_cards(
    bundle: dict,
    run_id: str,
    condition: str,
    target_count: int = 10,
) -> tuple[list[dict], list]:
    """
    grounding 미사용. 반환 (ideas, []) — 두 번째는 하위 호환용 빈 리스트.
    """
    _ = run_id
    client = genai.Client(api_key=GOOGLE_AI_STUDIO_API_KEY)
    prompt = _build_prompt(bundle, condition=condition, target_count=target_count)

    try:
        raw_text = _call_fast(client, prompt)
        parsed = _extract_json(raw_text)
        ideas = parsed.get("ideas", [])
    except (json.JSONDecodeError, ValueError):
        logger.warning("JSON 파싱 실패 — strict 재시도")
        ideas = _retry_strict_json(client, bundle, condition, target_count)
    except Exception as e:
        logger.error("idea 생성 실패: %s", e)
        ideas = []

    return ideas, []


def _retry_strict_json(
    client: genai.Client,
    bundle: dict,
    condition: str,
    target_count: int,
) -> list[dict]:
    suffix = "\n\n[IMPORTANT] 유효한 JSON만. 마크다운/설명 없이 JSON만."
    prompt = _build_prompt(bundle, condition, target_count) + suffix
    try:
        raw_text = _call_fast(client, prompt)
        parsed = _extract_json(raw_text)
        return parsed.get("ideas", [])
    except Exception as e:
        logger.error("strict 재시도 실패: %s", e)
        return []


def retry_with_quality_suffix(
    bundle: dict,
    run_id: str,
    *,
    condition: str,
    target_count: int = 10,
) -> tuple[list[dict], list]:
    """valid 부족 시 동일 condition으로 품질 suffix 재시도."""
    _ = run_id
    client = genai.Client(api_key=GOOGLE_AI_STUDIO_API_KEY)
    quality_suffix = (
        f"\n\n[QUALITY RETRY] 이전 생성 결과에 generic 카드가 너무 많았다."
        f" condition={condition!r} 영역에 부합하는 카드만 다시 생성하라."
        f" buyer는 실제 직업/조직명으로, use_case에 결과물 타입(배너/포스터/카드뉴스 등) 포함 필수."
        f" used_evidence는 bundle에 실제 있는 신호만."
    )
    prompt = _build_prompt(bundle, condition, target_count) + quality_suffix
    try:
        raw_text = _call_fast(client, prompt)
        parsed = _extract_json(raw_text)
        return parsed.get("ideas", []), []
    except Exception as e:
        logger.error("품질 재시도 실패: %s", e)
        return [], []
