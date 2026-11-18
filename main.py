# -*- coding: utf-8 -*-
"""
Quran Bot — Discord bot for recitations, ayahs, prayer times and athkar.
Run with:  python main.py
Token goes in a .env file:  DISCORD_TOKEN=...
"""

import asyncio
import json
import os
import random
from datetime import datetime, timezone
from typing import Optional
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv

import quran_api
from quran_api import SURAH_LIST, RECITERS, ATHKAR, find_surah, RECITER_BY_ID
from player import Player, Track

# Voice diagnostics: show DAVE session / transitions / player details in the console
import logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
logging.getLogger("discord.voice_state").setLevel(logging.DEBUG)
logging.getLogger("discord.player").setLevel(logging.DEBUG)

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
DEV_GUILD_ID = os.getenv("DEV_GUILD_ID")  # optional: instant slash commands in a test guild

GREEN = 0x1F8A5B
GOLD = 0xC9A227
CONFIG_PATH = os.path.join(quran_api.DATA_DIR, "config.json")

DEFAULT_TIME = "06:00"
DEFAULT_TZ = "Africa/Cairo"

# ================================================================ config


def load_config() -> dict:
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_config(cfg: dict):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=1)


CONFIG = load_config()

# ================================================================ bot

intents = discord.Intents.default()
intents.voice_states = True


class QuranBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=intents, help_command=None)
        self.player = Player(self)

    async def setup_hook(self):
        await self.tree.sync()
        if DEV_GUILD_ID:  # instant command sync in the dev guild
            g = discord.Object(id=int(DEV_GUILD_ID))
            self.tree.copy_global_to(guild=g)
            await self.tree.sync(guild=g)
        daily_task.start()

    async def on_ready(self):
        await self.change_presence(
            activity=discord.Activity(type=discord.ActivityType.listening, name="the Quran | /help")
        )
        print(f"Logged in as {self.user} | guilds: {len(self.guilds)}")
        asyncio.create_task(self._autorejoin())

    async def _autorejoin(self):
        """If the auto channel has people in it after a restart, rejoin and
        resume the khatma exactly where it stopped."""
        await asyncio.sleep(3)
        for gid, cfg in list(CONFIG.items()):
            try:
                auto_id = cfg.get("auto_channel_id")
                if not auto_id:
                    continue
                guild = self.get_guild(int(gid))
                if not guild:
                    continue
                ch = guild.get_channel(int(auto_id))
                if not ch:
                    continue
                async with voice_lock(guild.id):
                    if guild.voice_client and guild.voice_client.is_connected():
                        continue
                    if not any(not m.bot for m in ch.members):
                        continue
                    restore_khatma_if_needed(guild)
                    state = self.player.get_state(guild)
                    k = state.khatma
                    if not k.active or k.suspended:
                        continue
                    ach = guild.get_channel(k.announce_channel_id) if k.announce_channel_id else None
                    state.last_channel = ach or guild.system_channel or ch
                    vc = await ensure_voice(guild, ch)
                    if vc is None:
                        continue
                    if not vc.is_playing() and not vc.is_paused():
                        await self.player.play_next(guild)
                print(f"Resumed khatma in guild: {guild.name}")
            except Exception as exc:
                print(f"[autorejoin] error in guild {gid}: {exc!r}")


bot = QuranBot()


# ================================================================ khatma persistence


def persist_khatma(changed_guild=None):
    """Save the khatma state of every guild into config.json (called by the player)."""
    dirty = False
    for gid, st in bot.player.states.items():
        k = st.khatma
        if k.active:
            CONFIG.setdefault(str(gid), {})["khatma"] = {
                "reciter_id": k.reciter_id,
                "mode": k.mode,
                "order": k.order,
                "index": k.index,
                "completed": k.completed,
                "announce_channel_id": k.announce_channel_id,
            }
            dirty = True
    if dirty:
        save_config(CONFIG)


bot.player.persist = persist_khatma


def restore_khatma_if_needed(guild: discord.Guild):
    """If a khatma is saved for this guild but the player doesn't have it yet
    (e.g. after a restart), restore it."""
    st = bot.player.get_state(guild)
    if st.khatma.active:
        return
    saved = CONFIG.get(str(guild.id), {}).get("khatma")
    if saved:
        bot.player.setup_khatma(guild, saved=saved)


# ================================================================ voice helpers

# One lock per guild so two tasks (a voice event + the rejoin after a restart,
# or two people joining at once) never call connect() at the same time —
# that was the "Already connected to a voice channel" error.
VOICE_LOCKS: dict[int, asyncio.Lock] = {}


