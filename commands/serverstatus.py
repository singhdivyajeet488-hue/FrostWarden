import asyncio
import time
import re
import discord
from discord import app_commands
from discord.ext import commands, tasks

try:
    import aiohttp
    AIOHTTP_AVAILABLE = True
except ImportError:
    AIOHTTP_AVAILABLE = False

# ─────────────────────────────────────────────
#  Constants
# ─────────────────────────────────────────────
MC_STATUS_API    = "https://api.mcsrvstat.us/3/{host}"
EMBED_COLOR_ON   = 0x57F287
EMBED_COLOR_OFF  = 0xED4245
EMBED_COLOR_ERR  = 0x99AAB5
REQUEST_TIMEOUT  = 8
REFRESH_INTERVAL = 30  # seconds

MC_FORMAT_RE = re.compile(r"§[0-9a-fk-or]", re.IGNORECASE)


def strip_mc_formatting(text: str) -> str:
    return MC_FORMAT_RE.sub("", text).strip()


def parse_motd(motd_data) -> str:
    if motd_data is None:
        return "No MOTD"
    if isinstance(motd_data, str):
        return strip_mc_formatting(motd_data) or "No MOTD"
    lines = motd_data.get("clean") or motd_data.get("raw") or []
    joined = "\n".join(lines) if isinstance(lines, list) else str(lines)
    return strip_mc_formatting(joined) or "No MOTD"


async def fetch_server_data(ip: str):
    """Returns (data_dict, error_str). One of them will be None."""
    url = MC_STATUS_API.format(host=ip)
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
                data = await resp.json(content_type=None)
        return data, None
    except Exception as exc:
        return None, str(exc)


def build_online_embed(ip: str, data: dict) -> tuple:
    players_online = data.get("players", {}).get("online", 0)
    players_max    = data.get("players", {}).get("max", 0)
    version        = data.get("version", "Unknown")
    motd           = parse_motd(data.get("motd"))

    favicon_data_uri = data.get("icon")
    favicon_file = None
    if favicon_data_uri and favicon_data_uri.startswith("data:image/png;base64,"):
        try:
            import base64, io
            img_bytes = base64.b64decode(favicon_data_uri.split(",", 1)[1])
            favicon_file = discord.File(io.BytesIO(img_bytes), filename="favicon.png")
        except Exception:
            favicon_file = None

    embed = discord.Embed(title="🟢  Server Online", color=EMBED_COLOR_ON)
    embed.add_field(name="🖥  Server IP", value=f"`{ip}`",                           inline=False)
    embed.add_field(name="👥  Players",   value=f"`{players_online}/{players_max}`",  inline=True)
    embed.add_field(name="📡  Ping",      value="`N/A`",                              inline=True)
    embed.add_field(name="🎮  Version",   value=f"`{version}`",                       inline=True)
    embed.add_field(name="📝  MOTD",      value=f"```{motd}```",                      inline=False)
    embed.add_field(name="🟢  Status",    value="`Online`",                           inline=True)

    if favicon_file:
        embed.set_thumbnail(url="attachment://favicon.png")

    return embed, favicon_file


def build_offline_embed(ip: str) -> discord.Embed:
    return discord.Embed(
        title="🔴  Server Offline",
        description=(
            f"**`{ip}`** is currently **offline** or unreachable.\n"
            "Double-check the address or try again later."
        ),
        color=EMBED_COLOR_OFF
    )


# ─────────────────────────────────────────────
#  Refresh loop — one per active status message
# ─────────────────────────────────────────────
class StatusRefresher:
    """Runs a background loop that edits a channel message every 30 s."""

    def __init__(self, ip: str, channel: discord.TextChannel, message_id: int):
        self.ip = ip
        self.channel = channel
        self.message_id = message_id
        self._task = asyncio.create_task(self._loop())

    async def _loop(self):
        try:
            while True:
                await asyncio.sleep(REFRESH_INTERVAL)
                try:
                    msg = await self.channel.fetch_message(self.message_id)
                except (discord.NotFound, discord.Forbidden):
                    break  # message deleted — stop

                data, error = await fetch_server_data(self.ip)

                if error or data is None or not data.get("online", False):
                    embed = build_offline_embed(self.ip)
                    await msg.edit(embed=embed, attachments=[])
                else:
                    embed, favicon_file = build_online_embed(self.ip, data)
                    if favicon_file:
                        await msg.edit(embed=embed, attachments=[favicon_file])
                    else:
                        await msg.edit(embed=embed, attachments=[])

        except asyncio.CancelledError:
            pass
        except Exception:
            pass

    def cancel(self):
        self._task.cancel()


# ─────────────────────────────────────────────
#  Cog
# ─────────────────────────────────────────────
class ServerStatusCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._refreshers: list[StatusRefresher] = []

    @app_commands.command(
        name="serverstatus",
        description="Fetch the live status of a Minecraft Java Edition server."
    )
    @app_commands.describe(ip="Server IP address (e.g. play.hypixel.net)")
    async def serverstatus(self, interaction: discord.Interaction, ip: str) -> None:
        await interaction.response.defer()

        ip = ip.strip()
        if not ip:
            await interaction.followup.send(
                embed=discord.Embed(description="❌ Please provide a valid server IP.", color=EMBED_COLOR_ERR)
            )
            return

        data, error = await fetch_server_data(ip)

        if error:
            await interaction.followup.send(embed=discord.Embed(
                title="⚠️  Error fetching server status",
                description=f"**Server:** `{ip}`\n**Reason:** {error}",
                color=EMBED_COLOR_ERR
            ))
            return

        online: bool = data.get("online", False)

        if not online:
            embed = build_offline_embed(ip)
            webhook_msg = await interaction.followup.send(embed=embed, wait=True)
        else:
            embed, favicon_file = build_online_embed(ip, data)
            if favicon_file:
                webhook_msg = await interaction.followup.send(embed=embed, file=favicon_file, wait=True)
            else:
                webhook_msg = await interaction.followup.send(embed=embed, wait=True)

        # Fetch the real Message object so we can edit it later
        real_msg = await interaction.channel.fetch_message(webhook_msg.id)

        # Start background refresh
        refresher = StatusRefresher(
            ip=ip,
            channel=interaction.channel,
            message_id=real_msg.id
        )
        self._refreshers.append(refresher)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(ServerStatusCog(bot))
