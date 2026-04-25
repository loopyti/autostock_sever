"""
central/bot.py: 텔레그램 봇 — 자체 long-polling.
명령: /run /list /approve /reject /regen /status
"""
from __future__ import annotations

import logging
import re
import sys
from typing import cast

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)

from central import db
from central.run import run_pipeline, _upcoming_weeks
from central.settings import DEFAULT_MARKET, TELEGRAM_ADMIN_CHAT_IDS, TELEGRAM_BOT_TOKEN

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)
logger = logging.getLogger("central.bot")

_ISO_WEEK_RE = re.compile(r"^\d{4}-W\d{2}$")


# ──────────────────────────────────────────────────────────────────────────────
# 권한 체크
# ──────────────────────────────────────────────────────────────────────────────

def _is_allowed(update: Update) -> bool:
    if not TELEGRAM_ADMIN_CHAT_IDS:
        return True
    chat_id = update.effective_chat.id if update.effective_chat else None
    return chat_id in TELEGRAM_ADMIN_CHAT_IDS


async def _check(update: Update) -> bool:
    if not _is_allowed(update):
        await update.message.reply_text("권한 없음.")
        return False
    return True


# ──────────────────────────────────────────────────────────────────────────────
# 명령 핸들러
# ──────────────────────────────────────────────────────────────────────────────

async def cmd_run(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/run <week>  —  단일 흐름 실행."""
    if not await _check(update):
        return

    args = context.args or []
    if args and _ISO_WEEK_RE.match(args[0]):
        target_week = args[0]
    else:
        weeks = _upcoming_weeks(1)
        target_week = weeks[0]
        await update.message.reply_text(f"target_week 미지정 → {target_week} 사용")

    await update.message.reply_text(f"⏳ {target_week} 실행 중...")
    try:
        result = run_pipeline(
            market=DEFAULT_MARKET,
            target_week=target_week,
            triggered_by="bot",
        )
        status = result["status"]
        emoji = "✅" if status == "succeeded" else "⚠️" if status == "skipped" else "❌"
        msg = (
            f"{emoji} *{target_week}* 완료\n"
            f"inserted: {result['cards_inserted']}\n"
            f"skipped: {result['cards_skipped']}\n"
            f"invalid: {result['cards_invalid']}\n"
            f"fallback: {'yes' if result['used_fallback'] else 'no'}"
        )
        await update.message.reply_text(msg, parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"❌ 실행 실패: {e}")


async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/list <week>  —  pending 카드 인라인 키보드."""
    if not await _check(update):
        return

    args = context.args or []
    if args and _ISO_WEEK_RE.match(args[0]):
        target_week = args[0]
    else:
        weeks = _upcoming_weeks(1)
        target_week = weeks[0]

    cards = db.fetch_pending_cards(DEFAULT_MARKET, target_week)
    if not cards:
        await update.message.reply_text(f"{target_week}: pending 카드 없음.")
        return

    await update.message.reply_text(f"*{target_week}* pending 카드 {len(cards)}개:", parse_mode="Markdown")

    for card in cards[:20]:
        card_id = card["id"]
        title = card.get("idea_title", "(제목 없음)")
        buyer = card.get("buyer", "")
        use_case = card.get("use_case", "")
        weight = card.get("weight") or 0
        text = f"*{title}*\n🧑 {buyer}\n📌 {use_case}\n⚖️ {weight:.2f}"
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ 승인", callback_data=f"approve:{card_id}"),
                InlineKeyboardButton("❌ 거절", callback_data=f"reject:{card_id}"),
                InlineKeyboardButton("🔄 재생성", callback_data=f"regen:{card_id}"),
            ]
        ])
        await update.message.reply_text(text, parse_mode="Markdown", reply_markup=keyboard)


