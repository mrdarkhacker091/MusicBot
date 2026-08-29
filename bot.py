#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
ADVANCED MUSIC BOT - PRODUCTION REFACTOR
Created by Mr DarkHacker
"""

import os, sys, time, asyncio, logging, datetime, hashlib, urllib.parse, json, re, shutil, tempfile, subprocess
from pathlib import Path
from html import escape
from dataclasses import dataclass
from typing import Optional, Tuple, List, Dict, Any

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import aiosqlite
import yt_dlp
import requests
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, LabeledPrice
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes, PreCheckoutQueryHandler, filters
from telegram.constants import ParseMode
from telegram.error import RetryAfter, Forbidden, BadRequest

# ===== CONFIGURATION =====
TOKEN = os.getenv("BOT_TOKEN")
if not TOKEN:
    raise RuntimeError("BOT_TOKEN environment variable is required")

OWNER_ID = int(os.getenv("OWNER_ID", "0"))
BOT_USERNAME = os.getenv("BOT_USERNAME", "")
CHANNEL_ID = os.getenv("CHANNEL_ID", "")
ALYA_API_KEY = os.getenv("ALYA_API_KEY", "G7I6X7")

DOWNLOAD_LIMIT = 5
POINTS_PER_REFERRAL = 10
POINTS_PER_DOWNLOAD = 10
POINTS_PER_LYRICS = 5
MAX_CONCURRENT_DOWNLOADS = 3
MAX_SOCIAL_SIZE_MB = 100
MAX_SONG_VIDEO_SIZE_MB = 50
CACHE_TTL_SECONDS = 15 * 60

BASE_DIR = Path(__file__).parent.resolve()
DB_PATH = BASE_DIR / "bot.db"
DOWNLOADS_DIR = BASE_DIR / "downloads"
COOKIES_DIR = BASE_DIR / "cookies"
for d in [DOWNLOADS_DIR, COOKIES_DIR]:
    d.mkdir(parents=True, exist_ok=True)
COOKIES_FILE = COOKIES_DIR / "cookies.txt"

# ===== LOGGING =====
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO
)
logger = logging.getLogger("music_bot")

# ===== SAFE SMALL CAPS =====
_SC_MAP = str.maketrans(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ",
    "ᴀʙᴄᴅᴇғɢʜɪᴊᴋʟᴍɴᴏᴘǫʀsᴛᴜᴠᴡxʏᴢᴀʙᴄᴅᴇғɢʜɪᴊᴋʟᴍɴᴏᴘǫʀsᴛᴜᴠᴡxʏᴢ"
)

_PROTECTED_RE = re.compile(
    r'(https?://[^\s<>"{}|\\^`\[\]]+'
    r'|t\.me/[^\s<>"{}|\\^`\[\]]+'
    r'|@[A-Za-z0-9_]{3,}'
    r'|/[A-Za-z0-9_]+(?:@[A-Za-z0-9_]+)?'
    r'|<[^>]+>)',
    re.IGNORECASE
)

def safe_sc(text: str) -> str:
    if not text:
        return ""
    protected: List[str] = []
    def protect(m: re.Match) -> str:
        protected.append(m.group(0))
        return f"\x00{len(protected)-1}\x00"
    text = _PROTECTED_RE.sub(protect, text)
    text = text.translate(_SC_MAP)
    for i, val in enumerate(protected):
        text = text.replace(f"\x00{i}\x00", val)
    return text

def h(text: Any) -> str:
    return escape(str(text) if text is not None else "")

# ===== DATABASE =====
@dataclass
class User:
    id: int
    points: int
    premium_expire: Optional[str]
    downloads: int
    download_date: Optional[str]
    referrer: Optional[int]
    first_name: Optional[str]
    username: Optional[str]
    joined_date: Optional[str]

async def init_db() -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY,
                points INTEGER DEFAULT 0,
                premium_expire TEXT,
                downloads INTEGER DEFAULT 0,
                download_date TEXT,
                referrer INTEGER,
                first_name TEXT,
                username TEXT,
                joined_date TEXT
            )
        """)
        await db.commit()

async def get_user(user_id: int) -> Optional[User]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM users WHERE id = ?", (user_id,)) as cursor:
            row = await cursor.fetchone()
            if row:
                return User(**dict(row))
            return None

async def create_user(user_id: int, first_name: str, username: str, referrer: Optional[int] = None) -> None:
    now = datetime.datetime.now().isoformat()
    today = datetime.date.today().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO users (id, points, premium_expire, downloads, download_date, referrer, first_name, username, joined_date)
            VALUES (?, 0, NULL, 0, ?, ?, ?, ?, ?)
        """, (user_id, today, referrer, first_name, username, now))
        await db.commit()

async def update_user_profile(user_id: int, first_name: str, username: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET first_name = ?, username = ? WHERE id = ?", (first_name, username, user_id))
        await db.commit()

async def add_points(user_id: int, points: int) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET points = points + ? WHERE id = ?", (points, user_id))
        await db.commit()

async def deduct_points_atomic(user_id: int, amount: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("UPDATE users SET points = points - ? WHERE id = ? AND points >= ?", (amount, user_id, amount))
        await db.commit()
        return cursor.rowcount == 1

async def reserve_download(user_id: int, today: str, limit: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("""
            UPDATE users 
            SET downloads = CASE WHEN download_date != ? THEN 1 ELSE downloads + 1 END,
                download_date = ?
            WHERE id = ?
            AND (download_date != ? OR downloads < ?)
        """, (today, today, user_id, today, limit))
        await db.commit()
        return cursor.rowcount == 1

async def refund_download(user_id: int, today: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            UPDATE users SET downloads = downloads - 1 
            WHERE id = ? AND download_date = ? AND downloads > 0
        """, (user_id, today))
        await db.commit()

def is_premium(user: Optional[User]) -> bool:
    if not user or not user.premium_expire:
        return False
    try:
        return datetime.datetime.fromisoformat(user.premium_expire) > datetime.datetime.now()
    except Exception:
        return False

async def extend_premium(user_id: int, days: int) -> datetime.datetime:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT premium_expire FROM users WHERE id = ?", (user_id,)) as cursor:
            row = await cursor.fetchone()

        now = datetime.datetime.now()
        if row and row["premium_expire"]:
            try:
                current = datetime.datetime.fromisoformat(row["premium_expire"])
                base = current if current > now else now
            except Exception:
                base = now
        else:
            base = now

        new_expire = base + datetime.timedelta(days=days)
        await db.execute("UPDATE users SET premium_expire = ? WHERE id = ?", (new_expire.isoformat(), user_id))
        await db.commit()
        return new_expire

# ===== CONCURRENCY =====
DOWNLOAD_SEM = asyncio.Semaphore(MAX_CONCURRENT_DOWNLOADS)

# ===== CACHE =====
search_cache: Dict[int, Dict[str, Any]] = {}
trending_cache: Dict[str, Any] = {"data": [], "timestamp": 0}

def clean_cache() -> None:
    now = time.time()
    expired = [k for k, v in search_cache.items() if now - v.get("timestamp", 0) > CACHE_TTL_SECONDS]
    for k in expired:
        del search_cache[k]

