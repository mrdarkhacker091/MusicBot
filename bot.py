#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
🎵 ADVANCED MUSIC BOT - FIXED
Created by ❦ ᴍʀ ᴅᴀʀᴋ<\\>ʜᴀᴄᴋᴇʀ 🫟
"""

import sqlite3
import yt_dlp
import requests
import os
import datetime
import uuid
import hashlib
import time
import asyncio
import re
from pathlib import Path

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters
)
from telegram.constants import ParseMode

TOKEN = "8350984585:AAFSm-9J9MTrwluT1WQk6eHhPplSoBR6c0k"
OWNER_ID = 8854936887
BOT_USERNAME = "All_MusicDownloader_Bot"

DOWNLOAD_LIMIT = 5
COOLDOWN_HOURS = 24

BASE_DIR = Path(__file__).parent.resolve()
DOWNLOADS_DIR = BASE_DIR / "downloads"
COOKIES_DIR = BASE_DIR / "cookies"

for d in [DOWNLOADS_DIR, COOKIES_DIR]:
    d.mkdir(parents=True, exist_ok=True)

COOKIES_FILE = COOKIES_DIR / "cookies.txt"

# DATABASE
db = sqlite3.connect("bot.db", check_same_thread=False)
cursor = db.cursor()
cursor.execute("""
CREATE TABLE IF NOT EXISTS users(
id INTEGER PRIMARY KEY,
points INTEGER DEFAULT 0,
premium_expire TEXT,
downloads INTEGER DEFAULT 0,
last_reset TEXT,
referrer INTEGER
)
""")
db.commit()

search_cache = {}

# ===== MAIN MENU REPLY KEYBOARD =====
def get_main_menu_keyboard(user_id: int) -> ReplyKeyboardMarkup:
    user = get_user(user_id)
    points = user[1] if user else 0
    downloads = user[3] if user else 0
    premium_text = "💎 Premium" if is_premium(user) else "⭐ Upgrade"

    keyboard = [
        ["🎵 Search Music", "🔥 Trending"],
        [f"📊 Account ({points} pts)", f"⬇️ {downloads}/{DOWNLOAD_LIMIT}"],
        ["🔗 Referral", "🤖 Other Bots"],
        [premium_text, "❓ Help"],
        ["📞 Contact"]
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

# ===== USER FUNCTIONS =====
def get_user(user_id):
    cursor.execute("SELECT * FROM users WHERE id=?", (user_id,))
    user = cursor.fetchone()
    if not user:
        now = datetime.datetime.now().isoformat()
        cursor.execute("INSERT INTO users(id,last_reset) VALUES(?,?)", (user_id, now))
        db.commit()
        cursor.execute("SELECT * FROM users WHERE id=?", (user_id,))
        user = cursor.fetchone()
    return user

def reset_downloads(user):
    if not user or not user[4]:
        return False
    try:
        last_reset = datetime.datetime.fromisoformat(user[4])
        now = datetime.datetime.now()
        diff = now - last_reset
        if diff.total_seconds() > COOLDOWN_HOURS * 3600:
            cursor.execute("UPDATE users SET downloads=0,last_reset=? WHERE id=?", (now.isoformat(), user[0]))
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
    cursor.execute("UPDATE users SET downloads=downloads+1 WHERE id=?", (user_id,))
    db.commit()

def add_points(user_id, points):
    cursor.execute("UPDATE users SET points=points+? WHERE id=?", (points, user_id))
    db.commit()

# ===== YT-DLP WITH COOKIES AND FIXED OPTIONS =====
def get_ydl_opts(output_path=None, audio_only=True):
    opts = {
        "quiet": True,
        "no_warnings": True,
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
        opts["outtmpl"] = output_path or "%(title)s.%(ext)s"
    else:
        opts["format"] = "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best"
        opts["outtmpl"] = output_path or "%(title)s.%(ext)s"

    return opts

def search_music(query, max_results=50):
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
        print(f"Search error: {e}")
        return []

async def download_media_async(video_id, title, audio_only=True):
    loop = asyncio.get_event_loop()
    file_id = hashlib.md5(f"{video_id}{time.time()}".encode()).hexdigest()[:12]
    ext = "mp3" if audio_only else "mp4"
    output_path = str(DOWNLOADS_DIR / f"{file_id}.%(ext)s")

    def _download():
        try:
            opts = get_ydl_opts(output_path=output_path, audio_only=audio_only)
            url = f"https://youtube.com/watch?v={video_id}"
            with yt_dlp.YoutubeDL(opts) as ydl:
                ydl.download([url])
            for f in DOWNLOADS_DIR.glob(f"{file_id}.*"):
                if audio_only:
                    if f.suffix in [".mp3", ".m4a", ".webm", ".opus"]:
                        return f
                else:
                    if f.suffix in [".mp4", ".mkv", ".webm"]:
                        return f
            return None
        except Exception as e:
            print(f"Download error: {e}")
            raise

    try:
        result = await asyncio.wait_for(loop.run_in_executor(None, _download), timeout=120.0)
        return result
    except asyncio.TimeoutError:
        print("Download timed out")
        return None
    except Exception as e:
        print(f"Download crashed: {e}")
        return None

# ===== LYRICS - MULTIPLE SOURCES =====
def fetch_lyrics(title, artist=""):
    title = title.strip()
    artist = artist.strip()
    if not artist and " - " in title:
        parts = title.split(" - ", 1)
        artist = parts[0].strip()
        title = parts[1].strip()

    # 1) lyrics.ovh
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

    # 2) LRCLIB
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

    # 3) Genius scraping
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

    # 4) AZLyrics (fallback)
    try:
        query = f"{artist} {title}".strip().lower().replace(" ", "")
        url = f"https://www.azlyrics.com/lyrics/{query}.html"
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
        r = requests.get(url, headers=headers, timeout=8)
        if r.status_code == 200:
            match = re.search(r'<div[^>]*class="[^"]*lyricsh[^"]*"[^>]*>(.*?)</div>', r.text, re.DOTALL)
            if match:
                raw = match.group(1)
                raw = re.sub(r'<[^>]+>', '', raw)
                raw = re.sub(r'\n{3,}', '\n\n', raw).strip()
                if raw:
                    return raw
    except:
        pass

    # 5) Musixmatch (via API - you need API key)
    # Uncomment if you have an API key
    # try:
    #     api_key = "YOUR_MUSIXMATCH_API_KEY"
    #     url = f"https://api.musixmatch.com/ws/1.1/matcher.lyrics.get?q_track={requests.utils.quote(title)}&q_artist={requests.utils.quote(artist)}&apikey={api_key}"
    #     r = requests.get(url, timeout=8)
    #     if r.status_code == 200:
    #         data = r.json()
    #         if data.get("message", {}).get("body", {}).get("lyrics", {}).get("lyrics_body"):
    #             return data["message"]["body"]["lyrics"]["lyrics_body"]
    # except:
    #     pass

    return f"🎵 *{title}* — *{artist}*\n\n_Lyrics not found in our databases._"

# ===== START COMMAND =====
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    ref = None
    if context.args:
        try:
            ref = int(context.args[0])
        except:
            pass

    user = get_user(user_id)
    if user and user[5] is None and ref and ref != user_id:
        cursor.execute("UPDATE users SET referrer=? WHERE id=?", (ref, user_id))
        cursor.execute("UPDATE users SET points=points+10 WHERE id=?", (ref,))
        db.commit()
        try:
            await context.bot.send_message(
                ref,
                f"🎉 *Referral Bonus!*\n\nUser [{update.effective_user.first_name}](tg://user?id={user_id}) joined using your link!\n✅ You earned *+10 points*!",
                parse_mode=ParseMode.MARKDOWN
            )
        except:
            pass

    text = f"""
