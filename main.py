import logging
import os

from signalbot import SignalBot, enable_console_logging

from family_safety_bot.config import Settings
from family_safety_bot.formatting import format_duration
from family_safety_bot.storage import PlaytimeStore
from family_safety_bot.watcher import PlaytimeManager


def main() -> None:
    enable_console_logging(logging.WARNING)

    # enable_console_logging() only configures the "signalbot" logger.
    # Configure root logging as well so family_safety_bot logs are visible.
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s [%(levelname)s] - %(message)s",
    )
    # Keep network/request tracing out of normal logs unless explicitly enabled.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    settings = Settings.from_env()
    store = PlaytimeStore(os.path.join(settings.data_dir, "playtime.sqlite3"))

    bot = SignalBot(
        {
            "signal_service": os.environ["SIGNAL_SERVICE"],
            "phone_number": os.environ["PHONE_NUMBER"],
            # Use signalbot's SQLite storage for internal state
            "storage": {
                "type": "sqlite",
                "sqlite_db": os.path.join(settings.data_dir, "signalbot.sqlite3"),
            },
        }
    )

    # Register the playtime manager for the designated group
    manager = PlaytimeManager(settings=settings, store=store)
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
    logging.info("  Default profile: %s", settings.default_rule_profile.name)
    logging.info("  Weekly addition: %s", format_duration(settings.default_rule_profile.weekly_addition_minutes))
    logging.info("  Max bank: %s", format_duration(settings.default_rule_profile.max_bank_minutes))
    logging.info("  Accrued playtime max: %s", format_duration(settings.default_rule_profile.accrued_playtime_max_minutes))
    logging.info("  Break recovery rate: %.1fx", settings.default_rule_profile.break_recovery_rate)
    
    bot.start()


if __name__ == "__main__":
    main()