# ===== KEYBOARD =====
async def get_main_menu_keyboard(user_id: int) -> ReplyKeyboardMarkup:
    user = await get_user(user_id)
    points = user.points if user else 0
    downloads = user.downloads if user else 0
    premium = is_premium(user)
    prem_text = safe_sc("💎 Premium") if premium else safe_sc("⭐ Upgrade")
    keyboard = [
        [safe_sc("🎵 Search Music"), safe_sc("🔥 Trending")],
        [safe_sc(f"📊 Account ({points} pts)"), safe_sc(f"⬇️ {downloads}/{DOWNLOAD_LIMIT}")],
        [safe_sc("🔗 Referral"), safe_sc("🤖 Other Bots")],
        [prem_text, safe_sc("❓ Help")],
        [safe_sc("📞 Contact")]
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

# ===== AUDIO DOWNLOADER (PRESERVED APIs) =====
def get_audio_download_url(youtube_url: str) -> Tuple[str, str]:
    encoded = urllib.parse.quote_plus(youtube_url)
    apis = [
        ("EliteProTech", lambda: requests.get(f"https://eliteprotech-apis.zone.id/ytdown?url={encoded}&format=mp3", timeout=15),
         lambda d: (d.get("downloadURL"), d.get("title")) if d.get("success") else (None, None)),
        ("DavidCyril", lambda: requests.get(f"https://apis.davidcyril.name.ng/youtube/mp3?url={encoded}", timeout=15),
         lambda d: (d.get("result", {}).get("download_url"), d.get("result", {}).get("title")) if d.get("status") else (None, None)),
        ("Alya", lambda: requests.get(f"https://api.alyachan.pro/api/ytmp3?url={encoded}&apikey={ALYA_API_KEY}", timeout=15),
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
                logger.warning(f"[{name}] HTTP {r.status_code}")
                continue
            data = r.json()
            url, title = extract(data)
            if url and url.startswith(("http://", "https://")):
                logger.info(f"[{name}] SUCCESS: got download URL")
                return url, title or ""
        except Exception as e:
            logger.warning(f"[{name}] FAILED: {type(e).__name__}: {e}")

    logger.info("All external APIs failed. Trying yt-dlp local fallback...")
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
    raise RuntimeError("All download methods failed")

def _validate_audio_response(response: requests.Response) -> None:
    ct = response.headers.get("Content-Type", "").lower()
    if ct and not (ct.startswith("audio/") or "octet-stream" in ct or "video" in ct):
        logger.warning(f"Unexpected content-type: {ct}")

def _detect_extension_from_response(response: requests.Response) -> str:
    ct = response.headers.get("Content-Type", "").lower()
    if "mpeg" in ct or "mp3" in ct:
        return ".mp3"
    if "m4a" in ct or "mp4" in ct:
        return ".m4a"
    if "webm" in ct:
        return ".webm"
    if "ogg" in ct:
        return ".ogg"
    if "opus" in ct:
        return ".opus"
    return ".mp3"

def _convert_to_mp3(input_path: Path, output_path: Path) -> bool:
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-i", str(input_path), "-codec:a", "libmp3lame", "-q:a", "2", str(output_path)],
            check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=120
        )
        return output_path.exists() and output_path.stat().st_size > 1024
    except Exception as e:
        logger.warning(f"ffmpeg conversion failed: {e}")
        return False

async def download_audio_async(video_id: str, title: str, youtube_url: str) -> Tuple[Optional[Path], Optional[str]]:
    async with DOWNLOAD_SEM:
        loop = asyncio.get_running_loop()
        file_id = hashlib.md5(f"{video_id}{time.time()}".encode()).hexdigest()[:12]
        temp_dir = tempfile.mkdtemp(prefix="audio_")
        out_path = Path(temp_dir) / f"{file_id}.mp3"

        def _download() -> Tuple[Optional[Path], Optional[str]]:
            try:
                download_url, final_title = get_audio_download_url(youtube_url)
                if download_url.startswith("file://"):
                    src = Path(download_url[7:])
                    if src.exists():
                        ext = _detect_extension_from_response(requests.head(str(src), timeout=10, allow_redirects=True))
                        if ext != ".mp3" and _convert_to_mp3(src, out_path):
                            src.unlink(missing_ok=True)
                            return out_path, final_title or title
                        shutil.copy2(src, out_path)
                        src.unlink(missing_ok=True)
                        return out_path, final_title or title

                logger.info(f"Downloading audio from: {download_url[:80]}...")
                r = requests.get(download_url, timeout=120, stream=True, headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                    "Accept": "*/*", "Accept-Encoding": "gzip, deflate, br", "Connection": "keep-alive"
                })
                if r.status_code not in (200, 206):
                    raise RuntimeError(f"HTTP {r.status_code}")

                _validate_audio_response(r)
                ext = _detect_extension_from_response(r)
                raw_path = Path(temp_dir) / f"{file_id}{ext}"

                with open(raw_path, "wb") as f:
                    for chunk in r.iter_content(chunk_size=65536):
                        if chunk:
                            f.write(chunk)

                if not raw_path.exists() or raw_path.stat().st_size < 50 * 1024:
                    raise RuntimeError("File too small or missing")

                if ext != ".mp3":
                    if _convert_to_mp3(raw_path, out_path):
                        raw_path.unlink(missing_ok=True)
                        return out_path, final_title or title
                    else:
                        out_path = raw_path

                return out_path, final_title or title
            except Exception as e:
                logger.error(f"Audio download error: {e}")
                return None, None

        try:
            result = await asyncio.wait_for(loop.run_in_executor(None, _download), timeout=180.0)
            return result
        except asyncio.TimeoutError:
            logger.error("Audio download timed out")
            return None, None
        except Exception as e:
            logger.error(f"Audio download crashed: {e}")
            return None, None
        finally:
            if out_path.exists() and out_path.parent != Path(temp_dir):
                shutil.rmtree(temp_dir, ignore_errors=True)

# ===== SONG VIDEO DOWNLOADER =====
async def download_song_video_async(video_id: str, title: str, youtube_url: str) -> Tuple[Optional[Path], Optional[str]]:
    async with DOWNLOAD_SEM:
        loop = asyncio.get_running_loop()
        file_id = hashlib.md5(f"vid{video_id}{time.time()}".encode()).hexdigest()[:12]
        temp_dir = tempfile.mkdtemp(prefix="songvid_")
        out_template = str(Path(temp_dir) / f"{file_id}.%(ext)s")

        def _download() -> Tuple[Optional[Path], Optional[str]]:
            try:
                opts = {
                    "quiet": True, "no_warnings": True, "noplaylist": True,
                    "format": f"best[filesize<{MAX_SONG_VIDEO_SIZE_MB}M][ext=mp4]/best[filesize<{MAX_SONG_VIDEO_SIZE_MB}M]/best[height<=480][ext=mp4]/best[height<=480]/worst[ext=mp4]/worst",
                    "outtmpl": out_template,
                    "merge_output_format": "mp4",
                    "max_filesize": MAX_SONG_VIDEO_SIZE_MB * 1024 * 1024,
                    "retries": 2, "fragment_retries": 2,
                    "socket_timeout": 30
                }
                if COOKIES_FILE.exists():
                    opts["cookies"] = str(COOKIES_FILE)
                with yt_dlp.YoutubeDL(opts) as ydl:
                    info = ydl.extract_info(youtube_url, download=True)
                    final_title = info.get("title", title)

                files = [p for p in Path(temp_dir).rglob("*") if p.is_file() and p.suffix.lower() in {".mp4", ".mkv", ".webm", ".mov", ".m4v"}]
                if not files:
                    raise RuntimeError("No video file produced")

                best = max(files, key=lambda p: p.stat().st_size)
                if best.stat().st_size > MAX_SONG_VIDEO_SIZE_MB * 1024 * 1024:
                    raise RuntimeError(f"Video exceeds {MAX_SONG_VIDEO_SIZE_MB}MB")
                return best, final_title
            except Exception as e:
                logger.error(f"Song video download error: {e}")
                return None, None

        try:
            return await asyncio.wait_for(loop.run_in_executor(None, _download), timeout=180.0)
        except asyncio.TimeoutError:
            logger.error("Song video download timed out")
            return None, None
        except Exception as e:
            logger.error(f"Song video download crashed: {e}")
            return None, None

# ===== LYRICS FETCHER =====
def fetch_lyrics(title: str, artist: str = "") -> Optional[str]:
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
    except Exception as e:
        logger.debug(f"lyrics.ovh failed: {e}")

    try:
        search_term = f"{artist} {title}".strip()
        url = f"https://lrclib.net/api/search?q={requests.utils.quote(search_term)}"
        r = requests.get(url, timeout=10)
        if r.status_code == 200:
            data = r.json()
            if data and len(data) > 0 and data[0].get("plainLyrics"):
                return data[0]["plainLyrics"]
    except Exception as e:
        logger.debug(f"lrclib failed: {e}")

    try:
        url = f"https://api.lyrics.kashishmusic.in/lyrics?title={requests.utils.quote(title)}&artist={requests.utils.quote(artist)}"
        r = requests.get(url, timeout=10)
        if r.status_code == 200:
            data = r.json()
            if data.get("lyrics"):
                return data["lyrics"]
    except Exception as e:
        logger.debug(f"kashishmusic failed: {e}")

    return None

# ===== SOCIAL MEDIA DOWNLOADER =====
SOCIAL_DOMAINS = {
    "tiktok.com", "vm.tiktok.com", "vt.tiktok.com",
    "instagram.com", "www.instagram.com",
    "facebook.com", "www.facebook.com", "fb.watch",
    "youtube.com", "www.youtube.com", "youtu.be",
    "x.com", "www.x.com", "twitter.com", "www.twitter.com",
    "reddit.com", "www.reddit.com", "redd.it"
}
URL_PATTERN = re.compile(r'https?://[^\s<>"{}|\\^`\[\]]+', re.IGNORECASE)

def extract_url(text: str) -> Optional[str]:
    if not text:
        return None
    match = URL_PATTERN.search(text)
    return match.group(0).rstrip(".,!?)]}") if match else None

def is_social_url(url: str) -> bool:
    try:
        hostname = urllib.parse.urlparse(url).hostname
        if not hostname:
            return False
        hostname = hostname.lower().rstrip(".")
        return any(hostname == d or hostname.endswith("." + d) for d in SOCIAL_DOMAINS)
    except Exception:
        return False

def social_platform(url: str) -> str:
    try:
        h = urllib.parse.urlparse(url).hostname or ""
        h = h.lower()
        if "tiktok" in h: return "TikTok"
        if "instagram" in h: return "Instagram"
        if "facebook" in h or h == "fb.watch": return "Facebook"
        if "youtube" in h or h == "youtu.be": return "YouTube"
        if "twitter" in h or h == "x.com": return "X/Twitter"
        if "reddit" in h: return "Reddit"
    except Exception:
        pass
    return "Social Media"

def format_bytes(size: int) -> str:
    if size < 1024: return f"{size} B"
    if size < 1024 * 1024: return f"{size / 1024:.1f} KB"
    return f"{size / (1024 * 1024):.1f} MB"

async def download_social_video(url: str, temp_dir: str) -> Tuple[Optional[Path], Optional[Dict[str, Any]]]:
    def _download() -> Tuple[Optional[Path], Optional[Dict[str, Any]]]:
        try:
            out_path = Path(temp_dir)
            template = str(out_path / "download.%(ext)s")
            ydl_opts = {
                "format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
                "outtmpl": template, "merge_output_format": "mp4", "noplaylist": True,
                "writethumbnail": False, "writesubtitles": False, "writeautomaticsub": False,
                "quiet": True, "no_warnings": True, "retries": 2, "fragment_retries": 2,
                "max_filesize": MAX_SOCIAL_SIZE_MB * 1024 * 1024,
                "socket_timeout": 30
            }
            if COOKIES_FILE.exists():
                ydl_opts["cookies"] = str(COOKIES_FILE)
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
            files = [p for p in out_path.rglob("*") if p.is_file() and p.suffix.lower() in {".mp4", ".mkv", ".webm", ".mov", ".m4v"}]
            if not files:
                raise RuntimeError("No video file produced")
            best = max(files, key=lambda p: p.stat().st_size)
            if best.stat().st_size > MAX_SOCIAL_SIZE_MB * 1024 * 1024:
                raise RuntimeError(f"Video exceeds {MAX_SOCIAL_SIZE_MB}MB")
            return best, info
        except Exception as e:
            logger.error(f"Social download error: {e}")
            return None, None

    loop = asyncio.get_running_loop()
    try:
        return await asyncio.wait_for(loop.run_in_executor(None, _download), timeout=180.0)
    except asyncio.TimeoutError:
        logger.error("Social download timed out")
        return None, None
    except Exception as e:
        logger.error(f"Social download crashed: {e}")
        return None, None

# ===== MUSIC SEARCH =====
def search_music(query: str, max_results: int = 20) -> List[Dict[str, Any]]:
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

# ===== TRENDING (COUNTRY + GENRE) =====
TRENDING_COUNTRIES = {
    "nigeria": "🇳🇬 Nigeria",
    "ghana": "🇬🇭 Ghana",
    "usa": "🇺🇸 United States",
    "uk": "🇬🇧 United Kingdom",
    "south_africa": "🇿🇦 South Africa",
    "jamaica": "🇯🇲 Jamaica",
    "kenya": "🇰🇪 Kenya",
    "tanzania": "🇹🇿 Tanzania",
}

TRENDING_GENRES = {
    "afrobeat": "🎵 Afrobeat",
    "hiphop": "🎤 Hip Hop",
    "fuji": "🎸 Fuji",
    "gospel": "🙏 Gospel",
    "highlife": "🎶 Highlife",
    "streetpop": "🔥 Street Pop",
    "afropop": "🎼 Afropop",
    "other": "🌍 Other",
}

def build_trending_query(country: str, genre: str) -> str:
    mapping = {
        "nigeria": "Nigerian",
        "ghana": "Ghanaian",
        "usa": "American",
        "uk": "UK",
        "south_africa": "South African",
        "jamaica": "Jamaican",
        "kenya": "Kenyan",
        "tanzania": "Tanzanian",
    }
    nationality = mapping.get(country, country.title())
    return f"{nationality} {genre} official music video latest"

# ===== NOTIFICATIONS =====
async def notify_channel(context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    if not CHANNEL_ID:
        return
    try:
        await context.bot.send_message(CHANNEL_ID, text, parse_mode=ParseMode.HTML)
    except Exception as e:
        logger.error(f"Channel notify failed: {e}")

# ===== PREMIUM PLANS =====
PREMIUM_PLANS = {
    7: 50,
    30: 150,
    90: 400,
}

# ===== HANDLERS =====

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    user_id = user.id
    ref = None
    if context.args:
        try:
            ref = int(context.args[0])
        except Exception:
            pass

    existing = await get_user(user_id)
    is_new = existing is None

    if is_new:
        await create_user(user_id, user.first_name or "Unknown", user.username or "None", ref)
    else:
        await update_user_profile(user_id, user.first_name or "Unknown", user.username or "None")

    if is_new and ref and ref != user_id:
        ref_user = await get_user(ref)
        if ref_user:
            await add_points(ref, POINTS_PER_REFERRAL)
            try:
                ref_name = ref_user.first_name or "User"
                await context.bot.send_message(
                    ref,
                    safe_sc(f"🎉 ʜᴇʏ {ref_name}!\n\nʏᴏᴜ ᴊᴜsᴛ ɢᴏᴛ ᴀ ɴᴇᴡ ʀᴇғᴇʀʀᴀʟ!\n\n"
                            f"👤 ɴᴀᴍᴇ: {user.first_name or 'Unknown'}\n"
                            f"📛 ᴜsᴇʀɴᴀᴍᴇ: @{user.username or 'None'}\n"
                            f"🆔 ᴜsᴇʀ ɪᴅ: {user_id}\n"
                            f"📅 ᴅᴀᴛᴇ: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
                            f"✅ ʏᴏᴜ ᴇᴀʀɴᴇᴅ +{POINTS_PER_REFERRAL} ᴘᴏɪɴᴛs!"),
                    parse_mode=ParseMode.HTML
                )
            except Exception as e:
                logger.warning(f"Referral notify failed: {e}")

    if is_new and CHANNEL_ID:
        ref_by = "ᴅɪʀᴇᴄᴛ ᴊᴏɪɴ"
        if ref:
            ref_u = await get_user(ref)
            if ref_u and ref_u.username and ref_u.username != "None":
                ref_by = f"@{ref_u.username}"
            elif ref_u and ref_u.first_name:
                ref_by = ref_u.first_name
        await notify_channel(context,
            f"📥 <b>ɴᴇᴡ ᴜsᴇʀ ᴊᴏɪɴᴇᴅ</b>\n\n"
            f"👤 ɴᴀᴍᴇ: {h(user.first_name)}\n"
            f"📛 ᴜsᴇʀɴᴀᴍᴇ: @{h(user.username)}\n"
            f"🆔 ᴜsᴇʀ ɪᴅ: {user_id}\n"
            f"📅 ᴅᴀᴛᴇ: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"👥 ʀᴇғᴇʀʀᴇᴅ ʙʏ: {ref_by}\n\n"
            f"🤖 <a href=\"https://t.me/{BOT_USERNAME}?start={user_id}\">sᴛᴀʀᴛ ʙᴏᴛ</a>"
        )

    text = safe_sc(f"""
🎵 ᴡᴇʟᴄᴏᴍᴇ ᴛᴏ ᴀᴅᴠᴀɴᴄᴇᴅ ᴍᴜsɪᴄ ʙᴏᴛ!

👋 ʜɪ {user.first_name or 'ғʀɪᴇɴᴅ'}!

ɪ ᴄᴀɴ ʜᴇʟᴘ ʏᴏᴜ ғɪɴᴅ ᴀɴᴅ ᴅᴏᴡɴʟᴏᴀᴅ ᴍᴜsɪᴄ ғʀᴏᴍ ʏᴏᴜᴛᴜʙᴇ.

✨ ғᴇᴀᴛᴜʀᴇs:
• 🎵 sᴇᴀʀᴄʜ ᴀɴʏ sᴏɴɢ
• ⬇️ ᴅᴏᴡɴʟᴏᴀᴅ ᴍᴘ3 ᴀᴜᴅɪᴏ
• 🎬 ᴅᴏᴡɴʟᴏᴀᴅ sᴏɴɢ ᴠɪᴅᴇᴏ
• 📜 ɢᴇᴛ sᴏɴɢ ʟʏʀɪᴄs (ᴘʀᴇᴍɪᴜᴍ)
• 🔗 ʀᴇғᴇʀ ғʀɪᴇɴᴅs & ᴇᴀʀɴ ᴘᴏɪɴᴛs
• 💎 ᴘʀᴇᴍɪᴜᴍ ғᴏʀ ᴜɴʟɪᴍɪᴛᴇᴅ ᴅᴏᴡɴʟᴏᴀᴅs
• 🎬 sᴏᴄɪᴀʟ ᴍᴇᴅɪᴀ ᴠɪᴅᴇᴏ ᴅᴏᴡɴʟᴏᴀᴅᴇʀ

ᴜsᴇ ᴛʜᴇ ʙᴜᴛᴛᴏɴs ʙᴇʟᴏᴡ ᴛᴏ ɢᴇᴛ sᴛᴀʀᴛᴇᴅ!
""")
    await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=await get_main_menu_keyboard(user_id))

async def handle_menu_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    text = update.message.text or ""

    if safe_sc("🎵 Search Music") in text or text == "🎵 Search Music":
        await update.message.reply_text(
            safe_sc('🎵 sᴇɴᴅ ᴍᴇ ᴀ sᴏɴɢ ɴᴀᴍᴇ ᴏʀ ᴀʀᴛɪsᴛ ᴛᴏ sᴇᴀʀᴄʜ!\n\n<i>ᴇxᴀᴍᴘʟᴇ: "ᴄᴀʟᴍ ᴅᴏᴡɴ ʀᴇᴍᴀ"</i>'),
            parse_mode=ParseMode.HTML, reply_markup=await get_main_menu_keyboard(user_id)
        )
    elif safe_sc("🔥 Trending") in text or text == "🔥 Trending":
        await show_trending_countries(update, context)
    elif safe_sc("📊 Account") in text or text == "📊 Account":
        await show_account(update, context)
    elif safe_sc("⬇️") in text or text.startswith("⬇️"):
        user = await get_user(user_id)
        today = datetime.date.today().isoformat()
        downloads = user.downloads if user and user.download_date == today else 0
        remaining = DOWNLOAD_LIMIT - downloads
        await update.message.reply_text(
            safe_sc(f"📊 ᴅᴏᴡɴʟᴏᴀᴅ ᴜsᴀɢᴇ\n\nᴛᴏᴅᴀʏ: {downloads}/{DOWNLOAD_LIMIT}\nʀᴇᴍᴀɪɴɪɴɢ: {remaining}\nᴘʀᴇᴍɪᴜᴍ: {'✅ ᴀᴄᴛɪᴠᴇ' if is_premium(user) else '❌ ɴᴏᴛ ᴀᴄᴛɪᴠᴇ'}"),
            parse_mode=ParseMode.HTML, reply_markup=await get_main_menu_keyboard(user_id)
        )
    elif safe_sc("🔗 Referral") in text or text == "🔗 Referral":
        await show_referral(update, context)
    elif safe_sc("🤖 Other Bots") in text or text == "🤖 Other Bots":
        await show_other_bots(update, context)
    elif safe_sc("⭐ Upgrade") in text or text == "⭐ Upgrade" or safe_sc("💎 Premium") in text or text == "💎 Premium":
        await show_premium(update, context)
    elif safe_sc("❓ Help") in text or text == "❓ Help":
        await show_help(update, context)
    elif safe_sc("📞 Contact") in text or text == "📞 Contact":
        await show_contact(update, context)
    else:
        url = extract_url(update.message.text)
        if url and is_social_url(url):
            await handle_social_download(update, context, url)
        else:
            await song_search(update, context)

async def show_account(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    user = await get_user(user_id)
    today = datetime.date.today().isoformat()
    points = user.points if user else 0
    downloads = user.downloads if user and user.download_date == today else 0
    premium_status = safe_sc("💎 ᴀᴄᴛɪᴠᴇ") if is_premium(user) else safe_sc("❌ ɪɴᴀᴄᴛɪᴠᴇ")
    text = safe_sc(f"""
👤 ʏᴏᴜʀ ᴀᴄᴄᴏᴜɴᴛ

💰 ᴘᴏɪɴᴛs: {points}
⬇️ ᴅᴏᴡɴʟᴏᴀᴅs ᴛᴏᴅᴀʏ: {downloads}/{DOWNLOAD_LIMIT}
💎 ᴘʀᴇᴍɪᴜᴍ: {premium_status}

1 ʀᴇғᴇʀʀᴀʟ = {POINTS_PER_REFERRAL} ᴘᴏɪɴᴛs
{POINTS_PER_DOWNLOAD} ᴘᴏɪɴᴛs = 1 ᴇxᴛʀᴀ ᴅᴏᴡɴʟᴏᴀᴅ
{POINTS_PER_LYRICS} ᴘᴏɪɴᴛs = 1 ʟʏʀɪᴄs sᴇᴀʀᴄʜ

ɪɴᴠɪᴛᴇ ғʀɪᴇɴᴅs ᴀɴᴅ ᴇᴀʀɴ ᴘᴏɪɴᴛs!
""")
    await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=await get_main_menu_keyboard(user_id))

async def show_referral(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    link = f"https://t.me/{BOT_USERNAME}?start={user_id}"
    text = safe_sc(f"""
🔗 ʏᴏᴜʀ ʀᴇғᴇʀʀᴀʟ ʟɪɴᴋ

sʜᴀʀᴇ ᴛʜɪs ʟɪɴᴋ ᴡɪᴛʜ ʏᴏᴜʀ ғʀɪᴇɴᴅs:

<code>{link}</code>

✨ ʜᴏᴡ ɪᴛ ᴡᴏʀᴋs:
• ᴇᴀᴄʜ ғʀɪᴇɴᴅ ᴡʜᴏ ᴊᴏɪɴs ɢɪᴠᴇs ʏᴏᴜ +{POINTS_PER_REFERRAL} ᴘᴏɪɴᴛs!
• ᴜsᴇ ᴘᴏɪɴᴛs ᴛᴏ ᴜɴʟᴏᴄᴋ ᴇxᴛʀᴀ ᴅᴏᴡɴʟᴏᴀᴅs ᴀɴᴅ ʟʏʀɪᴄs!

<i>ᴛᴀᴘ ᴀɴᴅ ʜᴏʟᴅ ᴛᴏ ᴄᴏᴘʏ ᴛʜᴇ ʟɪɴᴋ</i>
""")
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton(safe_sc("📤 sʜᴀʀᴇ ʟɪɴᴋ"), url=f"https://t.me/share/url?url={link}&text=🎵%20ɢᴇᴛ%20ᴍᴜsɪᴄ%20ғᴏʀ%20ғʀᴇᴇ!")]
    ])
    await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=keyboard)

