"""
Loads the pre-rendered icon PNGs in assets/icons/ as CTkImage objects, with
an in-memory cache so the same icon+color+size combo is only ever loaded
from disk once.

Icons were rasterized ahead of time from Lucide (ISC licensed,
https://lucide.dev) at build time - no SVG renderer is required at runtime,
just Pillow (already a moviepy dependency) and CustomTkinter's CTkImage.
"""

import os

from PIL import Image
import customtkinter as ctk

_ASSETS_DIR = os.path.join(os.path.dirname(__file__), "assets", "icons")
_cache = {}


def get_icon(name, color="text", size=20):
    """Return a CTkImage for (name, color, size), or None if it doesn't exist."""
    key = (name, color, size)
    if key in _cache:
        return _cache[key]

    path = os.path.join(_ASSETS_DIR, f"{name}__{color}__{size}.png")
    if not os.path.exists(path):
        _cache[key] = None
        return None

    try:
        pil_img = Image.open(path)
        icon = ctk.CTkImage(light_image=pil_img, dark_image=pil_img, size=(size, size))
    except Exception:
        icon = None
    _cache[key] = icon
    return icon


def get_rotated_icon(name, color, size, angle_degrees):
    """Used for the spinner - rotates a base icon by an arbitrary angle on the fly."""
    key = ("rot", name, color, size, round(angle_degrees))
    if key in _cache:
        return _cache[key]

    path = os.path.join(_ASSETS_DIR, f"{name}__{color}__{size}.png")
    if not os.path.exists(path):
        _cache[key] = None
        return None

    try:
        pil_img = Image.open(path).convert("RGBA").rotate(-angle_degrees, resample=Image.BICUBIC)
        icon = ctk.CTkImage(light_image=pil_img, dark_image=pil_img, size=(size, size))
    except Exception:
        icon = None
    _cache[key] = icon
    return icon
