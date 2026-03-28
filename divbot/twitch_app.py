import time
from typing import Any

import discord
from twitchio.ext import commands as twitch_commands

from divbot import common
from divbot.common import (
    COOLDOWN,
    GUILD_ID,
    TWITCH_CHANNEL,
    TWITCH_TOKEN,
    cleanup_expired_codes,
    config,
    cooldowns,
    links,
    pending_codes,
    save_links,
    teams,
    unlink_discord_user,
    unlink_twitch_user,
)
from divbot.discord_app import discord_bot, give_role
from divbot.team_logic import build_embed, resolve_duel, start_duel


def is_twitch_admin(author: Any) -> bool:
    return bool(
        getattr(author, "is_broadcaster", False)
        or getattr(author, "is_mod", False)
        or getattr(author, "name", "").lower() == TWITCH_CHANNEL.lower()
    )


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

        if message.author is None or not message.content:
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
            if not isinstance(rule, dict):
                continue

            rule_type = rule.get("type")
            rule_value = str(rule.get("value", ""))
            rule_role = str(rule.get("role", "")).strip()
            if not rule_value or not rule_role:
                continue

            if rule_type == "contains" and rule_value.lower() in msg.lower():
                await give_role(discord_id, rule_role)
            elif rule_type == "emote" and message.tags.get("emotes") and rule_value in msg:
                await give_role(discord_id, rule_role)

    @twitch_commands.command(name="match", aliases=["duel"])
    async def duel_command(self, ctx: twitch_commands.Context, *team_names: str) -> None:
        if not is_twitch_admin(ctx.author):
            await ctx.send("Seuls le streamer ou les modérateurs peuvent lancer un affrontement.")
            return

        selected_teams = list(team_names)
        if not selected_teams:
            selected_teams = list(teams["teams"].keys())

        _, message, new_duel = start_duel(selected_teams, common.active_duel)
        common.active_duel = new_duel
        await ctx.send(message)

    @twitch_commands.command(name="win")
    async def win_command(self, ctx: twitch_commands.Context, winner_reference: str, points: int = 1) -> None:
        if not is_twitch_admin(ctx.author):
            await ctx.send("Seuls le streamer ou les modérateurs peuvent valider une victoire.")
            return

        winner_team_name = winner_reference
        winner_label = winner_reference
        if winner_reference.startswith("@"):
            twitch_username = winner_reference[1:].strip().lower()
            team_name = await self.resolve_team_name_for_twitch_user(twitch_username)
            if team_name is None:
                await ctx.send(
                    "Impossible de trouver une équipe pour cet utilisateur. "
                    "Le compte Twitch doit être lié à Discord et appartenir à une team."
                )
                return
            winner_team_name = team_name
            winner_label = winner_reference

        success, message, new_duel = resolve_duel(winner_team_name, points, common.active_duel)
        common.active_duel = new_duel
        if success and winner_reference.startswith("@"):
            await ctx.send(f"Victoire de {winner_label} ! +{points} point(s) pour {winner_team_name.title()}.")
            return

        await ctx.send(message)

    async def resolve_team_name_for_twitch_user(self, twitch_username: str) -> str | None:
        discord_id = links.get(twitch_username)
        if discord_id is None:
            return None

        guild = discord_bot.get_guild(GUILD_ID)
        if guild is None:
            return None

        member = guild.get_member(discord_id)
        if member is None:
            try:
                member = await guild.fetch_member(discord_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                return None

        for team_name, team_data in teams["teams"].items():
            role_id = team_data.get("role_id")
            if not isinstance(role_id, int):
                continue
            if any(role.id == role_id for role in member.roles):
                return team_name

        return None

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
                    common.SUCCESS_COLOR,
                )
            )
        except discord.Forbidden:
            return


async def start_twitch_bot() -> None:
    bot = TwitchBot()
    await bot.start()
