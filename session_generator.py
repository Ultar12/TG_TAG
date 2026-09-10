"""Admin-only Telethon StringSession generator for the Telegram bot."""

import logging

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


async def session_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    admin_id = context.application.bot_data["admin_id"]
    if not _is_private_admin(update, admin_id):
        if update.message:
            await update.message.reply_text("This command is available only to the bot administrator in a private chat.")
        return ConversationHandler.END

    context.user_data.clear()
    await update.message.reply_text(
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
    await update.effective_chat.send_message("Now send your API hash.")
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
    await update.effective_chat.send_message(
        "Send the phone number for the Telegram account, including the country code (for example +15551234567)."
    )
    return PHONE


async def receive_phone(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.message:
        return PHONE
    phone = update.message.text.strip()
    if not phone.startswith("+") or len(phone) < 8:
        await update.message.reply_text("Please send a phone number with country code, starting with +.")
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
    await update.effective_chat.send_message(
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
        await update.effective_chat.send_message("Two-step verification is enabled. Send your Telegram 2FA password.")
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
        await update.effective_chat.send_message(
            "Session generated successfully and sent to your Telegram Saved Messages. "
            "For security, it is not displayed in this bot chat."
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