async def show_other_bots(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = safe_sc("""
🤖 ᴏᴜʀ ᴏᴛʜᴇʀ ʙᴏᴛs

<b>@DarkHacker_BanBot</b> — ɢʜᴏsᴛsʜᴇʟʟ ᴍᴅ
ᴀ ᴍᴜʟᴛɪᴘᴜʀᴘᴏsᴇ ᴡʜᴀᴛsᴀᴘᴘ ʙᴏᴛ ᴡɪᴛʜ ᴀɴᴛɪᴅᴇʟᴇᴛᴇ, ᴡᴀʟʟᴘᴀᴘᴇʀs, ᴍᴇᴅɪᴀ ᴛᴏᴏʟs, ɢʀᴏᴜᴘ ᴍᴀɴᴀɢᴇᴍᴇɴᴛ, ᴀɪ ᴄʜᴀᴛ, ᴅᴏᴡɴʟᴏᴀᴅᴇʀs, ᴀɴᴅ ᴍᴏʀᴇ.

<b>@WormGPT_Prover_Bot</b>
[ᴅᴇsᴄʀɪᴘᴛɪᴏɴ ᴡɪʟʟ ʙᴇ ᴀᴅᴅᴇᴅ ʙʏ ᴏᴡɴᴇʀ]

<b>@Whatsapp2_Ban_bot</b>
[ᴅᴇsᴄʀɪᴘᴛɪᴏɴ ᴡɪʟʟ ʙᴇ ᴀᴅᴅᴇᴅ ʙʏ ᴏᴡɴᴇʀ]

<b>@Image_ConverterTo_linkBot</b>
ᴄᴏɴᴠᴇʀᴛ ɪᴍᴀɢᴇs, ᴠɪᴅᴇᴏs, ғɪʟᴇs, ᴀɴᴅ ᴠᴏɪᴄᴇ ɴᴏᴛᴇs ɪɴᴛᴏ ᴅɪʀᴇᴄᴛ ʟɪɴᴋs. ᴜᴘʟᴏᴀᴅ ᴀɴʏ ᴍᴇᴅɪᴀ ᴀɴᴅ ɢᴇᴛ ᴀ sʜᴀʀᴀʙʟᴇ ʟɪɴᴋ ɪɴsᴛᴀɴᴛʟʏ.

<i>ᴍᴏʀᴇ ʙᴏᴛs ᴄᴏᴍɪɴɢ sᴏᴏɴ!</i>
""")
    await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=await get_main_menu_keyboard(update.effective_user.id))

