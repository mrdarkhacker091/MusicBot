#!/usr/bin/env python3
# -- coding: utf-8 --

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
from pathlib import Path
from urllib.parse import urlparse

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import yt_dlp
import requests
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters
)
from telegram.constants import ParseMode, ChatAction

TOKEN = "8350984585:AAFSm-9J9MTrwluT1WQk6eHhPplSoBR6c0k"
OWNER_ID = int(os.getenv("OWNER_ID", "8854936887"))
BOT_USERNAME = os.getenv("BOT_USERNAME", "All_MusicDownloader_Bot")
PREMIUM_CHANNEL_ID = os.getenv("PREMIUM_CHANNEL_ID", "")

DOWNLOAD_LIMIT = 5
COOLDOWN_HOURS = 24
POINTS_PER_REFERRAL = 10
POINTS_PER_DOWNLOAD = 10
POINTS_PER_LYRICS = 5

BASE_DIR = Path(__file__).parent.resolve()
DOWNLOADS_DIR = BASE_DIR / "downloads"
COOKIES_DIR = BASE_DIR / "cookies"
THUMBNAILS_DIR = BASE_DIR / "thumbnails"

for d in [DOWNLOADS_DIR, COOKIES_DIR, THUMBNAILS_DIR]:
    d.mkdir(parents=True, exist_ok=True)

COOKIES_FILE = COOKIES_DIR / "cookies.txt"

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

SMALL_CAPS_MAP = str.maketrans(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ",
    "ᴀʙᴄᴅᴇꜰɢʜɪᴊᴋʟᴍɴᴏᴘǫʀꜱᴛᴜᴠᴡxʏᴢᴀʙᴄᴅᴇꜰɢʜɪᴊᴋʟᴍɴᴏᴘǫʀꜱᴛᴜᴠᴡxʏᴢ"
)

def sc(text: str) -> str:
    if not text:
        return ""
    return text.translate(SMALL_CAPS_MAP)

db = sqlite3.connect("bot.db", check_same_thread=False)
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
    if not user or not user[4]:
        return False
    try:
        last_reset = datetime.datetime.fromisoformat(user[4])
        now = datetime.datetime.now()
        if (now - last_reset).total_seconds() > COOLDOWN_HOURS * 3600:
            cursor.execute(
                "UPDATE users SET downloads=0,last_reset=? WHERE id=?",
                (now.isoformat(), user[0])
            )
            db.commit()
            return True
    except:
        pass
    return False

def is_premium(user):
    if not user or not user[2]:
        return False
    try:
        expire = datetime.datetime.fromisoformat(user[2])
        return expire > datetime.datetime.now()
    except:
        return False

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
    return user[1] if user else 0

def can_download(user_id):
    user = get_user(user_id)
    reset_downloads(user)
    if is_premium(user):
        return True, 0
    remaining = DOWNLOAD_LIMIT - (user[3] if user else 0)
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
    cursor.execute("SELECT COUNT() FROM users WHERE referrer=?", (user_id,))
    return cursor.fetchone()[0]

