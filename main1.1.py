import os
import re
import asyncio
import logging
from typing import Optional, Deque, List, Dict
from collections import deque

import discord
from discord.ext import commands
from dotenv import load_dotenv
import yt_dlp

# ---------- 기본 설정/로그 ----------
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logging.getLogger("discord").setLevel(logging.INFO)
log = logging.getLogger("yuki-bot")

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
if not TOKEN:
    raise SystemExit("DISCORD_TOKEN이 .env에 없습니다.")

intents = discord.Intents.default()
intents.message_content = True  # ! 명령용
intents.voice_states = True
LEAVE_GRACE = 1
choose_inflight: Dict[int, bool] = {}
guild_locks: Dict[int, asyncio.Lock] = {}

def get_lock(gid: int) -> asyncio.Lock:
    if gid not in guild_locks:
        guild_locks[gid] = asyncio.Lock()
    return guild_locks[gid]

leave_tasks: Dict[int, asyncio.Task] = {}

def _has_humans(vc: Optional[discord.VoiceClient]) -> bool:
    if not vc or not vc.channel:
        return False
    return any(not m.bot for m in vc.channel.members)

def cancel_leave(guild_id: int):
    t = leave_tasks.pop(guild_id, None)
    if t and not t.done():
        t.cancel()

def schedule_leave(guild_id: int, delay: int = LEAVE_GRACE):
    cancel_leave(guild_id)
    async def _wait():
        await asyncio.sleep(delay)
        g = bot.get_guild(guild_id)
        if not g:
            return
        vc = g.voice_client
        if vc and vc.is_connected() and not _has_humans(vc):
            # 큐/현재곡 비우고 퇴장
            if guild_id in players:
                players[guild_id].queue.clear()
                players[guild_id].current = None
            await _notify(guild_id, "아무도 남지 않았군요. 저도 이만 물러나겠습니다.")
            await vc.disconnect(force=True)
    leave_tasks[guild_id] = asyncio.create_task(_wait())
    
LAST_TEXT_CHANNEL: Dict[int, int] = {}

async def _notify(guild_id: int, msg: str):
    ch = None
    cid = LAST_TEXT_CHANNEL.get(guild_id)
    if cid:
        ch = bot.get_channel(cid)
    if ch is None:
        g = bot.get_guild(guild_id)
        if g and g.system_channel:
            ch = g.system_channel
        elif g:
            for c in g.text_channels:
                me = g.me or (g.get_member(bot.user.id) if bot.user else None)
                if me and c.permissions_for(me).send_messages:
                    ch = c
                    break
    if ch:
        try:
            await ch.send(msg)
        except:
            pass


