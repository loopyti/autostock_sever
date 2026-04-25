-- Demand Intelligence Server — Initial Schema
-- 독립 신규 프로젝트 (기존 auto-stock candidates 테이블과 무관)

create extension if not exists "pgcrypto";

-- ─────────────────────────────────────────────
-- 1. runs: 수집/생성 실행 로그
-- ─────────────────────────────────────────────
create table public.runs (
  id uuid primary key default gen_random_uuid(),
  market text not null,                              -- 'KR'
  target_week text not null,                         -- 'YYYY-Www'
  target_date date,
  triggered_by text not null default 'cli'
    check (triggered_by in ('cron', 'bot', 'cli')),
  status text not null default 'running'
    check (status in ('running', 'succeeded', 'failed', 'skipped')),
  cards_inserted int not null default 0,
  cards_skipped int not null default 0,
  cards_invalid int not null default 0,
  used_fallback boolean not null default false,
  raw_response jsonb,
  error text,
  started_at timestamptz not null default now(),
  ended_at timestamptz
);

create index on public.runs (target_week);
create index on public.runs (market, target_week);
create index on public.runs (started_at desc);


-- ─────────────────────────────────────────────
-- 2. source_signals: 1차 수집 원신호
-- ─────────────────────────────────────────────
create table public.source_signals (
  id uuid primary key default gen_random_uuid(),
  run_id uuid not null references public.runs(id) on delete cascade,
  source text not null
    check (source in ('calendar', 'naver_datalab', 'pinterest')),
  market text not null,
  target_week text not null,
  raw jsonb not null default '{}'::jsonb,
  normalized jsonb,
  collected_at timestamptz not null default now()
);

create index on public.source_signals (run_id);
create index on public.source_signals (source, market, target_week);


-- ─────────────────────────────────────────────
-- 3. evidence: Gemini grounding 근거 (1행 = 1 chunk)
-- ─────────────────────────────────────────────
create table public.evidence (
  id uuid primary key default gen_random_uuid(),
  run_id uuid not null references public.runs(id) on delete cascade,
  source text not null
    check (source in ('google_grounding', 'naver_datalab', 'pinterest', 'calendar')),
  title text,
  url text,
  snippet text,
  raw jsonb,
  collected_at timestamptz not null default now()
);

create index on public.evidence (run_id);
create index on public.evidence (source);


-- ─────────────────────────────────────────────
-- 4. idea_cards: 최종 아이디어 카드
-- ─────────────────────────────────────────────
create table public.idea_cards (
  id uuid primary key default gen_random_uuid(),
  run_id uuid references public.runs(id) on delete set null,

  -- 시간 범위
  market text not null,
  target_week text not null,
  target_date date,
  event_date date,
  lead_start_days int not null default 7,
  lead_end_days int not null default 30,

  -- idea card 핵심 (사용자 명세 그대로)
  idea_title text not null,
  buyer text not null,
  use_case text not null,
  reason text,
  intent text,
  source_keywords text[] not null default '{}',
  source_summary text,
  asset_hints text[] not null default '{}',
  used_evidence jsonb not null default '[]'::jsonb,

  -- Gemini 분류 (idea card 생성 시 함께 요청)
  industry text,
  date_specificity text
    check (date_specificity in ('high', 'medium', 'low')),

  -- 서버 계산 (enrich 단계)
  date_weight numeric(4,3)
    check (date_weight between 0 and 1),
  weight numeric(4,3)
    check (weight between 0 and 1),

  -- 운영
  status text not null default 'pending'
    check (status in ('pending', 'approved', 'rejected')),
  dedupe_key text not null,

  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create unique index on public.idea_cards (dedupe_key);
create index on public.idea_cards (market, target_week, status);
create index on public.idea_cards (target_date);
create index on public.idea_cards (run_id);
create index on public.idea_cards (event_date);

-- updated_at 자동 갱신
create or replace function public.set_updated_at()
returns trigger language plpgsql as $$
begin
  new.updated_at = now();
  return new;
end;
$$;

create trigger idea_cards_updated_at
  before update on public.idea_cards
  for each row execute function public.set_updated_at();
