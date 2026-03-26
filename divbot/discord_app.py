import time

import discord
from discord import app_commands
from discord.ext import commands as discord_commands

from divbot.common import (
    ALLOWED_RULE_TYPES,
    CODE_EXPIRATION,
    DISCORD_TOKEN,
    ERROR_COLOR,
    GUILD_ID,
    INFO_COLOR,
    SUCCESS_COLOR,
    WARNING_COLOR,
    cleanup_expired_codes,
    config,
    generate_code,
    links,
    pending_codes,
    remove_pending_codes_for_discord_user,
    save_config,
    save_links,
    save_teams,
    teams,
    unlink_discord_user,
)
from divbot.team_logic import (
    build_embed,
    current_team_member_limit,
    format_rules,
    get_team_entry_by_role,
    leaderboard_embed,
    set_team_limit,
    team_detail_embed,
    team_member_limit_label,
    team_overview_embed,
)


async def send_ctx_embed(ctx: discord_commands.Context, title: str, description: str, color: discord.Color) -> None:
    await ctx.send(embed=build_embed(title, description, color))


async def send_interaction_embed(
    interaction: discord.Interaction,
    title: str,
    description: str,
    color: discord.Color,
    *,
    ephemeral: bool = False,
    view: discord.ui.View | None = None,
) -> None:
    embed = build_embed(title, description, color)
    send_kwargs: dict[str, object] = {"embed": embed, "ephemeral": ephemeral}
    if view is not None:
        send_kwargs["view"] = view

    if interaction.response.is_done():
        await interaction.followup.send(**send_kwargs)
        return
    await interaction.response.send_message(**send_kwargs)


def is_discord_moderator(interaction: discord.Interaction) -> bool:
    member = interaction.user
    if not isinstance(member, discord.Member):
        return False

    permissions = member.guild_permissions
    return any(
        (
            permissions.administrator,
            permissions.manage_guild,
            permissions.manage_roles,
            permissions.moderate_members,
            permissions.kick_members,
            permissions.ban_members,
        )
    )


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

    async def on_member_update(self, before: discord.Member, after: discord.Member) -> None:
        if before.roles == after.roles:
            return
        await enforce_team_limit_for_member(after)


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
                "➡️ Utilise le bouton **Copier la commande** pour obtenir un message prêt à copier sur mobile.\n"
                f"⏱️ Le code expire dans **{CODE_EXPIRATION} secondes**."
            ),
            INFO_COLOR,
            ephemeral=True,
            view=LinkCodeView(code),
        )