async def show_premium(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    user = await get_user(user_id)
    if is_premium(user):
        expire = "N/A"
        if user and user.premium_expire:
            try:
                expire = datetime.datetime.fromisoformat(user.premium_expire).strftime("%Y-%m-%d %H:%M")
            except Exception:
                pass
        text = safe_sc(f"💎 ᴘʀᴇᴍɪᴜᴍ ᴀᴄᴛɪᴠᴇ\n\n📅 ᴇxᴘɪʀᴇs: {expire}\n⬇️ ᴜɴʟɪᴍɪᴛᴇᴅ ᴅᴏᴡɴʟᴏᴀᴅs\n📜 ᴜɴʟɪᴍɪᴛᴇᴅ ʟʏʀɪᴄs\n🎬 ᴜɴʟɪᴍɪᴛᴇᴅ ᴠɪᴅᴇᴏs\n\nᴛʜᴀɴᴋ ʏᴏᴜ ғᴏʀ sᴜᴘᴘᴏʀᴛɪɴɢ ᴛʜᴇ ʙᴏᴛ! 🙏")
        await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=await get_main_menu_keyboard(user_id))
        return

    text = safe_sc("""
💎 ᴜᴘɢʀᴀᴅᴇ ᴛᴏ ᴘʀᴇᴍɪᴜᴍ

✨ ʙᴇɴᴇғɪᴛs:
• ⬇️ ᴜɴʟɪᴍɪᴛᴇᴅ ᴀᴜᴅɪᴏ ᴅᴏᴡɴʟᴏᴀᴅs
• 🎬 ᴜɴʟɪᴍɪᴛᴇᴅ sᴏɴɢ ᴠɪᴅᴇᴏs
• 🚀 ᴘʀɪᴏʀɪᴛʏ ᴅᴏᴡɴʟᴏᴀᴅ sᴘᴇᴇᴅ
• 🎵 ʜᴅ ᴀᴜᴅɪᴏ ǫᴜᴀʟɪᴛʏ
• 📜 ᴜɴʟɪᴍɪᴛᴇᴅ ʟʏʀɪᴄs ᴀᴄᴄᴇss
• 🚫 ɴᴏ ᴄᴏᴏʟᴅᴏᴡɴ ᴘᴇʀɪᴏᴅs
• 🎬 sᴏᴄɪᴀʟ ᴍᴇᴅɪᴀ ᴠɪᴅᴇᴏ ᴅᴏᴡɴʟᴏᴀᴅs

ᴄʜᴏᴏsᴇ ʏᴏᴜʀ ᴘʟᴀɴ ʙᴇʟᴏᴡ:
""")
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton(safe_sc("⭐ 7 ᴅᴀʏs — 50 sᴛᴀʀs"), callback_data="premium_7")],
        [InlineKeyboardButton(safe_sc("⭐ 30 ᴅᴀʏs — 150 sᴛᴀʀs"), callback_data="premium_30")],
        [InlineKeyboardButton(safe_sc("⭐ 90 ᴅᴀʏs — 400 sᴛᴀʀs"), callback_data="premium_90")],
        [InlineKeyboardButton(safe_sc("📞 ᴄᴏɴᴛᴀᴄᴛ ᴏᴡɴᴇʀ"), url="https://t.me/Mr_Unique_Hacker002")]
    ])
    await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=keyboard)

async def show_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = safe_sc(f"""
❓ ʜᴇʟᴘ ɢᴜɪᴅᴇ

<b>ʜᴏᴡ ᴛᴏ ᴜsᴇ:</b>
1. sᴇɴᴅ ᴀ sᴏɴɢ ɴᴀᴍᴇ ᴏʀ ᴀʀᴛɪsᴛ
2. ᴄʟɪᴄᴋ ᴏɴ ᴀ ʀᴇsᴜʟᴛ
3. ᴄʜᴏᴏsᴇ ᴅᴏᴡɴʟᴏᴀᴅ ᴀᴜᴅɪᴏ, ᴠɪᴅᴇᴏ, ᴏʀ ʟʏʀɪᴄs

<b>ᴄᴏᴍᴍᴀɴᴅs:</b>
/start — sᴛᴀʀᴛ ᴛʜᴇ ʙᴏᴛ
/account — ᴠɪᴇᴡ ʏᴏᴜʀ ᴘʀᴏғɪʟᴇ
/trending — ᴛʀᴇɴᴅɪɴɢ sᴏɴɢs
/help — ᴛʜɪs ʜᴇʟᴘ ᴍᴇssᴀɢᴇ

<b>ғʀᴇᴇ ᴜsᴇʀs:</b>
• {DOWNLOAD_LIMIT} ᴀᴜᴅɪᴏ ᴅᴏᴡɴʟᴏᴀᴅs ᴘᴇʀ ᴅᴀʏ
• ʟʏʀɪᴄs ʟᴏᴄᴋᴇᴅ (ᴘʀᴇᴍɪᴜᴍ ᴏɴʟʏ)
• sᴏɴɢ ᴠɪᴅᴇᴏs ʟᴏᴄᴋᴇᴅ (ᴘʀᴇᴍɪᴜᴍ ᴏɴʟʏ)
• sᴏᴄɪᴀʟ ᴍᴇᴅɪᴀ ᴅᴏᴡɴʟᴏᴀᴅs ʟᴏᴄᴋᴇᴅ
• ᴜsᴇ ᴘᴏɪɴᴛs ғᴏʀ ᴇxᴛʀᴀ ғᴇᴀᴛᴜʀᴇs

<b>ᴘʀᴇᴍɪᴜᴍ:</b>
• ᴜɴʟɪᴍɪᴛᴇᴅ ᴇᴠᴇʀʏᴛʜɪɴɢ
• ɴᴏ ᴡᴀɪᴛ ᴛɪᴍᴇs

ᴄᴏɴᴛᴀᴄᴛ @Mr_Unique_Hacker002 ғᴏʀ sᴜᴘᴘᴏʀᴛ
""")
    await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=await get_main_menu_keyboard(update.effective_user.id))

async def show_contact(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("📩 Telegram", url="https://t.me/Mr_Unique_Hacker002")],
        [InlineKeyboardButton("📱 WhatsApp", url="https://wa.me/2349123578884")]
    ])
    await update.message.reply_text(
        safe_sc("📞 ᴄᴏɴᴛᴀᴄᴛ ᴜs\n\nʀᴇᴀᴄʜ ᴏᴜᴛ ᴛᴏ ᴜs ᴏɴ:"),
        parse_mode=ParseMode.HTML, reply_markup=keyboard
    )

async def song_search(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    query = (update.message.text or "").strip()
    if len(query) < 2:
        await update.message.reply_text(
            safe_sc("⚠️ ᴘʟᴇᴀsᴇ ᴇɴᴛᴇʀ ᴀᴛ ʟᴇᴀsᴛ 2 ᴄʜᴀʀᴀᴄᴛᴇʀs ᴛᴏ sᴇᴀʀᴄʜ."),
            reply_markup=await get_main_menu_keyboard(user_id)
        )
        return

    clean_cache()
    msg = await update.message.reply_text(safe_sc("🔎 sᴇᴀʀᴄʜɪɴɢ..."), parse_mode=ParseMode.HTML)
    results = await asyncio.to_thread(search_music, query)

    if not results:
        await msg.edit_text(
            safe_sc("❌ ɴᴏ ʀᴇsᴜʟᴛs ғᴏᴜɴᴅ.\n\n💡 ᴛʀʏ ᴅɪғғᴇʀᴇɴᴛ ᴋᴇʏᴡᴏʀᴅs ᴏʀ ᴀʀᴛɪsᴛ ɴᴀᴍᴇ."),
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(safe_sc("🔙 ʙᴀᴄᴋ ᴛᴏ ᴍᴇɴᴜ"), callback_data="back_to_menu")]])
        )
        return

    chat_id = update.message.chat_id
    search_cache[chat_id] = {"results": results, "timestamp": time.time()}
    keyboard = []
    for i, r in enumerate(results[:20]):
        dur = f" ⏱{r['duration']//60}:{r['duration']%60:02d}" if r.get('duration') else ""
        title = r['title'][:45] + "..." if len(r['title']) > 45 else r['title']
        keyboard.append([InlineKeyboardButton(f"🎵 {title}{dur}", callback_data=f"song_{i}")])
    if len(results) > 20:
        keyboard.append([InlineKeyboardButton(safe_sc("➕ ᴍᴏʀᴇ ʀᴇsᴜʟᴛs"), callback_data="more_20")])
    keyboard.append([InlineKeyboardButton(safe_sc("🔙 ʙᴀᴄᴋ ᴛᴏ ᴍᴇɴᴜ"), callback_data="back_to_menu")])

    await msg.edit_text(
        safe_sc(f"🎵 <b>ʀᴇsᴜʟᴛs ғᴏʀ:</b> <code>{h(query[:50])}</code>\n\nsᴇʟᴇᴄᴛ ᴀ sᴏɴɢ:"),
        parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def song_info(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    chat_id = q.message.chat_id
    index = int(q.data.split("_")[1])
    cached = search_cache.get(chat_id)
    if not cached or time.time() - cached.get("timestamp", 0) > CACHE_TTL_SECONDS:
        await q.edit_message_text(safe_sc("⚠️ sᴇᴀʀᴄʜ ᴇxᴘɪʀᴇᴅ. ᴘʟᴇᴀsᴇ sᴇᴀʀᴄʜ ᴀɢᴀɪɴ."))
        return
    results = cached["results"]
    if index >= len(results):
        await q.edit_message_text(safe_sc("⚠️ ɪɴᴠᴀʟɪᴅ sᴏɴɢ sᴇʟᴇᴄᴛɪᴏɴ."))
        return

    video = results[index]
    dur = f"{video['duration']//60}:{video['duration']%60:02d}" if video.get('duration') else "N/A"
    caption = safe_sc(f"""
🎵 <b>{h(video['title'][:100])}</b>

👤 ᴀʀᴛɪsᴛ: <code>{h(video.get('uploader', 'Unknown'))}</code>
⏱ ᴅᴜʀᴀᴛɪᴏɴ: <code>{dur}</code>
🔗 <a href=\"{h(video['url'])}\">ᴡᴀᴛᴄʜ ᴏɴ ʏᴏᴜᴛᴜʙᴇ</a>

ᴄʜᴏᴏsᴇ ᴀɴ ᴀᴄᴛɪᴏɴ ʙᴇʟᴏᴡ:
""")
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton(safe_sc("⬇️ ᴅᴏᴡɴʟᴏᴀᴅ ᴀᴜᴅɪᴏ"), callback_data=f"dl_audio_{index}")],
        [InlineKeyboardButton(safe_sc("🎬 ᴅᴏᴡɴʟᴏᴀᴅ ᴠɪᴅᴇᴏ"), callback_data=f"dl_video_{index}")],
        [InlineKeyboardButton(safe_sc("📜 ʟʏʀɪᴄs"), callback_data=f"lyrics_{index}")],
        [InlineKeyboardButton(safe_sc("🔙 ʙᴀᴄᴋ ᴛᴏ ʀᴇsᴜʟᴛs"), callback_data="page_0")]
    ])
    await q.edit_message_text(caption, parse_mode=ParseMode.HTML, reply_markup=keyboard)

