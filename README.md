# 🕌 Quran Bot for Discord

A Discord bot that streams Quran recitations **24/7** with a **continuous khatma** (full-Quran playlist) in a dedicated voice channel — plus ayahs, Quran search, prayer times, and athkar.

Built with Python and [discord.py](https://github.com/Rapptz/discord.py). All bot code is in English; the Arabic content lives only in the data files (Quran text, athkar, surah/reciter names).

## ✨ Features

| Feature | Description |
|---|---|
| 📻 Auto Quran voice channel | You designate one voice channel once — the bot joins **automatically** when anyone joins it, and leaves when it empties |
| 🔄 Continuous khatma | Plays through all 114 surahs (random 🎲 or in order 📖), announces every completed khatma, then starts a new one — a full khatma roughly every 2–3 days on a 24/7 host |
| 💾 Restart-proof progress | The khatma position is saved to disk; the bot resumes the **exact same surah** after a crash, restart, or redeploy |
| 🎧 On-demand recitations | `/play` any surah with any of **19 reciters** (Alafasy, Al-Husary, Abdul Basit, Al-Minshawi, Maher, ...) with a full queue system |
| 📖 Ayahs | Random or specific ayah with English translation and a quran.com link |
| 🔎 Quran search | Full-text search of the Quran in Arabic |
| 🕌 Prayer times | For any city worldwide + Hijri date, with 5 calculation methods |
| 📿 Athkar | Morning, evening, and bedtime remembrances |
| 🔔 Daily message | Automatic daily ayah (+ prayer times) in a channel of your choice |

## 🚀 Quick start

### 1) Create the bot on Discord (once)

1. Open the [Discord Developer Portal](https://discord.com/developers/applications) → **New Application** → give it a name.
2. Go to **Bot** → **Reset Token** → copy the token. **Never share or commit it.**
3. Build an invite link — replace `CLIENT_ID` with the **Application ID** from *General Information*:

   ```
   https://discord.com/oauth2/authorize?client_id=CLIENT_ID&scope=bot+applications.commands&permissions=3263488
   ```

4. Open the link, pick your server, authorize. The bot appears offline until you run it.

### 2) Run it on your machine (for testing)

Requirements: **Python 3.10+** and **FFmpeg**.

```bash
# Install FFmpeg first:
#   Windows:  winget install ffmpeg
#   macOS:    brew install ffmpeg
#   Linux:    sudo apt install ffmpeg

cd quran-bot
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env             # Windows: copy .env.example .env
# Open .env and put: DISCORD_TOKEN=your_token

python main.py
```

When you see `Logged in as ...` the bot is online. 

> 💡 Slash commands can take up to an hour to appear globally. For instant commands while testing, add `DEV_GUILD_ID=your_server_id` to `.env`.

### 3) Set up the auto channel

In any text channel on your server:

```
/auto set channel:#your-voice-channel
```

That's it — anyone who joins that voice channel gets the bot joining automatically, reciting continuously. To use a different reciter or order:

```
/auto set channel:#quran reciter:Al-Husary order:In order
```

## ☁️ Hosting 24/7 (free)

The bot only streams audio and stores a tiny JSON file, so a free tier is enough.

### Option A — Wispbyte (recommended, no credit card)

1. Sign up at [wispbyte.com](https://wispbyte.com) → create a **Python** server (free plan).
2. Open the **Files** tab and upload the project zip, then **Unarchive** it so the files sit at the server root.
3. Create a file named `.env` with your token inside:
   ```
   DISCORD_TOKEN=your_token
   ```
4. Click **Start** (startup file: `main.py`). The bot stays online 24/7 and restarts automatically after host reboots.

### Option B — Railway / any Docker host

A `Dockerfile` and `nixpacks.toml` (installs FFmpeg automatically) are included. Set `DISCORD_TOKEN` as an environment variable and deploy.

> ⚠️ Railway bills by usage — a 24/7 voice bot burns the free credit within days. Prefer Wispbyte or a VPS for continuous audio.

### Option C — Your own VPS (Oracle Cloud free tier, any Ubuntu box)

```bash
# Upload the quran-bot folder, then inside it:
nano .env                        # put your DISCORD_TOKEN
bash setup_server.sh             # installs ffmpeg + deps, creates a systemd service
sudo systemctl enable --now quran-bot
sudo journalctl -u quran-bot -f  # live logs
```

The service auto-restarts on crashes and on boot.

## 🤖 Commands (17)

| Command | Description |
|---|---|
| `/auto set channel: [reciter:] [order:] [announce:]` | 📻 **Main one**: enable the auto channel + start the continuous khatma |
| `/auto pause` · `/auto resume` | Pause/resume the khatma (position is kept) |
| `/auto status` | Progress: surahs done, completed khatmas, next surah |
| `/auto off` | Disable the auto channel and the khatma |
| `/play surah: [reciter:]` | Play a surah on demand (autocomplete for names & numbers) |
| `/pause` · `/resume` · `/skip` · `/stop` | Playback controls |
| `/queue` · `/volume 0-100` · `/leave` | Queue, volume, leave the channel |
| `/reciters` · `/setreciter` | List reciters / set the server default for `/play` |
| `/ayah [surah:] [ayah:]` | A random ayah, or a specific one |
| `/search query:` | Search the Quran text |
| `/prayertimes city: [country:] [method:]` | Prayer times + Hijri date |
| `/athkar kind:` | Morning / Evening / Bedtime athkar |
| `/daily set channel: [time:] [city:] [tz:]` | Daily ayah (+ prayer times) message |
| `/daily off` · `/daily info` · `/daily preview` | Manage the daily message |
| `/help` | All commands inside Discord |

## 🔄 How the khatma works

1. `/auto set` picks the voice channel, reciter, and surah order.
2. Someone joins the channel → the bot joins and starts from where the khatma left off.
3. A full recitation takes ~60 hours, so a complete khatma lands every 2–3 days on a 24/7 host — the bot posts a 🎉 announcement and starts a new one.
4. Manual `/play` requests jump the queue; the khatma resumes automatically when the queue empties.
5. `/skip` counts as finishing the current surah in the khatma. `/auto pause` suspends it. `/stop` suspends it too (bring it back with `/auto resume`).
6. Progress is persisted in `data/config.json` — restart-safe.

## 📁 Project structure

```
quran-bot/
├── main.py            # bot: all commands, auto-join, daily message, khatma wiring
├── player.py          # audio player: queue, khatma engine, FFmpeg streaming
├── quran_api.py       # API layer: ayahs, search, prayer times, reciter URLs
├── setup_server.sh    # one-shot Ubuntu/VPS setup (ffmpeg + systemd service)
├── requirements.txt
├── Dockerfile         # for Docker-based hosts
├── nixpacks.toml      # for Railway
├── .env.example       # template for your token
└── data/
    ├── surahs.json    # the 114 surahs (names + ayah counts)
    ├── reciters.json  # 19 reciters and their mp3quran.net servers
    ├── athkar.json    # morning/evening/bedtime athkar
    └── config.json    # per-server settings + khatma state (created at runtime)
```

## 🛠 Customization

- **Add a reciter**: grab an `id` from the [mp3quran.net API](https://mp3quran.net/api/v3/reciters?language=ar) and append it to `data/reciters.json` following the same shape.
- **Edit athkar**: modify `data/athkar.json` (keys: `morning`, `evening`, `bedtime`).
- **FFmpeg not on the host?** Set `FFMPEG_PATH=/path/to/ffmpeg` in `.env`, or drop an `ffmpeg` binary next to `main.py` — the bot picks it up automatically.
- **Default reciter**: `/setreciter` in Discord, or pre-set `"reciter": "<reciter_id>"` per guild in `data/config.json`.

## ❓ FAQ

**Slash commands don't show up.** Discord syncs global commands slowly (up to an hour). Add `DEV_GUILD_ID` to `.env` for instant sync in one server, or just wait.

**"FFmpeg is not installed" in chat.** Install FFmpeg on the machine running the bot (see Customization above for the portable-binary trick).

**The bot joined but no announcements appear.** Set an `announce:` text channel in `/auto set`, or make sure the server has a system channel the bot can write to.

**Someone used `/stop`.** The khatma is suspended — bring it back with `/auto resume`.

## 🙏 Credits & data sources

- Quran text & translations: [alquran.cloud](https://alquran.cloud)
- Recitations: [mp3quran.net](https://mp3quran.net)
- Prayer times: [aladhan.com](https://aladhan.com)

> «The best of you are those who learn the Quran and teach it.» — may Allah accept it from you 🌙

## 📄 License

[MIT](LICENSE)