async def notify_premium_channel(context, user_id, days, granted_by):
    if not PREMIUM_CHANNEL_ID:
        return
    try:
        user = get_user(user_id)
        username = user[7] if user and user[7] else sc("NONE")
        first_name = user[8] if user and user[8] else sc("UNKNOWN")
        expire = datetime.datetime.now() + datetime.timedelta(days=days)
        expire_str = expire.strftime("%Y-%m-%d %H:%M:%S")
        text = f"""⭐ {sc('NEW PREMIUM USER')}  👤 {sc('NAME')}: {first_name} 📛 {sc('USERNAME')}: @{username} 🆔 {sc('USER ID')}: {user_id} 📅 {sc('DAYS')}: {days} ⏰ {sc('EXPIRE')}: {expire_str} 👤 {sc('GRANTED BY')}: {granted_by}  🚀 {sc('START BOT')} | 💎 {sc('GET PREMIUM')}"""
        await context.bot.send_message(PREMIUM_CHANNEL_ID, text, parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        logger.error(f"{sc('PREMIUM CHANNEL NOTIFICATION FAILED')}: {e}")

async def notify_new_user_channel(context, user_id, referrer_id=None):
    if not PREMIUM_CHANNEL_ID:
        return
    try:
        user = get_user(user_id)
        username = user[7] if user and user[7] else sc("NONE")
        first_name = user[8] if user and user[8] else sc("UNKNOWN")
        join_date = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        if referrer_id:
            ref_user = get_user(referrer_id)
            ref_name = ref_user[8] if ref_user and ref_user[8] else str(referrer_id)
            ref_by = f"{ref_name} ({referrer_id})"
        else:
            ref_by = sc("DIRECT JOIN")
        text = f"""📥 {sc('NEW USER JOINED')}  👤 {sc('NAME')}: {first_name} 📛 {sc('USERNAME')}: @{username} 🆔 {sc('USER ID')}: {user_id} 📅 {sc('DATE')}: {join_date} 👥 {sc('REFERRED BY')}: {ref_by}  🤖 {sc('START BOT')}"""
        await context.bot.send_message(PREMIUM_CHANNEL_ID, text, parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        logger.error(f"{sc('NEW USER CHANNEL NOTIFICATION FAILED')}: {e}")


def get_main_menu_keyboard(user_id: int) -> ReplyKeyboardMarkup:
    user = get_user(user_id)
    points = user[1] if user else 0
    downloads = user[3] if user else 0
    premium = is_premium(user)
    premium_text = sc("💎 PREMIUM") if premium else sc("⭐ UPGRADE")
    keyboard = [
        [sc("🎵 SEARCH MUSIC"), sc("🔥 TRENDING")],
        [sc("📊 ACCOUNT") + f" ({points} pts)", sc("⬇️ DOWNLOADS") + f" {downloads}/{DOWNLOAD_LIMIT}"],
        [sc("🔗 REFERRAL"), sc("🤖 OTHER BOTS")],
        [sc("📹 VIDEO DOWNLOADER"), premium_text],
        [sc("❓ HELP"), sc("📞 CONTACT")]
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)


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
        logger.error(f"{sc('SEARCH ERROR')}: {e}")
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

def get_audio_download_url(youtube_url):
    encoded_url = urllib.parse.quote_plus(youtube_url)
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
            "func": lambda: requests.get(f"https://api.alyachan.pro/api/ytmp3?url={encoded_url}&apikey=G7I6X7", timeout=15)
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
            r = method["func"]()
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
    logger.info(sc("ALL EXTERNAL APIS FAILED. TRYING YT-DLP..."))
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
        logger.error(f"{sc('YT-DLP FALLBACK FAILED')}: {e}")
    raise Exception(sc("ALL DOWNLOADER APIS AND YT-DLP FALLBACK FAILED"))

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
                raise Exception(f"{sc('DOWNLOAD URL RETURNED STATUS')} {r.status_code}")
            with open(out_path, "wb") as f:
                for chunk in r.iter_content(chunk_size=1024 * 64):
                    if chunk:
                        f.write(chunk)
            if not out_path.exists() or out_path.stat().st_size < 50 * 1024:
                raise Exception(sc("DOWNLOADED FILE TOO SMALL OR CORRUPT"))
            return out_path, final_title or title
        except Exception as e:
            logger.error(f"{sc('DOWNLOAD ERROR')}: {e}")
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

def fetch_lyrics(title, artist=""):
    title = title.strip()
    artist = artist.strip()
    if not artist and " - " in title:
        parts = title.split(" - ", 1)
        artist = parts[0].strip()
        title = parts[1].strip()
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
    try:
        url = f"https://api.musixmatch.com/ws/1.1/matcher.lyrics.get?q_track={requests.utils.quote(title)}&q_artist={requests.utils.quote(artist)}&apikey=YOUR_MUSIXMATCH_API_KEY"
        r = requests.get(url, timeout=8)
        if r.status_code == 200:
            data = r.json()
            lyrics = data.get("message", {}).get("body", {}).get("lyrics", {}).get("lyrics_body")
            if lyrics:
                return lyrics
    except:
        pass
    return None


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
    return match.group(0).rstrip(".,!?)]}")

def is_video_url(url: str) -> bool:
    try:
        hostname = urlparse(url).hostname
        if not hostname:
            return False
        hostname = hostname.lower().rstrip(".")
        return any(
            hostname == d or hostname.endswith("." + d)
            for d in SUPPORTED_VIDEO_DOMAINS
        )
    except Exception:
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

def download_social_video(url: str, output_dir: str) -> tuple[Path | None, dict]:
    out_path = Path(output_dir)
    template = str(out_path / "%(title).80s-%(id)s.%(ext)s")
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


# ============================================================
# PART 2 — TELEGRAM HANDLERS
# ============================================================

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
    is_new = db_user[9] is None if db_user else True
    if is_new or (db_user and not db_user[8]):
        update_user_profile(user_id, user.username, user.first_name)
    if db_user and db_user[5] is None and ref and ref != user_id:
        set_referral(user_id, ref)
        add_points(ref, POINTS_PER_REFERRAL)
        ref_count = get_referral_count(ref)
        try:
            await context.bot.send_message(
                ref,
                f"""🎯 {sc('FRESH POINTS DROP!')}  👤 {sc('NEW REFERRAL')}: {sc(user.first_name or 'USER')} 📛 {sc('USERNAME')}: @{user.username or sc('NONE')} 🆔 {sc('USER ID')}: {user_id} 📅 {sc('DATE')}: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')} 📊 {sc('TOTAL REFERRALS')}: {ref_count}  ✅ {sc('YOU EARNED')} +{POINTS_PER_REFERRAL} {sc('POINTS!')} 💡 {sc('10 POINTS = 1 DOWNLOAD + 2 LYRICS SEARCHES')}  🚀 {sc('KEEP SHARING YOUR LINK!')}""",
                parse_mode=ParseMode.MARKDOWN
            )
        except Exception as e:
            logger.error(f"{sc('REFERRAL NOTIFY FAILED')}: {e}")
    await notify_new_user_channel(context, user_id, ref)
    text = f"""
🎵 {sc('WELCOME TO ADVANCED MUSIC BOT')}  👋 {sc('HI')}, {user.first_name}!  {sc('I CAN HELP YOU FIND AND DOWNLOAD MUSIC FROM YOUTUBE AND VIDEOS FROM SOCIAL MEDIA')}  ✨ {sc('FEATURES')}: • 🎵 {sc('SEARCH ANY SONG')} • ⬇️ {sc('DOWNLOAD MP3 AUDIO')} • 📜 {sc('GET SONG LYRICS')} • 🔗 {sc('REFER FRIENDS & EARN POINTS')} • 📹 {sc('DOWNLOAD SOCIAL MEDIA VIDEOS')} • 💎 {sc('PREMIUM FOR UNLIMITED DOWNLOADS')}  {sc('USE THE BUTTONS BELOW TO GET STARTED')}! """
    await update.message.reply_text(
        text,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=get_main_menu_keyboard(user_id)
    )

async def handle_menu_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = update.message.text
    if text == sc("🎵 SEARCH MUSIC"):
        await update.message.reply_text(
            f"🎵 {sc('SEARCH MUSIC')}  {sc('SEND ME A SONG NAME OR ARTIST TO SEARCH')}  {sc('EXAMPLE')}: \"Calm Down Rema\"", 
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=get_main_menu_keyboard(user_id)
        )
    elif text == sc("🔥 TRENDING"):
        await show_trending(update, context)
    elif text.startswith(sc("📊 ACCOUNT")):
        await show_account(update, context)
    elif text.startswith(sc("⬇️ DOWNLOADS")):
        user = get_user(user_id)
        reset_downloads(user)
        remaining = DOWNLOAD_LIMIT - (user[3] if user else 0)
        premium = is_premium(user)
        pts = get_user_points(user_id)
        await update.message.reply_text(
            f"""📊 {sc('DOWNLOAD USAGE')}  {sc('TODAY')}: {user[3] if user else 0} / {DOWNLOAD_LIMIT} {sc('REMAINING')}: {remaining if not premium else sc('UNLIMITED')} {sc('POINTS BALANCE')}: {pts} {sc('PREMIUM')}: {'✅ ' + sc('ACTIVE') if premium else '❌ ' + sc('NOT ACTIVE')}  💡 {sc('10 POINTS = 1 DOWNLOAD + 2 LYRICS SEARCHES')}""",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=get_main_menu_keyboard(user_id)
        )
    elif text == sc("🔗 REFERRAL"):
        await show_referral(update, context)
    elif text == sc("🤖 OTHER BOTS"):
        await show_other_bots(update, context)
    elif text == sc("📹 VIDEO DOWNLOADER"):
        await update.message.reply_text(
            f"""📹 {sc('SOCIAL VIDEO DOWNLOADER')}  {sc('SEND ME A LINK FROM')}: • TikTok • Instagram • Facebook • YouTube • X/Twitter • Reddit  {sc('I WILL DOWNLOAD AND SEND THE VIDEO TO YOU')}  {sc('JUST PASTE THE LINK DIRECTLY')}""",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=get_main_menu_keyboard(user_id)
        )
    elif text == sc("⭐ UPGRADE") or text == sc("💎 PREMIUM"):
        await show_premium(update, context)
    elif text == sc("❓ HELP"):
        await show_help(update, context)
    elif text == sc("📞 CONTACT"):
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
    points = user[1] if user else 0
    downloads = user[3] if user else 0
    total_dl = user[6] if user else 0
    premium_status = "💎 " + sc("ACTIVE") if is_premium(user) else "❌ " + sc("INACTIVE")
    ref_count = get_referral_count(user_id)
    text = f"""
👤 {sc('YOUR ACCOUNT')}  💰 {sc('POINTS')}: {points} ⬇️ {sc('DOWNLOADS TODAY')}: {downloads}/{DOWNLOAD_LIMIT} 📊 {sc('TOTAL DOWNLOADS')}: {total_dl} 💎 {sc('PREMIUM')}: {premium_status} 🔗 {sc('REFERRALS')}: {ref_count}  {sc('INVITE FRIENDS AND EARN 10 POINTS EACH')}! """
    await update.message.reply_text(
        text,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=get_main_menu_keyboard(user_id)
    )

async def show_referral(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    link = f"https://t.me/{BOT_USERNAME}?start={user_id}"
    ref_count = get_referral_count(user_id)
    pts = get_user_points(user_id)
    text = f"""
🔗 {sc('YOUR REFERRAL LINK')}  {sc('SHARE THIS LINK WITH YOUR FRIENDS')}:  {link}  ✨ {sc('HOW IT WORKS')}: • {sc('EACH FRIEND WHO JOINS GIVES YOU')} +{POINTS_PER_REFERRAL} {sc('POINTS')} • {sc('10 POINTS = 1 SONG DOWNLOAD + 2 LYRICS SEARCHES')} • {sc('USE POINTS WHEN YOU HIT YOUR DAILY LIMIT')}  📊 {sc('YOUR STATS')}: 👥 {sc('REFERRALS')}: {ref_count} 💰 {sc('POINTS')}: {pts}  {sc('TAP AND HOLD TO COPY THE LINK')} """
    share_text = urllib.parse.quote(f"🎵 {sc('GET MUSIC FOR FREE')} — {sc('DOWNLOAD SONGS AND VIDEOS')}")
    message = update.effective_message
    await message.reply_text(
        text,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton(sc("📤 SHARE LINK"), url=f"https://t.me/share/url?url={link}&text={share_text}")]
        ])
    )

