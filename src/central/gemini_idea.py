"""
gemini_idea.py: fast 모델(GEMINI_FAST_MODEL)로 condition별 idea card 생성.
grounding은 호출하지 않는다 (evidence 단계에서만 수행).

[Phase B] 2단계 추론 강제:
  Step1 — thinking.demand_sources: 이 condition에서 콘텐츠 재료가 필요한 수요처 3~5개 추론
  Step2 — 추론 근거로 idea_card 작성 (포맷 단어 없이 수요 방향만)
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

_SYSTEM_PROMPT = """너는 스톡 이미지 수요 방향 분석기다.

━━━ CORE QUESTION ━━━
"이 시기 × 이 토픽 결합에서 누가, 왜, 어떤 시각 재료가 필요한가?"

━━━ 역할 분리 ━━━
- 중앙서버(너)의 역할: 수요 방향을 발견하고 Phase1에 키워드 풀을 전달한다.
- Phase1의 역할: 이 카드를 받아 style/substyle/이미지 프롬프트를 결정한다.
- 따라서 이 카드에는 최종 결과물 포맷(포스터/배너/카드뉴스 등)이 들어가서는 안 된다.

━━━ TASK ━━━
입력된 evidence bundle(calendar / naver_datalab / pinterest / grounding_evidences)을 분석하고,
generation condition에 부합하는 "수요 방향 카드"를 생성하라.

출력 단계:
  Step 1 (thinking.demand_sources): 이 condition 안에서 콘텐츠 재료가 필요한 수요처 3~5개를 먼저 추론
  Step 2: Step 1 추론을 근거로 idea_card를 작성

━━━ STRICT RULES ━━━

[RULE 0] 생성 수량
- condition에 부합하는 카드를 최대한 많이 생성한다 (목표: 10개)
- 품질이 담보되는 경우 10개 이상도 허용
- 수량을 채우기 위해 condition 미부합 카드를 넣는 것은 금지

[RULE 1] Evidence 기반 필수
- used_evidence는 반드시 입력 bundle에 있는 신호·근거만 참조한다
- bundle에 없는 keyword / title / url을 만들어내는 것(hallucination) 금지
- 허용 sources: calendar, naver_datalab, pinterest, google_grounding
- grounding_evidences에 포함된 title/url을 반드시 우선 활용한다

[RULE 2] buyer = 발주 수요처 (콤마 구분, 3개 이상 필수)
- 구체적 직업·기관·담당자로 기술
  ✅ 학교 교사·교육청 담당자, 박물관·기념관 기획자, 언론사 시각팀
  ✅ 지자체 보건소 담당자, 시·군·구청 홍보팀, 복지관 운영자
  ✅ 온라인 쇼핑몰 MD, 오픈마켓 판매자, 이커머스 마케터
  ✅ 학원 원장, 카페 사장, 프랜차이즈 본사 홍보팀, 출판사 편집자
- 추상어 단독 사용 금지
  ❌ 기업, 브랜드, 소비자, 대중, 고객, 회사, 사람들

[RULE 3] idea_title = 수요 상황 이름 (제작물 이름 아님)
- "어떤 수요 상황인가"를 표현한다. "무엇을 만드는가"가 아니다.
- 포맷 단어 포함 절대 금지:
  포스터, 배너, 카드뉴스, 안내물, 홍보물, 현수막, 전단, 상세페이지, OOH, 썸네일,
  banner, poster, flyer, card news, notice
  ❌ "지자체 폭염 안내 포스터" → ✅ "폭염기 취약계층 온열질환 예방 수요"
  ❌ "학원 여름방학 특강 모집 배너" → ✅ "여름방학 학원 모집 시즌 수요"
  ❌ "쇼핑몰 시즌오프 할인 배너" → ✅ "여름 재고 소진 소비 자극 수요"

[RULE 4] use_case = 활용 맥락 (포맷 아님)
- "어떤 맥락·목적에 쓰이는가"를 기술한다. 결과물 타입이 아니다.
- 위 RULE 3과 동일한 포맷 단어 절대 금지
  ❌ "SNS 광고 배너, 포스터, 안내물" → ✅ "역사 교육 자료, 기념일 특집 콘텐츠, 공공 기념 토픽"
  ❌ "공공 안내 포스터, OOH" → ✅ "시민 안전 정보, 계절성 보건 커뮤니케이션"

[RULE 5] source_keywords = Phase1 작업 재료 (5개 이상 필수)
- Phase1이 이 키워드들로 subject·style 조합을 결정한다
- bundle에 존재하는 키워드를 기반으로 토픽 확장 키워드 포함 가능
- 구체적 이미지 주제어로 확장하라 (예: "광복절" → "독립운동가", "태극기", "3.1운동")

