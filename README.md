# DivBot

Bot Discord + Twitch pour gérer :
- la liaison entre un compte Twitch et un compte Discord ;
- l'attribution automatique de rôles Discord selon les messages Twitch ;
- les équipes, les points et le classement via Discord.

## Fonctionnalités ajoutées

### Côté Discord
- réponses plus propres avec des **embeds** ;
- **commandes slash** synchronisées sur le serveur (`GUILD_ID`) ;
- conservation des commandes texte existantes principales (`!verify`, `!unlink`, `!rules`, `!leaderboard`) ;
- messages d'erreur et de confirmation plus lisibles.

### Commandes slash disponibles
- `/verify`
- `/unlink`
- `/addrule`
- `/rules`
- `/delrule`
- `/createteam`
- `/join`
- `/addpoints`
- `/win`
- `/leaderboard`

## Librairies nécessaires

Installe Python **3.11+** recommandé, puis :

```bash
pip install discord.py twitchio python-dotenv
```

Si tu veux figer les versions, tu peux par exemple utiliser :

```bash
pip install "discord.py>=2.4.0" "twitchio>=2.10.0" "python-dotenv>=1.0.1"
```

## Fichiers utilisés

Le bot crée ou utilise ces fichiers JSON dans le dossier du projet :
- `links.json`
- `teams.json`
- `config.json`

Ils sont générés automatiquement au premier lancement si besoin.

## Configuration `.env`

Crée un fichier `.env` à la racine avec ce contenu :

```env
TWITCH_TOKEN=oauth:remplace_par_ton_token_twitch
TWITCH_CHANNEL=nom_de_ta_chaine
DISCORD_TOKEN=remplace_par_ton_token_discord
GUILD_ID=123456789012345678
```

> Le projet contient déjà un `.env` d'exemple modifiable directement.

## Lancer le bot

```bash
python bot.py
```

## Notes importantes

- Les commandes slash sont synchronisées sur le serveur défini par `GUILD_ID`.
- Pour `/addrule`, les types acceptés sont `contains` et `emote`.
- La commande Twitch `!link` génère un code à valider ensuite sur Discord avec `/verify`.