async def show_other_bots(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = f"""
🤖 {sc('OUR BOT NETWORK')}  🟢 {sc('ONLINE')}  • @DarkHacker_BanBot — GhostShell MD   {sc('A MULTIPURPOSE WHATSAPP BOT WITH GROUP MANAGEMENT, ANTILINK, ANTISPAM, WELCOME/GOODBYE MESSAGES, TAG-ALL, STICKER CREATION, AND FULL WHATSAPP AUTOMATION')}  • @Image_ConverterTo_linkBot   {sc('CONVERT IMAGES, VIDEOS, FILES, AND VOICE NOTES TO DIRECT DOWNLOAD LINKS INSTANTLY')}  🔴 {sc('MORE BOTS COMING SOON')}  @WormGPT_Prover_Bot {sc('DESCRIPTION WILL BE ADDED BY OWNER')}  @Whatsapp2_Ban_bot {sc('DESCRIPTION WILL BE ADDED BY OWNER')}  💬 {sc('STAY TUNED FOR MORE POWERFUL TOOLS')}! """
    await update.message.reply_text(
        text,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=get_main_menu_keyboard(update.effective_user.id)
    )

async def show_premium(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user = get_user(user_id)
    message = update.effective_message
    if is_premium(user):
        expire = "N/A"
        if user[2]:
            try:
                expire = datetime.datetime.fromisoformat(user[2]).strftime("%Y-%m-%d %H:%M")
            except:
                pass
        text = f"""💎 {sc('PREMIUM ACTIVE')}  📅 {sc('EXPIRES')}: {expire} ⬇️ {sc('DAILY LIMIT')}: {sc('UNLIMITED')} 🎵 {sc('LYRICS')}: {sc('UNLIMITED')} 📹 {sc('VIDEO DOWNLOADS')}: {sc('UNLIMITED')}  {sc('THANK YOU FOR SUPPORTING THE BOT')} 🙏"""
        await message.reply_text(
            text,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=get_main_menu_keyboard(user_id)
        )
        return
    keyboard = [
        [InlineKeyboardButton(sc("⭐ PAY WITH TELEGRAM STARS"), callback_data="pay_stars")],
        [InlineKeyboardButton(sc("📞 CONTACT OWNER"), url="https://t.me/Mr_Unique_Hacker002")],
        [InlineKeyboardButton(sc("🔙 BACK"), callback_data="back_to_menu")]
    ]
    text = f"""
💎 {sc('UPGRADE TO PREMIUM')}  ✨ {sc('PREMIUM BENEFITS')}: • ⬇️ {sc('UNLIMITED DOWNLOADS PER DAY')} • 🚀 {sc('PRIORITY DOWNLOAD SPEED')} • 🎵 {sc('HD AUDIO QUALITY')} • 📜 {sc('UNLIMITED LYRICS ACCESS')} • 📹 {sc('UNLIMITED VIDEO DOWNLOADS')} • 🚫 {sc('NO COOLDOWN PERIODS')} • ⚡ {sc('NO ADS OR INTERRUPTIONS')}  💰 {sc('PAYMENT METHODS')}: • ⭐ {sc('TELEGRAM STARS — FAST & SECURE')} • 💬 {sc('CONTACT')} @Mr_Unique_Hacker002 {sc('FOR OTHER METHODS')}  🎯 {sc('HOW TO PAY WITH STARS')}: 1️⃣ {sc('CLICK THE BUTTON BELOW')} 2️⃣ {sc('SEND THE REQUIRED STARS AS A GIFT')} 3️⃣ {sc('SCREENSHOT THE PAYMENT AND SEND TO')} @Mr_Unique_Hacker002 4️⃣ {sc('YOUR PREMIUM WILL BE ACTIVATED INSTANTLY')}  🔥 {sc('DONT MISS OUT — UPGRADE NOW')}! """
    await message.reply_text(
        text,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def show_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = f"""
❓ {sc('HELP GUIDE')}  {sc('HOW TO USE')}: 1️⃣ {sc('SEND A SONG NAME OR ARTIST')} 2️⃣ {sc('CLICK ON A RESULT')} 3️⃣ {sc('CHOOSE DOWNLOAD AUDIO OR LYRICS')}  {sc('COMMANDS')}: /start — {sc('START THE BOT')} /account — {sc('VIEW YOUR PROFILE')} /trending — {sc('TRENDING SONGS')} /help — {sc('THIS HELP MESSAGE')}  {sc('POINTS SYSTEM')}: • 1 {sc('REFERRAL')} = 10 {sc('POINTS')} • 10 {sc('POINTS')} = 1 {sc('SONG DOWNLOAD')} + 2 {sc('LYRICS SEARCHES')} • {sc('USE POINTS WHEN YOU HIT YOUR DAILY LIMIT')}  {sc('PREMIUM BENEFITS')}: • {sc('UNLIMITED DOWNLOADS')} • {sc('UNLIMITED LYRICS')} • {sc('UNLIMITED VIDEO DOWNLOADS')} • {sc('NO COOLDOWNS')}  {sc('CONTACT')} @Mr_Unique_Hacker002 {sc('FOR PREMIUM')} """
    await update.message.reply_text(
        text,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=get_main_menu_keyboard(update.effective_user.id)
    )

async def show_contact(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton(sc("📩 TELEGRAM"), url="https://t.me/Mr_Unique_Hacker002")],
        [InlineKeyboardButton(sc("📱 WHATSAPP"), url="https://wa.me/2349123578884")]
    ]
    await update.message.reply_text(
        f"📞 {sc('CONTACT US')}  {sc('REACH OUT TO US ON')}:",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def show_trending(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text(
        f"🔥 {sc('FETCHING REAL TRENDING SONGS')}...",
        parse_mode=ParseMode.MARKDOWN
    )
    try:
        songs = await asyncio.get_event_loop().run_in_executor(None, fetch_trending_songs, 10)
    except Exception as e:
        logger.error(f"{sc('TRENDING ERROR')}: {e}")
        songs = []
    if not songs:
        await msg.edit_text(
            f"❌ {sc('COULD NOT FETCH TRENDING SONGS')}  {sc('PLEASE TRY AGAIN LATER')}",
            parse_mode=ParseMode.MARKDOWN
        )
        return
    keyboard = []
    for i, song in enumerate(songs[:10]):
        title = song['title'][:40] + "..." if len(song['title']) > 40 else song['title']
        duration = f" ⏱{song['duration']//60}:{song['duration']%60:02d}" if song.get('duration') else ""
        keyboard.append([InlineKeyboardButton(f"🔥 {title}{duration}", callback_data=f"trend{song['id']}")])
    keyboard.append([InlineKeyboardButton(sc("🔙 BACK TO MENU"), callback_data="back_to_menu")])
    await msg.edit_text(
        f"🔥 {sc('TRENDING SONGS RIGHT NOW')}  {sc('CLICK ANY SONG TO DOWNLOAD')}:",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def song_search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    query = update.message.text.strip()
    if len(query) < 2:
        await update.message.reply_text(
            f"⚠️ {sc('PLEASE ENTER AT LEAST 2 CHARACTERS TO SEARCH')}",
            reply_markup=get_main_menu_keyboard(user_id)
        )
        return
    msg = await update.message.reply_text(
        f"🔎 {sc('SEARCHING')}...",
        parse_mode=ParseMode.MARKDOWN
    )
    results = await asyncio.get_event_loop().run_in_executor(None, search_music, query)
    if not results:
        await msg.edit_text(
            f"❌ {sc('NO RESULTS FOUND')}  💡 {sc('TRY DIFFERENT KEYWORDS OR ARTIST NAME')}",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(sc("🔙 BACK TO MENU"), callback_data="back_to_menu")]])
        )
        return
    chat_id = update.message.chat_id
    search_cache[chat_id] = results
    keyboard = []
    for i, r in enumerate(results[:20]):
        duration = f" ⏱{r['duration']//60}:{r['duration']%60:02d}" if r.get('duration') else ""
        title = r['title'][:45] + "..." if len(r['title']) > 45 else r['title']
        keyboard.append([InlineKeyboardButton(f"🎵 {title}{duration}", callback_data=f"song{i}")])
    if len(results) > 20:
        keyboard.append([InlineKeyboardButton(sc("➕ MORE RESULTS"), callback_data="more_20")])
    keyboard.append([InlineKeyboardButton(sc("🔙 BACK TO MENU"), callback_data="back_to_menu")])
    await msg.edit_text(
        f"🎵 {sc('RESULTS FOR')}: {query[:50]}  {sc('SELECT A SONG')}:",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def song_info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    chat_id = q.message.chat_id
    index = int(q.data.split("")[1])
    results = search_cache.get(chat_id)
    if not results or index >= len(results):
        await q.edit_message_text(f"⚠️ {sc('SONG EXPIRED. PLEASE SEARCH AGAIN')}")
        return
    video = results[index]
    duration = f"{video['duration']//60}:{video['duration']%60:02d}" if video.get('duration') else "N/A"
    caption = f"""
🎵 {video['title'][:100]}  👤 {sc('ARTIST')}: {video.get('uploader', 'Unknown')} ⏱ {sc('DURATION')}: {duration} 🔗 {sc('WATCH ON YOUTUBE')}  {sc('CHOOSE AN ACTION')}: """
    keyboard = [
        [InlineKeyboardButton(sc("⬇️ DOWNLOAD AUDIO"), callback_data=f"download_audio{index}")],
        [InlineKeyboardButton(sc("📜 LYRICS"), callback_data=f"lyrics_{index}")],
        [InlineKeyboardButton(sc("🔙 BACK TO RESULTS"), callback_data="page_0")]
    ]
    await q.edit_message_text(
        caption,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def download_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer(f"⬇️ {sc('STARTING DOWNLOAD')}...")
    user_id = q.from_user.id
    chat_id = q.message.chat_id
    index = int(q.data.split("")[2])
    user = get_user(user_id)
    reset_downloads(user)
    can_dl, remaining = can_download(user_id)
    if not can_dl:
        keyboard = [
            [InlineKeyboardButton(sc("⭐ UPGRADE TO PREMIUM"), callback_data="upgrade_now")],
            [InlineKeyboardButton(sc("🔗 EARN POINTS"), callback_data="earn_points")]
        ]
        await q.message.reply_text(
            f"""⛔ {sc('DOWNLOAD LIMIT REACHED')}  {sc('YOU HAVE USED ALL YOUR FREE DOWNLOADS FOR TODAY')}  💡 {sc('OPTIONS')}: • 💎 {sc('UPGRADE TO PREMIUM FOR UNLIMITED DOWNLOADS')} • 🔗 {sc('REFER FRIENDS TO EARN POINTS')} • ⭐ {sc('USE POINTS TO DOWNLOAD (10 PTS = 1 DL)')}  {sc('CHOOSE AN OPTION BELOW')}:""",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return
    results = search_cache.get(chat_id)
    if not results or index >= len(results):
        await q.message.reply_text(f"⚠️ {sc('SONG EXPIRED. PLEASE SEARCH AGAIN')}")
        return
    video = results[index]
    status_msg = await q.message.reply_text(
        f"⬇️ {sc('DOWNLOAD IN PROGRESS')}... {sc('PLEASE WAIT')}",
        parse_mode=ParseMode.MARKDOWN
    )
    try:
        file_path, final_title = await download_audio_async(video["id"], video["title"], video["url"])
        if not file_path or not file_path.exists():
            await status_msg.edit_text(
                f"""❌ {sc('DOWNLOAD FAILED')}  {sc('ALL DOWNLOADER APIS AND LOCAL CONVERSION ARE CURRENTLY UNAVAILABLE')} {sc('PLEASE TRY AGAIN LATER OR CONTACT SUPPORT')}""",
                parse_mode=ParseMode.MARKDOWN
            )
            return
        await status_msg.edit_text(f"📤 {sc('UPLOADING')}...", parse_mode=ParseMode.MARKDOWN)
        with open(file_path, "rb") as f:
            await q.message.reply_audio(
                audio=f,
                title=final_title[:100],
                performer=video.get("uploader", "Unknown")[:100],
                duration=video.get("duration", 0),
                caption=f"🎵 {final_title[:100]}  ✅ {sc('AUDIO DOWNLOADED SUCCESSFULLY')}!",
                parse_mode=ParseMode.MARKDOWN
            )
        file_path.unlink(missing_ok=True)
        increment_downloads(user_id)
        if not is_premium(user) and remaining <= 0:
            use_points_download(user_id)
        add_points(user_id, 1)
        await status_msg.delete()
    except Exception as e:
        logger.error(f"{sc('DOWNLOAD ERROR')}: {e}")
        await status_msg.edit_text(
            f"❌ {sc('DOWNLOAD ERROR')}  {str(e)[:200]}",
            parse_mode=ParseMode.MARKDOWN
        )

async def lyrics_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer(f"📜 {sc('FETCHING LYRICS')}...")
    chat_id = q.message.chat_id
    index = int(q.data.split("")[1])
    user_id = q.from_user.id
    user = get_user(user_id)
    if not is_premium(user) and not can_use_lyrics(user_id):
        keyboard = [
            [InlineKeyboardButton(sc("⭐ UPGRADE TO PREMIUM"), callback_data="upgrade_now")],
            [InlineKeyboardButton(sc("🔗 EARN POINTS"), callback_data="earn_points")]
        ]
        await q.message.reply_text(
            f"""🔒 {sc('LYRICS LOCKED')}  {sc('LYRICS ARE ONLY AVAILABLE FOR PREMIUM USERS OR WITH POINTS')}  💡 {sc('OPTIONS')}: • 💎 {sc('UPGRADE TO PREMIUM FOR UNLIMITED LYRICS')} • 🔗 {sc('REFER FRIENDS TO EARN POINTS')} • ⭐ {sc('USE 5 POINTS PER LYRICS SEARCH')}  {sc('CHOOSE AN OPTION BELOW')}:""",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return
    results = search_cache.get(chat_id)
    if not results or index >= len(results):
        await q.message.reply_text(f"⚠️ {sc('SONG EXPIRED. PLEASE SEARCH AGAIN')}")
        return
    video = results[index]
    status = await q.message.reply_text(
        f"🔎 {sc('SEARCHING LYRICS')}...",
        parse_mode=ParseMode.MARKDOWN
    )
    title = video["title"]
    artist = video.get("uploader", "")
    lyrics = await asyncio.get_event_loop().run_in_executor(None, fetch_lyrics, title, artist)
    if not lyrics:
        await status.delete()
        await q.message.reply_text(
            f"""🎵 {video['title'][:100]}  ❌ {sc('LYRICS NOT FOUND')}  {sc('WE TRIED MULTIPLE DATABASES BUT COULD NOT FIND THE LYRICS FOR THIS SONG')}  {sc('TRY A DIFFERENT SONG OR CHECK BACK LATER')}""",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(sc("⬇️ DOWNLOAD AUDIO"), callback_data=f"download_audio_{index}")],
                [InlineKeyboardButton(sc("🔙 BACK TO RESULTS"), callback_data="page_0")]
            ])
        )
        return
    if not is_premium(user):
        use_lyrics(user_id)
    if len(lyrics) > 4000:
        lyrics = lyrics[:3997] + "..."
    await status.delete()
    await q.message.reply_text(
        f"🎵 {video['title'][:100]}  📜 {sc('LYRICS')}:  {lyrics}",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton(sc("⬇️ DOWNLOAD AUDIO"), callback_data=f"download_audio_{index}")],
            [InlineKeyboardButton(sc("🔙 BACK TO RESULTS"), callback_data="page_0")]
        ])
    )

