"""
pinterest collector: Pinterest Trends API v5.
KR 미지원이라 US 리전으로 통일. 글로벌 비주얼 트렌드 보조 신호.
토큰 만료/4xx 시 빈 결과 + 경고 로그.

올바른 엔드포인트: /v5/trends/keywords/{region}/top/{trend_type}
참고: server/collectors/pinterest_trends.py (auto-stock 레포)
"""
from __future__ import annotations

import base64
import logging
import os
from typing import Any

import httpx

from central.settings import (
    PINTEREST_ACCESS_TOKEN,
    PINTEREST_CLIENT_ID,
    PINTEREST_CLIENT_SECRET,
    PINTEREST_REFRESH_TOKEN,
)

logger = logging.getLogger(__name__)

_BASE = "https://api.pinterest.com/v5/trends/keywords"
_TOKEN_URL = "https://api.pinterest.com/v5/oauth/token"
_REGION = "US"
_TOP_N = 50
_TREND_TYPES: list[str] = ["growing", "monthly"]

# 한국 B2B 스톡 이미지 수요와 무관한 개인취미 카테고리 키워드 — 번들에서 제외
# nails/hair/recipes/crafts 등은 글로벌 소비자 트렌드이며 한국 디자인 발주 수요를 유발하지 않음
_NOISE_SUBSTRINGS: frozenset[str] = frozenset({
    "nail", "nails", "hair", "hairstyle", "hairstyles", "makeup", "recipe",
    "recipes", "dinner", "lunch", "breakfast", "food", "cooking", "bake",
    "baking", "craft", "crafts", "diy", "drawing", "doodle", "doodles",
    "painting", "wallpaper", "aesthetic", "outfit", "outfits", "dress",
    "prom", "braids", "braid", "braided", "poop", "chicken coop",
    "sigil", "sigils", "ahh", "random ahh",
})

_NOISE_EXACT: frozenset[str] = frozenset({
    "nails", "hair", "hairstyles", "makeup", "wallpaper", "aesthetic",
    "outfit", "funny", "drawing ideas", "painting ideas", "pose reference",
    "reaction pictures", "braids", "youtube",
})


def _is_noise(keyword: str) -> bool:
    """한국 B2B 수요와 무관한 개인취미 키워드면 True."""
    kw_lower = keyword.lower().strip()
    if kw_lower in _NOISE_EXACT:
        return True
    return any(sub in kw_lower for sub in _NOISE_SUBSTRINGS)


def _get_access_token() -> str:
    # settings에서 로드한 값 우선, 런타임 갱신된 값은 os.environ에 저장
    return os.environ.get("PINTEREST_ACCESS_TOKEN", PINTEREST_ACCESS_TOKEN or "").strip()


def _has_refresh_credentials() -> bool:
    return bool(
        (os.environ.get("PINTEREST_CLIENT_ID") or PINTEREST_CLIENT_ID or "").strip()
        and (os.environ.get("PINTEREST_CLIENT_SECRET") or PINTEREST_CLIENT_SECRET or "").strip()
        and (os.environ.get("PINTEREST_REFRESH_TOKEN") or PINTEREST_REFRESH_TOKEN or "").strip()
    )


def refresh_access_token() -> str:
    if not _has_refresh_credentials():
        return ""
    client_id = (os.environ.get("PINTEREST_CLIENT_ID") or PINTEREST_CLIENT_ID or "").strip()
    client_secret = (os.environ.get("PINTEREST_CLIENT_SECRET") or PINTEREST_CLIENT_SECRET or "").strip()
    refresh_token = (os.environ.get("PINTEREST_REFRESH_TOKEN") or PINTEREST_REFRESH_TOKEN or "").strip()
    basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode("ascii")
    try:
        resp = httpx.post(
            _TOKEN_URL,
            headers={
                "Accept": "application/json",
                "Authorization": f"Basic {basic}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            data={"grant_type": "refresh_token", "refresh_token": refresh_token},
            timeout=20.0,
        )
        if resp.status_code in {401, 403}:
            logger.warning("Pinterest refresh token 인증 실패.")
            return ""
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        logger.warning("Pinterest 토큰 갱신 실패: %s", exc)
        return ""

    payload = resp.json()
    access_token = str(payload.get("access_token") or "").strip()
    if not access_token:
        logger.warning("Pinterest refresh 응답에 access_token 없음.")
        return ""
    os.environ["PINTEREST_ACCESS_TOKEN"] = access_token
    new_refresh = str(payload.get("refresh_token") or "").strip()
    if new_refresh:
        os.environ["PINTEREST_REFRESH_TOKEN"] = new_refresh
    return access_token


def _fetch_trend_type(client: httpx.Client, token: str, trend_type: str) -> list[dict]:
    """올바른 URL: /v5/trends/keywords/{region}/top/{trend_type}"""
    url = f"{_BASE}/{_REGION}/top/{trend_type}"
    resp = client.get(
        url,
        headers={"Accept": "application/json", "Authorization": f"Bearer {token}"},
        params={"limit": _TOP_N},
    )
    if resp.status_code in {400, 404}:
        logger.info("Pinterest region=%s trend_type=%s 지원 안 함 (%s)", _REGION, trend_type, resp.status_code)
        return []
    if resp.status_code in {401, 403}:
        raise httpx.HTTPStatusError("auth_error", request=resp.request, response=resp)
    resp.raise_for_status()
    payload = resp.json()
    return payload.get("trends") or payload.get("items") or []


def collect(target_week: str, market: str = "KR") -> list[dict[str, Any]]:
    """
    Returns:
        list of {
            "keyword": str,
            "trend_type": str,
            "rank": int,
            "pct_growth_wow": int,
            "pct_growth_mom": int,
            "region": str,
        }
    """
    token = _get_access_token()
    if not token and not _has_refresh_credentials():
        logger.warning("Pinterest credentials 없음. 수집 건너뜀.")
        return []
    if not token:
        token = refresh_access_token()
        if not token:
            logger.warning("Pinterest 토큰 취득 실패. 수집 건너뜀.")
            return []

    with httpx.Client(timeout=15.0) as client:
        refreshed = False
        results: list[dict[str, Any]] = []
        seen_kws: set[str] = set()

        for trend_type in _TREND_TYPES:
            try:
                items = _fetch_trend_type(client, token, trend_type)
            except httpx.HTTPStatusError:
                if not refreshed:
                    refreshed = True
                    new_token = refresh_access_token()
                    if new_token:
                        token = new_token
                        try:
                            items = _fetch_trend_type(client, token, trend_type)
                        except Exception as exc:
                            logger.warning("Pinterest %s 재시도 실패: %s", trend_type, exc)
                            continue
                    else:
                        logger.warning("Pinterest 토큰 갱신 실패. 수집 중단.")
                        break
                else:
                    continue
            except Exception as exc:
                logger.warning("Pinterest %s 수집 실패: %s", trend_type, exc)
                continue

            for rank, item in enumerate(items, start=1):
                kw = str(item.get("keyword") or item.get("name") or "").strip()
                if not kw or kw in seen_kws:
                    continue
                if _is_noise(kw):
                    continue
                seen_kws.add(kw)
                results.append({
                    "keyword": kw,
                    "trend_type": trend_type,
                    "rank": rank,
                    "pct_growth_wow": int(item.get("pct_growth_wow") or item.get("weekly_change") or 0),
                    "pct_growth_mom": int(item.get("pct_growth_mom") or 0),
                    "region": _REGION,
                })

    return results
