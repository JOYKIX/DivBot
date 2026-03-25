import time
from typing import Any

import discord
from twitchio.ext import commands as twitch_commands

from divbot import common
from divbot.common import (
    COOLDOWN,
    TWITCH_CHANNEL,
    TWITCH_TOKEN,
    cleanup_expired_codes,
    config,
    cooldowns,
    links,
    pending_codes,
    save_links,
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

        _, message, new_duel = start_duel(team_one, team_two, points, common.active_duel)
        common.active_duel = new_duel
        await ctx.send(message)

    @twitch_commands.command(name="win")
    async def win_command(self, ctx: twitch_commands.Context, team_name: str) -> None:
        if not is_twitch_admin(ctx.author):
            await ctx.send("Seuls le streamer ou les modérateurs peuvent valider une victoire.")
            return

        _, message, new_duel = resolve_duel(team_name, common.active_duel)
        common.active_duel = new_duel
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
                    common.SUCCESS_COLOR,
                )
            )
        except discord.Forbidden:
            return


async def start_twitch_bot() -> None:
    bot = TwitchBot()
    await bot.start()
