import asyncio
import json
import os
import random
import string
import time
from pathlib import Path
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands as discord_commands
from dotenv import load_dotenv
from twitchio.ext import commands as twitch_commands


BASE_DIR = Path(__file__).parent
load_dotenv(BASE_DIR / ".env")


def get_required_env(name: str) -> str:
    value = os.getenv(name)
    if value is None or not value.strip():
        raise RuntimeError(f"Variable d'environnement manquante : {name}")
    return value.strip()


TWITCH_TOKEN = get_required_env("TWITCH_TOKEN")
TWITCH_CHANNEL = get_required_env("TWITCH_CHANNEL")
DISCORD_TOKEN = get_required_env("DISCORD_TOKEN")
GUILD_ID = int(get_required_env("GUILD_ID"))

COOLDOWN = 10
CODE_EXPIRATION = 120
WIN_POINTS = 10
MAX_TEAM_MEMBERS_DISPLAY = 10
MAX_TEAM_MEMBERS_DETAIL = 25
ALLOWED_RULE_TYPES = {"contains", "emote"}
DEFAULT_COLOR = discord.Color.blurple()
SUCCESS_COLOR = discord.Color.green()
ERROR_COLOR = discord.Color.red()
WARNING_COLOR = discord.Color.orange()
INFO_COLOR = discord.Color.gold()
DATA_FILES: dict[str, Any] = {
    "links.json": {},
    "teams.json": {"teams": {}},
    "config.json": {"rules": []},
}


def data_path(filename: str) -> Path:
    return BASE_DIR / filename


# ===== FILE UTILS =====
def load_json(filename: str, default: Any) -> Any:
    path = data_path(filename)
    try:
        with path.open("r", encoding="utf-8") as file:
            return json.load(file)
    except (FileNotFoundError, json.JSONDecodeError):
        return default



def save_json(filename: str, data: Any) -> None:
    path = data_path(filename)
    with path.open("w", encoding="utf-8") as file:
        json.dump(data, file, indent=4, ensure_ascii=False)



def ensure_data_files() -> None:
    for filename, default_value in DATA_FILES.items():
        path = data_path(filename)
        if not path.exists():
            save_json(filename, default_value)


ensure_data_files()


# ===== DATA =====
links = load_json("links.json", {})
teams = load_json("teams.json", {"teams": {}})
config = load_json("config.json", {"rules": []})

cooldowns: dict[str, float] = {}
pending_codes: dict[str, dict[str, Any]] = {}
active_duel: dict[str, Any] | None = None


def build_embed(title: str, description: str, color: discord.Color = DEFAULT_COLOR) -> discord.Embed:
    return discord.Embed(title=title, description=description, color=color)


async def send_ctx_embed(ctx: discord_commands.Context, title: str, description: str, color: discord.Color) -> None:
    await ctx.send(embed=build_embed(title, description, color))


async def send_interaction_embed(
    interaction: discord.Interaction,
    title: str,
    description: str,
    color: discord.Color,
    *,
    ephemeral: bool = False,
) -> None:
    embed = build_embed(title, description, color)
    if interaction.response.is_done():
        await interaction.followup.send(embed=embed, ephemeral=ephemeral)
        return
    await interaction.response.send_message(embed=embed, ephemeral=ephemeral)


# ===== UTILS =====
def generate_code() -> str:
    return "".join(random.choices(string.ascii_uppercase + string.digits, k=6))



def save_teams() -> None:
    save_json("teams.json", teams)



def save_links() -> None:
    save_json("links.json", links)



def save_config() -> None:
    save_json("config.json", config)



def normalize_team_data() -> None:
    changed = False
    for team_data in teams["teams"].values():
        if "points" not in team_data:
            team_data["points"] = 0
            changed = True
        if "emoji" not in team_data:
            team_data["emoji"] = "🏷️"
            changed = True
        if "wins" not in team_data:
            team_data["wins"] = 0
            changed = True
        if "losses" not in team_data:
            team_data["losses"] = 0
            changed = True

    if changed:
        save_teams()


normalize_team_data()


def cleanup_expired_codes() -> None:
    now = time.time()
    expired_codes = [
        code for code, data in pending_codes.items() if now > data["expires"]
    ]

    for code in expired_codes:
        del pending_codes[code]


