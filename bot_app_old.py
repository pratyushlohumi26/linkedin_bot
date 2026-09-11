#!/usr/bin/env python3
"""Legacy entrypoint kept for backward compatibility.

This module now delegates to the refactored package implementation.
"""

from telegram_bot.bot_app import main

if __name__ == "__main__":
    main()