def voice_lock(guild_id: int) -> asyncio.Lock:
    lock = VOICE_LOCKS.get(guild_id)
    if lock is None:
        lock = VOICE_LOCKS[guild_id] = asyncio.Lock()
    return lock


async def ensure_voice(guild: discord.Guild, channel: discord.VoiceChannel):
    """Join `channel` (or move into it) safely. Returns the connected
    VoiceClient, or None if it couldn't connect. Call it inside voice_lock()."""
    vc = guild.voice_client
    try:
        if vc is not None and not vc.is_connected():
            # a connection is still being set up / reconnecting: give it a moment
            for _ in range(10):
                await asyncio.sleep(0.5)
                if vc.is_connected():
                    break
            else:
                # stale client from a dropped connection: clean it up
                await vc.disconnect(force=True)
                vc = None
        if vc is None:
            vc = await channel.connect(self_deaf=True)
        elif vc.channel is None or vc.channel.id != channel.id:
            await vc.move_to(channel)
    except discord.ClientException:
        # something else connected first: just use that connection
        vc = guild.voice_client
    except (discord.HTTPException, asyncio.TimeoutError) as exc:
        print(f"[voice] could not connect in {guild.name}: {exc!r}")
        return None
    if vc is None or not vc.is_connected():
        return None
    return vc


# ================================================================ helpers

RECITER_CHOICES = [
    app_commands.Choice(name=f"{r['reciter']} - {r['moshaf']}", value=r["id"])
    for r in RECITERS
]


async def surah_autocomplete(interaction: discord.Interaction, current: str):
    current = (current or "").strip()
    nq = quran_api._normalize(current).replace("-", "").replace(" ", "")
    out = []
    for s in SURAH_LIST:
        if (
            not nq
            or nq in str(s["number"])
            or nq in s["norm"]
            or nq in s["norm_en"]
        ):
            out.append(app_commands.Choice(
                name=f"{s['number']}. {s['name_en']} ({s['name_ar']})",
                value=str(s["number"]),
            ))
        if len(out) == 25:
            break
    return out


def ayah_embed(a: dict, color: int = GREEN) -> discord.Embed:
    e = discord.Embed(
        title=f"Surah {a['surah_name_en']} ({a['surah_name']}) - Ayah {a['ayah']}",
        description=f"{a['arabic']}",
        color=color,
    )
    e.add_field(name="Translation (Sahih International)", value=a["translation"], inline=False)
    e.add_field(
        name="Reference",
        value=f"[quran.com/{a['surah']}/{a['ayah']}](https://quran.com/{a['surah']}/{a['ayah']})",
        inline=False,
    )
    e.set_footer(text="\"The best of you are those who learn the Quran and teach it.\"")
    return e


async def build_daily_embed(cfg: dict) -> list[discord.Embed]:
    """Daily message: a random ayah + prayer times if configured."""
    embeds = [await random_ayah_embed()]
    if cfg.get("city"):
        pt = await quran_api.prayer_times(cfg["city"], cfg.get("country", "Egypt"), 5)
        if pt:
            embeds.append(prayer_embed(cfg["city"], cfg.get("country", "Egypt"), pt))
    return embeds


async def random_ayah_embed() -> discord.Embed:
    a = await quran_api.random_ayah()
    return ayah_embed(a, GOLD)


def prayer_embed(city: str, country: str, pt: dict) -> discord.Embed:
    rows = "\n".join(
        f"**{k}:**  `{v}`"
        for k, v in pt["timings"].items()
        if k in ("Fajr", "Sunrise", "Dhuhr", "Asr", "Maghrib", "Isha")
    )
    e = discord.Embed(
        title=f"Prayer times - {city}, {country}",
        description=rows,
        color=GREEN,
    )
    e.set_footer(text=f"{pt['hijri']}  -  {pt['gregorian']}  -  source: AlAdhan")
    return e


def split_athkar(items: list, title: str) -> list[discord.Embed]:
    """Split athkar across multiple embeds when the text is too long."""
    chunks, cur, size = [], [], 0
    for i, z in enumerate(items, 1):
        line = f"**{i}.** {z}\n\n"
        if size + len(line) > 3800 and cur:
            chunks.append(cur)
            cur, size = [], 0
        cur.append(line)
        size += len(line)
    if cur:
        chunks.append(cur)

    embeds = []
    for idx, chunk in enumerate(chunks):
        e = discord.Embed(
            title=title if idx == 0 else f"{title} (cont.)",
            description="".join(chunk).strip(),
            color=GREEN,
        )
        embeds.append(e)
    embeds[-1].set_footer(text="From Hisn al-Muslim")
    return embeds