def remove_pending_codes_for_discord_user(discord_id: int) -> None:
    codes_to_remove = [
        code for code, data in pending_codes.items() if data.get("discord_id") == discord_id
    ]
    for code in codes_to_remove:
        del pending_codes[code]



def unlink_twitch_user(twitch_user: str) -> None:
    links.pop(twitch_user, None)



def unlink_discord_user(discord_id: int) -> list[str]:
    linked_accounts = [
        twitch_user
        for twitch_user, linked_discord_id in links.items()
        if linked_discord_id == discord_id
    ]

    for twitch_user in linked_accounts:
        del links[twitch_user]

    return linked_accounts



def is_known_team_role(role: discord.Role) -> bool:
    return role.id in [team["role_id"] for team in teams["teams"].values()]



def get_team_entry_by_name(team_name: str) -> tuple[str, dict[str, Any]] | None:
    normalized_name = team_name.strip().lower()
    return next(
        (
            (stored_name, data)
            for stored_name, data in teams["teams"].items()
            if stored_name == normalized_name
        ),
        None,
    )


def get_team_entry_by_role(role: discord.Role) -> tuple[str, dict[str, Any]] | None:
    return get_team_entry_by_name(role.name)


def get_team_role(guild: discord.Guild, team_data: dict[str, Any]) -> discord.Role | None:
    return guild.get_role(team_data["role_id"])


def get_member_team_roles(member: discord.Member) -> list[discord.Role]:
    team_role_ids = {team["role_id"] for team in teams["teams"].values()}
    return [role for role in member.roles if role.id in team_role_ids]


def format_member_list(role: discord.Role) -> str:
    if not role.members:
        return "Aucun membre"

    member_names = [member.display_name for member in role.members[:MAX_TEAM_MEMBERS_DISPLAY]]
    extra_members = len(role.members) - len(member_names)
    if extra_members > 0:
        member_names.append(f"+{extra_members} autre(s)")
    return ", ".join(member_names)


def format_rules() -> str:
    if not config["rules"]:
        return "Aucune règle configurée pour le moment."

    lines = []
    for index, rule in enumerate(config["rules"]):
        lines.append(f"`{index}` • **{rule['type']}** → `{rule['value']}` → **{rule['role']}**")
    return "\n".join(lines)



def leaderboard_lines(guild: discord.Guild) -> list[str]:
    sorted_teams = sorted(
        teams["teams"].items(),
        key=lambda item: item[1]["points"],
        reverse=True,
    )

    lines = []
    for index, (_, data) in enumerate(sorted_teams, start=1):
        role = guild.get_role(data["role_id"])
        if not role:
            continue
        lines.append(
            f"**{index}.** {data['emoji']} **{role.name}** — `{data['points']} pts` • "
            f"`{data['wins']}V-{data['losses']}D` • `{len(role.members)} joueurs`"
        )
    return lines


def placement_emoji(position: int) -> str:
    if position == 1:
        return "🥇"
    if position == 2:
        return "🥈"
    if position == 3:
        return "🥉"
    return "🏅"


def team_winrate(team_data: dict[str, Any]) -> float:
    total_matches = team_data["wins"] + team_data["losses"]
    if total_matches <= 0:
        return 0.0
    return (team_data["wins"] / total_matches) * 100


def leaderboard_embed(guild: discord.Guild) -> discord.Embed:
    sorted_teams = sorted(
        teams["teams"].items(),
        key=lambda item: (item[1]["points"], item[1]["wins"]),
        reverse=True,
    )

    if not sorted_teams:
        return build_embed("🏆 Leaderboard des équipes", "Aucune équipe n'est configurée pour le moment.", INFO_COLOR)

    embed = build_embed(
        "🏆 Leaderboard des équipes",
        "Classement général des teams du serveur.",
        INFO_COLOR,
    )

    ranking_lines = []
    for index, (_, data) in enumerate(sorted_teams, start=1):
        role = get_team_role(guild, data)
        if not role:
            continue
        ranking_lines.append(
            f"{placement_emoji(index)} **#{index} • {data['emoji']} {role.mention}**\n"
            f"└ `Points: {data['points']}` • `W/L: {data['wins']}/{data['losses']}` • `Winrate: {team_winrate(data):.1f}%` • `Membres: {len(role.members)}`"
        )

    embed.add_field(name="Classement", value="\n".join(ranking_lines), inline=False)
    best_team_name, best_team_data = sorted_teams[0]
    best_team_role = get_team_role(guild, best_team_data)
    best_team_label = best_team_role.mention if best_team_role else best_team_name.title()
    embed.add_field(
        name="📌 Focus",
        value=(
            f"**Top team actuelle** : {best_team_data['emoji']} {best_team_label}\n"
            f"**Bilan** : `{best_team_data['wins']} victoire(s)` / `{best_team_data['losses']} défaite(s)`"
        ),
        inline=False,
    )
    embed.set_footer(text="Utilise /team @nomdelateam pour afficher le détail complet d'une équipe.")
    return embed