🎵 *Welcome to Advanced Music Bot!*

👋 Hi *{update.effective_user.first_name}*!

I can help you find and download music from YouTube.

✨ *Features:*
• 🎵 Search any song
• ⬇️ Download MP3 audio
• 🎬 Download video (MP4)
• 📜 Get song lyrics
• 🔗 Refer friends & earn points
• 💎 Premium for unlimited downloads

*Use the buttons below to get started!*
"""
    await update.message.reply_text(
        text,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=get_main_menu_keyboard(user_id)
    )

# ===== MENU BUTTON HANDLER =====
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
        remaining = DOWNLOAD_LIMIT - (user[3] if user else 0)
        await update.message.reply_text(
            f"📊 *Download Usage*\n\n"
            f"Today: *{user[3] if user else 0} / {DOWNLOAD_LIMIT}*\n"
            f"Remaining: *{remaining}*\n"
            f"Premium: *{'✅ Active' if is_premium(user) else '❌ Not active'}*",
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
        await song_search(update, context)

# ===== ACCOUNT =====
async def show_account(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user = get_user(user_id)
    reset_downloads(user)
    points = user[1] if user else 0
    downloads = user[3] if user else 0
    premium_status = "💎 Active" if is_premium(user) else "❌ Inactive"

    text = f"""
