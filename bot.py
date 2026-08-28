#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
ADVANCED MUSIC BOT
Created by Mr DarkHacker
"""

import os, sys, time, asyncio, logging, sqlite3, datetime, hashlib, urllib.parse, json, re, shutil, tempfile
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import yt_dlp
import requests
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, LabeledPrice
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes, PreCheckoutQueryHandler, filters
from telegram.constants import ParseMode

TOKEN = "8350984585:AAFSm-9J9MTrwluT1WQk6eHhPplSoBR6c0k"
OWNER_ID = int(os.getenv("OWNER_ID", "8854936887"))
BOT_USERNAME = os.getenv("BOT_USERNAME", "All_MusicDownloader_Bot")
CHANNEL_ID = os.getenv("CHANNEL_ID", "")
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

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

_sc_map = str.maketrans(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ",
    "ᴀʙᴄᴅᴇғɢʜɪᴊᴋʟᴍɴᴏᴘǫʀsᴛᴜᴠᴡxʏᴢᴀʙᴄᴅᴇғɢʜɪᴊᴋʟᴍɴᴏᴘǫʀsᴛᴜᴠᴡxʏᴢ"
)
def sc(text):
    if not text:
        return ""
    return str(text).translate(_sc_map)

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
    first_name TEXT,
    username TEXT,
    joined_date TEXT
)
""")
db.commit()
search_cache = {}
trending_cache = {"data": [], "last_update": 0}

SOCIAL_DOMAINS = {
    "tiktok.com", "vm.tiktok.com", "vt.tiktok.com",
    "instagram.com", "www.instagram.com",
    "facebook.com", "www.facebook.com", "fb.watch",
    "youtube.com", "www.youtube.com", "youtu.be",
    "x.com", "www.x.com", "twitter.com", "www.twitter.com",
    "reddit.com", "www.reddit.com", "redd.it"
}
URL_PATTERN = re.compile(r'https?://[^s<>"]+', re.IGNORECASE)

def get_user(user_id):
    cursor.execute("SELECT * FROM users WHERE id=?", (user_id,))
    user = cursor.fetchone()
    if not user:
        now = datetime.datetime.now().isoformat()
        cursor.execute("INSERT INTO users(id,last_reset,joined_date) VALUES(?,?,?)", (user_id, now, now))
        db.commit()
        cursor.execute("SELECT * FROM users WHERE id=?", (user_id,))
        user = cursor.fetchone()
    return user

def update_user_profile(user_id, first_name, username):
    cursor.execute("UPDATE users SET first_name=?, username=? WHERE id=?", (first_name, username, user_id))
    db.commit()

def reset_downloads(user):
    if not user or not user[4]:
        return False
    try:
        last_reset = datetime.datetime.fromisoformat(user[4])
        if (datetime.datetime.now() - last_reset).total_seconds() > COOLDOWN_HOURS * 3600:
            cursor.execute("UPDATE users SET downloads=0,last_reset=? WHERE id=?", (datetime.datetime.now().isoformat(), user[0]))
            db.commit()
            return True
    except:
        pass
    return False

def is_premium(user):
    if not user or not user[2]:
        return False
    try:
        return datetime.datetime.fromisoformat(user[2]) > datetime.datetime.now()
    except:
        return False

def increment_downloads(user_id):
    cursor.execute("UPDATE users SET downloads=downloads+1 WHERE id=?", (user_id,))
    db.commit()

def add_points(user_id, points):
    cursor.execute("UPDATE users SET points=points+? WHERE id=?", (points, user_id))
    db.commit()

def deduct_points(user_id, points):
    cursor.execute("UPDATE users SET points=points-? WHERE id=?", (points, user_id))
    db.commit()

def grant_premium(user_id, days):
    expire = datetime.datetime.now() + datetime.timedelta(days=days)
    cursor.execute("UPDATE users SET premium_expire=? WHERE id=?", (expire.isoformat(), user_id))
    db.commit()
    return expire

