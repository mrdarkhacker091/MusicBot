#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
🎵 ADVANCED MUSIC BOT - Final Production Version
Created by ❦ ᴍʀ ᴅᴀʀᴋ<\>ʜᴀᴄᴋᴇʀ
"""

import os
import sys
import time
import asyncio
import logging
import hashlib
import datetime
import re
import json
import subprocess
from pathlib import Path
from typing import Optional, List, Dict

import sqlite3
import yt_dlp
import requests

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    ReplyKeyboardMarkup, KeyboardButton,
    BotCommand, constants
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters
)
from telegram.constants import ParseMode

# ═══════════════════════════════════════════════════════════════
# CONFIGURATION
# ═══════════════════════════════════════════════════════════════

TOKEN = "8350984585:AAFSm-9J9MTrwluT1WQk6eHhPplSoBR6c0k"
OWNER_ID = 8502323501
BOT_USERNAME = "All_MusicDownloader_Bot"

DOWNLOAD_LIMIT = 5
COOLDOWN_HOURS = 24

BASE_DIR = Path(__file__).parent.resolve()
DOWNLOADS_DIR = BASE_DIR / "downloads"
COOKIES_DIR = BASE_DIR / "cookies"
THUMBNAILS_DIR = BASE_DIR / "thumbnails"

for d in [DOWNLOADS_DIR, COOKIES_DIR, THUMBNAILS_DIR]:
    d.mkdir(parents=True, exist_ok=True)

COOKIES_FILE = COOKIES_DIR / "youtube_cookies.txt"

# Logging
logging.basicConfig(
    format="%(asctime)s — %(name)s — %(levelname)s — %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════
# DATABASE
# ═══════════════════════════════════════════════════════════════

DB_PATH = BASE_DIR / "bot.db"

def init_db():
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    cursor = conn.cursor()
    cursor.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            points INTEGER DEFAULT 0,
            premium_expire TEXT,
            downloads_today INTEGER DEFAULT 0,
            total_downloads INTEGER DEFAULT 0,
            last_reset TEXT,
            referrer INTEGER,
            joined_at TEXT DEFAULT CURRENT_TIMESTAMP,
            is_banned INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS favorites (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            video_id TEXT,
            title TEXT,
            artist TEXT,
            thumbnail_url TEXT,
            added_at TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(user_id, video_id)
        );
        CREATE TABLE IF NOT EXISTS downloads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            video_id TEXT,
            title TEXT,
            downloaded_at TEXT DEFAULT CURRENT_TIMESTAMP,
            file_size INTEGER,
            duration INTEGER
        );
        CREATE TABLE IF NOT EXISTS referrals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            referrer_id INTEGER,
            referred_id INTEGER,
            points_awarded INTEGER DEFAULT 10,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(referrer_id, referred_id)
        );
    """)
    conn.commit()
    conn.close()
    logger.info("✅ Database initialized")

init_db()

def get_db():
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

# User helpers
def get_user(user_id: int):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM users WHERE id=?", (user_id,))
    user = cursor.fetchone()
    if not user:
        now = datetime.datetime.now().isoformat()
        cursor.execute("INSERT INTO users (id, last_reset, joined_at) VALUES (?, ?, ?)", (user_id, now, now))
        conn.commit()
        cursor.execute("SELECT * FROM users WHERE id=?", (user_id,))
        user = cursor.fetchone()
    conn.close()
    return user

def update_user(user_id: int, **kwargs):
    conn = get_db()
    cursor = conn.cursor()
    fields = ", ".join([f"{k}=?" for k in kwargs])
    values = list(kwargs.values()) + [user_id]
    cursor.execute(f"UPDATE users SET {fields} WHERE id=?", values)
    conn.commit()
    conn.close()

def reset_downloads_if_needed(user_id: int) -> bool:
    user = get_user(user_id)
    if not user or not user["last_reset"]:
        return False
    try:
        last_reset = datetime.datetime.fromisoformat(user["last_reset"])
        now = datetime.datetime.now()
        if (now - last_reset).total_seconds() > COOLDOWN_HOURS * 3600:
            update_user(user_id, downloads_today=0, last_reset=now.isoformat())
            return True
    except:
        pass
    return False