async def more_tracks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    chat_id = q.message.chat_id
    offset = int(q.data.split("")[1])
    results = search_cache.get(chat_id)
    if not results:
        await q.edit_message_text(f"⚠️ {sc('SEARCH EXPIRED. PLEASE SEARCH AGAIN')}")
        return
    keyboard = []
    for i in range(offset, min(offset + 20, len(results))):
        r = results[i]
        duration = f" ⏱{r['duration']//60}:{r['duration']%60:02d}" if r.get('duration') else ""
        title = r['title'][:45] + "..." if len(r['title']) > 45 else r['title']
        keyboard.append([InlineKeyboardButton(f"🎵 {title}{duration}", callback_data=f"song{i}")])
    if offset + 20 < len(results):
        keyboard.append([InlineKeyboardButton(sc("➕ MORE RESULTS"), callback_data=f"more_{offset+20}")])
    keyboard.append([InlineKeyboardButton(sc("🔙 BACK TO MENU"), callback_data="back_to_menu")])
    await q.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup(keyboard))

async def page_navigation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    chat_id = q.message.chat_id
    results = search_cache.get(chat_id)
    if not results:
        await q.edit_message_text(f"⚠️ {sc('SEARCH EXPIRED')}")
        return
    keyboard = []
    for i, r in enumerate(results[:20]):
        duration = f" ⏱{r['duration']//60}:{r['duration']%60:02d}" if r.get('duration') else ""
        title = r['title'][:45] + "..." if len(r['title']) > 45 else r['title']
        keyboard.append([InlineKeyboardButton(f"🎵 {title}{duration}", callback_data=f"song_{i}")])
    if len(results) > 20:
        keyboard.append([InlineKeyboardButton(sc("➕ MORE RESULTS"), callback_data="more_20")])
    keyboard.append([InlineKeyboardButton(sc("🔙 BACK TO MENU"), callback_data="back_to_menu")])
    await q.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup(keyboard))