# ================================================================ voice commands

@app_commands.guild_only()
@bot.tree.command(name="play", description="Play a surah recitation in your voice channel")
@app_commands.autocomplete(surah=surah_autocomplete)
@app_commands.choices(reciter=RECITER_CHOICES)
@app_commands.describe(
    surah="Surah name or number (e.g. Baqarah or 2)",
    reciter="Reciter (default: Mishary Alafasy)",
)
async def play_cmd(interaction: discord.Interaction, surah: str,
                   reciter: Optional[app_commands.Choice[str]] = None):
    s = find_surah(surah)
    if not s:
        return await interaction.response.send_message(
            "Can't find that surah! Type its name (e.g. `Baqarah`) or a number from 1 to 114.",
            ephemeral=True)
    if interaction.user.voice is None or interaction.user.voice.channel is None:
        return await interaction.response.send_message(
            "You need to join a voice channel first!", ephemeral=True)

    r = RECITER_BY_ID.get(reciter.value) if reciter else None
    if r is None:
        gid = str(interaction.guild_id)
        default_id = CONFIG.get(gid, {}).get("reciter", RECITERS[0]["id"])
        r = RECITER_BY_ID.get(default_id, RECITERS[0])
    url = quran_api.audio_url(r["id"], s["number"])
    if url is None:
        return await interaction.response.send_message(
            "That recitation isn't available for the chosen reciter. Try another one.",
            ephemeral=True)

    await interaction.response.defer()
    track = Track(
        url=url,
        title=f"{s['name_en']} ({s['name_ar']}) - {r['reciter']}",
        surah_name=s["name_en"],
        reciter=f"{r['reciter']} ({r['moshaf']})",
        requester=interaction.user.display_name,
    )
    position = await bot.player.enqueue(interaction, track)
    if position > 0:
        await interaction.followup.send(f"Added to queue (position {position}): **{track.title}**")
    else:
        await interaction.followup.send(f"Now playing: **{track.title}**")


