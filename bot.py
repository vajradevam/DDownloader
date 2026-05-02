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
import json
from dotenv import load_dotenv

from rich.console import Console
from rich.logging import RichHandler
from rich.progress import Progress, SpinnerColumn, BarColumn, TextColumn, TimeElapsedColumn, MofNCompleteColumn
from rich.panel import Panel
from rich.table import Table
from rich import box
from rich.text import Text
from rich.rule import Rule

load_dotenv()

console = Console()

logging.basicConfig(
    level=logging.INFO,
    format="%(message)s",
    datefmt="%H:%M:%S",
    handlers=[RichHandler(console=console, rich_tracebacks=True, markup=True, show_path=False)],
)
# Silence noisy discord.py internals
logging.getLogger("discord.gateway").setLevel(logging.WARNING)
logging.getLogger("discord.client").setLevel(logging.WARNING)
logging.getLogger("discord.http").setLevel(logging.WARNING)
logging.getLogger("aiohttp").setLevel(logging.WARNING)

log = logging.getLogger("ddownloader")

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


def is_media_url(url):
    ext = os.path.splitext(url.split("?")[0].lower())[1]
    if ext in IMAGE_EXTS | VIDEO_EXTS:
        return True
    return any(p.search(url) for p in MEDIA_URL_PATTERNS)


def get_media_type(url):
    ext = os.path.splitext(url.split("?")[0].lower())[1]
    if ext in VIDEO_EXTS:
        return "video"
    if ext in IMAGE_EXTS:
        return "image"
    if any(x in url.lower() for x in [".mp4", ".webm", ".mov", "video"]):
        return "video"
    return "image"


def sanitize_filename(name):
    return re.sub(r'[<>:"/\\|?*\s]', "_", name)


def extract_media(message):
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


async def download_file(session, url, dest_path, semaphore, failed_log):
    async with semaphore:
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=120)) as resp:
                if resp.status != 200:
                    reason = "HTTP {}".format(resp.status)
                    failed_log.append((url, reason))
                    log.warning("[yellow]FAILED[/yellow] [dim]{}[/dim] — {}".format(reason, url))
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
            log.warning("[yellow]FAILED[/yellow] [dim]Timeout[/dim] — {}".format(url))
            return False
        except Exception as e:
            failed_log.append((url, str(e)))
            log.warning("[yellow]FAILED[/yellow] [dim]{}[/dim] — {}".format(e, url))
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
        if now - last_status >= 5:
            last_status = now
            await status_cb()

        for url, fname, mtype in extract_media(message):
            if url in seen_urls:
                stats["skipped_duplicate"] += 1
                continue
            seen_urls.add(url)

            ts = message.created_at.strftime("%Y%m%d_%H%M%S")
            safe = sanitize_filename("{}_{}".format(ts, fname))
            if len(safe) > 180:
                safe = safe[:170] + os.path.splitext(safe)[1]
            dest = channel_dir / safe

            if dest.exists():
                stats["skipped_exists"] += 1
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