def team_overview_embed(guild: discord.Guild) -> discord.Embed:
    embed = build_embed("Équipes enregistrées", "Vue détaillée des équipes.", INFO_COLOR)

    sorted_teams = sorted(
        teams["teams"].items(),
        key=lambda item: (item[1]["points"], item[1]["wins"]),
        reverse=True,
    )

    for _, data in sorted_teams:
        role = get_team_role(guild, data)
        if not role:
            continue

        embed.add_field(
            name=f"{data['emoji']} {role.name}",
            value=(
                f"**Points** : `{data['points']}`\n"
                f"**Bilan** : `{data['wins']} victoire(s)` / `{data['losses']} défaite(s)`\n"
                f"**Membres** : {format_member_list(role)}"
            ),
            inline=False,
        )

    if not embed.fields:
        embed.description = "Aucune équipe n'est configurée pour le moment."

    return embed


def team_detail_embed(guild: discord.Guild, role: discord.Role) -> discord.Embed:
    team_entry = get_team_entry_by_role(role)
    if team_entry is None:
        return build_embed("Équipe introuvable", "Cette team n'est pas enregistrée.", ERROR_COLOR)

    _team_name, data = team_entry
    member_mentions = [member.mention for member in role.members[:MAX_TEAM_MEMBERS_DETAIL]]
    members_value = ", ".join(member_mentions) if member_mentions else "Aucun membre"
    remaining = len(role.members) - len(member_mentions)
    if remaining > 0:
        members_value += f"\n… et **{remaining}** autre(s) membre(s)."

    embed = build_embed(
        f"{data['emoji']} Détail de la team {role.name}",
        "Fiche complète de l'équipe et de ses membres.",
        INFO_COLOR,
    )
    embed.add_field(name="Points", value=f"`{data['points']}`", inline=True)
    embed.add_field(name="Victoires", value=f"`{data['wins']}`", inline=True)
    embed.add_field(name="Défaites", value=f"`{data['losses']}`", inline=True)
    embed.add_field(name="Winrate", value=f"`{team_winrate(data):.1f}%`", inline=True)
    embed.add_field(name="Nombre de membres", value=f"`{len(role.members)}`", inline=True)
    embed.add_field(name="Rôle Discord", value=role.mention, inline=True)
    embed.add_field(name="Membres", value=members_value, inline=False)
    embed.set_footer(text="Commande disponible: /team @nomdelateam")
    return embed


def start_duel(team_one_name: str, team_two_name: str, points: int) -> tuple[bool, str]:
    global active_duel

    if points <= 0:
        return False, "Le nombre de points doit être supérieur à zéro."

    if active_duel is not None:
        return False, "Un duel est déjà en cours. Termine-le avec `!win <équipe>` avant d'en lancer un autre."

    team_one = get_team_entry_by_name(team_one_name)
    team_two = get_team_entry_by_name(team_two_name)

    if team_one is None or team_two is None:
        return False, "Une des équipes indiquées n'existe pas."

    if team_one[0] == team_two[0]:
        return False, "Tu dois choisir deux équipes différentes."

    active_duel = {
        "team_one": team_one[0],
        "team_two": team_two[0],
        "points": points,
    }
    return True, (
        f"Duel lancé : **{team_one_name}** VS **{team_two_name}** pour **{points}** point(s). "
        "Utilise `!win <équipe>` pour annoncer le gagnant."
    )