class LinkCodeView(discord.ui.View):
    def __init__(self, code: str) -> None:
        super().__init__(timeout=CODE_EXPIRATION)
        self.command = f"!link {code}"

    @discord.ui.button(
        label="Copier la commande",
        style=discord.ButtonStyle.secondary,
        emoji="📋",
    )
    async def copy_link_command(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await interaction.response.send_message(
            self.command,
            ephemeral=True,
        )


discord_bot = DiscordBot()


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


async def enforce_team_limit_for_member(member: discord.Member) -> None:
    guild = member.guild
    limit = current_team_member_limit()
    if limit <= 0:
        return

    member_team_roles = []
    for role in member.roles:
        team_entry = get_team_entry_by_role(role)
        if team_entry is not None:
            member_team_roles.append((role, team_entry[1]))

    if not member_team_roles:
        return

    for role, _team_data in member_team_roles:
        if len(role.members) <= limit:
            continue

        try:
            await member.remove_roles(role, reason=f"Limite de {limit} membre(s) pour les teams atteinte")
        except discord.HTTPException:
            continue

        alert_channel = guild.system_channel
        if alert_channel is None:
            continue
        await alert_channel.send(
            embed=build_embed(
                "Limite de team atteinte",
                (
                    f"{member.mention} n'a pas pu rejoindre **{role.name}** : "
                    f"la limite est fixée à **{limit}** membre(s)."
                ),
                WARNING_COLOR,
            )
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

    await ctx.send(embed=team_detail_embed(ctx.guild, role)

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
@app_commands.check(is_discord_moderator)
async def slash_linkpanel(interaction: discord.Interaction) -> None:
    if interaction.channel is None:
        await send_interaction_embed(interaction, "Erreur", "Salon introuvable.", ERROR_COLOR, ephemeral=True)
        return

    embed = build_embed(
        "Connexion Discord ↔ Twitch",
        (
            "Clique sur le bouton ci-dessous pour obtenir un **code privé**.\n"
            "Ensuite, envoie ce code dans le chat Twitch avec `!link CODE`.\n\n"
            "Exemple : `!link ABC123`\n"
            "🟣 Twitch : https://www.twitch.tv/joykix"
        ),
        INFO_COLOR,
    )
    await interaction.channel.send(embed=embed, view=LinkAccountView())
    await send_interaction_embed(interaction, "Panel envoyé", "Le message de liaison a été publié.", SUCCESS_COLOR, ephemeral=True)


@discord_bot.tree.command(name="addrule", description="Ajouter une règle Twitch vers un rôle Discord", guild=guild_object)
@app_commands.describe(trigger_type="contains ou emote", value="Mot-clé ou emote", role_name="Nom exact du rôle à donner")
@app_commands.check(is_discord_moderator)
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
@app_commands.check(is_discord_moderator)
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
@app_commands.describe(role="Rôle représentant l'équipe", emoji="Emoji affiché comme blason", motto="Devise de l'équipe (optionnel)")
@app_commands.check(is_discord_moderator)
async def slash_createteam(interaction: discord.Interaction, role: discord.Role, emoji: str, motto: str = "") -> None:
    name = role.name.lower()
    if name in teams["teams"]:
        await send_interaction_embed(interaction, "Équipe existante", "Cette équipe existe déjà.", ERROR_COLOR, ephemeral=True)
        return

    teams["teams"][name] = {
        "role_id": role.id,
        "points": 0,
        "emoji": emoji,
        "wins": 0,
        "losses": 0,
        "captain_id": None,
        "vice_captain_id": None,
        "motto": motto.strip(),
    }
    save_teams()
    await send_interaction_embed(interaction, "Équipe créée", f"Nouvelle équipe : {emoji} **{role.name}**.", SUCCESS_COLOR)


@discord_bot.tree.command(name="deleteteam", description="Supprimer une équipe", guild=guild_object)
@app_commands.describe(role="Rôle représentant l'équipe à supprimer")
@app_commands.check(is_discord_moderator)
async def slash_deleteteam(interaction: discord.Interaction, role: discord.Role) -> None:
    name = role.name.lower()
    if name not in teams["teams"]:
        await send_interaction_embed(interaction, "Équipe introuvable", "Cette équipe n'existe pas.", ERROR_COLOR, ephemeral=True)
        return

    del teams["teams"][name]
    save_teams()
    await send_interaction_embed(interaction, "Équipe supprimée", f"L'équipe **{role.name}** a été supprimée.", SUCCESS_COLOR)


@discord_bot.tree.command(name="editteam", description="Modifier une équipe", guild=guild_object)
@app_commands.describe(
    role="Rôle représentant l'équipe",
    emoji="Nouvel emoji (optionnel)",
    motto="Nouvelle devise (optionnel)",
)
@app_commands.check(is_discord_moderator)
async def slash_editteam(
    interaction: discord.Interaction,
    role: discord.Role,
    emoji: str | None = None,
    motto: str | None = None,
) -> None:
    name = role.name.lower()
    if name not in teams["teams"]:
        await send_interaction_embed(interaction, "Équipe introuvable", "Cette équipe n'existe pas.", ERROR_COLOR, ephemeral=True)
        return

    if emoji is None and motto is None:
        await send_interaction_embed(
            interaction,
            "Aucune modification",
            "Tu dois fournir au moins un champ à modifier (emoji et/ou devise).",
            ERROR_COLOR,
            ephemeral=True,
        )
        return

    team_data = teams["teams"][name]
    updates: list[str] = []

    if emoji is not None:
        cleaned_emoji = emoji.strip()
        if not cleaned_emoji:
            await send_interaction_embed(interaction, "Emoji invalide", "L'emoji ne peut pas être vide.", ERROR_COLOR, ephemeral=True)
            return
        team_data["emoji"] = cleaned_emoji
        updates.append(f"Emoji → {cleaned_emoji}")

    if motto is not None:
        cleaned_motto = motto.strip()
        if len(cleaned_motto) > 120:
            await send_interaction_embed(
                interaction,
                "Devise trop longue",
                "La devise doit contenir au maximum 120 caractères.",
                ERROR_COLOR,
                ephemeral=True,
            )
            return
        team_data["motto"] = cleaned_motto
        motto_display = cleaned_motto if cleaned_motto else "Aucune devise"
        updates.append(f"Devise → *{motto_display}*")

    save_teams()
    await send_interaction_embed(
        interaction,
        "Équipe modifiée",
        f"**{role.name}**\n" + "\n".join(f"• {update}" for update in updates),
        SUCCESS_COLOR,
    )


@discord_bot.tree.command(name="setteammotto", description="Définir la devise d'une équipe", guild=guild_object)
@app_commands.describe(role="Rôle représentant l'équipe", motto="Nouvelle devise")
@app_commands.check(is_discord_moderator)
async def slash_setteammotto(interaction: discord.Interaction, role: discord.Role, motto: str) -> None:
    name = role.name.lower()
    if name not in teams["teams"]:
        await send_interaction_embed(interaction, "Équipe introuvable", "Cette équipe n'existe pas.", ERROR_COLOR, ephemeral=True)
        return

    cleaned_motto = motto.strip()
    if len(cleaned_motto) > 120:
        await send_interaction_embed(
            interaction,
            "Devise trop longue",
            "La devise doit contenir au maximum 120 caractères.",
            ERROR_COLOR,
            ephemeral=True,
        )
        return

    teams["teams"][name]["motto"] = cleaned_motto
    save_teams()
    motto_display = cleaned_motto if cleaned_motto else "Aucune devise"
    await send_interaction_embed(
        interaction,
        "Devise mise à jour",
        f"**{role.name}** → *{motto_display}*",
        SUCCESS_COLOR,
    )


@discord_bot.tree.command(name="addpoints", description="Ajouter des points à une équipe", guild=guild_object)
@app_commands.describe(role="Rôle de l'équipe", amount="Nombre de points à ajouter")
@app_commands.check(is_discord_moderator)
async def slash_addpoints(interaction: discord.Interaction, role: discord.Role, amount: int) -> None:
    name = role.name.lower()
    if name not in teams["teams"]:
        await send_interaction_embed(interaction, "Équipe introuvable", "Cette équipe n'existe pas.", ERROR_COLOR, ephemeral=True)
        return

    teams["teams"][name]["points"] += amount
    save_teams()
    await send_interaction_embed(interaction, "Points ajoutés", f"**{role.name}** reçoit **{amount}** point(s).", SUCCESS_COLOR)


@discord_bot.tree.command(name="teamlimit", description="Définir la limite max de membres par team", guild=guild_object)
@app_commands.describe(limit="0 = illimité, sinon nombre max de membres par team")
@app_commands.check(is_discord_moderator)
async def slash_teamlimit(interaction: discord.Interaction, limit: int) -> None:
    if limit < 0:
        await send_interaction_embed(
            interaction,
            "Valeur invalide",
            "La limite doit être supérieure ou égale à 0.",
            ERROR_COLOR,
            ephemeral=True,
        )
        return

    set_team_limit(limit)
    await send_interaction_embed(
        interaction,
        "Limite mise à jour",
        f"Nouvelle limite de membres par team : **{team_member_limit_label()}**.",
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


@discord_bot.tree.command(name="setcaptain", description="Définir le capitaine d'une team", guild=guild_object)
@app_commands.describe(role="Rôle de la team", member="Membre à nommer capitaine")
@app_commands.check(is_discord_moderator)
async def slash_setcaptain(interaction: discord.Interaction, role: discord.Role, member: discord.Member) -> None:
    team_entry = get_team_entry_by_role(role)
    if team_entry is None:
        await send_interaction_embed(interaction, "Équipe introuvable", "Cette équipe n'est pas enregistrée.", ERROR_COLOR, ephemeral=True)
        return

    if role not in member.roles:
        await send_interaction_embed(interaction, "Membre invalide", "Le capitaine doit être membre de cette team.", ERROR_COLOR, ephemeral=True)
        return

    _team_name, team_data = team_entry
    team_data["captain_id"] = member.id
    if team_data.get("vice_captain_id") == member.id:
        team_data["vice_captain_id"] = None
    save_teams()
    await send_interaction_embed(interaction, "Capitaine défini", f"{member.mention} est maintenant capitaine de **{role.name}**.", SUCCESS_COLOR)


@discord_bot.tree.command(name="setvicecaptain", description="Définir le vice-capitaine de ta team", guild=guild_object)
@app_commands.describe(role="Rôle de la team", member="Membre à nommer vice-capitaine")
async def slash_setvicecaptain(interaction: discord.Interaction, role: discord.Role, member: discord.Member) -> None:
    team_entry = get_team_entry_by_role(role)
    if team_entry is None:
        await send_interaction_embed(interaction, "Équipe introuvable", "Cette équipe n'est pas enregistrée.", ERROR_COLOR, ephemeral=True)
        return

    if not isinstance(interaction.user, discord.Member):
        await send_interaction_embed(interaction, "Erreur", "Commande disponible uniquement sur le serveur.", ERROR_COLOR, ephemeral=True)
        return

    _team_name, team_data = team_entry
    captain_id = team_data.get("captain_id")
    if captain_id is None:
        await send_interaction_embed(interaction, "Capitaine absent", "Aucun capitaine n'est défini pour cette team.", ERROR_COLOR, ephemeral=True)
        return

    if interaction.user.id != captain_id:
        await send_interaction_embed(interaction, "Permission refusée", "Seul le capitaine de cette team peut nommer un vice-capitaine.", ERROR_COLOR, ephemeral=True)
        return

    if role not in member.roles:
        await send_interaction_embed(interaction, "Membre invalide", "Le vice-capitaine doit être membre de cette team.", ERROR_COLOR, ephemeral=True)
        return

    if member.id == captain_id:
        await send_interaction_embed(interaction, "Membre invalide", "Le capitaine ne peut pas être son propre vice-capitaine.", ERROR_COLOR, ephemeral=True)
        return

    team_data["vice_captain_id"] = member.id
    save_teams()
    await send_interaction_embed(
        interaction,
        "Vice-capitaine défini",
        f"{member.mention} est maintenant vice-capitaine de **{role.name}**.",
        SUCCESS_COLOR,
    )


@slash_addrule.error
@slash_linkpanel.error
@slash_delrule.error
@slash_createteam.error
@slash_deleteteam.error
@slash_editteam.error
@slash_addpoints.error
@slash_teamlimit.error
@slash_setcaptain.error
@slash_setteammotto.error
async def admin_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
    if isinstance(error, (app_commands.MissingPermissions, app_commands.CheckFailure)):
        await send_interaction_embed(interaction, "Permission refusée", "Tu n'as pas la permission d'utiliser cette commande.", ERROR_COLOR, ephemeral=True)
        return
    raise error


async def start_discord_bot() -> None:
    await discord_bot.start(DISCORD_TOKEN)