def print_summary(stats, output_dir, failed_log, user_name, elapsed):
    console.print()
    console.print(Rule("[bold green]Download Complete[/bold green]"))

    table = Table(box=box.ROUNDED, show_header=False, padding=(0, 2))
    table.add_column("Key", style="dim")
    table.add_column("Value", style="bold")

    table.add_row("User",             user_name)
    table.add_row("Output folder",    str(output_dir))
    table.add_row("Time elapsed",     elapsed)
    table.add_row("Messages scanned", "{:,}".format(stats["messages_scanned"]))
    table.add_row("Images",           "[cyan]{:,}[/cyan]".format(stats["images"]))
    table.add_row("Videos",           "[magenta]{:,}[/magenta]".format(stats["videos"]))
    table.add_row("Skipped (dupe)",   "[dim]{:,}[/dim]".format(stats["skipped_duplicate"]))
    table.add_row("Skipped (exists)", "[dim]{:,}[/dim]".format(stats["skipped_exists"]))
    table.add_row("Failed",           "[red]{:,}[/red]".format(stats["failed"]) if stats["failed"] else "[green]0[/green]")
    if failed_log:
        table.add_row("Failed log",   "failed_downloads.txt")

    console.print(table)
    console.print()


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
    started_at = datetime.now()
    timestamp = started_at.strftime("%Y%m%d_%H%M%S")
    output_dir = (
        Path("downloads")
        / sanitize_filename(guild.name)
        / sanitize_filename(str(user))
        / timestamp
    )
    output_dir.mkdir(parents=True, exist_ok=True)

    channels_to_scan = (
        [channel] if channel
        else [ch for ch in guild.text_channels if ch.permissions_for(guild.me).read_message_history]
    )

    console.print()
    console.print(Panel(
        "[bold]User:[/bold] {}\n[bold]Server:[/bold] {}\n[bold]Channels:[/bold] {:,}".format(
            user.display_name, guild.name, len(channels_to_scan)
        ),
        title="[bold blue]Starting Media Download[/bold blue]",
        border_style="blue",
    ))

    status_msg = await interaction.channel.send(
        "Starting scan for **{}** across {} channel(s)...".format(user.display_name, len(channels_to_scan))
    )

    stats = {
        "images": 0, "videos": 0, "failed": 0,
        "skipped_duplicate": 0, "skipped_exists": 0, "messages_scanned": 0,
    }
    seen_urls = set()
    failed_log = []

    semaphore = asyncio.Semaphore(DOWNLOAD_CONCURRENCY)
    connector = aiohttp.TCPConnector(limit=DOWNLOAD_CONCURRENCY + 5, ttl_dns_cache=300)

    async with aiohttp.ClientSession(connector=connector) as session:
        with Progress(
            SpinnerColumn(),
            TextColumn("[bold blue]{task.description}"),
            BarColumn(),
            MofNCompleteColumn(),
            TextColumn("[dim]{task.fields[info]}"),
            TimeElapsedColumn(),
            console=console,
            transient=False,
        ) as progress:
            overall = progress.add_task(
                "Overall",
                total=len(channels_to_scan),
                info="{} channels".format(len(channels_to_scan)),
            )

            for i, ch in enumerate(channels_to_scan, 1):
                ch_task = progress.add_task(
                    "#{:<25}".format(ch.name[:25]),
                    total=None,
                    info="scanning...",
                )
                msgs_before = stats["messages_scanned"]

                async def discord_status_update(ch=ch, i=i, ct=ch_task):
                    msgs_so_far = stats["messages_scanned"] - msgs_before
                    # Update Rich terminal
                    progress.update(
                        ct,
                        completed=msgs_so_far,
                        total=max(msgs_so_far, 1),
                        info="[yellow]scanning...[/yellow] {:,} msgs".format(msgs_so_far),
                    )
                    # Update Discord message
                    try:
                        await status_msg.edit(content="\n".join([
                            "**Scanning `#{}`** ({}/{})".format(ch.name, i, len(channels_to_scan)),
                            "Messages: `{:,}` | Images: `{:,}` | Videos: `{:,}`".format(
                                stats["messages_scanned"], stats["images"], stats["videos"]
                            ),
                            "Skipped: `{:,}` | Failed: `{:,}`".format(
                                stats["skipped_duplicate"] + stats["skipped_exists"], stats["failed"]
                            ),
                        ]))
                    except Exception:
                        pass

                try:
                    ch_dir = output_dir / sanitize_filename(ch.name)
                    await scrape_channel(
                        ch, user, ch_dir, session, semaphore,
                        stats, seen_urls, failed_log, discord_status_update
                    )
                    msgs_found = stats["messages_scanned"] - msgs_before
                    progress.update(
                        ch_task,
                        info="[green]done[/green] — {:,} msgs".format(msgs_found),
                        completed=1,
                        total=1,
                    )
                except discord.Forbidden:
                    progress.update(ch_task, completed=1, total=1, info="[red]no access[/red]")
                    log.warning("No access to [bold]#{}[/bold], skipping.".format(ch.name))
                except Exception as e:
                    progress.update(ch_task, completed=1, total=1, info="[red]error[/red]")
                    log.error("Error in [bold]#{}[/bold]: {}".format(ch.name, e))

                progress.advance(overall)
                await discord_status_update()

    # Write failed log
    failed_log_path = output_dir / "failed_downloads.txt"
    if failed_log:
        with open(failed_log_path, "w") as f:
            f.write("Failed downloads — {}\n".format(datetime.now()))
            f.write("Total: {}\n\n".format(len(failed_log)))
            for url, reason in failed_log:
                f.write("[{}] {}\n".format(reason, url))

    elapsed = str(datetime.now() - started_at).split(".")[0]
    print_summary(stats, output_dir, failed_log, user.display_name, elapsed)

    summary = "\n".join([
        "**Done — {}**\n".format(user.display_name),
        "Saved to: `{}`".format(output_dir),
        "Messages scanned: `{:,}`".format(stats["messages_scanned"]),
        "Images: `{:,}` | Videos: `{:,}`".format(stats["images"], stats["videos"]),
        "Skipped: `{:,}` | Failed: `{:,}`".format(
            stats["skipped_duplicate"] + stats["skipped_exists"], stats["failed"]
        ),
    ] + (["Failed URLs saved to `failed_downloads.txt`"] if failed_log else []))

    try:
        await status_msg.edit(content=summary)
    except Exception:
        await interaction.channel.send(summary)


