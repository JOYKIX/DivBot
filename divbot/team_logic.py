from typing import Any

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
            if stored_name == normalized_name
        ),
        None,
    )


def get_team_entry_by_role(role: discord.Role) -> tuple[str, dict[str, Any]] | None:
    return get_team_entry_by_name(role.name)


def get_team_role(guild: discord.Guild, team_data: dict[str, Any]) -> discord.Role | None:
    return guild.get_role(team_data["role_id"])


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
    return int(config.get("max_team_members", 0))


def team_member_limit_label() -> str:
    limit = current_team_member_limit()
    if limit <= 0:
        return "Illimité"
    return str(limit)


def format_rules() -> str:
    if not config["rules"]:
        return f"Aucune règle configurée pour le moment.\nLimite de membres par team : **{team_member_limit_label()}**."

    lines = []
    for index, rule in enumerate(config["rules"]):
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


def team_winrate(team_data: dict[str, Any]) -> float:
    total_matches = team_data["wins"] + team_data["losses"]
    if total_matches <= 0:
        return 0.0
    return (team_data["wins"] / total_matches) * 100


def build_embed(title: str, description: str, color: discord.Color) -> discord.Embed:
    return discord.Embed(title=title, description=description, color=color)


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
                f"**Staff** :\n{team_staff_mentions(guild, data)}\n"
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
    embed.add_field(name="Staff", value=team_staff_mentions(guild, data), inline=False)
    embed.add_field(name="Membres", value=members_value, inline=False)
    embed.set_footer(text="Commande disponible: /team @nomdelateam")
    return embed


def start_duel(team_one_name: str, team_two_name: str, points: int, active_duel: dict[str, Any] | None) -> tuple[bool, str, dict[str, Any] | None]:
    if points <= 0:
        return False, "Le nombre de points doit être supérieur à zéro.", active_duel

    if active_duel is not None:
        return False, "Un duel est déjà en cours. Termine-le avec `!win <équipe>` avant d'en lancer un autre.", active_duel

    team_one = get_team_entry_by_name(team_one_name)
    team_two = get_team_entry_by_name(team_two_name)

    if team_one is None or team_two is None:
        return False, "Une des équipes indiquées n'existe pas.", active_duel

    if team_one[0] == team_two[0]:
        return False, "Tu dois choisir deux équipes différentes.", active_duel

    new_duel = {
        "team_one": team_one[0],
        "team_two": team_two[0],
        "points": points,
    }
    message = (
        f"Duel lancé : **{team_one_name}** VS **{team_two_name}** pour **{points}** point(s). "
        "Utilise `!win <équipe>` pour annoncer le gagnant."
    )
    return True, message, new_duel


def resolve_duel(winner_name: str, active_duel: dict[str, Any] | None) -> tuple[bool, str, dict[str, Any] | None]:
    if active_duel is None:
        return False, "Aucun duel n'est en cours.", active_duel

    winner = get_team_entry_by_name(winner_name)
    if winner is None:
        return False, "Cette équipe n'existe pas.", active_duel

    duel_teams = {active_duel["team_one"], active_duel["team_two"]}
    if winner[0] not in duel_teams:
        return False, "L'équipe gagnante doit faire partie du duel en cours.", active_duel

    loser_key = next(team_name for team_name in duel_teams if team_name != winner[0])
    loser_data = teams["teams"][loser_key]
    duel_points = active_duel["points"]

    winner[1]["points"] += duel_points
    winner[1]["wins"] += 1
    loser_data["losses"] += 1
    save_teams()

    winner_display = winner[0].title()
    loser_display = loser_key.title()
    return True, (
        f"Victoire de **{winner_display}** ! +**{duel_points}** point(s). "
        f"Défaite enregistrée pour **{loser_display}**."
    ), None


def set_team_limit(limit: int) -> None:
    config["max_team_members"] = limit
    save_config()
