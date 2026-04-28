import asyncio
import random
import re
import time
from datetime import datetime, timezone
from typing import Awaitable, Callable, Literal, TypeVar

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
    save_data,
    save_config,
    save_links,
    save_teams,
    teams,
    load_data,
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
from division_war import DivisionWarSystem

API_RETRY_BASE_DELAY_SECONDS = 1.0
API_RETRY_ATTEMPTS = 4
LEADERBOARD_REFRESH_DEBOUNCE_SECONDS = 2.0

_T = TypeVar("_T")


async def run_discord_request(
    request_factory: Callable[[], Awaitable[_T]],
    *,
    max_attempts: int = API_RETRY_ATTEMPTS,
) -> _T:
    """Run a Discord API request with retry/backoff on 429 responses only."""
    for attempt in range(1, max_attempts + 1):
        try:
            return await request_factory()
        except discord.HTTPException as error:
            if error.status != 429 or attempt >= max_attempts:
                raise
            retry_after = float(getattr(error, "retry_after", 0) or 0)
            delay = retry_after if retry_after > 0 else API_RETRY_BASE_DELAY_SECONDS * (2 ** (attempt - 1))
            await asyncio.sleep(delay)

    raise RuntimeError("Discord request retry loop exhausted unexpectedly")



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


def _division_war_member_label(guild: discord.Guild, user_id: int) -> str:
    member = guild.get_member(user_id)
    if member is None:
        return f"Utilisateur {user_id}"
    return member.display_name


def _build_divwar_embed(
    *,
    title: str,
    summary_lines: list[str],
    combat_lines: list[str],
    status_line: str | None = None,
) -> discord.Embed:
    lines = list(summary_lines)
    if status_line:
        lines.append(status_line)
    if combat_lines:
        lines.append("")
        lines.append("**Résumé du combat**")
        lines.extend(f"• {line}" for line in combat_lines)
    return build_embed(title, "\n".join(lines), INFO_COLOR)


async def _animate_division_war_message(
    *,
    message: discord.Message,
    summary_lines: list[str],
    duel_log: list[str],
) -> None:
    if not duel_log:
        await message.edit(embed=_build_divwar_embed(title="⚔️ Résultat du duel de divisions", summary_lines=summary_lines, combat_lines=[]))
        return

    step_delay_seconds = 1.1
    max_lines_displayed = 14
    updates: list[list[str]] = []
    current_block: list[str] = []
    for line in duel_log:
        if line.startswith("Round ") and current_block:
            updates.append(current_block)
            current_block = [line]
            continue
        current_block.append(line)
    if current_block:
        updates.append(current_block)

    streamed_lines: list[str] = []
    for index, update_block in enumerate(updates, start=1):
        streamed_lines.extend(update_block)
        current_preview = streamed_lines[-max_lines_displayed:]
        in_progress_embed = _build_divwar_embed(
            title="⚔️ Duel de divisions en direct",
            summary_lines=summary_lines,
            combat_lines=current_preview,
            status_line=f"🟡 **Combat en cours** • Round `{index}/{len(updates)}`",
        )
        await message.edit(embed=in_progress_embed)
        await asyncio.sleep(step_delay_seconds)

    final_preview = streamed_lines[-max_lines_displayed:]
    if len(streamed_lines) > max_lines_displayed:
        final_preview.append(f"... ({len(streamed_lines) - max_lines_displayed} événement(s) supplémentaires)")
    final_embed = _build_divwar_embed(
        title="⚔️ Résultat du duel de divisions",
        summary_lines=summary_lines,
        combat_lines=final_preview,
        status_line="✅ **Combat terminé**",
    )
    await message.edit(embed=final_embed)


