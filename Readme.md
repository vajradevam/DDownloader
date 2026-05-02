# Discord Media Downloader Bot

Downloads all images and videos sent by a specific user across every channel in a Discord server.

---

## Requirements

- Python 3.10+
- A Discord bot token
- The bot invited to your server with the correct permissions

---

## Project Structure

```
discord_media_bot/
├── bot.py
├── .env               # Your token (never commit this)
├── .env.example       # Template
├── .gitignore
├── requirements.txt
└── downloads/         # Created automatically on first run
    └── ServerName/
        └── Username/
            └── 20240501_153000/
                ├── general/
                │   ├── 20240101_120000_photo.jpg
                │   └── 20240215_183000_clip.mp4
                └── memes/
                    └── 20240310_090000_funny.gif
```

---

## Discord Developer Portal Setup

1. Go to https://discord.com/developers/applications
2. Click **New Application** and give it a name
3. Go to **Bot** in the left sidebar
4. Click **Reset Token**, copy the token
5. Under **Privileged Gateway Intents**, enable all three:
   - Presence Intent
   - Server Members Intent
   - Message Content Intent
6. Go to **OAuth2 > URL Generator**
   - Scopes: `bot`, `applications.commands`
   - Bot Permissions: `Read Messages/View Channels`, `Read Message History`, `Send Messages`
7. Copy the generated URL, open it in a browser, and invite the bot to your server

---

## Local Setup

### 1. Clone or download the project

```bash
cd discord_media_bot
```

### 2. Create and activate a virtual environment

```bash
python -m venv venv

# macOS / Linux
source venv/bin/activate

# Windows
venv\Scripts\activate
```

### 3. Install dependencies

```bash
pip install -r requirements.txt
```

### 4. Set up your token

Copy `.env.example` to `.env`:

```bash
cp .env.example .env
```

Open `.env` and fill in your token:

```
DISCORD_TOKEN=your_token_here
```

### 5. Run the bot

```bash
python bot.py
```

You should see:
```
Logged in as YourBot#1234 — slash commands synced.
```

---

## Usage

In any channel in your Discord server, type:

```
/download_media user:@Username
```

### Parameters

| Parameter    | Required | Description |
|-------------|----------|-------------|
| `user`      | Yes      | The user whose media you want to download |
| `media_type`| No       | `Both Images & Videos` (default), `Images Only`, `Videos Only` |
| `channel`   | No       | Limit to one channel. Leave blank to scan all channels |

> You need the **Manage Messages** permission in Discord to run this command.

---

## What Gets Downloaded

- Attachments (images/videos directly uploaded to Discord)
- Embed images and thumbnails
- Embed videos
- Direct media URLs posted in message text (Discord CDN, Imgur, Tenor, Giphy, etc.)

---

## Output

Files are saved to:
```
downloads/<ServerName>/<Username>/<Timestamp>/<ChannelName>/<date_filename>
```

A `failed_downloads.txt` file is created in the run folder if any downloads fail, listing each URL and the reason it failed.

---

## Tuning Performance

At the top of `bot.py`:

```python
DOWNLOAD_CONCURRENCY = 10
```

Increase this if your internet connection is fast. Lower it if you hit rate limits or errors. 10 is a safe default.

---

## Stopping the Bot

Press `Ctrl+C` in the terminal. Any in-progress downloads will be abandoned but already-saved files are kept. Re-running the command will skip files already on disk.

---

## Troubleshooting

**Session has been invalidated (login loop)**
- Your token is wrong or was reset. Generate a new one in the Developer Portal and update `.env`.
- Make sure all three Privileged Gateway Intents are enabled in the Developer Portal.

**Commands not showing up in Discord**
- Slash commands can take up to 1 hour to propagate globally. Try restarting the bot.

**Bot skips a channel**
- The bot doesn't have `Read Message History` permission in that channel. Grant it in Discord server settings.

**PyNaCl warning on startup**
- Harmless. PyNaCl is only needed for voice features, which this bot doesn't use.