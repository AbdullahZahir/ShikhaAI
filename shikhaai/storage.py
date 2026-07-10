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