def division_power_for_role(guild: discord.Guild, role_id: int) -> float:
    role = guild.get_role(role_id)
    role_name = role.name if role is not None else None
    division_profile = discord_bot.division_war.build_division_profile(role_id, role_name)
    return division_profile.division_power


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
        self.team_spam_release_task: asyncio.Task[None] | None = None
        self.leaderboard_refresh_task: asyncio.Task[None] | None = None
        self._guild_cache: dict[int, discord.Guild] = {}
        self._text_channel_cache: dict[int, discord.TextChannel] = {}
        self.division_war = DivisionWarSystem()
        self._team_profiles_initialized = False

    def get_cached_guild(self, guild_id: int) -> discord.Guild | None:
        # Optimization: re-use cached guild objects to avoid repeated resolver work.
        guild = self._guild_cache.get(guild_id)
        if guild is None:
            guild = self.get_guild(guild_id)
            if guild is not None:
                self._guild_cache[guild_id] = guild
        return guild

    def get_cached_text_channel(self, channel_id: int) -> discord.TextChannel | None:
        # Optimization: central channel cache to avoid duplicate lookup paths.
        channel = self._text_channel_cache.get(channel_id)
        if channel is None:
            raw_channel = self.get_channel(channel_id)
            if isinstance(raw_channel, discord.TextChannel):
                self._text_channel_cache[channel_id] = raw_channel
                channel = raw_channel
        return channel

    async def schedule_leaderboard_refresh(self) -> None:
        # Optimization: debounce bursts of member events into a single leaderboard refresh.
        if self.leaderboard_refresh_task is not None and not self.leaderboard_refresh_task.done():
            return

        async def _runner() -> None:
            await asyncio.sleep(LEADERBOARD_REFRESH_DEBOUNCE_SECONDS)
            await refresh_registered_leaderboards()

        self.leaderboard_refresh_task = asyncio.create_task(_runner())

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
        if not self._team_profiles_initialized:
            await self.initialize_team_member_profiles()
            self._team_profiles_initialized = True
        if self.team_spam_release_task is None or self.team_spam_release_task.done():
            self.team_spam_release_task = asyncio.create_task(self.release_team_spam_members_periodic())
        print(f"[DISCORD] Connecté : {self.user}")

    async def initialize_team_member_profiles(self, minimum_level: int = 0) -> tuple[int, int]:
        guild = self.get_cached_guild(GUILD_ID)
        if guild is None:
            return (0, 0)

        # Optimization: avoid a full guild chunk (can be very slow on large servers).
        # We only need members that currently have a team role.
        configured_team_role_ids = {
            team_data.get("role_id")
            for team_data in teams.get("teams", {}).values()
            if isinstance(team_data.get("role_id"), int)
        }
        team_members: dict[int, discord.Member] = {}
        for role_id in configured_team_role_ids:
            role = guild.get_role(role_id)
            if role is None:
                continue
            for member in role.members:
                team_members[member.id] = member

        # Fallback: if role caches are empty (just after startup/intents delay),
        # do a standard member scan without forcing chunk.
        members_to_process = team_members.values() if team_members else guild.members

        created_profiles = 0
        leveled_profiles = 0
        for member in members_to_process:
            division_role_id = get_primary_team_role_id(member)
            if division_role_id is None:
                continue
            existing_profile = self.division_war.get_member(member.id)
            division_member = self.division_war.get_or_create_member(member.id, division_role_id)
            if existing_profile is None:
                created_profiles += 1
            if minimum_level > 0 and division_member.level < minimum_level:
                minimum_xp = int((minimum_level ** 2) * 50)
                division_member.xp = max(division_member.xp, minimum_xp)
                self.division_war.recalculate_member_level_and_stats(division_member)
                leveled_profiles += 1

        print(
            "[DIVISION] Profils vérifiés pour les membres en team : "
            f"{created_profiles} profil(s) créé(s), {leveled_profiles} ajusté(s) niveau {minimum_level}+."
        )
        return (created_profiles, leveled_profiles)

    async def on_member_update(self, before: discord.Member, after: discord.Member) -> None:
        if before.roles == after.roles:
            return
        await enforce_manual_delinquent_punishment(before, after)
        if await enforce_delinquent_team_block(before, after):
            return
        await enforce_single_team_membership(before, after)
        await enforce_team_limit_for_member(after)
        current_member = after.guild.get_member(after.id)
        if current_member is None:
            return
        await announce_team_joins(before, current_member)
        await announce_team_departures(before, current_member)
        if team_role_ids_for_member(before) != team_role_ids_for_member(current_member):
            await self.schedule_leaderboard_refresh()

    async def on_member_remove(self, member: discord.Member) -> None:
        if team_role_ids_for_member(member):
            await self.schedule_leaderboard_refresh()

    async def on_member_join(self, member: discord.Member) -> None:
        if team_role_ids_for_member(member):
            await self.schedule_leaderboard_refresh()

    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot:
            return

        if isinstance(message.author, discord.Member):
            division_role_id = get_primary_team_role_id(message.author)
            self.division_war.handle_message(
                user_id=message.author.id,
                division_id=division_role_id,
                content=message.content,
            )

        await self.process_commands(message)

    async def release_team_spam_members_periodic(self) -> None:
        while not self.is_closed():
            await release_due_team_spam_members()
            await asyncio.sleep(TEAM_SPAM_RELEASE_POLL_SECONDS)


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
                "2. Envoie dans le chat Twitch soit :\n"
                "   • un seul message `!link CODE` suivi de l'emote de la division\n"
                "   • ou deux messages (`!link CODE` puis l'emote)\n"
                "3. Emotes de division :\n"
                "   • <:Les_Malicieux:1484997361569890416>\n"
                "   • <:Les_Audacieux:1484997333740683417>\n"
                "   • <:Les_Radieux:1484997282951594095>\n\n"
                "⚠️ Si tu en mets plusieurs, elles doivent toutes correspondre à la même division.\n\n"
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
LEADERBOARD_CHANNEL_ID = 1487174285368758354
LEADERBOARD_STATE_KEY = "leaderboard"
leaderboard_state: dict[str, dict[str, dict[str, object]]] = load_data(LEADERBOARD_STATE_KEY, {"channels": {}})
TEAM_SWITCH_ALERT_CHANNEL_ID = 1487218240647205054
DELINQUENT_ROLE_ID = 1487122699275862099
TEAM_SPAM_RESTORE_ROLE_ID = 1158378155489366106
TEAM_SWITCH_SPAM_THRESHOLD = 3
TEAM_SPAM_RESTORE_DELAY_SECONDS = 60 * 60 * 24  # 24h en production : 60 * 60 * 24
TEAM_SPAM_RELEASE_POLL_SECONDS = 30
ROULETTE_JOIN_WINDOW_SECONDS = 10
ROULETTE_PUNISHMENT_SECONDS = 60 * 10
MANUAL_DELINQUENT_PUNISHMENT_SECONDS = 60 * 60 * 24
TEAM_SPAM_PUNISHMENTS_KEY = "team_spam_punishments"
team_switch_violations: dict[int, int] = {}
team_enforcement_locks: dict[int, asyncio.Lock] = {}
roulette_russe_locks: dict[int, asyncio.Lock] = {}
team_spam_punishments: dict[str, dict[str, dict[str, object]]] = load_data(
    TEAM_SPAM_PUNISHMENTS_KEY, {"members": {}}
)
team_record_snapshot: dict[str, dict[str, int]] = {}

if not isinstance(leaderboard_state, dict):
    leaderboard_state = {"channels": {}}
if "channels" not in leaderboard_state or not isinstance(leaderboard_state["channels"], dict):
    leaderboard_state["channels"] = {}
if not isinstance(team_spam_punishments, dict):
    team_spam_punishments = {"members": {}}
if "members" not in team_spam_punishments or not isinstance(team_spam_punishments["members"], dict):
    team_spam_punishments["members"] = {}


def get_team_enforcement_lock(member_id: int) -> asyncio.Lock:
    lock = team_enforcement_locks.get(member_id)
    if lock is None:
        lock = asyncio.Lock()
        team_enforcement_locks[member_id] = lock
    return lock


def get_roulette_russe_lock(guild_id: int) -> asyncio.Lock:
    lock = roulette_russe_locks.get(guild_id)
    if lock is None:
        lock = asyncio.Lock()
        roulette_russe_locks[guild_id] = lock
    return lock


async def restore_member_after_team_spam(
    guild_id: int,
    member_id: int,
    team_role_id: int | None,
    restore_role_ids: list[int] | None = None,
) -> None:
    guild = discord_bot.get_cached_guild(guild_id)
    if guild is None:
        return

    member = guild.get_member(member_id)
    if member is None:
        return

    delinquent_role = guild.get_role(DELINQUENT_ROLE_ID)
    try:
        if delinquent_role is not None and delinquent_role in member.roles:
            await member.remove_roles(delinquent_role, reason="Fin de sanction pour spam de changement de team")

        role_ids = restore_role_ids
        if role_ids is None:
            role_ids = []
            if TEAM_SPAM_RESTORE_ROLE_ID > 0:
                role_ids.append(TEAM_SPAM_RESTORE_ROLE_ID)
            if team_role_id is not None:
                role_ids.append(team_role_id)
        roles_to_restore = []
        for role_id in role_ids:
            role = guild.get_role(role_id)
            if role is not None and role not in member.roles:
                roles_to_restore.append(role)

        if roles_to_restore:
            await member.add_roles(*roles_to_restore, reason="Fin de sanction pour spam de changement de team")
    except discord.HTTPException:
        pass


def persist_team_spam_punishments() -> None:
    save_data(TEAM_SPAM_PUNISHMENTS_KEY, team_spam_punishments)


async def apply_temporary_delinquent_punishment(
    member: discord.Member,
    *,
    duration_seconds: int,
    reason: str,
    restore_role_ids: list[int],
    source: str,
) -> bool:
    delinquent_role = member.guild.get_role(DELINQUENT_ROLE_ID)
    if delinquent_role is None:
        return False
    bot_member = member.guild.me
    if bot_member is None:
        return False
    if not member.guild.me.guild_permissions.manage_roles:
        return False
    if delinquent_role >= bot_member.top_role:
        return False

    role_ids_to_remove = set(restore_role_ids)
    roles_to_remove = [
        role
        for role in member.roles
        if role.id in role_ids_to_remove and not role.managed and role < bot_member.top_role
    ]
    try:
        for role in roles_to_remove:
            await member.remove_roles(role, reason=reason)
        if delinquent_role not in member.roles:
            await member.add_roles(delinquent_role, reason=reason)
    except discord.HTTPException:
        return False

    register_team_spam_punishment(
        member.id,
        member.guild.id,
        team_role_id=None,
        restore_role_ids=[role.id for role in roles_to_remove],
        duration_seconds=duration_seconds,
        source=source,
    )
    return True