def is_premium(user_id: int) -> bool:
    user = get_user(user_id)
    if not user or not user["premium_expire"]:
        return False
    try:
        expire = datetime.datetime.fromisoformat(user["premium_expire"])
        return expire > datetime.datetime.now()
    except:
        return False

def get_download_limit(user_id: int) -> int:
    return 50 if is_premium(user_id) else DOWNLOAD_LIMIT

def can_download(user_id: int) -> tuple:
    reset_downloads_if_needed(user_id)
    user = get_user(user_id)
    limit = get_download_limit(user_id)
    remaining = limit - (user["downloads_today"] or 0)
    return remaining > 0, remaining

def increment_downloads(user_id: int):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("UPDATE users SET downloads_today = downloads_today + 1, total_downloads = total_downloads + 1 WHERE id=?", (user_id,))
    conn.commit()
    conn.close()

def add_points(user_id: int, points: int):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("UPDATE users SET points = points + ? WHERE id=?", (points, user_id))
    conn.commit()
    conn.close()

# ═══════════════════════════════════════════════════════════════
# REPLY KEYBOARD (Main Menu)
# ═══════════════════════════════════════════════════════════════

def get_main_menu_keyboard(user_id: int) -> ReplyKeyboardMarkup:
    user = get_user(user_id)
    points = user["points"] if user else 0
    downloads = user["downloads_today"] if user else 0
    limit = get_download_limit(user_id)
    premium_text = "💎 Premium" if is_premium(user_id) else "⭐ Upgrade"
    keyboard = [
        ["🎵 Search Music", "🔥 Trending"],
        [f"📊 Account ({points} pts)", f"⬇️ {downloads}/{limit}"],
        ["🔗 Referral", "🤖 Other Bots"],
        [premium_text, "❓ Help"],
        ["📞 Contact"]
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

# ═══════════════════════════════════════════════════════════════
# YT-DLP – MODERN, WITH COOKIES AND VIDEO SUPPORT
# ═══════════════════════════════════════════════════════════════

def get_ydl_opts(output_path: str = None, audio_only: bool = True) -> dict:
    opts = {
        "quiet": False,
        "no_warnings": False,
        "noplaylist": True,
        "geo_bypass": True,
        "ignoreerrors": False,
        "no_check_certificate": True,
        "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
        "extractor_args": {
            "youtube": {
                "player_client": ["android", "web"],
                "skip": ["hls", "dash"]
            }
        }
    }

    if COOKIES_FILE.exists():
        opts["cookies"] = str(COOKIES_FILE)

    if audio_only:
        opts["format"] = "bestaudio[ext=m4a]/bestaudio/best"
        opts["postprocessors"] = [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "192",
        }]
        if output_path:
            opts["outtmpl"] = output_path
        else:
            opts["outtmpl"] = str(DOWNLOADS_DIR / "%(id)s_%(title)s")
    else:
        opts["format"] = "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best"
        if output_path:
            opts["outtmpl"] = output_path
        else:
            opts["outtmpl"] = str(DOWNLOADS_DIR / "%(id)s_%(title)s")

    return opts

def search_music(query: str, max_results: int = 50) -> List[Dict]:
    try:
        opts = get_ydl_opts(audio_only=False)
        opts["extract_flat"] = True
        opts["playlistend"] = max_results
        
        with yt_dlp.YoutubeDL(opts) as ydl:
            result = ydl.extract_info(f"ytsearch{max_results}:{query}", download=False)
            entries = result.get("entries", []) if result else []
            
            valid = []
            for entry in entries:
                if entry and entry.get("id") and entry.get("title"):
                    duration = entry.get("duration", 0)
                    if duration:
                        duration = int(duration)
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

