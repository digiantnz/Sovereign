import asyncio
import os
import json
import logging
import httpx

from telegram import Update
from telegram.ext import Application, MessageHandler, CommandHandler, filters, ContextTypes

from session_store import Session, SessionStore

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
AUTHORIZED_USER_ID = int(os.environ["OPENCLAW_TELEGRAM_ADMIN_CHAT_ID"])
SOVEREIGN_URL = os.environ.get("SOVEREIGN_CORE_URL", "http://sovereign-core:8000")

# Telegram splits pastes >4096 chars into sequential messages. We wait this
# many seconds after the last chunk before forwarding the assembled text.
CHUNK_TIMEOUT = float(os.environ.get("GATEWAY_CHUNK_TIMEOUT", "1.5"))

store = SessionStore()


def _format_result(data: dict) -> str:
    """Format a sovereign-core /chat success response for Telegram."""
    intent = data.get("intent", "")
    agent = data.get("agent", "")
    result = data.get("result", {})

    lines = [f"[{agent} / {intent}]", ""]

    # Browser search result — show human-readable response + source list
    if "data" in result and isinstance(result.get("data"), dict):
        enriched = result["data"]
        # Prefer pre-synthesised response field (set by execution engine)
        if result.get("response"):
            lines.append(result["response"])
        else:
            synth = enriched.get("sovereign_synthesis", {})
            summary = synth.get("summary", "")
            if summary:
                lines.append(summary)
            top = enriched.get("results", [])
            if top:
                lines.append("\nTop sources:")
                for r in top[:5]:
                    title = r.get("title", "")
                    url = r.get("url", "")
                    if title and url:
                        import urllib.parse
                        host = urllib.parse.urlparse(url).netloc
                        lines.append(f"• {title} — {host}")
        return "\n".join(lines).strip()

    if "containers" in result:
        for c in result["containers"]:
            name = c.get("name", ["?"])
            if isinstance(name, list):
                name = name[0].lstrip("/")
            lines.append(f"  {name}: {c.get('status', c.get('state', ''))}")

    elif "logs" in result:
        log_text = result["logs"] or "(empty)"
        lines.append(log_text[-3000:] if len(log_text) > 3000 else log_text)

    elif "stats" in result:
        s = result["stats"]
        lines.append(str(s)[:2000])

    elif "response" in result:
        lines.append(result["response"])

    elif "content" in result:
        lines.append(result["content"])

    elif "files" in result:
        for f in result.get("files", []):
            lines.append(f"  {f}")

    elif "messages" in result:
        for m in result.get("messages", [])[:5]:
            subj = m.get("subject", "(no subject)")
            frm = m.get("from", "")
            lines.append(f"  [{frm}] {subj}")

    elif "message" in result:
        lines.append(result["message"])

    elif "status" in result:
        lines.append(f"Status: {result['status']}")

    else:
        lines.append(json.dumps(result, indent=2)[:2000])

    return "\n".join(lines).strip()