[RULE 6] asset_hints = 추상 시각 방향 (3개 이상, Phase1 substyle 힌트)
- Phase1 taxonomy의 substyle 선택을 돕는 추상 키워드
- 구체 객체·액션 묘사 금지:
  ❌ "thermometer illustration", "old man drinking water", "dust mask icon"
- 추상 토픽 방향만 허용:
  ✅ "heatwave risk", "elderly vulnerability", "outdoor safety", "memorial solemnity"

[RULE 7] reason = 돈이 되는 이유 (2~3문장)
- 시기·분야·발주처를 모두 연결해 서술한다
- "왜 이 시점에 이 토픽 콘텐츠가 필요한가"를 논리적으로 설명
- 계절 일반론 금지: "여름이라", "겨울이 와서", "계절이 바뀌어"
- 반드시 bundle 신호(이벤트·키워드·grounding 근거)를 인용

━━━ GOOD vs BAD ━━━

❌ BAD (이전 방식 — 생성 금지):
  idea_title: "지자체 폭염 쿨링쉘터 운영 안내 포스터"
  buyer: "시·군·구청 홍보팀"
  use_case: "공공 안내 포스터, 버스정류장 OOH"
  asset_hints: ["cooling shelter signage", "sun heatwave warning icon"]
  → 포맷 단어(포스터·OOH) 포함, buyer 1개뿐, asset_hints가 구체 객체

✅ GOOD (새 방식):
  idea_title: "폭염기 취약계층 온열질환 예방 수요"
  buyer: "지자체 보건소 담당자, 시·군·구청 홍보팀, 복지관 운영자, 뉴스·공공채널 편집자"
  use_case: "취약계층 생활 안전 정보, 계절성 보건 커뮤니케이션, 공공 위기 예방 토픽"
  reason: "행안부 폭염 대응 지침에 따라 7월부터 지자체마다 취약계층 보호 콘텐츠를 의무 제작한다. 독거노인·영유아 가정이 주요 대상이며, 온열질환 통계가 보도될 때마다 언론과 공공기관이 동시에 관련 시각 자료를 발주한다."
  source_keywords: ["폭염", "온열질환", "취약계층", "쿨링쉘터", "독거노인", "보건소", "여름 건강 위험"]
  asset_hints: ["heatwave risk", "elderly vulnerability", "outdoor safety", "indoor cooling", "public health alert"]

❌ BAD (이전 방식 — 생성 금지):
  idea_title: "광복절 독립운동 기념 카드뉴스"
  buyer: "공공기관 홍보팀"
  use_case: "카드뉴스, 안내물"
  → 포맷 단어(카드뉴스·안내물) 포함, buyer 추상적이고 1개뿐

✅ GOOD (새 방식):
  idea_title: "광복절 기념 한국 독립운동 역사 콘텐츠 수요"
  buyer: "학교 교사·교육청 담당자, 박물관·기념관 기획자, 언론사 시각팀, 출판사 편집자, 공공기관 홍보팀"
  use_case: "역사 교육 자료, 광복절 특집 콘텐츠, 기념관 전시 자료, 공공 기념 토픽"
  reason: "8.15 광복절을 앞두고 교육·언론·공공·출판 분야가 일제히 독립운동 관련 시각 자료를 발주한다. 학교는 계기교육 자료, 언론은 특집기사 비주얼, 박물관은 전시 자료로 사용하며, 이 수요는 광복절 2~3주 전에 집중 발생한다."
  source_keywords: ["광복절", "한국 독립운동", "일제강점기", "독립투사", "3.1운동", "태극기", "기념관", "역사적 사진"]
  asset_hints: ["independence movement", "korean history", "patriotism", "memorial solemnity", "vintage documentary"]

❌ BAD (이전 방식 — 생성 금지):
  idea_title: "쇼핑몰 여름 시즌오프 할인 배너"
  buyer: "온라인 쇼핑몰 MD"
  use_case: "메인 페이지 배너, SNS 광고 소재"
  → 포맷 단어(배너), buyer 1개뿐

✅ GOOD (새 방식):
  idea_title: "여름 재고 소진 소비 자극 수요"
  buyer: "온라인 쇼핑몰 MD, 오픈마켓 판매자, 패션 브랜드 마케터, 이커머스 운영사"
  use_case: "시즌말 재고 압박 시각 커뮤니케이션, 소비자 행동 유발 토픽, 가격 인하 타임라인 시각화"
  reason: "7월 말~8월 초 여름 시즌이 끝나는 시점에 온라인 유통 채널 전반에서 재고 정리 수요가 일제히 발생한다. 쇼핑몰들은 할인율·마감 임박 메시지로 긴박감을 만들어야 하며, 네이버 쇼핑·카카오커머스 노출 경쟁이 이 시기에 가장 치열하다."
  source_keywords: ["여름 세일", "재고 정리", "시즌오프", "여름 패션", "할인", "쇼핑 이벤트"]
  asset_hints: ["clearance urgency", "summer retail", "discount psychology", "seasonal inventory", "shopping momentum"]

