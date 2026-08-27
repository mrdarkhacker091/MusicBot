#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
🎵 ADVANCED MUSIC BOT - WITH ROBUST DOWNLOADER FALLBACKS
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
import json
import urllib.parse
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

# ===== CONFIG =====
TOKEN = os.getenv("BOT_TOKEN")  # MUST be set in environment – never hardcode!
if not TOKEN:
    raise ValueError("BOT_TOKEN environment variable not set!")
OWNER_ID = int(os.getenv("OWNER_ID", "8854936887"))
BOT_USERNAME = os.getenv("BOT_USERNAME", "All_MusicDownloader_Bot")

DOWNLOAD_LIMIT = 5
COOLDOWN_HOURS = 24

BASE_DIR = Path(__file__).parent.resolve()
DOWNLOADS_DIR = BASE_DIR / "downloads"
COOKIES_DIR = BASE_DIR / "cookies"
THUMBNAILS_DIR = BASE_DIR / "thumbnails"

for d in [DOWNLOADS_DIR, COOKIES_DIR, THUMBNAILS_DIR]:
    d.mkdir(parents=True, exist_ok=True)

COOKIES_FILE = COOKIES_DIR / "cookies.txt"

# ===== DATABASE =====
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
        if (now - last_reset).total_seconds() > COOLDOWN_HOURS * 3600:
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

# ===== YT SEARCH =====
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

# ===== DOWNLOADER API FALLBACK CHAIN =====
def get_audio_download_url(youtube_url):
    """
    Try multiple external APIs to get a direct download URL for audio.
    Returns a tuple (download_url, title) or raises Exception.
    """
    encoded_url = urllib.parse.quote_plus(youtube_url)   # URL-encode for safety

    # API list – each method should return {'download': url, 'title': str} or raise
    api_methods = [
        {
            "name": "EliteProTech",
            "func": lambda: (
                lambda r: r.json() if r.status_code == 200 else {}
            )(requests.get(f"https://eliteprotech-apis.zone.id/ytdown?url={encoded_url}&format=mp3", timeout=15))
        },
        {
            "name": "Yupra",
            "func": lambda: (
                lambda r: r.json() if r.status_code == 200 else {}
            )(requests.get(f"https://api.yupra.my.id/api/downloader/ytmp3?url={encoded_url}", timeout=15))
        },
        {
            "name": "Okatsu",
            "func": lambda: (
                lambda r: r.json() if r.status_code == 200 else {}
            )(requests.get(f"https://okatsu-rolezapiiz.vercel.app/downloader/ytmp3?url={encoded_url}", timeout=15))
        },
        {
            "name": "Alya",
            "func": lambda: (
                lambda r: r.json() if r.status_code == 200 else {}
            )(requests.get(f"https://api.alyachan.pro/api/ytmp3?url={encoded_url}&apikey=G7I6X7", timeout=15))
        },
        {
            "name": "Vreden",
            "func": lambda: (
                lambda r: r.json() if r.status_code == 200 else {}
            )(requests.get(f"https://api.vreden.my.id/api/ytmp3?url={encoded_url}", timeout=15))
        },
        {
            "name": "DavidCyril",
            "func": lambda: (
                lambda r: r.json() if r.status_code == 200 else {}
            )(requests.get(f"https://apis.davidcyril.name.ng/youtube/mp3?url={encoded_url}", timeout=15))
        },
        {
            "name": "PrexzyVilla",
            "func": lambda: (
                lambda r: r.json() if r.status_code == 200 else {}
            )(requests.get(f"https://apis.prexzyvilla.site/download/ytmp3?url={encoded_url}", timeout=15))
        }
    ]

    for method in api_methods:
        try:
            data = method["func"]()
            print(f"[{method['name']}] RESPONSE: {json.dumps(data, indent=2)[:500]}")

            # Normalize response from each API
            if method["name"] == "EliteProTech":
                if data.get("success") and data.get("downloadURL"):
                    return data["downloadURL"], data.get("title", "")
            elif method["name"] == "Yupra":
                if data.get("success") and data.get("data", {}).get("download_url"):
                    return data["data"]["download_url"], data["data"].get("title", "")
            elif method["name"] == "Okatsu":
                if data.get("dl"):
                    return data["dl"], data.get("title", "")
            elif method["name"] == "Alya":
                if data.get("status") and data.get("data", {}).get("url"):
                    return data["data"]["url"], data["data"].get("title", "")
            elif method["name"] == "Vreden":
                if data.get("status") and data.get("result", {}).get("download", {}).get("url"):
                    return data["result"]["download"]["url"], data["result"]["metadata"].get("title", "")
            elif method["name"] == "DavidCyril":
                if data.get("status") and data.get("result", {}).get("download_url"):
                    return data["result"]["download_url"], data["result"].get("title", "")
            elif method["name"] == "PrexzyVilla":
                if data.get("success") and data.get("result", {}).get("download_url"):
                    return data["result"]["download_url"], data["result"].get("title", "")
        except Exception as e:
            print(f"[{method['name']}] FAILED: {type(e).__name__}: {e}")

    # ===== FALLBACK: use yt-dlp directly =====
    print("All external APIs failed. Trying yt-dlp local download...")
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
            # find the resulting file
            base = DOWNLOADS_DIR / f"{info['id']}.mp3"
            if base.exists():
                # we need to return a download URL (not local file) so we serve it via a local HTTP server?
                # But this function is expected to return a URL. Instead, we'll store the local path and handle it in the caller.
                # We'll raise a special exception that we catch in download_audio_async.
                # Better: we modify download_audio_async to handle a local file.
                # For now, we return a placeholder "local" URL that the caller recognizes.
                return f"file://{base.absolute()}", info.get("title", "")
            else:
                raise Exception("yt-dlp failed to create file")
    except Exception as e:
        print(f"yt-dlp fallback failed: {e}")
        raise Exception("All downloader APIs and yt-dlp fallback failed")

