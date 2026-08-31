#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import sys
import time
import asyncio
import logging
import sqlite3
import datetime
import hashlib
import urllib.parse
import json
import re
import shutil
import tempfile
import html
from pathlib import Path
from urllib.parse import urlparse

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import yt_dlp
import requests
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    LabeledPrice,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    PreCheckoutQueryHandler,
    filters,
)
from telegram.constants import ParseMode, ChatAction

# ------------------------------------------------------------
# Configuration from environment variables
# ------------------------------------------------------------
TOKEN = os.getenv("BOT_TOKEN")
if not TOKEN:
    raise RuntimeError("BOT_TOKEN environment variable is not set")

OWNER_ID = int(os.getenv("OWNER_ID", "8854936887"))
BOT_USERNAME = os.getenv("BOT_USERNAME", "All_MusicDownloader_Bot")
PREMIUM_CHANNEL_ID = os.getenv("PREMIUM_CHANNEL_ID", "")

DOWNLOAD_LIMIT = 5
COOLDOWN_HOURS = 24
POINTS_PER_REFERRAL = 10
POINTS_PER_DOWNLOAD = 10
POINTS_PER_LYRICS = 5

# Premium payment configuration (Telegram Stars)
PREMIUM_DAYS = 30
PREMIUM_STARS = 100  # price in Telegram Stars
PREMIUM_TITLE = "Premium Access"
PREMIUM_DESCRIPTION = f"Unlock unlimited downloads, lyrics and more for {PREMIUM_DAYS} days."

MAX_VIDEO_SIZE_MB = 100
MAX_VIDEO_SIZE_BYTES = MAX_VIDEO_SIZE_MB * 1024 * 1024

# ------------------------------------------------------------
# Paths and directories
# ------------------------------------------------------------
BASE_DIR = Path(__file__).parent.resolve()
DOWNLOADS_DIR = BASE_DIR / "downloads"
COOKIES_DIR = BASE_DIR / "cookies"
THUMBNAILS_DIR = BASE_DIR / "thumbnails"

for d in [DOWNLOADS_DIR, COOKIES_DIR, THUMBNAILS_DIR]:
    d.mkdir(parents=True, exist_ok=True)

COOKIES_FILE = COOKIES_DIR / "cookies.txt"

# ------------------------------------------------------------
# Logging
# ------------------------------------------------------------
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ------------------------------------------------------------
# Small caps mapping (for decorative text)
# ------------------------------------------------------------
SMALL_CAPS_MAP = str.maketrans(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ",
    "ᴀʙᴄᴅᴇꜰɢʜɪᴊᴋʟᴍɴᴏᴘǫʀꜱᴛᴜᴠᴡxʏᴢᴀʙᴄᴅᴇꜰɢʜɪᴊᴋʟᴍɴᴏᴘǫʀꜱᴛᴜᴠᴡxʏᴢ"
)

def sc(text: str) -> str:
    if not text:
        return ""
    return text.translate(SMALL_CAPS_MAP)

# Helper to safely escape HTML for Telegram messages
def e(text: str) -> str:
    """Escape HTML special characters."""
    return html.escape(str(text), quote=False)

# ------------------------------------------------------------
# Database setup
# ------------------------------------------------------------
db = sqlite3.connect("bot.db", check_same_thread=False)
db.row_factory = sqlite3.Row  # enable column access by name
cursor = db.cursor()

cursor.execute("""
CREATE TABLE IF NOT EXISTS users(
    id INTEGER PRIMARY KEY,
    points INTEGER DEFAULT 0,
    premium_expire TEXT,
    downloads INTEGER DEFAULT 0,
    last_reset TEXT,
    referrer INTEGER,
    total_downloads INTEGER DEFAULT 0,
    username TEXT,
    first_name TEXT,
    join_date TEXT
)
""")
db.commit()

search_cache = {}
video_dl_semaphore = asyncio.Semaphore(2)

# ------------------------------------------------------------
# Database helper functions (using column names)
# ------------------------------------------------------------
def get_user(user_id):
    cursor.execute("SELECT * FROM users WHERE id=?", (user_id,))
    user = cursor.fetchone()
    if not user:
        now = datetime.datetime.now().isoformat()
        cursor.execute(
            "INSERT INTO users(id,last_reset,join_date) VALUES(?,?,?)",
            (user_id, now, now)
        )
        db.commit()
        cursor.execute("SELECT * FROM users WHERE id=?", (user_id,))
        user = cursor.fetchone()
    return user

def update_user_profile(user_id, username, first_name):
    cursor.execute(
        "UPDATE users SET username=?, first_name=? WHERE id=?",
        (username, first_name, user_id)
    )
    db.commit()

def reset_downloads(user):
    if not user or not user["last_reset"]:
        return False
    try:
        last_reset = datetime.datetime.fromisoformat(user["last_reset"])
        now = datetime.datetime.now()
        if (now - last_reset).total_seconds() > COOLDOWN_HOURS * 3600:
            cursor.execute(
                "UPDATE users SET downloads=0,last_reset=? WHERE id=?",
                (now.isoformat(), user["id"])
            )
            db.commit()
            return True
    except:
        pass
    return False

def is_premium(user):
    if not user or not user["premium_expire"]:
        return False
    try:
        expire = datetime.datetime.fromisoformat(user["premium_expire"])
        return expire > datetime.datetime.now()
    except:
        return False

def get_premium_expire(user):
    if user and user["premium_expire"]:
        try:
            return datetime.datetime.fromisoformat(user["premium_expire"])
        except:
            pass
    return None

def increment_downloads(user_id):
    cursor.execute(
        "UPDATE users SET downloads=downloads+1, total_downloads=total_downloads+1 WHERE id=?",
        (user_id,)
    )
    db.commit()

def add_points(user_id, points):
    cursor.execute("UPDATE users SET points=points+? WHERE id=?", (points, user_id))
    db.commit()

def deduct_points(user_id, points):
    cursor.execute("UPDATE users SET points=MAX(0, points-?) WHERE id=?", (points, user_id))
    db.commit()

def get_user_points(user_id):
    user = get_user(user_id)
    return user["points"] if user else 0

def can_download(user_id):
    user = get_user(user_id)
    reset_downloads(user)
    if is_premium(user):
        return True, 0
    remaining = DOWNLOAD_LIMIT - (user["downloads"] if user else 0)
    if remaining > 0:
        return True, remaining
    points = get_user_points(user_id)
    if points >= POINTS_PER_DOWNLOAD:
        return True, remaining
    return False, remaining

def can_use_lyrics(user_id):
    user = get_user(user_id)
    if is_premium(user):
        return True
    points = get_user_points(user_id)
    return points >= POINTS_PER_LYRICS

def use_lyrics(user_id):
    deduct_points(user_id, POINTS_PER_LYRICS)

def use_points_download(user_id):
    deduct_points(user_id, POINTS_PER_DOWNLOAD)

def set_referral(user_id, referrer_id):
    cursor.execute("UPDATE users SET referrer=? WHERE id=?", (referrer_id, user_id))
    db.commit()

def get_referral_count(user_id):
    cursor.execute("SELECT COUNT(*) FROM users WHERE referrer=?", (user_id,))
    row = cursor.fetchone()
    return row[0] if row else 0

# ------------------------------------------------------------
# Notification functions (using HTML parse_mode)
# ------------------------------------------------------------
async def notify_premium_channel(context, user_id, days, granted_by):
    if not PREMIUM_CHANNEL_ID:
        return
    try:
        user = get_user(user_id)
        username = user["username"] if user and user["username"] else "NONE"
        first_name = user["first_name"] if user and user["first_name"] else "UNKNOWN"
        expire = datetime.datetime.now() + datetime.timedelta(days=days)
        expire_str = expire.strftime("%Y-%m-%d %H:%M:%S")
        text = (
            f"⭐ <b>{e(sc('NEW PREMIUM USER'))}</b>\n\n"
            f"👤 <b>{e(sc('NAME'))}</b>: {e(first_name)}\n"
            f"📛 <b>{e(sc('USERNAME'))}</b>: @{e(username)}\n"
            f"🆔 <b>{e(sc('USER ID'))}</b>: {user_id}\n"
            f"📅 <b>{e(sc('DAYS'))}</b>: {days}\n"
            f"⏰ <b>{e(sc('EXPIRE'))}</b>: {expire_str}\n"
            f"👤 <b>{e(sc('GRANTED BY'))}</b>: {e(granted_by)}\n\n"
            f"🚀 <b>{e(sc('START BOT'))}</b> | 💎 <b>{e(sc('GET PREMIUM'))}</b>"
        )
        await context.bot.send_message(PREMIUM_CHANNEL_ID, text, parse_mode=ParseMode.HTML)
    except Exception as e:
        logger.error(f"Premium channel notification failed: {e}")