👤 *Your Account*

💰 Points: *{points}*
⬇️ Downloads Today: *{downloads}/{DOWNLOAD_LIMIT}*
📊 Total Downloads: *{user[3] if user else 0}*
💎 Premium: *{premium_status}*

_Invite friends and earn 10 points each!_
"""
    await update.message.reply_text(
        text,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=get_main_menu_keyboard(user_id)
    )

# ===== REFERRAL =====
async def show_referral(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    link = f"https://t.me/{BOT_USERNAME}?start={user_id}"

    text = f"""
🔗 *Your Referral Link*

Share this link with your friends:

`{link}`

✨ *How it works:*
• Each friend who joins gives you *+10 points*!
• Use points to unlock premium features!

_Tap and hold to copy the link_
"""
    await update.message.reply_text(
        text,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("📤 Share Link", url=f"https://t.me/share/url?url={link}&text=🎵%20Get%20music%20for%20free!")]
        ])
    )

# ===== OTHER BOTS =====
async def show_other_bots(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = """
🤖 *Our Other Bots*

*Online 🟢*
• @Mrdarkhacker_appeal_bot
• @Menstrual_Ai_Bot
• @Stylishname_generator_bot
• @Dark_Hacker_Reaction_Bot

*Offline 🔴*
• @Dark_Web_Scrapping_Bot
• @Multipurposetele_Bot

_More bots coming soon!_
"""
    await update.message.reply_text(
        text,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=get_main_menu_keyboard(update.effective_user.id)
    )

# ===== PREMIUM =====
async def show_premium(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user = get_user(user_id)

    if is_premium(user):
        expire = "N/A"
        if user[2]:
            try:
                expire = datetime.datetime.fromisoformat(user[2]).strftime("%Y-%m-%d")
            except:
                pass
        text = f"💎 *Premium Active*\n\n📅 Expires: `{expire}`\n⬇️ Daily Limit: *50 downloads*\n\nThank you for supporting the bot! 🙏"
    else:
        text = """
💎 *Upgrade to Premium*

*✨ Benefits:*
• ⬇️ 50 downloads per day
• 🚀 Priority download speed
• 🎵 HD audio quality
• 🎬 Video downloads included
• 🚫 No cooldown periods

📞 *Contact @Mr_Unique_Hacker001 to upgrade!*
"""
    await update.message.reply_text(
        text,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=get_main_menu_keyboard(user_id)
    )

# ===== HELP =====
async def show_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = """
❓ *Help Guide*

*How to use:*
1. Send a song name or artist
2. Click on a result
3. Choose Download Audio, Download Video, or Lyrics

*Commands:*
/start - Start the bot
/account - View your profile
/trending - Trending songs
/help - This help message

*Premium Benefits:*
• 50 downloads per day
• No cooldown
• HD quality audio & video

