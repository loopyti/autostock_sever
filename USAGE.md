# 사용법 — Demand Intelligence (idea cards)

프로젝트 루트에서 `uv`로 실행한다. Python **3.11+** 가정.

## 1. 준비

```bash
cd /path/to/auto_stock_db
uv sync
```

프로젝트 루트에 `.env`를 두면 `central.settings`가 자동 로드한다.

### 필수 환경변수

| 변수 | 설명 |
|------|------|
| `SUPABASE_URL` | Supabase 프로젝트 URL |
| `SUPABASE_SERVICE_KEY` | 서비스 롤 키 (서버용) |
| `GOOGLE_AI_STUDIO_API_KEY` | Google AI Studio API 키 |

### 선택 (신호 품질·그라운딩 보강)

| 변수 | 설명 |
|------|------|
| `NAVER_DATALAB_CLIENT_ID` / `NAVER_DATALAB_CLIENT_SECRET` | 네이버 DataLab |
| Pinterest 관련 토큰·클라이언트 | `PINTEREST_*` (자세한 키는 `src/central/settings.py` 참고) |
| `DATA_GO_KR_API_KEY` | 공휴일 등 data.go.kr |
| `GEMINI_FAST_MODEL` | 미지정 시 `gemini-3.1-flash-lite-preview` |

### 텔레그램 (봇 long-polling 쓸 때)

| 변수 | 설명 |
|------|------|
| `TELEGRAM_BOT_TOKEN` | [@BotFather](https://t.me/BotFather)에서 발급한 봇 토큰 |
| `TELEGRAM_ADMIN_CHAT_IDS` | 허용할 **숫자 chat_id** 여러 개면 쉼표 구분 (비우면 **모든 채팅 허용** — 운영에서는 반드시 지정) |

`chat_id` 확인: 봇에게 `/start` 보낸 뒤 `https://api.telegram.org/bot<TOKEN>/getUpdates` 로 `message.chat.id` 조회하거나, [@userinfobot](https://t.me/userinfobot) 등으로 본인 id 확인.

## 2. 메인 CLI — 주차별 idea card 생성

모듈 진입점:

```bash
uv run python -m central.run [옵션]
```

### 자주 쓰는 예시

**특정 ISO 주 + 기준일** (기념일·캠페인 정렬에 `target_date` 권장):

```bash
uv run python -m central.run --target-week 2026-W33 --target-date 2026-08-15 --force
```

- `--target-week`: `YYYY-Www` (ISO 주차). `target_date`가 속한 주와 맞출 것.
- `--target-date`: `YYYY-MM-DD`. 생략 시 해당 주 **월요일**이 기준일.
- `--force`: 해당 주에 이미 카드가 많아도 실행 (`CARDS_MIN_PER_WEEK` 무시).

**마켓만 바꿀 때** (기본 `KR`):

```bash
uv run python -m central.run --market KR --target-week 2026-W29 --force
```

### Cron 모드 (주차 미지정)

`--target-week` 없이 실행하면 **오늘 기준 향후 `WEEKS_AHEAD`주**(기본 4주)를 순차 실행한다.

```bash
uv run python -m central.run
```

주차 간 약 5초 대기 후 다음 주 실행.

## 3. 텔레그램 봇 (수동 실행·승인 워크플로)

CLI와 **별도 프로세스**로 돌린다. **long-polling** 이라 항상 켜 둔 터미널(또는 systemd/supervisor)이 필요하다.

### 실행

프로젝트 루트에서:

```bash
uv run python -m central.bot
```

또는 설치된 스크립트:

```bash
uv run central-bot
```

`TELEGRAM_BOT_TOKEN`이 없으면 종료한다. 로그에 `텔레그램 봇 시작 (long-polling)` 이 나오면 준비 완료.

### 명령 (구현: [`src/central/bot.py`](src/central/bot.py))

| 명령 | 설명 |
|------|------|
| `/start` | 봇 인사(권한 있으면 `/help` 안내) |
| `/help` | 전체 명령 설명(권한 없어도 목록 표시, 실행은 권한 필요) |
| `/run [YYYY-Www]` | 해당 주 파이프라인 실행(인자 없으면 향후 1주 중 첫 주). `triggered_by=bot` |
| `/list [YYYY-Www]` | `pending` 카드 목록 + 인라인 승인/거절/재생성 버튼 |
| `/approve <card_id>` | 카드 상태 `approved` |
| `/reject <card_id>` | 카드 상태 `rejected` |
| `/regen <YYYY-Www>` | 해당 주 `min_cards=0`으로 파이프라인 재실행 |
| `/status` | 최근 `runs` 5건 요약 |

`/run`은 **기본 `CARDS_MIN_PER_WEEK`(30)** 규칙을 따른다. 이미 카드가 많으면 `skipped` 될 수 있음 — 강제 실행은 CLI에서 `--force`.

### GitHub Actions와의 관계

[`.github/workflows/central-cron.yml`](.github/workflows/central-cron.yml)은 **실패 시** `curl`로 텔레그램에 한 줄 알림만 보낸다. 일상적인 생성 트리거는 이 워크플로 또는 위 CLI·봇 `/run` 중 택일하면 된다.

## 4. 파이프라인 개요

1. 캘린더 / 네이버 DataLab / Pinterest 수집  
2. evidence bundle + `source_signals` 저장  
3. **Grounding 1회** (Google Search 도구, 설정된 2.5 모델 체인)  
4. **고정 5 슬롯** (`central.slots.assign_slots`) — 슬롯마다 `primary_keyword`·`industry` 잠금  
5. 슬롯당 Gemini fast 호출(기본 목표 약 4장/슬롯)  
6. enrich → validate → (필요 시 첫 슬롯 품질 재시도) → Supabase `idea_cards` 저장  

슬롯 종류: `calendar_event_slot`, `search_trend_slot`, `pinterest_visual_slot`, `commercial_evergreen_slot`, `long_tail_niche_slot`.

상업 evergreen 시드는 [`data/evergreen_seeds.yaml`](data/evergreen_seeds.yaml)에서 월별로 조정할 수 있다.

## 5. 실행 결과 확인

- 로그에 `run_id`, `inserted`, `skipped`, `status`가 출력된다.
- Supabase에서 `runs` 테이블과 `idea_cards`의 `target_week` / `market`으로 필터해 확인한다.

## 6. 자주 겪는 동작

| 현상 | 원인 |
|------|------|
| `503 UNAVAILABLE` (Gemini) | 일시적 고부하. 코드에 재시도·모델 폴백이 있음. 잠시 뒤 `--force` 재실행. |
| `skipped` | 해당 `target_week`에 이미 카드가 `CARDS_MIN_PER_WEEK`(기본 30) 이상. `--force`로 무시. |
| `valid`가 적음 | 번들 신호가 적거나 슬롯 `primary`가 번들에 없을 때. Naver/Pinterest/공휴일 API·`.env` 확인. |
| 봇이 “권한 없음” | `TELEGRAM_ADMIN_CHAT_IDS`에 **현재 채팅의 chat_id**가 포함돼 있는지 확인. |

## 7. 기타 스크립트

레거시 idea 카드 정리 등은 `scripts/` 디렉터리를 본다. (예: `scripts/cleanup_legacy_idea_cards.py`)

---

DB 스키마는 [`supabase/migrations/001_init.sql`](supabase/migrations/001_init.sql)을 참고한다.
