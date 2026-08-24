# -*- coding: utf-8 -*-

# --- Imports ---
import logging
import os
import sys
import asyncio
import uuid
import shutil
import time
import requests
import json
from bs4 import BeautifulSoup
import base64
import pymupdf as fitz  # PyMuPDF
import google.generativeai as genai
import openai # For DALL-E image creation
import pytesseract # For OCR
from PIL import Image # For OCR
import io # For OCR
import smtplib
from email.message import EmailMessage
import shlex # For smart command parsing
import re # For robust button handling
from gtts import gTTS
import random
import subprocess # For running the Node.js script
import datetime # For the recurring email job

from telegram import Update, ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters, CallbackQueryHandler, AIORateLimiter, JobQueue
from telegram.constants import ParseMode, ChatAction
from telegram.helpers import escape_markdown

from sqlalchemy import create_engine, Column, String, text
from sqlalchemy.orm import sessionmaker, declarative_base
from sqlalchemy.exc import OperationalError, IntegrityError
from sqlalchemy.types import BigInteger
import yt_dlp

from prettytable import PrettyTable

# Load environment variables from .env file for local development
from dotenv import load_dotenv
from pathlib import Path

# Load the repository-local .env for local development and dashboard deployments
# that include the tracked file. Platform config vars still take precedence.
load_dotenv(dotenv_path=Path(__file__).resolve().parent / ".env")

# --- Configuration ---
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# --- Environment Variables & API Configurations ---
BOT_TOKEN = os.environ.get("BOT_TOKEN")
DATABASE_URL = os.environ.get("DATABASE_URL")
ADMIN_ID = os.environ.get("ADMIN_ID")
# AI Keys
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY") # For DALL-E only
# Email Keys
GMAIL_ADDRESS = os.environ.get("GMAIL_ADDRESS")
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD")
# Other API Keys
REPLICATE_API_TOKEN = os.environ.get("REPLICATE_API_TOKEN") 
STABILITY_API_KEY = os.environ.get("STABILITY_API_KEY")
OPENWEATHER_API_KEY = os.environ.get("OPENWEATHER_API_KEY")
SCREENSHOT_API_KEY = os.environ.get("SCREENSHOT_API_KEY")
TMDB_API_KEY = os.environ.get("TMDB_API_KEY")

# --- Initial Checks ---
missing_required = [
    name for name, value in {
        "BOT_TOKEN": BOT_TOKEN,
        "ADMIN_ID": ADMIN_ID,
    }.items() if not value
]
if missing_required:
    logger.critical("Missing required environment variables: %s. Exiting.", ", ".join(missing_required))
    sys.exit(1)

try:
    ADMIN_ID = int(ADMIN_ID)
except (TypeError, ValueError):
    logger.critical("ADMIN_ID must be a numeric Telegram user ID. Exiting.")
    sys.exit(1)

if not DATABASE_URL:
    DATABASE_URL = "sqlite:///tg_tag.db"
    logger.warning("DATABASE_URL is not set; using ephemeral SQLite database at tg_tag.db.")

# --- API Configurations ---
try:
    if GEMINI_API_KEY:
        genai.configure(api_key=GEMINI_API_KEY)
        gemini_model = genai.GenerativeModel('gemini-1.5-flash')
        logger.info("Gemini AI client configured.")
    else: gemini_model = None; logger.warning("GEMINI_API_KEY not found.")
except Exception as e: gemini_model = None; logger.error(f"Failed to configure Gemini API: {e}")

try:
    if OPENAI_API_KEY:
        openai_client = openai.AsyncOpenAI(api_key=OPENAI_API_KEY)
        logger.info("OpenAI client configured for DALL-E.")
    else: openai_client = None; logger.warning("OPENAI_API_KEY not found.")
except Exception as e: openai_client = None; logger.error(f"Failed to configure OpenAI API: {e}")

# --- Constants & Database Setup ---
DOWNLOAD_DIR = "downloads"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)
# NEW: Define YouTube cookies file
YTDL_COOKIES_FILE = os.environ.get("YTDL_COOKIES_FILE", "cookies_youtube.txt")
YTDL_COOKIE_OPTIONS = {}
if os.path.isfile(YTDL_COOKIES_FILE) and os.path.getsize(YTDL_COOKIES_FILE) > 0:
    YTDL_COOKIE_OPTIONS["cookiefile"] = YTDL_COOKIES_FILE
else:
    logger.info("No YouTube cookies file configured; yt-dlp will run without cookies.")

# Current YouTube extraction requires an external JavaScript runtime and EJS
# challenge scripts. Heroku installs Node.js through the Node buildpack.
YTDL_COMMON_OPTIONS = {
    "js_runtimes": {"node": {}},
    **YTDL_COOKIE_OPTIONS,
}

VIDEO_FORMAT_SELECTOR = (
    "bestvideo[height<=1080]+bestaudio/"
    "best[height<=1080]/"
    "best"
)

async def has_audio_stream(file_path: str) -> bool:
    """Return whether ffprobe detects at least one audio stream."""
    try:
        process = await asyncio.create_subprocess_exec(
            "ffprobe", "-v", "error", "-select_streams", "a:0",
            "-show_entries", "stream=codec_name", "-of", "csv=p=0", file_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await process.communicate()
        return process.returncode == 0 and bool(stdout.strip())
    except (FileNotFoundError, OSError):
        logger.error("ffprobe is not installed; cannot verify audio streams.")
        return False

if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)
Base = declarative_base()

class User(Base):
    __tablename__ = 'users'
    user_id = Column(BigInteger, primary_key=True, nullable=False)
    first_name = Column(String, nullable=True)
    username = Column(String, nullable=True)
    chat_id = Column(BigInteger, primary_key=True, nullable=False)

engine_options = {"pool_pre_ping": True}
if DATABASE_URL.startswith("sqlite"):
    engine_options["connect_args"] = {"check_same_thread": False}
else:
    engine_options.update({"pool_size": 5, "max_overflow": 10})
engine = create_engine(DATABASE_URL, **engine_options)
try: Base.metadata.create_all(engine)
except OperationalError as e: logger.critical(f"Failed to connect to database: {e}. Exiting."); sys.exit(1)
Session = sessionmaker(bind=engine)

# --- User & Notification Functions ---
async def save_user_to_db(update: Update, context: ContextTypes.DEFAULT_TYPE, event_type: str = "User Interacted"):
    if not hasattr(update, 'effective_user') or not update.effective_user: return
    user, chat_id = update.effective_user, update.effective_chat.id
    session = Session()
    try:
        if not session.query(User).filter_by(user_id=user.id, chat_id=chat_id).first():
            new_user = User(user_id=user.id, first_name=user.first_name, username=user.username, chat_id=chat_id)
            session.add(new_user); session.commit()
            if user.id != ADMIN_ID: await send_notification_to_admin(context, {'user_id': user.id, 'first_name': user.first_name, 'username': user.username}, "New User Added")
        elif user.id != ADMIN_ID: await send_notification_to_admin(context, {'user_id': user.id, 'first_name': user.first_name, 'username': user.username}, event_type)
    except IntegrityError: session.rollback()
    except Exception as e: logger.error(f"DB Error for user {user.id}: {e}"); session.rollback()
    finally: session.close()

async def send_notification_to_admin(context: ContextTypes.DEFAULT_TYPE, user_info: dict, event_type: str):
    first_name = user_info.get('first_name', 'N/A')
    username = f"@{user_info.get('username')}" if user_info.get('username') else "Not set"
    message = (f"Interaction: {event_type}\n" f"User: {first_name} (ID: `{user_info.get('user_id')}`)\n" f"Username: {username}")
    try: await context.bot.send_message(chat_id=ADMIN_ID, text=message, parse_mode=ParseMode.MARKDOWN)
    except Exception as e: logger.error(f"Failed to send admin notification: {e}")

# --- Menu Functions ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    keyboard = [
        [KeyboardButton("AI Tools"), KeyboardButton("Media Tools")],
        [KeyboardButton("Utilities"), KeyboardButton("Help")],
        [KeyboardButton("Send Suggestion")]
    ]
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    await update.message.reply_text("Welcome! How can I help you today?\nSelect an option from the menu below.", reply_markup=reply_markup)

async def show_ai_tools_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    keyboard = [
        [KeyboardButton("Chat with AI"), KeyboardButton("Create Image")],
        [KeyboardButton("Read Text from Image"), KeyboardButton("Text to Speech")],
        [KeyboardButton("Animate Image"), KeyboardButton("Upscale Image")],
        [KeyboardButton("Summarize Link"), KeyboardButton("Summarize File")],
        [KeyboardButton("Back to Main Menu")]
    ]
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    await update.message.reply_text("AI Tools:", reply_markup=reply_markup)

async def show_media_tools_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    keyboard = [
        [KeyboardButton("Play Music / Video"), KeyboardButton("Download Media")],
        [KeyboardButton("Download PDF"), KeyboardButton("Search TikTok")],
        [KeyboardButton("Youtube"), KeyboardButton("Search Movie")],
        [KeyboardButton("Back to Main Menu")]
    ]
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    await update.message.reply_text("Media Tools:", reply_markup=reply_markup)

async def show_utilities_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    keyboard = [
        [KeyboardButton("Weather"), KeyboardButton("Crypto Prices")],
        [KeyboardButton("Translate Text"), KeyboardButton("Tell a Joke")],
        [KeyboardButton("Ask a Riddle"), KeyboardButton("Take Screenshot")],
    ]
    if update.effective_user.id == ADMIN_ID:
        keyboard.append([KeyboardButton("Send Email (Admin)")])
    keyboard.append([KeyboardButton("Convert Video to Audio")])
    keyboard.append([KeyboardButton("Back to Main Menu")])
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    await update.message.reply_text("Utilities:", reply_markup=reply_markup)

# --- Feature Functions ---
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    help_text = (
        "**Bot Commands Guide:**\n\n"
        "You can use the menu buttons or the following commands:\n\n"
        "**/gemini <prompt>**: Ask the AI a question.\n"
        "**/create <prompt>**: Generate an image from text.\n"
        "**/novel <title>**: Search for a novel to download.\n"
        "**/movie <title>**: Get information about a movie.\n"
        "**/tiktoksearch <query>**: Search for TikTok videos.\n"
        "**/ytsearch <query>**: Search for YouTube videos.\n"
        "**/play <song name>**: Search and download a song or video.\n"
        "**/tts <text>**: Convert text to speech.\n"
        "**/mp4**: Reply to a video to convert it to an MP3 audio file.\n"
        "**/4k**: Reply to a video to upscale its quality to 4K resolution (Warning: heavy process).\n"
        "**/gmail**: Send an email (Admin only).\n"
        "**/connect +<number>**: Connect to a WhatsApp account using a pairing code."
    )
    await update.message.reply_text(help_text, parse_mode=ParseMode.MARKDOWN)

async def db_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("Sorry, this command is for the admin only.")
        return
    await save_user_to_db(update, context, event_type="Used /db command")
    context.user_data['state'] = 'awaiting_db_table_name'
    await update.message.reply_text("Please enter the name of the database table you want to view (e.g., `users`).")

async def view_db_table(update: Update, context: ContextTypes.DEFAULT_TYPE, table_name: str) -> None:
    session = Session()
    feedback = await update.message.reply_text(f"Fetching data from table `{table_name}`...", parse_mode=ParseMode.MARKDOWN)
    try:
        table_name = table_name.strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", table_name):
            await feedback.edit_text("Invalid table name.")
            return
        result_proxy = session.execute(text(f"SELECT * FROM {table_name}"))
        result = result_proxy.fetchall()
        if not result:
            await feedback.edit_text(f"Table `{table_name}` is empty or does not exist.")
            return
        column_names = result_proxy.keys()
        table = PrettyTable()
        table.field_names = column_names
        for row in result:
            table.add_row(row)
        formatted_table = f"```\n{str(table)}\n```"
        if len(formatted_table) > 4000:
            await feedback.edit_text("The table content is too large to display.")
            return
        await feedback.edit_text(formatted_table, parse_mode=ParseMode.MARKDOWN_V2)
    except OperationalError:
        await feedback.edit_text(f"Database error: The table `{table_name}` may not exist.")
    except Exception as e:
        logger.error(f"Error viewing table {table_name}: {e}")
        await feedback.edit_text("An unexpected error occurred.")
    finally:
        session.close()