def register_team_spam_punishment(
    member_id: int,
    guild_id: int,
    team_role_id: int | None,
    *,
    restore_role_ids: list[int] | None = None,
    duration_seconds: int = TEAM_SPAM_RESTORE_DELAY_SECONDS,
    source: str = "team_spam",
) -> None:
    now = datetime.now(timezone.utc)
    release_at = now.timestamp() + duration_seconds
    if restore_role_ids is None:
        restore_role_ids = []
        if TEAM_SPAM_RESTORE_ROLE_ID > 0:
            restore_role_ids.append(TEAM_SPAM_RESTORE_ROLE_ID)
        if team_role_id is not None:
            restore_role_ids.append(team_role_id)
    team_spam_punishments["members"][str(member_id)] = {
        "guild_id": guild_id,
        "team_role_id": team_role_id,
        "restore_role_ids": restore_role_ids,
        "source": source,
        "punished_at_utc": now.isoformat(),
        "release_at_utc": datetime.fromtimestamp(release_at, tz=timezone.utc).isoformat(),
    }
    persist_team_spam_punishments()


async def release_due_team_spam_members() -> None:
    now_utc = datetime.now(timezone.utc)
    due_member_ids: list[str] = []
    for member_id, punishment_data in team_spam_punishments["members"].items():
        if not isinstance(punishment_data, dict):
            due_member_ids.append(member_id)
            continue

        release_at_raw = punishment_data.get("release_at_utc")
        try:
            release_at = datetime.fromisoformat(str(release_at_raw))
        except ValueError:
            due_member_ids.append(member_id)
            continue

        if release_at.tzinfo is None:
            release_at = release_at.replace(tzinfo=timezone.utc)
        if release_at <= now_utc:
            due_member_ids.append(member_id)

    if not due_member_ids:
        return

    for member_id in due_member_ids:
        punishment_data = team_spam_punishments["members"].get(member_id, {})
        try:
            guild_id = int(punishment_data.get("guild_id", 0))
        except (TypeError, ValueError):
            guild_id = 0
        team_role_raw = punishment_data.get("team_role_id")
        try:
            team_role_id = int(team_role_raw) if team_role_raw is not None else None
        except (TypeError, ValueError):
            team_role_id = None
        restore_role_ids_raw = punishment_data.get("restore_role_ids", [])
        restore_role_ids: list[int] = []
        if isinstance(restore_role_ids_raw, list):
            for role_id in restore_role_ids_raw:
                try:
                    restore_role_ids.append(int(role_id))
                except (TypeError, ValueError):
                    continue
        try:
            member_id_int = int(member_id)
        except ValueError:
            member_id_int = 0
        if guild_id > 0 and member_id_int > 0:
            await restore_member_after_team_spam(guild_id, member_id_int, team_role_id, restore_role_ids or None)
        team_spam_punishments["members"].pop(member_id, None)

    persist_team_spam_punishments()


async def clear_delinquent_status(member: discord.Member, *, reason: str) -> bool:
    delinquent_role = member.guild.get_role(DELINQUENT_ROLE_ID)
    if delinquent_role is None:
        return False
    if delinquent_role not in member.roles:
        return False

    try:
        await member.remove_roles(delinquent_role, reason=reason)
    except discord.HTTPException:
        return False

    return True


def parse_punishment_duration(raw_duration: str) -> int | None:
    cleaned_value = raw_duration.strip().lower()
    match = re.fullmatch(r"(\d+)\s*([mh])", cleaned_value)
    if match is None:
        return None

    amount = int(match.group(1))
    if amount <= 0:
        return None

    unit = match.group(2)
    if unit == "m":
        return amount * 60
    if unit == "h":
        return amount * 60 * 60
    return None


async def enforce_manual_delinquent_punishment(before: discord.Member, after: discord.Member) -> None:
    delinquent_role = after.guild.get_role(DELINQUENT_ROLE_ID)
    if delinquent_role is None:
        return
    if delinquent_role in before.roles or delinquent_role not in after.roles:
        return

    existing_entry = team_spam_punishments["members"].get(str(after.id))
    if isinstance(existing_entry, dict):
        return

    bot_member = after.guild.me
    if bot_member is None:
        return

    roles_to_remove = [
        role
        for role in after.roles
        if role != after.guild.default_role
        and role != delinquent_role
        and role < bot_member.top_role
    ]
    if not roles_to_remove:
        register_team_spam_punishment(
            after.id,
            after.guild.id,
            team_role_id=None,
            restore_role_ids=[],
            duration_seconds=MANUAL_DELINQUENT_PUNISHMENT_SECONDS,
            source="manual_delinquent",
        )
        return

    removed_roles: list[discord.Role] = []
    for role in roles_to_remove:
        try:
            await after.remove_roles(role, reason="Attribution manuelle du rôle Délinquant")
            removed_roles.append(role)
        except discord.HTTPException:
            continue

    register_team_spam_punishment(
        after.id,
        after.guild.id,
        team_role_id=None,
        restore_role_ids=[role.id for role in removed_roles],
        duration_seconds=MANUAL_DELINQUENT_PUNISHMENT_SECONDS,
        source="manual_delinquent",
    )


async def refresh_registered_leaderboards() -> None:
    await ensure_leaderboard_channel_message(LEADERBOARD_CHANNEL_ID)

    for channel_id_str, entry in list(leaderboard_state["channels"].items()):
        channel_id = int(channel_id_str)
        message_id = int(entry.get("message_id", 0))
        if message_id <= 0:
            continue

        message = leaderboard_messages.get(message_id)
        if message is None:
            message = await fetch_leaderboard_message(channel_id, message_id)
            if message is None:
                clear_leaderboard_registration(channel_id)
                continue
            leaderboard_messages[message_id] = message

        guild = message.guild
        if guild is None:
            clear_leaderboard_registration(channel_id)
            leaderboard_messages.pop(message_id, None)
            continue

        try:
            await run_discord_request(
                lambda: message.edit(embed=leaderboard_embed(guild, lambda role_id: division_power_for_role(guild, role_id)))
            )
        except (discord.NotFound, discord.Forbidden):
            clear_leaderboard_registration(channel_id)
            leaderboard_messages.pop(message_id, None)
            continue
        except discord.HTTPException:
            continue

        register_leaderboard_message(message)
        update_leaderboard_last_refresh(channel_id)


register_team_update_callback(discord_bot.schedule_leaderboard_refresh)


def register_leaderboard_message(message: discord.Message) -> None:
    leaderboard_messages[message.id] = message
    channel_id = message.channel.id
    leaderboard_state["channels"][str(channel_id)] = {
        "message_id": message.id,
        "last_refresh_utc": datetime.now(timezone.utc).isoformat(),
    }
    persist_leaderboard_state()


def persist_leaderboard_state() -> None:
    save_data(LEADERBOARD_STATE_KEY, leaderboard_state)


def clear_leaderboard_registration(channel_id: int) -> None:
    leaderboard_state["channels"].pop(str(channel_id), None)
    persist_leaderboard_state()


def update_leaderboard_last_refresh(channel_id: int) -> None:
    entry = leaderboard_state["channels"].get(str(channel_id))
    if entry is None:
        return
    entry["last_refresh_utc"] = datetime.now(timezone.utc).isoformat()
    persist_leaderboard_state()