async def download_media_async(video_id: str, title: str, audio_only: bool = True) -> Optional[Path]:
    """Download media from YouTube. Returns Path to downloaded file or None on failure."""
    loop = asyncio.get_event_loop()
    file_id = hashlib.md5(f"{video_id}{time.time()}".encode()).hexdigest()[:12]

    def _download():
        try:
            output_path = str(DOWNLOADS_DIR / file_id)
            opts = get_ydl_opts(output_path=output_path, audio_only=audio_only)
            url = f"https://youtube.com/watch?v={video_id}"
            
            logger.info(f"Starting download: {url}")
            
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=True)
                logger.info(f"Download completed for {video_id}")
            
            # Search for the actual downloaded file
            search_patterns = [
                f"{file_id}.mp3",
                f"{file_id}.m4a",
                f"{file_id}.mp4",
                f"{file_id}.mkv",
                f"{file_id}.webm",
                f"{file_id}.*"
            ]
            
            for pattern in search_patterns:
                matches = list(DOWNLOADS_DIR.glob(pattern))
                if matches:
                    result_file = matches[0]
                    if result_file.exists() and result_file.stat().st_size > 0:
                        logger.info(f"Found downloaded file: {result_file}")
                        return result_file
            
            logger.error(f"No file found after download for video_id={video_id}")
            return None
            
        except Exception as e:
            logger.error(f"Download error in _download: {type(e).__name__}: {e}")
            raise

    try:
        result = await asyncio.wait_for(loop.run_in_executor(None, _download), timeout=180.0)
        return result
    except asyncio.TimeoutError:
        logger.error(f"Download timeout for {video_id}")
        return None
    except Exception as e:
        logger.error(f"Download async error: {type(e).__name__}: {e}")
        return None

async def download_thumbnail(thumbnail_url: str, video_id: str) -> Optional[Path]:
    if not thumbnail_url:
        return None
    thumb_path = THUMBNAILS_DIR / f"{video_id}.jpg"
    if thumb_path.exists():
        return thumb_path
    try:
        r = requests.get(thumbnail_url, timeout=10)
        if r.status_code == 200:
            thumb_path.write_bytes(r.content)
            return thumb_path
    except:
        pass
    return None

# ═══════════════════════════════════════════════════════════════
# LYRICS – MULTIPLE SOURCES
# ═══════════════════════════════════════════════════════════════

def fetch_lyrics(title: str, artist: str = "") -> str:
    title = title.strip()
    artist = artist.strip()
    
    if not artist and " - " in title:
        parts = title.split(" - ", 1)
        artist = parts[0].strip()
        title = parts[1].strip()

    # Try lyrics.ovh
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

    # LRCLIB
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

    # Genius scraping
    try:
        query = f"{artist} {title}".strip()
        search_url = f"https://genius.com/search?q={requests.utils.quote(query)}"
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
        r = requests.get(search_url, headers=headers, timeout=8)
        if r.status_code == 200:
            match = re.search(r'<a[^>]+href="([^"]+)"[^>]*data-search-result="true"', r.text)
            if match:
                song_url = match.group(1)
                if not song_url.startswith("http"):
                    song_url = "https://genius.com" + song_url
                r2 = requests.get(song_url, headers=headers, timeout=8)
                if r2.status_code == 200:
                    lyrics_match = re.search(r'<div[^>]*data-lyrics-container="true"[^>]*>(.*?)</div>', r2.text, re.DOTALL)
                    if lyrics_match:
                        raw = lyrics_match.group(1)
                        raw = re.sub(r'<[^>]+>', '', raw)
                        raw = re.sub(r'\n{3,}', '\n\n', raw).strip()
                        if raw:
                            return raw
    except:
        pass

    return f"🎵 *{title}* — *{artist}*\n\n_Lyrics not found in our databases._"

# ═══════════════════════════════════════════════════════════════
# SEARCH CACHE
# ═══════════════════════════════════════════════════════════════

search_cache = {}