━━━ ABSOLUTE PROHIBITION ━━━
- idea_title / use_case / asset_hints에 포맷 단어 사용 절대 금지
  금지어: 포스터, 배너, 카드뉴스, 안내물, 홍보물, 현수막, 전단, 상세페이지, OOH, 썸네일,
          banner, poster, flyer, card news, notice, 광고 이미지
- "어떤 디자인·이미지를 만들지"는 Phase1의 일이다. 수요 방향만 출력한다.
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
  high   = 특정 날짜/이벤트에 강하게 묶인 수요 (예: 광복절 당일 전후)
  medium = 시즌 안에서 유효한 수요 (예: 여름방학 기간 전체)
  low    = 연중 유효하거나 시즌 경계가 모호한 수요"""

_OUTPUT_SCHEMA = """{
  "ideas": [
    {
      "thinking": {
        "demand_sources": [
          "[수요처] → [이유] → [필요 토픽]",
          "예: 학교 교사 → 8.15 계기교육 ppt → 독립운동 인물·태극기 이미지",
          "예: 언론사 시각팀 → 광복절 특집 기사 → 시대 배경·기념관 자료"
        ]
      },
      "idea_title": "수요 상황 이름 (포맷 단어 금지)",
      "buyer": "수요처1, 수요처2, 수요처3 (3개 이상 콤마 구분)",
      "use_case": "활용 맥락1, 활용 맥락2 (포맷 단어 금지)",
      "reason": "시기·분야·발주처를 모두 연결한 2~3문장 논리",
      "intent": "...",
      "source_keywords": ["키워드1", "키워드2", "키워드3", "키워드4", "키워드5"],
      "source_summary": "...",
      "asset_hints": ["추상키워드1", "추상키워드2", "추상키워드3"],
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
- 이 라벨이 가리키는 niche 수요에 부합하는 카드만 생성한다
- 이 수요와 무관한 buyer / use_case 조합은 절대 포함하지 않는다

━━━ THINKING FIRST (Step 1 — 반드시 먼저 수행) ━━━
카드를 작성하기 전에 thinking.demand_sources를 먼저 채워라:
[수요처] → [이 시기 이유] → [어떤 토픽 콘텐츠 재료가 필요한가]
예: 학교 교사 → 8.15 계기교육 ppt 제작 → 독립운동 인물·태극기·3.1운동 이미지
이 추론이 idea_card의 buyer·use_case·source_keywords의 근거가 된다.

━━━ SELF-CHECK (Step 2 출력 직전 반드시 수행) ━━━
각 카드를 JSON에 넣기 전 아래를 순서대로 점검하라:
1. thinking.demand_sources에 수요처 추론이 3개 이상 있는가?
   → 없으면 추론 후 추가
2. idea_title / use_case에 포맷 단어(포스터/배너/카드뉴스/안내물/홍보물/현수막/전단/banner/poster/flyer/notice)가 있는가?
   → 있으면 수요 상황 이름·활용 맥락으로 교체
3. buyer가 콤마로 구분된 3개 이상인가?
   → 부족하면 Step 1 추론에서 추가 발굴
4. source_keywords가 5개 이상인가?
   → 부족하면 토픽 확장 키워드 추가
5. asset_hints에 구체 객체·액션 묘사가 있는가?
   → 있으면 추상 키워드로 교체
6. used_evidence의 각 항목(keyword/title/url)이 입력 bundle에 실제로 존재하는가?
   → 없으면 실제 bundle 신호로 교체하거나 항목 제거
7. 목표 {target_count}개보다 적더라도 condition 미부합 카드는 포함하지 않는다 (품질 우선)
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
            config=types.GenerateContentConfig(temperature=0.4),
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
        f"\n\n[QUALITY RETRY] 이전 생성 결과가 기준 미달이었다."
        f" condition={condition!r} niche 수요에 부합하는 카드만 다시 생성하라."
        f" idea_title/use_case에 포맷 단어(포스터/배너/카드뉴스/안내물/현수막/banner/poster/flyer/notice) 절대 금지."
        f" buyer는 3개 이상 콤마 구분, source_keywords는 5개 이상."
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