async def fetch_leaderboard_message(channel_id: int, message_id: int) -> discord.Message | None:
    channel = discord_bot.get_cached_text_channel(channel_id)
    if channel is None:
        return None
    try:
        return await run_discord_request(lambda: channel.fetch_message(message_id))
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        return None


async def ensure_leaderboard_channel_message(channel_id: int) -> None:
    entry = leaderboard_state["channels"].get(str(channel_id))
    if entry is not None and int(entry.get("message_id", 0)) > 0:
        return

    channel = discord_bot.get_cached_text_channel(channel_id)
    if channel is None:
        return

    sent_message = await run_discord_request(
        lambda: channel.send(embed=leaderboard_embed(channel.guild, lambda role_id: division_power_for_role(channel.guild, role_id)))
    )
    register_leaderboard_message(sent_message)


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
            "Exemple : `!link ABC123` (ou `!link ABC123 EMOTE_DE_TA_TEAM`)\n"
            "Besoin d'aide pour choisir une division ? Utilise le bouton **Comment rejoindre une division**.\n"
            "🟣 Twitch : https://www.twitch.tv/zogaa_"
        ),
        INFO_COLOR,
    )
    await run_discord_request(lambda: interaction.channel.send(embed=embed, view=LinkAccountView()))
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

    normalized_role_name = role_name.strip().lower()
    if normalized_role_name in {"délinquant", "delinquant"}:
        role = guild.get_role(DELINQUENT_ROLE_ID)
    else:
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


async def enforce_delinquent_team_block(before: discord.Member, after: discord.Member) -> bool:
    delinquent_role = after.guild.get_role(DELINQUENT_ROLE_ID)
    if delinquent_role is None or delinquent_role not in after.roles:
        return False

    before_team_role_ids = {
        role.id
        for role in before.roles
        if get_team_entry_by_role(role) is not None
    }
    blocked_team_roles = [
        role
        for role in after.roles
        if get_team_entry_by_role(role) is not None and role.id not in before_team_role_ids
    ]
    if not blocked_team_roles:
        return False

    try:
        await after.remove_roles(*blocked_team_roles, reason="Le rôle Délinquant empêche de rejoindre une team")
    except discord.HTTPException:
        return False

    blocked_team_names = ", ".join(f"**{role.name}**" for role in blocked_team_roles)
    try:
        await after.send(
            embed=build_embed(
                "🚫 Team refusée",
                (
                    "T'as été un vilain garnement 😈\n"
                    f"Impossible de rejoindre {blocked_team_names} tant que tu as le rôle **Délinquant**."
                ),
                WARNING_COLOR,
            )
        )
    except discord.HTTPException:
        pass

    return True


async def enforce_single_team_membership(before: discord.Member, after: discord.Member) -> None:
    lock = get_team_enforcement_lock(after.id)
    async with lock:
        guild = after.guild
        current_member = guild.get_member(after.id)
        if current_member is None:
            return

        before_team_roles = [role for role in before.roles if get_team_entry_by_role(role) is not None]
        after_team_roles = [role for role in current_member.roles if get_team_entry_by_role(role) is not None]
        if len(after_team_roles) <= 1:
            return

        protected_role_ids = {role.id for role in before_team_roles}
        removed_roles = []
        for role in after_team_roles:
            if role.id in protected_role_ids:
                continue
            removed_roles.append(role)

        if not removed_roles:
            kept_role = max(after_team_roles, key=lambda role: role.position)
            removed_roles = [role for role in after_team_roles if role.id != kept_role.id]

        if not removed_roles:
            return

        try:
            await current_member.remove_roles(*removed_roles, reason="Un membre ne peut appartenir qu'à une seule team")
        except discord.HTTPException:
            return

        kept_team_roles = [role for role in after_team_roles if role.id not in {removed.id for removed in removed_roles}]
        kept_role = kept_team_roles[0] if kept_team_roles else None
        removed_names = ", ".join(f"**{role.name}**" for role in removed_roles)
        kept_name = f"**{kept_role.name}**" if kept_role is not None else "aucune"
        violations = team_switch_violations.get(current_member.id, 0) + 1
        team_switch_violations[current_member.id] = violations

        warning_messages = {
            1: f"⚠️ Avertissement 1/{TEAM_SWITCH_SPAM_THRESHOLD} : tu es déjà dans {kept_name}. Retrait de {removed_names}.",
            2: (
                f"🚨 Avertissement 2/{TEAM_SWITCH_SPAM_THRESHOLD} : Il me semble avoir été clair nan ? "
                f"Retrait de {removed_names}."
            ),
        }
        user_warning_message = warning_messages.get(
            violations,
            (
                f"⛔ Avertissement {TEAM_SWITCH_SPAM_THRESHOLD}/{TEAM_SWITCH_SPAM_THRESHOLD} : spam détecté. "
                "Tu es retiré de ta team actuelle et envoyé dans la fosse."
            ),
        )

        try:
            await current_member.send(
                embed=build_embed(
                    "Une seule team autorisée",
                    user_warning_message,
                    WARNING_COLOR,
                )
            )
        except discord.HTTPException:
            pass

        if violations >= TEAM_SWITCH_SPAM_THRESHOLD:
            delinquent_role = guild.get_role(DELINQUENT_ROLE_ID)
            restored_role = guild.get_role(TEAM_SPAM_RESTORE_ROLE_ID)
            kept_role_id = kept_role.id if kept_role is not None else None
            try:
                if kept_role is not None:
                    await current_member.remove_roles(kept_role, reason="Spam de changement de team (3 avertissements)")
                if restored_role is not None and restored_role in current_member.roles:
                    await current_member.remove_roles(restored_role, reason="Spam de changement de team (3 avertissements)")
                if delinquent_role is not None and delinquent_role not in current_member.roles:
                    await current_member.add_roles(delinquent_role, reason="Spam de changement de team (3 avertissements)")
            except discord.HTTPException:
                pass
            finally:
                team_switch_violations[current_member.id] = 0
                register_team_spam_punishment(current_member.id, guild.id, kept_role_id)

        team_ping_text = ""
        team_ping_content = None
        if violations >= TEAM_SWITCH_SPAM_THRESHOLD:
            ping_role_id = kept_role.id if kept_role is not None else None
            if ping_role_id is None and kept_role is None:
                for _team_name, team_data in teams.get("teams", {}).items():
                    if team_data.get("role_id") in {role.id for role in before_team_roles}:
                        ping_role_id = team_data.get("role_id")
                        break

            if ping_role_id is not None:
                team_ping_text = f" ⚠️ Team concernée: <@&{ping_role_id}>."
                team_ping_content = f"<@&{ping_role_id}>"

        alert_channel = guild.get_channel(TEAM_SWITCH_ALERT_CHANNEL_ID)
        if isinstance(alert_channel, discord.TextChannel):
            await alert_channel.send(
                content=team_ping_content,
                embed=build_embed(
                    "Team multiple bloquée",
                    (
                        f"{current_member.mention} a tenté un changement de team. "
                        f"Avertissement **{min(violations, TEAM_SWITCH_SPAM_THRESHOLD)}/{TEAM_SWITCH_SPAM_THRESHOLD}**."
                        + (
                            " Sanction appliquée : retrait de sa team + rôle **Délinquant** + envoyé à la fosse."
                            if violations >= TEAM_SWITCH_SPAM_THRESHOLD
                            else f" Retrait de {removed_names}."
                        )
                        + team_ping_text
                    ),
                    WARNING_COLOR,
                ),
                allowed_mentions=discord.AllowedMentions(roles=True),
            )


