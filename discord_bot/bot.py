import os
import certifi
os.environ['SSL_CERT_FILE'] = certifi.where()
os.environ['REQUESTS_CA_BUNDLE'] = certifi.where()

import asyncio
import aiohttp
import discord
import ssl
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv
import json
from pathlib import Path

# Ultimate SSL Fix for restricted environments
try:
    ssl._create_default_https_context = ssl._create_unverified_context
except Exception:
    pass

load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
API_URL       = os.getenv("INFRA_API_URL", "http://localhost:8000")
API_KEY       = os.getenv("INFRA_API_KEY")
ALERT_POLL_SECONDS = float(os.getenv("HEIMDALL_ALERT_POLL_SECONDS", "10"))

if not DISCORD_TOKEN:
    raise RuntimeError("DISCORD_TOKEN is not set.")

if not API_KEY:
    raise RuntimeError("INFRA_API_KEY is not set.")

HEADERS       = {"X-API-Key": API_KEY, "Content-Type": "application/json"}

# ── Bot setup ─────────────────────────────────────────────────────────────────

intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree

# Avoid hanging interactions indefinitely when the API is down or DNS is flaky.
# aiohttp's default timeout is quite long, which can make the bot *look* frozen.
HTTP_TIMEOUT = aiohttp.ClientTimeout(total=20)
_http: aiohttp.ClientSession | None = None
_alert_task: asyncio.Task | None = None
_last_service_status: dict[str, str] = {}
_last_node_status: dict[str, str] = {}
_alert_channel_id: int | None = None

_ALERT_CFG_PATH = Path(__file__).with_name("alert_config.json")

def _load_alert_channel_id() -> int | None:
    # Prefer config file; fall back to env for backwards compatibility.
    try:
        if _ALERT_CFG_PATH.exists():
            data = json.loads(_ALERT_CFG_PATH.read_text(encoding="utf-8"))
            cid = data.get("channel_id")
            if isinstance(cid, int):
                return cid
            if isinstance(cid, str) and cid.isdigit():
                return int(cid)
    except Exception:
        pass
    env = os.getenv("HEIMDALL_ALERT_CHANNEL_ID")
    if env and env.isdigit():
        return int(env)
    return None

def _save_alert_channel_id(channel_id: int | None) -> None:
    try:
        if channel_id is None:
            if _ALERT_CFG_PATH.exists():
                _ALERT_CFG_PATH.unlink()
            return
        _ALERT_CFG_PATH.write_text(json.dumps({"channel_id": int(channel_id)}, indent=2) + "\n", encoding="utf-8")
    except Exception as e:
        # Don't crash the bot if the FS is read-only, etc.
        print(f"Failed to write alert config: {e}")

# ── Helpers (Basic) ───────────────────────────────────────────────────────────

def status_emoji(status: str) -> str:
    return {"pending": "⏳", "running": "🔄", "success": "✅", "failed": "❌", "booting": "🟡", "healthy": "🟢", "dead": "🔴"}.get(status, "❓")

def node_emoji(status: str) -> str:
    return {"ONLINE": "🟢", "OFFLINE": "🔴"}.get(status, "⚪")

async def api_post(path: str, payload: dict) -> dict:
    assert _http is not None
    async with _http.post(f"{API_URL}{path}", json=payload, headers=HEADERS) as r:
        r.raise_for_status()
        return await r.json()

async def api_get(path: str) -> dict:
    assert _http is not None
    async with _http.get(f"{API_URL}{path}", headers=HEADERS) as r:
        r.raise_for_status()
        return await r.json()

async def _get_alert_channel() -> discord.abc.Messageable | None:
    if _alert_channel_id is None:
        return None
    ch = bot.get_channel(_alert_channel_id)
    if ch is not None:
        return ch
    try:
        return await bot.fetch_channel(_alert_channel_id)
    except Exception:
        return None