async def download_audio_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer(safe_sc("⬇️ sᴛᴀʀᴛɪɴɢ ᴀᴜᴅɪᴏ ᴅᴏᴡɴʟᴏᴀᴅ..."))
    user_id = q.from_user.id
    chat_id = q.message.chat_id
    index = int(q.data.split("_")[2])
    today = datetime.date.today().isoformat()

    user = await get_user(user_id)
    can_download = False
    used_points = False

    if is_premium(user):
        can_download = True
    elif await reserve_download(user_id, today, DOWNLOAD_LIMIT):
        can_download = True
    elif await deduct_points_atomic(user_id, POINTS_PER_DOWNLOAD):
        can_download = True
        used_points = True
    else:
        await q.message.reply_text(
            safe_sc(f"⛔ ᴅᴏᴡɴʟᴏᴀᴅ ʟɪᴍɪᴛ ʀᴇᴀᴄʜᴇᴅ!\n\nʏᴏᴜ ʜᴀᴠᴇ ᴜsᴇᴅ ʏᴏᴜʀ {DOWNLOAD_LIMIT} ғʀᴇᴇ ᴅᴏᴡɴʟᴏᴀᴅs.\n💰 sᴘᴇɴᴅ {POINTS_PER_DOWNLOAD} ᴘᴏɪɴᴛs ғᴏʀ ᴀɴ ᴇxᴛʀᴀ ᴅᴏᴡɴʟᴏᴀᴅ, ᴏʀ ᴜᴘɢʀᴀᴅᴇ ᴛᴏ ᴘʀᴇᴍɪᴜᴍ."),
            parse_mode=ParseMode.HTML
        )
        return

    cached = search_cache.get(chat_id)
    if not cached or time.time() - cached.get("timestamp", 0) > CACHE_TTL_SECONDS:
        if not is_premium(user) and not used_points:
            await refund_download(user_id, today)
        await q.message.reply_text(safe_sc("⚠️ sᴏɴɢ ᴇxᴘɪʀᴇᴅ. ᴘʟᴇᴀsᴇ sᴇᴀʀᴄʜ ᴀɢᴀɪɴ."))
        return

    results = cached["results"]
    if index >= len(results):
        if not is_premium(user) and not used_points:
            await refund_download(user_id, today)
        await q.message.reply_text(safe_sc("⚠️ ɪɴᴠᴀʟɪᴅ sᴏɴɢ sᴇʟᴇᴄᴛɪᴏɴ."))
        return

    video = results[index]
    status_msg = await q.message.reply_text(
        safe_sc("⬇️ ᴅᴏᴡɴʟᴏᴀᴅ ɪɴ ᴘʀᴏɢʀᴇss... ᴘʟᴇᴀsᴇ ᴡᴀɪᴛ."),
        parse_mode=ParseMode.HTML
    )
    file_path = None
    success = False

    try:
        file_path, final_title = await download_audio_async(video["id"], video["title"], video["url"])
        if not file_path or not file_path.exists():
            await status_msg.edit_text(
                safe_sc("❌ ᴅᴏᴡɴʟᴏᴀᴅ ғᴀɪʟᴇᴅ.\n\nᴀʟʟ ᴅᴏᴡɴʟᴏᴀᴅ ᴍᴇᴛʜᴏᴅs ᴀʀᴇ ᴄᴜʀʀᴇɴᴛʟʏ ᴜɴᴀᴠᴀɪʟᴀʙʟᴇ. ᴘʟᴇᴀsᴇ ᴛʀʏ ᴀɢᴀɪɴ ʟᴀᴛᴇʀ."),
                parse_mode=ParseMode.HTML
            )
            return

        await status_msg.edit_text(safe_sc("📤 ᴜᴘʟᴏᴀᴅɪɴɢ..."), parse_mode=ParseMode.HTML)

        with open(file_path, "rb") as f:
            await q.message.reply_audio(
                audio=f,
                title=final_title[:100] if final_title else video['title'][:100],
                performer=video.get("uploader", "Unknown")[:100],
                duration=video.get("duration", 0),
                caption=safe_sc(f"🎵 <b>{h((final_title or video['title'])[:100])}</b>\n\n✅ ᴀᴜᴅɪᴏ ᴅᴏᴡɴʟᴏᴀᴅᴇᴅ sᴜᴄᴄᴇssғᴜʟʟʏ!"),
                parse_mode=ParseMode.HTML
            )
        success = True

    except Exception as e:
        logger.error(f"Download callback error: {e}")
        await status_msg.edit_text(
            safe_sc("❌ ᴅᴏᴡɴʟᴏᴀᴅ ᴇʀʀᴏʀ\n\nᴘʟᴇᴀsᴇ ᴛʀʏ ᴀɢᴀɪɴ ʟᴀᴛᴇʀ."),
            parse_mode=ParseMode.HTML
        )
    finally:
        if file_path and file_path.exists():
            try:
                shutil.rmtree(file_path.parent, ignore_errors=True)
            except Exception:
                file_path.unlink(missing_ok=True)
        if success:
            try:
                await status_msg.delete()
            except Exception:
                pass
            if used_points:
                pass
            else:
                await add_points(user_id, 1)
        else:
            if not is_premium(user) and not used_points:
                await refund_download(user_id, today)
            if used_points:
                await add_points(user_id, POINTS_PER_DOWNLOAD)

async def download_video_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer(safe_sc("🎬 sᴛᴀʀᴛɪɴɢ ᴠɪᴅᴇᴏ ᴅᴏᴡɴʟᴏᴀᴅ..."))
    user_id = q.from_user.id
    chat_id = q.message.chat_id
    index = int(q.data.split("_")[2])

    user = await get_user(user_id)
    if not is_premium(user):
        await q.message.reply_text(
            safe_sc("🔒 sᴏɴɢ ᴠɪᴅᴇᴏ ᴅᴏᴡɴʟᴏᴀᴅ ɪs ᴀ ᴘʀᴇᴍɪᴜᴍ ғᴇᴀᴛᴜʀᴇ!\n\n💎 ᴜᴘɢʀᴀᴅᴇ ᴛᴏ ᴘʀᴇᴍɪᴜᴍ ᴛᴏ ᴅᴏᴡɴʟᴏᴀᴅ sᴏɴɢ ᴠɪᴅᴇᴏs."),
            parse_mode=ParseMode.HTML
        )
        return

    cached = search_cache.get(chat_id)
    if not cached or time.time() - cached.get("timestamp", 0) > CACHE_TTL_SECONDS:
        await q.message.reply_text(safe_sc("⚠️ sᴏɴɢ ᴇxᴘɪʀᴇᴅ. ᴘʟᴇᴀsᴇ sᴇᴀʀᴄʜ ᴀɢᴀɪɴ."))
        return

    results = cached["results"]
    if index >= len(results):
        await q.message.reply_text(safe_sc("⚠️ ɪɴᴠᴀʟɪᴅ sᴏɴɢ sᴇʟᴇᴄᴛɪᴏɴ."))
        return

    video = results[index]
    status_msg = await q.message.reply_text(
        safe_sc("🎬 ᴠɪᴅᴇᴏ ᴅᴏᴡɴʟᴏᴀᴅ ɪɴ ᴘʀᴏɢʀᴇss... ᴘʟᴇᴀsᴇ ᴡᴀɪᴛ."),
        parse_mode=ParseMode.HTML
    )
    file_path = None
    success = False

    try:
        file_path, final_title = await download_song_video_async(video["id"], video["title"], video["url"])
        if not file_path or not file_path.exists():
            await status_msg.edit_text(
                safe_sc("❌ ᴠɪᴅᴇᴏ ᴅᴏᴡɴʟᴏᴀᴅ ғᴀɪʟᴇᴅ.\n\nᴛʜᴇ ᴠɪᴅᴇᴏ ᴍᴀʏ ʙᴇ ᴛᴏᴏ ʟᴀʀɢᴇ ᴏʀ ᴜɴᴀᴠᴀɪʟᴀʙʟᴇ."),
                parse_mode=ParseMode.HTML
            )
            return

        await status_msg.edit_text(safe_sc("📤 ᴜᴘʟᴏᴀᴅɪɴɢ ᴠɪᴅᴇᴏ..."), parse_mode=ParseMode.HTML)

        with open(file_path, "rb") as f:
            await q.message.reply_video(
                video=f,
                caption=safe_sc(f"🎬 <b>{h((final_title or video['title'])[:100])}</b>\n\n✅ ᴠɪᴅᴇᴏ ᴅᴏᴡɴʟᴏᴀᴅᴇᴅ sᴜᴄᴄᴇssғᴜʟʟʏ!"),
                supports_streaming=True,
                read_timeout=120, write_timeout=120,
                parse_mode=ParseMode.HTML
            )
        success = True

    except Exception as e:
        logger.error(f"Video download callback error: {e}")
        await status_msg.edit_text(
            safe_sc("❌ ᴠɪᴅᴇᴏ ᴅᴏᴡɴʟᴏᴀᴅ ᴇʀʀᴏʀ\n\nᴘʟᴇᴀsᴇ ᴛʀʏ ᴀɢᴀɪɴ ʟᴀᴛᴇʀ."),
            parse_mode=ParseMode.HTML
        )
    finally:
        if file_path and file_path.exists():
            try:
                shutil.rmtree(file_path.parent, ignore_errors=True)
            except Exception:
                file_path.unlink(missing_ok=True)
        if success:
            try:
                await status_msg.delete()
            except Exception:
                pass

