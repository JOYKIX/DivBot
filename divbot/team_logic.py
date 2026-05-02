from datetime import datetime, timezone
from typing import Any, Callable

import discord

from divbot.common import (
    INFO_COLOR,
    MAX_TEAM_MEMBERS_DETAIL,
    MAX_TEAM_MEMBERS_DISPLAY,
    ERROR_COLOR,
    config,
    save_config,
    save_teams,
    teams,
)


def get_team_entry_by_name(team_name: str) -> tuple[str, dict[str, Any]] | None:
    normalized_name = team_name.strip().lower()
    return next(
        (
            (stored_name, data)
            for stored_name, data in teams["teams"].items()
            if str(stored_name).strip().lower() == normalized_name
        ),
        None,
    )


def get_team_entry_by_role(role: discord.Role) -> tuple[str, dict[str, Any]] | None:
    return get_team_entry_by_name(role.name)


def get_team_role(guild: discord.Guild, team_data: dict[str, Any]) -> discord.Role | None:
    role_id = team_data.get("role_id")
    if not isinstance(role_id, int):
        return None
    return guild.get_role(role_id)


def team_staff_mentions(guild: discord.Guild, team_data: dict[str, Any]) -> str:
    captain_id = team_data.get("captain_id")
    vice_captain_id = team_data.get("vice_captain_id")

    captain = guild.get_member(captain_id) if captain_id else None
    vice_captain = guild.get_member(vice_captain_id) if vice_captain_id else None

    captain_label = captain.mention if captain else "Non défini"
    vice_label = vice_captain.mention if vice_captain else "Non défini"
    return f"Capitaine : {captain_label}\nVice-capitaine : {vice_label}"


def format_member_list(role: discord.Role) -> str:
    if not role.members:
        return "Aucun membre"

    member_names = [member.display_name for member in role.members[:MAX_TEAM_MEMBERS_DISPLAY]]
    extra_members = len(role.members) - len(member_names)
    if extra_members > 0:
        member_names.append(f"+{extra_members} autre(s)")
    return ", ".join(member_names)


def current_team_member_limit() -> int:
    try:
        return max(0, int(config.get("max_team_members", 0)))
    except (TypeError, ValueError):
        return 0


def team_member_limit_label() -> str:
    limit = current_team_member_limit()
    if limit <= 0:
        return "Illimité"
    return str(limit)


def format_rules() -> str:
    rules = config.get("rules", [])
    if not rules:
        return f"Aucune règle configurée pour le moment.\nLimite de membres par team : **{team_member_limit_label()}**."

    lines = []
    for index, rule in enumerate(rules):
        lines.append(f"`{index}` • **{rule['type']}** → `{rule['value']}` → **{rule['role']}**")
    lines.append(f"\nLimite de membres par team : **{team_member_limit_label()}**.")
    return "\n".join(lines)


def placement_emoji(position: int) -> str:
    if position == 1:
        return "🥇"
    if position == 2:
        return "🥈"
    if position == 3:
        return "🥉"
    return "🏅"


def current_month() -> int:
    try:
        return max(1, int(config.get("current_month", 1)))
    except (TypeError, ValueError):
        return 1


def team_month_wins(team_data: dict[str, Any], month: int | None = None) -> int:
    target_month = str(month if month is not None else current_month())
    monthly_wins = team_data.get("monthly_wins", {})
    if not isinstance(monthly_wins, dict):
        return 0
    try:
        return max(0, int(monthly_wins.get(target_month, 0)))
    except (TypeError, ValueError):
        return 0


def team_total_wins(team_data: dict[str, Any]) -> int:
    monthly_wins = team_data.get("monthly_wins", {})
    if not isinstance(monthly_wins, dict):
        return 0

    total_wins = 0
    for wins in monthly_wins.values():
        try:
            total_wins += max(0, int(wins))
        except (TypeError, ValueError):
            continue
    return total_wins