@download_media.error
async def on_error(interaction: discord.Interaction, error):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message("You need **Manage Messages** permission.", ephemeral=True)
    else:
        log.error("Command error: {}".format(error))
        try:
            await interaction.response.send_message("Error: `{}`".format(error), ephemeral=True)
        except Exception:
            pass


CONTEXT_WINDOW = 5  # how many messages before the target's reply to use as input


def clean_text(text: str) -> str:
    """Strip mentions, excessive whitespace, and bot commands."""
    text = re.sub(r"<@!?\d+>", "", text)       # user mentions
    text = re.sub(r"<#\d+>", "", text)          # channel mentions
    text = re.sub(r"<@&\d+>", "", text)         # role mentions
    text = re.sub(r"<a?:\w+:\d+>", "", text)    # custom emojis
    text = re.sub(r"https?://\S+", "", text)     # URLs
    text = re.sub(r"^\s*/\w+.*$", "", text, flags=re.MULTILINE)  # slash commands
    text = re.sub(r"\n{3,}", "\n\n", text)       # collapse triple+ newlines
    return text.strip()


BATCH_SIZE = 50  # flush to disk every N samples


async def collect_training_data(
    channel: discord.TextChannel,
    target_user: discord.Member,
    context_size: int,
    stats: dict,
    status_cb,
    out_file,
) -> int:
    """
    Streams samples directly to out_file in batches of BATCH_SIZE.
    Returns the number of samples written for this channel.
    """
    channel_samples = 0
    batch = []
    # Sliding window: keep only the last (context_size + buffer) messages
    # Large enough to always have context, small enough to not buffer everything
    window = []
    # Buffer of target user's consecutive messages being collected
    pending_assistant = []
    last_status = asyncio.get_event_loop().time()

    def flush_batch():
        nonlocal batch
        for sample in batch:
            out_file.write(json.dumps(sample, ensure_ascii=False) + "\n")
        out_file.flush()
        batch = []
        log.info("Flushed {:,} samples to disk (total: {:,})".format(len(batch) if batch else BATCH_SIZE, stats["samples"]))

    def emit_sample():
        """Called when we have a complete assistant block and context."""
        nonlocal pending_assistant
        if not pending_assistant:
            return

        assistant_text = "\n".join(pending_assistant)
        pending_assistant = []

        # Get up to context_size non-target messages from window (excluding pending)
        context_msgs = []
        for m_author, m_text in reversed(window):
            if m_author != target_user.id and m_text:
                context_msgs.insert(0, m_text)
            if len(context_msgs) >= context_size:
                break

        if not context_msgs:
            stats["skipped_no_context"] += 1
            return

        messages = [{"role": "user", "content": c} for c in context_msgs]
        messages.append({"role": "assistant", "content": assistant_text})
        batch.append({"messages": messages})
        stats["samples"] += 1

        if len(batch) >= BATCH_SIZE:
            flush_batch()

    async for msg in channel.history(limit=None, oldest_first=True):
        stats["messages_scanned"] += 1

        now = asyncio.get_event_loop().time()
        if now - last_status >= 5:
            last_status = now
            await status_cb()

        if not msg.content or msg.author.bot or msg.type != discord.MessageType.default:
            continue

        text = clean_text(msg.content)

        if msg.author.id == target_user.id:
            # Accumulate consecutive messages from target
            if text:
                pending_assistant.append(text)
        else:
            # New message from someone else — flush any pending assistant block first
            if pending_assistant:
                emit_sample()
                channel_samples += 1
            # Add to sliding context window
            if text:
                window.append((msg.author.id, text))
                # Keep window bounded
                if len(window) > context_size * 3:
                    window.pop(0)

    # Flush any trailing assistant block at end of channel
    if pending_assistant:
        emit_sample()
        channel_samples += 1

    # Flush remaining batch
    if batch:
        flush_batch()

    return channel_samples



