"""
Small JSON-backed persistence layer.

The original app kept video/podcast history only in memory, so it vanished
every time the app closed. This module saves history (and user settings) to
disk under ~/.shikhaai so your library survives restarts.
"""

import json
import os
import threading
from datetime import datetime

from . import config

_lock = threading.Lock()


def _read_json(path, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return default


def _write_json_atomic(path, data):
    """Write via a temp file + rename so a crash mid-write can't corrupt the file."""
    tmp_path = path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp_path, path)


class HistoryStore:
    """Tracks generated videos and podcasts, persisted as JSON."""

    def __init__(self, path=None):
        self.path = path or config.HISTORY_FILE
        with _lock:
            self.data = _read_json(self.path, {"videos": [], "podcasts": []})
            self.data.setdefault("videos", [])
            self.data.setdefault("podcasts", [])

    def add(self, kind, topic, filepath, level):
        entry = {
            "topic": topic,
            "path": filepath,
            "level": level,
            "created_at": datetime.now().isoformat(timespec="seconds"),
        }
        with _lock:
            self.data[kind].append(entry)
            _write_json_atomic(self.path, self.data)
        return entry

    def get(self, kind):
        """Return entries newest-first, silently dropping ones whose files vanished."""
        with _lock:
            items = [e for e in self.data.get(kind, []) if os.path.exists(e.get("path", ""))]
            if len(items) != len(self.data.get(kind, [])):
                self.data[kind] = items
                _write_json_atomic(self.path, self.data)
        return list(reversed(items))


class SettingsStore:
    """Tiny key/value settings file (model tier, narrator voice, window prefs)."""

    DEFAULTS = {
        "tier": config.DEFAULT_TIER,
        "narrator_voice": config.DEFAULT_NARRATOR_VOICE,
        "max_scenes": config.MAX_VIDEO_SCENES,
        "music_enabled": config.MUSIC_ENABLED_DEFAULT,
        "music_volume": config.MUSIC_VOLUME_DEFAULT,
        # ADHD-friendly captions
        "captions_enabled": True,
        "caption_size": config.DEFAULT_CAPTION_SIZE,
        "caption_words": config.DEFAULT_CAPTION_WORDS,
        "caption_highlight": True,
        "caption_bold_starts": False,
        # Sensory & motion
        "reduce_motion": False,
        "calm_visuals": False,
        # Lesson style
        "literal_language": False,
        "predictable_structure": False,
        "bite_size": False,
        "calm_hosts": False,
        # Reading
        "reading_font": config.FONT_FAMILY,
        "script_text_size": "Standard",
        "line_spacing": False,
        # Quizzes
        "quiz_questions": "5",
        "quiz_time_limit": "30 seconds",
        "quiz_extra_time": "Standard time",
        "quiz_feedback": config.QUIZ_FEEDBACK_MODES[0],
        "quiz_calm_timer": False,
    }

    def __init__(self, path=None):
        self.path = path or config.SETTINGS_FILE
        with _lock:
            data = _read_json(self.path, {})
        self.data = {**self.DEFAULTS, **data}

    def get(self, key):
        return self.data.get(key, self.DEFAULTS.get(key))

    def set(self, key, value):
        self.data[key] = value
        with _lock:
            _write_json_atomic(self.path, self.data)

    def update(self, **kwargs):
        self.data.update(kwargs)
        with _lock:
            _write_json_atomic(self.path, self.data)


class QuizResultsStore:
    """Every finished quiz's score, so progress can be tracked over time."""

    def __init__(self, path=None):
        self.path = path or config.QUIZ_RESULTS_FILE
        with _lock:
            self.data = _read_json(self.path, [])
        if not isinstance(self.data, list):
            self.data = []

    def add(self, topic, level, score, total, seconds_per_question, elapsed_seconds, timed_out):
        entry = {
            "topic": topic,
            "level": level,
            "score": score,
            "total": total,
            "seconds_per_question": seconds_per_question,
            "elapsed_seconds": round(elapsed_seconds),
            "timed_out": timed_out,
            "created_at": datetime.now().isoformat(timespec="seconds"),
        }
        with _lock:
            self.data.append(entry)
            _write_json_atomic(self.path, self.data)
        return entry

    def recent(self, limit=8):
        """Newest-first."""
        return list(reversed(self.data[-limit:]))