def team_motto(team_data: dict[str, Any]) -> str:
    motto = str(team_data.get("motto", "")).strip()
    return motto if motto else "Aucune devise définie."


def build_embed(title: str, description: str, color: discord.Color) -> discord.Embed:
    return discord.Embed(title=title, description=description, color=color)


def leaderboard_embed(
    guild: discord.Guild,
    division_power_lookup: Callable[[int], float] | None = None,
) -> discord.Embed:
    sorted_teams = sorted(
        teams["teams"].items(),
        key=lambda item: (item[1]["points"], team_total_wins(item[1])),
        reverse=True,
    )

    if not sorted_teams:
        return build_embed("🏆 Leaderboard des équipes", "Aucune équipe n'est configurée pour le moment.", INFO_COLOR)

    embed = build_embed(
        "🏆 Leaderboard des équipes",
        f"Classement général des teams du serveur.\n📆 **Mois en cours : {current_month()}**",
        INFO_COLOR,
    )

    ranking_lines = []
    for index, (_, data) in enumerate(sorted_teams, start=1):
        role = get_team_role(guild, data)
        if not role:
            continue
        ranking_lines.append(
            f"{placement_emoji(index)} **#{index} • {data['emoji']} {role.mention}**\n"
            f"└ `Points: {data['points']}` • `Victoires totales: {team_total_wins(data)}` • `Membres: {len(role.members)}`"
            f" • `Puissance: {(division_power_lookup(role.id) if division_power_lookup else 0.0):.1f}`"
        )

    if not ranking_lines:
        return build_embed(
            "🏆 Leaderboard des équipes",
            "Aucune équipe valide n'est disponible (rôles manquants ou supprimés).",
            INFO_COLOR,
        )

    embed.add_field(name="Classement", value="\n".join(ranking_lines), inline=False)
    best_team_name, best_team_data = sorted_teams[0]
    best_team_role = get_team_role(guild, best_team_data)
    best_team_label = best_team_role.mention if best_team_role else best_team_name.title()
    embed.add_field(
        name="📌 Focus",
        value=(
            f"**Top team actuelle** : {best_team_data['emoji']} {best_team_label}\n"
            f"**Victoires totales** : `{team_total_wins(best_team_data)}`"
        ),
        inline=False,
    )
    refreshed_at = datetime.now(timezone.utc)
    refreshed_label = refreshed_at.strftime("%d/%m/%Y à %H:%M UTC")
    embed.set_footer(
        text=(
            "Utilise /team @nomdelateam pour afficher le détail complet d'une équipe. "
            f"• Dernière actualisation : {refreshed_label}"
        )
    )
    return embed


def team_overview_embed(guild: discord.Guild) -> discord.Embed:
    embed = build_embed("📖 Fiches d'équipes", "Nouvelles cartes d'identité des équipes.", INFO_COLOR)

    sorted_teams = sorted(
        teams["teams"].items(),
        key=lambda item: (item[1]["points"], team_month_wins(item[1])),
        reverse=True,
    )

    for _, data in sorted_teams:
        role = get_team_role(guild, data)
        if not role:
            continue

        embed.add_field(
            name=f"{data['emoji']}  {role.name}",
            value=(
                f"**BLASON**\n{data['emoji']} {data['emoji']} {data['emoji']}\n"
                f"**Devise** : *{team_motto(data)}*\n"
                f"**Points** : `{data['points']}` • **Victoires mois {current_month()}** : `{team_month_wins(data)}`\n"
                f"**Membres** : `{len(role.members)}`\n"
                f"**Staff** :\n{team_staff_mentions(guild, data)}\n"
                f"**Roster** : {format_member_list(role)}"
            ),
            inline=False,
        )

    if not embed.fields:
        embed.description = "Aucune équipe n'est configurée pour le moment."

    return embed