async def notify_new_user_channel(context, user_id, referrer_id=None):
    if not PREMIUM_CHANNEL_ID:
        return
    try:
        user = get_user(user_id)
        username = user["username"] if user and user["username"] else "NONE"
        first_name = user["first_name"] if user and user["first_name"] else "UNKNOWN"
        join_date = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        if referrer_id:
            ref_user = get_user(referrer_id)
            ref_name = ref_user["first_name"] if ref_user and ref_user["first_name"] else str(referrer_id)
            ref_by = f"{ref_name} ({referrer_id})"
        else:
            ref_by = sc("DIRECT JOIN")
        text = (
            f"📥 <b>{e(sc('NEW USER JOINED'))}</b>\n\n"
            f"👤 <b>{e(sc('NAME'))}</b>: {e(first_name)}\n"
            f"📛 <b>{e(sc('USERNAME'))}</b>: @{e(username)}\n"
            f"🆔 <b>{e(sc('USER ID'))}</b>: {user_id}\n"
            f"📅 <b>{e(sc('DATE'))}</b>: {join_date}\n"
            f"👥 <b>{e(sc('REFERRED BY'))}</b>: {e(ref_by)}\n\n"
            f"🤖 <b>{e(sc('START BOT'))}</b>"
        )
        await context.bot.send_message(PREMIUM_CHANNEL_ID, text, parse_mode=ParseMode.HTML)
    except Exception as e:
        logger.error(f"New user channel notification failed: {e}")

# ------------------------------------------------------------
# Main menu keyboard
# ------------------------------------------------------------
def get_main_menu_keyboard(user_id: int) -> ReplyKeyboardMarkup:
    user = get_user(user_id)
    points = user["points"] if user else 0
    downloads = user["downloads"] if user else 0
    premium = is_premium(user)
    premium_text = "💎 PREMIUM" if premium else "⭐ UPGRADE"
    keyboard = [
        ["🎵 SEARCH MUSIC", "🔥 TRENDING"],
        [f"📊 ACCOUNT ({points} pts)", f"⬇️ DOWNLOADS {downloads}/{DOWNLOAD_LIMIT}"],
        ["🔗 REFERRAL", "🤖 OTHER BOTS"],
        ["📹 VIDEO DOWNLOADER", premium_text],
        ["❓ HELP", "📞 CONTACT"]
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

# ------------------------------------------------------------
# YouTube search and trending
# ------------------------------------------------------------
def search_music(query, max_results=50):
    try:
        opts = {
            "quiet": True,
            "no_warnings": True,
            "extract_flat": True,
            "playlistend": max_results,
            "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "extractor_args": {"youtube": {"player_client": ["android"]}}
        }
        with yt_dlp.YoutubeDL(opts) as ydl:
            result = ydl.extract_info(f"ytsearch{max_results}:{query}", download=False)
            entries = result.get("entries", []) if result else []
            valid = []
            for entry in entries:
                if entry and entry.get("id") and entry.get("title"):
                    duration = int(entry.get("duration", 0)) if entry.get("duration") else 0
                    valid.append({
                        "id": entry["id"],
                        "title": entry["title"],
                        "duration": duration,
                        "uploader": entry.get("uploader", "Unknown"),
                        "view_count": entry.get("view_count", 0),
                        "thumbnail": entry.get("thumbnail", ""),
                        "url": f"https://youtube.com/watch?v={entry['id']}"
                    })
            return valid
    except Exception as e:
        logger.error(f"Search error: {e}")
        return []

def fetch_trending_songs(max_results=10):
    queries = [
        "trending music 2026",
        "top hits 2026",
        "viral songs 2026"
    ]
    all_results = []
    for q in queries:
        res = search_music(q, max_results=max_results)
        all_results.extend(res)
        if len(all_results) >= max_results:
            break
    seen = set()
    unique = []
    for r in all_results:
        if r["id"] not in seen:
            seen.add(r["id"])
            unique.append(r)
    return unique[:max_results]

# ------------------------------------------------------------
# Audio download: external APIs then yt-dlp fallback
# ------------------------------------------------------------
def get_audio_download_url(youtube_url):
    encoded_url = urllib.parse.quote_plus(youtube_url)
    # Environment variable for Alya API key if needed
    alya_key = os.getenv("ALYA_API_KEY", "")
    api_methods = [
        {
            "name": "EliteProTech",
            "func": lambda: requests.get(f"https://eliteprotech-apis.zone.id/ytdown?url={encoded_url}&format=mp3", timeout=15)
        },
        {
            "name": "DavidCyril",
            "func": lambda: requests.get(f"https://apis.davidcyril.name.ng/youtube/mp3?url={encoded_url}", timeout=15)
        },
        {
            "name": "Alya",
            "func": lambda: requests.get(f"https://api.alyachan.pro/api/ytmp3?url={encoded_url}&apikey={alya_key}", timeout=15) if alya_key else None
        },
        {
            "name": "Okatsu",
            "func": lambda: requests.get(f"https://okatsu-rolezapiiz.vercel.app/downloader/ytmp3?url={encoded_url}", timeout=15)
        },
        {
            "name": "Vreden",
            "func": lambda: requests.get(f"https://api.vreden.my.id/api/ytmp3?url={encoded_url}", timeout=15)
        },
        {
            "name": "PrexzyVilla",
            "func": lambda: requests.get(f"https://apis.prexzyvilla.site/download/ytmp3?url={encoded_url}", timeout=15)
        }
    ]
    for method in api_methods:
        try:
            func = method["func"]
            if func is None:
                continue
            r = func()
            if r.status_code != 200:
                continue
            data = r.json()
            download_url = None
            title = ""
            if method["name"] == "EliteProTech" and data.get("success"):
                download_url = data.get("downloadURL")
                title = data.get("title", "")
            elif method["name"] == "DavidCyril" and data.get("status"):
                download_url = data.get("result", {}).get("download_url")
                title = data.get("result", {}).get("title", "")
            elif method["name"] == "Alya" and data.get("status"):
                download_url = data.get("data", {}).get("url")
                title = data.get("data", {}).get("title", "")
            elif method["name"] == "Okatsu" and data.get("dl"):
                download_url = data["dl"]
                title = data.get("title", "")
            elif method["name"] == "Vreden" and data.get("status"):
                download_url = data.get("result", {}).get("download", {}).get("url")
                title = data.get("result", {}).get("metadata", {}).get("title", "")
            elif method["name"] == "PrexzyVilla" and data.get("success"):
                download_url = data.get("result", {}).get("download_url")
                title = data.get("result", {}).get("title", "")
            if download_url and download_url.startswith(("http://", "https://")):
                return download_url, title
        except:
            continue
    # yt-dlp fallback
    logger.info("All external APIs failed. Trying yt-dlp...")
    try:
        opts = {
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "format": "bestaudio/best",
            "extractaudio": True,
            "audioformat": "mp3",
            "outtmpl": str(DOWNLOADS_DIR / "%(id)s.%(ext)s"),
            "postprocessors": [{
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }]
        }
        if COOKIES_FILE.exists():
            opts["cookies"] = str(COOKIES_FILE)
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(youtube_url, download=True)
            base = DOWNLOADS_DIR / f"{info['id']}.mp3"
            if base.exists():
                return f"file://{base.absolute()}", info.get("title", "")
    except Exception as e:
        logger.error(f"yt-dlp fallback failed: {e}")
    raise Exception("All downloader APIs and yt-dlp fallback failed")

async def download_audio_async(video_id, title, youtube_url):
    loop = asyncio.get_event_loop()
    file_id = hashlib.md5(f"{video_id}{time.time()}".encode()).hexdigest()[:12]
    out_path = DOWNLOADS_DIR / f"{file_id}.mp3"

    def _download():
        try:
            download_url, final_title = get_audio_download_url(youtube_url)
            if download_url.startswith("file://"):
                src = Path(download_url[7:])
                if src.exists():
                    shutil.copy2(src, out_path)
                    src.unlink()
                    return out_path, final_title or title
            r = requests.get(
                download_url,
                timeout=120,
                stream=True,
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                    "Accept": "*/*",
                    "Accept-Encoding": "gzip, deflate, br",
                    "Connection": "keep-alive",
                    "Range": "bytes=0-"
                }
            )
            if r.status_code not in (200, 206):
                raise Exception(f"Download URL returned status {r.status_code}")
            with open(out_path, "wb") as f:
                for chunk in r.iter_content(chunk_size=1024 * 64):
                    if chunk:
                        f.write(chunk)
            if not out_path.exists() or out_path.stat().st_size < 50 * 1024:
                raise Exception("Downloaded file too small or corrupt")
            return out_path, final_title or title
        except Exception as e:
            logger.error(f"Download error: {e}")
            return None, None

    try:
        result = await asyncio.wait_for(loop.run_in_executor(None, _download), timeout=180.0)
        if result[0] and result[0].exists():
            return result
        return None, None
    except asyncio.TimeoutError:
        return None, None
    except:
        return None, None

