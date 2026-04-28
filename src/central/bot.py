"""
central/bot.py: 텔레그램 봇 — 자체 long-polling.
명령: /start /help /run /list /approve /reject /regen /status
"""
from __future__ import annotations

import logging
import re
import sys

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

_HELP_TEXT = """\
명령:
• /run [YYYY-Www] — idea 카드 파이프라인 (주 생략 시 향후 첫 대상 주)
• /list [YYYY-Www] — pending 카드 + 승인/거절/재생성 버튼
• /status — 최근 runs 5건
• /regen YYYY-Www — 해당 주 재생성
• /approve <card_id> /reject <card_id>

팁: 그룹에서 BotFather 프라이버시가 켜져 있으면 /run@봇사용자명 처럼 보내야 할 수 있습니다.
"""


# ──────────────────────────────────────────────────────────────────────────────
# 권한 체크
# ──────────────────────────────────────────────────────────────────────────────

def _is_allowed(update: Update) -> bool:
    """허용: TELEGRAM_ADMIN_CHAT_IDS가 비면 전체. 아니면 user id 또는 chat id 일치."""
    if not TELEGRAM_ADMIN_CHAT_IDS:
        return True
    allowed = set(TELEGRAM_ADMIN_CHAT_IDS)
    uid = update.effective_user.id if update.effective_user else None
    cid = update.effective_chat.id if update.effective_chat else None
    return (uid in allowed) or (cid in allowed)


async def _check(update: Update) -> bool:
    if not _is_allowed(update):
        msg = update.effective_message
        if msg:
            uid = update.effective_user.id if update.effective_user else "?"
            cid = update.effective_chat.id if update.effective_chat else "?"
            await msg.reply_text(
                "권한 없음.\n"
                f"• 이 채팅 chat_id: `{cid}`\n"
                f"• 보낸 사람 user_id: `{uid}`\n"
                "`.env`의 `TELEGRAM_ADMIN_CHAT_IDS`에 위 숫자 중 하나를 넣고 봇을 재시작하세요.",
                parse_mode="Markdown",
            )
        return False
    return True


# ──────────────────────────────────────────────────────────────────────────────
# 명령 핸들러
# ──────────────────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """봇 시작 — 짧은 인사 + /help 유도."""
    msg = update.effective_message
    if not msg:
        return
    if not _is_allowed(update):
        uid = update.effective_user.id if update.effective_user else "?"
        cid = update.effective_chat.id if update.effective_chat else "?"
        await msg.reply_text(
            f"봇은 동작 중입니다. 권한이 없습니다.\nchat_id={cid}, user_id={uid}\n"
            "TELEGRAM_ADMIN_CHAT_IDS에 등록 후 봇 재시작.\n전체 명령은 /help"
        )
        return
    await msg.reply_text(
        "Demand Intelligence 봇입니다.\n"
        "명령 목록은 /help 를 입력하세요."
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """전체 명령 설명 (권한 없어도 목록은 볼 수 있음)."""
    msg = update.effective_message
    if not msg:
        return
    body = _HELP_TEXT.strip()
    if not _is_allowed(update):
        uid = update.effective_user.id if update.effective_user else "?"
        cid = update.effective_chat.id if update.effective_chat else "?"
        await msg.reply_text(
            body + f"\n\n— 실행·승인 등은 권한이 필요합니다. (chat_id={cid}, user_id={uid})"
        )
        return
    await msg.reply_text(body)


async def cmd_run(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/run <week>  —  단일 흐름 실행."""
    if not await _check(update):
        return

    msg = update.effective_message
    if not msg:
        return

    args = context.args or []
    if args and _ISO_WEEK_RE.match(args[0]):
        target_week = args[0]
    else:
        weeks = _upcoming_weeks(1)
        target_week = weeks[0]
        await msg.reply_text(f"target_week 미지정 → {target_week} 사용")

    await msg.reply_text(f"⏳ {target_week} 실행 중...")
    try:
        result = run_pipeline(
            market=DEFAULT_MARKET,
            target_week=target_week,
            triggered_by="bot",
        )
        status = result["status"]
        emoji = "✅" if status == "succeeded" else "⚠️" if status == "skipped" else "❌"
        text = (
            f"{emoji} {target_week} 완료\n"
            f"inserted: {result['cards_inserted']}\n"
            f"skipped: {result['cards_skipped']}\n"
            f"invalid: {result['cards_invalid']}\n"
            f"fallback: {'yes' if result['used_fallback'] else 'no'}"
        )
        await msg.reply_text(text)
    except Exception as e:
        await msg.reply_text(f"❌ 실행 실패: {e}")


async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/list <week>  —  pending 카드 인라인 키보드."""
    if not await _check(update):
        return

    msg = update.effective_message
    if not msg:
        return

    args = context.args or []
    if args and _ISO_WEEK_RE.match(args[0]):
        target_week = args[0]
    else:
        weeks = _upcoming_weeks(1)
        target_week = weeks[0]

    cards = db.fetch_pending_cards(DEFAULT_MARKET, target_week)
    if not cards:
        await msg.reply_text(f"{target_week}: pending 카드 없음.")
        return

    await msg.reply_text(f"{target_week} pending 카드 {len(cards)}개:")

    for card in cards[:20]:
        card_id = card["id"]
        title = card.get("idea_title", "(제목 없음)")
        buyer = card.get("buyer", "")
        use_case = card.get("use_case", "")
        weight = card.get("weight") or 0
        text = f"{title}\n🧑 {buyer}\n📌 {use_case}\n⚖️ {weight:.2f}"
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ 승인", callback_data=f"approve:{card_id}"),
                InlineKeyboardButton("❌ 거절", callback_data=f"reject:{card_id}"),
                InlineKeyboardButton("🔄 재생성", callback_data=f"regen:{card_id}"),
            ]
        ])
        await msg.reply_text(text, reply_markup=keyboard)


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
        await query.message.reply_text(f"{label}: {card_id[:8]}")

    elif action == "regen":
        card = db.get_client().table("idea_cards").select("*").eq("id", card_id).execute().data
        if not card:
            await query.message.reply_text("카드를 찾을 수 없습니다.")
            return
        card = card[0]
        db.update_card_status(card_id, "rejected")
        await query.message.reply_text(f"🔄 {card_id[:8]} 거절 처리 후 재생성 실행...")
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
    msg = update.effective_message
    if not msg:
        return
    args = context.args or []
    if not args:
        await msg.reply_text("사용법: /approve <card_id>")
        return
    db.update_card_status(args[0], "approved")
    await msg.reply_text(f"✅ approved: {args[0][:8]}")