# ═══════════════════════════════════════════════════════════════
# HANDLERS
# ═══════════════════════════════════════════════════════════════

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_id = user.id
    update_user(user_id, username=user.username, first_name=user.first_name)

    # referral
    if context.args:
        try:
            ref = int(context.args[0])
            if ref != user_id:
                user_data = get_user(user_id)
                if user_data and user_data["referrer"] is None:
                    conn = get_db()
                    cursor = conn.cursor()
                    cursor.execute("SELECT 1 FROM users WHERE id=?", (ref,))
                    if cursor.fetchone():
                        cursor.execute("UPDATE users SET referrer=? WHERE id=?", (ref, user_id))
                        cursor.execute("UPDATE users SET points=points+10 WHERE id=?", (ref,))
                        cursor.execute("INSERT INTO referrals (referrer_id, referred_id, points_awarded) VALUES (?, ?, 10)", (ref, user_id))
                        conn.commit()
                        try:
                            await context.bot.send_message(ref, f"🎉 *Referral Bonus!*\n\nUser [{user.first_name}](tg://user?id={user_id}) joined using your link!\n✅ You earned *+10 points*!", parse_mode=ParseMode.MARKDOWN)
                        except:
                            pass
                    conn.close()
        except:
            pass

    text = (
        f"🎵 *Welcome to Advanced Music Bot!*\n\n"
        f"👋 Hi *{user.first_name}*!\n\n"
        f"I can help you find and download music from YouTube.\n\n"
        f"✨ *Features:*\n"
        f"• 🎵 Search any song\n"
        f"• ⬇️ Download MP3 audio\n"
        f"• 🎬 Download video (MP4)\n"
        f"• 📜 Get song lyrics\n"
        f"• 🔗 Refer friends & earn points\n"
        f"• 💎 Premium for unlimited downloads\n\n"
        f"*Use the buttons below to get started!*"
    )
    await update.message.reply_text(
        text,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=get_main_menu_keyboard(user_id)
    )

async def handle_menu_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = update.message.text

    if text == "🎵 Search Music":
        await update.message.reply_text(
            "🎵 *Search Music*\n\nSend me a song name or artist to search!\n\n_Example: \"Calm Down Rema\"_",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=get_main_menu_keyboard(user_id)
        )
    elif text == "🔥 Trending":
        await show_trending(update, context)
    elif text.startswith("📊 Account"):
        await show_account(update, context)
    elif text.startswith("⬇️"):
        user = get_user(user_id)
        remaining = get_download_limit(user_id) - (user["downloads_today"] or 0)
        await update.message.reply_text(
            f"📊 *Download Usage*\n\n"
            f"Today: *{user['downloads_today'] or 0} / {get_download_limit(user_id)}*\n"
            f"Remaining: *{remaining}*\n"
            f"Premium: *{'✅ Active' if is_premium(user_id) else '❌ Not active'}*",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=get_main_menu_keyboard(user_id)
        )
    elif text == "🔗 Referral":
        await show_referral(update, context)
    elif text == "🤖 Other Bots":
        await show_other_bots(update, context)
    elif text == "⭐ Upgrade" or text == "💎 Premium":
        await show_premium(update, context)
    elif text == "❓ Help":
        await show_help(update, context)
    elif text == "📞 Contact":
        await show_contact(update, context)
    else:
        # treat as search query
        await song_search(update, context)