async def lyrics_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer(safe_sc("📜 ғᴇᴛᴄʜɪɴɢ ʟʏʀɪᴄs..."))
    chat_id = q.message.chat_id
    index = int(q.data.split("_")[1])
    user_id = q.from_user.id
    user = await get_user(user_id)

    can_view = False
    used_points = False

    if is_premium(user):
        can_view = True
    elif await deduct_points_atomic(user_id, POINTS_PER_LYRICS):
        can_view = True
        used_points = True
    else:
        await q.message.reply_text(
            safe_sc(f"🔒 ʟʏʀɪᴄs ᴀʀᴇ ᴘʀᴇᴍɪᴜᴍ ᴏɴʟʏ!\n\n💰 sᴘᴇɴᴅ {POINTS_PER_LYRICS} ᴘᴏɪɴᴛs ᴛᴏ ᴠɪᴇᴡ ʟʏʀɪᴄs, ᴏʀ ᴜᴘɢʀᴀᴅᴇ ᴛᴏ ᴘʀᴇᴍɪᴜᴍ."),
            parse_mode=ParseMode.HTML
        )
        return

    cached = search_cache.get(chat_id)
    if not cached or time.time() - cached.get("timestamp", 0) > CACHE_TTL_SECONDS:
        if used_points:
            await add_points(user_id, POINTS_PER_LYRICS)
        await q.message.reply_text(safe_sc("⚠️ sᴏɴɢ ᴇxᴘɪʀᴇᴅ. ᴘʟᴇᴀsᴇ sᴇᴀʀᴄʜ ᴀɢᴀɪɴ."))
        return

    results = cached["results"]
    if index >= len(results):
        if used_points:
            await add_points(user_id, POINTS_PER_LYRICS)
        await q.message.reply_text(safe_sc("⚠️ ɪɴᴠᴀʟɪᴅ sᴏɴɢ sᴇʟᴇᴄᴛɪᴏɴ."))
        return

    video = results[index]
    status = await q.message.reply_text(safe_sc("🔎 sᴇᴀʀᴄʜɪɴɢ ʟʏʀɪᴄs..."), parse_mode=ParseMode.HTML)

    try:
        lyrics = await asyncio.to_thread(fetch_lyrics, video["title"], video.get("uploader", ""))
        if not lyrics:
            await status.edit_text(
                safe_sc("❌ ʟʏʀɪᴄs ɴᴏᴛ ғᴏᴜɴᴅ ɪɴ ᴏᴜʀ ᴅᴀᴛᴀʙᴀsᴇs."),
                parse_mode=ParseMode.HTML
            )
            return

        if len(lyrics) > 4000:
            lyrics = lyrics[:3997] + "..."

        await status.delete()
        await q.message.reply_text(
            safe_sc(f"🎵 <b>{h(video['title'][:100])}</b>\n\n📜 <b>ʟʏʀɪᴄs:</b>\n\n<pre>{h(lyrics)}</pre>"),
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(safe_sc("⬇️ ᴅᴏᴡɴʟᴏᴀᴅ ᴀᴜᴅɪᴏ"), callback_data=f"dl_audio_{index}")],
                [InlineKeyboardButton(safe_sc("🔙 ʙᴀᴄᴋ ᴛᴏ ʀᴇsᴜʟᴛs"), callback_data="page_0")]
            ])
        )
    except Exception as e:
        logger.error(f"Lyrics callback error: {e}")
        await status.edit_text(safe_sc("❌ ғᴀɪʟᴇᴅ ᴛᴏ ғᴇᴛᴄʜ ʟʏʀɪᴄs."), parse_mode=ParseMode.HTML)
        if used_points:
            await add_points(user_id, POINTS_PER_LYRICS)

# ===== TRENDING FLOW =====
async def show_trending_countries(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    keyboard = []
    for code, label in TRENDING_COUNTRIES.items():
        keyboard.append([InlineKeyboardButton(safe_sc(label), callback_data=f"trend_country_{code}")])
    keyboard.append([InlineKeyboardButton(safe_sc("🔙 ʙᴀᴄᴋ ᴛᴏ ᴍᴇɴᴜ"), callback_data="back_to_menu")])

    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text(
            safe_sc("🔥 <b>ᴘᴏᴘᴜʟᴀʀ ᴍᴜsɪᴄ</b>\n\nsᴇʟᴇᴄᴛ ᴀ ᴄᴏᴜɴᴛʀʏ:"),
            parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(keyboard)
        )
    else:
        await update.message.reply_text(
            safe_sc("🔥 <b>ᴘᴏᴘᴜʟᴀʀ ᴍᴜsɪᴄ</b>\n\nsᴇʟᴇᴄᴛ ᴀ ᴄᴏᴜɴᴛʀʏ:"),
            parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(keyboard)
        )

async def show_trending_genres(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    country = q.data.split("_")[2]

    keyboard = []
    for code, label in TRENDING_GENRES.items():
        keyboard.append([InlineKeyboardButton(safe_sc(label), callback_data=f"trend_genre_{country}_{code}")])
    keyboard.append([InlineKeyboardButton(safe_sc("🔙 ʙᴀᴄᴋ"), callback_data="trend_back_countries")])

    country_label = TRENDING_COUNTRIES.get(country, country.title())
    await q.edit_message_text(
        safe_sc(f"🔥 <b>{h(country_label)}</b>\n\nᴄʜᴏᴏsᴇ ᴀ ɢᴇɴʀᴇ:"),
        parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def show_trending_results(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    parts = q.data.split("_")
    country = parts[2]
    genre = parts[3]

    country_label = TRENDING_COUNTRIES.get(country, country.title())
    genre_label = TRENDING_GENRES.get(genre, genre.title())

    msg = await q.edit_message_text(
        safe_sc(f"🔥 <b>{h(country_label)} • {h(genre_label)}</b>\n\n⏳ ғᴇᴛᴄʜɪɴɢ ᴘᴏᴘᴜʟᴀʀ ᴍᴜsɪᴄ..."),
        parse_mode=ParseMode.HTML
    )

    query = build_trending_query(country, genre)
    results = await asyncio.to_thread(search_music, query, 10)

    if not results:
        await msg.edit_text(
            safe_sc(f"❌ ɴᴏ ʀᴇsᴜʟᴛs ғᴏᴜɴᴅ ғᴏʀ {h(country_label)} • {h(genre_label)}.\n\nᴛʀʏ ᴀɴᴏᴛʜᴇʀ ɢᴇɴʀᴇ ᴏʀ ᴄᴏᴜɴᴛʀʏ."),
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(safe_sc("🔙 ʙᴀᴄᴋ"), callback_data=f"trend_country_{country}")]])
        )
        return

    chat_id = q.message.chat_id
    search_cache[chat_id] = {"results": results, "timestamp": time.time()}

    keyboard = []
    for i, r in enumerate(results[:10]):
        dur = f" ⏱{r['duration']//60}:{r['duration']%60:02d}" if r.get('duration') else ""
        title = r['title'][:45] + "..." if len(r['title']) > 45 else r['title']
        keyboard.append([InlineKeyboardButton(f"🔥 {title}{dur}", callback_data=f"song_{i}")])
    keyboard.append([InlineKeyboardButton(safe_sc("🔙 ʙᴀᴄᴋ ᴛᴏ ᴄᴏᴜɴᴛʀɪᴇs"), callback_data="trend_back_countries")])

    await msg.edit_text(
        safe_sc(f"🔥 <b>{h(country_label)} • {h(genre_label)}</b>\n\n<i>ᴛʜᴇsᴇ ᴀʀᴇ sᴇᴀʀᴄʜ ʀᴇsᴜʟᴛs, ɴᴏᴛ ᴏғғɪᴄɪᴀʟ ᴄʜᴀʀᴛs.</i>\n\nᴄʟɪᴄᴋ ᴀ sᴏɴɢ ᴛᴏ ᴅᴏᴡɴʟᴏᴀᴅ:"),
        parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def more_tracks(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    chat_id = q.message.chat_id
    offset = int(q.data.split("_")[1])
    cached = search_cache.get(chat_id)
    if not cached or time.time() - cached.get("timestamp", 0) > CACHE_TTL_SECONDS:
        await q.edit_message_text(safe_sc("⚠️ sᴇᴀʀᴄʜ ᴇxᴘɪʀᴇᴅ. ᴘʟᴇᴀsᴇ sᴇᴀʀᴄʜ ᴀɢᴀɪɴ."))
        return
    results = cached["results"]
    keyboard = []
    for i in range(offset, min(offset + 20, len(results))):
        r = results[i]
        dur = f" ⏱{r['duration']//60}:{r['duration']%60:02d}" if r.get('duration') else ""
        title = r['title'][:45] + "..." if len(r['title']) > 45 else r['title']
        keyboard.append([InlineKeyboardButton(f"🎵 {title}{dur}", callback_data=f"song_{i}")])
    if offset + 20 < len(results):
        keyboard.append([InlineKeyboardButton(safe_sc("➕ ᴍᴏʀᴇ ʀᴇsᴜʟᴛs"), callback_data=f"more_{offset+20}")])
    keyboard.append([InlineKeyboardButton(safe_sc("🔙 ʙᴀᴄᴋ ᴛᴏ ᴍᴇɴᴜ"), callback_data="back_to_menu")])
    await q.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup(keyboard))

async def page_navigation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    chat_id = q.message.chat_id
    cached = search_cache.get(chat_id)
    if not cached or time.time() - cached.get("timestamp", 0) > CACHE_TTL_SECONDS:
        await q.edit_message_text(safe_sc("⚠️ sᴇᴀʀᴄʜ ᴇxᴘɪʀᴇᴅ."))
        return
    results = cached["results"]
    keyboard = []
    for i, r in enumerate(results[:20]):
        dur = f" ⏱{r['duration']//60}:{r['duration']%60:02d}" if r.get('duration') else ""
        title = r['title'][:45] + "..." if len(r['title']) > 45 else r['title']
        keyboard.append([InlineKeyboardButton(f"🎵 {title}{dur}", callback_data=f"song_{i}")])
    if len(results) > 20:
        keyboard.append([InlineKeyboardButton(safe_sc("➕ ᴍᴏʀᴇ ʀᴇsᴜʟᴛs"), callback_data="more_20")])
    keyboard.append([InlineKeyboardButton(safe_sc("🔙 ʙᴀᴄᴋ ᴛᴏ ᴍᴇɴᴜ"), callback_data="back_to_menu")])
    await q.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup(keyboard))

async def back_to_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    user_id = q.from_user.id
    await q.edit_message_text(safe_sc("🎵 ᴍᴀɪɴ ᴍᴇɴᴜ"), parse_mode=ParseMode.HTML)
    text = safe_sc(f"🎵 ᴡᴇʟᴄᴏᴍᴇ ʙᴀᴄᴋ, {q.from_user.first_name or 'ғʀɪᴇɴᴅ'}!\n\nᴡʜᴀᴛ ᴡᴏᴜʟᴅ ʏᴏᴜ ʟɪᴋᴇ ᴛᴏ ᴅᴏ?")
    await q.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=await get_main_menu_keyboard(user_id))