# ===== ASYNC DOWNLOAD =====
async def download_audio_async(video_id, title, youtube_url):
    loop = asyncio.get_event_loop()
    file_id = hashlib.md5(f"{video_id}{time.time()}".encode()).hexdigest()[:12]
    out_path = DOWNLOADS_DIR / f"{file_id}.mp3"

    def _download():
        try:
            download_url, final_title = get_audio_download_url(youtube_url)

            # If it's a local file from yt-dlp fallback, copy it
            if download_url.startswith("file://"):
                src = Path(download_url[7:])
                if src.exists():
                    import shutil
                    shutil.copy2(src, out_path)
                    src.unlink()  # clean up the original
                    return out_path, final_title or title

            # Otherwise download from the URL
            r = requests.get(download_url, timeout=60, stream=True, headers={"User-Agent": "Mozilla/5.0"})
            if r.status_code != 200:
                raise Exception(f"Download URL returned status {r.status_code}")
            with open(out_path, "wb") as f:
                for chunk in r.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)
            # Verify file size > 50KB
            if out_path.stat().st_size < 50 * 1024:
                raise Exception("Downloaded file too small (invalid)")
            return out_path, final_title or title
        except Exception as e:
            print(f"Download error: {e}")
            return None, None

    try:
        result = await asyncio.wait_for(loop.run_in_executor(None, _download), timeout=180.0)
        if result[0] and result[0].exists():
            return result
        return None, None
    except asyncio.TimeoutError:
        print("Download timed out")
        return None, None
    except Exception as e:
        print(f"Download crashed: {e}")
        return None, None

# ===== LYRICS (multi-source) =====
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

    # 4) AZLyrics
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

    return f"🎵 *{title}* — *{artist}*\n\n_Lyrics not found in our databases._"

# ===== TELEGRAM HANDLERS =====
# ... (all the handlers remain the same as in your original script, 
#      but make sure to use the updated download_audio_async)

# To save space, I'll only show the download_callback which uses the new function,
# and the rest of the handlers (start, account, etc.) are unchanged.

async def download_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer("⬇️ Starting download...")
    user_id = q.from_user.id
    chat_id = q.message.chat_id
    index = int(q.data.split("_")[2])  # download_audio_0

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
    status_msg = await q.message.reply_text("⬇️ *Download in progress...* Please wait.", parse_mode=ParseMode.MARKDOWN)

    try:
        file_path, final_title = await download_audio_async(video["id"], video["title"], video["url"])
        if not file_path or not file_path.exists():
            await status_msg.edit_text(
                "❌ *Download failed.*\n\n"
                "All downloader APIs and local conversion are currently unavailable.\n"
                "Please try again later or contact support.",
                parse_mode=ParseMode.MARKDOWN
            )
            return

        await status_msg.edit_text("📤 *Uploading...*", parse_mode=ParseMode.MARKDOWN)

        with open(file_path, "rb") as f:
            await q.message.reply_audio(
                audio=f,
                title=final_title[:100],
                performer=video.get("uploader", "Unknown")[:100],
                duration=video.get("duration", 0),
                caption=f"🎵 *{final_title[:100]}*\n\n✅ Audio downloaded successfully!",
                parse_mode=ParseMode.MARKDOWN
            )

        file_path.unlink(missing_ok=True)
        increment_downloads(user_id)
        add_points(user_id, 1)
        await status_msg.delete()

    except Exception as e:
        print(f"Download error: {e}")
        await status_msg.edit_text(f"❌ *Download Error*\n\n`{str(e)[:200]}`", parse_mode=ParseMode.MARKDOWN)

# ... (rest of the handlers like start, account, etc. remain as in your earlier version)

# ===== MAIN =====
def main():
    print("""
╔══════════════════════════════════════════════════════════════╗
║           🎵 ADVANCED MUSIC BOT 🎵                           ║
║   Created by ❦ ᴍʀ ᴅᴀʀᴋ<\\>ʜᴀᴄᴋᴇʀ 🫟                        ║
╚══════════════════════════════════════════════════════════════╝
    """)
    if not COOKIES_FILE.exists():
        print(f"⚠️ No cookie file found at: {COOKIES_FILE}")
        print("   (yt-dlp fallback may be limited)\n")
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

    # MESSAGE HANDLER
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_menu_buttons))

    # CALLBACKS
    app.add_handler(CallbackQueryHandler(back_to_menu, pattern="^back_to_menu$"))
    app.add_handler(CallbackQueryHandler(song_info, pattern="^song_"))
    app.add_handler(CallbackQueryHandler(download_callback, pattern="^download_audio_"))
    app.add_handler(CallbackQueryHandler(lyrics_callback, pattern="^lyrics_"))
    app.add_handler(CallbackQueryHandler(more_tracks, pattern="^more_"))
    app.add_handler(CallbackQueryHandler(page_navigation, pattern="^page_"))
    app.add_handler(CallbackQueryHandler(trend_song, pattern="^trend_"))

    app.add_error_handler(error_handler)
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