async def callback_action(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """인라인 키보드 approve/reject/regen 처리."""
    query = update.callback_query
    await query.answer()

    if not _is_allowed(update):
        return

    data = query.data or ""
    if ":" not in data:
        return

    action, card_id = data.split(":", 1)

    if action in ("approve", "reject"):
        db.update_card_status(card_id, action + "d")
        label = "✅ 승인됨" if action == "approve" else "❌ 거절됨"
        await query.edit_message_reply_markup(None)
        await query.message.reply_text(f"{label}: `{card_id[:8]}`", parse_mode="Markdown")

    elif action == "regen":
        card = db.get_client().table("idea_cards").select("*").eq("id", card_id).execute().data
        if not card:
            await query.message.reply_text("카드를 찾을 수 없습니다.")
            return
        card = card[0]
        db.update_card_status(card_id, "rejected")
        await query.message.reply_text(f"🔄 `{card_id[:8]}` 거절 처리 후 재생성 실행...", parse_mode="Markdown")
        try:
            result = run_pipeline(
                market=card["market"],
                target_week=card["target_week"],
                triggered_by="bot",
                min_cards=0,
            )
            await query.message.reply_text(
                f"재생성 완료: inserted={result['cards_inserted']}"
            )
        except Exception as e:
            await query.message.reply_text(f"재생성 실패: {e}")


async def cmd_approve(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/approve <id>"""
    if not await _check(update):
        return
    args = context.args or []
    if not args:
        await update.message.reply_text("사용법: /approve <card_id>")
        return
    db.update_card_status(args[0], "approved")
    await update.message.reply_text(f"✅ approved: {args[0][:8]}")


async def cmd_reject(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/reject <id>"""
    if not await _check(update):
        return
    args = context.args or []
    if not args:
        await update.message.reply_text("사용법: /reject <card_id>")
        return
    db.update_card_status(args[0], "rejected")
    await update.message.reply_text(f"❌ rejected: {args[0][:8]}")


async def cmd_regen(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/regen <week>  —  해당 주 재생성."""
    if not await _check(update):
        return
    args = context.args or []
    if not args or not _ISO_WEEK_RE.match(args[0]):
        await update.message.reply_text("사용법: /regen <YYYY-Www>")
        return
    target_week = args[0]
    await update.message.reply_text(f"🔄 {target_week} 재생성 중...")
    try:
        result = run_pipeline(
            market=DEFAULT_MARKET,
            target_week=target_week,
            triggered_by="bot",
            min_cards=0,
        )
        await update.message.reply_text(
            f"완료: inserted={result['cards_inserted']}, skipped={result['cards_skipped']}"
        )
    except Exception as e:
        await update.message.reply_text(f"❌ 실패: {e}")


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/status  —  최근 5개 run 상태."""
    if not await _check(update):
        return
    runs = db.get_recent_runs(5)
    if not runs:
        await update.message.reply_text("실행 기록 없음.")
        return
    lines = ["*최근 실행 5개*"]
    for r in runs:
        emoji = {"succeeded": "✅", "failed": "❌", "running": "⏳", "skipped": "⏭️"}.get(r["status"], "❓")
        lines.append(
            f"{emoji} {r['target_week']} | +{r['cards_inserted']} cards | {r['triggered_by']}"
        )
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


# ──────────────────────────────────────────────────────────────────────────────
# 메인
# ──────────────────────────────────────────────────────────────────────────────

def main() -> None:
    if not TELEGRAM_BOT_TOKEN:
        logger.error("TELEGRAM_BOT_TOKEN이 설정되지 않았습니다.")
        sys.exit(1)

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("run", cmd_run))
    app.add_handler(CommandHandler("list", cmd_list))
    app.add_handler(CommandHandler("approve", cmd_approve))
    app.add_handler(CommandHandler("reject", cmd_reject))
    app.add_handler(CommandHandler("regen", cmd_regen))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CallbackQueryHandler(callback_action))

    logger.info("텔레그램 봇 시작 (long-polling)")
    app.run_polling()


if __name__ == "__main__":
    main()