async def trend_song(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    video_id = q.data.split("", 1)[1]
    msg = await q.message.reply_text(f"🔎 {sc('LOADING')}...", parse_mode=ParseMode.MARKDOWN)
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
                caption = f"""
🎵 {video['title'][:100]}  👤 {sc('ARTIST')}: {video.get('uploader', 'Unknown')} ⏱ {sc('DURATION')}: {duration}  {sc('CHOOSE AN ACTION')}: """
                keyboard = [
                    [InlineKeyboardButton(sc("⬇️ DOWNLOAD AUDIO"), callback_data="download_audio_0")],
                    [InlineKeyboardButton(sc("📜 LYRICS"), callback_data="lyrics_0")],
                    [InlineKeyboardButton(sc("🔙 BACK TO MENU"), callback_data="back_to_menu")]
                ]
                await msg.edit_text(caption, parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(keyboard))
            else:
                await msg.edit_text(f"❌ {sc('SONG NOT FOUND')}")
    except Exception as e:
        logger.error(f"{sc('TREND SONG ERROR')}: {e}")
        await msg.edit_text(f"❌ {sc('ERROR')}: {str(e)[:200]}")

async def back_to_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    user_id = q.from_user.id
    text = f"🎵 {sc('WELCOME BACK')}, {q.from_user.first_name}!  {sc('WHAT WOULD YOU LIKE TO DO')}?"
    await q.edit_message_text("🎵 " + sc("MAIN MENU"), parse_mode=ParseMode.MARKDOWN)
    await q.message.reply_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=get_main_menu_keyboard(user_id))