async def start_ai_chat(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await save_user_to_db(update, context, event_type="Started AI Chat")
    context.user_data['state'] = 'continuous_chat'
    context.user_data['gemini_history'] = []
    chat_keyboard = [[KeyboardButton("End Chat")]]
    reply_markup = ReplyKeyboardMarkup(chat_keyboard, resize_keyboard=True)
    await update.message.reply_text("You are now in a continuous chat with the AI.\n\nSend your message, or press 'End Chat' to return to the main menu.", reply_markup=reply_markup)

async def end_chat(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.pop('state', None)
    context.user_data.pop('gemini_history', None)
    await update.message.reply_text("Chat ended. Returning to the main menu.")
    await start(update, context)

async def gemini_command(update: Update, context: ContextTypes.DEFAULT_TYPE, prompt_text: str = None) -> None:
    if not gemini_model:
        await update.message.reply_text("AI service is not configured.")
        return
    is_continuous_chat = context.user_data.get('state') == 'continuous_chat'
    history = context.user_data.get('gemini_history', []) if is_continuous_chat else []
    if not prompt_text:
        prompt_text = " ".join(context.args) if not is_continuous_chat else update.message.text
    if not prompt_text:
        await update.message.reply_text("Please provide a prompt.")
        return
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)
    try:
        chat_session = gemini_model.start_chat(history=history)
        response = await asyncio.to_thread(chat_session.send_message, prompt_text)
        if is_continuous_chat:
            context.user_data['gemini_history'] = chat_session.history
        safe_reply = escape_markdown(response.text, version=2)
        await update.message.reply_text(safe_reply, parse_mode=ParseMode.MARKDOWN_V2)
    except Exception as e:
        logger.error(f"Gemini command error: {e}")
        await update.message.reply_text("Sorry, an error occurred with the AI.")

async def gmail_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("Sorry, this command is for the admin only.")
        return
    if not GMAIL_ADDRESS or not GMAIL_APP_PASSWORD:
        await update.message.reply_text("The email service is not configured.")
        return
    context.user_data['state'] = 'awaiting_email_address'
    await update.message.reply_text("Step 1 of 3: Please enter the recipient's email address.\n\nFor multiple recipients, separate them with commas.")

# --- Functions for recurring email feature ---

async def resend_email_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """The job function that resends the email."""
    job = context.job
    data = job.data
    
    # weekday() returns 0 for Monday and 6 for Sunday.
    if datetime.datetime.now().weekday() == data['stop_day_index']:
        logger.info(f"Stopping recurring email job {job.name} as it has reached the stop day.")
        await context.bot.send_message(
            chat_id=data['chat_id'], 
            text=f"Recurring email to *{escape_markdown(data['to'], version=2)}* has now stopped as scheduled\\.",

            parse_mode=ParseMode.MARKDOWN_V2
        )
        job.schedule_removal()
        return

    logger.info(f"Running recurring email job {job.name} to {data['to']}")
    msg = EmailMessage()
    msg.set_content(data['body'])
    msg['Subject'] = data['subject']
    msg['From'] = GMAIL_ADDRESS
    msg['To'] = data['to'] # This string can contain comma-separated addresses
    
    try:
        with smtplib.SMTP_SSL('smtp.gmail.com', 465) as s:
            s.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
            s.send_message(msg)
        logger.info(f"Successfully resent email via job {job.name}")
    except Exception as e:
        logger.error(f"Failed to send email via job {job.name}: {e}")
        await context.bot.send_message(
            chat_id=data['chat_id'], 
            text=f"⚠️ Failed to send recurring email to *{escape_markdown(data['to'], version=2)}*\\.",

            parse_mode=ParseMode.MARKDOWN_V2
        )

async def handle_resend_interval_selection(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles the user's selection of the resend interval."""
    query = update.callback_query
    await query.answer()

    _, interval_str, job_id = query.data.split(':', 2)
    interval = int(interval_str)

    if job_id not in context.chat_data:
        await query.edit_message_text("This request has expired. Please try sending the email again.")
        return

    context.chat_data[job_id]['interval'] = interval

    days = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    keyboard = []
    days_buttons = [InlineKeyboardButton(day, callback_data=f"resend_stop:{i}:{job_id}") for i, day in enumerate(days)]
    keyboard.append(days_buttons[:4])
    keyboard.append(days_buttons[4:])
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.edit_message_text(
        f"Great\\! The email will be resent every {interval // 60} minutes\\.\n\nOn which day should it stop?",

        reply_markup=reply_markup,
        parse_mode=ParseMode.MARKDOWN_V2
    )

async def handle_resend_stop_day_selection(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles the user's selection of the stop day and schedules the job."""
    query = update.callback_query
    await query.answer()

    _, stop_day_index_str, job_id = query.data.split(':', 2)
    stop_day_index = int(stop_day_index_str)

    if job_id not in context.chat_data or 'interval' not in context.chat_data.get(job_id, {}):
        await query.edit_message_text("This request has expired or is incomplete. Please try sending the email again.")
        return
        
    email_data = context.chat_data.pop(job_id)
    
    job_data = {
        'chat_id': query.message.chat_id,
        'to': email_data['to'],
        'subject': email_data['subject'],
        'body': email_data['body'],
        'stop_day_index': stop_day_index
    }
    
    interval = email_data['interval']
    
    context.job_queue.run_repeating(
        resend_email_job,
        interval=interval,
        first=interval,
        data=job_data,
        name=f"email_{job_id}"
    )
    
    days = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    stop_day_name = days[stop_day_index]
    
    await query.edit_message_text(
        f"✅ All set\\! I will resend the email to *{escape_markdown(email_data['to'], version=2)}* every {interval // 60} minutes\\. This will stop on *{stop_day_name}*\\.",

        parse_mode=ParseMode.MARKDOWN_V2
    )

async def handle_resend_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Cancels the resend setup."""
    query = update.callback_query
    await query.answer()
    
    try:
        _, job_id = query.data.split(':', 1)
        if job_id in context.chat_data:
            del context.chat_data[job_id]
    except (ValueError, KeyError):
        pass
        
    await query.edit_message_text("Okay, no recurring email will be sent.")

# --- END of recurring email functions ---

async def movie_command(update: Update, context: ContextTypes.DEFAULT_TYPE, title: str = None) -> None:
    if not TMDB_API_KEY:
        await update.message.reply_text("❌ Movie search service is not configured. Set `TMDB_API_KEY` in your environment.")
        return
    if not title:
        title = " ".join(context.args)
        if not title:
            await update.message.reply_text("Please provide a movie title. Usage: `/movie The Matrix`")
            return
    feedback = await update.message.reply_text(f"[Searching] for '{title}'...")
    try:
        search_url = f"https://api.themoviedb.org/3/search/movie"
        params = {"api_key": TMDB_API_KEY, "query": title}
        response = requests.get(search_url, params=params, timeout=10)
        
        # Check for API errors
        if response.status_code == 401:
            await feedback.edit_text("❌ Invalid TMDB API key. Please check your `TMDB_API_KEY`.")
            return
        elif response.status_code == 429:
            await feedback.edit_text("⚠️ Rate limited. Please try again in a few moments.")
            return
        
        response.raise_for_status()
        results = response.json().get('results')
        if not results:
            await feedback.edit_text("Sorry, I couldn't find any movie with that title.")
            return
        movie = results[0]
        caption = (
            f"[Movie] *{escape_markdown(movie.get('title', 'N/A'), version=2)} ({movie.get('release_date', '----').split('-')[0]})*\n\n"
            f"⭐ *Rating:* {movie.get('vote_average', 0):.1f}/10\n\n"
            f"_{escape_markdown(movie.get('overview', 'No summary available.'), version=2)}_"
        )
        poster_path = movie.get('poster_path')
        if poster_path:
            poster_url = f"https://image.tmdb.org/t/p/w500{poster_path}"
            await context.bot.send_photo(chat_id=update.effective_chat.id, photo=poster_url, caption=caption, parse_mode=ParseMode.MARKDOWN_V2)
            await feedback.delete()
        else:
            await feedback.edit_text(caption, parse_mode=ParseMode.MARKDOWN_V2)
    except requests.exceptions.Timeout:
        logger.error(f"Movie command timeout")
        await feedback.edit_text("❌ Request timed out. TMDB service may be slow or offline.")
    except Exception as e:
        logger.error(f"Movie command failed: {e}")
        await feedback.edit_text(f"❌ Search failed: {str(e)[:100]}.")

async def screenshot_command(update: Update, context: ContextTypes.DEFAULT_TYPE, url: str = None) -> None:
    if not SCREENSHOT_API_KEY:
        await update.message.reply_text("Screenshot service is not configured.")
        return
    if not url:
        url = " ".join(context.args)
        if not url:
            await update.message.reply_text("Please provide a URL. Usage: `/screenshot google.com`")
            return
    if not url.startswith('http'):
        url = 'http://' + url
    feedback = await update.message.reply_text(f"Capturing screenshot for `{url}`...")
    try:
        api_url = f"https://shot.screenshotapi.net/screenshot?token={SCREENSHOT_API_KEY}&url={url}&full_page=true&fresh=true&output=image&file_type=png"
        response = requests.get(api_url, timeout=45)
        if response.status_code == 200:
            await context.bot.send_photo(chat_id=update.effective_chat.id, photo=response.content, caption=f"Screenshot of `{url}`", parse_mode=ParseMode.MARKDOWN)
            await feedback.delete()
        else:
            await feedback.edit_text("Sorry, failed to capture the screenshot.")
    except Exception as e:
        logger.error(f"Screenshot command failed: {e}")
        await feedback.edit_text("An unexpected error occurred.")

async def read_text_from_image_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message.reply_to_message or not update.message.reply_to_message.photo:
        await update.message.reply_text("Please reply to an image with /readtext.")
        return
    feedback = await update.message.reply_text("Reading text from image...")
    try:
        photo_file = await update.message.reply_to_message.photo[-1].get_file()
        image = Image.open(io.BytesIO(await photo_file.download_as_bytearray()))
        text = await asyncio.to_thread(pytesseract.image_to_string, image)
        await feedback.edit_text(f"**Extracted Text:**\n\n{text}" if text.strip() else "Couldn't find any readable text.")
    except Exception as e:
        logger.error(f"OCR Error: {e}")
        await feedback.edit_text("Sorry, an error occurred while processing the image.")

async def summarize_url(update: Update, context: ContextTypes.DEFAULT_TYPE, url: str) -> None:
    if not gemini_model:
        await update.message.reply_text("AI summarizer is not configured.")
        return
    feedback = await update.message.reply_text("Analyzing link...")
    try:
        response = requests.get(url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=15)
        soup = BeautifulSoup(response.content, 'html.parser')
        article_text = ' '.join(p.get_text() for p in soup.find_all('p'))
        if len(article_text) < 100:
            await feedback.edit_text("Couldn't extract enough text to summarize.")
            return
        await feedback.edit_text("Content extracted. Summarizing with AI...")
        prompt = f"Please provide a concise but comprehensive summary of the following article text:\n\n{article_text[:15000]}"
        ai_response = await asyncio.to_thread(gemini_model.generate_content, prompt)
        await feedback.edit_text(ai_response.text, parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        logger.error(f"Summarize URL error: {e}")
        await feedback.edit_text("Sorry, I couldn't read or summarize that URL.")

async def summarize_file_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not gemini_model:
        await update.message.reply_text("AI service is not configured.")
        return
    if not update.message.reply_to_message:
        await update.message.reply_text("Please reply to an image or a PDF file with /summarize_file.")
        return
    replied_message = update.message.reply_to_message
    feedback = await replied_message.reply_text("Processing file...")
    try:
        text_to_summarize = ""
        if replied_message.photo:
            photo_file = await replied_message.photo[-1].get_file()
            photo_bytes = await photo_file.download_as_bytearray()
            image_part = {"mime_type": "image/jpeg", "data": photo_bytes}
            response = await asyncio.to_thread(gemini_model.generate_content, ["Describe this image in detail.", image_part])
            text_to_summarize = response.text
        elif replied_message.document and replied_message.document.mime_type == 'application/pdf':
            pdf_file = await replied_message.document.get_file()
            pdf_bytes = await pdf_file.download_as_bytearray()
            with fitz.open(stream=pdf_bytes, filetype="pdf") as doc:
                text_to_summarize = "".join(page.get_text() for page in doc)
            if not text_to_summarize.strip():
                await feedback.edit_text("Could not extract any text from this PDF.")
                return
            prompt = f"Please provide a detailed summary of the following document:\n\n{text_to_summarize[:15000]}"
            response = await asyncio.to_thread(gemini_model.generate_content, prompt)
            text_to_summarize = response.text
        else:
            await feedback.edit_text("This command only works on an image or PDF file.")
            return
        await feedback.edit_text(f"**Summary:**\n\n{text_to_summarize}", parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        logger.error(f"File summarization error: {e}")
        await feedback.edit_text("Sorry, an error occurred while processing the file.")

async def create_image_command(update: Update, context: ContextTypes.DEFAULT_TYPE, prompt: str = None) -> None:
    if not openai_client:
        await update.message.reply_text("Image generation service (OpenAI) is not configured.")
        return
    if not prompt:
        prompt = " ".join(context.args)
    if not prompt:
        await update.message.reply_text("Please describe the image you want to create.")
        return
    feedback = await update.message.reply_text("Creating your image with DALL-E 3...")
    try:
        response = await openai_client.images.generate(model="dall-e-3", prompt=prompt, n=1, size="1024x1024", quality="standard")
        await context.bot.send_photo(update.effective_chat.id, photo=response.data[0].url, caption=f"Creation: `{prompt}`", parse_mode=ParseMode.MARKDOWN)
        await feedback.delete()
    except Exception as e:
        logger.error(f"DALL-E 3 API error: {e}")
        await feedback.edit_text("Sorry, I couldn't create the image.")

async def upscale_image_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not REPLICATE_API_TOKEN:
        await update.message.reply_text("❌ Image upscaling service (Replicate) is not configured. Set `REPLICATE_API_TOKEN` in your environment.")
        return
    if not update.message.reply_to_message or not update.message.reply_to_message.photo:
        await update.message.reply_text("Please reply to an image with the `/upscale` command.")
        return
    feedback = await update.message.reply_text("🚀 Sending image to Replicate for upscaling... (this may take 30-60 seconds)")
    try:
        photo_file = await update.message.reply_to_message.photo[-1].get_file()
        image_bytes = await photo_file.download_as_bytearray()
        
        # Check image size (Replicate has limits)
        image_size_mb = len(image_bytes) / (1024 * 1024)
        if image_size_mb > 5:
            await feedback.edit_text(f"⚠️ Image is quite large ({image_size_mb:.2f} MB). Upscaling may be slow or fail. Please try a smaller image (< 5 MB).")
            return
        
        b64_image = base64.b64encode(image_bytes).decode("utf-8")
        data_uri = f"data:image/png;base64,{b64_image}"
        
        # Replicate 4x upscaler model with detail enhancement
        start_response = requests.post(
            "https://api.replicate.com/v1/predictions",
            headers={"Authorization": f"Token {REPLICATE_API_TOKEN}", "Content-Type": "application/json"},
            json={
                "version": "42fed1c4974146d4d2414e2be2c5236e7a8c90531b54541756316998143e4034",
                "input": {
                    "img": data_uri, 
                    "scale": 4,  # Maximum scale factor (4x upscale)
                    "face_enhance": True  # Enhance facial details
                },
            },
            timeout=10
        )
        start_response.raise_for_status()
        
        prediction_url = start_response.json()["urls"]["get"]
        await feedback.edit_text("⏳ Upscaling job started... Processing your image with AI-powered 4x upscaler.")
        
        result_data = {}
        for attempt in range(60):  # Max 2 minutes (60 * 2s)
            await asyncio.sleep(2)
            poll_response = requests.get(prediction_url, headers={"Authorization": f"Token {REPLICATE_API_TOKEN}"}, timeout=10)
            poll_response.raise_for_status()
            result_data = poll_response.json()
            
            if result_data["status"] == "succeeded":
                break
            elif result_data["status"] in ["failed", "canceled"]:
                error_msg = result_data.get('error', 'Unknown error')
                raise Exception(f"Replicate job failed: {error_msg}")
            
            # Update user every 10 seconds
            if attempt % 5 == 0 and attempt > 0:
                await feedback.edit_text(f"⏳ Still processing... ({attempt * 2} seconds elapsed)")
        
        if result_data.get("status") != "succeeded" or not result_data.get("output"):
            raise Exception("Upscaling job timed out or did not produce output.")
        
        final_image_url = result_data["output"]
        
        await feedback.edit_text("✅ Upscaling complete! Downloading and sending...")
        
        # Download upscaled image
        img_response = requests.get(final_image_url, timeout=30)
        img_response.raise_for_status()
        
        await context.bot.send_photo(
            chat_id=update.effective_chat.id,
            photo=img_response.content,
            caption='✨ Here is your upscaled image (4x resolution, enhanced by AI)!'
        )
        await feedback.delete()
        
    except requests.exceptions.ConnectionError:
        logger.error("Connection error to Replicate API")
        await feedback.edit_text("❌ Connection error. Replicate service may be temporarily unavailable.")
    except Exception as e:
        logger.error(f"Upscale command error: {e}")
        error_str = str(e)
        if "401" in error_str or "Unauthorized" in error_str:
            await feedback.edit_text("❌ Invalid Replicate API token. Check your `REPLICATE_API_TOKEN`.")
        elif "rate" in error_str.lower():
            await feedback.edit_text("❌ Rate limit exceeded. Please wait before trying again.")
        else:
            await feedback.edit_text(f"❌ Upscaling failed: {error_str[:100]}. Please try a different image.")

async def animate_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not STABILITY_API_KEY:
        await update.message.reply_text("❌ Video animation service not configured. Set `STABILITY_API_KEY` in your environment.")
        return
    if not update.message.reply_to_message or not update.message.reply_to_message.photo:
        await update.message.reply_text("Please reply to an image with /animate.")
        return
    feedback = await update.message.reply_text("[Processing] image to animation engine... (this may take 1-2 minutes)")
    try:
        photo_file = await update.message.reply_to_message.photo[-1].get_file()
        image_bytes = await photo_file.download_as_bytearray()
        
        # Check image size
        image_size_mb = len(image_bytes) / (1024 * 1024)
        if image_size_mb > 5:
            await feedback.edit_text(f"⚠️ Image is quite large ({image_size_mb:.2f} MB). Animation may fail. Please try a smaller image (< 5 MB).")
            return
        
        response = requests.post(
            "https://api.stability.ai/v2/generation/image-to-video",
            headers={"authorization": f"Bearer {STABILITY_API_KEY}"},
            files={"image": image_bytes},
            data={"motion_bucket_id": 40},
            timeout=30,
        )
        response.raise_for_status()
        generation_id = response.json()["id"]
        await feedback.edit_text("⏳ Animation processing... this may take a minute. Please be patient.")
        
        for poll_attempt in range(45):  # Max 3 minutes
            await asyncio.sleep(4)
            res = requests.get(
                f"https://api.stability.ai/v2/generation/image-to-video/result/{generation_id}",
                headers={'authorization': f"Bearer {STABILITY_API_KEY}", 'accept': "video/mp4"},
                timeout=20,
            )
            if res.status_code == 200:
                await feedback.edit_text("✅ Animation complete! Uploading video...")
                await context.bot.send_video(
                    update.effective_chat.id, 
                    video=res.content, 
                    caption="[Animation] Here is your animated video!"
                )
                await feedback.delete()
                return
            elif res.status_code >= 400:
                raise Exception(f"API error {res.status_code}: {res.text[:200]}")
            
            # Update progress
            if poll_attempt % 10 == 0 and poll_attempt > 0:
                await feedback.edit_text(f"⏳ Still animating... ({poll_attempt * 4} seconds elapsed)")
        
        await feedback.edit_text("❌ Animation timed out. The video generation took too long. Please try a different image.")
        
    except requests.exceptions.ConnectionError:
        logger.error("Connection error to Stability AI API")
        await feedback.edit_text("❌ Connection error. Stability AI service may be temporarily unavailable.")
    except Exception as e:
        logger.error(f"Animate command error: {e}")
        error_str = str(e)
        if "401" in error_str or "Unauthorized" in error_str:
            await feedback.edit_text("❌ Invalid Stability API key. Check your `STABILITY_API_KEY`.")
        elif "403" in error_str or "Forbidden" in error_str:
            await feedback.edit_text("❌ Access denied. Your Stability API account may have insufficient credits or be restricted.")
        else:
            await feedback.edit_text(f"❌ Animation failed: {error_str[:100]}. Please try a different image.")
    
# --- NEW 4K VIDEO UPSCALING COMMAND ---
async def four_k_upscale_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message.reply_to_message or not update.message.reply_to_message.video:
        await update.message.reply_text("Please reply to a video with the `/4k` command to upscale it.")
        return
    
    feedback = await update.message.reply_text("[Downloading] video...")
    temp_dir = os.path.join(DOWNLOAD_DIR, str(uuid.uuid4()))
    os.makedirs(temp_dir)
    
    try:
        video_file = await update.message.reply_to_message.video.get_file()
        input_path = os.path.join(temp_dir, "input.mp4")
        await video_file.download_to_drive(input_path)
        
        output_path = os.path.join(temp_dir, "upscaled_4k.mp4")
        
        # Check size before starting the expensive process
        if os.path.getsize(input_path) > 10 * 1024 * 1024:
             await feedback.edit_text("⚠️ The video is quite large. Upscaling may be very slow or fail due to processing limits. Proceeding with caution...")
        
        await feedback.edit_text("[Processing] Starting 4K Upscale and Quality Enhancement...")

        # --- EFFICIENT FFmpeg COMMAND APPLIED ---
        # Scale to 4K (3840x2160)
        # -b:v 5000k: Reduced bitrate to 5 Mbps (more stable for low-cost hosting)
        # -preset veryfast: Prioritize speed over file size optimization
        ffmpeg_cmd = [
            'ffmpeg', '-i', input_path,
            '-vf', 'scale=3840:2160:flags=lanczos',
            '-c:v', 'libx264',
            '-crf', '28', # A higher CRF value gives smaller file size at the expense of quality.
            '-b:v', '5000k', # Target max video bitrate of 5 Mbps
            '-maxrate', '5500k',
            '-bufsize', '10000k',
            '-c:a', 'copy',
            '-preset', 'veryfast', # Significant speed boost
            output_path
        ]
        # --- END EFFICIENT FFmpeg COMMAND ---
        
        process = await asyncio.create_subprocess_exec(*ffmpeg_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        stdout, stderr = await process.communicate()
        
        if process.returncode != 0:
            error_output = stderr.decode()
            logger.error(f"FFmpeg 4K upscale failed: {error_output}")
            await feedback.edit_text(f"❌ 4K upscale failed! The process was terminated. Please try a much shorter video (under 10 seconds). FFmpeg Error: {error_output[:200]}...")
            return

        await feedback.edit_text("⬆️ 4K Upscale complete. Uploading video...")
        
        # Final file size check against Telegram's 50MB limit
        if os.path.getsize(output_path) > 49 * 1024 * 1024:
            await feedback.edit_text("The final 4K video is too large (over 50 MB) for Telegram. Please try a shorter source video.")
            return

        with open(output_path, 'rb') as f:
            await context.bot.send_video(
                chat_id=update.effective_chat.id, 
                video=f, 
                caption="✅ Video upscaled to 4K HD resolution!", 
                supports_streaming=True
            )
        await feedback.delete()
        
    except Exception as e:
        logger.error(f"Error in /4k command: {e}")
        await feedback.edit_text("An unexpected error occurred during the 4K upscale process.")
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)
# --- END NEW 4K VIDEO UPSCALING COMMAND ---

async def convert_video_to_audio(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message.reply_to_message or not update.message.reply_to_message.video:
        await update.message.reply_text("Please reply to a video with the `/mp4` command.")
        return
    feedback = await update.message.reply_text("Downloading video...")
    temp_dir = os.path.join(DOWNLOAD_DIR, str(uuid.uuid4()))
    os.makedirs(temp_dir)
    try:
        video_file = await update.message.reply_to_message.video.get_file()
        video_path = os.path.join(temp_dir, "video.mp4")
        await video_file.download_to_drive(video_path)
        await feedback.edit_text("Converting to audio...")
        audio_path = os.path.join(temp_dir, "audio.mp3")
        ffmpeg_cmd = ['ffmpeg', '-i', video_path, '-q:a', '0', '-map', 'a', audio_path]
        process = await asyncio.create_subprocess_exec(*ffmpeg_cmd)
        await process.wait()
        if process.returncode != 0:
            await feedback.edit_text("Sorry, the conversion failed.")
            return
        await feedback.edit_text("Uploading audio...")
        with open(audio_path, 'rb') as f:
            await context.bot.send_audio(chat_id=update.effective_chat.id, audio=f)
        await feedback.delete()
    except Exception as e:
        logger.error(f"Error in /mp4 command: {e}")
        await feedback.edit_text("An unexpected error occurred.")
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)

async def tts_command(update: Update, context: ContextTypes.DEFAULT_TYPE, text_to_speak: str = None) -> None:
    text = text_to_speak or " ".join(context.args)
    if not text and update.message.reply_to_message:
        text = update.message.reply_to_message.text
    if not text:
        await update.message.reply_text("Usage: `/tts <text>` or reply to a message.")
        return
    if len(text) > 1000:
        await update.message.reply_text("Text is too long (max 1000 characters).")
        return
    feedback = await update.message.reply_text("Generating audio...")
    temp_audio_path = os.path.join(DOWNLOAD_DIR, f"{uuid.uuid4()}.mp3")
    try:
        await asyncio.to_thread(gTTS(text=text, lang='en').save, temp_audio_path)
        with open(temp_audio_path, 'rb') as f:
            await context.bot.send_audio(chat_id=update.effective_chat.id, audio=f)
        await feedback.delete()
    except Exception as e:
        logger.error(f"gTTS Error: {e}")
        await feedback.edit_text("Sorry, an error occurred while generating the audio.")
    finally:
        if os.path.exists(temp_audio_path):
            os.remove(temp_audio_path)

# --- NEW: Corrected and Improved TikTok Search Functions ---

async def tiktok_search_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Handles the /tiktoksearch command. 
    It extracts the query from the user's message and proceeds to ask for the video count.
    """
    query = " ".join(context.args)
    if not query:
        # If the user just types /tiktoksearch, prompt them for a query via the state handler.
        await prompt_for_input(
            update, 
            context, 
            state='awaiting_tiktok_query', 
            message="What would you like to search for on TikTok?", 
            event="Used /tiktoksearch"
        )
        return
    
    # If a query was provided (e.g., /tiktoksearch funny cats), ask for the count directly.
    await ask_for_tiktok_count(update, context, query)

async def ask_for_tiktok_count(update: Update, context: ContextTypes.DEFAULT_TYPE, query: str) -> None:
    """
    Asks the user how many videos they want to receive for a given search query.
    """
    context.user_data['tiktok_query'] = query
    
    keyboard = [[
        InlineKeyboardButton("3", callback_data="tiktok_count:3"),
        InlineKeyboardButton("5", callback_data="tiktok_count:5"),
        InlineKeyboardButton("10", callback_data="tiktok_count:10")
    ]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(f"How many videos for '{query}'?", reply_markup=reply_markup)

async def handle_tiktok_count_selection(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Handles the button press for the video count, performs the API call, and sends the videos.
    """
    cb_query = update.callback_query
    await cb_query.answer()
    
    count = int(cb_query.data.split(":")[1])
    search_query = context.user_data.pop('tiktok_query', None)
    
    if not search_query:
        await cb_query.edit_message_text("This search has expired. Please try again.")
        return

    await cb_query.edit_message_text(f"Searching TikTok for '{search_query}'...")
    
    try:
        response = requests.post(
            'https://tikwm.com/api/feed/search',
            headers={"Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"},
            data={'keywords': search_query, 'count': '20'},
            timeout=20,
        )
        response.raise_for_status()
        videos = response.json().get('data', {}).get('videos', [])
        
        if not videos:
            await cb_query.edit_message_text("No TikTok videos found for that search.")
            return

        await cb_query.edit_message_text(f"Found {len(videos)} videos. Sending the top {count} of them...")
        
        random.shuffle(videos)
        sent_count = 0
        for video in videos:
            if sent_count >= count:
                break
            if video.get('play'):
                try:
                    await context.bot.send_video(
                        chat_id=cb_query.message.chat_id, 
                        video=video['play'], 
                        caption=video.get('title', '')
                    )
                    sent_count += 1
                except Exception as send_error:
                    logger.error(f"Failed to send a TikTok video: {send_error}")
        
        if sent_count == 0:
             await cb_query.edit_message_text("Found videos, but was unable to send any of them. They may be protected.")

    except Exception as e:
        logger.error(f"TikTok search failed: {e}")
        await cb_query.edit_message_text("An unexpected error occurred while searching TikTok.")
        
# --- END NEW TikTok Search Functions ---

async def youtube_command(update: Update, context: ContextTypes.DEFAULT_TYPE, query: str = None) -> None:
    if not query:
        query = " ".join(context.args)
        if not query:
            await update.message.reply_text("Please provide a search term.")
            return
    feedback = await update.message.reply_text(f"Searching YouTube for '{query}'...")
    try:
        # --- YOUTUBE SEARCH FIX APPLIED HERE: Using cookies is the best solution ---
        ydl_opts = {
            'noplaylist': True, 
            'quiet': True, 
            'default_search': 'ytsearch5',
            'ignoreerrors': True, # Keep to ignore soft errors
            **YTDL_COMMON_OPTIONS,
        }
        # --- END FIX ---
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(query, download=False)
        if not info or not info.get('entries'):
            await feedback.edit_text("Sorry, couldn't find any results or YouTube blocked the search request. If this continues, check your `cookies_youtube.txt` file.")
            return
        keyboard = [
            [InlineKeyboardButton(
                (v['title'][:60] + '..') if len(v['title']) > 60 else v['title'],
                callback_data=f"play_confirm:{v['id']}"
            )] for v in info['entries'] if v and v.get('id')
        ]
        await feedback.edit_text("Top 5 results:", reply_markup=InlineKeyboardMarkup(keyboard))
    except Exception as e:
        logger.error(f"YouTube error: {e}")
        await feedback.edit_text("An error occurred during the search.")

async def play_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    song_name = " ".join(context.args)
    if not song_name:
        await update.message.reply_text("Please provide a song name.")
        return
    await search_and_play_song(update, context, song_name)

async def search_and_play_song(update: Update, context: ContextTypes.DEFAULT_TYPE, song_name: str) -> None:
    msg_obj = update.callback_query.message if update.callback_query else update.message
    feedback = await msg_obj.reply_text(f"Searching for '{song_name}'...")
    try:
        # --- PLAY SEARCH FIX APPLIED HERE: Using cookies is the best solution ---
        ydl_opts = {
            'noplaylist': True, 
            'quiet': True, 
            'default_search': 'ytsearch1',
            'ignoreerrors': True,
            **YTDL_COMMON_OPTIONS,
        }
        # --- END FIX ---
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(song_name, download=False)
        if not info or not info.get('entries'):
            await feedback.edit_text("Sorry, couldn't find any results or YouTube blocked the search request. If this continues, check your `cookies_youtube.txt` file.")
            return
        video = info['entries'][0]
        keyboard = [
            [InlineKeyboardButton("Yes, that's it!", callback_data=f"play_confirm:{video['id']}")],
            [InlineKeyboardButton("No, cancel", callback_data="play_cancel")]
        ]
        await feedback.edit_text(f"Found: **{video['title']}**\n\nIs this correct?", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        logger.error(f"Play search error: {e}")
        await feedback.edit_text("An error occurred while searching.")

async def handle_play_confirmation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    video_id = query.data.split(":")[1]
    keyboard = [[
        InlineKeyboardButton("Audio", callback_data=f"dl_audio:{video_id}"),
        InlineKeyboardButton("Video", callback_data=f"dl_video:{video_id}")
    ]]
    await query.edit_message_text("Choose your format:", reply_markup=InlineKeyboardMarkup(keyboard))

async def handle_audio_download(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    video_id = query.data.split(":")[1]
    temp_dir = os.path.join(DOWNLOAD_DIR, str(uuid.uuid4()))
    os.makedirs(temp_dir)
    await query.edit_message_text("Downloading audio...")
    try:
        # --- YOUTUBE AUDIO FIX APPLIED HERE: Using cookies is the best solution ---
        audio_opts = {
            'format': 'bestaudio/best',
            'outtmpl': os.path.join(temp_dir, '%(title)s.%(ext)s'),
            'postprocessors': [{'key': 'FFmpegExtractAudio', 'preferredcodec': 'mp3'}],
            'noplaylist': True,
            'quiet': True,
            'ignoreerrors': True,
            **YTDL_COMMON_OPTIONS,
        }
        # --- END FIX ---
        with yt_dlp.YoutubeDL(audio_opts) as ydl:
            info = ydl.extract_info(video_id, download=True)
        downloaded_files = [
            name for name in os.listdir(temp_dir)
            if os.path.isfile(os.path.join(temp_dir, name))
        ]
        if not downloaded_files or not info:
            raise RuntimeError("yt-dlp did not produce an audio file")
        audio_path = os.path.join(temp_dir, downloaded_files[0])
        await query.edit_message_text("Sending audio...")
        with open(audio_path, 'rb') as f:
            await context.bot.send_audio(
                chat_id=query.message.chat_id,
                audio=f,
                title=info.get('title'),
                duration=info.get('duration')
            )
        await query.delete_message()
    except Exception as e:
        logger.error(f"Audio download error: {e}")
        await query.edit_message_text("An error occurred during download.")
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)

async def handle_video_download(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    video_id = query.data.split(":")[1]
    temp_dir = os.path.join(DOWNLOAD_DIR, str(uuid.uuid4()))
    os.makedirs(temp_dir)
    await query.edit_message_text("Checking video details...")
    video_path = None
    
    # --- Constants for size management ---
    SAFE_SIZE_LIMIT_MB = 48  # Safe size limit for local upload to Telegram (below the 50MB hard limit)
    
    try:
        # --- YTDL Options for High Quality Video Download ---
        ydl_opts = {
            'outtmpl': os.path.join(temp_dir, '%(title)s.%(ext)s'),
            'noplaylist': True,
            'quiet': True,
            'ignoreerrors': True,
            **YTDL_COMMON_OPTIONS,
            'format': VIDEO_FORMAT_SELECTOR,
            'merge_output_format': 'mp4' 
        }
        
        await query.edit_message_text("Downloading video to server...")
        with yt_dlp.YoutubeDL(ydl_opts) as ydl_dl:
            ydl_dl.download([video_id])
            
        downloaded_files = [
            os.path.join(temp_dir, name) for name in os.listdir(temp_dir)
            if os.path.isfile(os.path.join(temp_dir, name))
            and not name.endswith((".part", ".ytdl"))
        ]
        if not downloaded_files:
            await query.edit_message_text("Download failed: No file found.")
            return

        video_path = None
        for candidate in downloaded_files:
            if await has_audio_stream(candidate):
                video_path = candidate
                break
        if not video_path:
            await query.edit_message_text(
                "Download failed: no audio stream was found. Please retry after ffmpeg is installed."
            )
            return

        file_size_bytes = os.path.getsize(video_path)
        file_size_mb = file_size_bytes / (1024 * 1024)
        
        # --- 1. DIRECT SEND: If small enough ---
        if file_size_mb <= SAFE_SIZE_LIMIT_MB:
            await query.edit_message_text(f"Video ready ({file_size_mb:.2f} MB). Uploading...")
            with open(video_path, 'rb') as f:
                await context.bot.send_video(
                    chat_id=query.message.chat_id, 
                    video=f, 
                    supports_streaming=True
                )
            await query.delete_message()
            return # Success, exit function
            
        # --- 2. SPLITTING: If too large, attempt fast, copy-stream split ---
        
        await query.edit_message_text(f"Video is too large ({file_size_mb:.2f} MB). Attempting to split into chunks...")
        
        # Determine number of parts based on size (aiming for chunks under 48MB)
        num_parts = max(2, int(file_size_mb // SAFE_SIZE_LIMIT_MB) + 1)
        
        # Get total duration
        ffprobe_cmd = ['ffprobe', '-v', 'error', '-show_entries', 'format=duration', '-of', 'default=noprint_wrappers=1:nokey=1', video_path]
        process = await asyncio.create_subprocess_exec(*ffprobe_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        stdout, _ = await process.communicate()
        total_duration = float(stdout.decode().strip())
        
        chunk_duration = total_duration / num_parts
        split_paths = []
        ffmpeg_commands = [] # This list will hold the awaitable objects (process.wait() coroutines)
        
        for i in range(num_parts):
            start_time = i * chunk_duration
            part_path = os.path.join(temp_dir, f"video_part{i+1}.mp4")
            split_paths.append(part_path)
            
            # Use -ss for seek and -t for duration. -c copy is fast and preserves quality.
            cmd = [
                'ffmpeg', '-i', video_path, 
                '-ss', str(start_time), 
                '-t', str(chunk_duration), 
                '-c', 'copy', 
                part_path
            ]
            
            # Create the process and immediately append the .wait() coroutine
            process = await asyncio.create_subprocess_exec(*cmd)
            ffmpeg_commands.append(process.wait())
            
        # Execute all splits concurrently and wait for them to complete
        await asyncio.gather(*ffmpeg_commands)
        
        # Send the split videos
        sent_count = 0
        await query.edit_message_text("Sending split videos...")
        for i, path in enumerate(split_paths):
            if os.path.exists(path) and os.path.getsize(path) > 0:
                with open(path, 'rb') as f_split:
                    await context.bot.send_video(
                        chat_id=query.message.chat_id, 
                        video=f_split, 
                        caption=f"Video Part {i+1}/{num_parts}", 
                        supports_streaming=True
                    )
                    sent_count += 1
            
        if sent_count == num_parts:
            await query.delete_message()
            return # Success, exit function

        # --- 3. COMPRESSION FALLBACK: If splitting fails or is incomplete ---
        
        await query.edit_message_text("Splitting failed or was incomplete. Attempting to compress the original video as a fallback. This may take a while...")
        
        compressed_path = os.path.join(temp_dir, "compressed.mp4")
        
        # FFmpeg command for robust compression
        ffmpeg_compress = [
            'ffmpeg', '-i', video_path, 
            '-vcodec', 'libx264', '-crf', '30',  
            '-preset', 'fast', '-c:a', 'copy', 
            compressed_path
        ]
        
        process = await asyncio.create_subprocess_exec(*ffmpeg_compress, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        stdout, stderr = await process.communicate()
        
        if process.returncode != 0:
            logger.error(f"FFmpeg Compression failed: {stderr.decode()}")
            await query.edit_message_text("Compression failed! The original video was too large and couldn't be processed.")
            return

        # Check size of compressed file
        compressed_size_mb = os.path.getsize(compressed_path) / (1024 * 1024)
        
        if compressed_size_mb <= SAFE_SIZE_LIMIT_MB:
            await query.edit_message_text(f"Compression successful! ({compressed_size_mb:.2f} MB). Uploading...")
            with open(compressed_path, 'rb') as f_comp:
                await context.bot.send_video(
                    chat_id=query.message.chat_id, 
                    video=f_comp, 
                    caption="Compressed Video (Original was too large)", 
                    supports_streaming=True
                )
            await query.delete_message()
        else:
            await query.edit_message_text(f"Video remains too large ({compressed_size_mb:.2f} MB) even after compression. Cannot send.")

    except Exception as e:
        logger.error(f"Video download/processing error: {e}")
        error_message = f"An unexpected error occurred during the video process: {e}"
        await query.edit_message_text(error_message)
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)




async def handle_play_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("Search cancelled.")

async def show_download_platform_options(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    keyboard = [[
        InlineKeyboardButton("TikTok", callback_data="dl_platform:TikTok"),
        InlineKeyboardButton("Instagram", callback_data="dl_platform:Instagram")
    ], [
        InlineKeyboardButton("Facebook", callback_data="dl_platform:Facebook"),
        InlineKeyboardButton("YouTube", callback_data="dl_platform:YouTube")
    ], [
        # --- NEW ROW ---
        InlineKeyboardButton("Pinterest", callback_data="dl_platform:Pinterest")
    ]]
    await update.message.reply_text("Choose a platform:", reply_markup=InlineKeyboardMarkup(keyboard))

async def handle_platform_selection(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    platform = query.data.split(":")[1]
    context.user_data['state'] = 'awaiting_download_url'
    context.user_data['platform'] = platform
    await query.edit_message_text(f"Send me the {platform} URL.")

# --- TIKTOK DOWNLOAD WITH MULTIPLE API FALLBACKS ---
async def download_tiktok_image_post(update: Update, context: ContextTypes.DEFAULT_TYPE, url: str) -> None:
    """
    Downloads TikTok content using multiple fallback APIs.
    Tries: 1) tikwm API 2) ttdownloader API 3) musicaldown API 4) yt-dlp
    """
    feedback = await update.message.reply_text("[Processing] TikTok content...")
    
    # Try API Method 1: tikwm.com
    result = await try_tikwm_api(url, feedback)
    if result:
        await send_tiktok_result(update, context, result, feedback)
        return
    
    # Try API Method 2: ttdownloader.com
    await feedback.edit_text("[Trying] Alternative API method 2...")
    result = await try_ttdownloader_api(url, feedback)
    if result:
        await send_tiktok_result(update, context, result, feedback)
        return
    
    # Try API Method 3: musicaldown.com
    await feedback.edit_text("[Trying] Alternative API method 3...")
    result = await try_musicaldown_api(url, feedback)
    if result:
        await send_tiktok_result(update, context, result, feedback)
        return
    
    # Fallback: Use yt-dlp for direct video extraction
    await feedback.edit_text("[Trying] Direct video extraction...")
    try:
        await download_content_from_url(update, context, url, 'TikTok')
    except Exception as e:
        logger.error(f"All TikTok methods failed: {e}")
        await feedback.edit_text("[Error] TikTok download failed with all methods. The link may be invalid, private, or region-restricted.")

async def try_tikwm_api(url: str, feedback) -> dict:
    """Attempt download using tikwm.com API"""
    try:
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
        
        video_id = None
        if '/video/' in url:
            video_id = url.split('/video/')[1].split('?')[0].split('@')[0]
        elif 'vm.tiktok.com' in url or 'vt.tiktok.com' in url:
            try:
                resp = requests.get(url, allow_redirects=True, timeout=10, headers=headers)
                if '/video/' in resp.url:
                    video_id = resp.url.split('/video/')[1].split('?')[0].split('@')[0]
            except:
                pass
        
        if not video_id or len(video_id) == 0:
            logger.error(f"Could not extract video ID from {url}")
            return None
        
        api_url = 'https://tikwm.com/api/feed/video'
        response = requests.get(api_url, headers=headers, params={'video_id': video_id}, timeout=15)
        response.raise_for_status()
        data = response.json()
        
        if not data:
            logger.error("Empty response from tikwm API")
            return None
        
        media_data = data.get('data', {})
        if not media_data:
            logger.error("No data field in tikwm response")
            return None
        
        # Check for slideshow/carousel with images
        slides = media_data.get('slides', [])
        images = media_data.get('images', [])
        
        if slides and isinstance(slides, list) and len(slides) > 0:
            logger.info(f"Found {len(slides)} slides in tikwm response")
            return {'type': 'slideshow', 'slides': slides[:10]}
        
        if images and isinstance(images, list) and len(images) > 0:
            logger.info(f"Found {len(images)} images in tikwm response")
            return {'type': 'slideshow', 'slides': images[:10]}
        
        # Check for video
        video_url = media_data.get('play') or media_data.get('download_addr') or media_data.get('video_url')
        if video_url and isinstance(video_url, str) and len(video_url) > 0:
            logger.info(f"Found video URL in tikwm response")
            return {'type': 'video', 'url': video_url}
        
        logger.warning(f"No media found in tikwm response. Data: {str(media_data)[:200]}")
        return None
    except Exception as e:
        logger.error(f"tikwm API failed: {e}")
        return None

async def try_ttdownloader_api(url: str, feedback) -> dict:
    """Attempt download using ttdownloader.com API"""
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0',
            'Content-Type': 'application/x-www-form-urlencoded'
        }
        
        api_url = 'https://ttdownloader.com/api/download'
        payload = {'url': url}
        response = requests.post(api_url, data=payload, headers=headers, timeout=15)
        data = response.json()
        
        if data.get('status') != 'success':
            return None
        
        downloads = data.get('data', {}).get('downloads', [])
        if downloads and len(downloads) > 0:
            video_url = downloads[0].get('url')
            if video_url:
                return {'type': 'video', 'url': video_url}
        
        return None
    except Exception as e:
        logger.error(f"ttdownloader API failed: {e}")
        return None

async def try_musicaldown_api(url: str, feedback) -> dict:
    """Attempt download using musicaldown.com API"""
    try:
        headers = {'User-Agent': 'Mozilla/5.0'}
        api_url = 'https://musicaldown.com/api/download'
        payload = {'url': url}
        
        response = requests.post(api_url, json=payload, headers=headers, timeout=15)
        data = response.json()
        
        if data.get('success') or data.get('status') == 'success':
            video_url = data.get('video_url') or data.get('download_url')
            if video_url:
                return {'type': 'video', 'url': video_url}
            
            downloads = data.get('downloads', [])
            if downloads and len(downloads) > 0:
                video_url = downloads[0].get('url')
                if video_url:
                    return {'type': 'video', 'url': video_url}
        
        return None
    except Exception as e:
        logger.error(f"musicaldown API failed: {e}")
        return None

async def send_tiktok_result(update: Update, context: ContextTypes.DEFAULT_TYPE, result: dict, feedback) -> None:
    """Send TikTok result to user with robust error handling"""
    try:
        if not result or not isinstance(result, dict):
            await feedback.edit_text("[Error] Invalid result format.")
            return
        
        result_type = result.get('type')
        
        if result_type == 'slideshow' and result.get('slides'):
            slides = result.get('slides', [])
            if not isinstance(slides, list) or len(slides) == 0:
                await feedback.edit_text("[Error] No valid slides found.")
                return
            
            media = []
            for idx, img in enumerate(slides[:10]):
                try:
                    img_url = None
                    if isinstance(img, dict):
                        img_url = img.get('download_addr') or img.get('url') or img.get('pic')
                    elif isinstance(img, str):
                        img_url = img
                    
                    if img_url and isinstance(img_url, str) and len(img_url) > 0:
                        media.append({
                            "type": "photo",
                            "media": img_url,
                            "caption": "[TikTok] Slideshow" if idx == 0 else None,
                        })
                except Exception as img_err:
                    logger.error(f"Error processing slide {idx}: {img_err}")
                    continue
            
            if media and len(media) > 0:
                try:
                    await context.bot.send_media_group(chat_id=update.effective_chat.id, media=media)
                    await feedback.delete()
                    return
                except Exception as group_err:
                    logger.error(f"Failed to send media group: {group_err}")
                    await feedback.edit_text("[Error] Could not send images. Try downloading as video.")
                    return
            else:
                await feedback.edit_text("[Error] No valid images could be extracted.")
                return
        
        elif result_type == 'video' and result.get('url'):
            video_url = result['url']
            if not isinstance(video_url, str) or len(video_url) == 0:
                await feedback.edit_text("[Error] Invalid video URL.")
                return
            
            await feedback.edit_text("[Downloading] TikTok video...")
            try:
                video_response = requests.get(video_url, timeout=30, headers={'User-Agent': 'Mozilla/5.0'})
                
                if video_response.status_code != 200:
                    logger.error(f"Video download failed with status {video_response.status_code}")
                    await feedback.edit_text(f"[Error] Download failed (status {video_response.status_code}).")
                    return
                
                if len(video_response.content) == 0:
                    await feedback.edit_text("[Error] Downloaded video is empty.")
                    return
                
                await context.bot.send_video(
                    chat_id=update.effective_chat.id,
                    video=video_response.content,
                    caption="[TikTok] Video"
                )
                await feedback.delete()
                return
            except Exception as vid_err:
                logger.error(f"Error downloading video: {vid_err}")
                await feedback.edit_text(f"[Error] Video download failed: {str(vid_err)[:50]}")
                return
        
        await feedback.edit_text("[Error] Unknown result type or missing data.")
    except Exception as e:
        logger.error(f"Error in send_tiktok_result: {e}")
        await feedback.edit_text(f"[Error] Failed to process: {str(e)[:80]}")

# --- END TIKTOK DOWNLOAD WITH MULTIPLE API FALLBACKS ---

async def download_content_from_url(update: Update, context: ContextTypes.DEFAULT_TYPE, url: str, platform: str) -> None:
    feedback = await update.message.reply_text(f"Starting download from {platform}...")
    temp_dir = os.path.join(DOWNLOAD_DIR, str(uuid.uuid4()))
    os.makedirs(temp_dir)
    try:
        # --- 4K/HD AND YOUTUBE DOWNLOAD FIX APPLIED HERE ---
        ydl_opts = {
            'outtmpl': os.path.join(temp_dir, '%(title)s.%(ext)s'),
            'noplaylist': True,
            'quiet': True,
            'ignoreerrors': True,
            **YTDL_COMMON_OPTIONS,
            # Force the best quality by prioritizing 4K, then 2K, then best video/audio combination
            'format': VIDEO_FORMAT_SELECTOR,
            'merge_output_format': 'mp4' 
        }
        # --- END FIX ---
        
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])
            
        downloaded_files = [
            os.path.join(temp_dir, name) for name in os.listdir(temp_dir)
            if os.path.isfile(os.path.join(temp_dir, name))
            and not name.endswith((".part", ".ytdl"))
        ]
        if not downloaded_files:
            raise RuntimeError("yt-dlp produced no output file; YouTube may require cookies or a supported account")
        file_path = None
        for candidate in downloaded_files:
            if await has_audio_stream(candidate):
                file_path = candidate
                break
        if not file_path:
            raise RuntimeError("yt-dlp produced a video without an audio stream; ffmpeg may be missing")
        await feedback.edit_text("Uploading to Telegram...")
        with open(file_path, 'rb') as f:
            await context.bot.send_video(chat_id=update.effective_chat.id, video=f)
        await feedback.delete()
    except Exception as e:
        logger.error(f"Download error for {url}: {e}")
        await feedback.edit_text("Download failed. The link may be private or invalid.")
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)

# --- NEW: Router function for downloads ---
async def handle_media_download(update: Update, context: ContextTypes.DEFAULT_TYPE, url: str) -> None:
    """Routes the download request to the correct function based on the platform."""
    platform = context.user_data.pop('platform', 'unknown')
    
    if platform == 'TikTok':
        # Use the special handler for TikTok photo dumps
        await download_tiktok_image_post(update, context, url=url)
    elif platform == 'Pinterest':
        # Pinterest has special handling for images/videos
        await download_pinterest_content(update, context, url=url)
    elif platform in ['Instagram', 'Facebook', 'YouTube']:
        # Use the general yt_dlp handler for supported sites
        await download_content_from_url(update, context, url=url, platform=platform)
    else:
        # Fallback for an unknown or unhandled platform
        await update.message.reply_text(f"Download from '{platform}' is not supported or an error occurred.")
# --- END Router function ---

# --- PINTEREST DOWNLOAD WITH MULTIPLE API FALLBACKS ---
async def download_pinterest_content(update: Update, context: ContextTypes.DEFAULT_TYPE, url: str) -> None:
    """
    Downloads Pinterest content using multiple fallback methods.
    Tries: 1) yt-dlp 2) direct Pinterest API 3) pinterestdownloader 4) manual scraping
    """
    feedback = await update.message.reply_text("[Processing] Pinterest content...")
    
    # Try Method 1: yt-dlp with Pinterest options
    await feedback.edit_text("[Trying] Method 1 - yt-dlp...")
    result = await try_pinterest_ytdlp(url)
    if result:
        await send_pinterest_result(update, context, result, feedback)
        return
    
    # Try Method 2: Direct Pinterest API
    await feedback.edit_text("[Trying] Method 2 - Direct API...")
    result = await try_pinterest_direct_api(url)
    if result:
        await send_pinterest_result(update, context, result, feedback)
        return
    
    # Try Method 3: pinterestdownloader.com
    await feedback.edit_text("[Trying] Method 3 - Alternative service...")
    result = await try_pinterest_downloader_service(url)
    if result:
        await send_pinterest_result(update, context, result, feedback)
        return
    
    # Try Method 4: Requests-based scraping
    await feedback.edit_text("[Trying] Method 4 - Manual extraction...")
    result = await try_pinterest_manual_scrape(url)
    if result:
        await send_pinterest_result(update, context, result, feedback)
        return
    
    await feedback.edit_text("[Error] Pinterest download failed. All methods exhausted. The pin may be private, deleted, or region-restricted.")

async def try_pinterest_ytdlp(url: str) -> dict:
    """Try downloading Pinterest content using yt-dlp"""
    try:
        temp_dir = os.path.join(DOWNLOAD_DIR, str(uuid.uuid4()))
        os.makedirs(temp_dir)
        
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
        ydl_opts = {
            'outtmpl': os.path.join(temp_dir, '%(title)s.%(ext)s'),
            'quiet': True,
            'no_warnings': True,
            'socket_timeout': 30,
            'http_headers': headers,
            'format': 'best',
            'allow_unplayable_formats': True,
        }
        
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
        
        files = os.listdir(temp_dir)
        if files:
            file_path = os.path.join(temp_dir, files[0])
            if os.path.getsize(file_path) > 0:
                return {'type': 'file', 'path': file_path, 'temp_dir': temp_dir}
        
        shutil.rmtree(temp_dir, ignore_errors=True)
        return None
    except Exception as e:
        logger.error(f"yt-dlp Pinterest failed: {e}")
        return None

async def try_pinterest_direct_api(url: str) -> dict:
    """Try downloading Pinterest content via direct API"""
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)',
            'Accept': 'application/json',
        }
        
        # Extract pin ID from URL
        if '/pin/' in url:
            pin_id = url.split('/pin/')[1].split('/')[0]
        else:
            return None
        
        api_url = f'https://api.pinterest.com/v3/pins/{pin_id}'
        response = requests.get(api_url, headers=headers, timeout=15)
        
        if response.status_code != 200:
            return None
        
        data = response.json()
        
        # Look for image or video
        image_url = data.get('image', {}).get('original', {}).get('url')
        if image_url:
            return {'type': 'url', 'url': image_url, 'media_type': 'image'}
        
        video = data.get('videos', {}).get('video', {}).get('videoList', {}).get('V_HLSVARIANT', {}).get('url')
        if video:
            return {'type': 'url', 'url': video, 'media_type': 'video'}
        
        return None
    except Exception as e:
        logger.error(f"Direct Pinterest API failed: {e}")
        return None

async def try_pinterest_downloader_service(url: str) -> dict:
    """Try downloading Pinterest content via pinterestdownloader service"""
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0',
            'Content-Type': 'application/x-www-form-urlencoded'
        }
        
        api_url = 'https://pinterestdownloader.com/download.php'
        payload = {'url': url}
        
        response = requests.post(api_url, data=payload, headers=headers, timeout=15)
        
        if 'download' in response.text.lower():
            # Parse HTML for download links
            soup = BeautifulSoup(response.content, 'html.parser')
            links = soup.find_all('a', href=True)
            
            for link in links:
                href = link['href']
                if 'pcdn' in href or 'pinimg' in href or href.endswith(('.mp4', '.jpg', '.png')):
                    return {'type': 'url', 'url': href, 'media_type': 'unknown'}
        
        return None
    except Exception as e:
        logger.error(f"Pinterest downloader service failed: {e}")
        return None

async def try_pinterest_manual_scrape(url: str) -> dict:
    """Try scraping Pinterest page for media"""
    try:
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
        response = requests.get(url, headers=headers, timeout=15)
        
        soup = BeautifulSoup(response.content, 'html.parser')
        
        # Look for images
        img_tags = soup.find_all('img', src=True)
        for img in img_tags:
            src = img.get('src', '')
            if 'pinimg' in src or 'pcdn' in src:
                return {'type': 'url', 'url': src, 'media_type': 'image'}
        
        # Look for videos
        video_tags = soup.find_all('video', src=True)
        for video in video_tags:
            src = video.get('src', '')
            if src:
                return {'type': 'url', 'url': src, 'media_type': 'video'}
        
        # Look for source tags
        source_tags = soup.find_all('source', src=True)
        for source in source_tags:
            src = source.get('src', '')
            if src and ('pcdn' in src or 'pinimg' in src):
                return {'type': 'url', 'url': src, 'media_type': 'video'}
        
        return None
    except Exception as e:
        logger.error(f"Pinterest manual scrape failed: {e}")
        return None

async def send_pinterest_result(update: Update, context: ContextTypes.DEFAULT_TYPE, result: dict, feedback) -> None:
    """Send Pinterest result to user"""
    try:
        if result['type'] == 'file':
            file_path = result['path']
            temp_dir = result['temp_dir']
            file_ext = os.path.splitext(file_path)[1].lower()
            file_size_mb = os.path.getsize(file_path) / (1024 * 1024)
            
            try:
                if file_ext in ['.jpg', '.jpeg', '.png', '.webp', '.gif']:
                    await feedback.edit_text(f"[Uploading] Pinterest image ({file_size_mb:.2f} MB)...")
                    with open(file_path, 'rb') as f:
                        await context.bot.send_photo(chat_id=update.effective_chat.id, photo=f, caption="[Pinterest] Image")
                elif file_ext in ['.mp4', '.webm', '.mov', '.avi']:
                    if file_size_mb > 50:
                        await feedback.edit_text(f"[Error] Video too large ({file_size_mb:.2f} MB). Telegram limit is 50 MB.")
                        shutil.rmtree(temp_dir, ignore_errors=True)
                        return
                    await feedback.edit_text(f"[Uploading] Pinterest video ({file_size_mb:.2f} MB)...")
                    with open(file_path, 'rb') as f:
                        await context.bot.send_video(chat_id=update.effective_chat.id, video=f, caption="[Pinterest] Video")
                else:
                    await feedback.edit_text(f"[Uploading] Pinterest file ({file_size_mb:.2f} MB)...")
                    with open(file_path, 'rb') as f:
                        await context.bot.send_document(chat_id=update.effective_chat.id, document=f, filename=os.path.basename(file_path))
                
                await feedback.delete()
            except Exception as send_err:
                logger.error(f"Error sending Pinterest file: {send_err}")
                try:
                    await feedback.edit_text(f"[Error] Failed to send: {str(send_err)[:50]}")
                except:
                    pass
            finally:
                shutil.rmtree(temp_dir, ignore_errors=True)
        
        elif result['type'] == 'url':
            media_url = result['url']
            await feedback.edit_text("[Downloading] from URL...")
            
            media_response = requests.get(media_url, timeout=30)
            if media_response.status_code == 200:
                media_type = result.get('media_type', 'unknown')
                
                if media_type == 'image' or media_url.lower().endswith(('.jpg', '.jpeg', '.png', '.webp', '.gif')):
                    await context.bot.send_photo(chat_id=update.effective_chat.id, photo=media_response.content, caption="[Pinterest] Image")
                elif media_type == 'video' or media_url.lower().endswith(('.mp4', '.webm', '.mov')):
                    await context.bot.send_video(chat_id=update.effective_chat.id, video=media_response.content, caption="[Pinterest] Video")
                else:
                    await context.bot.send_document(chat_id=update.effective_chat.id, document=media_response.content, filename="pinterest_media")
                
                await feedback.delete()
                return
        
        await feedback.edit_text("[Error] Could not process Pinterest content.")
    except Exception as e:
        logger.error(f"Error sending Pinterest result: {e}")
        await feedback.edit_text(f"[Error] Failed to send: {str(e)[:50]}")

# --- END PINTEREST DOWNLOAD WITH MULTIPLE API FALLBACKS ---

async def get_joke(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        response = requests.get("https://v2.jokeapi.dev/joke/Any?blacklistFlags=nsfw,racist,sexist&type=twopart", timeout=5).json()
        if not response.get('error'):
            await update.message.reply_text(f"{response['setup']}\n\n...{response['delivery']}")
    except Exception:
        await update.message.reply_text("Sorry, couldn't get a joke.")

async def get_riddle(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        response = requests.get("https://riddles-api.vercel.app/random", timeout=7).json()
        await update.message.reply_text(f"Riddle:\n*_{response['riddle']}_*", parse_mode=ParseMode.MARKDOWN)
        await asyncio.sleep(8)
        await update.message.reply_text(f"**Answer:**\n||{escape_markdown(response['answer'], version=2)}||", parse_mode=ParseMode.MARKDOWN_V2)
    except Exception as e:
        logger.error(f"Riddle API error: {e}")
        await update.message.reply_text("Sorry, the riddle service is unavailable.")

async def get_crypto_prices(update: Update, context: ContextTypes.DEFAULT_TYPE, crypto_ids: str) -> None:
    ids = ','.join(s.strip().lower() for s in crypto_ids.split(','))
    try:
        prices = requests.get(f"https://api.coingecko.com/api/v3/simple/price?ids={ids}&vs_currencies=usd,ngn", timeout=5).json()
        if not prices:
            await update.message.reply_text("Couldn't find prices.")
            return
        message = "**Latest Crypto Prices:**\n" + "\n".join(f"• **{c.title()}**: ${d.get('usd', 0):,.2f} / ₦{d.get('ngn', 0):,.2f}" for c, d in prices.items())
        await update.message.reply_text(message, parse_mode=ParseMode.MARKDOWN)
    except Exception:
        await update.message.reply_text("Sorry, couldn't fetch crypto prices.")

async def get_weather(update: Update, context: ContextTypes.DEFAULT_TYPE, city: str) -> None:
    if not OPENWEATHER_API_KEY:
        await update.message.reply_text("Weather service not configured.")
        return
    try:
        data = requests.get(f"http://api.openweathermap.org/data/2.5/weather?q={city}&appid={OPENWEATHER_API_KEY}&units=metric", timeout=5).json()
        if data.get("cod") != 200:
            await update.message.reply_text(f"Sorry, couldn't find city '{city}'.")
            return
        message = f"**Weather in {data['name']}**: {data['weather'][0]['description'].title()} at {data['main']['temp']}°C"
        await update.message.reply_text(message, parse_mode=ParseMode.MARKDOWN)
    except Exception:
        await update.message.reply_text("Sorry, couldn't fetch the weather.")

async def translate_command(update: Update, context: ContextTypes.DEFAULT_TYPE, text_to_translate: str = None) -> None:
    if not gemini_model:
        await update.message.reply_text("Translate service is not configured.")
        return
    text = text_to_translate or " ".join(context.args)
    if not text or " " not in text.strip():
        await update.message.reply_text("Format: `<language> <text>`")
        return
    target_lang, text_to_trans = text.strip().split(None, 1)
    prompt = f"Translate the following text to {target_lang}: {text_to_trans}"
    response = await asyncio.to_thread(gemini_model.generate_content, prompt)
    await update.message.reply_text(response.text)

async def ping_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    webhook_url = context.job.data.get("webhook_url")
    if not webhook_url:
        logger.warning("Webhook URL not found for ping job.")
        return
    try:
        await asyncio.to_thread(requests.get, webhook_url, timeout=20)
        logger.info(f"Auto-ping successful.")
    except Exception as e:
        logger.warning(f"Auto-ping failed: {e}")

async def novel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await prompt_for_input(update, context, 'awaiting_novel_title', "What novel would you like to search for?", "Pressed 'Download Novel'")
        return
    query = " ".join(context.args)
    await search_for_novel(update.message, context, query)

async def search_for_novel(message, context: ContextTypes.DEFAULT_TYPE, query: str) -> None:
    """Search for PDFs using multiple sources"""
    feedback = await message.reply_text(f"[Searching] for '{query}'...")
    
    # Try Method 1: pdfroom.com
    results = await try_pdf_source_1_pdfroom(query)
    if results:
        await display_pdf_results(feedback, results, "pdfroom")
        context.user_data['pdf_results'] = results
        return
    
    # Try Method 2: z-library (zlibrary)
    await feedback.edit_text("[Trying] Alternative PDF source 2...")
    results = await try_pdf_source_2_archive(query)
    if results:
        await display_pdf_results(feedback, results, "archive.org")
        context.user_data['pdf_results'] = results
        return
    
    # Try Method 3: googledrive/scribd public PDFs
    await feedback.edit_text("[Trying] Alternative PDF source 3...")
    results = await try_pdf_source_3_public(query)
    if results:
        await display_pdf_results(feedback, results, "public")
        context.user_data['pdf_results'] = results
        return
    
    await feedback.edit_text("[Error] No PDFs found in any source. Try a different search term.")

async def try_pdf_source_1_pdfroom(query: str) -> list:
    """Search PDFs on pdfroom.com"""
    try:
        search_url = f"https://www.pdfroom.com/search?q={requests.utils.quote(query)}"
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
        response = requests.get(search_url, headers=headers, timeout=20)
        response.raise_for_status()
        soup = BeautifulSoup(response.content, 'html.parser')
        results = soup.find_all('div', class_='book-card', limit=5)
        
        if not results or len(results) == 0:
            return None
        
        books = []
        for idx, book in enumerate(results):
            try:
                title_elem = book.find('h5')
                if not title_elem:
                    continue
                title = title_elem.get_text(strip=True)
                
                link_elem = book.find('a')
                if not link_elem or 'href' not in link_elem.attrs:
                    continue
                
                book_page_url = f"https://www.pdfroom.com{link_elem['href']}"
                books.append({'title': title, 'url': book_page_url, 'source': 'pdfroom'})
            except:
                continue
        
        return books if len(books) > 0 else None
    except Exception as e:
        logger.error(f"PDF source 1 (pdfroom) failed: {e}")
        return None

async def try_pdf_source_2_archive(query: str) -> list:
    """Search PDFs on archive.org"""
    try:
        search_url = f"https://archive.org/advancedsearch.php?q={requests.utils.quote(query)}+AND+mediatype:texts&fl=identifier,title,downloads&output=json&rows=5"
        headers = {'User-Agent': 'Mozilla/5.0'}
        response = requests.get(search_url, headers=headers, timeout=20)
        response.raise_for_status()
        data = response.json()
        
        docs = data.get('response', {}).get('docs', [])
        if not docs or len(docs) == 0:
            return None
        
        books = []
        for doc in docs[:5]:
            try:
                identifier = doc.get('identifier')
                title = doc.get('title', identifier)
                if identifier:
                    book_url = f"https://archive.org/details/{identifier}"
                    books.append({'title': title, 'url': book_url, 'source': 'archive.org', 'identifier': identifier})
            except:
                continue
        
        return books if len(books) > 0 else None
    except Exception as e:
        logger.error(f"PDF source 2 (archive.org) failed: {e}")
        return None

async def try_pdf_source_3_public(query: str) -> list:
    """Search public PDF links"""
    try:
        # Search Google Scholar for PDFs
        search_url = f"https://scholar.google.com/scholar?q={requests.utils.quote(query)}&scisbd=1"
        headers = {'User-Agent': 'Mozilla/5.0'}
        response = requests.get(search_url, headers=headers, timeout=20)
        
        soup = BeautifulSoup(response.content, 'html.parser')
        results = soup.find_all('div', class_='gs_ri', limit=5)
        
        if not results or len(results) == 0:
            return None
        
        books = []
        for result in results[:5]:
            try:
                # Look for PDF links
                pdf_links = result.find_all('a', href=True)
                for link in pdf_links:
                    href = link.get('href', '')
                    if href.endswith('.pdf') or 'pdf' in href.lower():
                        title = result.find('h3')
                        if title:
                            books.append({
                                'title': title.get_text(strip=True),
                                'url': href,
                                'source': 'scholar'
                            })
                        break
            except:
                continue
        
        return books if len(books) > 0 else None
    except Exception as e:
        logger.error(f"PDF source 3 (scholar) failed: {e}")
        return None

async def display_pdf_results(feedback, results: list, source: str) -> None:
    """Display PDF search results as buttons"""
    try:
        if not results or len(results) == 0:
            await feedback.edit_text("[Error] No results to display.")
            return
        
        keyboard = []
        for idx, book in enumerate(results[:5]):
            try:
                title = book.get('title', 'Unknown')
                button_text = (title[:45] + '...') if len(title) > 45 else title
                # Store URL in context instead of callback data to avoid 64-byte limit
                callback_id = f"pdf_{idx}"
                keyboard.append([InlineKeyboardButton(button_text, callback_data=f"novel_dl:{callback_id}:{source}")])
            except:
                continue
        
        if keyboard:
            # Results are stored in context.user_data by the caller.
            await feedback.edit_text(f"[Found] Results from {source}. Choose one to download:", 
                                    reply_markup=InlineKeyboardMarkup(keyboard))
        else:
            await feedback.edit_text("[Error] Could not parse results.")
    except Exception as e:
        logger.error(f"Error displaying PDF results: {e}")
        try:
            await feedback.edit_text("[Error] Failed to display results.")
        except:
            pass

async def handle_novel_download(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle PDF download with multiple fallback methods"""
    query = update.callback_query
    await query.answer()
    
    # Parse callback data
    data_parts = query.data.split(":", 2)
    if len(data_parts) < 2:
        await query.edit_message_text("[Error] Invalid download request.")
        return
    
    callback_id = data_parts[1]
    source = data_parts[2] if len(data_parts) > 2 else "unknown"
    
    # Retrieve PDF URL from user data or stored results
    pdf_results = context.user_data.get('pdf_results', [])
    pdf_url = None
    
    if pdf_results:
        try:
            idx = int(callback_id.replace('pdf_', ''))
            if idx < len(pdf_results):
                pdf_url = pdf_results[idx].get('url')
        except:
            pass
    
    if not pdf_url:
        await query.edit_message_text("[Error] PDF URL not found. Please search again.")
        return
    
    feedback = await query.edit_message_text("[Starting] Downloading PDF...")
    
    try:
        # Method 1: Direct download
        result = await try_pdf_download_direct(pdf_url, feedback)
        if result:
            await send_pdf_to_user(update, context, result, feedback)
            return
        
        # Method 2: Archive.org viewer extraction
        if source == 'archive.org' and '/details/' in pdf_url:
            await feedback.edit_text("[Trying] Alternate download method...")
            result = await try_pdf_download_archive(pdf_url, feedback)
            if result:
                await send_pdf_to_user(update, context, result, feedback)
                return
        
        # Method 3: PDFRoom specific
        if source == 'pdfroom':
            await feedback.edit_text("[Trying] PDFRoom extraction...")
            result = await try_pdf_download_pdfroom(pdf_url, feedback)
            if result:
                await send_pdf_to_user(update, context, result, feedback)
                return
        
        await feedback.edit_text("[Error] All download methods failed. The PDF may be restricted or removed.")
    except Exception as e:
        logger.error(f"PDF download error: {e}")
        try:
            await feedback.edit_text(f"[Error] Download failed: {str(e)[:80]}")
        except:
            pass

