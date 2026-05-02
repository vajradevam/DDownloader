import discord
from discord.ext import commands
from discord import app_commands
import aiohttp
import asyncio
import os
import re
from datetime import datetime
from pathlib import Path
import mimetypes
import logging
from dotenv import load_dotenv
load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tiff", ".svg", ".avif"}
VIDEO_EXTS = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".flv", ".wmv", ".m4v", ".mpeg", ".mpg"}

MEDIA_URL_PATTERNS = [
    re.compile(r"https?://media\.tenor\.com/"),
    re.compile(r"https?://i\.imgur\.com/"),
    re.compile(r"https?://media\d*\.giphy\.com/"),
    re.compile(r"https?://cdn\.discordapp\.com/attachments/"),
    re.compile(r"https?://media\.discordapp\.net/attachments/"),
]

DOWNLOAD_CONCURRENCY = 10


def is_media_url(url: str) -> bool:
    ext = os.path.splitext(url.split("?")[0].lower())[1]
    if ext in IMAGE_EXTS | VIDEO_EXTS:
        return True
    return any(p.search(url) for p in MEDIA_URL_PATTERNS)


def get_media_type(url: str) -> str:
    ext = os.path.splitext(url.split("?")[0].lower())[1]
    if ext in VIDEO_EXTS:
        return "video"
    if ext in IMAGE_EXTS:
        return "image"
    if any(x in url.lower() for x in [".mp4", ".webm", ".mov", "video"]):
        return "video"
    return "image"


def sanitize_filename(name: str) -> str:
    return re.sub(r'[<>:"/\\|?*\s]', "_", name)


def extract_media(message: discord.Message) -> list:
    items = []
    seen = set()

    def add(url, fname, mtype):
        if url and url not in seen:
            seen.add(url)
            items.append((url, fname, mtype))

    for att in message.attachments:
        ext = os.path.splitext(att.filename)[1].lower()
        if ext in IMAGE_EXTS:
            add(att.url, att.filename, "image")
        elif ext in VIDEO_EXTS:
            add(att.url, att.filename, "video")

    for embed in message.embeds:
        for field in [embed.image, embed.thumbnail]:
            if field and field.url:
                fname = field.url.split("/")[-1].split("?")[0] or "embed"
                add(field.url, fname, "image")
        if embed.video and embed.video.url:
            fname = embed.video.url.split("/")[-1].split("?")[0] or "video"
            add(embed.video.url, fname, "video")

    for url in re.findall(r"https?://\S+", message.content):
        url = url.rstrip(".,)")
        if is_media_url(url):
            fname = url.split("/")[-1].split("?")[0] or "media"
            add(url, fname, get_media_type(url))

    return items


async def download_file(session, url, dest_path, semaphore, failed_log: list):
    async with semaphore:
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=120)) as resp:
                if resp.status != 200:
                    reason = f"HTTP {resp.status}"
                    failed_log.append((url, reason))
                    log.warning(f"FAILED [{reason}] {url}")
                    return False
                ct = resp.headers.get("Content-Type", "")
                if not dest_path.suffix:
                    guessed = mimetypes.guess_extension(ct.split(";")[0].strip())
                    if guessed:
                        dest_path = dest_path.with_suffix(guessed)
                dest_path.parent.mkdir(parents=True, exist_ok=True)
                with open(dest_path, "wb") as f:
                    async for chunk in resp.content.iter_chunked(65536):
                        f.write(chunk)
                return True
        except asyncio.TimeoutError:
            failed_log.append((url, "Timeout"))
            log.warning(f"FAILED [Timeout] {url}")
            return False
        except Exception as e:
            failed_log.append((url, str(e)))
            log.warning(f"FAILED [{e}] {url}")
            return False


async def scrape_channel(channel, target_user, channel_dir, session, semaphore, stats, seen_urls, failed_log, status_cb):
    channel_dir.mkdir(parents=True, exist_ok=True)
    pending_tasks = []
    last_status = asyncio.get_event_loop().time()

    async for message in channel.history(limit=None, oldest_first=False):
        if message.author.id != target_user.id:
            continue

        stats["messages_scanned"] += 1

        now = asyncio.get_event_loop().time()
        if now - last_status >= 4:
            last_status = now
            await status_cb()

        for url, fname, mtype in extract_media(message):
            if url in seen_urls:
                # Already downloaded in a previous channel or earlier in this one
                stats["skipped_duplicate"] += 1
                log.debug(f"SKIPPED [duplicate] {url}")
                continue
            seen_urls.add(url)

            ts = message.created_at.strftime("%Y%m%d_%H%M%S")
            safe = sanitize_filename(f"{ts}_{fname}")
            if len(safe) > 180:
                safe = safe[:170] + os.path.splitext(safe)[1]
            dest = channel_dir / safe

            if dest.exists():
                stats["skipped_exists"] += 1
                log.debug(f"SKIPPED [already on disk] {dest.name}")
                continue

            async def _dl(u=url, d=dest, t=mtype):
                ok = await download_file(session, u, d, semaphore, failed_log)
                if ok:
                    stats["images" if t == "image" else "videos"] += 1
                else:
                    stats["failed"] += 1

            pending_tasks.append(asyncio.create_task(_dl()))

    if pending_tasks:
        await asyncio.gather(*pending_tasks)