_Contact @Mr_Unique_Hacker001 for premium_
"""
    await update.message.reply_text(
        text,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=get_main_menu_keyboard(update.effective_user.id)
    )

# ===== CONTACT =====
async def show_contact(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("📩 Telegram", url="https://t.me/Mr_Unique_Hacker001")],
        [InlineKeyboardButton("📱 WhatsApp", url="https://wa.me/2349123578884")]
    ]
    await update.message.reply_text(
        "📞 *Contact Us*\n\nReach out to us on:",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

# ===== TRENDING =====
async def show_trending(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("🔥 Calm Down - Rema", callback_data="trend_CQL_2lDzBXs")],
        [InlineKeyboardButton("🔥 Last Last - Burna Boy", callback_data="trend_4NRXx6U8ABQ")],
        [InlineKeyboardButton("🔥 Unavailable - Davido", callback_data="trend_fG4d4h14Gec")],
        [InlineKeyboardButton("🔥 Water - Tyla", callback_data="trend_XoiOOiuH8iI")],
        [InlineKeyboardButton("🔥 Kill Bill - SZA", callback_data="trend_MSRc5J2Atrg")],
        [InlineKeyboardButton("🔥 Flowers - Miley Cyrus", callback_data="trend_G7KNmW9a75Y")],
        [InlineKeyboardButton("🔙 Back to Menu", callback_data="back_to_menu")]
    ]
    await update.message.reply_text(
        "🔥 *Trending Songs*\n\nClick any song to download:",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

# ===== SONG SEARCH =====
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

# ===== SONG INFO =====
async def song_info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    chat_id = q.message.chat_id
    index = int(q.data.split("_")[1])

    results = search_cache.get(chat_id)
    if not results or index >= len(results):
        await q.edit_message_text("⚠️ Song expired. Please search again.")
        return

    video = results[index]
    duration = f"{video['duration']//60}:{video['duration']%60:02d}" if video.get('duration') else "N/A"

    caption = f"""
🎵 *{video['title'][:100]}*

👤 Artist: `{video.get('uploader', 'Unknown')}`
⏱ Duration: `{duration}`
🔗 [Watch on YouTube]({video['url']})

Choose an action:
"""
    keyboard = [
        [InlineKeyboardButton("⬇️ Download Audio", callback_data=f"download_audio_{index}")],
        [InlineKeyboardButton("🎬 Download Video", callback_data=f"download_video_{index}")],
        [InlineKeyboardButton("📜 Lyrics", callback_data=f"lyrics_{index}")],
        [InlineKeyboardButton("🔙 Back to Results", callback_data=f"page_0")]
    ]
    await q.edit_message_text(
        caption,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

# ===== DOWNLOAD =====
async def download_callback(update: Update, context: ContextTypes.DEFAULT_TYPE, audio_only: bool):
    q = update.callback_query
    await q.answer("⬇️ Starting download...")
    user_id = q.from_user.id
    chat_id = q.message.chat_id
    index = int(q.data.split("_")[2])

    user = get_user(user_id)
    reset_downloads(user)

    if not is_premium(user) and (user[3] if user else 0) >= DOWNLOAD_LIMIT:
        await q.message.reply_text(
            "⛔ *Download Limit Reached!*\n\nUpgrade to Premium for unlimited downloads.",
            parse_mode=ParseMode.MARKDOWN
        )
        return

    results = search_cache.get(chat_id)
    if not results or index >= len(results):
        await q.message.reply_text("⚠️ Song expired. Please search again.")
        return

    video = results[index]
    status_msg = await q.message.reply_text(
        f"⬇️ *{'Audio' if audio_only else 'Video'} download in progress...* Please wait.",
        parse_mode=ParseMode.MARKDOWN
    )

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
            with open(file_path, "rb") as f:
                await q.message.reply_video(
                    video=f,
                    caption=f"🎬 *{video['title'][:100]}*\n\n✅ Video downloaded successfully!",
                    parse_mode=ParseMode.MARKDOWN,
                    supports_streaming=True
                )

        file_path.unlink(missing_ok=True)
        increment_downloads(user_id)
        add_points(user_id, 1)
        await status_msg.delete()

    except Exception as e:
        print(f"Download error: {e}")
        await status_msg.edit_text(f"❌ *Download Error*\n\n`{str(e)[:200]}`", parse_mode=ParseMode.MARKDOWN)

# ===== LYRICS =====
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

# ===== MORE TRACKS =====
async def more_tracks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    chat_id = q.message.chat_id
    offset = int(q.data.split("_")[1])

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
        keyboard.append([InlineKeyboardButton("➕ More Results", callback_data=f"more_{offset+20}")])
    keyboard.append([InlineKeyboardButton("🔙 Back to Menu", callback_data="back_to_menu")])

    await q.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup(keyboard))

# ===== PAGE NAVIGATION =====
async def page_navigation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    chat_id = q.message.chat_id

    results = search_cache.get(chat_id)
    if not results:
        await q.edit_message_text("⚠️ Search expired.")
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

# ===== TREND SONG =====
async def trend_song(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
                caption = f"""
