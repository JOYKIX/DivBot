# ZogBot

Bot Discord + Twitch (anciennement DivBot) pour gérer :
- la liaison entre un compte Twitch et un compte Discord ;
- l'attribution automatique de rôles Discord selon les messages Twitch ;
- les équipes, les points et le classement via Discord.

## Structure du projet

Le code est maintenant séparé en modules simples :

- `bot.py` : point d'entrée minimal (lance le bot).
- `divbot/main.py` : démarre Discord + Twitch en parallèle.
- `divbot/common.py` : config `.env`, constantes, Firebase et état partagé.
- `divbot/team_logic.py` : logique des équipes, leaderboard et duels.
- `divbot/discord_app.py` : commandes Discord, vues UI et gestion des rôles.
- `divbot/twitch_app.py` : commandes Twitch et liaison Twitch ↔ Discord.

## Fonctionnalités

### Côté Discord
- réponses propres avec des **embeds** ;
- **commandes slash** synchronisées sur le serveur (`GUILD_ID`) ;
- vue détaillée des équipes avec membres + bilan victoires/défaites ;
- leaderboard avec podium, winrate et focus de la meilleure équipe ;
- gestion des rôles d'encadrement d'équipe (capitaine / vice-capitaine) ;
- panneau de liaison via bouton **Link Discord ↔ Twitch**.

### Commandes slash disponibles
#### Liaison
- `/link remove`
- `/link panel`

#### Règles
- `/rule list`
- `/rule add`
- `/rule remove`

#### Teams
- `/team list`
- `/team detail`
- `/team leaderboard`
- `/team create`
- `/team delete`
- `/team edit`
- `/team motto`
- `/team points`
- `/team record`
- `/team reset`
- `/team limit`
- `/team captain`
- `/team vicecaptain`

### Commandes Twitch disponibles
- `!link <CODE>`
- `!match <équipe1> <équipe2> [équipe3 ...]` (alias: `!duel`)
- `!win <équipe_gagnante> [points]`

## Installation

Python **3.11+** recommandé :

```bash
pip install "discord.py>=2.4.0" "twitchio>=2.10.0" "python-dotenv>=1.0.1" "firebase-admin>=6.5.0"
```

## Stockage des données (Firebase uniquement)

Le bot utilise **Firebase Realtime Database** comme unique source de vérité :
- `links`
- `teams`
- `config`
- `leaderboard`
- `team_spam_punishments`

Au lancement, le bot :
1. initialise Firebase avec `firebase/zogbot-firebase.json` ;
2. crée automatiquement les clés manquantes avec des valeurs par défaut ;
3. charge ensuite toutes les données directement depuis Firebase.

## Configuration `.env`

Crée un fichier `.env` à la racine :

```env
TWITCH_TOKEN=oauth:remplace_par_ton_token_twitch
TWITCH_CHANNEL=nom_de_ta_chaine
DISCORD_TOKEN=remplace_par_ton_token_discord
GUILD_ID=123456789012345678
FIREBASE_DATABASE_URL=https://zogbot-default-rtdb.europe-west1.firebasedatabase.app/
```

Et place le fichier de service account Firebase dans :

`firebase/zogbot-firebase.json`

## Lancement

```bash
python bot.py
```