async def handle_social_download(update: Update, context: ContextTypes.DEFAULT_TYPE, url: str) -> None:
    user_id = update.effective_user.id
    user = await get_user(user_id)

    if not is_premium(user):
        await update.message.reply_text(
            safe_sc("🔒 sᴏᴄɪᴀʟ ᴍᴇᴅɪᴀ ᴠɪᴅᴇᴏ ᴅᴏᴡɴʟᴏᴀᴅ ɪs ᴀ ᴘʀᴇᴍɪᴜᴍ ғᴇᴀᴛᴜʀᴇ!\n\n💎 ᴜᴘɢʀᴀᴅᴇ ᴛᴏ ᴘʀᴇᴍɪᴜᴍ ᴛᴏ ᴅᴏᴡɴʟᴏᴀᴅ ᴠɪᴅᴇᴏs ғʀᴏᴍ ᴛɪᴋᴛᴏᴋ, ɪɴsᴛᴀɢʀᴀᴍ, ғᴀᴄᴇʙᴏᴏᴋ, ʏᴏᴜᴛᴜʙᴇ, ᴀɴᴅ ᴍᴏʀᴇ."),
            parse_mode=ParseMode.HTML
        )
        return

    platform = social_platform(url)
    status_msg = await update.message.reply_text(
        safe_sc(f"🔎 ᴅᴇᴛᴇᴄᴛᴇᴅ: {platform}\n⏳ ᴘʀᴇᴘᴀʀɪɴɢ ʏᴏᴜʀ ᴠɪᴅᴇᴏ..."),
        parse_mode=ParseMode.HTML
    )
    temp_dir = tempfile.mkdtemp(prefix="social_dl_")
    file_path = None

    try:
        await status_msg.edit_text(
            safe_sc(f"📥 ᴅᴏᴡɴʟᴏᴀᴅɪɴɢ {platform} ᴠɪᴅᴇᴏ...\nᴘʟᴇᴀsᴇ ᴡᴀɪᴛ."),
            parse_mode=ParseMode.HTML
        )
        file_path, info = await download_social_video(url, temp_dir)

        if not file_path or not file_path.exists():
            raise RuntimeError("No downloadable video was produced.")

        file_size = file_path.stat().st_size
        if file_size > MAX_SOCIAL_SIZE_MB * 1024 * 1024:
            raise RuntimeError(f"Video exceeds {MAX_SOCIAL_SIZE_MB}MB")

        title = info.get("title") if info else "Social Media Video"
        await status_msg.edit_text(safe_sc("📤 ᴜᴘʟᴏᴀᴅɪɴɢ ᴠɪᴅᴇᴏ ᴛᴏ ᴛᴇʟᴇɢʀᴀᴍ..."), parse_mode=ParseMode.HTML)

        caption = safe_sc(f"🎬 {h(title[:700])}\n\n📦 {format_bytes(file_size)}\n🌐 {platform}")
        with file_path.open("rb") as video_file:
            await update.message.reply_video(
                video=video_file,
                caption=caption,
                supports_streaming=True,
                read_timeout=120, write_timeout=120,
                connect_timeout=30, pool_timeout=30,
                parse_mode=ParseMode.HTML
            )
        await status_msg.delete()

    except yt_dlp.utils.DownloadError as exc:
        logger.warning(f"yt-dlp social download failed: {exc}")
        await status_msg.edit_text(
            safe_sc("❌ ɪ ᴄᴏᴜʟᴅɴ'ᴛ ᴅᴏᴡɴʟᴏᴀᴅ ᴛʜᴀᴛ ᴠɪᴅᴇᴏ.\n\n"
                    "ᴛʜᴇ ᴘᴏsᴛ ᴍᴀʏ ʙᴇ:\n"
                    "• ᴘʀɪᴠᴀᴛᴇ\n"
                    "• ᴜɴᴀᴠᴀɪʟᴀʙʟᴇ\n"
                    "• ᴀɢᴇ/ʟᴏɢɪɴ ʀᴇsᴛʀɪᴄᴛᴇᴅ\n"
                    "• ᴅᴇʟᴇᴛᴇᴅ\n"
                    "• ᴜɴsᴜᴘᴘᴏʀᴛᴇᴅ\n"
                    "• ʙʟᴏᴄᴋᴇᴅ ʙʏ ᴛʜᴇ ᴘʟᴀᴛғᴏʀᴍ\n\n"
                    "<i>ɴᴏᴛᴇ: sᴜᴘᴘᴏʀᴛ ᴅᴇᴘᴇɴᴅs ᴏɴ ᴘᴜʙʟɪᴄ ᴀᴄᴄᴇssɪʙɪʟɪᴛʏ ᴀɴᴅ ᴘʟᴀᴛғᴏʀᴍ ᴄʜᴀɴɢᴇs.</i>"),
            parse_mode=ParseMode.HTML
        )
    except Exception as exc:
        logger.exception("Unexpected social download error")
        error_text = str(exc).lower()
        if "exceeds" in error_text or "larger" in error_text:
            user_message = safe_sc(f"❌ ᴛʜᴇ ᴠɪᴅᴇᴏ ɪs ʟᴀʀɢᴇʀ ᴛʜᴀɴ {MAX_SOCIAL_SIZE_MB} ᴍʙ.")
        else:
            user_message = safe_sc("❌ sᴏᴍᴇᴛʜɪɴɢ ᴡᴇɴᴛ ᴡʀᴏɴɢ ᴡʜɪʟᴇ ᴘʀᴏᴄᴇssɪɴɢ ᴛʜᴇ ᴠɪᴅᴇᴏ.")
        await status_msg.edit_text(user_message, parse_mode=ParseMode.HTML)
    finally:
        try:
            shutil.rmtree(temp_dir, ignore_errors=True)
        except Exception:
            pass

# ===== PREMIUM PAYMENT =====
async def premium_invoice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    user_id = q.from_user.id
    days = int(q.data.split("_")[1])

    if days not in PREMIUM_PLANS:
        await q.message.reply_text(safe_sc("❌ ɪɴᴠᴀʟɪᴅ ᴘʀᴇᴍɪᴜᴍ ᴘʟᴀɴ."), parse_mode=ParseMode.HTML)
        return

    stars = PREMIUM_PLANS[days]
    title = safe_sc(f"💎 ᴘʀᴇᴍɪᴜᴍ — {days} ᴅᴀʏs")
    description = safe_sc(f"ᴜɴʟᴏᴄᴋ ᴜɴʟɪᴍɪᴛᴇᴅ ᴅᴏᴡɴʟᴏᴀᴅs, ʟʏʀɪᴄs, ᴠɪᴅᴇᴏs, ᴀɴᴅ sᴏᴄɪᴀʟ ᴍᴇᴅɪᴀ ғᴏʀ {days} ᴅᴀʏs.")
    payload = f"premium_{user_id}_{days}"

    try:
        await context.bot.send_invoice(
            chat_id=user_id,
            title=title,
            description=description,
            payload=payload,
            provider_token="",
            currency="XTR",
            prices=[LabeledPrice(label=safe_sc(f"{days} ᴅᴀʏs ᴘʀᴇᴍɪᴜᴍ"), amount=stars)],
            start_parameter=f"premium_{days}"
        )
    except Exception as e:
        logger.error(f"Invoice error: {e}")
        await q.message.reply_text(
            safe_sc("❌ ғᴀɪʟᴇᴅ ᴛᴏ ᴄʀᴇᴀᴛᴇ ɪɴᴠᴏɪᴄᴇ. ᴛʀʏ ᴀɢᴀɪɴ ʟᴀᴛᴇʀ."),
            parse_mode=ParseMode.HTML
        )

async def precheckout_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.pre_checkout_query
    if query.invoice_payload.startswith("premium_"):
        await query.answer(ok=True)
    else:
        await query.answer(ok=False, error_message=safe_sc("sᴏᴍᴇᴛʜɪɴɢ ᴡᴇɴᴛ ᴡʀᴏɴɢ ᴡɪᴛʜ ʏᴏᴜʀ ᴘᴀʏᴍᴇɴᴛ."))

async def successful_payment_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    payload = update.message.successful_payment.invoice_payload
    parts = payload.split("_")

    if len(parts) < 3 or parts[0] != "premium":
        await update.message.reply_text(safe_sc("❌ ɪɴᴠᴀʟɪᴅ ᴘᴀʏᴍᴇɴᴛ ᴘᴀʏʟᴏᴀᴅ."), parse_mode=ParseMode.HTML)
        return

    try:
        payload_user_id = int(parts[1])
        days = int(parts[2])
    except Exception:
        await update.message.reply_text(safe_sc("❌ ɪɴᴠᴀʟɪᴅ ᴘᴀʏᴍᴇɴᴛ ᴅᴀᴛᴀ."), parse_mode=ParseMode.HTML)
        return

    if payload_user_id != user.id:
        await update.message.reply_text(safe_sc("❌ ᴘᴀʏᴍᴇɴᴛ ᴜsᴇʀ ᴍɪsᴍᴀᴛᴄʜ."), parse_mode=ParseMode.HTML)
        return

    if days not in PREMIUM_PLANS:
        await update.message.reply_text(safe_sc("❌ ɪɴᴠᴀʟɪᴅ ᴘʀᴇᴍɪᴜᴍ ᴘʟᴀɴ."), parse_mode=ParseMode.HTML)
        return

    expected_stars = PREMIUM_PLANS[days]
    received = update.message.successful_payment.total_amount
    if received != expected_stars:
        await update.message.reply_text(
            safe_sc(f"❌ ᴘᴀʏᴍᴇɴᴛ ᴀᴍᴏᴜɴᴛ ᴍɪsᴍᴀᴛᴄʜ. ᴇxᴘᴇᴄᴛᴇᴅ {expected_stars}, ɢᴏᴛ {received}."),
            parse_mode=ParseMode.HTML
        )
        return

    expire = await extend_premium(user.id, days)
    await update.message.reply_text(
        safe_sc(f"🎉 ᴘᴀʏᴍᴇɴᴛ sᴜᴄᴄᴇssғᴜʟ!\n\n💎 ʏᴏᴜ ᴀʀᴇ ɴᴏᴡ ᴀ ᴘʀᴇᴍɪᴜᴍ ᴜsᴇʀ!\n📅 ᴇxᴘɪʀᴇs: {expire.strftime('%Y-%m-%d %H:%M')}\n\nᴇɴᴊᴏʏ ᴜɴʟɪᴍɪᴛᴇᴅ ᴅᴏᴡɴʟᴏᴀᴅs, ʟʏʀɪᴄs, ᴠɪᴅᴇᴏs, ᴀɴᴅ sᴏᴄɪᴀʟ ᴍᴇᴅɪᴀ! 🚀"),
        parse_mode=ParseMode.HTML,
        reply_markup=await get_main_menu_keyboard(user.id)
    )

    if CHANNEL_ID:
        await notify_channel(context,
            f"⭐ <b>ɴᴇᴡ ᴘʀᴇᴍɪᴜᴍ ᴜsᴇʀ</b>\n\n"
            f"👤 ɴᴀᴍᴇ: {h(user.first_name)}\n"
            f"📛 ᴜsᴇʀɴᴀᴍᴇ: @{h(user.username)}\n"
            f"🆔 ᴜsᴇʀ ɪᴅ: {user.id}\n"
            f"📅 ᴅᴀʏs: {days}\n"
            f"⏰ ᴇxᴘɪʀᴇ: {expire.strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"👤 ɢʀᴀɴᴛᴇᴅ ʙʏ: ᴛᴇʟᴇɢʀᴀᴍ sᴛᴀʀs\n\n"
            f"🚀 <a href=\"https://t.me/{BOT_USERNAME}?start={user.id}\">sᴛᴀʀᴛ ʙᴏᴛ</a> | 💎 ɢᴇᴛ ᴘʀᴇᴍɪᴜᴍ"
        )

