import logging
import os

from signalbot import SignalBot

from family_safety_bot.config import Settings
from family_safety_bot.storage import PlaytimeStore
from family_safety_bot.watcher import PlaytimeManager


def main() -> None:
    # Configure application and dependency logging through the standard root
    # logger. This works across signalbot releases without relying on its
    # optional enable_console_logging helper.
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s [%(levelname)s] - %(message)s",
    )
    # Keep network/request tracing out of normal logs unless explicitly enabled.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("signalbot").setLevel(logging.WARNING)

    settings = Settings.from_env()
    store = PlaytimeStore(os.path.join(settings.data_dir, "playtime.sqlite3"))

    bot = SignalBot(
        {
            "signal_service": os.environ["SIGNAL_SERVICE"],
            "phone_number": os.environ["PHONE_NUMBER"],
            # Use signalbot's SQLite storage for internal state
            "storage": {
                "type": "sqlite",
                "db": os.path.join(settings.data_dir, "signalbot.sqlite3"),
            },
        }
    )

    # Register the playtime manager for the designated group
    manager = PlaytimeManager(settings=settings, store=store)
    manager.bot = bot
    manager.setup()
    bot.register(
        manager,
        contacts=False,
        groups=[settings.signal_group_id],
    )
    
    logging.info("Playtime bot starting...")
    logging.info("  Signal group: %s", settings.signal_group_id)
    logging.info("  Admins:")
    for admin_phone in settings.signal_admins:
        logging.info("    - %s", admin_phone)
    logging.info("  Children:")
    for phone, child in settings.children.items():
        logging.info("    - %s (%s)", child.name, phone)
    logging.info("  Rule profiles are managed through Signal admin commands")
    
    bot.start()


if __name__ == "__main__":
    main()
