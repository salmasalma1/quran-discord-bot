# -*- coding: utf-8 -*-
"""
Audio player: per-guild queue + continuous khatma (full-Quran playlist)
+ playback controls. Uses FFmpeg to stream mp3 files directly from
mp3quran.net.
"""

import asyncio
import os
import random
from dataclasses import dataclass, field

import discord

from quran_api import RECITER_BY_ID, SURAH_BY_NUMBER, audio_url

FFMPEG_OPTS = {
    "before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
    "options": "-vn",
}


def ffmpeg_executable() -> str:
    """Pick the FFmpeg binary: FFMPEG_PATH env var first, then a local
    ./ffmpeg file next to the bot (useful on hosts without ffmpeg
    preinstalled), then whatever is on PATH."""
    env_path = os.getenv("FFMPEG_PATH")
    if env_path:
        return env_path
    local = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ffmpeg")
    if os.path.isfile(local) and os.access(local, os.X_OK):
        return local
    return "ffmpeg"

KHATMA_REQUESTER = "Auto Khatma"


@dataclass
class Track:
    url: str
    title: str          # e.g. "Al-Baqara — Mishary Alafasy"
    surah_name: str
    reciter: str
    requester: str
    volume: float = 0.8


@dataclass
class KhatmaState:
    active: bool = False
    suspended: bool = False          # paused via /auto pause or /stop
    reciter_id: str = ""
    mode: str = "random"             # "random" or "sequential"
    order: list = field(default_factory=list)   # surah numbers (1-114)
    index: int = 0                   # current position in the khatma
    completed: int = 0               # number of finished khatmas
    announce_channel_id: int | None = None      # text channel for announcements


@dataclass
class GuildState:
    queue: list[Track] = field(default_factory=list)
    volume: float = 0.8
    last_channel: discord.abc.Messageable | None = None   # notification channel
    idle_task: asyncio.Task | None = None                 # auto-leave task
    khatma: KhatmaState = field(default_factory=KhatmaState)