@bot.tree.command(name="download_media", description="Download all images/videos from a user across this server")
@app_commands.describe(
    user="The user whose media to download",
    media_type="Images, videos, or both",
    channel="Specific channel (leave blank for ALL channels)",
)
@app_commands.choices(
    media_type=[
        app_commands.Choice(name="Both Images & Videos", value="both"),
        app_commands.Choice(name="Images Only", value="images"),
        app_commands.Choice(name="Videos Only", value="videos"),
    ]
)
@app_commands.checks.has_permissions(manage_messages=True)
async def download_media(
    interaction: discord.Interaction,
    user: discord.Member,
    media_type: app_commands.Choice[str] = None,
    channel: discord.TextChannel = None,
):
    await interaction.response.defer()

    guild = interaction.guild
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_dir = Path("downloads") / sanitize_filename(guild.name) / sanitize_filename(str(user)) / timestamp
    output_dir.mkdir(parents=True, exist_ok=True)

    channels_to_scan = (
        [channel] if channel
        else [ch for ch in guild.text_channels if ch.permissions_for(guild.me).read_message_history]
    )

    status_msg = await interaction.followup.send(
        f"Starting scan for **{user.display_name}** across `{len(channels_to_scan)}` channel(s)...",
        wait=True,
    )

    stats = {
        "images": 0,
        "videos": 0,
        "failed": 0,
        "skipped_duplicate": 0,
        "skipped_exists": 0,
        "messages_scanned": 0,
    }
    seen_urls: set = set()
    failed_log: list = []  # list of (url, reason)
    current_ch_ref = [None]
    ch_index_ref = [0]

    async def update_status():
        ch = current_ch_ref[0]
        try:
            await status_msg.edit(content=(
                f"**Scanning `#{ch.name}`** ({ch_index_ref[0]}/{len(channels_to_scan)})\n"
                f"Messages: `{stats['messages_scanned']:,}` scanned\n"
                f"Images: `{stats['images']:,}` | Videos: `{stats['videos']:,}`\n"
                f"Skipped duplicates: `{stats['skipped_duplicate']:,}` | Already on disk: `{stats['skipped_exists']:,}`\n"
                f"Failed: `{stats['failed']:,}`"
            ))
        except Exception:
            pass

    semaphore = asyncio.Semaphore(DOWNLOAD_CONCURRENCY)
    connector = aiohttp.TCPConnector(limit=DOWNLOAD_CONCURRENCY + 5, ttl_dns_cache=300)

    async with aiohttp.ClientSession(connector=connector) as session:
        for i, ch in enumerate(channels_to_scan, 1):
            current_ch_ref[0] = ch
            ch_index_ref[0] = i
            try:
                log.info(f"[{i}/{len(channels_to_scan)}] Scanning #{ch.name}")
                await update_status()
                ch_dir = output_dir / sanitize_filename(ch.name)
                await scrape_channel(ch, user, ch_dir, session, semaphore, stats, seen_urls, failed_log, update_status)
            except discord.Forbidden:
                log.warning(f"No access to #{ch.name}, skipping.")
            except Exception as e:
                log.error(f"Error in #{ch.name}: {e}")

    # Write failed URLs to a log file for inspection
    failed_log_path = output_dir / "failed_downloads.txt"
    if failed_log:
        with open(failed_log_path, "w") as f:
            f.write(f"Failed downloads — {datetime.now()}\n")
            f.write(f"Total: {len(failed_log)}\n\n")
            for url, reason in failed_log:
                f.write(f"[{reason}] {url}\n")
        log.info(f"Failed URLs written to {failed_log_path}")

    summary = (
        f"**Done! Media for {user.mention}**\n\n"
        f"Saved to: `{output_dir}`\n"
        f"Messages scanned: `{stats['messages_scanned']:,}`\n"
        f"Images downloaded: `{stats['images']:,}`\n"
        f"Videos downloaded: `{stats['videos']:,}`\n"
        f"Skipped — duplicate URL: `{stats['skipped_duplicate']:,}`\n"
        f"Skipped — already on disk: `{stats['skipped_exists']:,}`\n"
        f"Failed: `{stats['failed']:,}`"
        + (f"\nFailed URLs saved to `failed_downloads.txt`" if failed_log else "")
    )
    await status_msg.edit(content=summary)
    log.info(f"Complete: {stats}")


@download_media.error
async def on_error(interaction: discord.Interaction, error):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message("You need **Manage Messages** permission.", ephemeral=True)
    else:
        log.error(f"Command error: {error}")
        try:
            await interaction.response.send_message(f"Error: `{error}`", ephemeral=True)
        except Exception:
            pass


@bot.event
async def on_ready():
    await bot.tree.sync()
    log.info(f"Logged in as {bot.user} — slash commands synced.")


def main():
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        raise ValueError("Set DISCORD_TOKEN environment variable.")
    bot.run(token)


if __name__ == "__main__":
    main()