def get_main_menu_keyboard(user_id):
    user = get_user(user_id)
    points = user[1] if user else 0
    downloads = user[3] if user else 0
    premium = is_premium(user)
    prem_text = sc("💎 Premium") if premium else sc("⭐ Upgrade")
    keyboard = [
        [sc("🎵 Search Music"), sc("🔥 Trending")],
        [sc(f"📊 Account ({points} pts)"), sc(f"⬇️ {downloads}/{DOWNLOAD_LIMIT}")],
        [sc("🔗 Referral"), sc("🤖 Other Bots")],
        [prem_text, sc("❓ Help")],
        [sc("📞 Contact")]
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

def search_music(query, max_results=50):
    try:
        opts = {
            "quiet": True, "no_warnings": True, "extract_flat": True,
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
                    dur = int(entry.get("duration", 0)) if entry.get("duration") else 0
                    valid.append({
                        "id": entry["id"], "title": entry["title"], "duration": dur,
                        "uploader": entry.get("uploader", "Unknown"),
                        "view_count": entry.get("view_count", 0),
                        "thumbnail": entry.get("thumbnail", ""),
                        "url": f"https://youtube.com/watch?v={entry['id']}"
                    })
            return valid
    except Exception as e:
        logger.error(f"Search error: {e}")
        return []

def fetch_trending_music():
    global trending_cache
    now = time.time()
    if trending_cache["data"] and (now - trending_cache["last_update"] < 3600):
        return trending_cache["data"]
    try:
        opts = {
            "quiet": True, "no_warnings": True, "extract_flat": True,
            "playlistend": 10,
            "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        }
        with yt_dlp.YoutubeDL(opts) as ydl:
            result = ydl.extract_info("ytsearch10:trending music official video 2026", download=False)
            entries = result.get("entries", []) if result else []
            valid = []
            for entry in entries:
                if entry and entry.get("id") and entry.get("title"):
                    dur = int(entry.get("duration", 0)) if entry.get("duration") else 0
                    valid.append({
                        "id": entry["id"], "title": entry["title"], "duration": dur,
                        "uploader": entry.get("uploader", "Unknown"),
                        "thumbnail": entry.get("thumbnail", ""),
                        "url": f"https://youtube.com/watch?v={entry['id']}"
                    })
            trending_cache = {"data": valid, "last_update": now}
            return valid
    except Exception as e:
        logger.error(f"Trending error: {e}")
        return trending_cache["data"]

def get_audio_download_url(youtube_url):
    encoded = urllib.parse.quote_plus(youtube_url)
    apis = [
        ("EliteProTech", lambda: requests.get(f"https://eliteprotech-apis.zone.id/ytdown?url={encoded}&format=mp3", timeout=15),
         lambda d: (d.get("downloadURL"), d.get("title")) if d.get("success") else (None, None)),
        ("DavidCyril", lambda: requests.get(f"https://apis.davidcyril.name.ng/youtube/mp3?url={encoded}", timeout=15),
         lambda d: (d.get("result", {}).get("download_url"), d.get("result", {}).get("title")) if d.get("status") else (None, None)),
        ("Alya", lambda: requests.get(f"https://api.alyachan.pro/api/ytmp3?url={encoded}&apikey=G7I6X7", timeout=15),
         lambda d: (d.get("data", {}).get("url"), d.get("data", {}).get("title")) if d.get("status") else (None, None)),
        ("Okatsu", lambda: requests.get(f"https://okatsu-rolezapiiz.vercel.app/downloader/ytmp3?url={encoded}", timeout=15),
         lambda d: (d.get("dl"), d.get("title")) if d.get("dl") else (None, None)),
        ("Vreden", lambda: requests.get(f"https://api.vreden.my.id/api/ytmp3?url={encoded}", timeout=15),
         lambda d: (d.get("result", {}).get("download", {}).get("url"), d.get("result", {}).get("metadata", {}).get("title")) if d.get("status") else (None, None)),
        ("PrexzyVilla", lambda: requests.get(f"https://apis.prexzyvilla.site/download/ytmp3?url={encoded}", timeout=15),
         lambda d: (d.get("result", {}).get("download_url"), d.get("result", {}).get("title")) if d.get("success") else (None, None)),
    ]
    for name, req_func, extract in apis:
        try:
            r = req_func()
            if r.status_code != 200:
                continue
            data = r.json()
            url, title = extract(data)
            if url and url.startswith(("http://", "https://")):
                return url, title or ""
        except Exception as e:
            logger.warning(f"[{name}] FAILED: {e}")
    logger.info("All APIs failed. Using yt-dlp fallback...")
    try:
        opts = {
            "quiet": True, "no_warnings": True, "noplaylist": True,
            "format": "bestaudio/best", "extractaudio": True, "audioformat": "mp3",
            "outtmpl": str(DOWNLOADS_DIR / "%(id)s.%(ext)s"),
            "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}]
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
    raise Exception("All download methods failed")

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
            r = requests.get(download_url, timeout=120, stream=True, headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Accept": "*/*", "Accept-Encoding": "gzip, deflate, br", "Connection": "keep-alive"
            })
            if r.status_code not in (200, 206):
                raise Exception(f"HTTP {r.status_code}")
            with open(out_path, "wb") as f:
                for chunk in r.iter_content(chunk_size=65536):
                    if chunk:
                        f.write(chunk)
            if not out_path.exists() or out_path.stat().st_size < 50 * 1024:
                raise Exception("File too small or missing")
            return out_path, final_title or title
        except Exception as e:
            logger.error(f"Download error: {e}")
            return None, None
    try:
        result = await asyncio.wait_for(loop.run_in_executor(None, _download), timeout=180.0)
        return result
    except asyncio.TimeoutError:
        return None, None
    except Exception as e:
        logger.error(f"Download crashed: {e}")
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
            r = requests.get(url, timeout=10)
            if r.status_code == 200:
                data = r.json()
                if data.get("lyrics"):
                    return data["lyrics"]
    except:
        pass
    try:
        search_term = f"{artist} {title}".strip()
        url = f"https://lrclib.net/api/search?q={requests.utils.quote(search_term)}"
        r = requests.get(url, timeout=10)
        if r.status_code == 200:
            data = r.json()
            if data and len(data) > 0 and data[0].get("plainLyrics"):
                return data[0]["plainLyrics"]
    except:
        pass
    try:
        url = f"https://api.lyrics.kashishmusic.in/lyrics?title={requests.utils.quote(title)}&artist={requests.utils.quote(artist)}"
        r = requests.get(url, timeout=10)
        if r.status_code == 200:
            data = r.json()
            if data.get("lyrics"):
                return data["lyrics"]
    except:
        pass
    return None

def extract_url(text):
    if not text:
        return None
    match = URL_PATTERN.search(text)
    return match.group(0).rstrip(".,!?)]}") if match else None

def is_social_url(url):
    try:
        hostname = urllib.parse.urlparse(url).hostname
        if not hostname:
            return False
        hostname = hostname.lower().rstrip(".")
        return any(hostname == d or hostname.endswith("." + d) for d in SOCIAL_DOMAINS)
    except:
        return False

def social_platform(url):
    try:
        h = urllib.parse.urlparse(url).hostname or ""
        h = h.lower()
        if "tiktok" in h: return "TikTok"
        if "instagram" in h: return "Instagram"
        if "facebook" in h or h == "fb.watch": return "Facebook"
        if "youtube" in h or h == "youtu.be": return "YouTube"
        if "twitter" in h or h == "x.com": return "X/Twitter"
        if "reddit" in h: return "Reddit"
    except:
        pass
    return "Social Media"

def format_bytes(size):
    if size < 1024: return f"{size} B"
    if size < 1024 * 1024: return f"{size / 1024:.1f} KB"
    return f"{size / (1024 * 1024):.1f} MB"

def find_video_file(directory):
    files = [p for p in directory.rglob("*") if p.is_file() and p.suffix.lower() in {".mp4", ".mkv", ".webm", ".mov", ".m4v"}]
    return max(files, key=lambda p: p.stat().st_size) if files else None

def download_social_video(url, output_dir):
    out_path = Path(output_dir)
    template = str(out_path / "%(title).80s-%(id)s.%(ext)s")
    def progress_hook(d):
        if d.get("status") == "downloading" and d.get("downloaded_bytes", 0) > 100 * 1024 * 1024:
            raise RuntimeError("File exceeds 100MB")
    ydl_opts = {
        "format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
        "outtmpl": template, "merge_output_format": "mp4", "noplaylist": True,
        "writethumbnail": False, "writesubtitles": False, "writeautomaticsub": False,
        "quiet": True, "no_warnings": True, "retries": 2, "fragment_retries": 2,
        "max_filesize": 100 * 1024 * 1024, "progress_hooks": [progress_hook],
        "socket_timeout": 30
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
    file_path = find_video_file(out_path)
    return file_path, info

async def notify_channel(context, text):
    if CHANNEL_ID:
        try:
            await context.bot.send_message(CHANNEL_ID, text, parse_mode=ParseMode.MARKDOWN)
        except Exception as e:
            logger.error(f"Channel notify failed: {e}")

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
    is_new = db_user[6] is None
    update_user_profile(user_id, user.first_name or "Unknown", user.username or "None")

    if db_user and db_user[5] is None and ref and ref != user_id:
        cursor.execute("UPDATE users SET referrer=? WHERE id=?", (ref, user_id))
        cursor.execute("UPDATE users SET points=points+? WHERE id=?", (POINTS_PER_REFERRAL, ref))
        db.commit()
        try:
            ref_user = get_user(ref)
            ref_name = ref_user[6] if ref_user and ref_user[6] else "User"
            await context.bot.send_message(
                ref,
                sc(f"🎉 ʜᴇʏ {ref_name}!\n\nʏᴏᴜ ᴊᴜsᴛ ɢᴏᴛ ᴀ ɴᴇᴡ ʀᴇғᴇʀʀᴀʟ!\n\n👤 ɴᴀᴍᴇ: {user.first_name or 'Unknown'}\n📛 ᴜsᴇʀɴᴀᴍᴇ: @{user.username or 'None'}\n🆔 ᴜsᴇʀ ɪᴅ: {user_id}\n📅 ᴅᴀᴛᴇ: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n✅ ʏᴏᴜ ᴇᴀʀɴᴇᴅ +{POINTS_PER_REFERRAL} ᴘᴏɪɴᴛs!"),
                parse_mode=ParseMode.MARKDOWN
            )
        except:
            pass

    if is_new and CHANNEL_ID:
        ref_by = "ᴅɪʀᴇᴄᴛ ᴊᴏɪɴ"
        if ref:
            ref_u = get_user(ref)
            if ref_u and ref_u[7]:
                ref_by = f"@{ref_u[7]}"
            elif ref_u and ref_u[6]:
                ref_by = ref_u[6]
        await notify_channel(context,
            f"📥 *ɴᴇᴡ ᴜsᴇʀ ᴊᴏɪɴᴇᴅ*\n\n"
            f"👤 ɴᴀᴍᴇ: {user.first_name or 'Unknown'}\n"
            f"📛 ᴜsᴇʀɴᴀᴍᴇ: @{user.username or 'None'}\n"
            f"🆔 ᴜsᴇʀ ɪᴅ: {user_id}\n"
            f"📅 ᴅᴀᴛᴇ: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"👥 ʀᴇғᴇʀʀᴇᴅ ʙʏ: {ref_by}\n\n"
            f"🤖 [sᴛᴀʀᴛ ʙᴏᴛ](https://t.me/{BOT_USERNAME}?start={user_id})"
        )

    text = sc(f"""
🎵 ᴡᴇʟᴄᴏᴍᴇ ᴛᴏ ᴀᴅᴠᴀɴᴄᴇᴅ ᴍᴜsɪᴄ ʙᴏᴛ!

👋 ʜɪ {user.first_name or 'ғʀɪᴇɴᴅ'}!

ɪ ᴄᴀɴ ʜᴇʟᴘ ʏᴏᴜ ғɪɴᴅ ᴀɴᴅ ᴅᴏᴡɴʟᴏᴀᴅ ᴍᴜsɪᴄ ғʀᴏᴍ ʏᴏᴜᴛᴜʙᴇ.

✨ ғᴇᴀᴛᴜʀᴇs:
• 🎵 sᴇᴀʀᴄʜ ᴀɴʏ sᴏɴɢ
• ⬇️ ᴅᴏᴡɴʟᴏᴀᴅ ᴍᴘ3 ᴀᴜᴅɪᴏ
• 📜 ɢᴇᴛ sᴏɴɢ ʟʏʀɪᴄs (ᴘʀᴇᴍɪᴜᴍ)
• 🔗 ʀᴇғᴇʀ ғʀɪᴇɴᴅs & ᴇᴀʀɴ ᴘᴏɪɴᴛs
• 💎 ᴘʀᴇᴍɪᴜᴍ ғᴏʀ ᴜɴʟɪᴍɪᴛᴇᴅ ᴅᴏᴡɴʟᴏᴀᴅs
• 🎬 sᴏᴄɪᴀʟ ᴍᴇᴅɪᴀ ᴠɪᴅᴇᴏ ᴅᴏᴡɴʟᴏᴀᴅᴇʀ

ᴜsᴇ ᴛʜᴇ ʙᴜᴛᴛᴏɴs ʙᴇʟᴏᴡ ᴛᴏ ɢᴇᴛ sᴛᴀʀᴛᴇᴅ!
""")
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=get_main_menu_keyboard(user_id))

async def handle_menu_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = update.message.text

    if sc("🎵 Search Music") in text or text == "🎵 Search Music":
        await update.message.reply_text(sc("🎵 sᴇɴᴅ ᴍᴇ ᴀ sᴏɴɢ ɴᴀᴍᴇ ᴏʀ ᴀʀᴛɪsᴛ ᴛᴏ sᴇᴀʀᴄʜ!\n\n_ᴇxᴀᴍᴘʟᴇ: \"ᴄᴀʟᴍ ᴅᴏᴡɴ ʀᴇᴍᴀ\"_"), parse_mode=ParseMode.MARKDOWN, reply_markup=get_main_menu_keyboard(user_id))
    elif sc("🔥 Trending") in text or text == "🔥 Trending":
        await show_trending(update, context)
    elif sc("📊 Account") in text or text == "📊 Account":
        await show_account(update, context)
    elif sc("⬇️") in text or text.startswith("⬇️"):
        user = get_user(user_id)
        reset_downloads(user)
        remaining = DOWNLOAD_LIMIT - (user[3] if user else 0)
        await update.message.reply_text(sc(f"📊 ᴅᴏᴡɴʟᴏᴀᴅ ᴜsᴀɢᴇ\n\nᴛᴏᴅᴀʏ: {user[3] if user else 0}/{DOWNLOAD_LIMIT}\nʀᴇᴍᴀɪɴɪɴɢ: {remaining}\nᴘʀᴇᴍɪᴜᴍ: {'✅ ᴀᴄᴛɪᴠᴇ' if is_premium(user) else '❌ ɴᴏᴛ ᴀᴄᴛɪᴠᴇ'}"), parse_mode=ParseMode.MARKDOWN, reply_markup=get_main_menu_keyboard(user_id))
    elif sc("🔗 Referral") in text or text == "🔗 Referral":
        await show_referral(update, context)
    elif sc("🤖 Other Bots") in text or text == "🤖 Other Bots":
        await show_other_bots(update, context)
    elif sc("⭐ Upgrade") in text or text == "⭐ Upgrade" or sc("💎 Premium") in text or text == "💎 Premium":
        await show_premium(update, context)
    elif sc("❓ Help") in text or text == "❓ Help":
        await show_help(update, context)
    elif sc("📞 Contact") in text or text == "📞 Contact":
        await show_contact(update, context)
    else:
        url = extract_url(update.message.text)
        if url and is_social_url(url):
            await handle_social_download(update, context, url)
        else:
            await song_search(update, context)

async def show_account(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user = get_user(user_id)
    reset_downloads(user)
    points = user[1] if user else 0
    downloads = user[3] if user else 0
    premium_status = sc("💎 ᴀᴄᴛɪᴠᴇ") if is_premium(user) else sc("❌ ɪɴᴀᴄᴛɪᴠᴇ")
    text = sc(f"""
👤 ʏᴏᴜʀ ᴀᴄᴄᴏᴜɴᴛ

💰 ᴘᴏɪɴᴛs: {points}
⬇️ ᴅᴏᴡɴʟᴏᴀᴅs ᴛᴏᴅᴀʏ: {downloads}/{DOWNLOAD_LIMIT}
💎 ᴘʀᴇᴍɪᴜᴍ: {premium_status}

1 ʀᴇғᴇʀʀᴀʟ = {POINTS_PER_REFERRAL} ᴘᴏɪɴᴛs
{POINTS_PER_DOWNLOAD} ᴘᴏɪɴᴛs = 1 ᴇxᴛʀᴀ ᴅᴏᴡɴʟᴏᴀᴅ
{POINTS_PER_LYRICS} ᴘᴏɪɴᴛs = 1 ʟʏʀɪᴄs sᴇᴀʀᴄʜ

ɪɴᴠɪᴛᴇ ғʀɪᴇɴᴅs ᴀɴᴅ ᴇᴀʀɴ ᴘᴏɪɴᴛs!
""")
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=get_main_menu_keyboard(user_id))

async def show_referral(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    link = f"https://t.me/{BOT_USERNAME}?start={user_id}"
    text = sc(f"""
🔗 ʏᴏᴜʀ ʀᴇғᴇʀʀᴀʟ ʟɪɴᴋ

sʜᴀʀᴇ ᴛʜɪs ʟɪɴᴋ ᴡɪᴛʜ ʏᴏᴜʀ ғʀɪᴇɴᴅs:

`{link}`

✨ ʜᴏᴡ ɪᴛ ᴡᴏʀᴋs:
• ᴇᴀᴄʜ ғʀɪᴇɴᴅ ᴡʜᴏ ᴊᴏɪɴs ɢɪᴠᴇs ʏᴏᴜ +{POINTS_PER_REFERRAL} ᴘᴏɪɴᴛs!
• ᴜsᴇ ᴘᴏɪɴᴛs ᴛᴏ ᴜɴʟᴏᴄᴋ ᴇxᴛʀᴀ ᴅᴏᴡɴʟᴏᴀᴅs ᴀɴᴅ ʟʏʀɪᴄs!

_ᴛᴀᴘ ᴀɴᴅ ʜᴏʟᴅ ᴛᴏ ᴄᴏᴘʏ ᴛʜᴇ ʟɪɴᴋ_
""")
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton(sc("📤 sʜᴀʀᴇ ʟɪɴᴋ"), url=f"https://t.me/share/url?url={link}&text=🎵%20ɢᴇᴛ%20ᴍᴜsɪᴄ%20ғᴏʀ%20ғʀᴇᴇ!")]
    ])
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard)

async def show_other_bots(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = sc("""
🤖 ᴏᴜʀ ᴏᴛʜᴇʀ ʙᴏᴛs

*@DarkHacker_BanBot* — ɢʜᴏsᴛsʜᴇʟʟ ᴍᴅ
ᴀ ᴍᴜʟᴛɪᴘᴜʀᴘᴏsᴇ ᴡʜᴀᴛsᴀᴘᴘ ʙᴏᴛ ᴡɪᴛʜ ᴀɴᴛɪᴅᴇʟᴇᴛᴇ, ᴡᴀʟʟᴘᴀᴘᴇʀs, ᴍᴇᴅɪᴀ ᴛᴏᴏʟs, ɢʀᴏᴜᴘ ᴍᴀɴᴀɢᴇᴍᴇɴᴛ, ᴀɪ ᴄʜᴀᴛ, ᴅᴏᴡɴʟᴏᴀᴅᴇʀs, ᴀɴᴅ ᴍᴏʀᴇ.

*@WormGPT_Prover_Bot*
[ᴅᴇsᴄʀɪᴘᴛɪᴏɴ ᴡɪʟʟ ʙᴇ ᴀᴅᴅᴇᴅ ʙʏ ᴏᴡɴᴇʀ]

*@Whatsapp2_Ban_bot*
[ᴅᴇsᴄʀɪᴘᴛɪᴏɴ ᴡɪʟʟ ʙᴇ ᴀᴅᴅᴇᴅ ʙʏ ᴏᴡɴᴇʀ]

*@Image_ConverterTo_linkBot*
ᴄᴏɴᴠᴇʀᴛ ɪᴍᴀɢᴇs, ᴠɪᴅᴇᴏs, ғɪʟᴇs, ᴀɴᴅ ᴠᴏɪᴄᴇ ɴᴏᴛᴇs ɪɴᴛᴏ ᴅɪʀᴇᴄᴛ ʟɪɴᴋs. ᴜᴘʟᴏᴀᴅ ᴀɴʏ ᴍᴇᴅɪᴀ ᴀɴᴅ ɢᴇᴛ ᴀ sʜᴀʀᴀʙʟᴇ ʟɪɴᴋ ɪɴsᴛᴀɴᴛʟʏ.

_ᴍᴏʀᴇ ʙᴏᴛs ᴄᴏᴍɪɴɢ sᴏᴏɴ!_
""")
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=get_main_menu_keyboard(update.effective_user.id))

async def show_premium(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user = get_user(user_id)
    if is_premium(user):
        expire = "N/A"
        if user[2]:
            try:
                expire = datetime.datetime.fromisoformat(user[2]).strftime("%Y-%m-%d %H:%M")
            except:
                pass
        text = sc(f"💎 ᴘʀᴇᴍɪᴜᴍ ᴀᴄᴛɪᴠᴇ\n\n📅 ᴇxᴘɪʀᴇs: {expire}\n⬇️ ᴜɴʟɪᴍɪᴛᴇᴅ ᴅᴏᴡɴʟᴏᴀᴅs\n📜 ᴜɴʟɪᴍɪᴛᴇᴅ ʟʏʀɪᴄs\n\nᴛʜᴀɴᴋ ʏᴏᴜ ғᴏʀ sᴜᴘᴘᴏʀᴛɪɴɢ ᴛʜᴇ ʙᴏᴛ! 🙏")
        await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=get_main_menu_keyboard(user_id))
        return

    text = sc("""
💎 ᴜᴘɢʀᴀᴅᴇ ᴛᴏ ᴘʀᴇᴍɪᴜᴍ

✨ ʙᴇɴᴇғɪᴛs:
• ⬇️ ᴜɴʟɪᴍɪᴛᴇᴅ ᴅᴏᴡɴʟᴏᴀᴅs
• 🚀 ᴘʀɪᴏʀɪᴛʏ ᴅᴏᴡɴʟᴏᴀᴅ sᴘᴇᴇᴅ
• 🎵 ʜᴅ ᴀᴜᴅɪᴏ ǫᴜᴀʟɪᴛʏ
• 📜 ᴜɴʟɪᴍɪᴛᴇᴅ ʟʏʀɪᴄs ᴀᴄᴄᴇss
• 🚫 ɴᴏ ᴄᴏᴏʟᴅᴏᴡɴ ᴘᴇʀɪᴏᴅs
• 🎬 sᴏᴄɪᴀʟ ᴍᴇᴅɪᴀ ᴠɪᴅᴇᴏ ᴅᴏᴡɴʟᴏᴀᴅs

ᴄʜᴏᴏsᴇ ʏᴏᴜʀ ᴘʟᴀɴ ʙᴇʟᴏᴡ:
""")
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton(sc("⭐ 7 ᴅᴀʏs — 50 sᴛᴀʀs"), callback_data="premium_7_50")],
        [InlineKeyboardButton(sc("⭐ 30 ᴅᴀʏs — 150 sᴛᴀʀs"), callback_data="premium_30_150")],
        [InlineKeyboardButton(sc("⭐ 90 ᴅᴀʏs — 400 sᴛᴀʀs"), callback_data="premium_90_400")],
        [InlineKeyboardButton(sc("📞 ᴄᴏɴᴛᴀᴄᴛ ᴏᴡɴᴇʀ"), url="https://t.me/Mr_Unique_Hacker002")]
    ])
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard)

async def show_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = sc(f"""
❓ ʜᴇʟᴘ ɢᴜɪᴅᴇ

*ʜᴏᴡ ᴛᴏ ᴜsᴇ:*
1. sᴇɴᴅ ᴀ sᴏɴɢ ɴᴀᴍᴇ ᴏʀ ᴀʀᴛɪsᴛ
2. ᴄʟɪᴄᴋ ᴏɴ ᴀ ʀᴇsᴜʟᴛ
3. ᴄʜᴏᴏsᴇ ᴅᴏᴡɴʟᴏᴀᴅ ᴀᴜᴅɪᴏ ᴏʀ ʟʏʀɪᴄs

*ᴄᴏᴍᴍᴀɴᴅs:*
/start — sᴛᴀʀᴛ ᴛʜᴇ ʙᴏᴛ
/account — ᴠɪᴇᴡ ʏᴏᴜʀ ᴘʀᴏғɪʟᴇ
/trending — ᴛʀᴇɴᴅɪɴɢ sᴏɴɢs
/help — ᴛʜɪs ʜᴇʟᴘ ᴍᴇssᴀɢᴇ

*ғʀᴇᴇ ᴜsᴇʀs:*
• {DOWNLOAD_LIMIT} ᴅᴏᴡɴʟᴏᴀᴅs ᴘᴇʀ ᴅᴀʏ
• ʟʏʀɪᴄs ʟᴏᴄᴋᴇᴅ (ᴘʀᴇᴍɪᴜᴍ ᴏɴʟʏ)
• ᴜsᴇ ᴘᴏɪɴᴛs ғᴏʀ ᴇxᴛʀᴀ ғᴇᴀᴛᴜʀᴇs

*ᴘʀᴇᴍɪᴜᴍ:*
• ᴜɴʟɪᴍɪᴛᴇᴅ ᴇᴠᴇʀʏᴛʜɪɴɢ
• ɴᴏ ᴡᴀɪᴛ ᴛɪᴍᴇs

ᴄᴏɴᴛᴀᴄᴛ @Mr_Unique_Hacker002 ғᴏʀ sᴜᴘᴘᴏʀᴛ
""")
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=get_main_menu_keyboard(update.effective_user.id))

async def show_contact(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("📩 Telegram", url="https://t.me/Mr_Unique_Hacker002")],
        [InlineKeyboardButton("📱 WhatsApp", url="https://wa.me/2349123578884")]
    ])
    await update.message.reply_text(sc("📞 ᴄᴏɴᴛᴀᴄᴛ ᴜs\n\nʀᴇᴀᴄʜ ᴏᴜᴛ ᴛᴏ ᴜs ᴏɴ:"), parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard)

async def show_trending(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text(sc("🔥 ғᴇᴛᴄʜɪɴɢ ʀᴇᴀʟ-ᴛɪᴍᴇ ᴛʀᴇɴᴅɪɴɢ sᴏɴɢs..."), parse_mode=ParseMode.MARKDOWN)
    results = await asyncio.get_event_loop().run_in_executor(None, fetch_trending_music)
    if not results:
        await msg.edit_text(sc("❌ ғᴀɪʟᴇᴅ ᴛᴏ ғᴇᴛᴄʜ ᴛʀᴇɴᴅɪɴɢ sᴏɴɢs. ᴛʀʏ ᴀɢᴀɪɴ ʟᴀᴛᴇʀ."), parse_mode=ParseMode.MARKDOWN)
        return
    chat_id = update.message.chat_id
    search_cache[chat_id] = results
    keyboard = []
    for i, r in enumerate(results[:10]):
        dur = f" ⏱{r['duration']//60}:{r['duration']%60:02d}" if r.get('duration') else ""
        title = r['title'][:45] + "..." if len(r['title']) > 45 else r['title']
        keyboard.append([InlineKeyboardButton(f"🔥 {title}{dur}", callback_data=f"song_{i}")])
    keyboard.append([InlineKeyboardButton(sc("🔙 ʙᴀᴄᴋ ᴛᴏ ᴍᴇɴᴜ"), callback_data="back_to_menu")])
    await msg.edit_text(sc("🔥 *ᴛʀᴇɴᴅɪɴɢ sᴏɴɢs*\n\nᴄʟɪᴄᴋ ᴀɴʏ sᴏɴɢ ᴛᴏ ᴅᴏᴡɴʟᴏᴀᴅ:"), parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(keyboard))

async def song_search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    query = update.message.text.strip()
    if len(query) < 2:
        await update.message.reply_text(sc("⚠️ ᴘʟᴇᴀsᴇ ᴇɴᴛᴇʀ ᴀᴛ ʟᴇᴀsᴛ 2 ᴄʜᴀʀᴀᴄᴛᴇʀs ᴛᴏ sᴇᴀʀᴄʜ."), reply_markup=get_main_menu_keyboard(user_id))
        return
    msg = await update.message.reply_text(sc("🔎 sᴇᴀʀᴄʜɪɴɢ..."), parse_mode=ParseMode.MARKDOWN)
    results = await asyncio.get_event_loop().run_in_executor(None, search_music, query)
    if not results:
        await msg.edit_text(sc("❌ ɴᴏ ʀᴇsᴜʟᴛs ғᴏᴜɴᴅ.\n\n💡 ᴛʀʏ ᴅɪғғᴇʀᴇɴᴛ ᴋᴇʏᴡᴏʀᴅs ᴏʀ ᴀʀᴛɪsᴛ ɴᴀᴍᴇ."), parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(sc("🔙 ʙᴀᴄᴋ ᴛᴏ ᴍᴇɴᴜ"), callback_data="back_to_menu")]]))
        return
    chat_id = update.message.chat_id
    search_cache[chat_id] = results
    keyboard = []
    for i, r in enumerate(results[:20]):
        dur = f" ⏱{r['duration']//60}:{r['duration']%60:02d}" if r.get('duration') else ""
        title = r['title'][:45] + "..." if len(r['title']) > 45 else r['title']
        keyboard.append([InlineKeyboardButton(f"🎵 {title}{dur}", callback_data=f"song_{i}")])
    if len(results) > 20:
        keyboard.append([InlineKeyboardButton(sc("➕ ᴍᴏʀᴇ ʀᴇsᴜʟᴛs"), callback_data="more_20")])
    keyboard.append([InlineKeyboardButton(sc("🔙 ʙᴀᴄᴋ ᴛᴏ ᴍᴇɴᴜ"), callback_data="back_to_menu")])
    await msg.edit_text(sc(f"🎵 *ʀᴇsᴜʟᴛs ғᴏʀ:* `{query[:50]}`\n\nsᴇʟᴇᴄᴛ ᴀ sᴏɴɢ:"), parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(keyboard))

async def song_info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    chat_id = q.message.chat_id
    index = int(q.data.split("_")[1])
    results = search_cache.get(chat_id)
    if not results or index >= len(results):
        await q.edit_message_text(sc("⚠️ sᴏɴɢ ᴇxᴘɪʀᴇᴅ. ᴘʟᴇᴀsᴇ sᴇᴀʀᴄʜ ᴀɢᴀɪɴ."))
        return
    video = results[index]
    dur = f"{video['duration']//60}:{video['duration']%60:02d}" if video.get('duration') else "N/A"
    caption = sc(f"""
🎵 *{video['title'][:100]}*

👤 ᴀʀᴛɪsᴛ: `{video.get('uploader', 'Unknown')}`
⏱ ᴅᴜʀᴀᴛɪᴏɴ: `{dur}`
🔗 [ᴡᴀᴛᴄʜ ᴏɴ ʏᴏᴜᴛᴜʙᴇ]({video['url']})

ᴄʜᴏᴏsᴇ ᴀɴ ᴀᴄᴛɪᴏɴ:
""")
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton(sc("⬇️ ᴅᴏᴡɴʟᴏᴀᴅ ᴀᴜᴅɪᴏ"), callback_data=f"download_audio_{index}")],
        [InlineKeyboardButton(sc("📜 ʟʏʀɪᴄs"), callback_data=f"lyrics_{index}")],
        [InlineKeyboardButton(sc("🔙 ʙᴀᴄᴋ ᴛᴏ ʀᴇsᴜʟᴛs"), callback_data="page_0")]
    ])
    await q.edit_message_text(caption, parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard)

async def download_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer(sc("⬇️ sᴛᴀʀᴛɪɴɢ ᴅᴏᴡɴʟᴏᴀᴅ..."))
    user_id = q.from_user.id
    chat_id = q.message.chat_id
    index = int(q.data.split("_")[2])
    user = get_user(user_id)
    reset_downloads(user)

    can_download = False
    used_points = False

    if is_premium(user):
        can_download = True
    elif (user[3] if user else 0) < DOWNLOAD_LIMIT:
        can_download = True
    elif (user[1] if user else 0) >= POINTS_PER_DOWNLOAD:
        can_download = True
        used_points = True
    else:
        await q.message.reply_text(sc(f"⛔ ᴅᴏᴡɴʟᴏᴀᴅ ʟɪᴍɪᴛ ʀᴇᴀᴄʜᴇᴅ!\n\nʏᴏᴜ ʜᴀᴠᴇ ᴜsᴇᴅ ʏᴏᴜʀ {DOWNLOAD_LIMIT} ғʀᴇᴇ ᴅᴏᴡɴʟᴏᴀᴅs.\n💰 sᴘᴇɴᴅ {POINTS_PER_DOWNLOAD} ᴘᴏɪɴᴛs ғᴏʀ ᴀɴ ᴇxᴛʀᴀ ᴅᴏᴡɴʟᴏᴀᴅ, ᴏʀ ᴜᴘɢʀᴀᴅᴇ ᴛᴏ ᴘʀᴇᴍɪᴜᴍ."), parse_mode=ParseMode.MARKDOWN)
        return

    results = search_cache.get(chat_id)
    if not results or index >= len(results):
        await q.message.reply_text(sc("⚠️ sᴏɴɢ ᴇxᴘɪʀᴇᴅ. ᴘʟᴇᴀsᴇ sᴇᴀʀᴄʜ ᴀɢᴀɪɴ."))
        return

    video = results[index]
    status_msg = await q.message.reply_text(sc("⬇️ ᴅᴏᴡɴʟᴏᴀᴅ ɪɴ ᴘʀᴏɢʀᴇss... ᴘʟᴇᴀsᴇ ᴡᴀɪᴛ."), parse_mode=ParseMode.MARKDOWN)
    file_path = None
    success = False

    try:
        file_path, final_title = await download_audio_async(video["id"], video["title"], video["url"])
        if not file_path or not file_path.exists():
            await status_msg.edit_text(sc("❌ ᴅᴏᴡɴʟᴏᴀᴅ ғᴀɪʟᴇᴅ.\n\nᴀʟʟ ᴅᴏᴡɴʟᴏᴀᴅ ᴍᴇᴛʜᴏᴅs ᴀʀᴇ ᴄᴜʀʀᴇɴᴛʟʏ ᴜɴᴀᴠᴀɪʟᴀʙʟᴇ. ᴘʟᴇᴀsᴇ ᴛʀʏ ᴀɢᴀɪɴ ʟᴀᴛᴇʀ."), parse_mode=ParseMode.MARKDOWN)
            return

        await status_msg.edit_text(sc("📤 ᴜᴘʟᴏᴀᴅɪɴɢ..."), parse_mode=ParseMode.MARKDOWN)

        with open(file_path, "rb") as f:
            await q.message.reply_audio(
                audio=f,
                title=final_title[:100],
                performer=video.get("uploader", "Unknown")[:100],
                duration=video.get("duration", 0),
                caption=sc(f"🎵 *{final_title[:100]}*\n\n✅ ᴀᴜᴅɪᴏ ᴅᴏᴡɴʟᴏᴀᴅᴇᴅ sᴜᴄᴄᴇssғᴜʟʟʏ!"),
                parse_mode=ParseMode.MARKDOWN
            )
        success = True

    except Exception as e:
        logger.error(f"Download error: {e}")
        await status_msg.edit_text(sc(f"❌ ᴅᴏᴡɴʟᴏᴀᴅ ᴇʀʀᴏʀ\n\n`{str(e)[:200]}`"), parse_mode=ParseMode.MARKDOWN)
    finally:
        if file_path and file_path.exists():
            file_path.unlink(missing_ok=True)
        if success:
            try:
                await status_msg.delete()
            except:
                pass
            if not is_premium(user):
                increment_downloads(user_id)
            if used_points:
                deduct_points(user_id, POINTS_PER_DOWNLOAD)
            add_points(user_id, 1)

async def lyrics_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer(sc("📜 ғᴇᴛᴄʜɪɴɢ ʟʏʀɪᴄs..."))
    chat_id = q.message.chat_id
    index = int(q.data.split("_")[1])
    user_id = q.from_user.id
    user = get_user(user_id)

    can_view = False
    used_points = False

    if is_premium(user):
        can_view = True
    elif (user[1] if user else 0) >= POINTS_PER_LYRICS:
        can_view = True
        used_points = True
    else:
        await q.message.reply_text(sc(f"🔒 ʟʏʀɪᴄs ᴀʀᴇ ᴘʀᴇᴍɪᴜᴍ ᴏɴʟʏ!\n\n💰 sᴘᴇɴᴅ {POINTS_PER_LYRICS} ᴘᴏɪɴᴛs ᴛᴏ ᴠɪᴇᴡ ʟʏʀɪᴄs, ᴏʀ ᴜᴘɢʀᴀᴅᴇ ᴛᴏ ᴘʀᴇᴍɪᴜᴍ."), parse_mode=ParseMode.MARKDOWN)
        return

    results = search_cache.get(chat_id)
    if not results or index >= len(results):
        await q.message.reply_text(sc("⚠️ sᴏɴɢ ᴇxᴘɪʀᴇᴅ. ᴘʟᴇᴀsᴇ sᴇᴀʀᴄʜ ᴀɢᴀɪɴ."))
        return

    video = results[index]
    status = await q.message.reply_text(sc("🔎 sᴇᴀʀᴄʜɪɴɢ ʟʏʀɪᴄs..."), parse_mode=ParseMode.MARKDOWN)

    try:
        lyrics = await asyncio.get_event_loop().run_in_executor(None, fetch_lyrics, video["title"], video.get("uploader", ""))
        if not lyrics:
            await status.edit_text(sc("❌ ʟʏʀɪᴄs ɴᴏᴛ ғᴏᴜɴᴅ ɪɴ ᴏᴜʀ ᴅᴀᴛᴀʙᴀsᴇs."), parse_mode=ParseMode.MARKDOWN)
            return

        if len(lyrics) > 4000:
            lyrics = lyrics[:3997] + "..."

        await status.delete()
        await q.message.reply_text(
            sc(f"🎵 *{video['title'][:100]}*\n\n📜 *ʟʏʀɪᴄs:*\n\n```\n{lyrics}\n```"),
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(sc("⬇️ ᴅᴏᴡɴʟᴏᴀᴅ ᴀᴜᴅɪᴏ"), callback_data=f"download_audio_{index}")],
                [InlineKeyboardButton(sc("🔙 ʙᴀᴄᴋ ᴛᴏ ʀᴇsᴜʟᴛs"), callback_data="page_0")]
            ])
        )
        if used_points:
            deduct_points(user_id, POINTS_PER_LYRICS)
    except Exception as e:
        logger.error(f"Lyrics error: {e}")
        await status.edit_text(sc("❌ ғᴀɪʟᴇᴅ ᴛᴏ ғᴇᴛᴄʜ ʟʏʀɪᴄs."), parse_mode=ParseMode.MARKDOWN)

async def handle_social_download(update: Update, context: ContextTypes.DEFAULT_TYPE, url):
    user_id = update.effective_user.id
    user = get_user(user_id)

    if not is_premium(user):
        await update.message.reply_text(sc("🔒 sᴏᴄɪᴀʟ ᴍᴇᴅɪᴀ ᴠɪᴅᴇᴏ ᴅᴏᴡɴʟᴏᴀᴅ ɪs ᴀ ᴘʀᴇᴍɪᴜᴍ ғᴇᴀᴛᴜʀᴇ!\n\n💎 ᴜᴘɢʀᴀᴅᴇ ᴛᴏ ᴘʀᴇᴍɪᴜᴍ ᴛᴏ ᴅᴏᴡɴʟᴏᴀᴅ ᴠɪᴅᴇᴏs ғʀᴏᴍ ᴛɪᴋᴛᴏᴋ, ɪɴsᴛᴀɢʀᴀᴍ, ғᴀᴄᴇʙᴏᴏᴋ, ʏᴏᴜᴛᴜʙᴇ, ᴀɴᴅ ᴍᴏʀᴇ."), parse_mode=ParseMode.MARKDOWN)
        return

    platform = social_platform(url)
    status_msg = await update.message.reply_text(sc(f"🔎 ᴅᴇᴛᴇᴄᴛᴇᴅ: {platform}\n⏳ ᴘʀᴇᴘᴀʀɪɴɢ ʏᴏᴜʀ ᴠɪᴅᴇᴏ..."), parse_mode=ParseMode.MARKDOWN)
    temp_dir = tempfile.mkdtemp(prefix="social_dl_")
    file_path = None

    try:
        await status_msg.edit_text(sc(f"📥 ᴅᴏᴡɴʟᴏᴀᴅɪɴɢ {platform} ᴠɪᴅᴇᴏ...\nᴘʟᴇᴀsᴇ ᴡᴀɪᴛ."), parse_mode=ParseMode.MARKDOWN)
        file_path, info = await asyncio.to_thread(download_social_video, url, temp_dir)

        if not file_path or not file_path.exists():
            raise RuntimeError("No downloadable video was produced.")

        file_size = file_path.stat().st_size
        if file_size > 100 * 1024 * 1024:
            raise RuntimeError("Video is larger than 100 MB.")

        title = info.get("title") or "Social Media Video"
        await status_msg.edit_text(sc("📤 ᴜᴘʟᴏᴀᴅɪɴɢ ᴠɪᴅᴇᴏ ᴛᴏ ᴛᴇʟᴇɢʀᴀᴍ..."), parse_mode=ParseMode.MARKDOWN)

        caption = sc(f"🎬 {title[:700]}\n\n📦 {format_bytes(file_size)}\n🌐 {platform}")
        with file_path.open("rb") as video_file:
            await update.message.reply_video(
                video=video_file,
                caption=caption,
                supports_streaming=True,
                read_timeout=120,
                write_timeout=120,
                connect_timeout=30,
                pool_timeout=30
            )
        await status_msg.delete()

    except yt_dlp.utils.DownloadError as exc:
        logger.warning(f"yt-dlp download failed: {exc}")
        await status_msg.edit_text(sc("❌ ɪ ᴄᴏᴜʟᴅɴ'ᴛ ᴅᴏᴡɴʟᴏᴀᴅ ᴛʜᴀᴛ ᴠɪᴅᴇᴏ.\n\nᴛʜᴇ ᴘᴏsᴛ ᴍᴀʏ ʙᴇ ᴘʀɪᴠᴀᴛᴇ, ᴜɴᴀᴠᴀɪʟᴀʙʟᴇ, ᴀɢᴇ/ʟᴏɢɪɴ ʀᴇsᴛʀɪᴄᴛᴇᴅ, ᴅᴇʟᴇᴛᴇᴅ, ᴜɴsᴜᴘᴘᴏʀᴛᴇᴅ, ᴏʀ ᴛʜᴇ ᴘʟᴀᴛғᴏʀᴍ ᴍᴀʏ ʜᴀᴠᴇ ᴄʜᴀɴɢᴇᴅ."), parse_mode=ParseMode.MARKDOWN)
    except Exception as exc:
        logger.exception("Unexpected download error")
        error_text = str(exc)
        if "exceeds" in error_text.lower() or "larger" in error_text.lower():
            user_message = sc("❌ ᴛʜᴇ ᴠɪᴅᴇᴏ ɪs ʟᴀʀɢᴇʀ ᴛʜᴀɴ 100 ᴍʙ.")
        else:
            user_message = sc("❌ sᴏᴍᴇᴛʜɪɴɢ ᴡᴇɴᴛ ᴡʀᴏɴɢ ᴡʜɪʟᴇ ᴘʀᴏᴄᴇssɪɴɢ ᴛʜᴇ ᴠɪᴅᴇᴏ.")
        await status_msg.edit_text(user_message, parse_mode=ParseMode.MARKDOWN)
    finally:
        try:
            shutil.rmtree(temp_dir, ignore_errors=True)
        except:
            pass

async def more_tracks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    chat_id = q.message.chat_id
    offset = int(q.data.split("_")[1])
    results = search_cache.get(chat_id)
    if not results:
        await q.edit_message_text(sc("⚠️ sᴇᴀʀᴄʜ ᴇxᴘɪʀᴇᴅ. ᴘʟᴇᴀsᴇ sᴇᴀʀᴄʜ ᴀɢᴀɪɴ."))
        return
    keyboard = []
    for i in range(offset, min(offset + 20, len(results))):
        r = results[i]
        dur = f" ⏱{r['duration']//60}:{r['duration']%60:02d}" if r.get('duration') else ""
        title = r['title'][:45] + "..." if len(r['title']) > 45 else r['title']
        keyboard.append([InlineKeyboardButton(f"🎵 {title}{dur}", callback_data=f"song_{i}")])
    if offset + 20 < len(results):
        keyboard.append([InlineKeyboardButton(sc("➕ ᴍᴏʀᴇ ʀᴇsᴜʟᴛs"), callback_data=f"more_{offset+20}")])
    keyboard.append([InlineKeyboardButton(sc("🔙 ʙᴀᴄᴋ ᴛᴏ ᴍᴇɴᴜ"), callback_data="back_to_menu")])
    await q.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup(keyboard))

async def page_navigation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    chat_id = q.message.chat_id
    results = search_cache.get(chat_id)
    if not results:
        await q.edit_message_text(sc("⚠️ sᴇᴀʀᴄʜ ᴇxᴘɪʀᴇᴅ."))
        return
    keyboard = []
    for i, r in enumerate(results[:20]):
        dur = f" ⏱{r['duration']//60}:{r['duration']%60:02d}" if r.get('duration') else ""
        title = r['title'][:45] + "..." if len(r['title']) > 45 else r['title']
        keyboard.append([InlineKeyboardButton(f"🎵 {title}{dur}", callback_data=f"song_{i}")])
    if len(results) > 20:
        keyboard.append([InlineKeyboardButton(sc("➕ ᴍᴏʀᴇ ʀᴇsᴜʟᴛs"), callback_data="more_20")])
    keyboard.append([InlineKeyboardButton(sc("🔙 ʙᴀᴄᴋ ᴛᴏ ᴍᴇɴᴜ"), callback_data="back_to_menu")])
    await q.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup(keyboard))

async def back_to_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    user_id = q.from_user.id
    await q.edit_message_text(sc("🎵 ᴍᴀɪɴ ᴍᴇɴᴜ"), parse_mode=ParseMode.MARKDOWN)
    text = sc(f"🎵 ᴡᴇʟᴄᴏᴍᴇ ʙᴀᴄᴋ, {q.from_user.first_name}!\n\nᴡʜᴀᴛ ᴡᴏᴜʟᴅ ʏᴏᴜ ʟɪᴋᴇ ᴛᴏ ᴅᴏ?")
    await q.message.reply_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=get_main_menu_keyboard(user_id))

async def premium_invoice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    user_id = q.from_user.id
    data = q.data.split("_")
    days = int(data[1])
    stars = int(data[2])

    title = sc(f"💎 ᴘʀᴇᴍɪᴜᴍ — {days} ᴅᴀʏs")
    description = sc(f"ᴜɴʟᴏᴄᴋ ᴜɴʟɪᴍɪᴛᴇᴅ ᴅᴏᴡɴʟᴏᴀᴅs, ʟʏʀɪᴄs, ᴀɴᴅ sᴏᴄɪᴀʟ ᴠɪᴅᴇᴏ ᴅᴏᴡɴʟᴏᴀᴅs ғᴏʀ {days} ᴅᴀʏs.")
    payload = f"premium_{user_id}_{days}_{stars}"

    try:
        await context.bot.send_invoice(
            chat_id=user_id,
            title=title,
            description=description,
            payload=payload,
            provider_token="",
            currency="XTR",
            prices=[LabeledPrice(label=sc(f"{days} ᴅᴀʏs ᴘʀᴇᴍɪᴜᴍ"), amount=stars)],
            start_parameter=f"premium_{days}"
        )
    except Exception as e:
        logger.error(f"Invoice error: {e}")
        await q.message.reply_text(sc("❌ ғᴀɪʟᴇᴅ ᴛᴏ ᴄʀᴇᴀᴛᴇ ɪɴᴠᴏɪᴄᴇ. ᴛʀʏ ᴀɢᴀɪɴ ʟᴀᴛᴇʀ."), parse_mode=ParseMode.MARKDOWN)

async def precheckout_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.pre_checkout_query
    if query.invoice_payload.startswith("premium_"):
        await query.answer(ok=True)
    else:
        await query.answer(ok=False, error_message=sc("sᴏᴍᴇᴛʜɪɴɢ ᴡᴇɴᴛ ᴡʀᴏɴɢ ᴡɪᴛʜ ʏᴏᴜʀ ᴘᴀʏᴍᴇɴᴛ."))

async def successful_payment_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    payload = update.message.successful_payment.invoice_payload
    parts = payload.split("_")
    if len(parts) >= 4 and parts[0] == "premium":
        days = int(parts[2])
        expire = grant_premium(user.id, days)
        await update.message.reply_text(
            sc(f"🎉 ᴘᴀʏᴍᴇɴᴛ sᴜᴄᴄᴇssғᴜʟ!\n\n💎 ʏᴏᴜ ᴀʀᴇ ɴᴏᴡ ᴀ ᴘʀᴇᴍɪᴜᴍ ᴜsᴇʀ!\n📅 ᴇxᴘɪʀᴇs: {expire.strftime('%Y-%m-%d %H:%M')}\n\nᴇɴᴊᴏʏ ᴜɴʟɪᴍɪᴛᴇᴅ ᴅᴏᴡɴʟᴏᴀᴅs, ʟʏʀɪᴄs, ᴀɴᴅ ᴠɪᴅᴇᴏ ᴅᴏᴡɴʟᴏᴀᴅs! 🚀"),
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=get_main_menu_keyboard(user.id)
        )
        if CHANNEL_ID:
            await notify_channel(context,
                f"⭐ *ɴᴇᴡ ᴘʀᴇᴍɪᴜᴍ ᴜsᴇʀ*\n\n"
                f"👤 ɴᴀᴍᴇ: {user.first_name or 'Unknown'}\n"
                f"📛 ᴜsᴇʀɴᴀᴍᴇ: @{user.username or 'None'}\n"
                f"🆔 ᴜsᴇʀ ɪᴅ: {user.id}\n"
                f"📅 ᴅᴀʏs: {days}\n"
                f"⏰ ᴇxᴘɪʀᴇ: {expire.strftime('%Y-%m-%d %H:%M:%S')}\n"
                f"👤 ɢʀᴀɴᴛᴇᴅ ʙʏ: ᴛᴇʟᴇɢʀᴀᴍ sᴛᴀʀs\n\n"
                f"🚀 [sᴛᴀʀᴛ ʙᴏᴛ](https://t.me/{BOT_USERNAME}?start={user.id}) | 💎 ɢᴇᴛ ᴘʀᴇᴍɪᴜᴍ"
            )

async def admin_premium(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return
    try:
        user = int(context.args[0])
        days = int(context.args[1])
        expire = grant_premium(user, days)
        await context.bot.send_message(user, sc(f"🎉 ᴄᴏɴɢʀᴀᴛᴜʟᴀᴛɪᴏɴs!\nʏᴏᴜ ʜᴀᴠᴇ ʙᴇᴇɴ ᴀᴡᴀʀᴅᴇᴅ ᴘʀᴇᴍɪᴜᴍ!\n📅 ᴇxᴘɪʀᴇs: {expire.strftime('%Y-%m-%d %H:%M')}"))
        await update.message.reply_text(sc(f"✅ ᴘʀᴇᴍɪᴜᴍ ɢʀᴀɴᴛᴇᴅ ᴛᴏ ᴜsᴇʀ {user} ғᴏʀ {days} ᴅᴀʏs."))
        if CHANNEL_ID:
            u = get_user(user)
            await notify_channel(context,
                f"⭐ *ɴᴇᴡ ᴘʀᴇᴍɪᴜᴍ ᴜsᴇʀ*\n\n"
                f"👤 ɴᴀᴍᴇ: {u[6] or 'Unknown'}\n"
                f"📛 ᴜsᴇʀɴᴀᴍᴇ: @{u[7] or 'None'}\n"
                f"🆔 ᴜsᴇʀ ɪᴅ: {user}\n"
                f"📅 ᴅᴀʏs: {days}\n"
                f"⏰ ᴇxᴘɪʀᴇ: {expire.strftime('%Y-%m-%d %H:%M:%S')}\n"
                f"👤 ɢʀᴀɴᴛᴇᴅ ʙʏ: ᴏᴡɴᴇʀ\n\n"
                f"🚀 [sᴛᴀʀᴛ ʙᴏᴛ](https://t.me/{BOT_USERNAME}?start={user}) | 💎 ɢᴇᴛ ᴘʀᴇᴍɪᴜᴍ"
            )
    except:
        await update.message.reply_text(sc("❌ ᴜsᴀɢᴇ: /premium <ᴜsᴇʀ_ɪᴅ> <ᴅᴀʏs>"))

async def admin_reward(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return
    try:
        user = int(context.args[0])
        pts = int(context.args[1])
        add_points(user, pts)
        await context.bot.send_message(user, sc(f"🎉 ᴄᴏɴɢʀᴀᴛᴜʟᴀᴛɪᴏɴs!\nʏᴏᴜ ʜᴀᴠᴇ ʙᴇᴇɴ ᴀᴡᴀʀᴅᴇᴅ {pts} ᴘᴏɪɴᴛs!"))
        await update.message.reply_text(sc(f"✅ ᴀᴡᴀʀᴅᴇᴅ {pts} ᴘᴏɪɴᴛs ᴛᴏ ᴜsᴇʀ {user}."))
    except:
        await update.message.reply_text(sc("❌ ᴜsᴀɢᴇ: /reward <ᴜsᴇʀ_ɪᴅ> <ᴘᴏɪɴᴛs>"))

async def admin_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return
    cursor.execute("SELECT COUNT(*) FROM users")
    users = cursor.fetchone()[0]
    cursor.execute("SELECT COUNT(*) FROM users WHERE premium_expire > datetime('now')")
    premium_users = cursor.fetchone()[0]
    await update.message.reply_text(
        sc(f"📊 ʙᴏᴛ sᴛᴀᴛɪsᴛɪᴄs\n\n👥 ᴛᴏᴛᴀʟ ᴜsᴇʀs: {users}\n💎 ᴘʀᴇᴍɪᴜᴍ ᴜsᴇʀs: {premium_users}"),
        parse_mode=ParseMode.MARKDOWN
    )

async def admin_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return
    if not update.message.reply_to_message:
        await update.message.reply_text(sc("📢 ʀᴇᴘʟʏ ᴛᴏ ᴀ ᴍᴇssᴀɢᴇ (ᴛᴇxᴛ/ᴘʜᴏᴛᴏ/ᴠɪᴅᴇᴏ) ᴛᴏ ʙʀᴏᴀᴅᴄᴀsᴛ."))
        return
    msg = update.message.reply_to_message
    cursor.execute("SELECT id FROM users")
    users = [u[0] for u in cursor.fetchall()]
    delivered = 0
    failed = 0
    status = await update.message.reply_text(sc(f"📢 ʙʀᴏᴀᴅᴄᴀsᴛɪɴɢ ᴛᴏ {len(users)} ᴜsᴇʀs..."))
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
        except Exception as e:
            logger.error(f"Broadcast failed for {u}: {e}")
            failed += 1
    await status.edit_text(
        sc(f"📢 *ʙʀᴏᴀᴅᴄᴀsᴛ ᴄᴏᴍᴘʟᴇᴛᴇ*\n\n👥 ᴛᴏᴛᴀʟ: {len(users)}\n✅ ᴅᴇʟɪᴠᴇʀᴇᴅ: {delivered}\n❌ ғᴀɪʟᴇᴅ: {failed}"),
        parse_mode=ParseMode.MARKDOWN
    )

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.error(f"Update {update} caused error: {context.error}")
    if update and update.effective_message:
        try:
            await update.effective_message.reply_text(
                sc("❌ ᴏᴏᴘs! sᴏᴍᴇᴛʜɪɴɢ ᴡᴇɴᴛ ᴡʀᴏɴɢ.\n\nᴘʟᴇᴀsᴇ ᴛʀʏ ᴀɢᴀɪɴ ʟᴀᴛᴇʀ ᴏʀ ᴄᴏɴᴛᴀᴄᴛ sᴜᴘᴘᴏʀᴛ."),
                parse_mode=ParseMode.MARKDOWN
            )
        except:
            pass

def main():
    print("""
╔══════════════════════════════════════════════════════════════╗
║           ADVANCED MUSIC BOT                                 ║
║   Created by Mr DarkHacker                                   ║
╚══════════════════════════════════════════════════════════════╝
    """)
    if not COOKIES_FILE.exists():
        print(f"⚠️ No cookie file found at: {COOKIES_FILE}")
        print("   (yt-dlp fallback may be limited)\n")
    print("🤖 Bot is running... Press Ctrl+C to stop.\n")

    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("account", show_account))
    app.add_handler(CommandHandler("trending", show_trending))
    app.add_handler(CommandHandler("help", show_help))
    app.add_handler(CommandHandler("premium", admin_premium))
    app.add_handler(CommandHandler("reward", admin_reward))
    app.add_handler(CommandHandler("stats", admin_stats))
    app.add_handler(CommandHandler("broadcast", admin_broadcast))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_menu_buttons))

    app.add_handler(CallbackQueryHandler(back_to_menu, pattern="^back_to_menu$"))
    app.add_handler(CallbackQueryHandler(song_info, pattern="^song_"))
    app.add_handler(CallbackQueryHandler(download_callback, pattern="^download_audio_"))
    app.add_handler(CallbackQueryHandler(lyrics_callback, pattern="^lyrics_"))
    app.add_handler(CallbackQueryHandler(more_tracks, pattern="^more_"))
    app.add_handler(CallbackQueryHandler(page_navigation, pattern="^page_"))
    app.add_handler(CallbackQueryHandler(premium_invoice, pattern="^premium_"))

    app.add_handler(PreCheckoutQueryHandler(precheckout_callback))
    app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment_callback))

    app.add_error_handler(error_handler)

    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