async def alert_monitor_loop():
    """
    Poll the control plane and announce when a node goes OFFLINE or a service becomes dead.
    This is intentionally polling-based so it works even if the control plane doesn't push events.
    """
    await bot.wait_until_ready()
    channel = await _get_alert_channel()
    if channel is None:
        return

    while not bot.is_closed():
        try:
            # Nodes
            nodes = await api_get("/nodes")
            for n in nodes:
                name = n.get("name", "?")
                status = n.get("status", "UNKNOWN")
                prev = _last_node_status.get(name)
                _last_node_status[name] = status
                if prev is not None and prev != status and status == "OFFLINE":
                    await channel.send(f"🔴 Node OFFLINE: `{name}`")

            # Services
            services = await api_get("/services")
            for s in services:
                name = s.get("name", "?")
                status = s.get("status", "unknown")
                node_name = s.get("node_name", "—")
                prev = _last_service_status.get(name)
                _last_service_status[name] = status
                if prev is not None and prev != status and status == "dead":
                    await channel.send(f"🔴 Service DEAD: `{name}` on node `{node_name}`")
        except Exception as e:
            # Avoid killing the bot for transient API/network failures.
            print(f"alert_monitor_loop error: {e}")
        await asyncio.sleep(ALERT_POLL_SECONDS)

async def _restart_alert_task():
    global _alert_task
    if _alert_task is not None:
        _alert_task.cancel()
        try:
            await _alert_task
        except asyncio.CancelledError:
            pass
        _alert_task = None

    # Reset state on (re)start to prevent spam from old baselines.
    _last_service_status.clear()
    _last_node_status.clear()

    if _alert_channel_id is not None:
        _alert_task = asyncio.create_task(alert_monitor_loop())

async def send_error_embed(interaction: discord.Interaction, error: str):
    embed = discord.Embed(title="❌ Heimdall API — Error", description=f"```{error}```", color=discord.Color.red())
    if "Connect call failed" in error or "Cannot connect to host" in error:
        embed.add_field(name="💡 Troubleshooting", value="The Control Plane seems offline. Ensure `bash start.sh` is running on port 8000.", inline=False)
    await interaction.followup.send(embed=embed)

async def safe_defer(interaction: discord.Interaction):
    if interaction.response.is_done():
        return
    try:
        await interaction.response.defer(thinking=True)
    except discord.NotFound:
        # Interaction expired; avoid crashing the command handler.
        return

def op_embed(op: dict) -> discord.Embed:
    s = op.get("status", "unknown")
    color = {"success": discord.Color.green(), "failed": discord.Color.red(), "running": discord.Color.yellow(), "pending": discord.Color.blurple()}.get(s, discord.Color.greyple())
    embed = discord.Embed(title=f"{status_emoji(s)}  {op.get('type','op').capitalize()} — {s.upper()}", description=op.get("message", ""), color=color)
    embed.add_field(name="Service", value=op.get("service", "—"), inline=True)
    if op.get("version"): embed.add_field(name="Version", value=f"`{op['version']}`", inline=True)
    url = op.get("healthcheck_url")
    if url: embed.add_field(name="Deployment URL", value=f"[Go to Service]({url})\n`{url}`", inline=False)
    if op.get("error"): embed.add_field(name="Error", value=f"```{op['error']}```", inline=False)
    embed.set_footer(text=f"op_id: {op.get('id','?')}")
    return embed