# ------------------------------------------------------------
# Lyrics fetching
# ------------------------------------------------------------
def fetch_lyrics(title, artist=""):
    title = title.strip()
    artist = artist.strip()
    if not artist and " - " in title:
        parts = title.split(" - ", 1)
        artist = parts[0].strip()
        title = parts[1].strip()
    # API 1: lyrics.ovh
    try:
        if artist and title:
            url = f"https://api.lyrics.ovh/v1/{requests.utils.quote(artist)}/{requests.utils.quote(title)}"
            r = requests.get(url, timeout=8)
            if r.status_code == 200:
                data = r.json()
                if data.get("lyrics"):
                    return data["lyrics"]
    except:
        pass
    # API 2: lrclib
    try:
        search_term = f"{artist} {title}".strip()
        url = f"https://lrclib.net/api/search?q={requests.utils.quote(search_term)}"
        r = requests.get(url, timeout=8)
        if r.status_code == 200:
            data = r.json()
            if data and len(data) > 0:
                lyrics = data[0].get("plainLyrics")
                if lyrics:
                    return lyrics
    except:
        pass
    # API 3: textyl
    try:
        search_term = f"{artist} {title}".strip()
        url = f"https://api.textyl.co/api/lyrics?q={requests.utils.quote(search_term)}"
        r = requests.get(url, timeout=8)
        if r.status_code == 200:
            data = r.json()
            if data and data.get("lyrics"):
                return data["lyrics"]
    except:
        pass
    # API 4: musixmatch (requires key, placeholder)
    try:
        musixmatch_key = os.getenv("MUSIXMATCH_API_KEY", "")
        if musixmatch_key:
            url = f"https://api.musixmatch.com/ws/1.1/matcher.lyrics.get?q_track={requests.utils.quote(title)}&q_artist={requests.utils.quote(artist)}&apikey={musixmatch_key}"
            r = requests.get(url, timeout=8)
            if r.status_code == 200:
                data = r.json()
                lyrics = data.get("message", {}).get("body", {}).get("lyrics", {}).get("lyrics_body")
                if lyrics:
                    return lyrics
    except:
        pass
    return None

# ------------------------------------------------------------
# Social video downloader
# ------------------------------------------------------------
SUPPORTED_VIDEO_DOMAINS = {
    "tiktok.com", "vm.tiktok.com", "vt.tiktok.com",
    "instagram.com", "www.instagram.com",
    "facebook.com", "www.facebook.com", "fb.watch",
    "youtube.com", "www.youtube.com", "youtu.be",
    "x.com", "www.x.com", "twitter.com", "www.twitter.com",
    "reddit.com", "www.reddit.com", "redd.it"
}

URL_PATTERN = re.compile(r'https?://[^\s<>"]+', re.IGNORECASE)

def extract_url(text: str) -> str | None:
    if not text:
        return None
    match = URL_PATTERN.search(text)
    if not match:
        return None
    url = match.group(0).rstrip(".,!?)]}")
    return url

def is_video_url(url: str) -> bool:
    try:
        hostname = urlparse(url).hostname
        if not hostname:
            return False
        hostname = hostname.lower().rstrip(".")
        return any(hostname == d or hostname.endswith("." + d) for d in SUPPORTED_VIDEO_DOMAINS)
    except:
        return False

def get_platform_name(url: str) -> str:
    hostname = (urlparse(url).hostname or "").lower()
    if "tiktok" in hostname:
        return "TikTok"
    if "instagram" in hostname:
        return "Instagram"
    if "facebook" in hostname or hostname == "fb.watch":
        return "Facebook"
    if "youtube" in hostname or hostname == "youtu.be":
        return "YouTube"
    if "twitter" in hostname or "x.com" in hostname:
        return "X/Twitter"
    if "reddit" in hostname:
        return "Reddit"
    return "Social Media"

def find_video_file(directory: Path) -> Path | None:
    files = [p for p in directory.rglob("*") if p.is_file() and p.suffix.lower() in {".mp4", ".mkv", ".webm", ".mov", ".avi", ".m4v"}]
    if not files:
        return None
    return max(files, key=lambda p: p.stat().st_size)

def check_ffmpeg() -> bool:
    return shutil.which("ffmpeg") is not None

def download_social_video(url: str, output_dir: str) -> tuple[Path | None, dict]:
    out_path = Path(output_dir)
    template = str(out_path / "%(title).80s-%(id)s.%(ext)s")
    # Check if ffmpeg exists for merging
    if not check_ffmpeg():
        # Fallback to single file format that doesn't require merge
        ydl_opts = {
            "format": "best[ext=mp4]/best",
            "outtmpl": template,
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
            "retries": 2,
            "fragment_retries": 2,
            "socket_timeout": 30,
        }
    else:
        ydl_opts = {
            "format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
            "outtmpl": template,
            "merge_output_format": "mp4",
            "noplaylist": True,
            "writethumbnail": False,
            "writesubtitles": False,
            "quiet": True,
            "no_warnings": True,
            "retries": 2,
            "fragment_retries": 2,
            "socket_timeout": 30,
        }
    info = {}
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
    file_path = find_video_file(out_path)
    return file_path, info

# ------------------------------------------------------------
# Telegram command and message handlers
# ------------------------------------------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_id = user.id
    ref = None
    if context.args:
        try:
            ref = int(context.args[0])
        except:
            pass
    db_user = get_user(user_id)
    is_new = db_user["join_date"] is None if db_user else True
    if is_new or (db_user and not db_user["first_name"]):
        update_user_profile(user_id, user.username, user.first_name)
    if db_user and db_user["referrer"] is None and ref and ref != user_id:
        set_referral(user_id, ref)
        add_points(ref, POINTS_PER_REFERRAL)
        ref_count = get_referral_count(ref)
        try:
            await context.bot.send_message(
                ref,
                f"🎯 <b>{e(sc('FRESH POINTS DROP!'))}</b>\n\n"
                f"👤 <b>{e(sc('NEW REFERRAL'))}</b>: {e(user.first_name or 'USER')}\n"
                f"📛 <b>{e(sc('USERNAME'))}</b>: @{e(user.username or 'NONE')}\n"
                f"🆔 <b>{e(sc('USER ID'))}</b>: {user_id}\n"
                f"📅 <b>{e(sc('DATE'))}</b>: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
                f"📊 <b>{e(sc('TOTAL REFERRALS'))}</b>: {ref_count}\n\n"
                f"✅ {e(sc('YOU EARNED'))} +{POINTS_PER_REFERRAL} {e(sc('POINTS!'))}\n"
                f"💡 {e(sc('10 POINTS = 1 DOWNLOAD + 2 LYRICS SEARCHES'))}\n\n"
                f"🚀 {e(sc('KEEP SHARING YOUR LINK!'))}",
                parse_mode=ParseMode.HTML
            )
        except Exception as e:
            logger.error(f"Referral notify failed: {e}")
    await notify_new_user_channel(context, user_id, ref)
    text = (
        f"🎵 <b>{e(sc('WELCOME TO ADVANCED MUSIC BOT'))}</b>\n\n"
        f"👋 {e(sc('HI'))}, {e(user.first_name)}!\n\n"
        f"{e(sc('I CAN HELP YOU FIND AND DOWNLOAD MUSIC FROM YOUTUBE AND VIDEOS FROM SOCIAL MEDIA'))}\n\n"
        f"✨ <b>{e(sc('FEATURES'))}</b>:\n"
        f"• 🎵 {e(sc('SEARCH ANY SONG'))}\n"
        f"• ⬇️ {e(sc('DOWNLOAD MP3 AUDIO'))}\n"
        f"• 📜 {e(sc('GET SONG LYRICS'))}\n"
        f"• 🔗 {e(sc('REFER FRIENDS & EARN POINTS'))}\n"
        f"• 📹 {e(sc('DOWNLOAD SOCIAL MEDIA VIDEOS'))}\n"
        f"• 💎 {e(sc('PREMIUM FOR UNLIMITED DOWNLOADS'))}\n\n"
        f"{e(sc('USE THE BUTTONS BELOW TO GET STARTED'))}!"
    )
    await update.message.reply_text(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=get_main_menu_keyboard(user_id)
    )

