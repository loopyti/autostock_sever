"""환경변수 로딩 및 설정."""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

# 프로젝트 루트 기준 .env 자동 로드 (src/central/settings.py → parents[2] = 프로젝트 루트)
_root = Path(__file__).resolve().parents[2]
load_dotenv(_root / ".env", override=False)


def _require(key: str) -> str:
    val = os.environ.get(key, "").strip()
    if not val:
        raise RuntimeError(f"필수 환경변수 누락: {key}")
    return val


def _optional(key: str, default: str = "") -> str:
    return os.environ.get(key, default).strip()


# Supabase
SUPABASE_URL: str = _require("SUPABASE_URL")
SUPABASE_SERVICE_KEY: str = _require("SUPABASE_SERVICE_KEY")

# Gemini (Google AI Studio)
GOOGLE_AI_STUDIO_API_KEY: str = _require("GOOGLE_AI_STUDIO_API_KEY")

# 그라운딩 필요 (idea card 생성) — 무료 티어에서 2.5 계열만 그라운딩 지원
# 각 모델 RPD 20, 합계 40/일
GEMINI_GROUNDING_MODELS: list[dict] = [
    {"name": "gemini-2.5-flash",      "rpm": 5,  "rpd": 18},
    {"name": "gemini-2.5-flash-lite", "rpm": 10, "rpd": 18},
]

# 그라운딩 불필요 (검증, 요약 등 단순 생성) — RPD 500
GEMINI_FAST_MODEL: str = _optional("GEMINI_FAST_MODEL", "gemini-3.1-flash-lite-preview")
GEMINI_FAST_RPM: int = 12   # hard 15, 여유 3
GEMINI_FAST_RPD: int = 450  # hard 500, 여유 50

# Naver DataLab
NAVER_DATALAB_CLIENT_ID: str = _optional("NAVER_DATALAB_CLIENT_ID")
NAVER_DATALAB_CLIENT_SECRET: str = _optional("NAVER_DATALAB_CLIENT_SECRET")

# Pinterest
PINTEREST_ACCESS_TOKEN: str = _optional("PINTEREST_ACCESS_TOKEN")
PINTEREST_REFRESH_TOKEN: str = _optional("PINTEREST_REFRESH_TOKEN")
PINTEREST_CLIENT_ID: str = _optional("PINTEREST_CLIENT_ID")
PINTEREST_CLIENT_SECRET: str = _optional("PINTEREST_CLIENT_SECRET")

# data.go.kr
DATA_GO_KR_API_KEY: str = _optional("DATA_GO_KR_API_KEY")

# Telegram
TELEGRAM_BOT_TOKEN: str = _optional("TELEGRAM_BOT_TOKEN")
TELEGRAM_ADMIN_CHAT_IDS: list[int] = [
    int(x)
    for x in _optional("TELEGRAM_ADMIN_CHAT_IDS", "").split(",")
    if x.strip().lstrip("-").isdigit()
]

# 하위 호환 (bot 등에서 참조 가능)
GEMINI_RPM_LIMIT: int = 10

# cron 기본값
CARDS_MIN_PER_WEEK: int = 30        # 이미 충분하면 skip
WEEKS_AHEAD: int = 4                # W+1 ~ W+4

# 기본 마켓
DEFAULT_MARKET: str = "KR"
