# romantic_music_bot.py
"""
Romantic-themed Telegram VC Music Bot (single-file)
Features:
 - /play <query or url> (supports audio & video via yt-dlp)
 - /stop (stop playback in current chat)
 - /stopall (owner only: stop playback in all chats)
 - /tagadmin (mention group admins with romantic touch)
 - /tagall (mention group members in chunks)
 - /filter <keyword> <reply> (set filter by replying or direct)
 - /filters (list all saved filters)
 - frozen_check loop (keeps assistant checked)
 - Lightweight JSON storage for filters & broadcast list (no DB required)
Notes:
 - Requires ffmpeg installed system-wide and yt_dlp python package
 - Requires pyrogram and pytgcalls
"""

import os
import sys
import time
import json
import asyncio
import logging
import tempfile
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List

from dotenv import load_dotenv
from pyrogram import Client, filters
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton
from pytgcalls import PyTgCalls
from pytgcalls.types import MediaStream
from pytgcalls.exceptions import AlreadyJoinedError

import yt_dlp as ytdl
import aiofiles
import aiohttp

load_dotenv()

API_ID = int(os.environ.get("API_ID", "0"))
API_HASH = os.environ.get("API_HASH", "")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
ASSISTANT_SESSION = os.environ.get("ASSISTANT_SESSION", None)
OWNER_ID = int(os.environ.get("OWNER_ID", "8287505664"))
PORT = int(os.environ.get("PORT", "8080"))
RESTART_SECRET = os.environ.get("RESTART_SECRET", "secret_token")

if not API_ID or not API_HASH or not BOT_TOKEN:
    print("Please set API_ID, API_HASH and BOT_TOKEN in the environment.")
    sys.exit(1)

# Setup clients
BOT_SESSION = "romantic_bot_session"
bot = Client(BOT_SESSION, api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)
assistant = None
if ASSISTANT_SESSION:
    assistant = Client("assistant_session", session_string=ASSISTANT_SESSION, api_id=API_ID, api_hash=API_HASH)

pytgcalls = None
if assistant:
    pytgcalls = PyTgCalls(assistant)
else:
    pytgcalls = None  # we'll try to stream using bot account if assistant not provided

# In-memory queues and state
queues: Dict[int, List[Dict]] = {}  # chat_id -> list of song dicts
playback_tasks: Dict[int, asyncio.Task] = {}
DOWNLOAD_DIR = Path(tempfile.gettempdir()) / "romantic_music_bot"
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

# Filters storage (simple JSON)
FILTERS_FILE = Path("romantic_filters.json")
try:
    if FILTERS_FILE.exists():
        with open(FILTERS_FILE, "r", encoding="utf8") as f:
            saved_filters = json.load(f)
    else:
        saved_filters = {}
except Exception:
    saved_filters = {}

# Simple logger
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("romantic_bot")

# Romantic helper texts
WELCOME_TEXT = (
    "💞 *नमस्ते इश्क़िया रूह!* 💞\n\n"
    "मैं आपकी दिल की धड़कन बनकर यहाँ आया हूँ — संगीत सबसे खूबसूरत इज़हार है।\n"
    "गाने चलाने के लिए लिखो: `/play <song name or youtube url>`\n"
    "या किसी ऑडियो/वीडियो को रिप्लाई करके भी भेजो।\n\n"
    "मेरे पास खास कमांड्स हैं: /tagadmin, /tagall, /filter, /filters, /stop, /stopall\n"
    "रोज़-ए-इश्क़ में दिल लगाकर सुनिए 🎧"
)

# yt-dlp options
YTDL_OPTS = {
    "format": "bestaudio/best",
    "outtmpl": str(DOWNLOAD_DIR / "%(id)s.%(ext)s"),
    "noplaylist": False,
    "quiet": True,
    "no_warnings": True,
    "ignoreerrors": True,
    "cachedir": False,
    # If you want to allow video downloads too:
    "merge_output_format": "mp4",
}