🎵 *{video['title'][:100]}*

👤 Artist: `{video.get('uploader', 'Unknown')}`
⏱ Duration: `{duration}`

Choose an action:
"""
                keyboard = [
                    [InlineKeyboardButton("⬇️ Download Audio", callback_data="download_audio_0")],
                    [InlineKeyboardButton("🎬 Download Video", callback_data="download_video_0")],
                    [InlineKeyboardButton("📜 Lyrics", callback_data="lyrics_0")],
                    [InlineKeyboardButton("🔙 Back to Menu", callback_data="back_to_menu")]
                ]
                await msg.edit_text(caption, parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(keyboard))
            else:
                await msg.edit_text("❌ Song not found.")
    except Exception as e:
        print(f"Trend song error: {e}")
        await msg.edit_text(f"❌ Error: {str(e)[:200]}")

# ===== BACK TO MENU =====
async def back_to_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    user_id = q.from_user.id
    await q.edit_message_text("🎵 *Main Menu*", parse_mode=ParseMode.MARKDOWN)
    text = f"🎵 *Welcome back, {q.from_user.first_name}!*\n\nWhat would you like to do?"
    await q.message.reply_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=get_main_menu_keyboard(user_id))

# ===== ADMIN COMMANDS =====
async def premium(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return
    try:
        user = int(context.args[0])
        days = int(context.args[1])
        expire = datetime.datetime.now() + datetime.timedelta(days=days)
        cursor.execute("UPDATE users SET premium_expire=? WHERE id=?", (expire.isoformat(), user))
        db.commit()
        await context.bot.send_message(user, f"🎉 Congratulations!\nYou have been awarded PREMIUM\nExpires: {expire.strftime('%Y-%m-%d %H:%M')}")
        await update.message.reply_text(f"✅ Premium granted to user {user} for {days} days.")
    except:
        await update.message.reply_text("❌ Usage: /premium <user_id> <days>")

async def reward(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return
    try:
        user = int(context.args[0])
        pts = int(context.args[1])
        cursor.execute("UPDATE users SET points=points+? WHERE id=?", (pts, user))
        db.commit()
        await context.bot.send_message(user, f"🎉 Congratulations!\nYou have been awarded {pts} points!")
        await update.message.reply_text(f"✅ Awarded {pts} points to user {user}.")
    except:
        await update.message.reply_text("❌ Usage: /reward <user_id> <points>")

async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return
    cursor.execute("SELECT COUNT(*) FROM users")
    users = cursor.fetchone()[0]
    cursor.execute("SELECT COUNT(*) FROM users WHERE premium_expire > datetime('now')")
    premium_users = cursor.fetchone()[0]
    await update.message.reply_text(
        f"📊 *Bot Statistics*\n\n"
        f"👥 Total Users: *{users}*\n"
        f"💎 Premium Users: *{premium_users}*",
        parse_mode=ParseMode.MARKDOWN
    )

async def broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return
    if not update.message.reply_to_message:
        await update.message.reply_text("📢 Reply to a message (text/photo/video) to broadcast.")
        return

    msg = update.message.reply_to_message
    cursor.execute("SELECT id FROM users")
    users = [u[0] for u in cursor.fetchall()]
    delivered = 0
    failed = 0

    status = await update.message.reply_text(f"📢 Broadcasting to {len(users)} users...")

    for u in users:
        try:
            if msg.text:
                await context.bot.send_message(u, msg.text, parse_mode=msg.parse_mode)
            elif msg.photo:
                await context.bot.send_photo(u, msg.photo[-1].file_id, caption=msg.caption)
            elif msg.video:
                await context.bot.send_video(u, msg.video.file_id, caption=msg.caption)
            elif msg.audio:
                await context.bot.send_audio(u, msg.audio.file_id, caption=msg.caption)
            elif msg.document:
                await context.bot.send_document(u, msg.document.file_id, caption=msg.caption)
            delivered += 1
            if delivered % 30 == 0:
                await asyncio.sleep(0.5)
        except:
            failed += 1

    await status.edit_text(
        f"📢 *BROADCAST COMPLETE*\n\n"
        f"👥 Total: *{len(users)}*\n"
        f"✅ Delivered: *{delivered}*\n"
        f"❌ Failed: *{failed}*",
        parse_mode=ParseMode.MARKDOWN
    )

# ===== ERROR HANDLER =====
async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    print(f"Error: {context.error}")
    if update and update.effective_message:
        try:
            await update.effective_message.reply_text(
                "⚠️ *Oops! Something went wrong.*\n\nPlease try again or contact support.",
                parse_mode=ParseMode.MARKDOWN
            )
        except:
            pass

# ===== RUN BOT =====
print("""
╔══════════════════════════════════════════════════════════════╗
║           🎵 ADVANCED MUSIC BOT 🎵                           ║
║   Created by ❦ ᴍʀ ᴅᴀʀᴋ<\\>ʜᴀᴄᴋᴇʀ 🫟                        ║
╚══════════════════════════════════════════════════════════════╝
""")

if not COOKIES_FILE.exists():
    print(f"⚠️ No cookie file found at: {COOKIES_FILE}")
    print("   YouTube downloads may be blocked.\n")

print("🤖 Bot is running... Press Ctrl+C to stop.\n")

app = Application.builder().token(TOKEN).build()

# COMMANDS
app.add_handler(CommandHandler("start", start))
app.add_handler(CommandHandler("account", show_account))
app.add_handler(CommandHandler("premium", premium))
app.add_handler(CommandHandler("reward", reward))
app.add_handler(CommandHandler("stats", stats))
app.add_handler(CommandHandler("trending", show_trending))
app.add_handler(CommandHandler("broadcast", broadcast))
app.add_handler(CommandHandler("help", show_help))

# MESSAGE HANDLER - handles text and menu buttons
app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_menu_buttons))

# CALLBACK HANDLERS
app.add_handler(CallbackQueryHandler(back_to_menu, pattern="^back_to_menu$"))
app.add_handler(CallbackQueryHandler(song_info, pattern="^song_"))
app.add_handler(CallbackQueryHandler(lambda u,c: download_callback(u,c,True), pattern="^download_audio_"))
app.add_handler(CallbackQueryHandler(lambda u,c: download_callback(u,c,False), pattern="^download_video_"))
app.add_handler(CallbackQueryHandler(lyrics_callback, pattern="^lyrics_"))
app.add_handler(CallbackQueryHandler(more_tracks, pattern="^more_"))
app.add_handler(CallbackQueryHandler(page_navigation, pattern="^page_"))
app.add_handler(CallbackQueryHandler(trend_song, pattern="^trend_"))

# ERROR HANDLER
app.add_error_handler(error_handler)

app.run_polling(allowed_updates=Update.ALL_TYPES)