async def show_trending(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    keyboard = [
        [InlineKeyboardButton("🔥 Calm Down - Rema", callback_data="trend_CQL_2lDzBXs")],
        [InlineKeyboardButton("🔥 Last Last - Burna Boy", callback_data="trend_4NRXx6U8ABQ")],
        [InlineKeyboardButton("🔥 Unavailable - Davido", callback_data="trend_fG4d4h14Gec")],
        [InlineKeyboardButton("🔥 Water - Tyla", callback_data="trend_XoiOOiuH8iI")],
        [InlineKeyboardButton("🔥 Kill Bill - SZA", callback_data="trend_MSRc5J2Atrg")],
        [InlineKeyboardButton("🔥 Flowers - Miley Cyrus", callback_data="trend_G7KNmW9a75Y")],
    ]
    await update.message.reply_text(
        "🔥 *Trending Songs*\n\nClick any song to download:",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def show_account(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user = get_user(user_id)
    reset_downloads_if_needed(user_id)
    premium_status = "💎 Active" if is_premium(user_id) else "❌ Inactive"
    text = (
        f"👤 *Your Account*\n\n"
        f"💰 Points: *{user['points']}*\n"
        f"⬇️ Downloads Today: *{user['downloads_today'] or 0}/{get_download_limit(user_id)}*\n"
        f"📊 Total Downloads: *{user['total_downloads']}*\n"
        f"💎 Premium: *{premium_status}*\n\n"
        f"_Invite friends and earn 10 points each!_"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=get_main_menu_keyboard(user_id))

async def show_referral(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    link = f"https://t.me/{BOT_USERNAME}?start={user_id}"
    text = (
        f"🔗 *Your Referral Link*\n\n"
        f"Share this link with your friends:\n\n"
        f"`{link}`\n\n"
        f"✨ *How it works:*\n"
        f"• Each friend who joins gives you *+10 points*!\n"
        f"• Use points to unlock premium features!\n\n"
        f"_Tap and hold to copy the link_"
    )
    await update.message.reply_text(
        text,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("📤 Share Link", url=f"https://t.me/share/url?url={link}&text=🎵%20Get%20music%20for%20free!")],
        ])
    )

async def show_other_bots(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "🤖 *Our Other Bots*\n\n"
        "*Online 🟢*\n"
        "• @Mrdarkhacker_appeal_bot\n"
        "• @Menstrual_Ai_Bot\n"
        "• @Stylishname_generator_bot\n"
        "• @Dark_Hacker_Reaction_Bot\n\n"
        "*Offline 🔴*\n"
        "• @Dark_Web_Scrapping_Bot\n"
        "• @Multipurposetele_Bot\n\n"
        "_More bots coming soon!_"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=get_main_menu_keyboard(update.effective_user.id))

async def show_premium(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if is_premium(user_id):
        user = get_user(user_id)
        expire = "N/A"
        if user["premium_expire"]:
            try:
                expire = datetime.datetime.fromisoformat(user["premium_expire"]).strftime("%Y-%m-%d")
            except:
                pass
        text = f"💎 *Premium Active*\n\n📅 Expires: `{expire}`\n⬇️ Daily Limit: *50 downloads*\n\nThank you for supporting the bot! 🙏"
    else:
        text = (
            "💎 *Upgrade to Premium*\n\n"
            "*✨ Benefits:*\n"
            "• ⬇️ 50 downloads per day\n"
            "• 🚀 Priority download speed\n"
            "• 🎵 HD audio quality\n"
            "• 🎬 Video downloads included\n"
            "• 🚫 No cooldown periods\n\n"
            "📞 *Contact @Mr_Unique_Hacker001 to upgrade!*"
        )
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=get_main_menu_keyboard(user_id))

async def show_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "❓ *Help Guide*\n\n"
        "*How to use:*\n"
        "1. Send a song name or artist\n"
        "2. Click on a result\n"
        "3. Choose Download Audio, Download Video, or Lyrics\n\n"
        "*Commands:*\n"
        "/start - Start the bot\n"
        "/account - View your profile\n"
        "/trending - Trending songs\n"
        "/help - This help message\n\n"
        "*Premium Benefits:*\n"
        "• 50 downloads per day\n"
        "• No cooldown\n"
        "• HD quality audio & video\n\n"
        "_Contact @Mr_Unique_Hacker001 for premium_"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=get_main_menu_keyboard(update.effective_user.id))