class Player:
    """Manages queues and khatma state for all guilds."""

    def __init__(self, bot: discord.Client):
        self.bot = bot
        self.states: dict[int, GuildState] = {}
        self.persist = None   # save callback — wired up in main.py

    def get_state(self, guild: discord.Guild) -> GuildState:
        if guild.id not in self.states:
            self.states[guild.id] = GuildState()
        return self.states[guild.id]

    # -------------------------------------------------------------- khatma

    def make_order(self, mode: str, reciter_id: str) -> list[int]:
        """Khatma surah order: sequential (Al-Fatiha -> An-Nas) or shuffled."""
        r = RECITER_BY_ID.get(reciter_id)
        avail = [n for n in range(1, 115)
                 if r and n in r["surahs"] and audio_url(reciter_id, n)]
        if mode == "random":
            random.shuffle(avail)
        return avail

    def setup_khatma(self, guild: discord.Guild, reciter_id: str = "",
                     mode: str = "random", announce_channel_id: int | None = None,
                     saved: dict | None = None):
        """Start a new khatma, or restore a saved one after a restart."""
        state = self.get_state(guild)
        k = state.khatma
        k.suspended = False
        if saved:
            k.active = True
            k.reciter_id = saved["reciter_id"]
            k.mode = saved.get("mode", "random")
            k.order = list(saved.get("order") or self.make_order(k.mode, k.reciter_id))
            k.index = int(saved.get("index", 0)) % max(len(k.order), 1)
            k.completed = int(saved.get("completed", 0))
            k.announce_channel_id = saved.get("announce_channel_id")
        else:
            k.active = True
            k.reciter_id = reciter_id
            k.mode = mode
            k.announce_channel_id = announce_channel_id
            k.index = 0
            k.completed = 0
            k.order = self.make_order(mode, reciter_id)
        if self.persist:
            self.persist(guild)

    def build_khatma_track(self, guild: discord.Guild) -> Track | None:
        """Get the next surah in the khatma, advancing the counter
        (and celebrating when a full khatma is completed)."""
        state = self.get_state(guild)
        k = state.khatma
        vc = guild.voice_client
        if not k.active or k.suspended:
            return None
        if vc is None or not vc.is_connected():
            return None
        if not k.order:
            k.order = self.make_order(k.mode, k.reciter_id)
        # finished the whole Quran? celebrate and start a new khatma
        if k.index >= len(k.order):
            k.completed += 1
            k.index = 0
            if k.mode == "random":
                k.order = self.make_order("random", k.reciter_id)
            self._announce_khatma_done(guild, state, k)
        surah_n = k.order[k.index]
        k.index += 1
        if self.persist:
            self.persist(guild)

        reciter = RECITER_BY_ID.get(k.reciter_id)
        s = SURAH_BY_NUMBER.get(surah_n)
        url = audio_url(k.reciter_id, surah_n)
        if reciter is None or s is None or url is None:
            return None
        return Track(
            url=url,
            title=f"{s['name_en']} ({s['name_ar']}) — {reciter['reciter']}",
            surah_name=s["name_en"],
            reciter=f"{reciter['reciter']} ({reciter['moshaf']})",
            requester=KHATMA_REQUESTER,
        )

    def _announce_khatma_done(self, guild: discord.Guild, state: GuildState, k: KhatmaState):
        ch = guild.get_channel(k.announce_channel_id) if k.announce_channel_id else None
        ch = ch or state.last_channel
        if not ch:
            return
        e = discord.Embed(
            title="🎉 Congratulations!",
            description=(
                f"The server just completed a **full khatma of the Quran** "
                f"(khatma **#{k.completed}**).\n"
                f"Starting a new one right now, in sha' Allah."
            ),
            color=0xC9A227,
        )
        e.set_footer(text="May Allah accept it from you")

        async def _send():
            try:
                await ch.send(embed=e)
            except discord.HTTPException:
                pass
        asyncio.create_task(_send())

    async def khatma_pause(self, guild: discord.Guild):
        state = self.get_state(guild)
        if state.khatma.active:
            state.khatma.suspended = True
            if self.persist:
                self.persist(guild)
        vc = guild.voice_client
        if vc and (vc.is_playing() or vc.is_paused()):
            vc.stop()

    async def khatma_resume(self, guild: discord.Guild):
        state = self.get_state(guild)
        state.khatma.suspended = False
        if self.persist:
            self.persist(guild)
        vc = guild.voice_client
        if vc and vc.is_connected() and not vc.is_playing() and not vc.is_paused():
            await self.play_next(guild)

    def khatma_status(self, guild: discord.Guild) -> dict:
        state = self.get_state(guild)
        k = state.khatma
        upcoming = (SURAH_BY_NUMBER.get(k.order[k.index])
                    if k.order and k.index < len(k.order) else None)
        return {
            "active": k.active,
            "suspended": k.suspended,
            "done": k.index,
            "total": len(k.order),
            "completed": k.completed,
            "next_surah": upcoming["name_en"] if upcoming else "-",
            "reciter_id": k.reciter_id,
            "mode": k.mode,
        }

    # -------------------------------------------------------------- playback

    async def enqueue(self, interaction: discord.Interaction, track: Track) -> int:
        """Add a track to the queue and start playback if idle.
        Returns its position in the queue (0 = started playing now)."""
        guild = interaction.guild
        state = self.get_state(guild)
        state.last_channel = interaction.channel

        vc: discord.VoiceClient = guild.voice_client
        if vc is None or not vc.is_connected():
            channel = interaction.user.voice.channel
            vc = await channel.connect(self_deaf=True)

        position = len(state.queue)
        state.queue.append(track)

        if not vc.is_playing() and not vc.is_paused():
            await self.play_next(guild)
            return 0
        return position

    async def play_next(self, guild: discord.Guild):
        state = self.get_state(guild)
        vc: discord.VoiceClient = guild.voice_client
        if vc is None or not vc.is_connected():
            state.queue.clear()
            return

        if state.queue:
            track = state.queue.pop(0)
        else:
            track = self.build_khatma_track(guild)
            if track is None:
                return

        def after(error):
            if error:
                print(f"[player] playback error: {error}")
            asyncio.run_coroutine_threadsafe(self.play_next(guild), self.bot.loop)

        try:
            source = discord.PCMVolumeTransformer(
                discord.FFmpegPCMAudio(track.url, executable=ffmpeg_executable(), **FFMPEG_OPTS),
                volume=state.volume,
            )
        except (FileNotFoundError, discord.ClientException):
            # FFmpeg is not installed on the host machine
            ch = state.last_channel
            if ch:
                try:
                    await ch.send(
                        "FFmpeg is not installed on the machine running this bot!\n"
                        "Install it with `sudo apt install ffmpeg` (Linux) or "
                        "`winget install ffmpeg` (Windows), then restart the bot."
                    )
                except discord.HTTPException:
                    pass
            print("[player] FFmpeg not found!")
            return

        vc.play(source, after=after)

        embed = discord.Embed(
            title="Now playing",
            description=f"## {track.title}",
            color=0x1F8A5B,
        )
        embed.add_field(name="Reciter", value=track.reciter, inline=True)
        if track.requester == KHATMA_REQUESTER:
            k = state.khatma
            embed.add_field(
                name="Khatma",
                value=f"{k.index}/{len(k.order)} surahs - {k.completed} completed",
                inline=True,
            )
            embed.set_footer(text="Continuous khatma")
        else:
            embed.add_field(name="Queue", value=f"{len(state.queue)} track(s)", inline=True)
            embed.set_footer(text=f"Requested by {track.requester}")
        ch = state.last_channel
        if ch:
            try:
                await ch.send(embed=embed)
            except discord.HTTPException:
                pass

    # -------------------------------------------------------------- controls

    def skip(self, guild: discord.Guild) -> bool:
        vc = guild.voice_client
        if vc and (vc.is_playing() or vc.is_paused()):
            vc.stop()  # after() continues the queue / khatma
            return True
        return False

    async def stop_and_clear(self, guild: discord.Guild):
        """Full stop: clear the queue and suspend the khatma."""
        state = self.get_state(guild)
        state.queue.clear()
        if state.khatma.active and not state.khatma.suspended:
            state.khatma.suspended = True
            if self.persist:
                self.persist(guild)
        vc = guild.voice_client
        if vc and (vc.is_playing() or vc.is_paused()):
            vc.stop()

    async def disconnect(self, guild: discord.Guild):
        """Leave the voice channel — the khatma stays saved and resumes
        automatically when someone joins the auto channel again."""
        state = self.get_state(guild)
        state.queue.clear()
        if state.idle_task and not state.idle_task.done():
            state.idle_task.cancel()
        vc = guild.voice_client
        if vc and vc.is_connected():
            await vc.disconnect(force=True)

    # -------------------------------------------------------------- idle auto-leave

    def schedule_idle_disconnect(self, guild: discord.Guild, delay: int = 300):
        """If the bot is alone in the channel, wait a bit then leave
        (the khatma stays saved)."""
        state = self.get_state(guild)

        async def _idle():
            try:
                await asyncio.sleep(delay)
                vc = guild.voice_client
                if vc and vc.is_connected():
                    humans = [m for m in vc.channel.members if not m.bot]
                    if not humans:
                        state.queue.clear()
                        await vc.disconnect(force=True)
                        if state.last_channel:
                            try:
                                await state.last_channel.send(
                                    "The channel is empty, so I left. "
                                    "I'll be back as soon as someone joins.")
                            except discord.HTTPException:
                                pass
            except asyncio.CancelledError:
                pass

        if state.idle_task and not state.idle_task.done():
            state.idle_task.cancel()
        state.idle_task = asyncio.create_task(_idle())

    def cancel_idle(self, guild: discord.Guild):
        state = self.get_state(guild)
        if state.idle_task and not state.idle_task.done():
            state.idle_task.cancel()