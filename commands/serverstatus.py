import asyncio
import time
import re
import discord
from discord import app_commands
from discord.ext import commands

try:
    import aiohttp
    AIOHTTP_AVAILABLE = True
except ImportError:
    AIOHTTP_AVAILABLE = False

# ─────────────────────────────────────────────
#  Constants
# ─────────────────────────────────────────────
MC_STATUS_API   = "https://api.mcsrvstat.us/3/{host}"  # mcsrvstat.us v3 — reliable, supports SRV
EMBED_COLOR_ON  = 0x57F287   # Discord green
EMBED_COLOR_OFF = 0xED4245   # Discord red
EMBED_COLOR_ERR = 0x99AAB5   # Grey for errors
REQUEST_TIMEOUT = 8           # seconds
REFRESH_INTERVAL = 30         # seconds between auto-refreshes

# Regex strips Minecraft § colour / formatting codes from MOTDs
MC_FORMAT_RE = re.compile(r"§[0-9a-fk-or]", re.IGNORECASE)


def strip_mc_formatting(text: str) -> str:
    """Remove Minecraft legacy colour codes (§X) from a string."""
    return MC_FORMAT_RE.sub("", text).strip()


def parse_motd(motd_data: dict | str | None) -> str:
    """
    mcsrvstat v3 returns motd as:
      { "raw": ["line1", "line2"], "clean": ["line1", "line2"], "html": [...] }
    We prefer 'clean' (already stripped), fall back to 'raw'.
    """
    if motd_data is None:
        return "No MOTD"

    if isinstance(motd_data, str):
        return strip_mc_formatting(motd_data) or "No MOTD"

    # Prefer clean lines (no colour codes)
    lines = motd_data.get("clean") or motd_data.get("raw") or []
    if isinstance(lines, list):
        joined = "\n".join(lines)
    else:
        joined = str(lines)

    return strip_mc_formatting(joined) or "No MOTD"


async def fetch_server_data(ip: str) -> tuple[dict | None, int, str | None]:
    """
    Query the mcsrvstat API for the given IP.
    Returns (data, latency_ms, error_message).
    data is None and error_message is set on failure.
    """
    url = MC_STATUS_API.format(host=ip)
    t_start = time.monotonic()

    try:
        if not AIOHTTP_AVAILABLE:
            raise RuntimeError("`aiohttp` is not installed.")

        async with aiohttp.ClientSession() as session:
            async with session.get(
                url,
                timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT),
                headers={"User-Agent": "FrostWarden-Discord-Bot/1.0"}
            ) as resp:
                if resp.status != 200:
                    raise ValueError(f"API returned HTTP {resp.status}")
                data: dict = await resp.json(content_type=None)

        latency_ms = round((time.monotonic() - t_start) * 1000)
        return data, latency_ms, None

    except Exception as exc:
        latency_ms = round((time.monotonic() - t_start) * 1000)
        return None, latency_ms, str(exc)


def build_online_embed(ip: str, data: dict, api_latency_ms: int) -> tuple[discord.Embed, discord.File | None]:
    """Build the online server embed. Returns (embed, favicon_file_or_None)."""
    players_online = data.get("players", {}).get("online", 0)
    players_max    = data.get("players", {}).get("max", 0)
    version        = data.get("version", "Unknown")
    motd           = parse_motd(data.get("motd"))
    ping_ms        = api_latency_ms
    tps            = "Unavailable"

    favicon_data_uri: str | None = data.get("icon")
    favicon_file: discord.File | None = None

    if favicon_data_uri and favicon_data_uri.startswith("data:image/png;base64,"):
        try:
            import base64, io
            raw_b64 = favicon_data_uri.split(",", 1)[1]
            img_bytes = base64.b64decode(raw_b64)
            favicon_file = discord.File(io.BytesIO(img_bytes), filename="favicon.png")
        except Exception:
            favicon_file = None

    embed = discord.Embed(title="🟢  Server Online", color=EMBED_COLOR_ON)
    embed.add_field(name="🖥  Server IP",   value=f"`{ip}`",                          inline=False)
    embed.add_field(name="👥  Players",      value=f"`{players_online}/{players_max}`", inline=True)
    embed.add_field(name="⚡  TPS",          value=f"`{tps}`",                          inline=True)
    embed.add_field(name="📡  Ping",         value=f"`{ping_ms} ms`",                   inline=True)
    embed.add_field(name="🎮  Version",      value=f"`{version}`",                      inline=True)
    embed.add_field(name="📝  MOTD",         value=f"```{motd}```",                     inline=False)
    embed.add_field(name="🟢  Status",       value="`Online`",                          inline=True)
    embed.add_field(name="\u200b",           value="\u200b",                            inline=False)
    embed.add_field(
        name="\u2501" * 22,
        value=f"API response time: **{api_latency_ms} ms**",
        inline=False
    )
    # No footer — "Powered by" line removed

    if favicon_file:
        embed.set_thumbnail(url="attachment://favicon.png")

    return embed, favicon_file


