import asyncio

from divbot.discord_app import start_discord_bot
from divbot.twitch_app import start_twitch_bot
from divbot.web_panel import start_web_panel


async def main() -> None:
    loop = asyncio.get_running_loop()
    web_panel_task = loop.run_in_executor(None, start_web_panel)
    await asyncio.gather(start_discord_bot(), start_twitch_bot(), web_panel_task)