async def try_pdf_download_direct(url: str, feedback) -> dict:
    """Attempt direct PDF download"""
    try:
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
        response = requests.get(url, headers=headers, timeout=120)
        response.raise_for_status()
        
        if len(response.content) == 0:
            return None
        
        file_size_mb = len(response.content) / (1024 * 1024)
        if file_size_mb > 50:
            return None
        
        filename = url.split('/')[-1]
        if not filename.endswith('.pdf'):
            filename = "document.pdf"
        
        return {'content': response.content, 'filename': filename, 'size_mb': file_size_mb}
    except Exception as e:
        logger.error(f"Direct PDF download failed: {e}")
        return None

async def try_pdf_download_archive(url: str, feedback) -> dict:
    """Attempt PDF extraction from archive.org"""
    try:
        # Extract identifier from archive.org URL
        if '/details/' in url:
            identifier = url.split('/details/')[1].split('/')[0]
            pdf_url = f"https://archive.org/download/{identifier}/{identifier}.pdf"
            return await try_pdf_download_direct(pdf_url, feedback)
        return None
    except Exception as e:
        logger.error(f"Archive.org extraction failed: {e}")
        return None

async def try_pdf_download_pdfroom(url: str, feedback) -> dict:
    """Attempt PDF download from pdfroom with page parsing"""
    try:
        headers = {'User-Agent': 'Mozilla/5.0'}
        response = requests.get(url, headers=headers, timeout=20)
        soup = BeautifulSoup(response.content, 'html.parser')
        
        # Find download button
        download_btn = soup.find('a', id='download-button')
        if not download_btn or 'href' not in download_btn.attrs:
            return None
        
        download_link = f"https://www.pdfroom.com{download_btn['href']}"
        return await try_pdf_download_direct(download_link, feedback)
    except Exception as e:
        logger.error(f"PDFRoom extraction failed: {e}")
        return None

