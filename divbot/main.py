import asyncio

from divbot.discord_app import start_discord_bot
from divbot.twitch_app import start_twitch_bot


async def main() -> None:
    await asyncio.gather(start_discord_bot(), start_twitch_bot())


if __name__ == "__main__":
    asyncio.run(main())
