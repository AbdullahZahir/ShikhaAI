"""
Thin wrapper around python-vlc.

Fixes two real bugs from the original app:
  1. video_player.set_hwnd(...) only works on Windows. Embedding a VLC
     player into a Tk widget needs a different call on Linux (set_xwindow)
     and macOS (set_nsobject). We pick the right one from sys.platform.
  2. If libVLC itself isn't installed on the machine (very easy to hit -
     python-vlc is just bindings, VLC/libVLC is a separate native install),
     the whole app used to crash on startup. Here that failure is caught and
     surfaced as `.available = False` so the rest of the app can keep
     working and just disable in-app playback with a helpful message.
"""

import os
import sys

try:
    import vlc
    _VLC_IMPORT_ERROR = None
except Exception as exc:  # pragma: no cover - depends on host machine
    vlc = None
    _VLC_IMPORT_ERROR = exc


class PlaybackUnavailable(Exception):
    pass


class VLCPlayer:
    """One embeddable VLC player. Create one per video surface / podcast."""

    def __init__(self):
        self.available = False
        self.error = None
        self.instance = None
        self.player = None

        if vlc is None:
            self.error = str(_VLC_IMPORT_ERROR)
            return

        try:
            self.instance = vlc.Instance("--quiet")
            self.player = self.instance.media_player_new()
            self.available = True
        except Exception as exc:
            self.error = str(exc)

    def attach(self, widget):
        """Embed video output into a Tk widget (Label/Frame)."""
        if not self.available:
            return
        widget.update_idletasks()
        wid = widget.winfo_id()
        if sys.platform.startswith("win"):
            self.player.set_hwnd(wid)
        elif sys.platform == "darwin":
            self.player.set_nsobject(wid)
        else:
            self.player.set_xwindow(wid)

    def load(self, path):
        if not self.available:
            raise PlaybackUnavailable(self.error or "Playback engine unavailable.")
        media = self.instance.media_new(os.path.abspath(path))
        self.player.set_media(media)

    def play(self):
        if self.available:
            self.player.play()

    def pause(self):
        if self.available:
            self.player.pause()

    def stop(self):
        if self.available:
            self.player.stop()

    def set_rate(self, rate):
        if self.available:
            self.player.set_rate(float(rate))

    def is_playing(self):
        return bool(self.available and self.player.is_playing())
