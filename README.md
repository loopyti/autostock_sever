# auto_stock_db — Demand Intelligence Server

calendar / naver_datalab / pinterest 신호를 수집하고,
Gemini Google grounding으로 검증/해석한 뒤,
스톡 이미지 수요 idea card를 Supabase에 저장하는 독립 서버.

## 구조

```
signal 수집 → evidence bundle → Gemini idea card 생성 → enrich → validate → idea_cards 저장
```

## 실행

```bash
# 환경 설정
cp .env.example .env
# (키 채우기)

# 의존성 설치
uv sync

# 특정 주차 idea card 생성
uv run python -m central.run --market KR --target-week 2026-W29

# Telegram 봇 실행 (별도 터미널)
uv run python -m central.bot
```

## 테이블 (Supabase)

| 테이블 | 역할 |
|--------|------|
| `runs` | 실행 로그 (cron/bot/cli) |
| `source_signals` | calendar/naver/pinterest 1차 수집 |
| `evidence` | Gemini grounding 근거 1행씩 |
| `idea_cards` | 최종 idea card |

## 절대 금지

- 이미지 생성, 프롬프트 생성
- style_substyle_id / head_noun / cutout_risk 판단
- generic topic ("여름 건강", "장마 안전" 같은 것)
