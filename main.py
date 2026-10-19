"""Telegram music bot: Deezer search, metadata, 30s previews, lyrics, link conversion."""
import html
import logging
import os
import re
from urllib.parse import quote

import httpx
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LinkPreviewOptions,
    Update,
)
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

logging.basicConfig(
    format="%(asctime)s %(name)s %(levelname)s %(message)s", level=logging.INFO
)
log = logging.getLogger("musicbot")

BOT_TOKEN = os.environ["BOT_TOKEN"]

DEEZER = "https://api.deezer.com"
ODESLI = "https://api.song.link/v1-alpha.1/links"
LYRICS = "https://api.lyrics.ovh/v1"

MUSIC_LINK = re.compile(
    r"https?://(?:open\.spotify\.com|(?:www\.)?deezer\.com|deezer\.page\.link|"
    r"music\.apple\.com|(?:listen\.)?tidal\.com|music\.youtube\.com|youtu\.be|"
    r"(?:www\.)?youtube\.com|(?:www\.|open\.)?qobuz\.com|music\.amazon\.com|"
    r"soundcloud\.com)\S*",
    re.I,
)

PLATFORMS = {
    "spotify": "Spotify",
    "appleMusic": "Apple Music",
    "deezer": "Deezer",
    "tidal": "Tidal",
    "youtubeMusic": "YouTube Music",
    "youtube": "YouTube",
    "amazonMusic": "Amazon Music",
    "soundcloud": "SoundCloud",
    "napster": "Napster",
    "pandora": "Pandora",
}

NO_PREVIEW = LinkPreviewOptions(is_disabled=True)

HELP = (
    "🎶 <b>Music Bot</b>\n\n"
    "• Send a song name to search\n"
    "• Send a Spotify / Apple Music / Tidal / YouTube link to get it on other platforms\n\n"
    "/search <i>query</i> – search tracks\n"
    "/lyrics <i>artist - title</i> – get lyrics\n"
    "/convert <i>link</i> – convert a music link"
)


# ---------- helpers ----------
async def fetch(context: ContextTypes.DEFAULT_TYPE, url: str, **params):
    http: httpx.AsyncClient = context.application.bot_data["http"]
    try:
        r = await http.get(url, params=params or None)
        r.raise_for_status()
        return r.json()
    except (httpx.HTTPError, ValueError) as e:
        log.warning("fetch failed %s: %s", url, e)
        return None


def chunks(text: str, size: int = 4000):
    for i in range(0, len(text), size):
        yield text[i : i + size]


async def get_track(context, track_id):
    t = await fetch(context, f"{DEEZER}/track/{track_id}")
    if not t or "error" in t:
        return None
    return t


# ---------- features ----------
async def do_search(update: Update, context: ContextTypes.DEFAULT_TYPE, query: str):
    data = await fetch(context, f"{DEEZER}/search", q=query, limit=8)
    tracks = (data or {}).get("data", [])
    if not tracks:
        await update.effective_message.reply_text("No results found.")
        return
    rows = [
        [
            InlineKeyboardButton(
                f"{t['title']} — {t['artist']['name']}"[:60],
                callback_data=f"t:{t['id']}",
            )
        ]
        for t in tracks
    ]
    await update.effective_message.reply_text(
        f"Results for “{query}”:", reply_markup=InlineKeyboardMarkup(rows)
    )


async def show_track(update: Update, context: ContextTypes.DEFAULT_TYPE, track_id: str):
    q = update.callback_query
    await q.answer()
    t = await get_track(context, track_id)
    chat_id = q.message.chat_id
    if not t:
        await context.bot.send_message(chat_id, "Couldn't load that track.")
        return

    mins, secs = divmod(int(t.get("duration", 0)), 60)
    caption = (
        f"<b>{html.escape(t['title'])}</b>\n"
        f"👤 {html.escape(t['artist']['name'])}\n"
        f"💿 {html.escape(t['album']['title'])}\n"
        f"📅 {t.get('release_date', '?')}\n"
        f"⏱ {mins}:{secs:02d}"
    )
    kb = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("📝 Lyrics", callback_data=f"l:{track_id}"),
                InlineKeyboardButton("🔗 Other platforms", callback_data=f"c:{track_id}"),
            ],
            [InlineKeyboardButton("Open on Deezer", url=t["link"])],
        ]
    )
    cover = t["album"].get("cover_xl") or t["album"].get("cover_big")
    if cover:
        await context.bot.send_photo(
            chat_id, cover, caption=caption, parse_mode=ParseMode.HTML, reply_markup=kb
        )
    else:
        await context.bot.send_message(
            chat_id, caption, parse_mode=ParseMode.HTML, reply_markup=kb
        )

    if t.get("preview"):
        try:
            await context.bot.send_audio(
                chat_id,
                t["preview"],
                title=t["title"],
                performer=t["artist"]["name"],
                caption="30-second preview",
            )
        except TelegramError as e:
            log.warning("preview failed: %s", e)