def team_detail_embed(
    guild: discord.Guild,
    role: discord.Role,
    division_power_lookup: Callable[[int], float] | None = None,
) -> discord.Embed:
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
        f"🛡️ Fiche d'équipe • {role.name}",
        f"# {data['emoji']}\n*{team_motto(data)}*",
        INFO_COLOR,
    )
    embed.add_field(name="Blason", value=data["emoji"], inline=True)
    embed.add_field(name="Rôle Discord", value=role.mention, inline=True)
    embed.add_field(name="Effectif", value=f"`{len(role.members)}`", inline=True)
    embed.add_field(name="Points", value=f"`{data['points']}`", inline=True)
    embed.add_field(name=f"Victoires mois {current_month()}", value=f"`{team_month_wins(data)}`", inline=True)
    division_power = division_power_lookup(role.id) if division_power_lookup else 0.0
    embed.add_field(name="Puissance d'équipe", value=f"`{division_power:.1f}`", inline=True)
    embed.add_field(name="Staff", value=team_staff_mentions(guild, data), inline=False)
    embed.add_field(name="Membres", value=members_value, inline=False)
    embed.set_footer(text="Commandes utiles : /team • /setteammotto")
    return embed


def start_duel(team_names: list[str], active_duel: dict[str, Any] | None) -> tuple[bool, str, dict[str, Any] | None]:
    if active_duel is not None:
        return False, "Un affrontement est déjà en cours. Termine-le avec `!win <équipe> [points]` avant d'en lancer un autre.", active_duel

    normalized_names = [name.strip().lower() for name in team_names if name.strip()]
    if len(normalized_names) < 2:
        return False, "Tu dois indiquer au moins deux équipes.", active_duel

    unique_names = list(dict.fromkeys(normalized_names))
    if len(unique_names) < 2:
        return False, "Tu dois choisir au moins deux équipes différentes.", active_duel

    missing_teams = [name for name in unique_names if get_team_entry_by_name(name) is None]
    if missing_teams:
        missing_label = ", ".join(name for name in missing_teams)
        return False, f"Ces équipes n'existent pas : {missing_label}.", active_duel

    new_duel = {"teams": unique_names}
    teams_label = " VS ".join(team.title() for team in unique_names)
    message = (
        f"Affrontement lancé : {teams_label}. "
        "Utilise `!win <équipe|@utilisateur> [points]` pour annoncer le gagnant."
    )
    return True, message, new_duel


def resolve_duel(winner_name: str, points: int, active_duel: dict[str, Any] | None) -> tuple[bool, str, dict[str, Any] | None]:
    if active_duel is None:
        return False, "Aucun affrontement n'est en cours.", active_duel

    if points < 0:
        return False, "Le nombre de points ne peut pas être négatif.", active_duel

    winner = get_team_entry_by_name(winner_name)
    if winner is None:
        return False, "Cette équipe n'existe pas.", active_duel

    duel_teams = set(active_duel.get("teams", []))
    if winner[0] not in duel_teams:
        return False, "L'équipe gagnante doit faire partie de l'affrontement en cours.", active_duel

    winner[1]["points"] += points
    losers = [team_name for team_name in duel_teams if team_name != winner[0]]

    winner_display = winner[0].title()
    loser_display = ", ".join(team_name.title() for team_name in losers)

    if points == 0:
        save_teams()
        return True, (
            f"Match de test validé pour {winner_display} (0 point). "
            "Aucune victoire ou défaite n'a été enregistrée."
        ), None

    month_key = str(current_month())
    winner_monthly = winner[1].setdefault("monthly_wins", {})
    winner_monthly[month_key] = int(winner_monthly.get(month_key, 0)) + 1
    save_teams()

    return True, (
        f"Victoire de {winner_display} ! +{points} point(s). "
        f"Victoire mensuelle enregistrée (mois {current_month()}) pour {winner_display}."
    ), None


def set_team_limit(limit: int) -> None:
    config["max_team_members"] = limit
    save_config()
