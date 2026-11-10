"""Run the bot:  python -m filmbuffs"""
import logging

import discord

from .bot import FilmBuffsBot
from .config import Config


def main() -> None:
    discord.utils.setup_logging(level=logging.INFO, root=True)
    config = Config()
    bot = FilmBuffsBot(config)
    bot.run(config.token, log_handler=None)


if __name__ == "__main__":
    main()
