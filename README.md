# DivBot

Bot Discord + Twitch pour gérer :
- la liaison entre un compte Twitch et un compte Discord ;
- l'attribution automatique de rôles Discord selon les messages Twitch ;
- les équipes, les points et le classement via Discord.

## Structure du projet (version organisée)

Le code est maintenant séparé en modules simples :

- `bot.py` : point d'entrée minimal (lance le bot).
- `divbot/main.py` : démarre Discord + Twitch en parallèle.
- `divbot/common.py` : config `.env`, constantes, JSON et état partagé.
- `divbot/team_logic.py` : logique des équipes, leaderboard et duels.
- `divbot/discord_app.py` : commandes Discord, vues UI et gestion des rôles.
- `divbot/twitch_app.py` : commandes Twitch et liaison Twitch ↔ Discord.

## Fonctionnalités

### Côté Discord
- réponses propres avec des **embeds** ;
- **commandes slash** synchronisées sur le serveur (`GUILD_ID`) ;
- commandes texte principales (`!unlink`, `!rules`, `!leaderboard`, `!teamsinfo`, `!team`) ;
- vue détaillée des équipes avec membres + bilan victoires/défaites ;
- leaderboard avec podium, winrate et focus de la meilleure équipe ;
- gestion des rôles d'encadrement d'équipe (capitaine / vice-capitaine) ;
- panneau de liaison via bouton **Link Discord ↔ Twitch**.

### Commandes slash disponibles
- `/verify`
- `/unlink`
- `/linkpanel`
- `/addrule`
- `/rules`
- `/delrule`
- `/createteam`
- `/addpoints`
- `/teamlimit`
- `/leaderboard`
- `/teams`
- `/team`
- `/setcaptain`
- `/setvicecaptain`

### Commandes Twitch disponibles
- `!link <CODE>`
- `!duel <équipe1> <équipe2> <points>`
- `!win <équipe>`

## Installation

Python **3.11+** recommandé :

```bash
pip install "discord.py>=2.4.0" "twitchio>=2.10.0" "python-dotenv>=1.0.1"
```

## Fichiers JSON utilisés

Le bot crée ou utilise ces fichiers à la racine :
- `links.json`
- `teams.json`
- `config.json`

Ils sont générés automatiquement au premier lancement si besoin.

## Configuration `.env`

Crée un fichier `.env` à la racine :

```env
TWITCH_TOKEN=oauth:remplace_par_ton_token_twitch
TWITCH_CHANNEL=nom_de_ta_chaine
DISCORD_TOKEN=remplace_par_ton_token_discord
GUILD_ID=123456789012345678
```

## Lancement

```bash
python bot.py
```
