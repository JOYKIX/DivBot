import json
import asyncio
import time
import os
import random
import string

from dotenv import load_dotenv
from twitchio.ext import commands as twitch_commands
import discord
from discord.ext import commands as discord_commands

# ===== LOAD ENV =====
load_dotenv()

TWITCH_TOKEN = os.getenv("TWITCH_TOKEN")
TWITCH_CHANNEL = os.getenv("TWITCH_CHANNEL")

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = int(os.getenv("GUILD_ID"))

COOLDOWN = 10
CODE_EXPIRATION = 120

if not TWITCH_TOKEN or not DISCORD_TOKEN:
    raise Exception("❌ Config manquante")

# ===== FILE UTILS =====
def load_json(file, default):
    try:
        with open(file, "r") as f:
            return json.load(f)
    except:
        return default

def save_json(file, data):
    with open(file, "w") as f:
        json.dump(data, f, indent=4)

# ===== DATA =====
links = load_json("links.json", {})
teams = load_json("teams.json", {"teams": {}})
config = load_json("config.json", {"rules": []})

cooldowns = {}
pending_codes = {}

# ===== DISCORD BOT =====
intents = discord.Intents.all()

class DiscordBot(discord_commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=intents)

    async def on_ready(self):
        print(f"[DISCORD] Connecté : {self.user}")

discord_bot = DiscordBot()

# ===== UTILS =====
def generate_code():
    return ''.join(random.choices(string.ascii_uppercase + string.digits, k=6))

def save_teams():
    save_json("teams.json", teams)

def save_links():
    save_json("links.json", links)

# ===== ROLE UTILS =====
async def give_role(discord_id, role_name):
    guild = discord_bot.get_guild(GUILD_ID)
    if not guild:
        return

    member = guild.get_member(discord_id)
    if not member:
        return

    role = discord.utils.get(guild.roles, name=role_name)
    if not role:
        return

    if role not in member.roles:
        await member.add_roles(role)

# ===== DISCORD COMMANDS =====

# 🔗 VERIFY
@discord_bot.command()
async def verify(ctx, code: str):
    code = code.upper()

    if code not in pending_codes:
        await ctx.send("❌ Code invalide")
        return

    data = pending_codes[code]

    if time.time() > data["expires"]:
        del pending_codes[code]
        await ctx.send("❌ Code expiré")
        return

    twitch_user = data["user"]

    links[twitch_user] = ctx.author.id
    save_links()

    del pending_codes[code]

    await ctx.send(f"✅ Compte lié à {twitch_user}")

# ===== RULE SYSTEM =====

@discord_bot.command()
async def addrule(ctx, trigger_type, value, role_name):
    rule = {
        "type": trigger_type,
        "value": value,
        "action": "give_role",
        "role": role_name
    }

    config["rules"].append(rule)
    save_json("config.json", config)

    await ctx.send(f"✅ Règle ajoutée : {trigger_type} → {value} → {role_name}")

@discord_bot.command()
async def rules(ctx):
    if not config["rules"]:
        await ctx.send("❌ Aucune règle")
        return

    msg = "📋 Règles :\n"
    for i, r in enumerate(config["rules"]):
        msg += f"{i} | {r['type']} : {r['value']} → {r['role']}\n"

    await ctx.send(msg)

@discord_bot.command()
async def delrule(ctx, index: int):
    try:
        removed = config["rules"].pop(index)
        save_json("config.json", config)
        await ctx.send(f"🗑️ Supprimé : {removed}")
    except:
        await ctx.send("❌ Index invalide")

# ===== TEAM SYSTEM =====

@discord_bot.command()
async def createteam(ctx, role: discord.Role, emoji):
    name = role.name.lower()

    if name in teams["teams"]:
        await ctx.send("❌ Cette équipe existe déjà")
        return

    teams["teams"][name] = {
        "role_id": role.id,
        "points": 0,
        "emoji": emoji
    }

    save_teams()

    await ctx.send(f"✅ Équipe créée : {emoji} {role.name}")

@discord_bot.command()
async def join(ctx, role: discord.Role):
    user = ctx.author

    team_roles = [t["role_id"] for t in teams["teams"].values()]

    if role.id not in team_roles:
        await ctx.send("❌ Ce rôle n'est pas une équipe")
        return

    roles_to_remove = [r for r in user.roles if r.id in team_roles and r != role]

    if roles_to_remove:
        await user.remove_roles(*roles_to_remove)

    if role not in user.roles:
        await user.add_roles(role)

    await ctx.send(f"✅ Tu as rejoint {role.name}")

@discord_bot.command()
async def addpoints(ctx, role: discord.Role, amount: int):
    name = role.name.lower()

    if name not in teams["teams"]:
        await ctx.send("❌ Équipe introuvable")
        return

    teams["teams"][name]["points"] += amount
    save_teams()

    await ctx.send(f"🏆 {role.name} +{amount} points")

@discord_bot.command()
async def win(ctx, role: discord.Role):
    name = role.name.lower()

    if name not in teams["teams"]:
        await ctx.send("❌ Équipe introuvable")
        return

    teams["teams"][name]["points"] += 10
    save_teams()

    await ctx.send(f"🔥 Victoire pour {role.name} (+10 pts)")

@discord_bot.command()
async def leaderboard(ctx):
    guild = ctx.guild

    sorted_teams = sorted(
        teams["teams"].items(),
        key=lambda x: x[1]["points"],
        reverse=True
    )

    msg = "🏆 **Classement des équipes** 🏆\n\n"

    for i, (name, data) in enumerate(sorted_teams, start=1):
        role = guild.get_role(data["role_id"])

        if role:
            msg += f"{i}. {data['emoji']} **{role.name}** — {data['points']} pts ({len(role.members)} joueurs)\n"

    await ctx.send(msg)

# ===== TWITCH BOT =====
class TwitchBot(twitch_commands.Bot):

    def __init__(self):
        super().__init__(
            token=TWITCH_TOKEN,
            prefix="!",
            initial_channels=[TWITCH_CHANNEL]
        )

    async def event_ready(self):
        print(f"[TWITCH] Connecté : {self.nick}")

    async def event_message(self, message):
        if message.echo:
            return

        username = message.author.name.lower()
        msg = message.content

        # ===== LINK =====
        if msg.lower().startswith("!link"):
            code = generate_code()

            pending_codes[code] = {
                "user": username,
                "expires": time.time() + CODE_EXPIRATION
            }

            if username == TWITCH_CHANNEL.lower():
                await message.channel.send(f"{username}, code : {code} | !verify {code}")
                return

            try:
                await message.author.send(f"🔐 Code : {code} | !verify {code} sur Discord")
                await message.channel.send(f"{username}, regarde tes messages privés 👍")
            except:
                await message.channel.send(f"{username}, impossible de t'envoyer un message privé")

            return

        # ===== COOLDOWN =====
        now = time.time()
        if username in cooldowns and now - cooldowns[username] < COOLDOWN:
            return

        cooldowns[username] = now

        # ===== RULE ENGINE =====
        if username not in links:
            return

        discord_id = links[username]

        for rule in config["rules"]:
            if rule["type"] == "contains":
                if rule["value"].lower() in msg.lower():
                    await give_role(discord_id, rule["role"])

            elif rule["type"] == "emote":
                if message.tags.get("emotes") and rule["value"] in msg:
                    await give_role(discord_id, rule["role"])

# ===== RUN =====
twitch_bot = TwitchBot()

loop = asyncio.get_event_loop()
loop.create_task(discord_bot.start(DISCORD_TOKEN))
loop.create_task(twitch_bot.start())

loop.run_forever()