def build_offline_embed(ip: str, api_latency_ms: int) -> discord.Embed:
    embed = discord.Embed(
        title="🔴  Server Offline",
        description=(
            f"**`{ip}`** is currently **offline** or unreachable.\n"
            "Double-check the address or try again later."
        ),
        color=EMBED_COLOR_OFF
    )
    embed.add_field(
        name="\u2501" * 22,
        value=f"API response time: **{api_latency_ms} ms**",
        inline=False
    )
    # No footer — "Powered by" line removed
    return embed


# ─────────────────────────────────────────────
#  Auto-refresh View
# ─────────────────────────────────────────────
class ServerStatusView(discord.ui.View):
    """
    Attaches to the status message and refreshes it every REFRESH_INTERVAL seconds.
    Stops automatically after the message is deleted or the bot restarts.
    """

    def __init__(self, ip: str, message: discord.Message):
        super().__init__(timeout=None)  # We manage our own lifecycle
        self.ip = ip
        self.message = message
        self._task = asyncio.create_task(self._refresh_loop())

    async def _refresh_loop(self):
        """Background task: re-fetch and edit the message every 30 s."""
        try:
            while True:
                await asyncio.sleep(REFRESH_INTERVAL)
                await self._do_refresh()
        except asyncio.CancelledError:
            pass
        except Exception:
            pass  # Don't crash the bot if something goes wrong

    async def _do_refresh(self):
        data, latency_ms, error = await fetch_server_data(self.ip)

        if error or data is None or not data.get("online", False):
            embed = build_offline_embed(self.ip, latency_ms)
            try:
                await self.message.edit(embed=embed, attachments=[], view=self)
            except (discord.NotFound, discord.Forbidden):
                self._task.cancel()  # Message deleted — stop refreshing
            return

        embed, favicon_file = build_online_embed(self.ip, data, latency_ms)

        try:
            if favicon_file:
                await self.message.edit(embed=embed, attachments=[favicon_file], view=self)
            else:
                await self.message.edit(embed=embed, attachments=[], view=self)
        except (discord.NotFound, discord.Forbidden):
            self._task.cancel()  # Message deleted — stop refreshing

    def stop(self):
        self._task.cancel()
        super().stop()


# ─────────────────────────────────────────────
#  Cog
# ─────────────────────────────────────────────
class ServerStatusCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(
        name="serverstatus",
        description="Fetch the live status of a Minecraft Java Edition server."
    )
    @app_commands.describe(ip="Server IP address (e.g. play.hypixel.net or play.example.com:25565)")
    async def serverstatus(self, interaction: discord.Interaction, ip: str) -> None:
        """
        /serverstatus ip:<server_ip>
        Returns a rich embed with player count, version, MOTD, ping, TPS,
        and server icon — refreshed automatically every 30 seconds.
        """
        await interaction.response.defer()

        ip = ip.strip()
        if not ip:
            await interaction.followup.send(
                embed=discord.Embed(
                    description="❌ Please provide a valid server IP.",
                    color=EMBED_COLOR_ERR
                )
            )
            return

        data, latency_ms, error = await fetch_server_data(ip)

        if error:
            await interaction.followup.send(embed=self._error_embed(ip, error))
            return

        online: bool = data.get("online", False)

        if not online:
            embed = build_offline_embed(ip, latency_ms)
            msg = await interaction.followup.send(embed=embed, wait=True)
        else:
            embed, favicon_file = build_online_embed(ip, data, latency_ms)
            if favicon_file:
                msg = await interaction.followup.send(embed=embed, file=favicon_file, wait=True)
            else:
                msg = await interaction.followup.send(embed=embed, wait=True)

        # Attach the auto-refresh view (starts background task)
        view = ServerStatusView(ip=ip, message=msg)
        await msg.edit(view=view)

    # ── Helper ──────────────────────────────────────────────────────────────
    @staticmethod
    def _error_embed(ip: str, reason: str) -> discord.Embed:
        """Return a generic error embed."""
        embed = discord.Embed(
            title="⚠️  Error fetching server status",
            description=f"**Server:** `{ip}`\n**Reason:** {reason}",
            color=EMBED_COLOR_ERR
        )
        return embed


# ─────────────────────────────────────────────
#  Extension entry-point
# ─────────────────────────────────────────────
async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(ServerStatusCog(bot))
