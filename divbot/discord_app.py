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
    register_team_update_callback,
    save_config,
    save_links,
    save_teams,
    teams,
    unlink_discord_user,
)
from divbot.team_logic import (
    build_embed,
    current_team_member_limit,
    get_team_entry_by_role,
    leaderboard_embed,
    set_team_limit,
    team_detail_embed,
    team_member_limit_label,
    team_overview_embed,
)



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
link_group = app_commands.Group(name="link", description="Commandes de liaison Twitch ↔ Discord")
rule_group = app_commands.Group(name="rule", description="Commandes de gestion des règles Twitch")
team_group = app_commands.Group(name="team", description="Commandes de gestion des équipes")


class DiscordBot(discord_commands.Bot):
    def __init__(self) -> None:
        super().__init__(command_prefix="!", intents=intents)
        self.synced = False

    async def setup_hook(self) -> None:
        self.tree.copy_global_to(guild=guild_object)
        self.tree.add_command(link_group, guild=guild_object)
        self.tree.add_command(rule_group, guild=guild_object)
        self.tree.add_command(team_group, guild=guild_object)
        self.add_view(LinkAccountView())

    async def on_ready(self) -> None:
        if not self.synced:
            synced_commands = await self.tree.sync(guild=guild_object)
            print(f"[DISCORD] {len(synced_commands)} commandes slash synchronisées sur {GUILD_ID}")
            self.synced = True
        print(f"[DISCORD] Connecté : {self.user}")

    async def on_member_update(self, before: discord.Member, after: discord.Member) -> None:
        if before.roles == after.roles:
            return
        await enforce_team_limit_for_member(after)
        if team_role_ids_for_member(before) != team_role_ids_for_member(after):
            await refresh_registered_leaderboards()

    async def on_member_remove(self, member: discord.Member) -> None:
        if team_role_ids_for_member(member):
            await refresh_registered_leaderboards()


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

    @discord.ui.button(
        label="Comment rejoindre une division",
        style=discord.ButtonStyle.secondary,
        emoji="🧭",
        custom_id="how_to_join_division",
        row=1,
    )
    async def how_to_join_division_button(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await send_interaction_embed(
            interaction,
            "Comment rejoindre une division",
            (
                "Pour rejoindre une division (**Les malicieux**, **Les audacieux** ou **Les radieux**) :\n\n"
                "1. Appuie sur **🔗 Link Discord ↔ Twitch**.\n"
                "2. Copie la commande générée, puis envoie-la dans le chat Twitch.\n"
                "3. Envoie un deuxième message avec l'emote de la division souhaitée :\n"
                "   • `<:Les_Malicieux:1484997361569890416>`\n"
                "   • `<:Les_Audacieux:1484997333740683417>`\n"
                "   • `<:Les_Radieux:1484997282951594095>`\n\n"
                "✅ Une fois fait, tu rejoins ta division et son salon dédié s'ouvre pour toi."
            ),
            INFO_COLOR,
            ephemeral=True,
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
leaderboard_messages: dict[int, discord.Message] = {}


async def refresh_registered_leaderboards() -> None:
    for message_id, message in list(leaderboard_messages.items()):
        guild = message.guild
        if guild is None:
            leaderboard_messages.pop(message_id, None)
            continue
        try:
            await message.edit(embed=leaderboard_embed(guild))
        except (discord.NotFound, discord.Forbidden):
            leaderboard_messages.pop(message_id, None)
        except discord.HTTPException:
            continue


register_team_update_callback(refresh_registered_leaderboards)


def register_leaderboard_message(message: discord.Message) -> None:
    leaderboard_messages[message.id] = message


async def handle_unlink_request(interaction: discord.Interaction) -> None:
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


async def handle_linkpanel_request(interaction: discord.Interaction) -> None:
    if interaction.channel is None:
        await send_interaction_embed(interaction, "Erreur", "Salon introuvable.", ERROR_COLOR, ephemeral=True)
        return

    embed = build_embed(
        "Connexion Discord ↔ Twitch",
        (
            "Clique sur le bouton ci-dessous pour obtenir un **code privé**.\n"
            "Ensuite, envoie ce code dans le chat Twitch avec `!link CODE`.\n\n"
            "Exemple : `!link ABC123`\n"
            "Besoin d'aide pour choisir une division ? Utilise le bouton **Comment rejoindre une division**.\n"
            "🟣 Twitch : https://www.twitch.tv/zogaa_"
        ),
        INFO_COLOR,
    )
    await interaction.channel.send(embed=embed, view=LinkAccountView())
    await send_interaction_embed(interaction, "Panel envoyé", "Le message de liaison a été publié.", SUCCESS_COLOR, ephemeral=True)


def build_rules_embed() -> discord.Embed:
    embed = build_embed(
        "📜 Règles Twitch → Discord",
        "Gère tes règles avec `/rule add` et `/rule remove`.",
        INFO_COLOR,
    )
    if not config["rules"]:
        embed.add_field(name="Aucune règle", value="Utilise `/rule add` pour créer la première règle.", inline=False)
        return embed

    for index, rule in enumerate(config["rules"]):
        embed.add_field(
            name=f"#{index} • {rule['type']}",
            value=f"Déclencheur : `{rule['value']}`\nRôle attribué : **{rule['role']}**",
            inline=False,
        )
    return embed


async def handle_addrule_request(interaction: discord.Interaction, trigger_type: str, value: str, role: discord.Role) -> None:
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

    cleaned_value = value.strip()
    if not cleaned_value:
        await send_interaction_embed(interaction, "Valeur invalide", "Le mot-clé ou l'emote ne peut pas être vide.", ERROR_COLOR, ephemeral=True)
        return

    role_name = role.name
    duplicate_rule = next(
        (
            rule
            for rule in config["rules"]
            if rule["type"] == trigger_type and rule["value"].lower() == cleaned_value.lower() and rule["role"] == role_name
        ),
        None,
    )
    if duplicate_rule is not None:
        await send_interaction_embed(
            interaction,
            "Règle déjà existante",
            "Cette règle existe déjà, aucune modification appliquée.",
            WARNING_COLOR,
            ephemeral=True,
        )
        return

    config["rules"].append({"type": trigger_type, "value": cleaned_value, "action": "give_role", "role": role_name})
    save_config()

    embed = build_embed("✅ Règle ajoutée", "La règle a bien été enregistrée.", SUCCESS_COLOR)
    embed.add_field(name="Type", value=f"`{trigger_type}`", inline=True)
    embed.add_field(name="Déclencheur", value=f"`{cleaned_value}`", inline=True)
    embed.add_field(name="Rôle", value=role.mention, inline=True)
    embed.set_footer(text=f"Total de règles : {len(config['rules'])}")
    if interaction.response.is_done():
        await interaction.followup.send(embed=embed, ephemeral=True)
        return
    await interaction.response.send_message(embed=embed, ephemeral=True)


async def handle_delrule_request(interaction: discord.Interaction, index: int) -> None:
    try:
        removed = config["rules"].pop(index)
    except IndexError:
        await send_interaction_embed(interaction, "Index invalide", "Aucune règle ne correspond à cet index.", ERROR_COLOR, ephemeral=True)
        return

    save_config()
    embed = build_embed("🗑️ Règle supprimée", "La règle a été retirée de la configuration.", SUCCESS_COLOR)
    embed.add_field(name="Type", value=f"`{removed['type']}`", inline=True)
    embed.add_field(name="Déclencheur", value=f"`{removed['value']}`", inline=True)
    embed.add_field(name="Rôle", value=f"**{removed['role']}**", inline=True)
    embed.set_footer(text=f"Règles restantes : {len(config['rules'])}")
    if interaction.response.is_done():
        await interaction.followup.send(embed=embed, ephemeral=True)
        return
    await interaction.response.send_message(embed=embed, ephemeral=True)


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


def team_role_ids_for_member(member: discord.Member) -> set[int]:
    return {
        role.id
        for role in member.roles
        if get_team_entry_by_role(role) is not None
    }


@link_group.command(name="remove", description="Supprimer la liaison avec ton compte Twitch")
async def link_remove(interaction: discord.Interaction) -> None:
    await handle_unlink_request(interaction)


@link_group.command(name="panel", description="Publier l'embed avec bouton de liaison Discord ↔ Twitch")
@app_commands.check(is_discord_moderator)
async def link_panel(interaction: discord.Interaction) -> None:
    await handle_linkpanel_request(interaction)


@rule_group.command(name="list", description="Afficher les règles configurées")
async def rule_list(interaction: discord.Interaction) -> None:
    embed = build_rules_embed()
    if interaction.response.is_done():
        await interaction.followup.send(embed=embed, ephemeral=True)
        return
    await interaction.response.send_message(embed=embed, ephemeral=True)


@rule_group.command(name="add", description="Ajouter une règle Twitch vers un rôle Discord")
@app_commands.describe(trigger_type="contains ou emote", value="Mot-clé ou emote", role="Rôle Discord à attribuer")
@app_commands.check(is_discord_moderator)
async def rule_add(interaction: discord.Interaction, trigger_type: str, value: str, role: discord.Role) -> None:
    await handle_addrule_request(interaction, trigger_type, value, role)


async def rule_remove_index_autocomplete(
    interaction: discord.Interaction,
    current: str,
) -> list[app_commands.Choice[int]]:
    del interaction
    search = current.lower().strip()
    choices: list[app_commands.Choice[int]] = []
    for index, rule in enumerate(config["rules"]):
        label = f"#{index} • {rule['type']}:{rule['value']} -> {rule['role']}"
        if search and search not in label.lower():
            continue
        choices.append(app_commands.Choice(name=label[:100], value=index))
        if len(choices) >= 25:
            break
    return choices


@rule_group.command(name="remove", description="Supprimer une règle par son index")
@app_commands.describe(index="Index visible dans /rule list")
@app_commands.autocomplete(index=rule_remove_index_autocomplete)
@app_commands.check(is_discord_moderator)
async def rule_remove(interaction: discord.Interaction, index: int) -> None:
    await handle_delrule_request(interaction, index)


@team_group.command(name="create", description="Créer une équipe à partir d'un rôle Discord")
@app_commands.describe(role="Rôle représentant l'équipe", emoji="Emoji affiché comme blason", motto="Devise de l'équipe (optionnel)")
@app_commands.check(is_discord_moderator)
async def team_create(interaction: discord.Interaction, role: discord.Role, emoji: str, motto: str = "") -> None:
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


@team_group.command(name="delete", description="Supprimer une équipe")
@app_commands.describe(role="Rôle représentant l'équipe à supprimer")
@app_commands.check(is_discord_moderator)
async def team_delete(interaction: discord.Interaction, role: discord.Role) -> None:
    name = role.name.lower()
    if name not in teams["teams"]:
        await send_interaction_embed(interaction, "Équipe introuvable", "Cette équipe n'existe pas.", ERROR_COLOR, ephemeral=True)
        return

    del teams["teams"][name]
    save_teams()
    await send_interaction_embed(interaction, "Équipe supprimée", f"L'équipe **{role.name}** a été supprimée.", SUCCESS_COLOR)


@team_group.command(name="edit", description="Modifier une équipe")
@app_commands.describe(
    role="Rôle représentant l'équipe",
    emoji="Nouvel emoji (optionnel)",
    motto="Nouvelle devise (optionnel)",
)
@app_commands.check(is_discord_moderator)
async def team_edit(
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


@team_group.command(name="motto", description="Définir la devise d'une équipe")
@app_commands.describe(role="Rôle représentant l'équipe", motto="Nouvelle devise")
@app_commands.check(is_discord_moderator)
async def team_motto(interaction: discord.Interaction, role: discord.Role, motto: str) -> None:
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


@team_group.command(name="points", description="Ajouter des points à une équipe")
@app_commands.describe(role="Rôle de l'équipe", amount="Nombre de points à ajouter")
@app_commands.check(is_discord_moderator)
async def team_points(interaction: discord.Interaction, role: discord.Role, amount: int) -> None:
    name = role.name.lower()
    if name not in teams["teams"]:
        await send_interaction_embed(interaction, "Équipe introuvable", "Cette équipe n'existe pas.", ERROR_COLOR, ephemeral=True)
        return

    teams["teams"][name]["points"] += amount
    save_teams()
    await send_interaction_embed(interaction, "Points ajoutés", f"**{role.name}** reçoit **{amount}** point(s).", SUCCESS_COLOR)


@team_group.command(name="record", description="Modifier le nombre de victoires et/ou défaites d'une équipe")
@app_commands.describe(
    role="Rôle de l'équipe",
    wins="Nouveau total de victoires (optionnel)",
    losses="Nouveau total de défaites (optionnel)",
)
@app_commands.check(is_discord_moderator)
async def team_record(
    interaction: discord.Interaction,
    role: discord.Role,
    wins: int | None = None,
    losses: int | None = None,
) -> None:
    name = role.name.lower()
    if name not in teams["teams"]:
        await send_interaction_embed(interaction, "Équipe introuvable", "Cette équipe n'existe pas.", ERROR_COLOR, ephemeral=True)
        return

    if wins is None and losses is None:
        await send_interaction_embed(
            interaction,
            "Aucune modification",
            "Tu dois renseigner `wins`, `losses`, ou les deux.",
            ERROR_COLOR,
            ephemeral=True,
        )
        return

    if wins is not None and wins < 0:
        await send_interaction_embed(interaction, "Valeur invalide", "Le nombre de victoires doit être supérieur ou égal à 0.", ERROR_COLOR, ephemeral=True)
        return

    if losses is not None and losses < 0:
        await send_interaction_embed(interaction, "Valeur invalide", "Le nombre de défaites doit être supérieur ou égal à 0.", ERROR_COLOR, ephemeral=True)
        return

    team_data = teams["teams"][name]
    updates: list[str] = []

    if wins is not None:
        team_data["wins"] = wins
        updates.append(f"Victoires → **{wins}**")

    if losses is not None:
        team_data["losses"] = losses
        updates.append(f"Défaites → **{losses}**")

    save_teams()
    await send_interaction_embed(
        interaction,
        "Bilan mis à jour",
        f"**{role.name}**\n" + "\n".join(f"• {update}" for update in updates),
        SUCCESS_COLOR,
    )


@team_group.command(name="reset", description="Réinitialiser une équipe (ou toutes si aucun rôle n'est indiqué)")
@app_commands.describe(role="Rôle de l'équipe (optionnel : vide pour tout réinitialiser)")
@app_commands.check(is_discord_moderator)
async def team_reset(interaction: discord.Interaction, role: discord.Role | None = None) -> None:
    if role is None:
        if not teams["teams"]:
            await send_interaction_embed(
                interaction,
                "Aucune équipe",
                "Aucune équipe n'est configurée à réinitialiser.",
                WARNING_COLOR,
                ephemeral=True,
            )
            return

        for team_data in teams["teams"].values():
            team_data["points"] = 0
            team_data["wins"] = 0
            team_data["losses"] = 0
        save_teams()
        await send_interaction_embed(
            interaction,
            "Statistiques réinitialisées",
            "Toutes les équipes ont maintenant **0 point**, **0 victoire** et **0 défaite**.",
            SUCCESS_COLOR,
        )
        return

    name = role.name.lower()
    if name not in teams["teams"]:
        await send_interaction_embed(interaction, "Équipe introuvable", "Cette équipe n'existe pas.", ERROR_COLOR, ephemeral=True)
        return

    team_data = teams["teams"][name]
    team_data["points"] = 0
    team_data["wins"] = 0
    team_data["losses"] = 0
    save_teams()
    await send_interaction_embed(
        interaction,
        "Statistiques réinitialisées",
        f"**{role.name}** a maintenant **0 point**, **0 victoire** et **0 défaite**.",
        SUCCESS_COLOR,
    )


@team_group.command(name="limit", description="Définir la limite max de membres par team")
@app_commands.describe(limit="0 = illimité, sinon nombre max de membres par team")
@app_commands.check(is_discord_moderator)
async def team_limit(interaction: discord.Interaction, limit: int) -> None:
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


@team_group.command(name="leaderboard", description="Afficher le classement des équipes")
async def team_leaderboard(interaction: discord.Interaction) -> None:
    guild = interaction.guild
    if guild is None:
        await send_interaction_embed(interaction, "Erreur", "Cette commande doit être utilisée dans le serveur.", ERROR_COLOR, ephemeral=True)
        return

    embed = leaderboard_embed(guild)
    if interaction.response.is_done():
        sent_message = await interaction.followup.send(embed=embed, wait=True)
    else:
        await interaction.response.send_message(embed=embed)
        sent_message = await interaction.original_response()

    register_leaderboard_message(sent_message)


@team_group.command(name="list", description="Afficher les membres et les statistiques des équipes")
async def team_list(interaction: discord.Interaction) -> None:
    guild = interaction.guild
    if guild is None:
        await send_interaction_embed(interaction, "Erreur", "Cette commande doit être utilisée dans le serveur.", ERROR_COLOR, ephemeral=True)
        return

    embed = team_overview_embed(guild)
    if interaction.response.is_done():
        await interaction.followup.send(embed=embed)
        return
    await interaction.response.send_message(embed=embed)


@team_group.command(name="detail", description="Voir le détail d'une team et ses membres")
@app_commands.describe(role="Rôle de la team (ex: @NomDeLaTeam)")
async def team_detail(interaction: discord.Interaction, role: discord.Role) -> None:
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


@team_group.command(name="captain", description="Définir le capitaine d'une team")
@app_commands.describe(role="Rôle de la team", member="Membre à nommer capitaine")
@app_commands.check(is_discord_moderator)
async def team_captain(interaction: discord.Interaction, role: discord.Role, member: discord.Member) -> None:
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


@team_group.command(name="vicecaptain", description="Définir le vice-capitaine de ta team")
@app_commands.describe(role="Rôle de la team", member="Membre à nommer vice-capitaine")
async def team_vicecaptain(interaction: discord.Interaction, role: discord.Role, member: discord.Member) -> None:
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


@team_create.error
@team_delete.error
@team_edit.error
@team_points.error
@team_record.error
@team_reset.error
@team_limit.error
@team_captain.error
@team_motto.error
@link_panel.error
@rule_add.error
@rule_remove.error
async def admin_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
    if isinstance(error, (app_commands.MissingPermissions, app_commands.CheckFailure)):
        await send_interaction_embed(interaction, "Permission refusée", "Tu n'as pas la permission d'utiliser cette commande.", ERROR_COLOR, ephemeral=True)
        return
    raise error


async def start_discord_bot() -> None:
    await discord_bot.start(DISCORD_TOKEN)