def team_role_ids_for_member(member: discord.Member) -> set[int]:
    return {
        role.id
        for role in member.roles
        if get_team_entry_by_role(role) is not None
    }


def get_primary_team_role_id(member: discord.Member) -> int | None:
    team_role_ids = team_role_ids_for_member(member)
    if not team_role_ids:
        return None
    return next(iter(sorted(team_role_ids)))


def get_team_channel_for_role(guild: discord.Guild, role: discord.Role) -> discord.TextChannel | None:
    team_entry = get_team_entry_by_role(role)
    if team_entry is None:
        return None
    _team_name, team_data = team_entry
    channel_id = team_data.get("channel_id")
    if not isinstance(channel_id, int):
        return None
    channel = guild.get_channel(channel_id)
    if isinstance(channel, discord.TextChannel):
        return channel
    return None


def get_team_channel_by_name(guild: discord.Guild, team_name: str) -> discord.TextChannel | None:
    team_data = teams.get("teams", {}).get(team_name.lower())
    if not isinstance(team_data, dict):
        return None
    role_id = team_data.get("role_id")
    if not isinstance(role_id, int):
        return None
    team_role = guild.get_role(role_id)
    if team_role is None:
        return None
    return get_team_channel_for_role(guild, team_role)


async def announce_team_points(team_name: str, points: int, *, reason: str | None = None) -> None:
    guild = discord_bot.get_guild(GUILD_ID)
    if guild is None:
        return
    team_channel = get_team_channel_by_name(guild, team_name)
    if team_channel is None:
        return
    team_data = teams.get("teams", {}).get(team_name)
    team_role = None
    if isinstance(team_data, dict):
        role_id = team_data.get("role_id")
        if isinstance(role_id, int):
            team_role = guild.get_role(role_id)
    reason_suffix = f" ({reason})" if reason else ""
    team_mention = team_role.mention if team_role is not None else team_name.title()
    try:
        await team_channel.send(f"🏆 {team_mention} gagne **{points}** point(s){reason_suffix} !")
    except discord.HTTPException:
        return


def snapshot_team_record_state() -> dict[str, dict[str, int]]:
    state: dict[str, dict[str, int]] = {}
    for team_name, team_data in teams.get("teams", {}).items():
        if not isinstance(team_data, dict):
            continue
        try:
            wins = int(team_data.get("wins", 0))
        except (TypeError, ValueError):
            wins = 0
        try:
            losses = int(team_data.get("losses", 0))
        except (TypeError, ValueError):
            losses = 0
        state[team_name] = {"wins": max(0, wins), "losses": max(0, losses)}
    return state


async def announce_team_victory(team_name: str, victories: int = 1) -> None:
    if victories <= 0:
        return
    guild = discord_bot.get_guild(GUILD_ID)
    if guild is None:
        return
    team_channel = get_team_channel_by_name(guild, team_name)
    if team_channel is None:
        return
    team_data = teams.get("teams", {}).get(team_name)
    team_role = None
    if isinstance(team_data, dict):
        role_id = team_data.get("role_id")
        if isinstance(role_id, int):
            team_role = guild.get_role(role_id)
    team_mention = team_role.mention if team_role is not None else team_name.title()
    suffix = "s" if victories > 1 else ""
    try:
        await team_channel.send(f"🏆 {team_mention} valide **{victories}** victoire{suffix} !")
    except discord.HTTPException:
        return


def get_loser_gif_urls() -> list[str]:
    raw_urls = config.get("loser_gif_urls", [])
    if not isinstance(raw_urls, list):
        return []
    cleaned_urls = [str(url).strip() for url in raw_urls]
    return [url for url in cleaned_urls if url.startswith(("http://", "https://"))]


async def announce_team_loss(team_name: str, defeats: int = 1) -> None:
    if defeats <= 0:
        return
    guild = discord_bot.get_guild(GUILD_ID)
    if guild is None:
        return
    team_channel = get_team_channel_by_name(guild, team_name)
    if team_channel is None:
        return
    loser_gif_urls = get_loser_gif_urls()
    if not loser_gif_urls:
        return
    loser_gif_url = random.choice(loser_gif_urls)
    try:
        await team_channel.send(
            f"{defeats} défaites… au moins vous êtes constants, c’est déjà ça.\n{loser_gif_url}"
            if defeats > 1
            else f"Défaite… belle coordination, chacun a bien perdu de son côté.\n{loser_gif_url}"
        )
    except discord.HTTPException:
        return


async def announce_team_record_changes() -> None:
    global team_record_snapshot
    current_snapshot = snapshot_team_record_state()
    previous_snapshot = team_record_snapshot
    team_record_snapshot = current_snapshot

    all_team_names = set(previous_snapshot) | set(current_snapshot)
    for team_name in sorted(all_team_names):
        current_entry = current_snapshot.get(team_name, {"wins": 0, "losses": 0})
        previous_entry = previous_snapshot.get(team_name, {"wins": 0, "losses": 0})
        win_delta = current_entry["wins"] - previous_entry["wins"]
        loss_delta = current_entry["losses"] - previous_entry["losses"]

        if win_delta > 0:
            await announce_team_victory(team_name, win_delta)
        if loss_delta > 0:
            await announce_team_loss(team_name, loss_delta)


team_record_snapshot = snapshot_team_record_state()
register_team_update_callback(announce_team_record_changes)