async def show_contact(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("📩 Telegram", url="https://t.me/Mr_Unique_Hacker001")],
        [InlineKeyboardButton("📱 WhatsApp", url="https://wa.me/2349123578884")],
    ]
    await update.message.reply_text(
        "📞 *Contact Us*\n\nReach out to us on:",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

# ─── SONG SEARCH ────────────────────────────────────────────────

async def song_search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    query = update.message.text.strip()
    
    if len(query) < 2:
        await update.message.reply_text("⚠️ Please enter at least 2 characters to search.", reply_markup=get_main_menu_keyboard(user_id))
        return

    msg = await update.message.reply_text("🔎 *Searching...*", parse_mode=ParseMode.MARKDOWN)
    results = await asyncio.get_event_loop().run_in_executor(None, search_music, query)
    
    if not results:
        await msg.edit_text(
            "❌ *No results found.*\n\n💡 Try different keywords or artist name.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back to Menu", callback_data="back_to_menu")]])
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
        keyboard.append([InlineKeyboardButton("➕ More Results", callback_data=f"more_20")])
    keyboard.append([InlineKeyboardButton("🔙 Back to Menu", callback_data="back_to_menu")])

    await msg.edit_text(
        f"🎵 *Results for:* `{query[:50]}`\n\nSelect a song:",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

# ─── CALLBACKS ──────────────────────────────────────────────────

async def back_to_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    user_id = q.from_user.id
    await q.edit_message_text(
        "🎵 *Main Menu*",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=None
    )
    text = f"🎵 *Welcome back, {q.from_user.first_name}!*\n\nWhat would you like to do?"
    await q.message.reply_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=get_main_menu_keyboard(user_id))

async def song_info_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    chat_id = q.message.chat_id
    user_id = q.from_user.id
    index = int(q.data.split("_")[1])
    results = search_cache.get(chat_id)

    if not results or index >= len(results):
        await q.edit_message_text("⚠️ Song expired. Please search again.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="back_to_menu")]]))
        return

    video = results[index]
    duration = f"{video['duration']//60}:{video['duration']%60:02d}" if video.get('duration') else "N/A"
    caption = (
        f"🎵 *{video['title'][:100]}*\n\n"
        f"👤 Artist: `{video.get('uploader', 'Unknown')}`\n"
        f"⏱ Duration: `{duration}`\n"
        f"🔗 [Watch on YouTube]({video['url']})\n\n"
        f"Choose an action:"
    )
    keyboard = [
        [InlineKeyboardButton("⬇️ Download Audio", callback_data=f"download_audio_{index}")],
        [InlineKeyboardButton("🎬 Download Video", callback_data=f"download_video_{index}")],
        [InlineKeyboardButton("📜 Lyrics", callback_data=f"lyrics_{index}")],
        [InlineKeyboardButton("🔙 Back to Results", callback_data=f"page_0")]
    ]
    await q.edit_message_text(caption, parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(keyboard))

async def download_callback(update: Update, context: ContextTypes.DEFAULT_TYPE, audio_only: bool):
    q = update.callback_query
    await q.answer("⬇️ Starting download...")
    user_id = q.from_user.id
    chat_id = q.message.chat_id
    index = int(q.data.split("_")[2])

    can_dl, remaining = can_download(user_id)
    if not can_dl:
        await q.message.reply_text("⛔ *Download Limit Reached!*\n\nUpgrade to Premium for unlimited downloads.", parse_mode=ParseMode.MARKDOWN)
        return

    results = search_cache.get(chat_id)
    if not results or index >= len(results):
        await q.message.reply_text("⚠️ Song expired. Please search again.")
        return

    video = results[index]
    status_msg = await q.message.reply_text(f"⬇️ *{'Audio' if audio_only else 'Video'} download in progress...* Please wait.", parse_mode=ParseMode.MARKDOWN)

    try:
        file_path = await download_media_async(video["id"], video["title"], audio_only=audio_only)
        
        if not file_path or not file_path.exists():
            await status_msg.edit_text("❌ *Download failed.*\n\nPlease try again or contact support.", parse_mode=ParseMode.MARKDOWN)
            return

        await status_msg.edit_text("📤 *Uploading...*", parse_mode=ParseMode.MARKDOWN)

        if audio_only:
            with open(file_path, "rb") as f:
                await q.message.reply_audio(
                    audio=f,
                    title=video["title"][:100],
                    performer=video.get("uploader", "Unknown")[:100],
                    duration=video.get("duration", 0),
                    caption=f"🎵 *{video['title'][:100]}*\n\n✅ Audio downloaded successfully!",
                    parse_mode=ParseMode.MARKDOWN
                )
        else:
            thumb_path = await download_thumbnail(video.get("thumbnail", ""), video["id"])
            with open(file_path, "rb") as f:
                await q.message.reply_video(
                    video=f,
                    caption=f"🎬 *{video['title'][:100]}*\n\n✅ Video downloaded successfully!",
                    parse_mode=ParseMode.MARKDOWN,
                    thumbnail=open(thumb_path, "rb") if thumb_path and thumb_path.exists() else None,
                    supports_streaming=True
                )

        file_path.unlink(missing_ok=True)
        increment_downloads(user_id)
        add_points(user_id, 1)
        await status_msg.delete()

    except Exception as e:
        logger.error(f"Download error: {e}")
        await status_msg.edit_text(f"❌ *Download Error*\n\n`{str(e)[:200]}`", parse_mode=ParseMode.MARKDOWN)

async def lyrics_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer("📜 Fetching lyrics...")
    chat_id = q.message.chat_id
    index = int(q.data.split("_")[1])
    results = search_cache.get(chat_id)

    if not results or index >= len(results):
        await q.message.reply_text("⚠️ Song expired. Please search again.")
        return

    video = results[index]
    status = await q.message.reply_text("🔎 *Searching lyrics...*", parse_mode=ParseMode.MARKDOWN)
    title = video["title"]
    artist = video.get("uploader", "")
    lyrics = await asyncio.get_event_loop().run_in_executor(None, fetch_lyrics, title, artist)

    if len(lyrics) > 4000:
        lyrics = lyrics[:3997] + "..."

    await status.delete()
    await q.message.reply_text(
        f"🎵 *{video['title'][:100]}*\n\n📜 *Lyrics:*\n\n```\n{lyrics}\n```",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("⬇️ Download Audio", callback_data=f"download_audio_{index}")],
            [InlineKeyboardButton("🎬 Download Video", callback_data=f"download_video_{index}")],
            [InlineKeyboardButton("🔙 Back to Results", callback_data=f"page_0")]
        ])
    )

