import json
import os
import random
import string
import time
import asyncio
import inspect
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import discord
import firebase_admin
from dotenv import load_dotenv
from firebase_admin import credentials, db

BASE_DIR = Path(__file__).resolve().parent.parent
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
    "config.json": {"rules": [], "max_team_members": 0},
    "leaderboard.json": {"channels": {}},
    "team_spam_punishments.json": {"members": {}},
}
FIREBASE_CREDENTIALS_PATH = BASE_DIR / "firebase" / "zogbot-firebase.json"
FIREBASE_DATABASE_URL = os.getenv(
    "FIREBASE_DATABASE_URL",
    "https://zogbot-default-rtdb.europe-west1.firebasedatabase.app/",
)
firebase_enabled = False


def data_path(filename: str) -> Path:
    return BASE_DIR / filename


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
    if firebase_enabled:
        db.reference(filename.removesuffix(".json")).set(data)


def ensure_data_files() -> None:
    for filename, default_value in DATA_FILES.items():
        path = data_path(filename)
        if not path.exists():
            save_json(filename, default_value)


def initialize_firebase() -> None:
    global firebase_enabled
    try:
        if not FIREBASE_CREDENTIALS_PATH.exists():
            print(
                "[FIREBASE] Fichier de credentials introuvable :"
                f" {FIREBASE_CREDENTIALS_PATH}"
            )
            return

        cred = credentials.Certificate(str(FIREBASE_CREDENTIALS_PATH))
        firebase_admin.initialize_app(
            cred,
            {"databaseURL": FIREBASE_DATABASE_URL},
        )
        firebase_enabled = True
        print(f"[FIREBASE] Realtime Database initialisée : {FIREBASE_DATABASE_URL}")
    except ValueError:
        firebase_enabled = True
    except Exception as error:
        print(f"[FIREBASE] Initialisation impossible, fallback local JSON : {error}")


def sync_local_json_to_firebase() -> None:
    if not firebase_enabled:
        return

    for filename, default_value in DATA_FILES.items():
        key = filename.removesuffix(".json")
        ref = db.reference(key)
        firebase_value = ref.get()
        local_value = load_json(filename, default_value)

        if firebase_value is None:
            ref.set(local_value)
            continue

        path = data_path(filename)
        with path.open("w", encoding="utf-8") as file:
            json.dump(firebase_value, file, indent=4, ensure_ascii=False)


def load_data(filename: str, default: Any) -> Any:
    if firebase_enabled:
        firebase_value = db.reference(filename.removesuffix(".json")).get()
        if firebase_value is not None:
            path = data_path(filename)
            with path.open("w", encoding="utf-8") as file:
                json.dump(firebase_value, file, indent=4, ensure_ascii=False)
            return firebase_value
    return load_json(filename, default)


initialize_firebase()
ensure_data_files()
sync_local_json_to_firebase()

links = load_data("links.json", {})
teams = load_data("teams.json", {"teams": {}})
config = load_data("config.json", {"rules": [], "max_team_members": 0})

cooldowns: dict[str, float] = {}
pending_codes: dict[str, dict[str, Any]] = {}
active_duel: dict[str, Any] | None = None
team_update_callbacks: list[Callable[[], Awaitable[None] | None]] = []


def generate_code() -> str:
    return "".join(random.choices(string.ascii_uppercase + string.digits, k=6))


def save_teams() -> None:
    save_json("teams.json", teams)
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return

    loop.create_task(notify_team_updates())


def register_team_update_callback(callback: Callable[[], Awaitable[None] | None]) -> None:
    if callback not in team_update_callbacks:
        team_update_callbacks.append(callback)


async def notify_team_updates() -> None:
    for callback in list(team_update_callbacks):
        try:
            callback_result = callback()
            if inspect.isawaitable(callback_result):
                await callback_result
        except Exception:
            continue


def save_links() -> None:
    save_json("links.json", links)


def save_config() -> None:
    save_json("config.json", config)


def normalize_config_data() -> None:
    global config
    changed = False
    if not isinstance(config, dict):
        config = {}
        changed = True

    if "rules" not in config or not isinstance(config["rules"], list):
        config["rules"] = []
        changed = True
    else:
        normalized_rules = []
        for rule in config["rules"]:
            if not isinstance(rule, dict):
                changed = True
                continue

            trigger_type = str(rule.get("type", "")).strip().lower()
            value = str(rule.get("value", "")).strip()
            role = str(rule.get("role", "")).strip()
            if trigger_type not in ALLOWED_RULE_TYPES or not value or not role:
                changed = True
                continue

            normalized_rules.append(
                {
                    "type": trigger_type,
                    "value": value,
                    "action": "give_role",
                    "role": role,
                }
            )

        if normalized_rules != config["rules"]:
            config["rules"] = normalized_rules
            changed = True

    if "max_team_members" not in config or not isinstance(config["max_team_members"], int):
        config["max_team_members"] = 0
        changed = True

    if config["max_team_members"] < 0:
        config["max_team_members"] = 0
        changed = True

    if changed:
        save_config()


def normalize_team_data() -> None:
    global teams
    changed = False
    if not isinstance(teams, dict):
        teams = {"teams": {}}
        changed = True

    if "teams" not in teams or not isinstance(teams["teams"], dict):
        teams["teams"] = {}
        changed = True

    invalid_team_names = []

    def normalize_int(value: Any, fallback: int = 0) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return fallback

    for team_name, team_data in teams["teams"].items():
        if not isinstance(team_data, dict):
            invalid_team_names.append(team_name)
            changed = True
            continue

        if "role_id" not in team_data or not isinstance(team_data["role_id"], int):
            team_data["role_id"] = 0
            changed = True

        if "points" not in team_data:
            team_data["points"] = 0
            changed = True
        elif not isinstance(team_data["points"], int):
            team_data["points"] = normalize_int(team_data["points"], 0)
            changed = True
        if "emoji" not in team_data:
            team_data["emoji"] = "🏷️"
            changed = True
        if "wins" not in team_data:
            team_data["wins"] = 0
            changed = True
        elif not isinstance(team_data["wins"], int):
            team_data["wins"] = normalize_int(team_data["wins"], 0)
            changed = True
        if "losses" not in team_data:
            team_data["losses"] = 0
            changed = True
        elif not isinstance(team_data["losses"], int):
            team_data["losses"] = normalize_int(team_data["losses"], 0)
            changed = True
        if "captain_id" not in team_data:
            team_data["captain_id"] = None
            changed = True
        if "vice_captain_id" not in team_data:
            team_data["vice_captain_id"] = None
            changed = True
        if "motto" not in team_data:
            team_data["motto"] = ""
            changed = True
        elif not isinstance(team_data["motto"], str):
            team_data["motto"] = str(team_data["motto"])
            changed = True

    for team_name in invalid_team_names:
        del teams["teams"][team_name]

    if changed:
        save_teams()


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


normalize_config_data()
normalize_team_data()
