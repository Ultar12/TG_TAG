"""Admin-only Telethon StringSession generator for the Telegram bot."""

import logging
import re
from html import escape as html_escape

from telegram import Update
from telegram.ext import ConversationHandler, ContextTypes
from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError
from telethon.sessions import StringSession

logger = logging.getLogger(__name__)

API_ID, API_HASH, PHONE, CODE, PASSWORD = range(5)


def _is_private_admin(update: Update, admin_id: int) -> bool:
    user = update.effective_user
    chat = update.effective_chat
    return bool(user and chat and user.id == admin_id and chat.type == "private")


async def _delete_user_message(update: Update) -> None:
    """Best-effort removal of credentials/codes from the bot chat."""
    if update.message:
        try:
            await update.message.delete()
        except Exception:
            pass


async def _delete_message_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Delete a temporary bot message after its short security window."""
    data = context.job.data
    try:
        await context.bot.delete_message(chat_id=data["chat_id"], message_id=data["message_id"])
    except Exception:
        logger.debug("Temporary session message could not be deleted", exc_info=True)


async def _send_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
    """Replace the previous bot prompt so completed stages do not remain in chat."""
    previous = context.user_data.pop("session_prompt", None)
    if previous:
        try:
            await context.bot.delete_message(
                chat_id=previous["chat_id"], message_id=previous["message_id"]
            )
        except Exception:
            pass
    message = await update.effective_chat.send_message(text)
    context.user_data["session_prompt"] = {
        "chat_id": message.chat_id,
        "message_id": message.message_id,
    }
    return message


def _normalize_phone(raw_phone: str) -> str | None:
    """Accept +country, spaced digits, or 00country formats and return E.164-like text."""
    value = raw_phone.strip()
    digits = re.sub(r"\D", "", value)
    if digits.startswith("00"):
        digits = digits[2:]
    if not 8 <= len(digits) <= 15:
        return None
    return f"+{digits}"


async def session_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    admin_id = context.application.bot_data["admin_id"]
    if not _is_private_admin(update, admin_id):
        if update.message:
            await update.message.reply_text("This command is available only to the bot administrator in a private chat.")
        return ConversationHandler.END

    context.user_data.clear()
    await _send_prompt(
        update, context,
        "Session generator started.\n\n"
        "Send your Telegram API ID (the numeric value from my.telegram.org).\n"
        "Use /cancel to stop."
    )
    return API_ID


async def receive_api_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.message:
        return API_ID
    try:
        api_id = int(update.message.text.strip())
        if api_id <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("API ID must be a positive number. Try again or use /cancel.")
        return API_ID
    context.user_data["session_api_id"] = api_id
    await _delete_user_message(update)
    await _send_prompt(update, context, "Now send your API hash.")
    return API_HASH


async def receive_api_hash(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.message:
        return API_HASH
    api_hash = update.message.text.strip()
    if not api_hash or len(api_hash) < 16:
        await update.message.reply_text("That API hash looks invalid. Please send the value from my.telegram.org.")
        return API_HASH
    context.user_data["session_api_hash"] = api_hash
    await _delete_user_message(update)
    await _send_prompt(
        update, context,
        "Send the phone number for the Telegram account. You may use + or spaces, for example:\n"
        "+234 916 391 6314\n234 9163916314"
    )
    return PHONE


async def receive_phone(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.message:
        return PHONE
    phone = _normalize_phone(update.message.text)
    if not phone:
        await update.message.reply_text(
            "Please send a valid phone number with country code. + and spaces are optional, for example 234 9163916314."
        )
        return PHONE

    client = TelegramClient(
        StringSession(),
        context.user_data["session_api_id"],
        context.user_data["session_api_hash"],
    )
    try:
        await client.connect()
        await client.send_code_request(phone)
    except Exception as exc:
        logger.warning("Telethon session setup failed while requesting code: %s", exc)
        await client.disconnect()
        await update.message.reply_text("Could not request a login code. Check the API credentials and phone number, then use /session again.")
        context.user_data.clear()
        return ConversationHandler.END

    context.user_data["session_client"] = client
    context.user_data["session_phone"] = phone
    await _delete_user_message(update)
    await _send_prompt(
        update, context,
        "A login code was sent by Telegram. Send that code here. If Telegram displays it with spaces, send it without spaces."
    )
    return CODE


async def receive_code(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.message:
        return CODE
    code = update.message.text.strip().replace(" ", "")
    client = context.user_data.get("session_client")
    if not client:
        await update.message.reply_text("This session expired. Start again with /session.")
        return ConversationHandler.END
    try:
        await client.sign_in(phone=context.user_data["session_phone"], code=code)
    except SessionPasswordNeededError:
        await _delete_user_message(update)
        await _send_prompt(update, context, "Two-step verification is enabled. Send your Telegram 2FA password.")
        return PASSWORD
    except Exception as exc:
        logger.warning("Telethon sign-in failed: %s", exc)
        await update.message.reply_text("The login code was rejected. Start again with /session.")
        await client.disconnect()
        context.user_data.clear()
        return ConversationHandler.END

    await _delete_user_message(update)
    return await _finish_session(update, context, client)


async def receive_password(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.message:
        return PASSWORD
    client = context.user_data.get("session_client")
    if not client:
        await update.message.reply_text("This session expired. Start again with /session.")
        return ConversationHandler.END
    try:
        await client.sign_in(password=update.message.text)
    except Exception as exc:
        logger.warning("Telethon 2FA sign-in failed: %s", exc)
        await update.message.reply_text("The 2FA password was rejected. Start again with /session.")
        await client.disconnect()
        context.user_data.clear()
        return ConversationHandler.END

    await _delete_user_message(update)
    return await _finish_session(update, context, client)


async def _finish_session(update: Update, context: ContextTypes.DEFAULT_TYPE, client: TelegramClient) -> int:
    try:
        string = client.session.save()
        await client.send_message("me", f"Generated Telethon session string:\n\n{string}")
        prompt = context.user_data.pop("session_prompt", None)
        if prompt:
            try:
                await context.bot.delete_message(
                    chat_id=prompt["chat_id"], message_id=prompt["message_id"]
                )
            except Exception:
                pass
        output = await update.effective_chat.send_message(
            "<b>Session generated.</b> It was also sent to Saved Messages.\n\n"
            "Tap the code below to copy it. This message will be deleted in 1 minute.\n\n"
            f"<code>{html_escape(string)}</code>",
            parse_mode="HTML",
        )
        context.job_queue.run_once(
            _delete_message_job,
            when=60,
            data={"chat_id": output.chat_id, "message_id": output.message_id},
            name=f"delete-session-{output.message_id}",
        )
    except Exception as exc:
        logger.exception("Could not save generated Telethon session: %s", exc)
        await update.effective_chat.send_message("The session was created, but I could not send it to Saved Messages.")
    finally:
        await client.disconnect()
        context.user_data.clear()
    return ConversationHandler.END


async def cancel_session(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    client = context.user_data.pop("session_client", None)
    if client:
        await client.disconnect()
    prompt = context.user_data.pop("session_prompt", None)
    if prompt:
        try:
            await context.bot.delete_message(chat_id=prompt["chat_id"], message_id=prompt["message_id"])
        except Exception:
            pass
    context.user_data.clear()
    if update.message:
        await update.message.reply_text("Session generation cancelled.")
    return ConversationHandler.END


def build_session_conversation(admin_id: int) -> ConversationHandler:
    from telegram.ext import CommandHandler, MessageHandler, filters

    return ConversationHandler(
        entry_points=[CommandHandler("session", session_command)],
        states={
            API_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_api_id)],
            API_HASH: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_api_hash)],
            PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_phone)],
            CODE: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_code)],
            PASSWORD: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_password)],
        },
        fallbacks=[CommandHandler("cancel", cancel_session)],
        name="session_generator",
        persistent=False,
    )


def configure_session_conversation(conversation: ConversationHandler) -> ConversationHandler:
    """Compatibility wrapper for callers that already build the conversation."""
    return conversation


__all__ = ["build_session_conversation", "configure_session_conversation"]