async def more_tracks_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    chat_id = q.message.chat_id
    offset = int(q.data.split("_")[1])
    results = search_cache.get(chat_id)

    if not results:
        await q.edit_message_text("⚠️ Search expired. Please search again.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="back_to_menu")]]))
        return

    keyboard = []
    for i in range(offset, min(offset + 20, len(results))):
        r = results[i]
        duration = f" ⏱{r['duration']//60}:{r['duration']%60:02d}" if r.get('duration') else ""
        title = r['title'][:45] + "..." if len(r['title']) > 45 else r['title']
        keyboard.append([InlineKeyboardButton(f"🎵 {title}{duration}", callback_data=f"song_{i}")])

    if offset + 20 < len(results):
        keyboard.append([InlineKeyboardButton("➕ More Results", callback_data=f"more_{offset+20}")])
    keyboard.append([InlineKeyboardButton("🔙 Back to Menu", callback_data="back_to_menu")])
    await q.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup(keyboard))

async def page_navigation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    chat_id = q.message.chat_id
    results = search_cache.get(chat_id)

    if not results:
        await q.edit_message_text("⚠️ Search expired.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="back_to_menu")]]))
        return

    keyboard = []
    for i, r in enumerate(results[:20]):
        duration = f" ⏱{r['duration']//60}:{r['duration']%60:02d}" if r.get('duration') else ""
        title = r['title'][:45] + "..." if len(r['title']) > 45 else r['title']
        keyboard.append([InlineKeyboardButton(f"🎵 {title}{duration}", callback_data=f"song_{i}")])

    if len(results) > 20:
        keyboard.append([InlineKeyboardButton("➕ More Results", callback_data="more_20")])
    keyboard.append([InlineKeyboardButton("🔙 Back to Menu", callback_data="back_to_menu")])
    await q.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup(keyboard))