class RouletteRusseJoinView(discord.ui.View):
    def __init__(self, host_id: int) -> None:
        super().__init__(timeout=ROULETTE_JOIN_WINDOW_SECONDS)
        self.host_id = host_id
        self.participant_ids: set[int] = {host_id}

    @discord.ui.button(label="Rejoindre", style=discord.ButtonStyle.danger, emoji="🔫")
    async def join(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        if not isinstance(interaction.user, discord.Member):
            await send_interaction_embed(interaction, "Erreur", "Commande serveur uniquement.", ERROR_COLOR, ephemeral=True)
            return

        if interaction.user.id in self.participant_ids:
            await send_interaction_embed(interaction, "Déjà inscrit", "Tu participes déjà à la roulette russe.", WARNING_COLOR, ephemeral=True)
            return

        self.participant_ids.add(interaction.user.id)
        await send_interaction_embed(
            interaction,
            "Inscription validée",
            f"Tu rejoins la roulette russe. Joueurs actuels : **{len(self.participant_ids)}**.",
            SUCCESS_COLOR,
            ephemeral=True,
        )


@discord_bot.tree.command(name="rouletterusse", description="Lance une roulette russe (10s pour rejoindre)", guild=guild_object)
@app_commands.checks.cooldown(1, 15.0, key=lambda i: (i.guild_id, i.user.id))
async def roulette_russe(interaction: discord.Interaction) -> None:
    guild = interaction.guild
    if guild is None or not isinstance(interaction.user, discord.Member):
        await send_interaction_embed(interaction, "Erreur", "Cette commande doit être utilisée dans le serveur.", ERROR_COLOR, ephemeral=True)
        return

    lock = get_roulette_russe_lock(guild.id)
    if lock.locked():
        await send_interaction_embed(interaction, "Partie en cours", "Une roulette russe est déjà active sur ce serveur.", WARNING_COLOR, ephemeral=True)
        return

    async with lock:
        view = RouletteRusseJoinView(interaction.user.id)
        await interaction.response.send_message(
            embed=build_embed(
                "🔫 Roulette russe",
                (
                    f"{interaction.user.mention} lance une roulette russe.\n"
                    f"Tu as **{ROULETTE_JOIN_WINDOW_SECONDS} secondes** pour cliquer sur **Rejoindre**."
                ),
                WARNING_COLOR,
            ),
            view=view,
        )

        await asyncio.sleep(ROULETTE_JOIN_WINDOW_SECONDS)
        view.stop()

        participants: list[discord.Member] = []
        for participant_id in view.participant_ids:
            member = guild.get_member(participant_id)
            if member is not None:
                participants.append(member)

        if len(participants) < 2:
            await interaction.followup.send(
                embed=build_embed(
                    "Roulette annulée",
                    "Pas assez de joueurs (minimum 2).",
                    WARNING_COLOR,
                )
            )
            return

        loser = random.choice(participants)
        restore_role_ids = [role.id for role in loser.roles if role.id == TEAM_SPAM_RESTORE_ROLE_ID or get_team_entry_by_role(role) is not None]
        punished = await apply_temporary_delinquent_punishment(
            loser,
            duration_seconds=ROULETTE_PUNISHMENT_SECONDS,
            reason="Roulette russe: sanction temporaire",
            restore_role_ids=restore_role_ids,
            source="roulette_russe",
        )
        if not punished:
            await interaction.followup.send(
                embed=build_embed(
                    "Erreur roulette",
                    "Impossible d'appliquer la sanction (rôle manquant ou permissions insuffisantes).",
                    ERROR_COLOR,
                )
            )
            return

        await interaction.followup.send(
            embed=build_embed(
                "💥 BOOM",
                (
                    f"Perdant: {loser.mention}\n"
                    "Sanction: retrait team + plèbe, rôle **Délinquant** pendant **10 minutes**.\n"
                    "Ses rôles sont sauvegardés et seront restaurés automatiquement."
                ),
                WARNING_COLOR,
            )
        )


async def announce_team_joins(before: discord.Member, current_member: discord.Member) -> None:
    if should_skip_team_membership_announcement(before, current_member):
        return

    before_team_role_ids = team_role_ids_for_member(before)
    joined_team_roles = [
        role
        for role in current_member.roles
        if get_team_entry_by_role(role) is not None and role.id not in before_team_role_ids
    ]
    if not joined_team_roles:
        return

    for role in joined_team_roles:
        team_channel = get_team_channel_for_role(current_member.guild, role)
        if team_channel is None:
            continue
        try:
            await team_channel.send(f"🎉 {current_member.mention} a rejoint la team **{role.name}** !")
        except discord.HTTPException:
            continue


async def announce_team_departures(before: discord.Member, current_member: discord.Member) -> None:
    if should_skip_team_membership_announcement(before, current_member):
        return

    current_team_role_ids = team_role_ids_for_member(current_member)
    departed_team_roles = [
        role
        for role in before.roles
        if get_team_entry_by_role(role) is not None and role.id not in current_team_role_ids
    ]
    if not departed_team_roles:
        return

    for role in departed_team_roles:
        team_channel = get_team_channel_for_role(current_member.guild, role)
        if team_channel is None:
            continue
        try:
            await team_channel.send(f"👋 {current_member.mention} a quitté la team **{role.name}**.")
        except discord.HTTPException:
            continue


def should_skip_team_membership_announcement(before: discord.Member, after: discord.Member) -> bool:
    delinquent_role = after.guild.get_role(DELINQUENT_ROLE_ID)
    if delinquent_role is None:
        return False
    return delinquent_role in before.roles or delinquent_role in after.roles


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
@app_commands.describe(
    role="Rôle représentant l'équipe",
    emoji="Emoji affiché comme blason",
    motto="Devise de l'équipe (optionnel)",
    channel="Salon de la team (optionnel)",
)
@app_commands.check(is_discord_moderator)
async def team_create(
    interaction: discord.Interaction,
    role: discord.Role,
    emoji: str,
    motto: str = "",
    channel: discord.TextChannel | None = None,
) -> None:
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
        "channel_id": channel.id if channel is not None else None,
    }
    save_teams()
    channel_line = f"\nSalon associé : {channel.mention}" if channel is not None else ""
    await send_interaction_embed(
        interaction,
        "Équipe créée",
        f"Nouvelle équipe : {emoji} **{role.name}**.{channel_line}",
        SUCCESS_COLOR,
    )


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
    channel="Nouveau salon d'équipe (optionnel)",
)
@app_commands.check(is_discord_moderator)
async def team_edit(
    interaction: discord.Interaction,
    role: discord.Role,
    emoji: str | None = None,
    motto: str | None = None,
    channel: discord.TextChannel | None = None,
) -> None:
    name = role.name.lower()
    if name not in teams["teams"]:
        await send_interaction_embed(interaction, "Équipe introuvable", "Cette équipe n'existe pas.", ERROR_COLOR, ephemeral=True)
        return

    if emoji is None and motto is None and channel is None:
        await send_interaction_embed(
            interaction,
            "Aucune modification",
            "Tu dois fournir au moins un champ à modifier (emoji, devise et/ou salon).",
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

    if channel is not None:
        team_data["channel_id"] = channel.id
        updates.append(f"Salon → {channel.mention}")

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


@team_group.command(name="loser", description="Gérer les GIFs envoyés aux équipes perdantes")
@app_commands.describe(
    action="Action à effectuer (add/remove/list/clear)",
    gif_url="Lien du GIF (requis pour add/remove)",
)
@app_commands.check(is_discord_moderator)
async def team_loser(
    interaction: discord.Interaction,
    action: Literal["add", "remove", "list", "clear"] = "add",
    gif_url: str | None = None,
) -> None:
    current_urls = get_loser_gif_urls()

    if action == "list":
        if not current_urls:
            await send_interaction_embed(
                interaction,
                "GIFs des perdants",
                "Aucun GIF configuré. Ajoute-en avec `/team loser add <url>`.",
                INFO_COLOR,
                ephemeral=True,
            )
            return
        urls_preview = "\n".join(f"`{index + 1}.` {url}" for index, url in enumerate(current_urls))
        await send_interaction_embed(
            interaction,
            "GIFs des perdants",
            urls_preview,
            INFO_COLOR,
            ephemeral=True,
        )
        return

    if action == "clear":
        config["loser_gif_urls"] = []
        save_config()
        await send_interaction_embed(
            interaction,
            "GIFs supprimés",
            "Tous les GIFs des équipes perdantes ont été supprimés.",
            SUCCESS_COLOR,
        )
        return

    cleaned_url = str(gif_url or "").strip()
    if not cleaned_url:
        await send_interaction_embed(
            interaction,
            "Lien invalide",
            "Tu dois fournir une URL de GIF pour cette action.",
            ERROR_COLOR,
            ephemeral=True,
        )
        return

    if not cleaned_url.startswith(("http://", "https://")):
        await send_interaction_embed(
            interaction,
            "Lien invalide",
            "Le lien doit commencer par `http://` ou `https://`.",
            ERROR_COLOR,
            ephemeral=True,
        )
        return

    if action == "add":
        if cleaned_url in current_urls:
            await send_interaction_embed(
                interaction,
                "Déjà présent",
                "Ce GIF est déjà dans la liste.",
                WARNING_COLOR,
                ephemeral=True,
            )
            return
        current_urls.append(cleaned_url)
        config["loser_gif_urls"] = current_urls
        save_config()
        await send_interaction_embed(
            interaction,
            "GIF ajouté",
            f"Le GIF a été ajouté. Total actuel : **{len(current_urls)}**.",
            SUCCESS_COLOR,
        )
        return

    if action == "remove":
        if cleaned_url not in current_urls:
            await send_interaction_embed(
                interaction,
                "GIF introuvable",
                "Ce GIF n'existe pas dans la liste actuelle.",
                ERROR_COLOR,
                ephemeral=True,
            )
            return
        updated_urls = [url for url in current_urls if url != cleaned_url]
        config["loser_gif_urls"] = updated_urls
        save_config()
        await send_interaction_embed(
            interaction,
            "GIF supprimé",
            f"GIF retiré. Total actuel : **{len(updated_urls)}**.",
            SUCCESS_COLOR,
        )
        return

    await send_interaction_embed(
        interaction,
        "Action inconnue",
        "Action non supportée.",
        ERROR_COLOR,
        ephemeral=True,
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
    if amount > 0:
        await announce_team_points(name, amount, reason="ajout de points")
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


@team_group.command(name="cp", description="Créer les profils DivWar des membres des teams (niveau minimum 1)")
@app_commands.check(is_discord_moderator)
async def team_createprofile(interaction: discord.Interaction) -> None:
    guild = interaction.guild
    if guild is None:
        await send_interaction_embed(interaction, "Erreur", "Cette commande doit être utilisée dans le serveur.", ERROR_COLOR, ephemeral=True)
        return

    if not interaction.response.is_done():
        await interaction.response.defer(thinking=True, ephemeral=True)

    created_profiles, leveled_profiles = await discord_bot.initialize_team_member_profiles(minimum_level=1)
    await interaction.followup.send(
        embed=build_embed(
            "Profils DivWar synchronisés",
            (
                "Tous les membres ayant un rôle de team ont été traités.\n"
                f"• Profils créés : **{created_profiles}**\n"
                f"• Profils montés au niveau 1 minimum : **{leveled_profiles}**"
            ),
            SUCCESS_COLOR,
        ),
        ephemeral=True,
    )


@team_group.command(name="pardon", description="Retirer le rôle Délinquant d'un membre")
@app_commands.describe(member="Membre à libérer de la sanction Délinquant")
@app_commands.check(is_discord_moderator)
async def team_pardon(interaction: discord.Interaction, member: discord.Member) -> None:
    removed = await clear_delinquent_status(member, reason=f"Retrait manuel via /team pardon par {interaction.user}")
    team_switch_violations.pop(member.id, None)
    had_punishment_entry = team_spam_punishments["members"].pop(str(member.id), None) is not None
    if had_punishment_entry:
        persist_team_spam_punishments()

    if removed:
        details = [f"{member.mention} n'a plus le rôle **Délinquant**."]
        if had_punishment_entry:
            details.append("Sa sanction programmée a aussi été retirée.")
        await send_interaction_embed(interaction, "Membre pardonné", "\n".join(details), SUCCESS_COLOR)
        return

    if had_punishment_entry:
        await send_interaction_embed(
            interaction,
            "Entrée de sanction supprimée",
            (
                f"{member.mention} n'avait pas le rôle **Délinquant**, "
                "mais la sanction planifiée a été supprimée."
            ),
            WARNING_COLOR,
        )
        return

    await send_interaction_embed(
        interaction,
        "Aucune sanction active",
        f"{member.mention} n'a pas le rôle **Délinquant**.",
        WARNING_COLOR,
        ephemeral=True,
    )


@team_group.command(name="punition", description="Mettre un membre en Délinquant pendant une durée personnalisée")
@app_commands.describe(member="Membre à sanctionner", duration="Durée au format 30m ou 2h")
@app_commands.check(is_discord_moderator)
async def team_punition(interaction: discord.Interaction, member: discord.Member, duration: str) -> None:
    duration_seconds = parse_punishment_duration(duration)
    if duration_seconds is None:
        await send_interaction_embed(
            interaction,
            "Durée invalide",
            "Utilise un format comme **30m** ou **2h** (minutes/heures).",
            ERROR_COLOR,
            ephemeral=True,
        )
        return

    restore_role_ids = [role.id for role in member.roles if role != member.guild.default_role and role.id != DELINQUENT_ROLE_ID]
    punished = await apply_temporary_delinquent_punishment(
        member,
        duration_seconds=duration_seconds,
        reason=f"Sanction manuelle via /team punition par {interaction.user}",
        restore_role_ids=restore_role_ids,
        source="team_punition",
    )
    if not punished:
        await send_interaction_embed(
            interaction,
            "Punition impossible",
            "Impossible d'appliquer la sanction (rôle Délinquant manquant ou permissions insuffisantes).",
            ERROR_COLOR,
            ephemeral=True,
        )
        return

    await send_interaction_embed(
        interaction,
        "Punition appliquée",
        (
            f"{member.mention} est maintenant **Délinquant** pour **{duration}**.\n"
            "Tous ses rôles (hors @everyone) ont été retirés et seront restaurés automatiquement."
        ),
        SUCCESS_COLOR,
    )


@team_group.command(name="leaderboard", description="Afficher le classement des équipes")
@app_commands.checks.cooldown(1, 5.0, key=lambda i: (i.guild_id, i.user.id))
async def team_leaderboard(interaction: discord.Interaction) -> None:
    guild = interaction.guild
    if guild is None:
        await send_interaction_embed(interaction, "Erreur", "Cette commande doit être utilisée dans le serveur.", ERROR_COLOR, ephemeral=True)
        return

    embed = leaderboard_embed(guild, lambda role_id: division_power_for_role(guild, role_id))
    # Optimization: defer once, then send one follow-up (avoids extra original_response fetch call).
    if not interaction.response.is_done():
        await interaction.response.defer(thinking=False)
    sent_message = await interaction.followup.send(embed=embed, wait=True)

    register_leaderboard_message(sent_message)


@team_group.command(name="list", description="Afficher les membres et les statistiques des équipes")
@app_commands.checks.cooldown(1, 5.0, key=lambda i: (i.guild_id, i.user.id))
async def team_list(interaction: discord.Interaction) -> None:
    guild = interaction.guild
    if guild is None:
        await send_interaction_embed(interaction, "Erreur", "Cette commande doit être utilisée dans le serveur.", ERROR_COLOR, ephemeral=True)
        return

    embed = team_overview_embed(guild)
    if not interaction.response.is_done():
        await interaction.response.defer(thinking=False)
    await interaction.followup.send(embed=embed)


@team_group.command(name="detail", description="Voir le détail d'une team et ses membres")
@app_commands.checks.cooldown(1, 3.0, key=lambda i: (i.guild_id, i.user.id))
@app_commands.describe(role="Rôle de la team (ex: @NomDeLaTeam)")
async def team_detail(interaction: discord.Interaction, role: discord.Role) -> None:
    guild = interaction.guild
    if guild is None:
        await send_interaction_embed(interaction, "Erreur", "Cette commande doit être utilisée dans le serveur.", ERROR_COLOR, ephemeral=True)
        return

    embed = team_detail_embed(guild, role, lambda role_id: division_power_for_role(guild, role_id))
    is_error_embed = embed.title == "Équipe introuvable"
    if not interaction.response.is_done():
        await interaction.response.defer(thinking=False, ephemeral=is_error_embed)
    await interaction.followup.send(embed=embed, ephemeral=is_error_embed)


@team_group.command(name="membres", description="Afficher le niveau et les stats d'un membre")
@app_commands.describe(member="Membre à consulter (optionnel)")
async def team_membres(interaction: discord.Interaction, member: discord.Member | None = None) -> None:
    target_member: discord.Member | None = member
    if target_member is None and isinstance(interaction.user, discord.Member):
        target_member = interaction.user

    if target_member is None:
        await send_interaction_embed(
            interaction,
            "Membre introuvable",
            "Impossible de déterminer le membre à afficher.",
            ERROR_COLOR,
            ephemeral=True,
        )
        return

    division_war_member = discord_bot.division_war.get_or_create_member(
        user_id=target_member.id,
        division_id=get_primary_team_role_id(target_member),
    )

    division_label = "Aucune"
    if division_war_member.division_id is not None and interaction.guild is not None:
        division_role = interaction.guild.get_role(division_war_member.division_id)
        if division_role is not None:
            division_label = division_role.name
        else:
            division_label = str(division_war_member.division_id)
    now_timestamp = time.time()
    seconds_since_last_message = max(0, int(now_timestamp - division_war_member.last_message_timestamp))
    xp_cooldown_seconds = int(discord_bot.division_war.config.min_seconds_between_xp)
    xp_ready_in_seconds = max(0, xp_cooldown_seconds - seconds_since_last_message)

    embed = build_embed(
        "Stats division",
        (
            f"👤 Membre : {target_member.mention}\n"
            f"🛡️ Division : **{division_label}**\n"
            f"⭐ XP : **{division_war_member.xp}**\n"
            f"📈 Niveau : **{division_war_member.level}**\n"
            f"❤️ HP : **{division_war_member.hp}**\n"
            f"⚔️ ATK : **{division_war_member.atk}**\n"
            f"💥 Puissance : **{division_war_member.member_power:.1f}**\n"
            f"⏱️ Dernier message comptabilisé : **il y a {seconds_since_last_message}s**\n"
            f"🧪 XP disponible dans : **{xp_ready_in_seconds}s**"
        ),
        INFO_COLOR,
    )

    await interaction.response.send_message(embed=embed, ephemeral=False)


@discord_bot.tree.command(name="divwar", description="Lancer un duel entre deux divisions", guild=guild_object)
@app_commands.describe(team1="Première division", team2="Deuxième division")
@app_commands.checks.cooldown(1, 5.0, key=lambda i: (i.guild_id, i.user.id))
async def divwar_command(interaction: discord.Interaction, team1: discord.Role, team2: discord.Role) -> None:
    guild = interaction.guild
    if guild is None:
        await send_interaction_embed(interaction, "Erreur", "Cette commande doit être utilisée dans le serveur.", ERROR_COLOR, ephemeral=True)
        return

    if team1.id == team2.id:
        await send_interaction_embed(
            interaction,
            "Paramètres invalides",
            "Choisis deux divisions différentes pour lancer un duel.",
            ERROR_COLOR,
            ephemeral=True,
        )
        return

    missing_divisions: list[str] = []
    if get_team_entry_by_role(team1) is None:
        missing_divisions.append(team1.mention)
    if get_team_entry_by_role(team2) is None:
        missing_divisions.append(team2.mention)
    if missing_divisions:
        await send_interaction_embed(
            interaction,
            "Division introuvable",
            f"Division(s) non enregistrée(s) : {', '.join(missing_divisions)}.",
            ERROR_COLOR,
            ephemeral=True,
        )
        return

    await interaction.response.defer(thinking=True)

    division_1 = discord_bot.division_war.build_division_profile(team1.id, team1.name)
    division_2 = discord_bot.division_war.build_division_profile(team2.id, team2.name)
    division_1_members = discord_bot.division_war.get_members_by_division(team1.id)
    division_2_members = discord_bot.division_war.get_members_by_division(team2.id)
    duel_result = discord_bot.division_war.simulate_division_war(
        division_1,
        division_2,
        user_label_resolver=lambda user_id: _division_war_member_label(guild, user_id),
    )

    winner_role = guild.get_role(duel_result.winner_division_id) if duel_result.winner_division_id else None
    winner_label = winner_role.mention if winner_role is not None else "Aucun vainqueur"
    summary_lines = [
        f"🛡️ **{team1.mention}** • Puissance: `{division_1.division_power:.1f}` • Membres: `{len(division_1_members)}`",
        f"🛡️ **{team2.mention}** • Puissance: `{division_2.division_power:.1f}` • Membres: `{len(division_2_members)}`",
        f"🏁 **Vainqueur** : {winner_label}",
        f"🔁 **Rounds joués** : `{duel_result.rounds}`",
    ]
    live_embed = _build_divwar_embed(
        title="⚔️ Duel de divisions en direct",
        summary_lines=summary_lines,
        combat_lines=[],
        status_line="🟡 **Préparation du combat...**",
    )
    live_message = await interaction.followup.send(embed=live_embed, ephemeral=False, wait=True)
    await _animate_division_war_message(message=live_message, summary_lines=summary_lines, duel_log=duel_result.log)


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
@team_pardon.error
@team_punition.error
@team_captain.error
@team_motto.error
@link_panel.error
@link_remove.error
@rule_add.error
@rule_remove.error
@team_detail.error
@team_leaderboard.error
@team_list.error
@roulette_russe.error
async def admin_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
    if isinstance(error, app_commands.CommandOnCooldown):
        await send_interaction_embed(interaction, "Cooldown", f"Réessaie dans {error.retry_after:.1f}s.", WARNING_COLOR, ephemeral=True)
        return
    if isinstance(error, (app_commands.MissingPermissions, app_commands.CheckFailure)):
        await send_interaction_embed(interaction, "Permission refusée", "Tu n'as pas la permission d'utiliser cette commande.", ERROR_COLOR, ephemeral=True)
        return
    raise error


async def start_discord_bot() -> None:
    retry_delay_seconds = API_RETRY_BASE_DELAY_SECONDS

    while True:
        try:
            await discord_bot.start(DISCORD_TOKEN)
            return
        except discord.HTTPException as error:
            if error.status != 429:
                raise

            retry_after = float(getattr(error, "retry_after", 0) or 0)
            wait_seconds = retry_after if retry_after > 0 else retry_delay_seconds
            print(
                f"[DISCORD] Connexion refusée (HTTP 429). "
                f"Nouvelle tentative dans {wait_seconds:.1f} secondes."
            )
            await asyncio.sleep(wait_seconds)
            retry_delay_seconds = min(retry_delay_seconds * 2, 60)