@app_commands.guild_only()
@bot.tree.command(name="pause", description="Pause the recitation")
async def pause_cmd(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if vc and vc.is_playing():
        vc.pause()
        await interaction.response.send_message("Paused. Use `/resume` to continue.")
    else:
        await interaction.response.send_message("Nothing is playing right now.", ephemeral=True)


@app_commands.guild_only()
@bot.tree.command(name="resume", description="Resume the recitation")
async def resume_cmd(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if vc and vc.is_paused():
        vc.resume()
        await interaction.response.send_message("Resumed.")
    else:
        await interaction.response.send_message("Nothing is paused.", ephemeral=True)


@app_commands.guild_only()
@bot.tree.command(name="skip", description="Skip the current surah")
async def skip_cmd(interaction: discord.Interaction):
    if bot.player.skip(interaction.guild):
        await interaction.response.send_message("Skipped! Playing the next one...")
    else:
        await interaction.response.send_message("Nothing to skip.", ephemeral=True)


@app_commands.guild_only()
@bot.tree.command(name="stop", description="Stop playback and clear the queue")
async def stop_cmd(interaction: discord.Interaction):
    await bot.player.stop_and_clear(interaction.guild)
    k = bot.player.get_state(interaction.guild).khatma
    if k.active:
        await interaction.response.send_message(
            "Stopped everything - the khatma is suspended. Bring it back with `/auto resume`.")
    else:
        await interaction.response.send_message("Stopped everything and cleared the queue.")


@app_commands.guild_only()
@bot.tree.command(name="leave", description="Make the bot leave the voice channel")
async def leave_cmd(interaction: discord.Interaction):
    await bot.player.disconnect(interaction.guild)
    await interaction.response.send_message("Left the voice channel. Bye!")


@app_commands.guild_only()
@bot.tree.command(name="volume", description="Set the playback volume (0-100)")
@app_commands.describe(level="Volume percentage from 0 to 100")
async def volume_cmd(interaction: discord.Interaction, level: app_commands.Range[int, 0, 100]):
    state = bot.player.get_state(interaction.guild)
    state.volume = level / 100
    vc = interaction.guild.voice_client
    if vc and vc.source and isinstance(vc.source, discord.PCMVolumeTransformer):
        vc.source.volume = state.volume
    await interaction.response.send_message(f"Volume set to **{level}%**")


@app_commands.guild_only()
@bot.tree.command(name="queue", description="Show the playback queue")
async def queue_cmd(interaction: discord.Interaction):
    state = bot.player.get_state(interaction.guild)
    if not state.queue:
        return await interaction.response.send_message("The queue is empty. Try `/play`!", ephemeral=True)
    lines = [f"**{i}.** {t.title} (by {t.requester})" for i, t in enumerate(state.queue[:15], 1)]
    extra = f"\n... and {len(state.queue) - 15} more" if len(state.queue) > 15 else ""
    e = discord.Embed(title=f"Queue ({len(state.queue)} surahs)",
                      description="\n".join(lines) + extra, color=GREEN)
    await interaction.response.send_message(embed=e)


# ================================================================ reciters

@bot.tree.command(name="reciters", description="List the available reciters")
async def reciters_cmd(interaction: discord.Interaction):
    lines = [f"- **{r['reciter']}** - {r['moshaf']}" for r in RECITERS]
    e = discord.Embed(
        title="Available reciters",
        description="\n".join(lines),
        color=GREEN,
    )
    e.set_footer(text="Change the default with /setreciter or pick one in /play")
    await interaction.response.send_message(embed=e)


@bot.tree.command(name="setreciter", description="Set the default reciter for this server")
@app_commands.default_permissions(manage_guild=True)
@app_commands.choices(reciter=RECITER_CHOICES)
@app_commands.describe(reciter="The reciter that /play will use by default")
async def setreciter_cmd(interaction: discord.Interaction, reciter: app_commands.Choice[str]):
    gid = str(interaction.guild_id)
    CONFIG.setdefault(gid, {})["reciter"] = reciter.value
    save_config(CONFIG)
    r = RECITER_BY_ID[reciter.value]
    await interaction.response.send_message(
        f"Default reciter is now: **{r['reciter']} ({r['moshaf']})**")


# ================================================================ ayahs & search

@bot.tree.command(name="ayah", description="A random ayah, or a specific one (surah + ayah number)")
@app_commands.autocomplete(surah=surah_autocomplete)
@app_commands.describe(surah="Surah name or number (leave empty for a random ayah)",
                       ayah="Ayah number within the surah")
async def ayah_cmd(interaction: discord.Interaction,
                   surah: Optional[str] = None,
                   ayah: Optional[app_commands.Range[int, 1, 286]] = None):
    await interaction.response.defer()
    if surah:
        s = find_surah(surah)
        if not s:
            return await interaction.followup.send("Can't find that surah!")
        a = await quran_api.get_ayah(s["number"], ayah)
        if a is None:
            return await interaction.followup.send(
                f"This surah has only {s['ayahs']} ayahs. Pick a number from 1 to {s['ayahs']}.")
    else:
        a = await quran_api.random_ayah()
    view = discord.ui.View()
    view.add_item(discord.ui.Button(label="Open on quran.com",
                                    url=f"https://quran.com/{a['surah']}/{a['ayah']}"))
    await interaction.followup(embed=ayah_embed(a), view=view)


@bot.tree.command(name="search", description="Search inside the Quran text")
@app_commands.describe(query="The word or phrase to look for")
async def search_cmd(interaction: discord.Interaction, query: str):
    await interaction.response.defer()
    try:
        res = await quran_api.search_quran(query.strip()[:100], limit=10)
    except Exception:
        return await interaction.followup.send("Something went wrong with the search. Try another word.")
    if not res["results"]:
        return await interaction.followup.send(f"No results for **{query}**")
    lines = []
    for m in res["results"]:
        snippet = m["text"][:150] + ("..." if len(m["text"]) > 150 else "")
        lines.append(f"**[{m['surah']}:{m['ayah']}] {m['surah_name']}**\n{snippet}\n")
    e = discord.Embed(
        title=f"Search results for: {query}",
        description="\n".join(lines)[:4000],
        color=GREEN,
    )
    e.set_footer(text=f"Total matches: {res['count']} ayahs - showing the first {len(res['results'])}")
    await interaction.followup.send(embed=e)


# ================================================================ prayer times

@bot.tree.command(name="prayertimes", description="Prayer times for any city")
@app_commands.choices(method=[
    app_commands.Choice(name="Egyptian General Authority", value=5),
    app_commands.Choice(name="Muslim World League", value=3),
    app_commands.Choice(name="Umm al-Qura (Makkah)", value=4),
    app_commands.Choice(name="Islamic University (Karachi)", value=1),
    app_commands.Choice(name="ISNA (North America)", value=2),
])
@app_commands.describe(city="City (e.g. Cairo, Dubai)",
                       country="Country (default: Egypt)",
                       method="Calculation method")
async def prayertimes_cmd(interaction: discord.Interaction, city: str,
                          country: str = "Egypt",
                          method: Optional[app_commands.Choice[int]] = None):
    await interaction.response.defer()
    pt = await quran_api.prayer_times(city, country, method.value if method else 5)
    if pt is None:
        return await interaction.followup.send(
            "Can't find that city. Try writing it in English (e.g. `Cairo`, `Dubai`).")
    await interaction.followup(embed=prayer_embed(city, country, pt))


# ================================================================ athkar

@bot.tree.command(name="athkar", description="Morning, evening and bedtime athkar")
@app_commands.choices(kind=[
    app_commands.Choice(name="Morning", value="morning"),
    app_commands.Choice(name="Evening", value="evening"),
    app_commands.Choice(name="Bedtime", value="bedtime"),
])
@app_commands.describe(kind="Which athkar")
async def athkar_cmd(interaction: discord.Interaction, kind: app_commands.Choice[str]):
    items = ATHKAR.get(kind.value, [])
    embeds = split_athkar(items, f"{kind.name} athkar")
    await interaction.response.send_message(embeds=embeds)


# ================================================================ live radio

# Live Quran radio streams (mp3quran.net / qurango.net icecast relays)
RADIO_STATIONS = {
    "alafasy":  "https://backup.qurango.net/radio/mishary_alafasi",
    "hussary":  "https://backup.qurango.net/radio/mahmoud_khalil_alhussary",
    "maher":    "https://backup.qurango.net/radio/maher",
    "sudais":   "https://backup.qurango.net/radio/abdulrahman_alsudaes",
    "yasser":   "https://backup.qurango.net/radio/yasser_aldosari",
    "minshawi": "https://backup.qurango.net/radio/mohammed_siddiq_alminshawi",
}


@bot.tree.command(name="radio", description="Play a live Quran radio station (non-stop stream)")
@app_commands.choices(station=[
    app_commands.Choice(name="Mishary Alafasy", value="alafasy"),
    app_commands.Choice(name="Mahmoud Khalil Al-Hussary", value="hussary"),
    app_commands.Choice(name="Maher Al-Muaiqly", value="maher"),
    app_commands.Choice(name="Abdulrahman As-Sudais", value="sudais"),
    app_commands.Choice(name="Yasser Al-Dosari", value="yasser"),
    app_commands.Choice(name="Mohammed Siddiq Al-Minshawi", value="minshawi"),
])
@app_commands.describe(station="Which station to play",
                       channel="Voice channel (optional - defaults to yours)")
async def radio_cmd(interaction: discord.Interaction, station: app_commands.Choice[str],
                    channel: Optional[discord.VoiceChannel] = None):
    await interaction.response.defer()

    guild = interaction.guild
    state = bot.player.get_state(guild)
    state.last_channel = interaction.channel

    vc: discord.VoiceClient = guild.voice_client
    target = channel or (interaction.user.voice.channel if interaction.user.voice else None)
    if target is None:
        return await interaction.followup.send(
            "Join a voice channel first, or pass channel: explicitly.")
    if vc is None or not vc.is_connected():
        await target.connect(self_deaf=True)
    elif vc.channel.id != target.id:
        await vc.move_to(target)

    name = station.name
    url = RADIO_STATIONS[station.value]
    track = Track(url=url, title=f"Live Radio - {name}", surah_name="Live radio",
                  reciter=name, requester="Radio")

    # radio takes over: clear the queue and pause the khatma while listening
    state.queue.clear()
    khatma_was_active = state.khatma.active and not state.khatma.suspended
    if state.khatma.active:
        state.khatma.suspended = True
        bot.player.persist(guild)

    vc = guild.voice_client
    if vc.is_playing() or vc.is_paused():
        vc.stop()  # the after-callback picks the radio track from the queue
    else:
        await bot.player.play_next(guild)

    e = discord.Embed(title="📻 Radio", description=f"**{name}** - live 24/7 stream",
                      color=GOLD)
    if khatma_was_active:
        e.set_footer(text="Khatma paused while the radio plays - /auto resume brings it back")
    await interaction.followup.send(embed=e)


# ================================================================ auto channel + continuous khatma

auto_group = app_commands.Group(
    name="auto",
    description="Auto Quran voice channel with a continuous khatma",
    guild_only=True,
)


def _auto_check(interaction: discord.Interaction) -> bool:
    return interaction.user.guild_permissions.manage_guild


@auto_group.command(name="set", description="Enable the auto channel and start the continuous khatma")
@app_commands.default_permissions(manage_guild=True)
@app_commands.choices(reciter=RECITER_CHOICES, order=[
    app_commands.Choice(name="Random (shuffled every khatma)", value="random"),
    app_commands.Choice(name="In order (Al-Fatiha to An-Nas)", value="sequential"),
])
@app_commands.describe(
    channel="The voice channel the bot joins automatically when someone joins",
    reciter="Reciter (default: Mishary Alafasy)",
    order="Surah order for the khatma",
    announce="Text channel for surah/khatma announcements (optional)",
)
async def auto_set(interaction: discord.Interaction, channel: discord.VoiceChannel,
                   reciter: Optional[app_commands.Choice[str]] = None,
                   order: Optional[app_commands.Choice[str]] = None,
                   announce: Optional[discord.TextChannel] = None):
    if not _auto_check(interaction):
        return await interaction.response.send_message(
            "You need the **Manage Server** permission.", ephemeral=True)

    r = RECITER_BY_ID.get(reciter.value) if reciter else RECITERS[0]
    mode = order.value if order else "random"

    gid = str(interaction.guild_id)
    CONFIG.setdefault(gid, {})
    CONFIG[gid]["auto_channel_id"] = channel.id
    CONFIG[gid]["announce_channel_id"] = announce.id if announce else None
    save_config(CONFIG)

    bot.player.setup_khatma(interaction.guild, r["id"], mode,
                            announce.id if announce else None)

    msg = (f"All set!\n"
           f"Radio channel: {channel.mention} - the bot joins **automatically** when someone joins.\n"
           f"Reciter: **{r['reciter']}** ({r['moshaf']})\n"
           f"Order: **{'Random' if mode == 'random' else 'Sequential (Al-Fatiha to An-Nas)'}**\n"
           f"Announcements: {announce.mention if announce else 'the server system channel'}\n\n"
           f"Info: a full Quran recitation takes about 60 hours, so with the bot running 24/7 "
           f"a khatma completes every 2-3 days, and it announces each completion.\n"
           f"The khatma resumes exactly where it stopped, even after a restart.")
    # if the bot is already sitting in that channel, start right away
    vc = interaction.guild.voice_client
    if vc and vc.is_connected() and vc.channel.id == channel.id and not vc.is_playing():
        await interaction.response.send_message(msg)
        await bot.player.play_next(interaction.guild)
    else:
        await interaction.response.send_message(msg)


@auto_group.command(name="pause", description="Pause the khatma")
@app_commands.default_permissions(manage_guild=True)
async def auto_pause(interaction: discord.Interaction):
    if not _auto_check(interaction):
        return await interaction.response.send_message(
            "You need the **Manage Server** permission.", ephemeral=True)
    k = bot.player.get_state(interaction.guild).khatma
    if not k.active:
        return await interaction.response.send_message(
            "No khatma is running. Enable it with `/auto set`.", ephemeral=True)
    await bot.player.khatma_pause(interaction.guild)
    await interaction.response.send_message(
        "Khatma paused. Resume it with `/auto resume` - the position is saved.")


@auto_group.command(name="resume", description="Resume the khatma where it stopped")
@app_commands.default_permissions(manage_guild=True)
async def auto_resume(interaction: discord.Interaction):
    if not _auto_check(interaction):
        return await interaction.response.send_message(
            "You need the **Manage Server** permission.", ephemeral=True)
    k = bot.player.get_state(interaction.guild).khatma
    if not k.active:
        return await interaction.response.send_message(
            "No khatma is running. Enable it with `/auto set`.", ephemeral=True)
    await bot.player.khatma_resume(interaction.guild)
    await interaction.response.send_message("Khatma resumed.")


@auto_group.command(name="status", description="Show khatma progress")
async def auto_status(interaction: discord.Interaction):
    guild = interaction.guild
    cfg = CONFIG.get(str(guild.id), {})
    st = bot.player.get_state(guild)
    if not st.khatma.active:
        restore_khatma_if_needed(guild)  # restore after a restart
    info = bot.player.khatma_status(guild)
    if not info["active"]:
        return await interaction.response.send_message(
            "No khatma is enabled. Set everything up with `/auto set`.", ephemeral=True)
    r = RECITER_BY_ID.get(info["reciter_id"])
    vc = guild.voice_client
    status_line = ("Paused" if info["suspended"]
                   else ("Playing now" if vc and vc.is_connected()
                         else "Waiting for someone to join"))
    lines = [
        f"Auto channel: <#{cfg.get('auto_channel_id') or '-'}>",
        f"Reciter: {r['reciter'] if r else '-'}",
        f"Order: {'Random' if info['mode'] == 'random' else 'Sequential'}",
        f"Progress: {info['done']} / {info['total']} surahs ({round(100 * info['done'] / max(info['total'], 1))}%)",
        f"Next surah: {info['next_surah']}",
        f"Completed khatmas: {info['completed']}",
        f"Status: {status_line}",
    ]
    e = discord.Embed(title="Khatma status", description="\n".join(lines), color=GREEN)
    await interaction.response.send_message(embed=e)


@auto_group.command(name="off", description="Disable the auto channel and the khatma")
@app_commands.default_permissions(manage_guild=True)
async def auto_off(interaction: discord.Interaction):
    if not _auto_check(interaction):
        return await interaction.response.send_message(
            "You need the **Manage Server** permission.", ephemeral=True)
    guild = interaction.guild
    st = bot.player.get_state(guild)
    st.khatma = type(st.khatma)()  # reset
    gid = str(guild.id)
    if gid in CONFIG:
        CONFIG[gid].pop("khatma", None)
        CONFIG[gid]["auto_channel_id"] = None
        save_config(CONFIG)
    await bot.player.disconnect(guild)
    await interaction.response.send_message("Disabled the auto channel and the khatma.")


bot.tree.add_command(auto_group)


# ================================================================ daily message

daily_group = app_commands.Group(
    name="daily",
    description="Daily message (ayah + prayer times) in a chosen channel",
    guild_only=True,
)


@app_commands.default_permissions(manage_guild=True)
@daily_group.command(name="set", description="Configure the daily message channel and time")
@app_commands.describe(
    channel="The channel that receives the daily message",
    time="Time in HH:MM format (default 06:00)",
    city="City for prayer times (optional)",
    country="Country (default: Egypt)",
    tz="Timezone (default: Africa/Cairo)",
)
async def daily_set(interaction: discord.Interaction, channel: discord.TextChannel,
                    time: str = DEFAULT_TIME, city: Optional[str] = None,
                    country: str = "Egypt", tz: str = DEFAULT_TZ):
    # validate the time
    try:
        hh, mm = map(int, time.split(":"))
        assert 0 <= hh < 24 and 0 <= mm < 60
        time = f"{hh:02d}:{mm:02d}"
    except (ValueError, AssertionError):
        return await interaction.response.send_message(
            "Invalid time format! Use something like `06:00`.", ephemeral=True)
    # validate the timezone
    try:
        ZoneInfo(tz)
    except Exception:
        return await interaction.response.send_message(
            f"Unknown timezone `{tz}`. Examples: `Africa/Cairo`, `Asia/Riyadh`", ephemeral=True)
    # validate the city if given
    if city:
        pt = await quran_api.prayer_times(city, country, 5)
        if pt is None:
            return await interaction.response.send_message(
                "Can't find that city for prayer times. Try it in English.", ephemeral=True)

    gid = str(interaction.guild_id)
    reciter = CONFIG.get(gid, {}).get("reciter")
    CONFIG[gid] = {"channel_id": channel.id, "time": time, "city": city,
                   "country": country, "tz": tz, "last_sent": None}
    if reciter:
        CONFIG[gid]["reciter"] = reciter
    save_config(CONFIG)

    msg = (f"Done! I'll send a **daily ayah every day at `{time}`** ({tz}) in {channel.mention}")
    if city:
        msg += f"\nPlus prayer times for **{city}, {country}**"
    await interaction.response.send_message(msg + "\nTip: use `/daily preview` to see what it looks like.")


@app_commands.default_permissions(manage_guild=True)
@daily_group.command(name="off", description="Disable the daily message")
async def daily_off(interaction: discord.Interaction):
    gid = str(interaction.guild_id)
    if gid in CONFIG:
        CONFIG[gid]["channel_id"] = None
        save_config(CONFIG)
        await interaction.response.send_message("Daily message disabled. (Settings are kept in case you re-enable it.)")
    else:
        await interaction.response.send_message("No daily message is configured.", ephemeral=True)


@daily_group.command(name="info", description="Show the daily message settings")
async def daily_info(interaction: discord.Interaction):
    cfg = CONFIG.get(str(interaction.guild_id))
    if not cfg or not cfg.get("channel_id"):
        return await interaction.response.send_message(
            "No daily message configured. Set it up with `/daily set`.", ephemeral=True)
    ch = f"<#{cfg['channel_id']}>"
    msg = (f"**Daily message**\n"
           f"Channel: {ch}\n"
           f"Time: `{cfg['time']}` ({cfg.get('tz', DEFAULT_TZ)})\n"
           f"Prayer times city: {cfg.get('city') or '-'}")
    await interaction.response.send_message(msg)


@daily_group.command(name="preview", description="Preview the daily message")
async def daily_preview(interaction: discord.Interaction):
    await interaction.response.defer()
    cfg = CONFIG.get(str(interaction.guild_id), {})
    embeds = await build_daily_embed(cfg)
    await interaction.followup(content="Here is what the daily message looks like:", embeds=embeds)


bot.tree.add_command(daily_group)


@tasks.loop(minutes=1)
async def daily_task():
    """Checks every minute: is it time to send the daily message in any guild?"""
    now_utc = datetime.now(timezone.utc)
    for gid, cfg in list(CONFIG.items()):
        try:
            if not cfg.get("channel_id"):
                continue
            tz = ZoneInfo(cfg.get("tz", DEFAULT_TZ))
            now = now_utc.astimezone(tz)
            today = now.date().isoformat()
            if now.strftime("%H:%M") != cfg.get("time", DEFAULT_TIME):
                continue
            if cfg.get("last_sent") == today:
                continue
            channel = bot.get_channel(int(cfg["channel_id"]))
            if channel is None:
                channel = await bot.fetch_channel(int(cfg["channel_id"]))
            embeds = await build_daily_embed(cfg)
            await channel.send(embeds=embeds)
            cfg["last_sent"] = today
            save_config(CONFIG)
        except Exception as exc:
            print(f"[daily] error in guild {gid}: {exc!r}")


# ================================================================ auto join / auto leave

@bot.event
async def on_voice_state_update(member, before, after):
    if member.bot:
        # if the bot itself left, cancel the idle task
        if member.id == bot.user.id and after.channel is None:
            bot.player.cancel_idle(member.guild)
        return

    # mute / deafen / camera / stream changes: nothing to do
    if before.channel == after.channel:
        return

    guild = member.guild
    cfg = CONFIG.get(str(guild.id), {})
    auto_id = cfg.get("auto_channel_id")

    # auto join: someone joined the dedicated channel
    if auto_id and after.channel and after.channel.id == auto_id:
        async with voice_lock(guild.id):
            # re-check inside the lock: another task may have joined already
            vc = guild.voice_client
            already_here = (vc is not None and vc.is_connected()
                            and vc.channel is not None and vc.channel.id == auto_id)
            restore_khatma_if_needed(guild)
            state = bot.player.get_state(guild)
            k = state.khatma
            if k.active and not already_here:
                # notification channel: the announce channel, or the system channel
                ach = guild.get_channel(k.announce_channel_id) if k.announce_channel_id else None
                state.last_channel = ach or guild.system_channel or after.channel
                vc = await ensure_voice(guild, after.channel)
                if vc is None:
                    return
                if not k.suspended and not vc.is_playing() and not vc.is_paused():
                    await bot.player.play_next(guild)

    # auto leave when the channel empties
    vc = guild.voice_client
    if vc is None or not vc.is_connected() or not vc.channel:
        return
    humans = [m for m in vc.channel.members if not m.bot]
    if humans:
        bot.player.cancel_idle(guild)
    else:
        bot.player.schedule_idle_disconnect(guild)


# ================================================================ /help & error handling

@bot.tree.command(name="help", description="Show all bot commands")
async def help_cmd(interaction: discord.Interaction):
    e = discord.Embed(
        title="Quran Bot - Commands",
        color=GREEN,
        description=(
            "**Recitations**\n"
            "`/play` play a surah - `/pause` - `/resume` - `/skip` - `/stop`\n"
            "`/radio` live Quran radio station - `/queue` queue - `/volume` volume - `/leave` leave\n\n"
            "**Auto channel + continuous khatma** (admin)\n"
            "`/auto set` enable - `/auto pause` - `/auto resume`\n"
            "`/auto status` progress - `/auto off` disable\n\n"
            "**Reciters**\n"
            "`/reciters` list - `/setreciter` set the default\n\n"
            "**Quran**\n"
            "`/ayah` a random or specific ayah - `/search` search the Quran text\n\n"
            "**Prayer times & athkar**\n"
            "`/prayertimes` prayer times - `/athkar` morning/evening/bedtime athkar\n\n"
            "**Daily message** (admin)\n"
            "`/daily set` enable - `/daily off` disable - `/daily info` - `/daily preview`"
        ),
    )
    e.set_footer(text="\"We have made the Quran easy to remember\"")
    await interaction.response.send_message(embed=e)


@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    msg = "Something unexpected went wrong. Please try again in a moment."
    if isinstance(error, app_commands.MissingPermissions):
        msg = "You need the **Manage Server** permission for this command."
    elif isinstance(error, app_commands.CheckFailure):
        msg = "I can't run this command here."
    try:
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)
    except Exception:
        pass
    if not isinstance(error, (app_commands.MissingPermissions, app_commands.CheckFailure)):
        raise error


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("No token found! Create a .env file with: DISCORD_TOKEN=...")
    bot.run(TOKEN)