async def poll_operation(op_id: str, message=None, max_wait: int = 60) -> dict:
    last_status = "pending"
    for _ in range(max_wait // 2):
        await asyncio.sleep(2)
        op = await api_get(f"/operations/{op_id}")
        status = op["status"]
        if status != last_status and status == "running" and message:
            last_status = status
            try: await message.edit(embed=op_embed(op))
            except: pass
        if status in ("success", "failed"): return op
    return await api_get(f"/operations/{op_id}")

async def send_live_health_monitor(interaction: discord.Interaction, service_name: str = None):
    title = f"🔍 Initializing {service_name or 'Global API'} monitor..."
    msg = await interaction.followup.send(embed=discord.Embed(title=title, color=discord.Color.greyple()))
    async def monitor_loop():
        i = 0
        while True:
            i += 1
            try:
                if service_name:
                    data = await api_get(f"/services/{service_name}")
                    status = data['status']
                    color = discord.Color.green() if status == "healthy" else discord.Color.orange() if status == "booting" else discord.Color.red()
                    title = f"{'🟢' if status == 'healthy' else '🟡' if status == 'booting' else '🔴'} Service: {service_name}"
                    desc = f"Status: **{status}**\nNode: `{data['node']}`\nURL: {data['healthcheck_url'] or 'None'}"
                else:
                    data = await api_get("/health")
                    status = data['status']
                    color = discord.Color.green() if status == "ok" else discord.Color.red()
                    title = f"{'🟢' if status == 'ok' else '🔴'} Heimdall API — Healthy"
                    desc = f"Status: **{status}**\nURL: `{API_URL}`"
                embed = discord.Embed(title=title, description=desc, color=color)
                embed.set_footer(text=f"Live monitoring active • Updates: {i}")
                await msg.edit(embed=embed)
            except Exception as e:
                embed = discord.Embed(title=f"🔴 {service_name or 'Heimdall API'} — Unreachable", description=f"Failed to connect to Control Plane.\n\n**Error:**\n```{e}```", color=discord.Color.red())
                embed.set_footer(text=f"Live monitoring active • Updates: {i}")
                await msg.edit(embed=embed)
            await asyncio.sleep(5)
    asyncio.create_task(monitor_loop())

def normalize_agent_host(host: str) -> str:
    raw = host.strip()
    if not raw:
        return raw
    if "://" in raw:
        return raw
    if ":" in raw:
        return f"http://{raw}"
    return f"http://{raw}:8001"

@tree.command(name="register-node", description="Register a new infrastructure node (agent).")
@app_commands.describe(name="Display name for the node", node_id="Unique identifier for the node", host="Agent host or IP (e.g. nixos or 10.0.0.5)")
async def cmd_node_register(interaction: discord.Interaction, name: str, node_id: str, host: str):
    await safe_defer(interaction)
    payload = {"name": name, "uuid": node_id, "host": normalize_agent_host(host)}
    try:
        resp = await api_post("/nodes", payload)
        await interaction.followup.send(f"✅ {resp.get('message', 'Node registered.')}")
    except Exception as e: await send_error_embed(interaction, str(e))

@tree.command(name="register", description="Declare a new service configuration.")
@app_commands.describe(service="Service name", node_name="Target node name", flake="Nix flake reference")
async def cmd_register(interaction: discord.Interaction, service: str, node_name: str, flake: str = None):
    await safe_defer(interaction)
    payload = {"service": service, "node_name": node_name, "triggered_by": str(interaction.user)}
    if flake:
        payload["flake"] = flake
    try:
        resp = await api_post("/services", payload)
        await interaction.followup.send(f"✅ {resp.get('message', 'Service declared.')}")
        await send_live_health_monitor(interaction, service_name=service)
    except Exception as e:
        await send_error_embed(interaction, str(e))
        if "Connect" in str(e): await send_live_health_monitor(interaction, service_name=service)

@tree.command(name="deploy", description="Trigger a project deployment.")
@app_commands.describe(service="Service name", node_name="Override node", version="Version tag/branch")
async def cmd_deploy(interaction: discord.Interaction, service: str, node_name: str = None, version: str = "latest"):
    await safe_defer(interaction)
    payload = {"service": service, "version": version, "triggered_by": str(interaction.user)}
    if node_name:
        payload["node_name"] = node_name
    try:
        resp = await api_post("/deploy", payload)
        op_id = resp["operation_id"]
        msg = await interaction.followup.send(embed=discord.Embed(title="⏳ Deploy queued", description=resp["message"], color=discord.Color.blurple()).set_footer(text=f"op_id: {op_id}"))
        op = await poll_operation(op_id, message=msg)
        await msg.edit(embed=op_embed(op))
        await send_live_health_monitor(interaction, service_name=service)
    except Exception as e:
        await send_error_embed(interaction, str(e))
        if "Connect" in str(e): await send_live_health_monitor(interaction, service_name=service)

@tree.command(name="teardown", description="Decommission a service from its node.")
@app_commands.describe(service="Service name to teardown")
async def cmd_teardown(interaction: discord.Interaction, service: str):
    await safe_defer(interaction)
    try:
        resp = await api_post("/teardown", {"service": service, "triggered_by": str(interaction.user)})
        op_id = resp["operation_id"]
        await interaction.followup.send(embed=discord.Embed(title=f"🗑️ Teardown — {service}", description="Queued...", color=discord.Color.orange()))
        op = await poll_operation(op_id)
        await interaction.followup.send(embed=op_embed(op))
    except Exception as e: await send_error_embed(interaction, str(e))

@tree.command(name="command", description="Run a manifest command for a service.")
@app_commands.describe(service="Service name", command="Manifest command name", node_name="Override node")
async def cmd_command(interaction: discord.Interaction, service: str, command: str, node_name: str = None):
    await safe_defer(interaction)
    payload = {"service": service, "command": command, "triggered_by": str(interaction.user)}
    if node_name:
        payload["node_name"] = node_name
    try:
        resp = await api_post("/command", payload)
        op_id = resp["operation_id"]
        msg = await interaction.followup.send(embed=discord.Embed(title="⏳ Command queued", description=resp["message"], color=discord.Color.blurple()).set_footer(text=f"op_id: {op_id}"))
        op = await poll_operation(op_id, message=msg)
        await msg.edit(embed=op_embed(op))
    except Exception as e:
        await send_error_embed(interaction, str(e))

@tree.command(name="rollback", description="Roll back a service.")
@app_commands.describe(service="Service name", target_version="Version to roll back to")
async def cmd_rollback(interaction: discord.Interaction, service: str, target_version: str, reason: str = ""):
    await safe_defer(interaction)
    try:
        resp = await api_post("/rollback", {
            "service": service,
            "target_version": target_version,
            "reason": reason or None,
            "triggered_by": str(interaction.user)
        })
        op_id = resp["operation_id"]
        await interaction.followup.send(embed=discord.Embed(title="⏳ Rollback queued", description=resp["message"], color=discord.Color.gold()).set_footer(text=f"op_id: {op_id}"))
        op = await poll_operation(op_id)
        await interaction.followup.send(embed=op_embed(op))
    except Exception as e: await send_error_embed(interaction, str(e))

@tree.command(name="status", description="Get operation status.")
async def cmd_status(interaction: discord.Interaction, operation_id: str):
    await safe_defer(interaction)
    try:
        op = await api_get(f"/operations/{operation_id}")
        await interaction.followup.send(embed=op_embed(op))
    except Exception as e: await send_error_embed(interaction, str(e))

@tree.command(name="nodes", description="List registered nodes.")
async def cmd_nodes(interaction: discord.Interaction):
    await safe_defer(interaction)
    try:
        nodes = await api_get("/nodes")
        embed = discord.Embed(title=f"🖥️ Registered Nodes ({len(nodes)})", color=discord.Color.teal())
        for n in nodes:
            s = n.get("status", "UNKNOWN")
            embed.add_field(name=f"{node_emoji(s)} {n['name']}", value=f"Host: `{n['host']}`", inline=True)
        await interaction.followup.send(embed=embed)
    except Exception as e: await send_error_embed(interaction, str(e))

@tree.command(name="services", description="List all registered services and their status.")
async def cmd_services(interaction: discord.Interaction):
    await safe_defer(interaction)
    try:
        services = await api_get("/services")
        if not services:
            await interaction.followup.send("No services registered.")
            return

        embed = discord.Embed(
            title=f"📦 Registered Services ({len(services)})",
            color=discord.Color.blue(),
        )
        for s in services:
            status = s.get("status", "unknown")
            node = s.get("node_name", "—")
            emoji = status_emoji(status)
            
            value = f"Node: `{node}`\nStatus: **{status}**"
            embed.add_field(name=f"{emoji} {s['name']}", value=value, inline=True)

        await interaction.followup.send(embed=embed)
    except Exception as e: await send_error_embed(interaction, str(e))

@tree.command(name="health", description="Live health monitor.")
async def cmd_health(interaction: discord.Interaction, service: str = None):
    await safe_defer(interaction)
    await send_live_health_monitor(interaction, service_name=service)

@tree.command(name="deploy-all", description="Trigger a full deployment for ALL registered services.")
async def cmd_deploy_all(interaction: discord.Interaction):
    await safe_defer(interaction)
    try:
        resp = await api_post("/deploy-all", {"triggered_by": str(interaction.user)})
        ids = resp.get("operation_ids", [])
        await interaction.followup.send(
            embed=discord.Embed(
                title="🚀 Bulk Deploy Initiated",
                description=f"Queued **{len(ids)}** deployments.\n\n{resp.get('message')}",
                color=discord.Color.purple()
            ).set_footer(text=f"Triggered by: {interaction.user}")
        )
    except Exception as e: await send_error_embed(interaction, str(e))

@tree.command(name="audit", description="View recent infrastructure audit logs.")
@app_commands.describe(limit="Number of logs to show (max 50)")
async def cmd_audit(interaction: discord.Interaction, limit: int = 15):
    await safe_defer(interaction)
    try:
        logs = await api_get(f"/operations/audit?limit={limit}")
        if not logs:
            await interaction.followup.send("No audit logs found.")
            return

        embed = discord.Embed(title="🛡️ Infrastructure Audit Logs", color=discord.Color.dark_grey())
        for log in logs:
            emoji = status_emoji(log.get("status", ""))
            time_str = log.get("created_at", "").split("T")[1][:8] if "T" in log.get("created_at", "") else "???"
            
            name = f"{emoji} {log['type'].upper()} — {log.get('service', 'General')}"
            value = f"By: **{log.get('triggered_by', 'System')}** at `{time_str}`\nStatus: `{log.get('status')}`"
            embed.add_field(name=name, value=value, inline=False)

        await interaction.followup.send(embed=embed)
    except Exception as e: await send_error_embed(interaction, str(e))

@tree.command(name="alerts", description="Configure where Heimdall posts down-alerts (node OFFLINE / service dead).")
@app_commands.describe(channel="Channel to post alerts to (omit to use this channel)")
@app_commands.checks.has_permissions(manage_guild=True)
async def cmd_alerts(interaction: discord.Interaction, channel: discord.TextChannel | None = None):
    await safe_defer(interaction)
    target = channel or interaction.channel
    if not isinstance(target, discord.TextChannel):
        await interaction.followup.send("This command must be used in a server text channel (or specify one).")
        return

    global _alert_channel_id
    _alert_channel_id = target.id
    _save_alert_channel_id(_alert_channel_id)
    await _restart_alert_task()
    await interaction.followup.send(f"Alerts channel set to {target.mention}.")

@cmd_alerts.error
async def cmd_alerts_error(interaction: discord.Interaction, error: Exception):
    # Keep error handling simple and user-friendly.
    if isinstance(error, app_commands.MissingPermissions):
        if not interaction.response.is_done():
            await interaction.response.send_message("Missing permission: Manage Server.", ephemeral=True)
        else:
            await interaction.followup.send("Missing permission: Manage Server.", ephemeral=True)
        return
    raise error

@tree.command(name="add-node", description="Alias for /register-node.")
@app_commands.describe(name="Display name", node_id="Unique ID", host="Agent URL")
async def cmd_add_node(interaction: discord.Interaction, name: str, node_id: str, host: str):
    # Just proxy to the same command logic
    await cmd_node_register(interaction, name, node_id, host)

@bot.event
async def setup_hook():
    """
    discord.py docs recommend doing async setup here (called once) instead of
    in on_ready() (which can run multiple times due to reconnect/resume).
    """
    global _http
    _http = aiohttp.ClientSession(timeout=HTTP_TIMEOUT)
    await tree.sync()
    global _alert_channel_id
    _alert_channel_id = _load_alert_channel_id()
    await _restart_alert_task()

@bot.event
async def on_ready():
    print(f"Heimdall bot ready: {bot.user}")

@bot.event
async def on_disconnect():
    # Helpful when running headless under systemd; shows when the gateway drops.
    print("Discord gateway disconnected; discord.py will attempt to reconnect.")

if __name__ == "__main__":
    bot.run(DISCORD_TOKEN)
