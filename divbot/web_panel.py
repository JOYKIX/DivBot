from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from flask import Flask, redirect, render_template, request, url_for

from divbot.common import ALLOWED_RULE_TYPES, config, save_config, save_teams, teams

WEB_HOST = os.getenv("WEB_HOST", "0.0.0.0")
WEB_PORT = int(os.getenv("WEB_PORT", "8080"))

TEMPLATE_DIR = Path(__file__).resolve().parent / "web" / "templates"
STATIC_DIR = Path(__file__).resolve().parent / "web" / "static"

app = Flask(__name__, template_folder=str(TEMPLATE_DIR), static_folder=str(STATIC_DIR))


def _sorted_teams() -> list[tuple[str, dict[str, Any]]]:
    return sorted(
        teams["teams"].items(),
        key=lambda item: (item[1].get("points", 0), item[1].get("wins", 0)),
        reverse=True,
    )


@app.get("/")
def index() -> str:
    return render_template(
        "dashboard.html",
        rules=config["rules"],
        teams=_sorted_teams(),
        max_team_members=config.get("max_team_members", 0),
        allowed_rule_types=sorted(ALLOWED_RULE_TYPES),
    )


@app.post("/rules")
def add_rule() -> Any:
    rule_type = request.form.get("type", "").strip().lower()
    value = request.form.get("value", "").strip()
    role = request.form.get("role", "").strip()

    if rule_type in ALLOWED_RULE_TYPES and value and role:
        config["rules"].append(
            {
                "type": rule_type,
                "value": value,
                "action": "give_role",
                "role": role,
            }
        )
        save_config()

    return redirect(url_for("index"))


@app.post("/rules/<int:rule_index>/delete")
def delete_rule(rule_index: int) -> Any:
    if 0 <= rule_index < len(config["rules"]):
        del config["rules"][rule_index]
        save_config()
    return redirect(url_for("index"))


@app.post("/team-limit")
def update_team_limit() -> Any:
    raw_limit = request.form.get("max_team_members", "0").strip()
    try:
        limit = max(0, int(raw_limit))
    except ValueError:
        limit = config.get("max_team_members", 0)

    config["max_team_members"] = limit
    save_config()
    return redirect(url_for("index"))


@app.post("/teams/<team_name>/points")
def add_points(team_name: str) -> Any:
    team = teams["teams"].get(team_name.lower())
    if team is None:
        return redirect(url_for("index"))

    try:
        delta = int(request.form.get("delta", "0"))
    except ValueError:
        delta = 0

    team["points"] = int(team.get("points", 0)) + delta
    save_teams()
    return redirect(url_for("index"))


@app.post("/teams/<team_name>/motto")
def update_motto(team_name: str) -> Any:
    team = teams["teams"].get(team_name.lower())
    if team is None:
        return redirect(url_for("index"))

    team["motto"] = request.form.get("motto", "").strip()
    save_teams()
    return redirect(url_for("index"))


def start_web_panel() -> None:
    print(f"[WEB] Panel disponible sur http://{WEB_HOST}:{WEB_PORT}")
    app.run(host=WEB_HOST, port=WEB_PORT, debug=False, use_reloader=False)