async def _dispatch_and_reply(
    payload: dict,
    input_text: str,
    chat_id: int,
    bot,
    session: Session,
    chunk_count: int = 1,
) -> None:
    """Forward a fully-assembled payload to sovereign-core and handle all response types."""
    await bot.send_chat_action(chat_id=chat_id, action="typing")

    try:
        async with httpx.AsyncClient(timeout=180.0) as client:
            r = await client.post(f"{SOVEREIGN_URL}/chat", json=payload)
            r.raise_for_status()
            data = r.json()
    except httpx.TimeoutException:
        await bot.send_message(chat_id=chat_id, text="Core timed out — the cognitive loop took too long.")
        return
    except Exception as e:
        await bot.send_message(chat_id=chat_id, text=f"Core error: {e}")
        return

    # Security guardrail confirmation
    if data.get("requires_security_confirmation"):
        session.awaiting_security_confirmation = True
        session.pending_delegation = data.get("pending_delegation")
        session.pending_input = input_text
        rules = data.get("rules", [])
        reason = data.get("reason", "")
        msg = "⚠️ *Security guardrail: confirmation required*\n"
        if reason:
            msg += f"Reason: {reason}\n"
        if rules:
            msg += f"Matched rules: {', '.join(rules)}\n"
        msg += "\nProceed? Reply yes or no."
        await bot.send_message(chat_id=chat_id, text=msg)
        return

    # Low memory confidence gate
    if data.get("requires_confidence_acknowledgement"):
        session.awaiting_confidence_ack = True
        session.pending_delegation = data.get("pending_delegation")
        session.pending_input = input_text
        confidence = data.get("confidence", 0.0)
        gaps = data.get("gaps", [])
        delegation = data.get("pending_delegation", {})
        intent = delegation.get("intent", "?")
        target = delegation.get("target") or ""
        summary = delegation.get("reasoning_summary", "")
        msg = f"⚠️ Low memory confidence ({confidence:.0%})\n"
        msg += f"Planned action: {intent}"
        if target:
            msg += f" → {target}"
        msg += "\n"
        if summary:
            msg += f"Reasoning: {summary}\n"
        if gaps:
            msg += f"Known gaps: {', '.join(gaps)}\n"
        msg += "\nProceed anyway? Reply yes or no."
        await bot.send_message(chat_id=chat_id, text=msg)
        return

    # Confirmation required (MID or HIGH tier)
    if data.get("requires_confirmation") or data.get("requires_double_confirmation"):
        session.awaiting_confirmation = True
        session.pending_delegation = data.get("pending_delegation")
        session.pending_input = input_text
        summary = data.get("summary", "")
        kind = "DOUBLE CONFIRMATION" if data.get("requires_double_confirmation") else "Confirmation"
        await bot.send_message(
            chat_id=chat_id,
            text=f"[{kind} required]\n{summary}\n\nReply yes to proceed or no to cancel.",
        )
        return

    # Error from core
    if data.get("error"):
        feedback = data.get("feedback", "")
        msg = f"Error: {data['error']}"
        if feedback:
            msg += f"\nFeedback: {feedback}"
        await bot.send_message(chat_id=chat_id, text=msg)
        return

    # Morning briefing — prospective items due today, sent before the main response
    if data.get("morning_briefing"):
        await bot.send_message(chat_id=chat_id, text=f"[Morning briefing]\n\n{data['morning_briefing']}")

    # Chunked input indicator — shown when Telegram split a large paste
    if chunk_count > 1:
        await bot.send_message(
            chat_id=chat_id,
            text=(
                f"⚠️ [Input arrived in {chunk_count} parts ({len(input_text)} chars total) "
                f"and was reassembled before processing.]"
            ),
        )

    # Success — CEO translation takes priority; fallback to structured formatter
    if data.get("director_message"):
        await bot.send_message(chat_id=chat_id, text=data["director_message"])
    else:
        reply = _format_result(data)
        await bot.send_message(chat_id=chat_id, text=reply)

    # Store turn in history for follow-up context (pronouns, "summarise that", etc.)
    session.push_turn(
        user=input_text,
        assistant=data.get("director_message") or _format_result(data),
    )