# ===== ADMIN COMMANDS =====
async def admin_premium(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id != OWNER_ID:
        return
    try:
        target_id = int(context.args[0])
        days = int(context.args[1])
        expire = await extend_premium(target_id, days)
        await context.bot.send_message(
            target_id,
            safe_sc(f"🎉 ᴄᴏɴɢʀᴀᴛᴜʟᴀᴛɪᴏɴs!\nʏᴏᴜ ʜᴀᴠᴇ ʙᴇᴇɴ ᴀᴡᴀʀᴅᴇᴅ ᴘʀᴇᴍɪᴜᴍ!\n📅 ᴇxᴘɪʀᴇs: {expire.strftime('%Y-%m-%d %H:%M')}"),
            parse_mode=ParseMode.HTML
        )
        await update.message.reply_text(
            safe_sc(f"✅ ᴘʀᴇᴍɪᴜᴍ ɢʀᴀɴᴛᴇᴅ ᴛᴏ ᴜsᴇʀ {target_id} ғᴏʀ {days} ᴅᴀʏs."),
            parse_mode=ParseMode.HTML
        )
        if CHANNEL_ID:
            u = await get_user(target_id)
            await notify_channel(context,
                f"⭐ <b>ɴᴇᴡ ᴘʀᴇᴍɪᴜᴍ ᴜsᴇʀ</b>\n\n"
                f"👤 ɴᴀᴍᴇ: {h(u.first_name if u else 'Unknown')}\n"
                f"📛 ᴜsᴇʀɴᴀᴍᴇ: @{h(u.username if u else 'None')}\n"
                f"🆔 ᴜsᴇʀ ɪᴅ: {target_id}\n"
                f"📅 ᴅᴀʏs: {days}\n"
                f"⏰ ᴇxᴘɪʀᴇ: {expire.strftime('%Y-%m-%d %H:%M:%S')}\n"
                f"👤 ɢʀᴀɴᴛᴇᴅ ʙʏ: ᴏᴡɴᴇʀ\n\n"
                f"🚀 <a href=\"https://t.me/{BOT_USERNAME}?start={target_id}\">sᴛᴀʀᴛ ʙᴏᴛ</a> | 💎 ɢᴇᴛ ᴘʀᴇᴍɪᴜᴍ"
            )
    except Exception:
        await update.message.reply_text(safe_sc("❌ ᴜsᴀɢᴇ: /premium <ᴜsᴇʀ_ɪᴅ> <ᴅᴀʏs>"), parse_mode=ParseMode.HTML)

async def admin_reward(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id != OWNER_ID:
        return
    try:
        target_id = int(context.args[0])
        pts = int(context.args[1])
        await add_points(target_id, pts)
        await context.bot.send_message(
            target_id,
            safe_sc(f"🎉 ᴄᴏɴɢʀᴀᴛᴜʟᴀᴛɪᴏɴs!\nʏᴏᴜ ʜᴀᴠᴇ ʙᴇᴇɴ ᴀᴡᴀʀᴅᴇᴅ {pts} ᴘᴏɪɴᴛs!"),
            parse_mode=ParseMode.HTML
        )
        await update.message.reply_text(
            safe_sc(f"✅ ᴀᴡᴀʀᴅᴇᴅ {pts} ᴘᴏɪɴᴛs ᴛᴏ ᴜsᴇʀ {target_id}."),
            parse_mode=ParseMode.HTML
        )
    except Exception:
        await update.message.reply_text(safe_sc("❌ ᴜsᴀɢᴇ: /reward <ᴜsᴇʀ_ɪᴅ> <ᴘᴏɪɴᴛs>"), parse_mode=ParseMode.HTML)

async def admin_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id != OWNER_ID:
        return
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT COUNT(*) FROM users") as cursor:
            total = (await cursor.fetchone())[0]
        async with db.execute("SELECT COUNT(*) FROM users WHERE premium_expire > datetime('now')") as cursor:
            premium = (await cursor.fetchone())[0]
    await update.message.reply_text(
        safe_sc(f"📊 ʙᴏᴛ sᴛᴀᴛɪsᴛɪᴄs\n\n👥 ᴛᴏᴛᴀʟ ᴜsᴇʀs: {total}\n💎 ᴘʀᴇᴍɪᴜᴍ ᴜsᴇʀs: {premium}"),
        parse_mode=ParseMode.HTML
    )

async def admin_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id != OWNER_ID:
        return
    if not update.message.reply_to_message:
        await update.message.reply_text(
            safe_sc("📢 ʀᴇᴘʟʏ ᴛᴏ ᴀ ᴍᴇssᴀɢᴇ (ᴛᴇxᴛ/ᴘʜᴏᴛᴏ/ᴠɪᴅᴇᴏ) ᴛᴏ ʙʀᴏᴀᴅᴄᴀsᴛ."),
            parse_mode=ParseMode.HTML
        )
        return

    msg = update.message.reply_to_message
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT id FROM users") as cursor:
            users = [u[0] async for u in cursor]

    delivered = 0
    failed = 0
    status = await update.message.reply_text(
        safe_sc(f"📢 ʙʀᴏᴀᴅᴄᴀsᴛɪɴɢ ᴛᴏ {len(users)} ᴜsᴇʀs..."),
        parse_mode=ParseMode.HTML
    )

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
            else:
                await context.bot.copy_message(chat_id=uid, from_chat_id=msg.chat_id, message_id=msg.message_id)
            delivered += 1
            await asyncio.sleep(0.05)
        except RetryAfter as e:
            await asyncio.sleep(e.retry_after)
            try:
                await context.bot.copy_message(chat_id=uid, from_chat_id=msg.chat_id, message_id=msg.message_id)
                delivered += 1
            except Exception:
                failed += 1
        except (Forbidden, BadRequest) as e:
            logger.warning(f"Broadcast skip {uid}: {e}")
            failed += 1
        except Exception as e:
            logger.error(f"Broadcast failed for {uid}: {e}")
            failed += 1

    await status.edit_text(
        safe_sc(f"📢 <b>ʙʀᴏᴀᴅᴄᴀsᴛ ᴄᴏᴍᴘʟᴇᴛᴇ</b>\n\n👥 ᴛᴏᴛᴀʟ: {len(users)}\n✅ ᴅᴇʟɪᴠᴇʀᴇᴅ: {delivered}\n❌ ғᴀɪʟᴇᴅ: {failed}"),
        parse_mode=ParseMode.HTML
    )

# ===== ERROR HANDLER =====
async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error(f"Update {update} caused error: {context.error}")
    if update and update.effective_message:
        try:
            await update.effective_message.reply_text(
                safe_sc("❌ ᴏᴏᴘs! sᴏᴍᴇᴛʜɪɴɢ ᴡᴇɴᴛ ᴡʀᴏɴɢ.\n\nᴘʟᴇᴀsᴇ ᴛʀʏ ᴀɢᴀɪɴ ʟᴀᴛᴇʀ ᴏʀ ᴄᴏɴᴛᴀᴄᴛ sᴜᴘᴘᴏʀᴛ."),
                parse_mode=ParseMode.HTML
            )
        except Exception:
            pass

# ===== MAIN =====
async def main() -> None:
    print("""
╔══════════════════════════════════════════════════════════════╗
║           ADVANCED MUSIC BOT - PRODUCTION REFACTOR           ║
║   Created by Mr DarkHacker                                   ║
╚══════════════════════════════════════════════════════════════╝
    """)
    if not COOKIES_FILE.exists():
        print(f"⚠️ No cookie file found at: {COOKIES_FILE}")
        print("   (yt-dlp fallback may be limited for some platforms)\n")
    print("🤖 Bot is starting... Press Ctrl+C to stop.\n")

    await init_db()

    app = Application.builder().token(TOKEN).build()

    # Commands
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("account", show_account))
    app.add_handler(CommandHandler("trending", show_trending_countries))
    app.add_handler(CommandHandler("help", show_help))
    app.add_handler(CommandHandler("premium", admin_premium))
    app.add_handler(CommandHandler("reward", admin_reward))
    app.add_handler(CommandHandler("stats", admin_stats))
    app.add_handler(CommandHandler("broadcast", admin_broadcast))

    # Messages
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_menu_buttons))

    # Callbacks
    app.add_handler(CallbackQueryHandler(back_to_menu, pattern="^back_to_menu$"))
    app.add_handler(CallbackQueryHandler(song_info, pattern="^song_"))
    app.add_handler(CallbackQueryHandler(download_audio_callback, pattern="^dl_audio_"))
    app.add_handler(CallbackQueryHandler(download_video_callback, pattern="^dl_video_"))
    app.add_handler(CallbackQueryHandler(lyrics_callback, pattern="^lyrics_"))
    app.add_handler(CallbackQueryHandler(more_tracks, pattern="^more_"))
    app.add_handler(CallbackQueryHandler(page_navigation, pattern="^page_"))
    app.add_handler(CallbackQueryHandler(premium_invoice, pattern=r"^premium_\d+$"))
    app.add_handler(CallbackQueryHandler(show_trending_genres, pattern="^trend_country_"))
    app.add_handler(CallbackQueryHandler(show_trending_results, pattern="^trend_genre_"))
    app.add_handler(CallbackQueryHandler(show_trending_countries, pattern="^trend_back_countries$"))

    # Payments
    app.add_handler(PreCheckoutQueryHandler(precheckout_callback))
    app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment_callback))

    # Errors
    app.add_error_handler(error_handler)

    await app.initialize()
    await app.start()
    await app.updater.start_polling(allowed_updates=Update.ALL_TYPES)

    # Keep running
    stop_event = asyncio.Event()
    try:
        await stop_event.wait()
    finally:
        await app.updater.stop()
        await app.stop()
        await app.shutdown()

if __name__ == "__main__":
    asyncio.run(main())