def resolve_duel(winner_name: str) -> tuple[bool, str]:
    global active_duel

    if active_duel is None:
        return False, "Aucun duel n'est en cours."

    winner = get_team_entry_by_name(winner_name)
    if winner is None:
        return False, "Cette équipe n'existe pas."

    duel_teams = {active_duel["team_one"], active_duel["team_two"]}
    if winner[0] not in duel_teams:
        return False, "L'équipe gagnante doit faire partie du duel en cours."

    loser_key = next(team_name for team_name in duel_teams if team_name != winner[0])
    loser_data = teams["teams"][loser_key]
    duel_points = active_duel["points"]

    winner[1]["points"] += duel_points
    winner[1]["wins"] += 1
    loser_data["losses"] += 1
    save_teams()

    winner_display = winner[0].title()
    loser_display = loser_key.title()
    active_duel = None
    return True, (
        f"Victoire de **{winner_display}** ! +**{duel_points}** point(s). "
        f"Défaite enregistrée pour **{loser_display}**."
    )


def is_twitch_admin(author: Any) -> bool:
    return bool(
        getattr(author, "is_broadcaster", False)
        or getattr(author, "is_mod", False)
        or getattr(author, "name", "").lower() == TWITCH_CHANNEL.lower()
    )


# ===== DISCORD BOT =====
intents = discord.Intents.all()
guild_object = discord.Object(id=GUILD_ID)


class DiscordBot(discord_commands.Bot):
    def __init__(self) -> None:
        super().__init__(command_prefix="!", intents=intents)
        self.synced = False

    async def setup_hook(self) -> None:
        self.tree.copy_global_to(guild=guild_object)
        self.add_view(LinkAccountView())

    async def on_ready(self) -> None:
        if not self.synced:
            synced_commands = await self.tree.sync(guild=guild_object)
            print(f"[DISCORD] {len(synced_commands)} commandes slash synchronisées sur {GUILD_ID}")
            self.synced = True
        print(f"[DISCORD] Connecté : {self.user}")

    async def on_command_error(self, ctx: discord_commands.Context, error: Exception) -> None:
        if isinstance(error, discord_commands.MissingPermissions):
            await send_ctx_embed(ctx, "Permission refusée", "Tu n'as pas la permission d'utiliser cette commande.", ERROR_COLOR)
            return

        if isinstance(error, discord_commands.MissingRequiredArgument):
            await send_ctx_embed(ctx, "Argument manquant", "Il manque un argument pour cette commande.", ERROR_COLOR)
            return

        if isinstance(error, discord_commands.BadArgument):
            await send_ctx_embed(ctx, "Argument invalide", "Un des arguments fournis est invalide.", ERROR_COLOR)
            return

        if isinstance(error, discord_commands.CommandNotFound):
            return

        raise error



discord_bot = DiscordBot()