async def cmd_reject(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/reject <id>"""
    if not await _check(update):
        return
    msg = update.effective_message
    if not msg:
        return
    args = context.args or []
    if not args:
        await msg.reply_text("사용법: /reject <card_id>")
        return
    db.update_card_status(args[0], "rejected")
    await msg.reply_text(f"❌ rejected: {args[0][:8]}")


async def cmd_regen(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/regen <week>  —  해당 주 재생성."""
    if not await _check(update):
        return
    msg = update.effective_message
    if not msg:
        return
    args = context.args or []
    if not args or not _ISO_WEEK_RE.match(args[0]):
        await msg.reply_text("사용법: /regen <YYYY-Www>")
        return
    target_week = args[0]
    await msg.reply_text(f"🔄 {target_week} 재생성 중...")
    try:
        result = run_pipeline(
            market=DEFAULT_MARKET,
            target_week=target_week,
            triggered_by="bot",
            min_cards=0,
        )
        await msg.reply_text(
            f"완료: inserted={result['cards_inserted']}, skipped={result['cards_skipped']}"
        )
    except Exception as e:
        await msg.reply_text(f"❌ 실패: {e}")


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/status  —  최근 5개 run 상태."""
    if not await _check(update):
        return
    msg = update.effective_message
    if not msg:
        return
    runs = db.get_recent_runs(5)
    if not runs:
        await msg.reply_text("실행 기록 없음.")
        return
    lines = ["최근 실행 5개"]
    for r in runs:
        emoji = {"succeeded": "✅", "failed": "❌", "running": "⏳", "skipped": "⏭️"}.get(r["status"], "❓")
        lines.append(
            f"{emoji} {r['target_week']} | +{r['cards_inserted']} cards | {r['triggered_by']}"
        )
    await msg.reply_text("\n".join(lines))


# ──────────────────────────────────────────────────────────────────────────────
# 메인
# ──────────────────────────────────────────────────────────────────────────────

def main() -> None:
    if not TELEGRAM_BOT_TOKEN:
        logger.error("TELEGRAM_BOT_TOKEN이 설정되지 않았습니다.")
        sys.exit(1)

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
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