async def _flush_buffer(chat_id: int, bot, session: Session) -> None:
    """Debounce timer: wait CHUNK_TIMEOUT seconds then assemble and forward."""
    try:
        await asyncio.sleep(CHUNK_TIMEOUT)
    except asyncio.CancelledError:
        return  # A new chunk arrived; the new task will flush when ready

    chunks = session.message_buffer[:]
    session.message_buffer.clear()
    session.flush_task = None

    if not chunks:
        return

    assembled = "\n".join(chunks)
    chunk_count = len(chunks)
    if chunk_count > 1:
        logger.info(
            "[chat=%s] Assembled %d chunks → %d chars total",
            chat_id, chunk_count, len(assembled),
        )

    payload = {"input": assembled}
    if session.history:
        payload["context_window"] = session.history

    await _dispatch_and_reply(payload, assembled, chat_id, bot, session, chunk_count=chunk_count)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    chat_id = update.effective_chat.id
    text = (update.message.text or "").strip()

    # Auth gate — ignore all other users silently
    if user.id != AUTHORIZED_USER_ID:
        logger.warning("Rejected message from user_id=%s", user.id)
        return

    session = store.get_or_create(chat_id)

    # --- Awaiting flows: bypass the buffer, dispatch immediately ---
    # These are always short "yes/no" replies and must not be debounced.

    if session.awaiting_security_confirmation:
        if text.lower() in ("yes", "y", "confirm"):
            saved = session.pending_input
            payload = {
                "input": saved,
                "pending_delegation": session.pending_delegation,
                "security_confirmed": True,
                "context_window": session.history,
            }
            session.awaiting_security_confirmation = False
            session.pending_delegation = None
            session.pending_input = None
            await _dispatch_and_reply(payload, saved or text, chat_id, context.bot, session)
            return
        elif text.lower() in ("no", "n", "cancel"):
            session.awaiting_security_confirmation = False
            session.pending_delegation = None
            session.pending_input = None
            await update.message.reply_text("Cancelled — guardrail block upheld.")
            return
        else:
            session.awaiting_security_confirmation = False
            session.pending_delegation = None
            session.pending_input = None
            await update.message.reply_text("Previous action cancelled — processing your new request.")

    if session.awaiting_confidence_ack:
        if text.lower() in ("yes", "y", "proceed"):
            saved = session.pending_input
            payload = {
                "input": saved,
                "pending_delegation": session.pending_delegation,
                "confidence_acknowledged": True,
                "context_window": session.history,
            }
            session.awaiting_confidence_ack = False
            session.pending_delegation = None
            session.pending_input = None
            await _dispatch_and_reply(payload, saved or text, chat_id, context.bot, session)
            return
        elif text.lower() in ("no", "n", "cancel"):
            session.awaiting_confidence_ack = False
            session.pending_delegation = None
            session.pending_input = None
            await update.message.reply_text("Cancelled.")
            return
        else:
            session.awaiting_confidence_ack = False
            session.pending_delegation = None
            session.pending_input = None
            await update.message.reply_text("Previous action cancelled — processing your new request.")

    if session.awaiting_confirmation:
        if text.lower() in ("yes", "y", "confirm"):
            saved = session.pending_input
            payload = {
                "input": saved,
                "pending_delegation": session.pending_delegation,
                "confirmed": True,
                "context_window": session.history,
            }
            session.awaiting_confirmation = False
            session.pending_delegation = None
            session.pending_input = None
            await _dispatch_and_reply(payload, saved or text, chat_id, context.bot, session)
            return
        elif text.lower() in ("no", "n", "cancel"):
            session.awaiting_confirmation = False
            session.pending_delegation = None
            session.pending_input = None
            await update.message.reply_text("Cancelled.")
            return
        else:
            # New substantive message — cancel the stale pending confirmation and process fresh
            session.awaiting_confirmation = False
            session.pending_delegation = None
            session.pending_input = None
            await update.message.reply_text("Previous action cancelled — processing your new request.")

    # --- Normal message: buffer and debounce ---
    # Telegram splits pastes >4096 chars into sequential messages arriving
    # within milliseconds of each other. We accumulate chunks and wait
    # CHUNK_TIMEOUT seconds after the last chunk before forwarding.
    session.message_buffer.append(text)
    logger.debug("[chat=%s] Buffered chunk %d (%d chars)", chat_id, len(session.message_buffer), len(text))

    if session.flush_task and not session.flush_task.done():
        session.flush_task.cancel()

    session.flush_task = asyncio.create_task(
        _flush_buffer(chat_id, context.bot, session)
    )


async def handle_verify(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /verify <sig_prefix> — anti-spoofing check against Rex's public key."""
    user = update.effective_user
    if user.id != AUTHORIZED_USER_ID:
        return
    prefix = " ".join(context.args).strip() if context.args else ""
    if not prefix:
        await update.message.reply_text("Usage: /verify <sig_prefix>\nExample: /verify a7f3b2")
        return
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.get(f"{SOVEREIGN_URL}/wallet/verify", params={"prefix": prefix})
            r.raise_for_status()
            data = r.json()
    except Exception as e:
        await update.message.reply_text(f"Verification error: {e}")
        return
    if data.get("verified"):
        event = data.get("event_type", "event")
        ts = data.get("ts", "")
        if data.get("address"):
            detail = f"Wallet keygen — Address: {data['address']}"
        else:
            detail = data.get("action", "") + (f" — {data['description']}" if data.get("description") else "")
        await update.message.reply_text(
            f"✓ Signature verified — this message originated from Sovereign.\n\n"
            f"Event: {event}\n"
            f"{detail}\n"
            f"Signed at: {ts}"
        )
    else:
        err = data.get("error", "Signature not found or invalid.")
        await update.message.reply_text(f"✗ Verification failed: {err}")


async def handle_remember(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /remember and /memorise slash commands."""
    user = update.effective_user
    if user.id != AUTHORIZED_USER_ID:
        return
    fact = " ".join(context.args).strip() if context.args else ""
    if not fact:
        await update.message.reply_text("Usage: /remember <fact to store>")
        return
    # Forward as natural language — CEO will classify as remember_fact
    update.message.text = f"remember that {fact}"
    await handle_message(update, context)


def main():
    logger.info("Sovereign gateway starting (authorized user: %s)", AUTHORIZED_USER_ID)
    logger.info("Sovereign core URL: %s", SOVEREIGN_URL)

    app = (
        Application.builder()
        .token(TOKEN)
        .build()
    )
    app.add_handler(CommandHandler(["remember", "memorise", "memorize"], handle_remember))
    app.add_handler(CommandHandler("verify", handle_verify))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
