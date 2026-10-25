"""
Central configuration for ShikhaAI.

Keeping every color, font, model name, and tunable constant in one file means:
  1. The look of the app can be restyled from one place.
  2. When a provider deprecates or renames a model (this happens often -
     DALL-E 2/3 were removed from the OpenAI API in May 2026, for example),
     you only need to update the strings below instead of hunting through
     the whole codebase.
"""

import os

# --------------------------------------------------------------------------
# Paths
# --------------------------------------------------------------------------
APP_DIR = os.path.join(os.path.expanduser("~"), ".shikhaai")
LIBRARY_DIR = os.path.join(APP_DIR, "library")
VIDEOS_DIR = os.path.join(LIBRARY_DIR, "videos")
PODCASTS_DIR = os.path.join(LIBRARY_DIR, "podcasts")
PROJECTS_DIR = os.path.join(APP_DIR, "projects")  # persistent scene images + narration per video, for the storyboard/regenerate-scene feature
HISTORY_FILE = os.path.join(APP_DIR, "history.json")
SETTINGS_FILE = os.path.join(APP_DIR, "settings.json")
QUIZ_RESULTS_FILE = os.path.join(APP_DIR, "quiz_results.json")
ICON_PNG = os.path.join(os.path.dirname(__file__), "assets", "logo.png")
ICON_ICO = os.path.join(os.path.dirname(__file__), "assets", "logo.ico")
BRAND_LOCKUP_PNG = os.path.join(os.path.dirname(__file__), "assets", "brand_lockup.png")

for _d in (APP_DIR, LIBRARY_DIR, VIDEOS_DIR, PODCASTS_DIR, PROJECTS_DIR):
    os.makedirs(_d, exist_ok=True)

# --------------------------------------------------------------------------
# Visual theme  (dark, modern "AI product" look)
# --------------------------------------------------------------------------
COLORS = {
    "bg": "#0B0E14",          # app background
    "bg_alt": "#0F1320",      # sidebar background
    "surface": "#161B29",     # cards / panels
    "surface_hi": "#1E2436",  # hovered / raised surface
    "border": "#262C40",
    "text": "#EDEEF5",
    "text_dim": "#8D93A8",
    "text_faint": "#5B6076",
    "accent": "#7C6CF0",       # violet - primary accent
    "accent_hover": "#6C5AE0",
    "accent_soft": "#241F3D",  # violet tinted surface
    "accent2": "#22D3EE",      # cyan - secondary accent
    "success": "#34D399",
    "danger": "#F0576B",
    "danger_hover": "#D8455A",
    "warning": "#FBBF24",
}

FONT_FAMILY = "Segoe UI"
FONT_FAMILY_MONO = "Consolas"

# --------------------------------------------------------------------------
# Grade level options
# --------------------------------------------------------------------------
GRADE_OPTIONS = {
    "Elementary": ["1st Grade", "2nd Grade", "3rd Grade", "4th Grade", "5th Grade"],
    "Middle School": ["6th Grade", "7th Grade", "8th Grade"],
    "High School": ["9th Grade", "10th Grade", "11th Grade", "12th Grade"],
    "College": ["College Freshman", "College Sophomore", "College Junior", "College Senior"],
}

# --------------------------------------------------------------------------
# AI model configuration
#
# OpenAI retires and renames models fairly often (gpt-3.5-turbo is on its way
# out, DALL-E was removed in favor of the GPT Image family, etc). Everything
# model-related is defined here as named "tiers" so the whole app can switch
# models from the Settings dialog without touching any generation code.
# --------------------------------------------------------------------------
MODEL_TIERS = {
    "Budget": {
        "description": "Cheapest option. Great for testing.",
        "cost_hint": "Lowest cost per lesson",
        "chat_model": "gpt-4.1-nano",
        "image_model": "gpt-image-1-mini",
        "image_quality": "low",
        "tts_model": "gpt-4o-mini-tts",
        "transcribe_model": "whisper-1",
    },
    "Balanced (recommended)": {
        "description": "Best quality-for-cost balance for everyday use.",
        "cost_hint": "A short video typically costs well under $0.50",
        "chat_model": "gpt-4.1-mini",
        "image_model": "gpt-image-1-mini",
        "image_quality": "medium",
        "tts_model": "gpt-4o-mini-tts",
        "transcribe_model": "whisper-1",
    },
    "Higher quality": {
        "description": "Better writing and sharper images, costs more per video.",
        "cost_hint": "Costs more per video than Balanced",
        "chat_model": "gpt-5.4-mini",
        "image_model": "gpt-image-1.5",
        "image_quality": "medium",
        "tts_model": "gpt-4o-mini-tts",
        "transcribe_model": "whisper-1",
    },
}
DEFAULT_TIER = "Balanced (recommended)"