async def send_lyrics(context, chat_id, artist: str, title: str):
    data = await fetch(
        context, f"{LYRICS}/{quote(artist, safe='')}/{quote(title, safe='')}"
    )
    text = ((data or {}).get("lyrics") or "").strip()
    if not text:
        await context.bot.send_message(chat_id, "No lyrics found for that song.")
        return
    for part in chunks(text):
        await context.bot.send_message(chat_id, part)


async def show_lyrics(update: Update, context: ContextTypes.DEFAULT_TYPE, track_id: str):
    q = update.callback_query
    await q.answer("Fetching lyrics…")
    t = await get_track(context, track_id)
    if not t:
        await context.bot.send_message(q.message.chat_id, "Couldn't load that track.")
        return
    await send_lyrics(
        context, q.message.chat_id, t["artist"]["name"], t.get("title_short") or t["title"]
    )


async def convert_link(message, context: ContextTypes.DEFAULT_TYPE, url: str):
    data = await fetch(context, ODESLI, url=url, userCountry="US")
    links = (data or {}).get("linksByPlatform")
    if not links:
        await message.reply_text("Couldn't convert that link.")
        return
    lines = [
        f'• <a href="{html.escape(v["url"], quote=True)}">{PLATFORMS[k]}</a>'
        for k, v in links.items()
        if k in PLATFORMS
    ]
    await message.reply_text(
        "🔗 Also available on:\n" + "\n".join(lines),
        parse_mode=ParseMode.HTML,
        link_preview_options=NO_PREVIEW,
    )


async def show_convert(update: Update, context: ContextTypes.DEFAULT_TYPE, track_id: str):
    q = update.callback_query
    await q.answer("Finding links…")
    await convert_link(q.message, context, f"https://www.deezer.com/track/{track_id}")


# ---------- handlers ----------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP, parse_mode=ParseMode.HTML)


async def cmd_search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = " ".join(context.args).strip()
    if not query:
        await update.message.reply_text("Usage: /search <song or artist>")
        return
    await do_search(update, context, query)


async def cmd_lyrics(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw = " ".join(context.args)
    if " - " not in raw:
        await update.message.reply_text("Usage: /lyrics Artist - Title")
        return
    artist, title = (s.strip() for s in raw.split(" - ", 1))
    await send_lyrics(context, update.effective_chat.id, artist, title)


async def cmd_convert(update: Update, context: ContextTypes.DEFAULT_TYPE):
    m = MUSIC_LINK.search(" ".join(context.args))
    if not m:
        await update.message.reply_text("Usage: /convert <music link>")
        return
    await convert_link(update.message, context, m.group(0))


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    m = MUSIC_LINK.search(text)
    if m:
        await convert_link(update.message, context, m.group(0))
    else:
        await do_search(update, context, text)


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    action, _, value = update.callback_query.data.partition(":")
    handler = {"t": show_track, "l": show_lyrics, "c": show_convert}.get(action)
    if handler:
        await handler(update, context, value)
    else:
        await update.callback_query.answer()


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    log.error("Unhandled error", exc_info=context.error)


async def post_init(app: Application):
    app.bot_data["http"] = httpx.AsyncClient(timeout=15, follow_redirects=True)


async def post_shutdown(app: Application):
    await app.bot_data["http"].aclose()


def main():
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )
    app.add_handler(CommandHandler(["start", "help"], cmd_start))
    app.add_handler(CommandHandler("search", cmd_search))
    app.add_handler(CommandHandler("lyrics", cmd_lyrics))
    app.add_handler(CommandHandler("convert", cmd_convert))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, on_text
        )
    )
    app.add_error_handler(on_error)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