async def handle_video_download(update: Update, context: ContextTypes.DEFAULT_TYPE, url: str = None):
    message = update.effective_message
    if not url:
        url = extract_url(message.text)
    if not url or not is_video_url(url):
        return
    platform = get_platform_name(url)
    status_message = await message.reply_text(
        f"🔎 {sc('DETECTED')}: {platform} ⏳ {sc('PREPARING YOUR VIDEO')}...",
        parse_mode=ParseMode.MARKDOWN
    )
    async with video_dl_semaphore:
        temp_directory = tempfile.mkdtemp(prefix="social_dl")
        try:
            await status_message.edit_text(
                f"📥 {sc('DOWNLOADING')} {platform} {sc('VIDEO')}... {sc('PLEASE WAIT')}",
                parse_mode=ParseMode.MARKDOWN
            )
            file_path, info = await asyncio.to_thread(download_social_video, url, temp_directory)
            if not file_path or not file_path.exists():
                raise RuntimeError(sc("NO DOWNLOADABLE VIDEO WAS PRODUCED"))
            file_size = file_path.stat().st_size
            if file_size > 100 * 1024 * 1024:
                raise RuntimeError(sc("VIDEO IS LARGER THAN 100 MB"))
            title = info.get("title") or sc("SOCIAL MEDIA VIDEO")
            await status_message.edit_text(f"📤 {sc('UPLOADING VIDEO TO TELEGRAM')}...", parse_mode=ParseMode.MARKDOWN)
            await message.chat.send_action(action=ChatAction.UPLOAD_VIDEO)
            caption = f"🎬 {title[:700]}  📦 {file_size // (1024*1024)} MB 🌐 {platform}"
            with file_path.open("rb") as video_file:
                await message.reply_video(
                    video=video_file,
                    caption=caption,
                    supports_streaming=True,
                    read_timeout=120,
                    write_timeout=120,
                    connect_timeout=30,
                    pool_timeout=30,
                )
            await status_message.delete()
        except yt_dlp.utils.DownloadError as exc:
            logger.warning(f"{sc('YT-DLP DOWNLOAD FAILED')}: {exc}")
            await status_message.edit_text(
                f"""❌ {sc('COULD NOT DOWNLOAD THAT VIDEO')}  {sc('THE POST MAY BE PRIVATE, UNAVAILABLE, AGE/LOGIN RESTRICTED, DELETED, UNSUPPORTED, OR THE PLATFORM MAY HAVE CHANGED')}""",
                parse_mode=ParseMode.MARKDOWN
            )
        except Exception as exc:
            logger.exception(sc("UNEXPECTED DOWNLOAD ERROR"))
            error_text = str(exc).lower()
            if "exceeds" in error_text or "larger than" in error_text:
                user_message = f"❌ {sc('THE VIDEO IS LARGER THAN 100 MB')}"
            else:
                user_message = f"❌ {sc('SOMETHING WENT WRONG WHILE PROCESSING THE VIDEO')}"
            await status_message.edit_text(user_message, parse_mode=ParseMode.MARKDOWN)
        finally:
            try:
                shutil.rmtree(temp_directory, ignore_errors=True)
            except:
                pass