@bot.tree.command(name="export_messages", description="Export a user's messages as JSONL for LLM fine-tuning")
@app_commands.describe(
    user="The user whose messages to export",
    channel="Specific channel (leave blank for ALL channels)",
    context_size="How many prior messages to use as context (default 5, max 10)",
)
@app_commands.checks.has_permissions(manage_messages=True)
async def export_messages(
    interaction: discord.Interaction,
    user: discord.Member,
    channel: discord.TextChannel = None,
    context_size: int = CONTEXT_WINDOW,
):
    await interaction.response.defer()

    context_size = max(1, min(context_size, 10))  # clamp 1-10
    guild = interaction.guild
    started_at = datetime.now()
    timestamp = started_at.strftime("%Y%m%d_%H%M%S")
    output_dir = (
        Path("exports")
        / sanitize_filename(guild.name)
        / sanitize_filename(str(user))
        / timestamp
    )
    output_dir.mkdir(parents=True, exist_ok=True)

    channels_to_scan = (
        [channel] if channel
        else [ch for ch in guild.text_channels if ch.permissions_for(guild.me).read_message_history]
    )

    console.print()
    console.print(Panel(
        "[bold]User:[/bold] {}\n[bold]Server:[/bold] {}\n[bold]Channels:[/bold] {:,}\n[bold]Context window:[/bold] {} messages".format(
            user.display_name, guild.name, len(channels_to_scan), context_size
        ),
        title="[bold blue]Exporting Messages for Fine-tuning[/bold blue]",
        border_style="blue",
    ))

    status_msg = await interaction.channel.send(
        "Starting message export for **{}** across {} channel(s)...".format(
            user.display_name, len(channels_to_scan)
        )
    )

    stats = {
        "messages_scanned": 0,
        "samples": 0,
        "skipped_no_context": 0,
    }

    output_file = output_dir / "{}.jsonl".format(sanitize_filename(user.display_name))
    out_file = open(output_file, "w", encoding="utf-8")
    try:
        with Progress(
            SpinnerColumn(),
            TextColumn("[bold blue]{task.description}"),
            BarColumn(),
            MofNCompleteColumn(),
            TextColumn("[dim]{task.fields[info]}"),
            TimeElapsedColumn(),
            console=console,
            transient=False,
        ) as progress:
            overall = progress.add_task("Overall", total=len(channels_to_scan), info="{} channels".format(len(channels_to_scan)))

            for i, ch in enumerate(channels_to_scan, 1):
                ch_task = progress.add_task(
                    "#{:<25}".format(ch.name[:25]),
                    total=None,
                    info="scanning...",
                )
                msgs_before = stats["messages_scanned"]

                async def discord_status_update(ch=ch, i=i, ct=ch_task):
                    msgs_so_far = stats["messages_scanned"] - msgs_before
                    progress.update(
                        ct,
                        completed=msgs_so_far,
                        total=max(msgs_so_far, 1),
                        info="[yellow]scanning...[/yellow] {:,} msgs".format(msgs_so_far),
                    )
                    try:
                        await status_msg.edit(content="\n".join([
                            "**Exporting `#{}`** ({}/{})".format(ch.name, i, len(channels_to_scan)),
                            "Messages scanned: `{:,}` | Samples: `{:,}`".format(
                                stats["messages_scanned"], stats["samples"]
                            ),
                            "Skipped (no context): `{:,}`".format(stats["skipped_no_context"]),
                        ]))
                    except Exception:
                        pass

                try:
                    ch_samples = await collect_training_data(ch, user, context_size, stats, discord_status_update, out_file)
                    msgs_found = stats["messages_scanned"] - msgs_before
                    progress.update(
                        ch_task,
                        completed=1, total=1,
                        info="[green]done[/green] — {:,} msgs, {:,} samples".format(msgs_found, ch_samples),
                    )
                except discord.Forbidden:
                    progress.update(ch_task, completed=1, total=1, info="[red]no access[/red]")
                    log.warning("No access to [bold]#{}[/bold], skipping.".format(ch.name))
                except Exception as e:
                    progress.update(ch_task, completed=1, total=1, info="[red]error[/red]")
                    log.error("Error in [bold]#{}[/bold]: {}".format(ch.name, e))

                progress.advance(overall)
                await discord_status_update()

    finally:
        out_file.flush()
        out_file.close()

    elapsed = str(datetime.now() - started_at).split(".")[0]

    # Terminal summary
    console.print()
    console.print(Rule("[bold green]Export Complete[/bold green]"))
    table = Table(box=box.ROUNDED, show_header=False, padding=(0, 2))
    table.add_column("Key", style="dim")
    table.add_column("Value", style="bold")
    table.add_row("User",              user.display_name)
    table.add_row("Output file",       str(output_file))
    table.add_row("Time elapsed",      elapsed)
    table.add_row("Messages scanned",  "{:,}".format(stats["messages_scanned"]))
    table.add_row("Samples written",   "[green]{:,}[/green]".format(stats["samples"]))
    table.add_row("Skipped",           "[dim]{:,}[/dim]".format(stats["skipped_no_context"]))
    table.add_row("Context window",    "{} messages".format(context_size))
    console.print(table)
    console.print()

    log.info("Wrote {:,} samples to {}".format(stats["samples"], output_file))

    summary = "\n".join([
        "**Export done — {}**\n".format(user.display_name),
        "File: `{}`".format(output_file),
        "Messages scanned: `{:,}`".format(stats["messages_scanned"]),
        "Samples written: `{:,}`".format(stats["samples"]),
        "Skipped (no context): `{:,}`".format(stats["skipped_no_context"]),
        "Context window: `{}` messages".format(context_size),
    ])

    try:
        await status_msg.edit(content=summary)
    except Exception:
        await interaction.channel.send(summary)


@export_messages.error
async def export_messages_error(interaction: discord.Interaction, error):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message("You need **Manage Messages** permission.", ephemeral=True)
    else:
        log.error("Export command error: {}".format(error))
        try:
            await interaction.response.send_message("Error: `{}`".format(error), ephemeral=True)
        except Exception:
            pass


@bot.event
async def on_ready():
    await bot.tree.sync()
    console.print()
    console.print(Panel(
        "[bold green]{}[/bold green]\n[dim]Slash commands synced. Ready.[/dim]".format(bot.user),
        title="[bold]DDownloader[/bold]",
        border_style="green",
    ))


def main():
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        raise ValueError("Set DISCORD_TOKEN environment variable.")
    bot.run(token, log_handler=None)  # suppress discord.py default handler, we use rich


if __name__ == "__main__":
    main()