"""
Entry point. Run with:  python -m shikhaai
"""

import os
import sys
import tkinter as tk
from tkinter import messagebox

from dotenv import load_dotenv


def _check_api_key():
    """Fail with a friendly dialog instead of a cryptic crash on first API call."""
    if os.getenv("OPENAI_API_KEY"):
        return True

    root = tk.Tk()
    root.withdraw()
    messagebox.showerror(
        "OpenAI API key not found",
        "ShikhaAI needs an OpenAI API key to generate scripts, images, and audio.\n\n"
        "1. Create a file named '.env' next to this app.\n"
        "2. Add this line to it:\n"
        "   OPENAI_API_KEY=sk-your-key-here\n"
        "3. Restart ShikhaAI.\n\n"
        "Get a key at https://platform.openai.com/api-keys",
    )
    root.destroy()
    return False


def main():
    load_dotenv()
    if not _check_api_key():
        sys.exit(1)

    # Imported after the API-key check so a missing customtkinter/vlc
    # install doesn't hide the more actionable "no API key" message first.
    from .app import ShikhaAIApp

    app = ShikhaAIApp()
    app.mainloop()


if __name__ == "__main__":
    main()