async def trend_song_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    video_id = q.data.split("_", 1)[1]
    msg = await q.message.reply_text("🔎 *Loading...*", parse_mode=ParseMode.MARKDOWN)

    try:
        opts = get_ydl_opts(audio_only=False)
        opts["extract_flat"] = True
        
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
                    f"🎵 *{video['title'][:100]}*\n\n"
                    f"👤 Artist: `{video.get('uploader', 'Unknown')}`\n"
                    f"⏱ Duration: `{duration}`\n\n"
                    f"Choose an action:"
                )
                keyboard = [
                    [InlineKeyboardButton("⬇️ Download Audio", callback_data="download_audio_0")],
                    [InlineKeyboardButton("🎬 Download Video", callback_data="download_video_0")],
                    [InlineKeyboardButton("📜 Lyrics", callback_data="lyrics_0")],
                    [InlineKeyboardButton("🔙 Back to Trending", callback_data="back_to_menu")]
                ]
                await msg.edit_text(caption, parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(keyboard))
            else:
                await msg.edit_text("❌ Song not found.")
    except Exception as e:
        logger.error(f"Trend song error: {e}")
        await msg.edit_text(f"❌ Error: {str(e)[:200]}")

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.error(f"Update {update} caused error: {context.error}")
    if update and update.effective_message:
        try:
            await update.effective_message.reply_text(
                "⚠️ *Oops! Something went wrong.*\n\nPlease try again or contact support.",
                parse_mode=ParseMode.MARKDOWN
            )
        except:
            pass

# ═══════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════

async def setup_commands(application: Application):
    await application.bot.set_my_commands([
        BotCommand("start", "🚀 Start the bot"),
        BotCommand("account", "👤 View your profile"),
        BotCommand("trending", "🔥 Trending songs"),
        BotCommand("help", "❓ Help guide"),
    ])

def main():
    print("""
╔══════════════════════════════════════════════════════════════╗
║           🎵 ADVANCED MUSIC BOT 🎵                           ║
║   Created by ❦ ᴍʀ ᴅᴀʀᴋ<\>ʜᴀᴄᴋᴇʀ                        ║
╚══════════════════════════════════════════════════════════════╝
    """)

    if not COOKIES_FILE.exists():
        print("⚠️ No cookie file found. YouTube downloads may be blocked.")
        print(f"   Export cookies from browser to: {COOKIES_FILE}\n")

    print("🤖 Bot is running... Press Ctrl+C to stop.\n")

    app = Application.builder().token(TOKEN).build()
    app.post_init = setup_commands

    # Commands
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("account", show_account))
    app.add_handler(CommandHandler("trending", show_trending))
    app.add_handler(CommandHandler("help", show_help))

    # Reply keyboard handler (text messages that are not commands)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_menu_buttons))

    # Callback handlers (inline)
    app.add_handler(CallbackQueryHandler(back_to_menu, pattern="^back_to_menu$"))
    app.add_handler(CallbackQueryHandler(song_info_callback, pattern="^song_"))
    app.add_handler(CallbackQueryHandler(lambda u,c: download_callback(u,c,True), pattern="^download_audio_"))
    app.add_handler(CallbackQueryHandler(lambda u,c: download_callback(u,c,False), pattern="^download_video_"))
    app.add_handler(CallbackQueryHandler(lyrics_callback, pattern="^lyrics_"))
    app.add_handler(CallbackQueryHandler(more_tracks_callback, pattern="^more_"))
    app.add_handler(CallbackQueryHandler(page_navigation, pattern="^page_"))
    app.add_handler(CallbackQueryHandler(trend_song_callback, pattern="^trend_"))

    app.add_error_handler(error_handler)
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
