import asyncio
import base64
import os
import json
import logging
import httpx
from datetime import datetime, timezone

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
    except httpx.ConnectError:
        await bot.send_message(chat_id=chat_id, text="Could not reach sovereign-core — it may be restarting. Try again in a few seconds.")
        return
    except httpx.HTTPStatusError as e:
        await bot.send_message(chat_id=chat_id, text=f"Core returned HTTP {e.response.status_code}: {e.response.text[:200]}")
        return
    except Exception as e:
        await bot.send_message(chat_id=chat_id, text=f"Core error ({type(e).__name__}): {e}")
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
    """Debounce timer: wait CHUNK_TIMEOUT seconds then assemble and forward.

    Acquires a per-session lock before dispatching so concurrent messages
    are serialised — responses arrive in the order they were asked.
    """
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

    lock = store.get_lock(chat_id)
    async with lock:
        # Snapshot context_window INSIDE the lock — previous message's push_turn()
        # completes before we acquire the lock, so history is correct here.
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


async def handle_install(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /install <goal> — autonomous skill acquisition harness.

    Bypasses all NL routing. Rex searches, ranks, and selects the best skill
    for the stated goal, then surfaces a single confirm gate before scanning
    and installing. No step-by-step micromanagement required.
    """
    user = update.effective_user
    chat_id = update.effective_chat.id
    if user.id != AUTHORIZED_USER_ID:
        return
    goal = " ".join(context.args).strip() if context.args else ""
    if not goal:
        await update.message.reply_text(
            "Usage: /install <goal or skill name>\n"
            "Example: /install crypto portfolio tracker\n"
            "Example: /install tether wallet development kit"
        )
        return
    session = store.get_or_create(chat_id)
    payload = {
        "input": goal,
        "_harness_cmd": "install",
        "context_window": session.history,
    }
    lock = store.get_lock(chat_id)
    async with lock:
        await _dispatch_and_reply(payload, goal, chat_id, context.bot, session)


async def handle_skills(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /skills <query> — browse skills without installing."""
    user = update.effective_user
    chat_id = update.effective_chat.id
    if user.id != AUTHORIZED_USER_ID:
        return
    query = " ".join(context.args).strip() if context.args else ""
    if not query:
        await update.message.reply_text("Usage: /skills <search query>\nExample: /skills wallet tracker")
        return
    session = store.get_or_create(chat_id)
    payload = {"input": query, "_harness_cmd": "skills", "context_window": session.history}
    lock = store.get_lock(chat_id)
    async with lock:
        await _dispatch_and_reply(payload, query, chat_id, context.bot, session)


async def handle_selfimprove(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /selfimprove — run SI observe cycle and surface pending proposals."""
    user = update.effective_user
    chat_id = update.effective_chat.id
    if user.id != AUTHORIZED_USER_ID:
        return
    session = store.get_or_create(chat_id)
    payload = {"input": "selfimprove", "_harness_cmd": "selfimprove", "context_window": session.history}
    lock = store.get_lock(chat_id)
    async with lock:
        await _dispatch_and_reply(payload, "/selfimprove", chat_id, context.bot, session)


async def handle_devcheck(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /devcheck — run full dev harness analysis cycle."""
    user = update.effective_user
    chat_id = update.effective_chat.id
    if user.id != AUTHORIZED_USER_ID:
        return
    session = store.get_or_create(chat_id)
    payload = {"input": "devcheck", "_harness_cmd": "devcheck", "context_window": session.history}
    lock = store.get_lock(chat_id)
    async with lock:
        await _dispatch_and_reply(payload, "/devcheck", chat_id, context.bot, session)


async def handle_portfolio(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /portfolio — trigger portfolio snapshot and return balances + value."""
    user = update.effective_user
    chat_id = update.effective_chat.id
    if user.id != AUTHORIZED_USER_ID:
        return
    session = store.get_or_create(chat_id)
    payload = {"input": "portfolio", "_harness_cmd": "portfolio", "context_window": session.history}
    lock = store.get_lock(chat_id)
    async with lock:
        await _dispatch_and_reply(payload, "/portfolio", chat_id, context.bot, session)


async def handle_pm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /pm — PM harness (stub, pending build)."""
    user = update.effective_user
    if user.id != AUTHORIZED_USER_ID:
        return
    await update.message.reply_text("PM harness not yet built — pending Director approval of the proposal.")


async def handle_do_tax(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /do_tax [year] — NZ tax report harness.

    Advances the tax report harness state machine by one turn.
    On first call: queries semantic memory for the FY, reports counts, asks for
    supplementary expense CSV names.
    On second call: parses named CSVs (or proceeds on 'none'), asks for confirmation.
    On third call (confirmed): generates income{year}.csv and expenses{year}.csv
    in /Digiant/Tax/FY{year}/ on Nextcloud.

    Usage:
      /do_tax        — uses current NZ financial year
      /do_tax 2026   — uses FY2026 (01 Apr 2025 – 31 Mar 2026)

    NOTE: Register this command with BotFather manually:
      /setcommands → add:  do_tax - Generate NZ tax report for a financial year
    """
    user = update.effective_user
    chat_id = update.effective_chat.id
    if user.id != AUTHORIZED_USER_ID:
        return

    # Optional year argument — e.g. /do_tax 2026
    year_arg = context.args[0].strip() if context.args else ""
    # Validate: must be a 4-digit year if provided
    if year_arg and (not year_arg.isdigit() or len(year_arg) != 4):
        await update.message.reply_text(
            "Usage: /do_tax [year]\nExample: /do_tax 2026\nYear must be a 4-digit NZ tax year."
        )
        return

    session = store.get_or_create(chat_id)
    payload = {
        "input":        year_arg or "do_tax",
        "_harness_cmd": "tax_report",
        "tax_year":     year_arg,
        "context_window": session.history,
    }
    lock = store.get_lock(chat_id)
    async with lock:
        await _dispatch_and_reply(payload, f"/do_tax {year_arg}".strip(), chat_id, context.bot, session)


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


async def handle_attachment(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle file/photo/voice/document messages — upload to Nextcloud via sovereign-core."""
    user = update.effective_user
    chat_id = update.effective_chat.id
    msg = update.message

    if user.id != AUTHORIZED_USER_ID:
        logger.warning("Rejected attachment from user_id=%s", user.id)
        return

    now = datetime.now(timezone.utc)
    ts = now.strftime("%Y%m%d_%H%M%S")

    # Resolve file_id, filename, mime_type from message type
    file_id = None
    filename = None
    mime_type = "application/octet-stream"

    if msg.document:
        file_id = msg.document.file_id
        filename = msg.document.file_name or f"doc_{ts}"
        mime_type = msg.document.mime_type or "application/octet-stream"
    elif msg.photo:
        # Highest-resolution PhotoSize
        photo = sorted(msg.photo, key=lambda p: p.width * p.height)[-1]
        file_id = photo.file_id
        filename = f"photo_{ts}.jpg"
        mime_type = "image/jpeg"
    elif msg.voice:
        file_id = msg.voice.file_id
        filename = f"voice_{ts}.ogg"
        mime_type = msg.voice.mime_type or "audio/ogg"
    elif msg.video_note:
        file_id = msg.video_note.file_id
        filename = f"videonote_{ts}.mp4"
        mime_type = "video/mp4"
    elif msg.video:
        file_id = msg.video.file_id
        filename = getattr(msg.video, "file_name", None) or f"video_{ts}.mp4"
        mime_type = msg.video.mime_type or "video/mp4"

    if not file_id:
        await msg.reply_text("Unsupported attachment type.")
        return

    await context.bot.send_chat_action(chat_id=chat_id, action="upload_document")

    try:
        tg_file = await context.bot.get_file(file_id)
        data = await tg_file.download_as_bytearray()
    except Exception as e:
        await msg.reply_text(f"Download from Telegram failed: {e}")
        return

    content_b64 = base64.b64encode(bytes(data)).decode()
    size = len(data)

    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            r = await client.post(
                f"{SOVEREIGN_URL}/attachment",
                json={
                    "filename":    filename,
                    "content_b64": content_b64,
                    "mime_type":   mime_type,
                    "size":        size,
                    "source":      "telegram",
                },
            )
            r.raise_for_status()
            data_resp = r.json()
    except httpx.TimeoutException:
        await msg.reply_text("Upload timed out.")
        return
    except Exception as e:
        await msg.reply_text(f"Upload error: {e}")
        return

    if data_resp.get("director_message"):
        await msg.reply_text(data_resp["director_message"])
    elif data_resp.get("error"):
        await msg.reply_text(f"Upload failed: {data_resp['error']}")
    else:
        await msg.reply_text(f"Uploaded {filename} ({round(size/1024, 1)} KB)")


def main():
    logger.info("Sovereign gateway starting (authorized user: %s)", AUTHORIZED_USER_ID)
    logger.info("Sovereign core URL: %s", SOVEREIGN_URL)

    app = (
        Application.builder()
        .token(TOKEN)
        .build()
    )
    app.add_handler(CommandHandler("install", handle_install))
    app.add_handler(CommandHandler("skills", handle_skills))
    app.add_handler(CommandHandler("selfimprove", handle_selfimprove))
    app.add_handler(CommandHandler("devcheck", handle_devcheck))
    app.add_handler(CommandHandler("portfolio", handle_portfolio))
    app.add_handler(CommandHandler("pm", handle_pm))
    app.add_handler(CommandHandler("do_tax", handle_do_tax))
    app.add_handler(CommandHandler(["remember", "memorise", "memorize"], handle_remember))
    app.add_handler(CommandHandler("verify", handle_verify))
    app.add_handler(MessageHandler(
        filters.Document.ALL | filters.PHOTO | filters.VOICE | filters.VIDEO | filters.VIDEO_NOTE,
        handle_attachment,
    ))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