async def handle_menu_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = update.message.text
    if text == "🎵 SEARCH MUSIC":
        await update.message.reply_text(
            f"🎵 <b>{e(sc('SEARCH MUSIC'))}</b>\n\n"
            f"{e(sc('SEND ME A SONG NAME OR ARTIST TO SEARCH'))}\n"
            f"{e(sc('EXAMPLE'))}: \"Calm Down Rema\"",
            parse_mode=ParseMode.HTML,
            reply_markup=get_main_menu_keyboard(user_id)
        )
    elif text == "🔥 TRENDING":
        await show_trending(update, context)
    elif text.startswith("📊 ACCOUNT"):
        await show_account(update, context)
    elif text.startswith("⬇️ DOWNLOADS"):
        user = get_user(user_id)
        reset_downloads(user)
        remaining = DOWNLOAD_LIMIT - (user["downloads"] if user else 0)
        premium = is_premium(user)
        pts = get_user_points(user_id)
        await update.message.reply_text(
            f"📊 <b>{e(sc('DOWNLOAD USAGE'))}</b>\n\n"
            f"{e(sc('TODAY'))}: {user['downloads'] if user else 0} / {DOWNLOAD_LIMIT}\n"
            f"{e(sc('REMAINING'))}: {remaining if not premium else 'UNLIMITED'}\n"
            f"{e(sc('POINTS BALANCE'))}: {pts}\n"
            f"{e(sc('PREMIUM'))}: {'✅ ' + sc('ACTIVE') if premium else '❌ ' + sc('NOT ACTIVE')}\n\n"
            f"💡 {e(sc('10 POINTS = 1 DOWNLOAD + 2 LYRICS SEARCHES'))}",
            parse_mode=ParseMode.HTML,
            reply_markup=get_main_menu_keyboard(user_id)
        )
    elif text == "🔗 REFERRAL":
        await show_referral(update, context)
    elif text == "🤖 OTHER BOTS":
        await show_other_bots(update, context)
    elif text == "📹 VIDEO DOWNLOADER":
        await update.message.reply_text(
            f"📹 <b>{e(sc('SOCIAL VIDEO DOWNLOADER'))}</b>\n\n"
            f"{e(sc('SEND ME A LINK FROM'))}:\n"
            f"• TikTok\n• Instagram\n• Facebook\n• YouTube\n• X/Twitter\n• Reddit\n\n"
            f"{e(sc('I WILL DOWNLOAD AND SEND THE VIDEO TO YOU'))}\n"
            f"{e(sc('JUST PASTE THE LINK DIRECTLY'))}",
            parse_mode=ParseMode.HTML,
            reply_markup=get_main_menu_keyboard(user_id)
        )
    elif text in ("⭐ UPGRADE", "💎 PREMIUM"):
        await show_premium(update, context)
    elif text == "❓ HELP":
        await show_help(update, context)
    elif text == "📞 CONTACT":
        await show_contact(update, context)
    else:
        url = extract_url(text)
        if url and is_video_url(url):
            await handle_video_download(update, context, url)
        else:
            await song_search(update, context)

async def show_account(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user = get_user(user_id)
    reset_downloads(user)
    points = user["points"] if user else 0
    downloads = user["downloads"] if user else 0
    total_dl = user["total_downloads"] if user else 0
    premium_status = "💎 ACTIVE" if is_premium(user) else "❌ INACTIVE"
    ref_count = get_referral_count(user_id)
    text = (
        f"👤 <b>{e(sc('YOUR ACCOUNT'))}</b>\n\n"
        f"💰 {e(sc('POINTS'))}: {points}\n"
        f"⬇️ {e(sc('DOWNLOADS TODAY'))}: {downloads}/{DOWNLOAD_LIMIT}\n"
        f"📊 {e(sc('TOTAL DOWNLOADS'))}: {total_dl}\n"
        f"💎 {e(sc('PREMIUM'))}: {premium_status}\n"
        f"🔗 {e(sc('REFERRALS'))}: {ref_count}\n\n"
        f"{e(sc('INVITE FRIENDS AND EARN 10 POINTS EACH'))}!"
    )
    await update.message.reply_text(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=get_main_menu_keyboard(user_id)
    )

async def show_referral(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    link = f"https://t.me/{BOT_USERNAME}?start={user_id}"
    ref_count = get_referral_count(user_id)
    pts = get_user_points(user_id)
    text = (
        f"🔗 <b>{e(sc('YOUR REFERRAL LINK'))}</b>\n\n"
        f"{e(sc('SHARE THIS LINK WITH YOUR FRIENDS'))}:\n"
        f"{link}\n\n"
        f"✨ <b>{e(sc('HOW IT WORKS'))}</b>:\n"
        f"• {e(sc('EACH FRIEND WHO JOINS GIVES YOU'))} +{POINTS_PER_REFERRAL} {e(sc('POINTS'))}\n"
        f"• {e(sc('10 POINTS = 1 SONG DOWNLOAD + 2 LYRICS SEARCHES'))}\n"
        f"• {e(sc('USE POINTS WHEN YOU HIT YOUR DAILY LIMIT'))}\n\n"
        f"📊 <b>{e(sc('YOUR STATS'))}</b>:\n"
        f"👥 {e(sc('REFERRALS'))}: {ref_count}\n"
        f"💰 {e(sc('POINTS'))}: {pts}\n\n"
        f"{e(sc('TAP AND HOLD TO COPY THE LINK'))}"
    )
    share_text = urllib.parse.quote(f"🎵 Get music for free — Download songs and videos")
    message = update.effective_message
    await message.reply_text(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("📤 SHARE LINK", url=f"https://t.me/share/url?url={link}&text={share_text}")]
        ])
    )