# Helper: save filters
def save_filters():
    try:
        with open(FILTERS_FILE, "w", encoding="utf8") as f:
            json.dump(saved_filters, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error("Error saving filters: %s", e)

# Helper: download via yt-dlp (async wrapper)
async def download_media(query: str, prefer_video: bool = False) -> Dict:
    """
    Returns dict:
      { "path": "/tmp/abc.mp4", "title": "...", "duration": seconds, "is_video": True/False }
    """
    loop = asyncio.get_event_loop()
    info = None

    def ytdl_extract(q):
        ydl_opts = YTDL_OPTS.copy()
        # if prefer video, request bestvideo+bestaudio
        if prefer_video:
            ydl_opts["format"] = "bestvideo+bestaudio/best"
        with ytdl.YoutubeDL(ydl_opts) as ydl:
            return ydl.extract_info(q, download=True)

    try:
        info = await loop.run_in_executor(None, ytdl_extract, query)
    except Exception as e:
        logger.error("yt-dlp error: %s", e)
        raise

    if not info:
        raise RuntimeError("Could not fetch media info.")

    # handle playlist: pick first entry
    if info.get("entries"):
        entry = info["entries"][0]
    else:
        entry = info

    if not entry:
        raise RuntimeError("No media entry found.")

    ext = entry.get("ext") or "m4a"
    filename = DOWNLOAD_DIR / f"{entry.get('id')}.{ext}"
    is_video = entry.get("vcodec") != "none" if "vcodec" in entry else False

    return {
        "path": str(filename),
        "title": entry.get("title", "Unknown"),
        "duration": int(entry.get("duration") or 0),
        "is_video": is_video,
        "webpage_url": entry.get("webpage_url")
    }

# Format time
def fmt_time(seconds: int) -> str:
    h, r = divmod(seconds, 3600)
    m, s = divmod(r, 60)
    if h > 0:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"

# Play function - uses PyTgCalls (assistant) if available, else try with bot (best-effort)
async def start_playback(chat_id: int):
    """
    Plays the first item in queues[chat_id].
    This function handles streaming via pytgcalls.
    """
    if chat_id not in queues or not queues[chat_id]:
        return

    song = queues[chat_id][0]
    path = song["path"]
    is_video = song.get("is_video", False)
    title = song.get("title", "दिल की धुन")

    # create a control message
    try:
        msg = await bot.send_message(
            chat_id,
            f"💘 *अब बज रहा है:* *{title}*\n⏱ {fmt_time(song.get('duration',0))}\n\n_आपके दिल के लिए ये सुर समर्पित है..._",
            parse_mode="markdown"
        )
    except Exception:
        msg = None

    # Ensure pytgcalls started
    global pytgcalls
    if pytgcalls is None and assistant:
        pytgcalls = PyTgCalls(assistant)

    try:
        if assistant and not pytgcalls.is_running:
            pytgcalls.start()
    except Exception:
        pass

    # join voice chat and play
    try:
        # If using assistant client
        client_for_call = assistant if assistant else bot
        # Attempt to join and play
        try:
            await pytgcalls.join_group_call(
                chat_id,
                MediaStream(path),
            )
        except AlreadyJoinedError:
            # if already in call, replace stream
            await pytgcalls.change_stream(
                chat_id,
                MediaStream(path),
            )
    except Exception as e:
        logger.error("Error starting playback: %s", e)
        await bot.send_message(chat_id, f"❌ दिलफ़रमा बाज़ी में गड़बड़ी: {e}")
        # remove the failing item and attempt next
        queues[chat_id].pop(0)
        # try next
        if queues[chat_id]:
            await start_playback(chat_id)
        else:
            # leave call if nothing to play
            try:
                await pytgcalls.leave_group_call(chat_id)
            except Exception:
                pass
        return

    # Wait until track finishes (simple sleep by duration) then pop & continue
    duration = song.get("duration") or 0
    # If duration is 0 (unknown), wait a safe amount (e.g., 5 minutes) or until process ends.
    wait_for = max(duration, 300)
    try:
        await asyncio.sleep(wait_for)
    except asyncio.CancelledError:
        # if stopped early
        pass

    # cleanup
    try:
        if os.path.exists(path):
            try:
                os.remove(path)
            except Exception:
                pass
    except Exception:
        pass

    if chat_id in queues and queues[chat_id]:
        queues[chat_id].pop(0)

    # Play next if exists
    if chat_id in queues and queues[chat_id]:
        await start_playback(chat_id)
    else:
        # leave voice chat
        try:
            await pytgcalls.leave_group_call(chat_id)
        except Exception:
            pass
        try:
            await bot.send_message(chat_id, "🌙 खुशनुमा पल ख़त्म हुआ — अगर चाहो तो फिर बुला लेना।")
        except Exception:
            pass

# --- Commands ---

@bot.on_message(filters.private & filters.command("start"))
async def cmd_start(_, message: Message):
    await message.reply(WELCOME_TEXT, parse_mode="markdown")

@bot.on_message(filters.command("play") & (filters.group | filters.private))
async def cmd_play(_, message: Message):
    chat_id = message.chat.id
    # If user replied to media (local file) handle that
    prefer_video = False
    query = None

    if message.reply_to_message and (message.reply_to_message.audio or message.reply_to_message.video or message.reply_to_message.document):
        # Download replied media and queue
        processing = await message.reply("💫 आपकी मीठी धुन ढूँढ रहा हूँ...")
        try:
            file_path = await bot.download_media(message.reply_to_message)
            # naive metadata
            song_info = {
                "path": file_path,
                "title": message.reply_to_message.caption or "रिटर्न-ट्यून",
                "duration": getattr(message.reply_to_message.audio or message.reply_to_message.video or message.reply_to_message.document, "duration", 0),
                "is_video": bool(message.reply_to_message.video)
            }
            queues.setdefault(chat_id, []).append(song_info)
            await processing.edit("✅ गीत पंक्ति में जुड़ गया — प्यार की राह पर बढ़ो।")
            if len(queues[chat_id]) == 1:
                # start playback
                task = asyncio.create_task(start_playback(chat_id))
                playback_tasks[chat_id] = task
        except Exception as e:
            await processing.edit(f"❌ फाइल डाउनलोड में परेशानी: {e}")
        return

    # else the command argument
    args = message.text.split(None, 1)
    if len(args) < 2:
        await message.reply("❌ कृपया गाना नाम या यूट्यूब लिंक भेजें।\nउदाहरण: `/play shape of you`", parse_mode="markdown")
        return
    query = args[1].strip()

    processing = await message.reply("🌹 तुम्हारी दिल की आरज़ू को ढूँढ रहा हूँ... कृपया धैर्य रखें।")
    try:
        # allow video if the query seems like a video link or user asked video
        if "youtube.com/watch" in query or "youtu.be" in query:
            prefer_video = True
        data = await download_media(query, prefer_video=prefer_video)
    except Exception as e:
        await processing.edit(f"❌ खोज में ख़राबी हुई: {e}\nकोशिश फिर से करो या अलग नाम भेजो।")
        return

    # add to queue
    song_info = {
        "path": data["path"],
        "title": data["title"],
        "duration": data["duration"],
        "is_video": data["is_video"]
    }
    queues.setdefault(chat_id, []).append(song_info)
    await processing.edit(f"💝 *{song_info['title']}* अब क्यू में जुड़ गया — आपकी धुन जल्द ही बजेगी।", parse_mode="markdown")

    # if first in queue, start playback
    if len(queues[chat_id]) == 1:
        task = asyncio.create_task(start_playback(chat_id))
        playback_tasks[chat_id] = task

@bot.on_message(filters.command("stop") & (filters.group | filters.private))
async def cmd_stop(_, message: Message):
    chat_id = message.chat.id
    # cancel task and clear queue for this chat
    if playback_tasks.get(chat_id):
        playback_tasks[chat_id].cancel()
        playback_tasks.pop(chat_id, None)
    if queues.get(chat_id):
        # remove files
        for s in queues[chat_id]:
            try:
                if os.path.exists(s.get("path","")):
                    os.remove(s["path"])
            except Exception:
                pass
        queues.pop(chat_id, None)
    # try leave group call
    try:
        if pytgcalls:
            await pytgcalls.leave_group_call(chat_id)
    except Exception:
        pass
    await message.reply("🛑 प्यार की धुन रोकी गई — आवाज़ अभी के लिए बंद।")

@bot.on_message(filters.command("stopall") & filters.user(OWNER_ID))
async def cmd_stopall(_, message: Message):
    # stop all chats
    for cid in list(list(queues.keys())):
        if playback_tasks.get(cid):
            playback_tasks[cid].cancel()
            playback_tasks.pop(cid, None)
        # remove files
        for s in queues.get(cid, []):
            try:
                if os.path.exists(s.get("path","")):
                    os.remove(s["path"])
            except Exception:
                pass
        queues.pop(cid, None)
        try:
            if pytgcalls:
                await pytgcalls.leave_group_call(cid)
        except Exception:
            pass
    await message.reply("🌌 सभी धुनें रोकी गईं — मालिक की हुकूमत पूरी हुई।")

# tagadmin - mention group admins (in romantic tone)
@bot.on_message(filters.group & filters.command("tagadmin"))
async def cmd_tagadmin(_, message: Message):
    chat = message.chat
    mention_list = []
    async for m in bot.get_chat_members(chat.id, filter="administrators"):
        user = m.user
        if user.is_bot:
            continue
        mention_list.append(f"[{user.first_name}](tg://user?id={user.id})")
    if not mention_list:
        await message.reply("💕 कोई प्रशासक नहीं मिला जिससे इश्क़ बाँट सकूँ...")
        return
    text = "💌 *ओह मेरे प्यारे एडमिन्स!* — " + ", ".join(mention_list)
    text += "\n\n_आप लोगों से एक प्यारी सी विनती है — संगीत चालू रखें।_"
    await message.reply(text, parse_mode="markdown")

# tagall - mention members in chunks (be careful with very large groups)
@bot.on_message(filters.group & filters.command("tagall"))
async def cmd_tagall(_, message: Message):
    chat_id = message.chat.id
    # we will fetch recent members (Pyrogram doesn't provide full member list easily for large groups)
    # We'll attempt to tag members from chat.get_members with a reasonable limit
    members = []
    count = 0
    async for member in bot.get_chat_members(chat_id, limit=200):
        if member.user.is_bot:
            continue
        members.append(member.user)
        count += 1
    if not members:
        await message.reply("💞 लगता है यहाँ औरतों/मर्दों की तखलीक कम है...")
        return
    # send in chunks of 20 to avoid message length overflow
    chunk_size = 20
    for i in range(0, len(members), chunk_size):
        chunk = members[i:i+chunk_size]
        mentions = " ".join([f"[{u.first_name}](tg://user?id={u.id})" for u in chunk])
        await message.reply(f"🎇 प्यारे सदस्यों, आपकी बारगीरी:\n{mentions}", parse_mode="markdown")
        await asyncio.sleep(1)  # slight delay between bursts

# Filter commands
@bot.on_message(filters.command("filter") & filters.group)
async def cmd_filter(_, message: Message):
    # usage: reply to a message with /filter keyword
    if message.reply_to_message:
        parts = message.text.split(None, 1)
        if len(parts) < 2:
            await message.reply("📝 उपयोग: रिप्लाई करके /filter <keyword>\nमैं उस रिप्लाई को उस keyword के साथ जोड़ दूँगा।")
            return
        keyword = parts[1].strip().lower()
        content = None
        # If replied to text: save text, else save file_id (so we can forward)
        if message.reply_to_message.text:
            content = {"type": "text", "data": message.reply_to_message.text}
        else:
            # try to store message id and chat id so we can forward it
            content = {"type": "forward", "chat_id": message.reply_to_message.chat.id, "message_id": message.reply_to_message.id}
        saved_filters[keyword] = content
        save_filters()
        await message.reply(f"💘 फिल्टर '{keyword}' सफलतापूर्वक सेव कर दिया गया — अब दिल पर राज़ तुम्हारा।")
    else:
        # maybe user used /filter keyword reply_text directly
        parts = message.text.split(None, 2)
        if len(parts) >= 3:
            keyword = parts[1].strip().lower()
            reply_text = parts[2].strip()
            saved_filters[keyword] = {"type": "text", "data": reply_text}
            save_filters()
            await message.reply(f"💞 फिल्टर '{keyword}' सेव हो गया।")
        else:
            await message.reply("📝 सही यूज़: रिप्लाई करके /filter <keyword> या /filter <keyword> <reply text>")

@bot.on_message(filters.group & filters.command("filters"))
async def cmd_filters(_, message: Message):
    if not saved_filters:
        await message.reply("💭 कोई फिल्टर सेव नहीं है — प्यार खाली है।")
        return
    keys = ", ".join(saved_filters.keys())
    await message.reply(f"📚 सेव्ड फिल्टर्स:\n{keys}")

# global message handler to respond to filters
@bot.on_message(filters.group & ~filters.command(["filter", "filters", "tagadmin", "tagall", "play", "stop"]))
async def handle_filters(_, message: Message):
    text = (message.text or "").lower()
    if not text:
        return
    for k, v in saved_filters.items():
        if k in text:
            # reply accordingly
            try:
                if v["type"] == "text":
                    await message.reply(v["data"])
                elif v["type"] == "forward":
                    await bot.copy_message(message.chat.id, v["chat_id"], v["message_id"])
                return
            except Exception as e:
                logger.error("Filter reply error: %s", e)
                return

# frozen_check: simple loop that messages assistant bot (if available) to ensure it's fine
async def frozen_check_loop():
    if not assistant:
        logger.info("No assistant client configured; skipping frozen_check.")
        return
    await bot.send_message(OWNER_ID, "❄️ Romantic bot: frozen_check loop started.")
    while True:
        try:
            # send ping to assistant's own account (works if assistant session is bot-like)
            me = await assistant.get_me()
            username = me.username
            if username:
                await assistant.send_message(username, "/frozen_check")
            # Wait & attempt to find reply (best-effort) - here we don't implement full reply checking
        except Exception as e:
            logger.error("frozen_check error: %s", e)
        await asyncio.sleep(60)

# Simple HTTP server for liveness and secure restart (requires RESTART_SECRET)
from http.server import HTTPServer, BaseHTTPRequestHandler
import threading

class SimpleHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"Romantic bot is alive.")
        elif self.path.startswith("/restart"):
            # expect /restart?secret=...
            from urllib.parse import urlparse, parse_qs
            qs = parse_qs(urlparse(self.path).query)
            secret = qs.get("secret", [""])[0]
            if secret != RESTART_SECRET:
                self.send_response(403)
                self.end_headers()
                self.wfile.write(b"Forbidden")
                return
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"Restarting...")
            # perform restart
            def do_restart():
                time.sleep(1)
                os.execv(sys.executable, [sys.executable] + sys.argv)
            threading.Thread(target=do_restart).start()
        else:
            self.send_response(404)
            self.end_headers()

def run_http():
    server = HTTPServer(("0.0.0.0", PORT), SimpleHandler)
    logger.info("HTTP server listening on port %s", PORT)
    server.serve_forever()

# Run the bot
async def main():
    # start http server
    threading.Thread(target=run_http, daemon=True).start()

    await bot.start()
    logger.info("Romantic bot started.")

    if assistant:
        try:
            await assistant.start()
            logger.info("Assistant started.")
            global pytgcalls
            if pytgcalls is None:
                pytgcalls = PyTgCalls(assistant)
            try:
                pytgcalls.start()
            except Exception:
                pass
        except Exception as e:
            logger.error("Assistant start failed: %s", e)

    # create frozen_check task
    asyncio.create_task(frozen_check_loop())

    # idle
    logger.info("Bot is running in romantic mode. Press Ctrl+C to stop.")
    # Just keep running
    while True:
        await asyncio.sleep(10)

if __name__ == "__main__":
    try:
        asyncio.get_event_loop().run_until_complete(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Shutting down romantic bot...")
        try:
            asyncio.get_event_loop().run_until_complete(bot.stop())
        except Exception:
            pass
        try:
            if assistant:
                asyncio.get_event_loop().run_until_complete(assistant.stop())
        except Exception:
            pass