# Narrator voice choices for gpt-4o-mini-tts (video mode)
NARRATOR_VOICES = ["nova", "alloy", "coral", "fable", "onyx", "sage", "shimmer", "verse", "ballad"]
DEFAULT_NARRATOR_VOICE = "nova"

# Fixed two-host podcast voice/personality configuration.
PODCAST_HOSTS = {
    "Shimmer": {
        "voice": "shimmer",
        "instructions": (
            "Speak energetically and playfully, like an upbeat, fun podcast "
            "co-host who loves the topic. Vary your pacing and let enthusiasm "
            "come through."
        ),
    },
    "Echo": {
        "voice": "echo",
        "instructions": (
            "Speak calmly and thoughtfully, like a measured, reflective "
            "podcast co-host. Take your time and sound genuinely curious."
        ),
    },
}

# Cost / time controls
MAX_VIDEO_SCENES = 6          # number of illustrated scenes generated per video
NARRATION_CHUNK_CHARS = 3200  # keeps each TTS call safely under model limits
PODCAST_LINE_CHUNK_CHARS = 1800

# Background music (podcast dialogue only - videos are always narration-only).
# Fully synthesized locally - see music.py - no licensing concerns since
# nothing is downloaded or bundled. Off by default; opt in from Settings.
MUSIC_ENABLED_DEFAULT = False
MUSIC_VOLUME_DEFAULT = 0.12   # quiet bed under dialogue, 0.0-0.4 sane range

# --------------------------------------------------------------------------
# Accessibility & learning support (see the Accessibility page)
# --------------------------------------------------------------------------
# ADHD-friendly captions: short, bold phrases with the spoken word highlighted.
CAPTION_SIZES = {"Medium": 18, "Large": 22, "Extra large": 28}
DEFAULT_CAPTION_SIZE = "Large"
DEFAULT_CAPTION_WORDS = 4     # words on screen at once; fewer = easier to follow

# Fonts offered for reading text (script, captions, quiz). Only the ones
# actually installed on the machine are listed - Atkinson Hyperlegible,
# Lexend and OpenDyslexic are free downloads designed for easier reading.
READING_FONTS = ["Segoe UI", "Atkinson Hyperlegible", "Lexend", "OpenDyslexic", "Verdana", "Comic Sans MS", "Arial"]
SCRIPT_TEXT_SIZES = {"Standard": 13, "Large": 16, "Extra large": 19}

# Timed quizzes
QUIZ_QUESTION_COUNTS = ["5", "8", "10", "15"]
QUIZ_TIME_LIMITS = {
    "15 seconds": 15, "30 seconds": 30, "45 seconds": 45, "60 seconds": 60,
    "90 seconds": 90, "No time limit": 0,
}
QUIZ_EXTRA_TIME = {"Standard time": 1.0, "1.5x time": 1.5, "2x time": 2.0}
QUIZ_FEEDBACK_MODES = ["After each question", "At the end"]

# --------------------------------------------------------------------------
# Misc
# --------------------------------------------------------------------------
APP_NAME = "ShikhaAI"
APP_TAGLINE = "AI Learning Studio"
WINDOW_SIZE = "1320x900"
WINDOW_MIN_SIZE = (1040, 700)