async def show_other_bots(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Using HTML parse mode and escaping all dynamic parts
    text = (
        f"🤖 <b>{e(sc('OUR BOT NETWORK'))}</b>\n\n"
        f"🟢 <b>{e(sc('ONLINE'))}</b>\n\n"
        f"• @DarkHacker_BanBot — GhostShell MD\n"
        f"  {e(sc('A MULTIPURPOSE WHATSAPP BOT WITH GROUP MANAGEMENT, ANTILINK, ANTISPAM, WELCOME/GOODBYE MESSAGES, TAG-ALL, STICKER CREATION, AND FULL WHATSAPP AUTOMATION'))}\n\n"
        f"• @Image_ConverterTo_linkBot\n"
        f"  {e(sc('CONVERT IMAGES, VIDEOS, FILES, AND VOICE NOTES TO DIRECT DOWNLOAD LINKS INSTANTLY'))}\n\n"
        f"🔴 <b>{e(sc('MORE BOTS COMING SOON'))}</b>\n\n"
        f"@WormGPT_Prover_Bot {e(sc('DESCRIPTION WILL BE ADDED BY OWNER'))}\n"
        f"@Whatsapp2_Ban_bot {e(sc('DESCRIPTION WILL BE ADDED BY OWNER'))}\n\n"
        f"💬 {e(sc('STAY TUNED FOR MORE POWERFUL TOOLS'))}!"
    )
    await update.message.reply_text(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=get_main_menu_keyboard(update.effective_user.id)
    )

async def show_premium(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user = get_user(user_id)
    message = update.effective_message
    if is_premium(user):
        expire = get_premium_expire(user)
        expire_str = expire.strftime("%Y-%m-%d %H:%M") if expire else "N/A"
        text = (
            f"💎 <b>{e(sc('PREMIUM ACTIVE'))}</b>\n\n"
            f"📅 {e(sc('EXPIRES'))}: {expire_str}\n"
            f"⬇️ {e(sc('DAILY LIMIT'))}: {e(sc('UNLIMITED'))}\n"
            f"🎵 {e(sc('LYRICS'))}: {e(sc('UNLIMITED'))}\n"
            f"📹 {e(sc('VIDEO DOWNLOADS'))}: {e(sc('UNLIMITED'))}\n\n"
            f"{e(sc('THANK YOU FOR SUPPORTING THE BOT'))} 🙏"
        )
        await message.reply_text(
            text,
            parse_mode=ParseMode.HTML,
            reply_markup=get_main_menu_keyboard(user_id)
        )
        return
    keyboard = [
        [InlineKeyboardButton("⭐ PAY WITH TELEGRAM STARS", callback_data="pay_stars")],
        [InlineKeyboardButton("📞 CONTACT OWNER", url="https://t.me/Mr_Unique_Hacker002")],
        [InlineKeyboardButton("🔙 BACK", callback_data="back_to_menu")]
    ]
    text = (
        f"💎 <b>{e(sc('UPGRADE TO PREMIUM'))}</b>\n\n"
        f"✨ <b>{e(sc('PREMIUM BENEFITS'))}</b>:\n"
        f"• ⬇️ {e(sc('UNLIMITED DOWNLOADS PER DAY'))}\n"
        f"• 🚀 {e(sc('PRIORITY DOWNLOAD SPEED'))}\n"
        f"• 🎵 {e(sc('HD AUDIO QUALITY'))}\n"
        f"• 📜 {e(sc('UNLIMITED LYRICS ACCESS'))}\n"
        f"• 📹 {e(sc('UNLIMITED VIDEO DOWNLOADS'))}\n"
        f"• 🚫 {e(sc('NO COOLDOWN PERIODS'))}\n"
        f"• ⚡ {e(sc('NO ADS OR INTERRUPTIONS'))}\n\n"
        f"💰 <b>{e(sc('PAYMENT METHODS'))}</b>:\n"
        f"• ⭐ {e(sc('TELEGRAM STARS — FAST & SECURE'))}\n"
        f"• 💬 {e(sc('CONTACT'))} @Mr_Unique_Hacker002 {e(sc('FOR OTHER METHODS'))}\n\n"
        f"🎯 <b>{e(sc('HOW TO PAY WITH STARS'))}</b>:\n"
        f"1️⃣ {e(sc('CLICK THE BUTTON BELOW'))}\n"
        f"2️⃣ {e(sc('COMPLETE THE PAYMENT IN TELEGRAM'))}\n"
        f"3️⃣ {e(sc('YOUR PREMIUM WILL BE ACTIVATED INSTANTLY'))}\n\n"
        f"🔥 {e(sc('DONT MISS OUT — UPGRADE NOW'))}!"
    )
    await message.reply_text(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def show_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        f"❓ <b>{e(sc('HELP GUIDE'))}</b>\n\n"
        f"<b>{e(sc('HOW TO USE'))}</b>:\n"
        f"1️⃣ {e(sc('SEND A SONG NAME OR ARTIST'))}\n"
        f"2️⃣ {e(sc('CLICK ON A RESULT'))}\n"
        f"3️⃣ {e(sc('CHOOSE DOWNLOAD AUDIO OR LYRICS'))}\n\n"
        f"<b>{e(sc('COMMANDS'))}</b>:\n"
        f"/start — {e(sc('START THE BOT'))}\n"
        f"/account — {e(sc('VIEW YOUR PROFILE'))}\n"
        f"/trending — {e(sc('TRENDING SONGS'))}\n"
        f"/help — {e(sc('THIS HELP MESSAGE'))}\n\n"
        f"<b>{e(sc('POINTS SYSTEM'))}</b>:\n"
        f"• 1 {e(sc('REFERRAL'))} = 10 {e(sc('POINTS'))}\n"
        f"• 10 {e(sc('POINTS'))} = 1 {e(sc('SONG DOWNLOAD'))} + 2 {e(sc('LYRICS SEARCHES'))}\n"
        f"• {e(sc('USE POINTS WHEN YOU HIT YOUR DAILY LIMIT'))}\n\n"
        f"<b>{e(sc('PREMIUM BENEFITS'))}</b>:\n"
        f"• {e(sc('UNLIMITED DOWNLOADS'))}\n"
        f"• {e(sc('UNLIMITED LYRICS'))}\n"
        f"• {e(sc('UNLIMITED VIDEO DOWNLOADS'))}\n"
        f"• {e(sc('NO COOLDOWNS'))}\n\n"
        f"{e(sc('CONTACT'))} @Mr_Unique_Hacker002 {e(sc('FOR PREMIUM'))}"
    )
    await update.message.reply_text(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=get_main_menu_keyboard(update.effective_user.id)
    )

async def show_contact(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("📩 TELEGRAM", url="https://t.me/Mr_Unique_Hacker002")],
        [InlineKeyboardButton("📱 WHATSAPP", url="https://wa.me/2349123578884")]
    ]
    await update.message.reply_text(
        f"📞 <b>{e(sc('CONTACT US'))}</b>\n\n{e(sc('REACH OUT TO US ON'))}:",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def show_trending(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text(
        f"🔥 <b>{e(sc('FETCHING REAL TRENDING SONGS'))}</b>...",
        parse_mode=ParseMode.HTML
    )
    try:
        songs = await asyncio.to_thread(fetch_trending_songs, 10)
    except Exception as e:
        logger.error(f"Trending error: {e}")
        songs = []
    if not songs:
        await msg.edit_text(
            f"❌ <b>{e(sc('COULD NOT FETCH TRENDING SONGS'))}</b>\n\n{e(sc('PLEASE TRY AGAIN LATER'))}",
            parse_mode=ParseMode.HTML
        )
        return
    keyboard = []
    for i, song in enumerate(songs[:10]):
        title = song['title'][:40] + "..." if len(song['title']) > 40 else song['title']
        duration = f" ⏱{song['duration']//60}:{song['duration']%60:02d}" if song.get('duration') else ""
        keyboard.append([InlineKeyboardButton(f"🔥 {title}{duration}", callback_data=f"trend_{song['id']}")])
    keyboard.append([InlineKeyboardButton("🔙 BACK TO MENU", callback_data="back_to_menu")])
    await msg.edit_text(
        f"🔥 <b>{e(sc('TRENDING SONGS RIGHT NOW'))}</b>\n\n{e(sc('CLICK ANY SONG TO DOWNLOAD'))}:",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def song_search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    query = update.message.text.strip()
    if len(query) < 2:
        await update.message.reply_text(
            f"⚠️ {e(sc('PLEASE ENTER AT LEAST 2 CHARACTERS TO SEARCH'))}",
            reply_markup=get_main_menu_keyboard(user_id)
        )
        return
    msg = await update.message.reply_text(
        f"🔎 <b>{e(sc('SEARCHING'))}</b>...",
        parse_mode=ParseMode.HTML
    )
    try:
        results = await asyncio.to_thread(search_music, query)
    except Exception as e:
        logger.error(f"Search error: {e}")
        results = []
    if not results:
        await msg.edit_text(
            f"❌ <b>{e(sc('NO RESULTS FOUND'))}</b>\n\n💡 {e(sc('TRY DIFFERENT KEYWORDS OR ARTIST NAME'))}",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 BACK TO MENU", callback_data="back_to_menu")]])
        )
        return
    chat_id = update.message.chat_id
    search_cache[chat_id] = results
    keyboard = []
    for i, r in enumerate(results[:20]):
        duration = f" ⏱{r['duration']//60}:{r['duration']%60:02d}" if r.get('duration') else ""
        title = r['title'][:45] + "..." if len(r['title']) > 45 else r['title']
        keyboard.append([InlineKeyboardButton(f"🎵 {title}{duration}", callback_data=f"song_{i}")])
    if len(results) > 20:
        keyboard.append([InlineKeyboardButton("➕ MORE RESULTS", callback_data="more_20")])
    keyboard.append([InlineKeyboardButton("🔙 BACK TO MENU", callback_data="back_to_menu")])
    await msg.edit_text(
        f"🎵 <b>{e(sc('RESULTS FOR'))}</b>: {e(query[:50])}\n\n{e(sc('SELECT A SONG'))}:",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def song_info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    chat_id = q.message.chat_id
    # callback data: song_<index>
    try:
        index = int(q.data.split("_", 1)[1])
    except (ValueError, IndexError):
        await q.edit_message_text("⚠️ Invalid song selection.")
        return
    results = search_cache.get(chat_id)
    if not results or index >= len(results):
        await q.edit_message_text("⚠️ Song expired. Please search again.")
        return
    video = results[index]
    duration = f"{video['duration']//60}:{video['duration']%60:02d}" if video.get('duration') else "N/A"
    caption = (
        f"🎵 <b>{e(video['title'][:100])}</b>\n\n"
        f"👤 {e(sc('ARTIST'))}: {e(video.get('uploader', 'Unknown'))}\n"
        f"⏱ {e(sc('DURATION'))}: {duration}\n"
        f"🔗 {e(sc('WATCH ON YOUTUBE'))}\n\n"
        f"{e(sc('CHOOSE AN ACTION'))}:"
    )
    keyboard = [
        [InlineKeyboardButton("⬇️ DOWNLOAD AUDIO", callback_data=f"download_audio_{index}")],
        [InlineKeyboardButton("📜 LYRICS", callback_data=f"lyrics_{index}")],
        [InlineKeyboardButton("🔙 BACK TO RESULTS", callback_data="page_0")]
    ]
    await q.edit_message_text(
        caption,
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def download_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer("⬇️ Starting download...")
    user_id = q.from_user.id
    chat_id = q.message.chat_id
    # callback data: download_audio_<index>
    try:
        index = int(q.data.split("_", 2)[2])
    except (ValueError, IndexError):
        await q.message.reply_text("⚠️ Invalid download request.")
        return
    user = get_user(user_id)
    reset_downloads(user)
    can_dl, remaining = can_download(user_id)
    if not can_dl:
        keyboard = [
            [InlineKeyboardButton("⭐ UPGRADE TO PREMIUM", callback_data="upgrade_now")],
            [InlineKeyboardButton("🔗 EARN POINTS", callback_data="earn_points")]
        ]
        await q.message.reply_text(
            f"⛔ <b>{e(sc('DOWNLOAD LIMIT REACHED'))}</b>\n\n"
            f"{e(sc('YOU HAVE USED ALL YOUR FREE DOWNLOADS FOR TODAY'))}\n\n"
            f"💡 <b>{e(sc('OPTIONS'))}</b>:\n"
            f"• 💎 {e(sc('UPGRADE TO PREMIUM FOR UNLIMITED DOWNLOADS'))}\n"
            f"• 🔗 {e(sc('REFER FRIENDS TO EARN POINTS'))}\n"
            f"• ⭐ {e(sc('USE POINTS TO DOWNLOAD (10 PTS = 1 DL)'))}\n\n"
            f"{e(sc('CHOOSE AN OPTION BELOW'))}:",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return
    results = search_cache.get(chat_id)
    if not results or index >= len(results):
        await q.message.reply_text("⚠️ Song expired. Please search again.")
        return
    video = results[index]
    status_msg = await q.message.reply_text(
        f"⬇️ <b>{e(sc('DOWNLOAD IN PROGRESS'))}</b>... {e(sc('PLEASE WAIT'))}",
        parse_mode=ParseMode.HTML
    )
    try:
        file_path, final_title = await download_audio_async(video["id"], video["title"], video["url"])
        if not file_path or not file_path.exists():
            await status_msg.edit_text(
                f"❌ <b>{e(sc('DOWNLOAD FAILED'))}</b>\n\n"
                f"{e(sc('ALL DOWNLOADER APIS AND LOCAL CONVERSION ARE CURRENTLY UNAVAILABLE'))}\n"
                f"{e(sc('PLEASE TRY AGAIN LATER OR CONTACT SUPPORT'))}",
                parse_mode=ParseMode.HTML
            )
            return
        await status_msg.edit_text(f"📤 <b>{e(sc('UPLOADING'))}</b>...", parse_mode=ParseMode.HTML)
        with open(file_path, "rb") as f:
            await q.message.reply_audio(
                audio=f,
                title=final_title[:100],
                performer=video.get("uploader", "Unknown")[:100],
                duration=video.get("duration", 0),
                caption=f"🎵 {e(final_title[:100])}\n✅ {e(sc('AUDIO DOWNLOADED SUCCESSFULLY'))}!",
                parse_mode=ParseMode.HTML
            )
        file_path.unlink(missing_ok=True)
        # Only increment and deduct after successful upload
        increment_downloads(user_id)
        if not is_premium(user) and remaining <= 0:
            # Free user used points because daily quota exceeded
            use_points_download(user_id)
        add_points(user_id, 1)  # 1 point per successful download
        await status_msg.delete()
    except Exception as e:
        logger.exception(f"Download error: {e}")
        await status_msg.edit_text(
            f"❌ <b>{e(sc('DOWNLOAD ERROR'))}</b>\n\n{e(str(e)[:200])}",
            parse_mode=ParseMode.HTML
        )

async def lyrics_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer("📜 Fetching lyrics...")
    chat_id = q.message.chat_id
    # callback data: lyrics_<index>
    try:
        index = int(q.data.split("_", 1)[1])
    except (ValueError, IndexError):
        await q.message.reply_text("⚠️ Invalid lyrics request.")
        return
    user_id = q.from_user.id
    user = get_user(user_id)
    if not is_premium(user) and not can_use_lyrics(user_id):
        keyboard = [
            [InlineKeyboardButton("⭐ UPGRADE TO PREMIUM", callback_data="upgrade_now")],
            [InlineKeyboardButton("🔗 EARN POINTS", callback_data="earn_points")]
        ]
        await q.message.reply_text(
            f"🔒 <b>{e(sc('LYRICS LOCKED'))}</b>\n\n"
            f"{e(sc('LYRICS ARE ONLY AVAILABLE FOR PREMIUM USERS OR WITH POINTS'))}\n\n"
            f"💡 <b>{e(sc('OPTIONS'))}</b>:\n"
            f"• 💎 {e(sc('UPGRADE TO PREMIUM FOR UNLIMITED LYRICS'))}\n"
            f"• 🔗 {e(sc('REFER FRIENDS TO EARN POINTS'))}\n"
            f"• ⭐ {e(sc('USE 5 POINTS PER LYRICS SEARCH'))}\n\n"
            f"{e(sc('CHOOSE AN OPTION BELOW'))}:",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return
    results = search_cache.get(chat_id)
    if not results or index >= len(results):
        await q.message.reply_text("⚠️ Song expired. Please search again.")
        return
    video = results[index]
    status = await q.message.reply_text(
        f"🔎 <b>{e(sc('SEARCHING LYRICS'))}</b>...",
        parse_mode=ParseMode.HTML
    )
    title = video["title"]
    artist = video.get("uploader", "")
    try:
        lyrics = await asyncio.to_thread(fetch_lyrics, title, artist)
    except Exception as e:
        logger.error(f"Lyrics fetch error: {e}")
        lyrics = None
    if not lyrics:
        await status.delete()
        await q.message.reply_text(
            f"🎵 <b>{e(video['title'][:100])}</b>\n\n"
            f"❌ {e(sc('LYRICS NOT FOUND'))}\n"
            f"{e(sc('WE TRIED MULTIPLE DATABASES BUT COULD NOT FIND THE LYRICS FOR THIS SONG'))}\n"
            f"{e(sc('TRY A DIFFERENT SONG OR CHECK BACK LATER'))}",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⬇️ DOWNLOAD AUDIO", callback_data=f"download_audio_{index}")],
                [InlineKeyboardButton("🔙 BACK TO RESULTS", callback_data="page_0")]
            ])
        )
        return
    # Deduct points only after successful retrieval
    if not is_premium(user):
        use_lyrics(user_id)
    if len(lyrics) > 4000:
        lyrics = lyrics[:3997] + "..."
    await status.delete()
    await q.message.reply_text(
        f"🎵 <b>{e(video['title'][:100])}</b>\n\n"
        f"📜 <b>{e(sc('LYRICS'))}</b>:\n\n{lyrics}",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("⬇️ DOWNLOAD AUDIO", callback_data=f"download_audio_{index}")],
            [InlineKeyboardButton("🔙 BACK TO RESULTS", callback_data="page_0")]
        ])
    )

async def more_tracks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    chat_id = q.message.chat_id
    # callback data: more_<offset>
    try:
        offset = int(q.data.split("_", 1)[1])
    except (ValueError, IndexError):
        await q.edit_message_text("⚠️ Invalid pagination.")
        return
    results = search_cache.get(chat_id)
    if not results:
        await q.edit_message_text("⚠️ Search expired. Please search again.")
        return
    keyboard = []
    for i in range(offset, min(offset + 20, len(results))):
        r = results[i]
        duration = f" ⏱{r['duration']//60}:{r['duration']%60:02d}" if r.get('duration') else ""
        title = r['title'][:45] + "..." if len(r['title']) > 45 else r['title']
        keyboard.append([InlineKeyboardButton(f"🎵 {title}{duration}", callback_data=f"song_{i}")])
    if offset + 20 < len(results):
        keyboard.append([InlineKeyboardButton("➕ MORE RESULTS", callback_data=f"more_{offset+20}")])
    keyboard.append([InlineKeyboardButton("🔙 BACK TO MENU", callback_data="back_to_menu")])
    await q.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup(keyboard))

async def page_navigation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    chat_id = q.message.chat_id
    # callback data: page_<index> (only page_0 used for now)
    try:
        page = int(q.data.split("_", 1)[1])
    except (ValueError, IndexError):
        await q.edit_message_text("⚠️ Invalid page.")
        return
    results = search_cache.get(chat_id)
    if not results:
        await q.edit_message_text("⚠️ Search expired.")
        return
    # Rebuild first page
    keyboard = []
    for i, r in enumerate(results[:20]):
        duration = f" ⏱{r['duration']//60}:{r['duration']%60:02d}" if r.get('duration') else ""
        title = r['title'][:45] + "..." if len(r['title']) > 45 else r['title']
        keyboard.append([InlineKeyboardButton(f"🎵 {title}{duration}", callback_data=f"song_{i}")])
    if len(results) > 20:
        keyboard.append([InlineKeyboardButton("➕ MORE RESULTS", callback_data="more_20")])
    keyboard.append([InlineKeyboardButton("🔙 BACK TO MENU", callback_data="back_to_menu")])
    await q.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup(keyboard))

async def trend_song(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    # callback data: trend_<video_id>
    try:
        video_id = q.data.split("_", 1)[1]
    except IndexError:
        await q.edit_message_text("⚠️ Invalid trending selection.")
        return
    msg = await q.message.reply_text(f"🔎 <b>{e(sc('LOADING'))}</b>...", parse_mode=ParseMode.HTML)
    try:
        opts = {
            "quiet": True,
            "extract_flat": True,
            "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        }
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(f"https://youtube.com/watch?v={video_id}", download=False)
            if info:
                video = {
                    "id": info["id"],
                    "title": info["title"],
                    "duration": int(info.get("duration", 0)),
                    "uploader": info.get("uploader", "Unknown"),
                    "view_count": info.get("view_count", 0),
                    "thumbnail": info.get("thumbnail", ""),
                    "url": f"https://youtube.com/watch?v={info['id']}"
                }
                chat_id = q.message.chat_id
                search_cache[chat_id] = [video]
                duration = f"{video['duration']//60}:{video['duration']%60:02d}" if video.get('duration') else "N/A"
                caption = (
                    f"🎵 <b>{e(video['title'][:100])}</b>\n\n"
                    f"👤 {e(sc('ARTIST'))}: {e(video.get('uploader', 'Unknown'))}\n"
                    f"⏱ {e(sc('DURATION'))}: {duration}\n\n"
                    f"{e(sc('CHOOSE AN ACTION'))}:"
                )
                keyboard = [
                    [InlineKeyboardButton("⬇️ DOWNLOAD AUDIO", callback_data="download_audio_0")],
                    [InlineKeyboardButton("📜 LYRICS", callback_data="lyrics_0")],
                    [InlineKeyboardButton("🔙 BACK TO MENU", callback_data="back_to_menu")]
                ]
                await msg.edit_text(caption, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(keyboard))
            else:
                await msg.edit_text("❌ Song not found.")
    except Exception as e:
        logger.error(f"Trend song error: {e}")
        await msg.edit_text(f"❌ Error: {e(str(e)[:200])}")

async def back_to_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    user_id = q.from_user.id
    text = f"🎵 <b>{e(sc('WELCOME BACK'))}</b>, {e(q.from_user.first_name)}!\n\n{e(sc('WHAT WOULD YOU LIKE TO DO'))}?"
    await q.edit_message_text("🎵 " + sc("MAIN MENU"), parse_mode=ParseMode.HTML)
    await q.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=get_main_menu_keyboard(user_id))

# ------------------------------------------------------------
# Payment handlers (Telegram Stars)
# ------------------------------------------------------------
async def pay_stars_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    user_id = q.from_user.id
    # Create invoice payload (unique per user)
    payload = f"premium_{user_id}_{int(time.time())}"
    # Store payload in context.user_data to verify later
    context.user_data["payment_payload"] = payload
    # Send invoice
    await context.bot.send_invoice(
        chat_id=user_id,
        title=PREMIUM_TITLE,
        description=PREMIUM_DESCRIPTION,
        payload=payload,
        provider_token="",  # For Telegram Stars, provider_token is empty
        currency="XTR",
        prices=[LabeledPrice(label="Premium", amount=PREMIUM_STARS)],
        start_parameter="premium",
    )

async def precheckout_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle pre-checkout queries."""
    query = update.pre_checkout_query
    # Always accept (you could add validation here)
    await query.answer(ok=True)

async def successful_payment_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle successful payment."""
    message = update.message
    if not message.successful_payment:
        return
    user_id = message.chat.id
    payload = message.successful_payment.invoice_payload
    # Validate payload format: premium_<user_id>_<timestamp>
    if payload.startswith("premium_"):
        parts = payload.split("_")
        if len(parts) >= 3 and parts[1].isdigit():
            target_user_id = int(parts[1])
            if target_user_id == user_id:
                # Activate premium
                user = get_user(user_id)
                current_expire = get_premium_expire(user)
                now = datetime.datetime.now()
                if current_expire and current_expire > now:
                    # Extend from current expiry
                    new_expire = current_expire + datetime.timedelta(days=PREMIUM_DAYS)
                else:
                    new_expire = now + datetime.timedelta(days=PREMIUM_DAYS)
                cursor.execute(
                    "UPDATE users SET premium_expire=? WHERE id=?",
                    (new_expire.isoformat(), user_id)
                )
                db.commit()
                # Notify user
                await context.bot.send_message(
                    user_id,
                    f"🎉 <b>{e(sc('CONGRATULATIONS'))}</b>\n\n"
                    f"{e(sc('PREMIUM ACTIVATED'))}!\n"
                    f"📅 {e(sc('EXPIRES'))}: {new_expire.strftime('%Y-%m-%d %H:%M')}\n\n"
                    f"🚀 {e(sc('ENJOY UNLIMITED DOWNLOADS'))}",
                    parse_mode=ParseMode.HTML
                )
                # Notify owner channel
                await notify_premium_channel(context, user_id, PREMIUM_DAYS, "Telegram Stars")
                return
    # If we reach here, payment wasn't for premium or invalid
    logger.warning(f"Received successful payment with unknown payload: {payload}")
    await context.bot.send_message(
        user_id,
        "⚠️ Payment received but could not be processed. Please contact support.",
    )

# ------------------------------------------------------------
# Social video download handler
# ------------------------------------------------------------
async def handle_video_download(update: Update, context: ContextTypes.DEFAULT_TYPE, url: str = None):
    message = update.effective_message
    if not url:
        url = extract_url(message.text)
    if not url or not is_video_url(url):
        return
    platform = get_platform_name(url)
    status_message = await message.reply_text(
        f"🔎 <b>{e(sc('DETECTED'))}</b>: {platform}\n"
        f"⏳ {e(sc('PREPARING YOUR VIDEO'))}...",
        parse_mode=ParseMode.HTML
    )
    async with video_dl_semaphore:
        temp_directory = tempfile.mkdtemp(prefix="social_dl")
        try:
            await status_message.edit_text(
                f"📥 <b>{e(sc('DOWNLOADING'))}</b> {platform} {e(sc('VIDEO'))}... {e(sc('PLEASE WAIT'))}",
                parse_mode=ParseMode.HTML
            )
            file_path, info = await asyncio.to_thread(download_social_video, url, temp_directory)
            if not file_path or not file_path.exists():
                raise RuntimeError("No downloadable video was produced")
            file_size = file_path.stat().st_size
            if file_size > MAX_VIDEO_SIZE_BYTES:
                raise RuntimeError(f"Video is larger than {MAX_VIDEO_SIZE_MB} MB")
            title = info.get("title") or "Social Media Video"
            await status_message.edit_text(f"📤 <b>{e(sc('UPLOADING VIDEO TO TELEGRAM'))}</b>...", parse_mode=ParseMode.HTML)
            await message.chat.send_action(action=ChatAction.UPLOAD_VIDEO)
            caption = f"🎬 {e(title[:700])}\n📦 {file_size // (1024*1024)} MB\n🌐 {platform}"
            with file_path.open("rb") as video_file:
                await message.reply_video(
                    video=video_file,
                    caption=caption,
                    parse_mode=ParseMode.HTML,
                    supports_streaming=True,
                    read_timeout=120,
                    write_timeout=120,
                    connect_timeout=30,
                    pool_timeout=30,
                )
            await status_message.delete()
        except yt_dlp.utils.DownloadError as exc:
            logger.warning(f"yt-dlp download failed: {exc}")
            await status_message.edit_text(
                f"❌ <b>{e(sc('COULD NOT DOWNLOAD THAT VIDEO'))}</b>\n\n"
                f"{e(sc('THE POST MAY BE PRIVATE, UNAVAILABLE, AGE/LOGIN RESTRICTED, DELETED, UNSUPPORTED, OR THE PLATFORM MAY HAVE CHANGED'))}",
                parse_mode=ParseMode.HTML
            )
        except Exception as exc:
            logger.exception("Unexpected download error")
            error_text = str(exc).lower()
            if "exceeds" in error_text or "larger than" in error_text:
                user_message = f"❌ <b>{e(sc('THE VIDEO IS LARGER THAN'))} {MAX_VIDEO_SIZE_MB} MB</b>"
            else:
                user_message = f"❌ {e(sc('SOMETHING WENT WRONG WHILE PROCESSING THE VIDEO'))}"
            await status_message.edit_text(user_message, parse_mode=ParseMode.HTML)
        finally:
            try:
                shutil.rmtree(temp_directory, ignore_errors=True)
            except:
                pass

# ------------------------------------------------------------
# Admin commands
# ------------------------------------------------------------
async def premium(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        await update.message.reply_text("⛔ Access denied")
        return
    try:
        target_user = int(context.args[0])
        days = int(context.args[1])
        user = get_user(target_user)
        current_expire = get_premium_expire(user)
        now = datetime.datetime.now()
        if current_expire and current_expire > now:
            expire = current_expire + datetime.timedelta(days=days)
        else:
            expire = now + datetime.timedelta(days=days)
        cursor.execute("UPDATE users SET premium_expire=? WHERE id=?", (expire.isoformat(), target_user))
        db.commit()
        await context.bot.send_message(
            target_user,
            f"🎉 <b>{e(sc('CONGRATULATIONS'))}</b>\n\n"
            f"{e(sc('YOU HAVE BEEN AWARDED PREMIUM ACCESS'))}\n"
            f"📅 {e(sc('EXPIRES'))}: {expire.strftime('%Y-%m-%d %H:%M')}\n\n"
            f"🚀 {e(sc('ENJOY UNLIMITED DOWNLOADS'))}",
            parse_mode=ParseMode.HTML
        )
        await notify_premium_channel(context, target_user, days, "Owner")
        await update.message.reply_text(
            f"✅ Premium granted to user {target_user} for {days} days",
            parse_mode=ParseMode.HTML
        )
    except Exception as e:
        await update.message.reply_text("❌ Usage: /premium <user_id> <days>")

async def reward(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return
    try:
        target_user = int(context.args[0])
        pts = int(context.args[1])
        cursor.execute("UPDATE users SET points=points+? WHERE id=?", (pts, target_user))
        db.commit()
        await context.bot.send_message(
            target_user,
            f"🎉 <b>{e(sc('CONGRATULATIONS'))}</b>\n\n"
            f"{e(sc('YOU HAVE BEEN AWARDED'))} {pts} {e(sc('POINTS'))}\n"
            f"💰 {e(sc('NEW BALANCE'))}: {get_user_points(target_user)}",
            parse_mode=ParseMode.HTML
        )
        await update.message.reply_text(
            f"✅ Awarded {pts} points to user {target_user}",
            parse_mode=ParseMode.HTML
        )
    except:
        await update.message.reply_text("❌ Usage: /reward <user_id> <points>")

async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return
    cursor.execute("SELECT COUNT(*) FROM users")
    total_users = cursor.fetchone()[0]
    cursor.execute("SELECT COUNT(*) FROM users WHERE premium_expire > datetime('now')")
    premium_users = cursor.fetchone()[0]
    cursor.execute("SELECT SUM(total_downloads) FROM users")
    total_downloads = cursor.fetchone()[0] or 0
    cursor.execute("SELECT SUM(points) FROM users")
    total_points = cursor.fetchone()[0] or 0
    await update.message.reply_text(
        f"📊 <b>{e(sc('BOT STATISTICS'))}</b>\n\n"
        f"👥 {e(sc('TOTAL USERS'))}: {total_users}\n"
        f"💎 {e(sc('PREMIUM USERS'))}: {premium_users}\n"
        f"⬇️ {e(sc('TOTAL DOWNLOADS'))}: {total_downloads}\n"
        f"💰 {e(sc('TOTAL POINTS CIRCULATING'))}: {total_points}",
        parse_mode=ParseMode.HTML
    )

async def broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return
    if not update.message.reply_to_message:
        await update.message.reply_text("📢 Reply to a message to broadcast")
        return
    msg = update.message.reply_to_message
    cursor.execute("SELECT id FROM users")
    users = [row[0] for row in cursor.fetchall()]
    delivered = 0
    failed = 0
    status = await update.message.reply_text(f"📢 Broadcasting to {len(users)} users...")
    for uid in users:
        try:
            if msg.text:
                await context.bot.send_message(uid, msg.text, parse_mode=ParseMode.HTML)
            elif msg.photo:
                await context.bot.send_photo(uid, msg.photo[-1].file_id, caption=msg.caption, parse_mode=ParseMode.HTML)
            elif msg.video:
                await context.bot.send_video(uid, msg.video.file_id, caption=msg.caption, parse_mode=ParseMode.HTML)
            elif msg.audio:
                await context.bot.send_audio(uid, msg.audio.file_id, caption=msg.caption, parse_mode=ParseMode.HTML)
            elif msg.document:
                await context.bot.send_document(uid, msg.document.file_id, caption=msg.caption, parse_mode=ParseMode.HTML)
            delivered += 1
            if delivered % 30 == 0:
                await asyncio.sleep(0.5)
        except Exception as e:
            logger.error(f"Broadcast failed for {uid}: {e}")
            failed += 1
    await status.edit_text(
        f"📢 <b>{e(sc('BROADCAST COMPLETE'))}</b>\n\n"
        f"👥 {e(sc('TOTAL'))}: {len(users)}\n"
        f"✅ {e(sc('DELIVERED'))}: {delivered}\n"
        f"❌ {e(sc('FAILED'))}: {failed}",
        parse_mode=ParseMode.HTML
    )

# ------------------------------------------------------------
# Error handler
# ------------------------------------------------------------
async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.exception(f"Update {update} caused error: {context.error}")
    if update and update.effective_message:
        try:
            await update.effective_message.reply_text(
                "⚠️ <b>Oops! Something went wrong.</b>\n\n"
                "Please try again later. If the problem persists, contact @Mr_Unique_Hacker002",
                parse_mode=ParseMode.HTML
            )
        except Exception:
            pass

# ------------------------------------------------------------
# Main entry point
# ------------------------------------------------------------
def main():
    print(f"""
╔══════════════════════════════════════════════════════════════╗
║           🎵 ADVANCED MUSIC BOT 🎵                           ║
║   CREATED BY ❦ ᴍʀ ᴅᴀʀᴋʜᴀᴄᴋᴇʀ                        ║
╚══════════════════════════════════════════════════════════════╝
    """)
    if not COOKIES_FILE.exists():
        print(f"⚠️ No cookie file found at: {COOKIES_FILE}")
        print("   yt-dlp fallback may be limited.")
    if not check_ffmpeg():
        print("⚠️ FFmpeg not found. Video merging may not work. Please install FFmpeg.")
    print("🤖 Bot is running... Press Ctrl+C to stop.")

    app = Application.builder().token(TOKEN).build()

    # Command handlers
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("account", show_account))
    app.add_handler(CommandHandler("trending", show_trending))
    app.add_handler(CommandHandler("help", show_help))
    app.add_handler(CommandHandler("premium", premium))
    app.add_handler(CommandHandler("reward", reward))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(CommandHandler("broadcast", broadcast))

    # Message handler for text (non-command)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_menu_buttons))

    # Callback query handlers
    app.add_handler(CallbackQueryHandler(back_to_menu, pattern="^back_to_menu$"))
    app.add_handler(CallbackQueryHandler(song_info, pattern="^song_"))
    app.add_handler(CallbackQueryHandler(download_callback, pattern="^download_audio_"))
    app.add_handler(CallbackQueryHandler(lyrics_callback, pattern="^lyrics_"))
    app.add_handler(CallbackQueryHandler(more_tracks, pattern="^more_"))
    app.add_handler(CallbackQueryHandler(page_navigation, pattern="^page_"))
    app.add_handler(CallbackQueryHandler(trend_song, pattern="^trend_"))
    app.add_handler(CallbackQueryHandler(show_premium, pattern="^upgrade_now$"))
    app.add_handler(CallbackQueryHandler(show_referral, pattern="^earn_points$"))
    app.add_handler(CallbackQueryHandler(pay_stars_callback, pattern="^pay_stars$"))

    # Payment handlers
    app.add_handler(PreCheckoutQueryHandler(precheckout_callback))
    app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment_callback))

    # Error handler
    app.add_error_handler(error_handler)

    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
