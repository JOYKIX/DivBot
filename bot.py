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


def get_required_env(name):
    value = os.getenv(name)
    if value is None or not value.strip():
        raise Exception(f"❌ Variable d'environnement manquante : {name}")
    return value.strip()


TWITCH_TOKEN = get_required_env("TWITCH_TOKEN")
TWITCH_CHANNEL = get_required_env("TWITCH_CHANNEL")
DISCORD_TOKEN = get_required_env("DISCORD_TOKEN")
GUILD_ID = int(get_required_env("GUILD_ID"))

COOLDOWN = 10
CODE_EXPIRATION = 120
WIN_POINTS = 10
ALLOWED_RULE_TYPES = {"contains", "emote"}


# ===== FILE UTILS =====
def load_json(file, default):
    try:
        with open(file, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default



def save_json(file, data):
    with open(file, "w", encoding="utf-8") as f:
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

    async def on_command_error(self, ctx, error):
        if isinstance(error, discord_commands.MissingPermissions):
            await ctx.send("❌ Tu n'as pas la permission d'utiliser cette commande")
            return

        if isinstance(error, discord_commands.MissingRequiredArgument):
            await ctx.send("❌ Argument manquant pour cette commande")
            return

        if isinstance(error, discord_commands.BadArgument):
            await ctx.send("❌ Argument invalide")
            return

        if isinstance(error, discord_commands.CommandNotFound):
            return

        raise error



discord_bot = DiscordBot()


# ===== UTILS =====
def generate_code():
    return "".join(random.choices(string.ascii_uppercase + string.digits, k=6))



def save_teams():
    save_json("teams.json", teams)



def save_links():
    save_json("links.json", links)



def cleanup_expired_codes():
    now = time.time()
    expired_codes = [
        code for code, data in pending_codes.items()
        if now > data["expires"]
    ]

    for code in expired_codes:
        del pending_codes[code]



def unlink_twitch_user(twitch_user):
    links.pop(twitch_user, None)



def unlink_discord_user(discord_id):
    linked_accounts = [
        twitch_user for twitch_user, linked_discord_id in links.items()
        if linked_discord_id == discord_id
    ]

    for twitch_user in linked_accounts:
        del links[twitch_user]

    return linked_accounts


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
    cleanup_expired_codes()
    code = code.upper()

    if code not in pending_codes:
        await ctx.send("❌ Code invalide")
        return

    data = pending_codes[code]
    twitch_user = data["user"]

    unlink_twitch_user(twitch_user)
    unlink_discord_user(ctx.author.id)
    links[twitch_user] = ctx.author.id
    save_links()

    del pending_codes[code]

    await ctx.send(f"✅ Compte lié à {twitch_user}")


@discord_bot.command()
async def unlink(ctx):
    removed_accounts = unlink_discord_user(ctx.author.id)

    if not removed_accounts:
        await ctx.send("❌ Aucun compte Twitch lié à ton compte Discord")
        return

    save_links()
    removed_list = ", ".join(removed_accounts)
    await ctx.send(f"🔓 Liaison supprimée : {removed_list}")


# ===== RULE SYSTEM =====
@discord_bot.command()
@discord_commands.has_permissions(manage_guild=True)
async def addrule(ctx, trigger_type, value, role_name):
    trigger_type = trigger_type.lower()

    if trigger_type not in ALLOWED_RULE_TYPES:
        allowed_types = ", ".join(sorted(ALLOWED_RULE_TYPES))
        await ctx.send(f"❌ Type invalide. Types autorisés : {allowed_types}")
        return

    rule = {
        "type": trigger_type,
        "value": value,
        "action": "give_role",
        "role": role_name,
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
@discord_commands.has_permissions(manage_guild=True)
async def delrule(ctx, index: int):
    try:
        removed = config["rules"].pop(index)
        save_json("config.json", config)
        await ctx.send(f"🗑️ Supprimé : {removed}")
    except IndexError:
        await ctx.send("❌ Index invalide")


# ===== TEAM SYSTEM =====
@discord_bot.command()
@discord_commands.has_permissions(manage_guild=True)
async def createteam(ctx, role: discord.Role, emoji):
    name = role.name.lower()

    if name in teams["teams"]:
        await ctx.send("❌ Cette équipe existe déjà")
        return

    teams["teams"][name] = {
        "role_id": role.id,
        "points": 0,
        "emoji": emoji,
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
@discord_commands.has_permissions(manage_guild=True)
async def addpoints(ctx, role: discord.Role, amount: int):
    name = role.name.lower()

    if name not in teams["teams"]:
        await ctx.send("❌ Équipe introuvable")
        return

    teams["teams"][name]["points"] += amount
    save_teams()

    await ctx.send(f"🏆 {role.name} +{amount} points")


@discord_bot.command()
@discord_commands.has_permissions(manage_guild=True)
async def win(ctx, role: discord.Role):
    name = role.name.lower()

    if name not in teams["teams"]:
        await ctx.send("❌ Équipe introuvable")
        return

    teams["teams"][name]["points"] += WIN_POINTS
    save_teams()

    await ctx.send(f"🔥 Victoire pour {role.name} (+{WIN_POINTS} pts)")


@discord_bot.command()
async def leaderboard(ctx):
    guild = ctx.guild

    sorted_teams = sorted(
        teams["teams"].items(),
        key=lambda x: x[1]["points"],
        reverse=True,
    )

    msg = "🏆 **Classement des équipes** 🏆\n\n"

    for i, (_, data) in enumerate(sorted_teams, start=1):
        role = guild.get_role(data["role_id"])

        if role:
            msg += (
                f"{i}. {data['emoji']} **{role.name}** — {data['points']} pts "
                f"({len(role.members)} joueurs)\n"
            )

    await ctx.send(msg)


# ===== TWITCH BOT =====
class TwitchBot(twitch_commands.Bot):
    def __init__(self):
        super().__init__(
            token=TWITCH_TOKEN,
            prefix="!",
            initial_channels=[TWITCH_CHANNEL],
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
            cleanup_expired_codes()
            code = generate_code()

            pending_codes[code] = {
                "user": username,
                "expires": time.time() + CODE_EXPIRATION,
            }

            if username == TWITCH_CHANNEL.lower():
                await message.channel.send(f"{username}, code : {code} | !verify {code}")
                return

            try:
                await message.author.send(f"🔐 Code : {code} | !verify {code} sur Discord")
                await message.channel.send(f"{username}, regarde tes messages privés 👍")
            except discord.Forbidden:
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
            if rule["type"] == "contains" and rule["value"].lower() in msg.lower():
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