class LinkAccountView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Link Discord ↔ Twitch",
        style=discord.ButtonStyle.primary,
        emoji="🔗",
        custom_id="link_discord_twitch",
    )
    async def link_accounts_button(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        linked_accounts = [
            twitch_user
            for twitch_user, linked_discord_id in links.items()
            if linked_discord_id == interaction.user.id
        ]
        if linked_accounts:
            await send_interaction_embed(
                interaction,
                "Compte déjà lié",
                f"Ton compte Discord est déjà lié à **{', '.join(linked_accounts)}**.",
                WARNING_COLOR,
                ephemeral=True,
            )
            return

        cleanup_expired_codes()
        remove_pending_codes_for_discord_user(interaction.user.id)

        code = generate_code()
        pending_codes[code] = {
            "discord_id": interaction.user.id,
            "expires": time.time() + CODE_EXPIRATION,
        }

        await send_interaction_embed(
            interaction,
            "Code de liaison",
            (
                "Ton code privé est : "
                f"`{code}`\n\n"
                "Écris ce message dans le chat Twitch pour finaliser : "
                f"`!link {code}`\n"
                f"⏱️ Le code expire dans **{CODE_EXPIRATION} secondes**."
            ),
            INFO_COLOR,
            ephemeral=True,
        )


# ===== ROLE UTILS =====
async def give_role(discord_id: int, role_name: str) -> bool:
    guild = discord_bot.get_guild(GUILD_ID)
    if not guild:
        return False

    member = guild.get_member(discord_id)
    if not member:
        return False

    role = discord.utils.get(guild.roles, name=role_name)
    if not role:
        return False

    if role not in member.roles:
        await member.add_roles(role, reason="Attribution automatique via règle Twitch")
        return True

    return False


# ===== DISCORD PREFIX COMMANDS =====
@discord_bot.command(help="Associer ton compte Discord avec un code Twitch")
async def verify(ctx: discord_commands.Context, code: str) -> None:
    _ = code
    await send_ctx_embed(
        ctx,
        "Commande remplacée",
        (
            "Le système a changé : utilise le bouton **Link Discord ↔ Twitch** sur Discord, "
            "puis envoie le code dans le chat Twitch avec `!link CODE`."
        ),
        WARNING_COLOR,
    )


@discord_bot.command(help="Supprimer le lien entre Twitch et Discord")
async def unlink(ctx: discord_commands.Context) -> None:
    removed_accounts = unlink_discord_user(ctx.author.id)

    if not removed_accounts:
        await send_ctx_embed(ctx, "Aucune liaison", "Aucun compte Twitch n'est lié à ton compte Discord.", WARNING_COLOR)
        return

    save_links()
    removed_list = ", ".join(removed_accounts)
    await send_ctx_embed(ctx, "Liaison supprimée", f"Compte(s) déliés : **{removed_list}**.", SUCCESS_COLOR)


@discord_bot.command(help="Afficher toutes les règles Twitch → Discord")
async def rules(ctx: discord_commands.Context) -> None:
    await ctx.send(embed=build_embed("Règles configurées", format_rules(), INFO_COLOR))


@discord_bot.command(help="Afficher le classement des équipes")
async def leaderboard(ctx: discord_commands.Context) -> None:
    if ctx.guild is None:
        await send_ctx_embed(ctx, "Erreur", "Cette commande doit être utilisée dans le serveur.", ERROR_COLOR)
        return

    await ctx.send(embed=leaderboard_embed(ctx.guild))


@discord_bot.command(help="Afficher le détail des équipes et de leurs membres")
async def teamsinfo(ctx: discord_commands.Context) -> None:
    if ctx.guild is None:
        await send_ctx_embed(ctx, "Erreur", "Cette commande doit être utilisée dans le serveur.", ERROR_COLOR)
        return

    await ctx.send(embed=team_overview_embed(ctx.guild))


@discord_bot.command(help="Afficher le détail d'une équipe via son rôle")
async def team(ctx: discord_commands.Context, *, role_name: str) -> None:
    if ctx.guild is None:
        await send_ctx_embed(ctx, "Erreur", "Cette commande doit être utilisée dans le serveur.", ERROR_COLOR)
        return

    role = discord.utils.get(ctx.guild.roles, name=role_name)
    if role is None:
        await send_ctx_embed(
            ctx,
            "Équipe introuvable",
            "Rôle introuvable. Utilise le nom exact de la team.",
            ERROR_COLOR,
        )
        return

    await ctx.send(embed=team_detail_embed(ctx.guild, role))


# ===== DISCORD SLASH COMMANDS =====
@discord_bot.tree.command(name="verify", description="Associer ton compte Discord avec un code Twitch", guild=guild_object)
@app_commands.describe(code="Code envoyé par le bot Twitch")
async def slash_verify(interaction: discord.Interaction, code: str) -> None:
    _ = code
    await send_interaction_embed(
        interaction,
        "Commande remplacée",
        (
            "Le système a changé : utilise le bouton **Link Discord ↔ Twitch** sur Discord, "
            "puis envoie le code dans le chat Twitch avec `!link CODE`."
        ),
        WARNING_COLOR,
        ephemeral=True,
    )


@discord_bot.tree.command(name="unlink", description="Supprimer la liaison avec ton compte Twitch", guild=guild_object)
async def slash_unlink(interaction: discord.Interaction) -> None:
    removed_accounts = unlink_discord_user(interaction.user.id)

    if not removed_accounts:
        await send_interaction_embed(interaction, "Aucune liaison", "Aucun compte Twitch n'est lié à ton compte Discord.", WARNING_COLOR, ephemeral=True)
        return

    save_links()
    await send_interaction_embed(
        interaction,
        "Liaison supprimée",
        f"Compte(s) déliés : **{', '.join(removed_accounts)}**.",
        SUCCESS_COLOR,
        ephemeral=True,
    )


@discord_bot.tree.command(
    name="linkpanel",
    description="Publier l'embed avec bouton de liaison Discord ↔ Twitch",
    guild=guild_object,
)
@app_commands.checks.has_permissions(manage_guild=True)
async def slash_linkpanel(interaction: discord.Interaction) -> None:
    if interaction.channel is None:
        await send_interaction_embed(interaction, "Erreur", "Salon introuvable.", ERROR_COLOR, ephemeral=True)
        return

    embed = build_embed(
        "Connexion Discord ↔ Twitch",
        (
            "Clique sur le bouton ci-dessous pour obtenir un **code privé**.\n"
            "Ensuite, envoie ce code dans le chat Twitch avec `!link CODE`.\n\n"
            "Exemple : `!link ABC123`"
        ),
        INFO_COLOR,
    )
    await interaction.channel.send(embed=embed, view=LinkAccountView())
    await send_interaction_embed(interaction, "Panel envoyé", "Le message de liaison a été publié.", SUCCESS_COLOR, ephemeral=True)


@discord_bot.tree.command(name="addrule", description="Ajouter une règle Twitch vers un rôle Discord", guild=guild_object)
@app_commands.describe(trigger_type="contains ou emote", value="Mot-clé ou emote", role_name="Nom exact du rôle à donner")
@app_commands.checks.has_permissions(manage_guild=True)
async def slash_addrule(interaction: discord.Interaction, trigger_type: str, value: str, role_name: str) -> None:
    trigger_type = trigger_type.lower()
    if trigger_type not in ALLOWED_RULE_TYPES:
        await send_interaction_embed(
            interaction,
            "Type invalide",
            f"Types autorisés : **{', '.join(sorted(ALLOWED_RULE_TYPES))}**.",
            ERROR_COLOR,
            ephemeral=True,
        )
        return

    config["rules"].append(
        {
            "type": trigger_type,
            "value": value,
            "action": "give_role",
            "role": role_name,
        }
    )
    save_config()

    await send_interaction_embed(
        interaction,
        "Règle ajoutée",
        f"Nouvelle règle : **{trigger_type}** → `{value}` → **{role_name}**.",
        SUCCESS_COLOR,
    )


@discord_bot.tree.command(name="rules", description="Afficher les règles configurées", guild=guild_object)
async def slash_rules(interaction: discord.Interaction) -> None:
    await send_interaction_embed(interaction, "Règles configurées", format_rules(), INFO_COLOR, ephemeral=True)


@discord_bot.tree.command(name="delrule", description="Supprimer une règle par son index", guild=guild_object)
@app_commands.describe(index="Index visible dans /rules")
@app_commands.checks.has_permissions(manage_guild=True)
async def slash_delrule(interaction: discord.Interaction, index: int) -> None:
    try:
        removed = config["rules"].pop(index)
    except IndexError:
        await send_interaction_embed(interaction, "Index invalide", "Aucune règle ne correspond à cet index.", ERROR_COLOR, ephemeral=True)
        return

    save_config()
    await send_interaction_embed(
        interaction,
        "Règle supprimée",
        f"Suppression de **{removed['type']}** → `{removed['value']}` → **{removed['role']}**.",
        SUCCESS_COLOR,
    )


@discord_bot.tree.command(name="createteam", description="Créer une équipe à partir d'un rôle Discord", guild=guild_object)
@app_commands.describe(role="Rôle représentant l'équipe", emoji="Emoji affiché dans le classement")
@app_commands.checks.has_permissions(manage_guild=True)
async def slash_createteam(interaction: discord.Interaction, role: discord.Role, emoji: str) -> None:
    name = role.name.lower()
    if name in teams["teams"]:
        await send_interaction_embed(interaction, "Équipe existante", "Cette équipe existe déjà.", ERROR_COLOR, ephemeral=True)
        return

    teams["teams"][name] = {"role_id": role.id, "points": 0, "emoji": emoji, "wins": 0, "losses": 0}
    save_teams()
    await send_interaction_embed(interaction, "Équipe créée", f"Nouvelle équipe : {emoji} **{role.name}**.", SUCCESS_COLOR)


@discord_bot.tree.command(name="join", description="Rejoindre une équipe", guild=guild_object)
@app_commands.describe(role="Rôle de l'équipe à rejoindre")
async def slash_join(interaction: discord.Interaction, role: discord.Role) -> None:
    member = interaction.user
    if not isinstance(member, discord.Member):
        await send_interaction_embed(interaction, "Erreur", "Cette commande doit être utilisée dans le serveur.", ERROR_COLOR, ephemeral=True)
        return

    if not is_known_team_role(role):
        await send_interaction_embed(interaction, "Équipe introuvable", "Ce rôle n'est pas enregistré comme équipe.", ERROR_COLOR, ephemeral=True)
        return

    current_team_roles = [existing_role for existing_role in get_member_team_roles(member) if existing_role != role]
    if current_team_roles:
        await send_interaction_embed(
            interaction,
            "Déjà dans une équipe",
            f"Tu es déjà dans **{current_team_roles[0].name}**. Quitte ton équipe actuelle avant d'en rejoindre une autre.",
            WARNING_COLOR,
            ephemeral=True,
        )
        return

    if role not in member.roles:
        await member.add_roles(role, reason="Rejoint une équipe")

    await send_interaction_embed(interaction, "Équipe rejointe", f"Tu as rejoint **{role.name}**.", SUCCESS_COLOR, ephemeral=True)


@discord_bot.tree.command(name="addpoints", description="Ajouter des points à une équipe", guild=guild_object)
@app_commands.describe(role="Rôle de l'équipe", amount="Nombre de points à ajouter")
@app_commands.checks.has_permissions(manage_guild=True)
async def slash_addpoints(interaction: discord.Interaction, role: discord.Role, amount: int) -> None:
    name = role.name.lower()
    if name not in teams["teams"]:
        await send_interaction_embed(interaction, "Équipe introuvable", "Cette équipe n'existe pas.", ERROR_COLOR, ephemeral=True)
        return

    teams["teams"][name]["points"] += amount
    save_teams()
    await send_interaction_embed(interaction, "Points ajoutés", f"**{role.name}** reçoit **{amount}** point(s).", SUCCESS_COLOR)


@discord_bot.tree.command(name="win", description="Attribuer une victoire à une équipe", guild=guild_object)
@app_commands.describe(role="Rôle de l'équipe gagnante")
@app_commands.checks.has_permissions(manage_guild=True)
async def slash_win(interaction: discord.Interaction, role: discord.Role) -> None:
    team_entry = get_team_entry_by_role(role)
    if team_entry is None:
        await send_interaction_embed(interaction, "Équipe introuvable", "Cette équipe n'existe pas.", ERROR_COLOR, ephemeral=True)
        return

    team_entry[1]["points"] += WIN_POINTS
    team_entry[1]["wins"] += 1
    save_teams()
    await send_interaction_embed(
        interaction,
        "Victoire enregistrée",
        f"🔥 **{role.name}** gagne **{WIN_POINTS}** points et ajoute une victoire à son bilan.",
        SUCCESS_COLOR,
    )


@discord_bot.tree.command(name="leaderboard", description="Afficher le classement des équipes", guild=guild_object)
async def slash_leaderboard(interaction: discord.Interaction) -> None:
    guild = interaction.guild
    if guild is None:
        await send_interaction_embed(interaction, "Erreur", "Cette commande doit être utilisée dans le serveur.", ERROR_COLOR, ephemeral=True)
        return

    embed = leaderboard_embed(guild)
    if interaction.response.is_done():
        await interaction.followup.send(embed=embed)
        return
    await interaction.response.send_message(embed=embed)


@discord_bot.tree.command(name="teams", description="Afficher les membres et les statistiques des équipes", guild=guild_object)
async def slash_teams(interaction: discord.Interaction) -> None:
    guild = interaction.guild
    if guild is None:
        await send_interaction_embed(interaction, "Erreur", "Cette commande doit être utilisée dans le serveur.", ERROR_COLOR, ephemeral=True)
        return

    embed = team_overview_embed(guild)
    if interaction.response.is_done():
        await interaction.followup.send(embed=embed)
        return
    await interaction.response.send_message(embed=embed)


@discord_bot.tree.command(name="team", description="Voir le détail d'une team et ses membres", guild=guild_object)
@app_commands.describe(role="Rôle de la team (ex: @NomDeLaTeam)")
async def slash_team(interaction: discord.Interaction, role: discord.Role) -> None:
    guild = interaction.guild
    if guild is None:
        await send_interaction_embed(interaction, "Erreur", "Cette commande doit être utilisée dans le serveur.", ERROR_COLOR, ephemeral=True)
        return

    embed = team_detail_embed(guild, role)
    is_error_embed = embed.title == "Équipe introuvable"
    if interaction.response.is_done():
        await interaction.followup.send(embed=embed, ephemeral=is_error_embed)
        return
    await interaction.response.send_message(embed=embed, ephemeral=is_error_embed)


@slash_addrule.error
@slash_linkpanel.error
@slash_delrule.error
@slash_createteam.error
@slash_addpoints.error
@slash_win.error
async def admin_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
    if isinstance(error, app_commands.MissingPermissions):
        await send_interaction_embed(interaction, "Permission refusée", "Tu n'as pas la permission d'utiliser cette commande.", ERROR_COLOR, ephemeral=True)
        return
    raise error


# ===== TWITCH BOT =====
class TwitchBot(twitch_commands.Bot):
    def __init__(self) -> None:
        super().__init__(
            token=TWITCH_TOKEN,
            prefix="!",
            initial_channels=[TWITCH_CHANNEL],
        )

    async def event_ready(self) -> None:
        print(f"[TWITCH] Connecté : {self.nick}")

    async def event_message(self, message) -> None:
        if message.echo:
            return

        username = message.author.name.lower()
        msg = message.content

        if msg.lower().startswith("!link"):
            cleanup_expired_codes()
            parts = msg.split()
            if len(parts) < 2:
                await message.channel.send(f"{username}, utilise `!link CODE` avec le code reçu sur Discord.")
                return

            code = parts[1].upper()
            data = pending_codes.get(code)
            if not data:
                await message.channel.send(f"{username}, code invalide ou expiré.")
                return

            discord_id = data["discord_id"]
            unlink_twitch_user(username)
            unlink_discord_user(discord_id)
            links[username] = discord_id
            save_links()
            del pending_codes[code]
            await self.delete_twitch_message(message)
            await self.send_link_confirmation_dm(discord_id, username)
            return

        await self.handle_commands(message)

        now = time.time()
        if username in cooldowns and now - cooldowns[username] < COOLDOWN:
            return

        cooldowns[username] = now

        if username not in links:
            return

        discord_id = links[username]
        for rule in config["rules"]:
            if rule["type"] == "contains" and rule["value"].lower() in msg.lower():
                await give_role(discord_id, rule["role"])
            elif rule["type"] == "emote" and message.tags.get("emotes") and rule["value"] in msg:
                await give_role(discord_id, rule["role"])

    @twitch_commands.command(name="duel")
    async def duel_command(self, ctx: twitch_commands.Context, team_one: str, team_two: str, points: int) -> None:
        if not is_twitch_admin(ctx.author):
            await ctx.send("Seuls le streamer ou les modérateurs peuvent lancer un duel.")
            return

        _, message = start_duel(team_one, team_two, points)
        await ctx.send(message)

    @twitch_commands.command(name="win")
    async def win_command(self, ctx: twitch_commands.Context, team_name: str) -> None:
        if not is_twitch_admin(ctx.author):
            await ctx.send("Seuls le streamer ou les modérateurs peuvent valider une victoire.")
            return

        _, message = resolve_duel(team_name)
        await ctx.send(message)

    async def delete_twitch_message(self, message) -> None:
        message_id = message.tags.get("id")
        if not message_id:
            return
        await message.channel.send(f"/delete {message_id}")

    async def send_link_confirmation_dm(self, discord_id: int, twitch_username: str) -> None:
        user = discord_bot.get_user(discord_id)
        if user is None:
            try:
                user = await discord_bot.fetch_user(discord_id)
            except discord.HTTPException:
                return

        try:
            await user.send(
                embed=build_embed(
                    "Compte lié ✅",
                    f"Ton compte Twitch **{twitch_username}** est maintenant lié à ton compte Discord.",
                    SUCCESS_COLOR,
                )
            )
        except discord.Forbidden:
            return


async def main() -> None:
    twitch_bot = TwitchBot()
    await asyncio.gather(discord_bot.start(DISCORD_TOKEN), twitch_bot.start())


if __name__ == "__main__":
    asyncio.run(main())
