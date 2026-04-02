import asyncio
import json
import re
import time
from collections import defaultdict
from pathlib import Path
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
    load_data,
    links,
    pending_codes,
    save_data,
    save_links,
    save_teams,
    teams,
    unlink_discord_user,
    unlink_twitch_user,
)
from divbot.discord_app import announce_team_points, discord_bot, give_role
from divbot.team_logic import build_embed, resolve_duel, start_duel


def is_twitch_admin(author: Any) -> bool:
    return bool(
        getattr(author, "is_broadcaster", False)
        or getattr(author, "is_mod", False)
        or getattr(author, "name", "").lower() == TWITCH_CHANNEL.lower()
    )


class TwitchBot(twitch_commands.Bot):
    ZOGQUIZ_FILE = Path(__file__).resolve().parent / "zogquiz.json"

    def __init__(self) -> None:
        super().__init__(
            token=TWITCH_TOKEN,
            prefix="!",
            initial_channels=[TWITCH_CHANNEL],
        )
        self.active_matchspam: dict[str, Any] | None = None
        self.active_zogquiz: dict[str, Any] | None = None
        self.zogquiz_scores: dict[str, int] = self.load_zogquiz_scores()

    async def event_ready(self) -> None:
        print(f"[TWITCH] Connecté : {self.nick}")

    async def event_message(self, message) -> None:
        if message.echo:
            return

        if message.author is None or not message.content:
            return

        username = message.author.name.lower()
        msg = message.content

        await self.track_matchspam_message(msg)
        await self.track_zogquiz_message(message)

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
            link_message_tail = " ".join(parts[2:])
            linked_team_roles = self.extract_team_roles_from_link_message(link_message_tail)
            await self.delete_twitch_message(message)
            await self.send_link_confirmation_dm(discord_id, username)
            await self.handle_link_team_join(message.channel, username, discord_id, linked_team_roles)
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

    def load_zogquiz_scores(self) -> dict[str, int]:
        raw_scores = load_data("zogquiz_scores", {"scores": {}})
        if not isinstance(raw_scores, dict):
            return {}
        score_entries = raw_scores.get("scores", {})
        if not isinstance(score_entries, dict):
            return {}
        normalized_scores: dict[str, int] = {}
        for discord_id, score_value in score_entries.items():
            cleaned_discord_id = str(discord_id).strip()
            if not cleaned_discord_id:
                continue
            try:
                score = int(score_value)
            except (TypeError, ValueError):
                continue
            if score < 0:
                score = 0
            normalized_scores[cleaned_discord_id] = score
        return normalized_scores

    def save_zogquiz_scores(self) -> None:
        save_data("zogquiz_scores", {"scores": self.zogquiz_scores})

    def normalize_zogquiz_answer(self, raw_answer: str) -> str:
        lowered = raw_answer.strip().lower()
        lowered = re.sub(r"[^\w\s]", " ", lowered, flags=re.UNICODE)
        lowered = re.sub(r"\s+", " ", lowered, flags=re.UNICODE)
        return lowered.strip()

    def load_zogquiz_questions(self) -> list[dict[str, Any]]:
        if not self.ZOGQUIZ_FILE.exists():
            return []
        try:
            with self.ZOGQUIZ_FILE.open("r", encoding="utf-8") as file:
                data = json.load(file)
        except (OSError, json.JSONDecodeError):
            return []

        if not isinstance(data, list):
            return []

        loaded_questions: list[dict[str, Any]] = []
        for item in data:
            if not isinstance(item, dict):
                continue
            question_id = item.get("id")
            raw_answers = item.get("answers", [])
            if not isinstance(question_id, int) or question_id < 1:
                continue
            if not isinstance(raw_answers, list):
                continue
            normalized_answers = {
                self.normalize_zogquiz_answer(str(answer))
                for answer in raw_answers
                if str(answer).strip()
            }
            normalized_answers.discard("")
            if not normalized_answers:
                continue

            loaded_questions.append(
                {
                    "id": question_id,
                    "answers": normalized_answers,
                }
            )

        loaded_questions.sort(key=lambda question: question["id"])
        return loaded_questions

    async def ask_next_zogquiz_question(self, channel) -> None:
        if self.active_zogquiz is None:
            return
        questions: list[dict[str, Any]] = self.active_zogquiz["questions"]
        current_index: int = self.active_zogquiz["index"]
        if current_index >= len(questions):
            self.active_zogquiz = None
            await channel.send("🏁 ZogQuiz terminé ! Utilisez `/zogquiz` sur Discord pour voir le classement.")
            return

        question = questions[current_index]
        await channel.send(f"❓ ZogQuiz Q{question['id']} : donne la réponse attendue dans le chat.")

    async def track_zogquiz_message(self, message) -> None:
        if self.active_zogquiz is None:
            return
        if message.author is None or not message.content:
            return

        if message.content.startswith("!"):
            return

        current_question_index = self.active_zogquiz["index"]
        questions: list[dict[str, Any]] = self.active_zogquiz["questions"]
        if current_question_index >= len(questions):
            return

        question = questions[current_question_index]
        normalized_message = self.normalize_zogquiz_answer(message.content)
        if not normalized_message:
            return

        if normalized_message not in question["answers"]:
            return

        twitch_username = message.author.name.lower()
        linked_discord_id = links.get(twitch_username)
        if linked_discord_id is None:
            return

        linked_discord_id_str = str(linked_discord_id)
        self.zogquiz_scores[linked_discord_id_str] = self.zogquiz_scores.get(linked_discord_id_str, 0) + 1
        self.save_zogquiz_scores()
        self.active_zogquiz["index"] += 1
        await message.channel.send(
            f"✅ {message.author.name} trouve la bonne réponse et gagne 1 point !"
        )
        await self.ask_next_zogquiz_question(message.channel)

    def extract_team_roles_from_link_message(self, link_message_tail: str) -> set[str]:
        normalized_tail = link_message_tail.strip()
        if not normalized_tail:
            return set()

        tail_tokens = normalized_tail.split()
        normalized_tail_lower = normalized_tail.lower()
        matched_roles: set[str] = set()
        for rule in config.get("rules", []):
            if not isinstance(rule, dict):
                continue

            action = str(rule.get("action", "give_role")).strip().lower()
            if action != "give_role":
                continue

            rule_type = str(rule.get("type", "")).strip().lower()
            if rule_type not in {"emote", "contains"}:
                continue

            rule_value = str(rule.get("value", "")).strip()
            role_name = str(rule.get("role", "")).strip()
            if not rule_value or not role_name:
                continue

            if role_name.lower() not in teams["teams"]:
                continue

            if rule_type == "emote" and any(token == rule_value for token in tail_tokens):
                matched_roles.add(role_name)
                continue

            if rule_type == "contains" and rule_value.lower() in normalized_tail_lower:
                matched_roles.add(role_name)

        return matched_roles

    async def handle_link_team_join(
        self,
        channel,
        username: str,
        discord_id: int,
        matched_roles: set[str],
    ) -> None:
        if not matched_roles:
            return

        if len(matched_roles) > 1:
            await channel.send(
                f"{username}, liaison OK ✅ mais emotes invalides pour rejoindre une team : "
                "mets une seule emote de team, ou plusieurs fois la même team."
            )
            return

        target_role = next(iter(matched_roles))
        assigned = await give_role(discord_id, target_role)
        if assigned:
            await channel.send(f"{username}, liaison + team OK ✅ ({target_role}).")

    def get_matchspam_emote_rules(self) -> list[tuple[str, str]]:
        emote_to_team: dict[str, str] = {}
        for rule in config.get("rules", []):
            if not isinstance(rule, dict):
                continue

            action = str(rule.get("action", "give_role")).strip().lower()
            if action != "give_role":
                continue

            rule_type = str(rule.get("type", "")).strip().lower()
            if rule_type not in {"emote", "contains"}:
                continue

            emote = str(rule.get("value", "")).strip()
            role_name = str(rule.get("role", "")).strip().lower()
            if not emote or role_name not in teams["teams"]:
                continue

            emote_to_team[emote] = role_name

        return [(emote, team_name) for emote, team_name in emote_to_team.items()]

    async def track_matchspam_message(self, message_content: str) -> None:
        if self.active_matchspam is None:
            return

        tokens = message_content.split()
        if not tokens:
            return

        emote_rules = self.active_matchspam.get("emote_rules", [])
        if not emote_rules:
            return

        counts: dict[str, int] = self.active_matchspam["counts"]
        for token in tokens:
            for emote, team_name in emote_rules:
                if token == emote:
                    counts[team_name] += 1

    async def finish_matchspam(self, channel) -> None:
        if self.active_matchspam is None:
            return

        matchspam = self.active_matchspam
        self.active_matchspam = None

        counts: dict[str, int] = matchspam["counts"]
        points: int = matchspam["points"]
        ranking = sorted(counts.items(), key=lambda item: item[1], reverse=True)

        if not ranking:
            await channel.send("⏱️ MatchSpam terminé, mais aucune team valide n'a été détectée via les règles emote.")
            return

        lines = [f"• {team_name.title()} : {score} emote(s)" for team_name, score in ranking]
        best_score = ranking[0][1]

        if best_score <= 0:
            await channel.send(
                "⏱️ MatchSpam terminé !\n"
                + "\n".join(lines)
                + "\nAucune emote n'a été envoyée, aucun point attribué."
            )
            return

        winners = [team_name for team_name, score in ranking if score == best_score]
        if len(winners) > 1:
            winners_label = ", ".join(winner.title() for winner in winners)
            await channel.send(
                "⏱️ MatchSpam terminé !\n"
                + "\n".join(lines)
                + f"\nÉgalité entre {winners_label} ({best_score} emote(s)), aucun point attribué."
            )
            return

        winner_team_name = winners[0]
        teams["teams"][winner_team_name]["points"] += points
        save_teams()
        await announce_team_points(winner_team_name, points, reason="matchspam")

        await channel.send(
            "⏱️ MatchSpam terminé !\n"
            + "\n".join(lines)
            + f"\n🏆 {winner_team_name.title()} gagne +{points} point(s) avec {best_score} emote(s) !"
        )

    @twitch_commands.command(name="matchspam")
    async def matchspam_command(self, ctx: twitch_commands.Context, duration_seconds: int, points: int) -> None:
        if not is_twitch_admin(ctx.author):
            await ctx.send("Seuls le streamer ou les modérateurs peuvent lancer un MatchSpam.")
            return

        if duration_seconds <= 0:
            await ctx.send("La durée doit être supérieure à 0 seconde.")
            return

        if points < 0:
            await ctx.send("Le nombre de points ne peut pas être négatif.")
            return

        if self.active_matchspam is not None:
            await ctx.send("Un MatchSpam est déjà en cours.")
            return

        emote_rules = self.get_matchspam_emote_rules()
        if not emote_rules:
            await ctx.send(
                "Aucune règle de déclencheur (`emote` ou `contains`) liée à une team n'est configurée. "
                "Ajoute des règles avec `/rule add` pour les emotes de team."
            )
            return

        counts: defaultdict[str, int] = defaultdict(int)
        for _, team_name in emote_rules:
            counts[team_name] = 0

        self.active_matchspam = {
            "points": points,
            "counts": counts,
            "emote_rules": emote_rules,
        }

        await ctx.send(
            f"🔥 MatchSpam lancé pour {duration_seconds} seconde(s) ! "
            f"La team avec le plus d'emotes gagne +{points} point(s)."
        )
        await asyncio.sleep(duration_seconds)
        await self.finish_matchspam(ctx.channel)

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
            if points == 0:
                await ctx.send(
                    f"Match de test validé pour {winner_label} (0 point) : "
                    "aucune victoire ou défaite enregistrée."
                )
            else:
                await ctx.send(f"Victoire de {winner_label} ! +{points} point(s) pour {winner_team_name.title()}.")
            return

        await ctx.send(message)

    @twitch_commands.command(name="pts")
    async def points_command(self, ctx: twitch_commands.Context, target: str, points: int) -> None:
        if not is_twitch_admin(ctx.author):
            await ctx.send("Seuls le streamer ou les modérateurs peuvent modifier les points.")
            return

        team_name = await self.resolve_team_name_reference(target)
        if team_name is None:
            await ctx.send(
                "Équipe introuvable. Utilise un nom d'équipe valide ou `@utilisateur` lié à une team."
            )
            return

        teams["teams"][team_name]["points"] += points
        save_teams()
        await announce_team_points(team_name, points, reason="ajustement manuel")
        if points >= 0:
            await ctx.send(f"✅ {team_name.title()} gagne +{points} point(s).")
        else:
            await ctx.send(f"✅ {team_name.title()} perd {abs(points)} point(s).")

    @twitch_commands.command(name="sm")
    async def score_match_command(
        self,
        ctx: twitch_commands.Context,
        participant_1: str,
        participant_2: str,
        participant_3: str,
        participant_4: str,
    ) -> None:
        if not is_twitch_admin(ctx.author):
            await ctx.send("Seuls le streamer ou les modérateurs peuvent attribuer les points SM.")
            return

        score_mapping = (
            (participant_1, 5),
            (participant_2, 3),
            (participant_3, 1),
            (participant_4, -1),
        )
        updated_teams: dict[str, int] = defaultdict(int)

        for user_reference, points in score_mapping:
            team_name = await self.resolve_team_name_reference(user_reference)
            if team_name is None:
                await ctx.send(
                    f"Impossible de trouver une team pour {user_reference}. "
                    "Tu peux utiliser `@utilisateur` (lié à Discord) ou directement le nom d'équipe."
                )
                return
            updated_teams[team_name] += points

        for team_name, points in updated_teams.items():
            teams["teams"][team_name]["points"] += points
            await announce_team_points(team_name, points, reason="SM")

        save_teams()
        recap = ", ".join(
            f"{team_name.title()} {points:+d}"
            for team_name, points in sorted(updated_teams.items())
            if points != 0
        )
        await ctx.send(f"✅ SM appliqué : {recap if recap else 'aucun changement de points'}.")

    @twitch_commands.command(name="zogquiz")
    async def zogquiz_command(self, ctx: twitch_commands.Context) -> None:
        if not is_twitch_admin(ctx.author):
            await ctx.send("Seuls le streamer ou les modérateurs peuvent lancer un ZogQuiz.")
            return

        if self.active_zogquiz is not None:
            await ctx.send("Un ZogQuiz est déjà en cours.")
            return

        questions = self.load_zogquiz_questions()
        if not questions:
            await ctx.send(
                "Aucune question ZogQuiz valide (id >= 1) trouvée dans zogquiz.json."
            )
            return

        self.active_zogquiz = {
            "questions": questions,
            "index": 0,
        }
        await ctx.send(
            f"🧠 ZogQuiz lancé ! {len(questions)} question(s). "
            "Le premier compte Twitch lié à Discord qui répond juste gagne 1 point."
        )
        await self.ask_next_zogquiz_question(ctx.channel)

    async def resolve_team_name_reference(self, team_or_user_reference: str) -> str | None:
        normalized_reference = team_or_user_reference.strip()
        if not normalized_reference:
            return None
        if normalized_reference.startswith("@"):
            twitch_username = normalized_reference[1:].strip().lower()
            if not twitch_username:
                return None
            return await self.resolve_team_name_for_twitch_user(twitch_username)

        normalized_team_name = normalized_reference.lower()
        if normalized_team_name in teams["teams"]:
            return normalized_team_name
        return None

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