async def send_pdf_to_user(update: Update, context: ContextTypes.DEFAULT_TYPE, result: dict, feedback) -> None:
    """Send PDF to user"""
    try:
        await feedback.edit_text(f"[Uploading] PDF ({result['size_mb']:.2f} MB)...")
        await context.bot.send_document(
            chat_id=update.effective_chat.id,
            document=result['content'],
            filename=result['filename'],
            caption="[PDF] Document"
        )
        try:
            await feedback.delete()
        except:
            pass
    except Exception as e:
        logger.error(f"Error sending PDF: {e}")
        try:
            await feedback.edit_text(f"[Error] Failed to send PDF: {str(e)[:50]}")
        except:
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text=f"[Error] Failed to send PDF: {str(e)[:50]}"
            )

async def prompt_for_input(update: Update, context: ContextTypes.DEFAULT_TYPE, state: str, message: str, event: str) -> None:
    await save_user_to_db(update, context, event_type=event)
    context.user_data['state'] = state
    await update.message.reply_text(message)

async def record_user_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.text: return
    state = context.user_data.get('state')
    if not state:
        await save_user_to_db(update, context)
        return
    text = update.message.text
    if state == 'continuous_chat':
        await gemini_command(update, context, prompt_text=text)
        return
    popped_state = context.user_data.pop('state')
    if popped_state == 'awaiting_email_address':
        context.user_data['email_to'] = text
        context.user_data['state'] = 'awaiting_email_subject'
        await update.message.reply_text("Step 2 of 3: What's the subject?")
    elif popped_state == 'awaiting_email_subject':
        context.user_data['email_subject'] = text
        context.user_data['state'] = 'awaiting_email_body'
        await update.message.reply_text("Step 3 of 3: What's the message?")
    elif popped_state == 'awaiting_email_body':
        to, subject = context.user_data.pop('email_to'), context.user_data.pop('email_subject')
        feedback = await update.message.reply_text(f"Sending email to {to}...")
        msg = EmailMessage()
        msg.set_content(text)
        msg['Subject'], msg['From'], msg['To'] = subject, GMAIL_ADDRESS, to
        try:
            with smtplib.SMTP_SSL('smtp.gmail.com', 465) as s:
                s.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
                s.send_message(msg)
            await feedback.edit_text("Email sent successfully!")

            # --- Recurring Email Feature ---
            job_id = str(uuid.uuid4())
            context.chat_data[job_id] = {'to': to, 'subject': subject, 'body': text}

            keyboard = [
                [
                    InlineKeyboardButton("5 mins", callback_data=f"resend_interval:300:{job_id}"),
                    InlineKeyboardButton("10 mins", callback_data=f"resend_interval:600:{job_id}"),
                    InlineKeyboardButton("15 mins", callback_data=f"resend_interval:900:{job_id}"),
                ],
                [InlineKeyboardButton("No, thanks", callback_data=f"resend_cancel:{job_id}")]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await update.message.reply_text(
                "Would you like to resend this email periodically?",
                reply_markup=reply_markup
            )

        except Exception as e:
            logger.error(f"Failed to send email: {e}")
            await feedback.edit_text("Failed to send email.")
    else:
        state_handlers = {
            'awaiting_gemini_prompt': lambda: gemini_command(update, context, prompt_text=text),
            'awaiting_create_prompt': lambda: create_image_command(update, context, prompt=text),
            'awaiting_novel_title': lambda: search_for_novel(update.message, context, query=text),
            'awaiting_song_name': lambda: search_and_play_song(update, context, song_name=text),
            'awaiting_city': lambda: get_weather(update, context, city=text),
            'awaiting_crypto_symbols': lambda: get_crypto_prices(update, context, crypto_ids=text),
            'awaiting_summary_url': lambda: summarize_url(update, context, url=text),
            'awaiting_translation_text': lambda: translate_command(update, context, text_to_translate=text),
            
            # --- UPDATED: Clean router for media downloads (Pinterest support added) ---
            'awaiting_download_url': lambda: handle_media_download(update, context, url=text),
            # --- END UPDATE ---

            'awaiting_screenshot_url': lambda: screenshot_command(update, context, url=text),
            'awaiting_movie_title': lambda: movie_command(update, context, title=text),
            'awaiting_tts_text': lambda: tts_command(update, context, text_to_speak=text),
            'awaiting_ytsearch_query': lambda: youtube_command(update, context, query=text),
            'awaiting_suggestion': lambda: handle_suggestion(update, context, suggestion=text),
            'awaiting_db_table_name': lambda: view_db_table(update, context, table_name=text),
            'awaiting_tiktok_query': lambda: ask_for_tiktok_count(update, context, query=text),
        }
        if popped_state in state_handlers:
            await state_handlers[popped_state]()

async def handle_suggestion(update: Update, context: ContextTypes.DEFAULT_TYPE, suggestion: str):
    user = update.effective_user
    admin_message = f"📩 Suggestion from {user.first_name} (`{user.id}`):\n\n{suggestion}"
    await context.bot.send_message(chat_id=ADMIN_ID, text=admin_message, parse_mode=ParseMode.MARKDOWN)
    await update.message.reply_text("Thank you! Your suggestion has been sent.")

def main() -> None:
    application = Application.builder().token(BOT_TOKEN).rate_limiter(AIORateLimiter()).job_queue(JobQueue()).build()
    
    cmd_handlers = [
        CommandHandler("start", start), CommandHandler("help", help_command),
        CommandHandler("gemini", gemini_command), CommandHandler("create", create_image_command),
        CommandHandler("upscale", upscale_image_command), CommandHandler("animate", animate_command),
        CommandHandler("summarize_file", summarize_file_command), CommandHandler("readtext", read_text_from_image_command),
        CommandHandler("play", play_command), CommandHandler("mp4", convert_video_to_audio),
        CommandHandler("4k", four_k_upscale_command), # NEW 4K VIDEO COMMAND
        CommandHandler("novel", novel_command), CommandHandler("riddle", get_riddle), 
        CommandHandler("gmail", gmail_command), CommandHandler("screenshot", screenshot_command),
        CommandHandler("movie", movie_command), CommandHandler("tts", tts_command),
        CommandHandler("tiktoksearch", tiktok_search_command), CommandHandler("ytsearch", youtube_command),
        CommandHandler("db", db_command)
    ]
    
    menu_button_texts = {
        "AI Tools": show_ai_tools_menu, "Media Tools": show_media_tools_menu,
        "Utilities": show_utilities_menu, "Help": help_command,
        "Back to Main Menu": start, "End Chat": end_chat,
        "Chat with AI": start_ai_chat, "Tell a Joke": get_joke, "Ask a Riddle": get_riddle,
        "Send Suggestion": lambda u,c: prompt_for_input(u,c,'awaiting_suggestion', "Please type your suggestion...", "Pressed 'Suggestion'"),
        "Create Image": lambda u,c: prompt_for_input(u,c,'awaiting_create_prompt', "Describe the image...", "Pressed 'Create Image'"),
        "Read Text from Image": lambda u,c: u.message.reply_text("Reply to an image with /readtext."),
        "Text to Speech": lambda u,c: prompt_for_input(u,c,'awaiting_tts_text', "What text to speak?", "Pressed 'TTS'"),
        "Upscale Image": lambda u,c: u.message.reply_text("Reply to an image with /upscale."),
        "Animate Image": lambda u,c: u.message.reply_text("Reply to an image with /animate."),
        "Summarize File": lambda u,c: u.message.reply_text("Reply to an image or PDF with /summarize_file."),
        "Summarize Link": lambda u,c: prompt_for_input(u,c,'awaiting_summary_url', "Send the article link.", "Pressed 'Summarize Link'"),
        "Play Music / Video": lambda u,c: prompt_for_input(u,c,'awaiting_song_name', "What song or video?", "Pressed 'Play'"),
        "Download PDF": lambda u,c: prompt_for_input(u,c,'awaiting_novel_title', "What PDF/novel to search for?", "Pressed 'Download PDF'"),
        "Download Media": show_download_platform_options,
        "Search Movie": lambda u,c: prompt_for_input(u,c,'awaiting_movie_title', "What movie?", "Pressed 'Search Movie'"),
        "Search TikTok": lambda u,c: prompt_for_input(u,c,'awaiting_tiktok_query', "Search TikTok for?", "Pressed 'Search TikTok'"),
        "Youtube": lambda u,c: prompt_for_input(u,c,'awaiting_ytsearch_query', "Search YouTube for?", "Pressed 'Youtube'"),
        "Weather": lambda u,c: prompt_for_input(u,c,'awaiting_city', "Enter a city name.", "Pressed 'Weather'"),
        "Crypto Prices": lambda u,c: prompt_for_input(u,c,'awaiting_crypto_symbols', "Enter coin IDs (e.g., bitcoin,ethereum).", "Pressed 'Crypto'"),
        "Translate Text": lambda u,c: prompt_for_input(u,c,'awaiting_translation_text', "Format: <language> <text>", "Pressed 'Translate'"),
        "Convert Video to Audio": lambda u,c: u.message.reply_text("Reply to a video with /mp4."),
        "Take Screenshot": lambda u,c: prompt_for_input(u,c,'awaiting_screenshot_url', "Enter the website URL.", "Pressed 'Take Screenshot'"),
        "Send Email (Admin)": gmail_command,
    }
    
    msg_handlers = [MessageHandler(filters.TEXT & filters.Regex(f"^{re.escape(p)}$"), f) for p, f in menu_button_texts.items()]
    
    callback_handlers = [
        CallbackQueryHandler(handle_play_confirmation, pattern="^play_confirm:"),
        CallbackQueryHandler(handle_play_cancel, pattern="^play_cancel"),
        CallbackQueryHandler(handle_audio_download, pattern="^dl_audio:"),
        CallbackQueryHandler(handle_video_download, pattern="^dl_video:"),
        CallbackQueryHandler(handle_platform_selection, pattern="^dl_platform:"),
        CallbackQueryHandler(handle_tiktok_count_selection, pattern="^tiktok_count:"),
        CallbackQueryHandler(handle_novel_download, pattern="^novel_dl:"),
        # --- HANDLERS for recurring email ---
        CallbackQueryHandler(handle_resend_interval_selection, pattern="^resend_interval:"),
        CallbackQueryHandler(handle_resend_stop_day_selection, pattern="^resend_stop:"),
        CallbackQueryHandler(handle_resend_cancel, pattern="^resend_cancel:"),
    ]

    application.add_handlers(cmd_handlers)
    application.add_handlers(msg_handlers)
    application.add_handlers(callback_handlers)
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, record_user_message))
    
    port = int(os.environ.get("PORT", "10000"))
    webhook_base_url = (
        os.environ.get("WEBHOOK_URL")
        or os.environ.get("RENDER_EXTERNAL_URL")
    )
    legacy_render_name = os.environ.get("RENDER_APP_NAME")
    if not webhook_base_url and legacy_render_name:
        webhook_base_url = f"https://{legacy_render_name}.onrender.com"

    # Heroku dynos must use an explicit public HTTPS URL for Telegram webhooks.
    # Do not silently switch a Heroku deployment back to polling mode.
    if os.environ.get("DYNO") and not webhook_base_url:
        logger.critical(
            "WEBHOOK_URL is required for Heroku webhook mode. "
            "Set it to the app's public HTTPS URL and redeploy."
        )
        sys.exit(1)

    if webhook_base_url:
        webhook_url = webhook_base_url.rstrip("/") + f"/{BOT_TOKEN}"
        logger.info("Running in webhook mode on port %s.", port)
        application.run_webhook(
            listen="0.0.0.0",
            port=port,
            url_path=BOT_TOKEN,
            webhook_url=webhook_url,
        )
    else:
        logger.info("Running in polling mode.")
        application.run_polling()

if __name__ == "__main__":
    main()