bot = commands.Bot(command_prefix="!", intents=intents)
@bot.event
async def on_voice_state_update(member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
    g = member.guild
    if g is None:
        return
    vc = g.voice_client
    if vc is None or vc.channel is None:
        return
    if before.channel != vc.channel and after.channel != vc.channel:
        return
    if _has_humans(vc):
        cancel_leave(g.id)
    else:
        schedule_leave(g.id, delay=LEAVE_GRACE)


# ---------- yt-dlp / ffmpeg 옵션 ----------
YDL_OPTS = {
    "noplaylist": True,
    "quiet": True,
    "default_search": "ytsearch",
    "skip_download": True,
    "extract_flat": False,   # 재생용(풀 추출)
    "socket_timeout": 15,
}
# 검색은 메타만 빠르게(flat)
YDL_SEARCH_OPTS = {
    **YDL_OPTS,
    "extract_flat": True,
    "quiet": True,
}

FFMPEG_OPTIONS = {
    "before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
    "options": "-vn"
}

# ---------- 모델 ----------
class Track:
    def __init__(self, url: str, title: str, page: Optional[str] = None):
        self.url = url              # ffmpeg가 재생할 실제 오디오 스트림 URL
        self.title = title
        self.page = page or url     # 원본 페이지(유튜브 등)

class TrackLite:
    def __init__(self, title: str, page: str, duration: Optional[int], channel: Optional[str]):
        self.title = title
        self.page = page
        self.duration = duration
        self.channel = channel

class GuildPlayer:
    def __init__(self):
        self.queue: Deque[Track] = deque()
        self.current: Optional[Track] = None

players: Dict[int, GuildPlayer] = {}
pending_searches: Dict[int, List[TrackLite]] = {}  # 길드별 최근 검색 결과(빠른 flat)

def get_player(guild_id: int) -> GuildPlayer:
    if guild_id not in players:
        players[guild_id] = GuildPlayer()
    return players[guild_id]

def is_url(text: str) -> bool:
    return bool(re.match(r"^https?://", text))

# ---------- yt-dlp 추출 ----------
def ytdlp_extract_one(query_or_page: str) -> Track:
    # 공통 옵션(포맷 지정 없음)
    base = {
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "extract_flat": False,
        "default_search": "ytsearch",
        "geo_bypass": True,
        "ignore_no_formats_error": True,
        "retries": 1,
        "extractor_retries": 0,
        "socket_timeout": 10,
    }

    def _extract(client: str):
        opts = dict(base)
        opts["extractor_args"] = {"youtube": {"player_client": [client]}}
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(query_or_page, download=False)
        if info is None:
            raise RuntimeError("검색 결과가 없어.")
        if "entries" in info and info["entries"]:
            info = info["entries"][0]

        # 오디오 우선으로 URL 고르기
        stream_url = None
        fmts = info.get("formats") or []
        afmts = [f for f in fmts if f.get("url") and f.get("acodec") and f["acodec"] != "none"]
        if afmts:
            afmts.sort(key=lambda f: (f.get("abr") or f.get("tbr") or 0), reverse=True)
            stream_url = afmts[0]["url"]
        if not stream_url:
            pfmts = [f for f in fmts
                     if f.get("url") and f.get("acodec") not in (None, "none")
                     and f.get("vcodec") not in (None, "none")]
            if pfmts:
                pfmts.sort(key=lambda f: (f.get("tbr") or 0), reverse=True)
                stream_url = pfmts[0]["url"]
        if not stream_url:
            stream_url = info.get("url")
        if not stream_url:
            raise RuntimeError("오디오 스트림 URL을 못 찾았어.")

        title = info.get("title") or "제목 없음"
        page_url = info.get("webpage_url") or info.get("original_url") or query_or_page
        duration = int(info.get("duration") or 0)
        return Track(url=stream_url, title=title, page=page_url)  # duration 필드 쓰면 여기에 넣어도 됨

    try:
        # 1차: 웹 클라이언트(‘이 앱에선 재생 불가’ 에러를 피함)
        return _extract("web")
    except Exception as e:
        # 2차: 안드로이드로 1회만 재시도
        if "not available on this app" in str(e).lower():
            return _extract("android")
        raise


def ytdlp_search_flat(query: str, count: int = 5) -> List[TrackLite]:
    """검색어로 상위 N개를 'flat'(메타만)으로 빠르게 가져오기"""
    q = f"ytsearch{count}:{query}"
    with yt_dlp.YoutubeDL(YDL_SEARCH_OPTS) as ydl:
        info = ydl.extract_info(q, download=False)
    entries = (info or {}).get("entries") or []
    out: List[TrackLite] = []
    for it in entries:
        title = it.get("title") or "제목 없음"
        page = it.get("url") or it.get("webpage_url") or it.get("original_url")
        # flat에서는 url이 video_id일 수 있음 → 유튜브 링크로 보정
        if page and not str(page).startswith("http"):
            page = f"https://www.youtube.com/watch?v={page}"
        dur = it.get("duration")
        ch = it.get("channel")
        if page:
            out.append(TrackLite(title=title, page=page, duration=dur, channel=ch))
    if not out:
        raise RuntimeError("검색 결과가 없어.")
    return out

# ---------- 재생 루프 ----------
async def play_next(ctx: commands.Context):
    player = get_player(ctx.guild.id)
    if not player.queue:
        player.current = None
        vc = ctx.voice_client
        if vc and not _has_humans(vc):
            schedule_leave(ctx.guild.id, delay=15)
        return


    track = player.queue.popleft()
    player.current = track
    vc: discord.VoiceClient = ctx.voice_client

    if vc is None:
        # 안전장치: 끊겼다면 다시 입장 시도
        if ctx.author.voice and ctx.author.voice.channel:
            await ctx.author.voice.channel.connect(reconnect=True)
            vc = ctx.voice_client
        else:
            await ctx.reply("음성 채널에 먼저 들어가줘!")
            return

    def after_play(err):
        if err:
            log.error("FFmpeg/Voice error: %s", err)
        fut = asyncio.run_coroutine_threadsafe(track_end(ctx), bot.loop)
        try:
            fut.result()
        except Exception as e:
            log.exception("after callback error: %s", e)

    log.info("Now playing: %s", track.title)
    source = discord.FFmpegPCMAudio(track.url, **FFMPEG_OPTIONS)
    cancel_leave(ctx.guild.id)
    vc.play(source, after=after_play)

async def track_end(ctx: commands.Context):
    await play_next(ctx)

# ---------- 이벤트 ----------
@bot.event
async def on_ready():
    # 슬래시 커맨드 전부 초기화
    bot.tree.clear_commands(guild=None)
    await bot.tree.sync(guild=None)

    log.info("Logged in as %s", bot.user)

# ---------- 유틸 ----------
def fmt_duration(sec: Optional[int]) -> str:
    if not sec and sec != 0:
        return ""
    m, s = divmod(int(sec), 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"

# ---------- 명령어 (! 전용) ----------
# 1) 재생: 링크면 즉시, 검색어면 후보(빠른 flat), 첨부면 첨부 재생
@bot.command(name="재생", aliases=["play", "틀어", "p"])
async def cmd_play(ctx: commands.Context, *, query: Optional[str] = None):
    LAST_TEXT_CHANNEL[ctx.guild.id] = ctx.channel.id
    
    # 자동 입장
    if ctx.author.voice is None or ctx.author.voice.channel is None:
        return await ctx.reply("먼저 음성 채널에 들어가주시죠.")
    if ctx.voice_client is None:
        await ctx.author.voice.channel.connect(reconnect=True)

    # (A) 같은 메시지에 첨부 파일이 있으면 그걸 재생
    if (not query) and ctx.message and ctx.message.attachments:
        a = ctx.message.attachments[0]
        t = Track(url=a.url, title=a.filename, page=a.url)

    # (B) URL → 즉시 풀 추출
    elif query and is_url(query):
        try:
            t = await bot.loop.run_in_executor(None, ytdlp_extract_one, query)
        except Exception as e:
            return await ctx.reply(f"추출 실패: {e}")

    # (C) 검색어 → 빠른 flat 후보 표시
    elif query:
        try:
            results = await bot.loop.run_in_executor(None, ytdlp_search_flat, query, 5)
        except Exception as e:
            return await ctx.reply(f"검색 실패: {e}")

        pending_searches[ctx.guild.id] = results
        lines = ["검색 결과 (고를 번호를 `!선택 <번호>`로 입력해주세요):"]
        for i, tr in enumerate(results, start=1):
            title = tr.title if len(tr.title) <= 70 else tr.title[:67] + "..."
            extra_parts = []
            if tr.channel:
                extra_parts.append(tr.channel)
            d = fmt_duration(tr.duration)
            if d:
                extra_parts.append(d)
            extra = " — " + " • ".join(extra_parts) if extra_parts else ""
            lines.append(f"{i}. {title}{extra}")
        return await ctx.reply("\n".join(lines))

    else:
        return await ctx.reply("재생할 곡을 알려주시죠. 유튜브 링크나 검색어를 넣어주시면… 제가 틀어드리겠습니다")

    # 큐/재생 진행
    async with get_lock(ctx.guild.id):
        player = get_player(ctx.guild.id)
        player.queue.append(t)
        cancel_leave(ctx.guild.id) 
        await ctx.reply(f"대기열 추가: **{t.title}**")

        vc = ctx.voice_client
        if not vc.is_playing() and player.current is None:
            await play_next(ctx)

# 2) 선택: 직전 flat 검색 결과에서 번호로 선택 → 그 하나만 풀 추출
@bot.command(name="선택", aliases=["choose", "pick"])
async def cmd_choose(ctx: commands.Context, index: Optional[int] = None):
    LAST_TEXT_CHANNEL[ctx.guild.id] = ctx.channel.id
    
    if choose_inflight.get(ctx.guild.id):
        return await ctx.reply("잠시만요… 방금 선택을 처리 중입니다.")
    choose_inflight[ctx.guild.id] = True

    try:
        
        if index is None:
            return await ctx.reply("사용법: `!선택 <번호>` (예: `!선택 2`)")

        results = pending_searches.get(ctx.guild.id)
        if not results:
            return await ctx.reply("남아 있는 흔적은 없군요… 듣고 싶은 곡이 있다면, !재생 <검색어>로 불러내 보시죠.")

        if not (1 <= index <= len(results)):
            return await ctx.reply(f"1 ~ {len(results)}번 사이에서 선택해 주시죠.")

        # 자동 입장
        if ctx.author.voice is None or ctx.author.voice.channel is None:
            return await ctx.reply("먼저 음성 채널에 들어가주시죠.")
        if ctx.voice_client is None:
            await ctx.author.voice.channel.connect(reconnect=True)

        lite = results[index - 1]
        try:
            t = await bot.loop.run_in_executor(None, ytdlp_extract_one, lite.page)
        except Exception as e:
            return await ctx.reply(f"추출 실패: {e}")

        player = get_player(ctx.guild.id)
        player.queue.append(t)
        cancel_leave(ctx.guild.id) 
        await ctx.reply(f"대기열 추가: **{t.title}**")
        pending_searches.pop(ctx.guild.id, None) 
        vc = ctx.voice_client
        if not vc.is_playing() and player.current is None:
            await play_next(ctx)
    finally:
        choose_inflight.pop(ctx.guild.id, None)

# 3) 일시정지/다시재생/스킵/정지
@bot.command(name="일시정지", aliases=["pause"])
async def cmd_pause(ctx: commands.Context):
    vc = ctx.voice_client
    if vc and vc.is_playing():
        vc.pause()
        await ctx.reply("⏸️ 일시정지")
    else:
        await ctx.reply("지금 재생 중이 아닙니다.")

@bot.command(name="다시재생", aliases=["resume"])
async def cmd_resume(ctx: commands.Context):
    vc = ctx.voice_client
    if vc and vc.is_paused():
        vc.resume()
        await ctx.reply("▶️ 다시 재생")
    else:
        await ctx.reply("일시정지 상태가 아닙니다.")

@bot.command(name="넘겨", aliases=["스킵", "skip"])
async def cmd_skip(ctx: commands.Context):
    vc = ctx.voice_client
    if vc and (vc.is_playing() or vc.is_paused()):
        vc.stop()
        await ctx.reply("⏭️ 스킵")
    else:
        await ctx.reply("스킵할 곡이 없습니다.")

@bot.command(name="정지", aliases=["stop"])
async def cmd_stop(ctx: commands.Context):
    player = get_player(ctx.guild.id)
    player.queue.clear()
    vc = ctx.voice_client
    if vc and (vc.is_playing() or vc.is_paused()):
        vc.stop()
    await ctx.reply("⏹️ 정지! 대기열 비웠습니다.")
    vc = ctx.voice_client
    if vc and not _has_humans(vc):
        schedule_leave(ctx.guild.id, delay=5)


# 4) 지금/대기열
@bot.command(name="지금", aliases=["now"])
async def cmd_now(ctx: commands.Context):
    player = get_player(ctx.guild.id)
    if player.current:
        await ctx.reply(f"지금: **{player.current.title}**\n{player.current.page}")
    else:
        await ctx.reply("지금 재생 중인 곡이 없습니다.")

@bot.command(name="대기열", aliases=["queue"])
async def cmd_queue(ctx: commands.Context):
    player = get_player(ctx.guild.id)
    if not player.queue:
        return await ctx.reply("대기열이 비었습니다.")
    lines = []
    for i, tr in enumerate(player.queue, start=1):
        title = tr.title if len(tr.title) <= 70 else tr.title[:67] + "..."
        lines.append(f"{i}. {title}")
    await ctx.reply("대기열:\n" + "\n".join(lines))

# ---------- 엔트리포인트 ----------
async def main():
    async with bot:
        await bot.start(TOKEN)

if __name__ == "__main__":
    asyncio.run(main())