# ============================================================
# PART 3 — ADMIN COMMANDS, ERROR HANDLER & MAIN
# ============================================================

async def premium(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        await update.message.reply_text(sc("⛔ ACCESS DENIED"))
        return
    try:
        target_user = int(context.args[0])
        days = int(context.args[1])
        expire = datetime.datetime.now() + datetime.timedelta(days=days)
        cursor.execute("UPDATE users SET premium_expire=? WHERE id=?", (expire.isoformat(), target_user))
        db.commit()
        await context.bot.send_message(
            target_user,
            f"🎉 {sc('CONGRATULATIONS')}  {sc('YOU HAVE BEEN AWARDED PREMIUM ACCESS')} 📅 {sc('EXPIRES')}: {expire.strftime('%Y-%m-%d %H:%M')}  🚀 {sc('ENJOY UNLIMITED DOWNLOADS')}"
        )
        await notify_premium_channel(context, target_user, days, sc("OWNER"))
        await update.message.reply_text(f"✅ {sc('PREMIUM GRANTED TO USER')} {target_user} {sc('FOR')} {days} {sc('DAYS')}", parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        await update.message.reply_text(f"❌ {sc('USAGE')}: /premium <user_id> <days>", parse_mode=ParseMode.MARKDOWN)

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
            f"🎉 {sc('CONGRATULATIONS')}  {sc('YOU HAVE BEEN AWARDED')} {pts} {sc('POINTS')} 💰 {sc('NEW BALANCE')}: {get_user_points(target_user)}"
        )
        await update.message.reply_text(f"✅ {sc('AWARDED')} {pts} {sc('POINTS TO USER')} {target_user}", parse_mode=ParseMode.MARKDOWN)
    except:
        await update.message.reply_text(f"❌ {sc('USAGE')}: /reward <user_id> <points>", parse_mode=ParseMode.MARKDOWN)

async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return
    cursor.execute("SELECT COUNT() FROM users")
    total_users = cursor.fetchone()[0]
    cursor.execute("SELECT COUNT(*) FROM users WHERE premium_expire > datetime('now')")
    premium_users = cursor.fetchone()[0]
    cursor.execute("SELECT SUM(total_downloads) FROM users")
    total_downloads = cursor.fetchone()[0] or 0
    cursor.execute("SELECT SUM(points) FROM users")
    total_points = cursor.fetchone()[0] or 0
    await update.message.reply_text(
        f"""📊 {sc('BOT STATISTICS')}  👥 {sc('TOTAL USERS')}: {total_users} 💎 {sc('PREMIUM USERS')}: {premium_users} ⬇️ {sc('TOTAL DOWNLOADS')}: {total_downloads} 💰 {sc('TOTAL POINTS CIRCULATING')}: {total_points}""",
        parse_mode=ParseMode.MARKDOWN
    )

async def broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return
    if not update.message.reply_to_message:
        await update.message.reply_text(f"📢 {sc('REPLY TO A MESSAGE TO BROADCAST')}")
        return
    msg = update.message.reply_to_message
    cursor.execute("SELECT id FROM users")
    users = [u[0] for u in cursor.fetchall()]
    delivered = 0
    failed = 0
    status = await update.message.reply_text(f"📢 {sc('BROADCASTING TO')} {len(users)} {sc('USERS')}...", parse_mode=ParseMode.MARKDOWN)
    for uid in users:
        try:
            if msg.text:
                await context.bot.send_message(uid, msg.text, parse_mode=msg.parse_mode)
            elif msg.photo:
                await context.bot.send_photo(uid, msg.photo[-1].file_id, caption=msg.caption, parse_mode=msg.parse_mode)
            elif msg.video:
                await context.bot.send_video(uid, msg.video.file_id, caption=msg.caption, parse_mode=msg.parse_mode)
            elif msg.audio:
                await context.bot.send_audio(uid, msg.audio.file_id, caption=msg.caption, parse_mode=msg.parse_mode)
            elif msg.document:
                await context.bot.send_document(uid, msg.document.file_id, caption=msg.caption, parse_mode=msg.parse_mode)
            delivered += 1
            if delivered % 30 == 0:
                await asyncio.sleep(0.5)
        except Exception as e:
            logger.error(f"{sc('BROADCAST FAILED FOR')} {uid}: {e}")
            failed += 1
    await status.edit_text(
        f"""📢 {sc('BROADCAST COMPLETE')}  👥 {sc('TOTAL')}: {len(users)} ✅ {sc('DELIVERED')}: {delivered} ❌ {sc('FAILED')}: {failed}""",
        parse_mode=ParseMode.MARKDOWN
    )

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.error(f"{sc('UPDATE')} {update} {sc('CAUSED ERROR')}: {context.error}")
    if update and update.effective_message:
        try:
            await update.effective_message.reply_text(
                f"""⚠️ {sc('OOPS SOMETHING WENT WRONG')}  {sc('PLEASE TRY AGAIN LATER')} {sc('IF THE PROBLEM PERSISTS CONTACT')} @Mr_Unique_Hacker002""",
                parse_mode=ParseMode.MARKDOWN
            )
        except Exception:
            pass

def main():
    print(f"""
╔══════════════════════════════════════════════════════════════╗
║           🎵 {sc('ADVANCED MUSIC BOT')} 🎵                           ║
║   {sc('CREATED BY')} ❦ ᴍʀ ᴅᴀʀᴋʜᴀᴄᴋᴇʀ                        ║
╚══════════════════════════════════════════════════════════════╝
    """)
    if not COOKIES_FILE.exists():
        print(f"⚠️ {sc('NO COOKIE FILE FOUND AT')}: {COOKIES_FILE}")
        print(f"   {sc('YT DLP FALLBACK MAY BE LIMITED')} ")
    print(f"🤖 {sc('BOT IS RUNNING')}... {sc('PRESS CTRL C TO STOP')} ")

    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("account", show_account))
    app.add_handler(CommandHandler("trending", show_trending))
    app.add_handler(CommandHandler("help", show_help))
    app.add_handler(CommandHandler("premium", premium))
    app.add_handler(CommandHandler("reward", reward))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(CommandHandler("broadcast", broadcast))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_menu_buttons))

    app.add_handler(CallbackQueryHandler(back_to_menu, pattern="^back_to_menu$"))
    app.add_handler(CallbackQueryHandler(song_info, pattern="^song_"))
    app.add_handler(CallbackQueryHandler(download_callback, pattern="^download_audio_"))
    app.add_handler(CallbackQueryHandler(lyrics_callback, pattern="^lyrics_"))
    app.add_handler(CallbackQueryHandler(more_tracks, pattern="^more_"))
    app.add_handler(CallbackQueryHandler(page_navigation, pattern="^page_"))
    app.add_handler(CallbackQueryHandler(trend_song, pattern="^trend_"))
    app.add_handler(CallbackQueryHandler(show_premium, pattern="^upgrade_now$"))
    app.add_handler(CallbackQueryHandler(show_referral, pattern="^earn_points$"))
    app.add_handler(CallbackQueryHandler(show_premium, pattern="^pay_stars$"))

    app.add_error_handler(error_handler)

    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
