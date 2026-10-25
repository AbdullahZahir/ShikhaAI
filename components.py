"""
Small custom-drawn UI components that plain CustomTkinter doesn't provide
out of the box: a gradient-filled button, toast notifications (replacing
blocking OS-native messageboxes), an animated spinner, a gradient divider
line, a friendlier "empty state" block, and the ADHD-friendly caption bar.

These are drawn with plain tkinter Canvas + PIL-rendered images rather than
system fonts baked into a bitmap, so there's no dependency on any particular
font file being present on the user's machine - all text uses Tk's normal
font handling.
"""

import os
import tkinter as tk
import tkinter.font

import numpy as np
from PIL import Image, ImageDraw, ImageTk
import customtkinter as ctk

from . import captions as captions_mod
from . import config
from . import icons as icon_mod

# Accessibility > "Reduce motion". Checked at event time rather than when a
# widget is built, so flipping it takes effect everywhere immediately: hover
# effects stop growing/shrinking and the spinner holds still.
REDUCE_MOTION = False


# --------------------------------------------------------------------------
# Easing (used by the hover-grow animations below)
# --------------------------------------------------------------------------
def _ease_out_quad(t):
    return 1 - (1 - t) * (1 - t)


# --------------------------------------------------------------------------
# Color helpers
# --------------------------------------------------------------------------
def _hex_to_rgb(hexcode):
    hexcode = hexcode.lstrip("#")
    return tuple(int(hexcode[i:i + 2], 16) for i in (0, 2, 4))


def _rgb_to_hex(rgb):
    return "#%02X%02X%02X" % rgb


def _lerp_rgb(a, b, t):
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


def _render_gradient_pill(width, height, color_start, color_end, radius):
    """A horizontal-gradient, rounded-rect RGBA image, transparent outside the shape.

    Vectorized with numpy (rather than a per-pixel Python loop) so this is
    cheap enough to call on every frame of a hover-grow animation - a
    ~160x46 image renders in well under a millisecond this way.
    """
    width, height = max(1, int(width)), max(1, int(height))
    c0 = np.array(_hex_to_rgb(color_start), dtype=np.float32)
    c1 = np.array(_hex_to_rgb(color_end), dtype=np.float32)
    t = np.linspace(0.0, 1.0, width, dtype=np.float32).reshape(1, width, 1)
    row = c0.reshape(1, 1, 3) + (c1 - c0).reshape(1, 1, 3) * t
    arr = np.repeat(row, height, axis=0).astype(np.uint8)
    base = Image.fromarray(arr, mode="RGB")

    mask = Image.new("L", (width, height), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, width - 1, height - 1], radius=radius, fill=255)
    out = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    out.paste(base, (0, 0), mask)
    return out


# --------------------------------------------------------------------------
# Generic smooth hover-grow animation for ordinary CTk widgets
# --------------------------------------------------------------------------
def add_hover_grow(widget, base_width, base_height, growth=4, duration_ms=110, steps=6, axis="both"):
    """Smoothly grows a widget by `growth` px on hover, shrinks back on leave.

    Uses `add="+"` when binding so this layers on top of (rather than
    replacing) CTkButton's own built-in hover-color bindings. Meant for
    small/medium fixed-size buttons - widgets that stretch to fill their
    cell (sticky="ew" in a weighted column) won't visibly grow in width, so
    pass axis="height" for those.
    """
    state = {"w": base_width, "h": base_height, "token": 0}

    def apply(w, h):
        kwargs = {}
        if axis in ("both", "width"):
            kwargs["width"] = max(1, round(w))
        if axis in ("both", "height"):
            kwargs["height"] = max(1, round(h))
        try:
            widget.configure(**kwargs)
        except Exception:
            pass

    def animate_to(target_w, target_h):
        state["token"] += 1
        token = state["token"]
        start_w, start_h = state["w"], state["h"]
        step_delay = max(1, duration_ms // steps)

        def step(i):
            if token != state["token"]:
                return
            t = _ease_out_quad(i / steps)
            w = start_w + (target_w - start_w) * t
            h = start_h + (target_h - start_h) * t
            state["w"], state["h"] = w, h
            apply(w, h)
            if i < steps:
                widget.after(step_delay, lambda: step(i + 1))

        step(0)

    def on_enter(_e=None):
        if not REDUCE_MOTION:
            animate_to(base_width + growth, base_height + growth)

    def on_leave(_e=None):
        if (state["w"], state["h"]) != (base_width, base_height):
            animate_to(base_width, base_height)

    widget.bind("<Enter>", on_enter, add="+")
    widget.bind("<Leave>", on_leave, add="+")


# --------------------------------------------------------------------------
# Gradient button
# --------------------------------------------------------------------------
class GradientButton(tk.Canvas):
    """A rounded, gradient-filled CTA button. Use for primary actions only -
    it's meant to draw the eye, so one or two per screen at most."""

    def __init__(self, parent, text="", command=None, width=150, height=42,
                 color_start=None, color_end=None, hover_color_start=None, hover_color_end=None,
                 text_color="#FFFFFF", font=None, mask_color=None, icon_name=None,
                 corner_radius=12, growth=6, duration_ms=110, steps=6):
        mask_color = mask_color or config.COLORS["surface"]
        pad = growth  # extra canvas room so the grown state never gets clipped
        canvas_w, canvas_h = width + pad, height + pad
        super().__init__(parent, width=canvas_w, height=canvas_h, bg=mask_color,
                          highlightthickness=0, bd=0)
        self._command = command
        self._enabled = True
        self.base_width, self.base_height = width, height
        self.growth = growth
        self.duration_ms = duration_ms
        self.steps = steps
        self.current_w, self.current_h = float(width), float(height)
        self._token = 0
        self._hovering = False
        self.corner_radius = corner_radius

        self.color_start = color_start or config.COLORS["accent"]
        self.color_end = color_end or config.COLORS["accent2"]
        self.hover_color_start = hover_color_start or config.COLORS["accent_hover"]
        self.hover_color_end = hover_color_end or self.color_end
        self.disabled_flat = config.COLORS["surface_hi"]

        cx, cy = canvas_w / 2, canvas_h / 2
        self._bg_photo = ImageTk.PhotoImage(_render_gradient_pill(width, height, self.color_start, self.color_end, corner_radius))
        self._bg_item = self.create_image(cx, cy, image=self._bg_photo)

        self._icon_photo = None
        text_center_x = cx
        if icon_name:
            icon_path = os.path.join(os.path.dirname(__file__), "assets", "icons",
                                      f"{icon_name}__on_accent__20.png")
            if os.path.exists(icon_path):
                self._icon_photo = ImageTk.PhotoImage(Image.open(icon_path))
                self.create_image(cx - width / 2 + 28, cy, image=self._icon_photo)
                text_center_x = cx + 10

        font = font or (config.FONT_FAMILY, 13, "bold")
        self._text_item = self.create_text(text_center_x, cy, text=text, fill=text_color, font=font)

        self._disabled_photo = ImageTk.PhotoImage(
            _render_gradient_pill(width, height, self.disabled_flat, self.disabled_flat, corner_radius))

        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)
        self.bind("<Button-1>", self._on_click)

    def _set_bg_photo(self, pil_img):
        self._bg_photo = ImageTk.PhotoImage(pil_img)  # keep a live reference or Tk garbage-collects it
        self.itemconfig(self._bg_item, image=self._bg_photo)

    def _on_enter(self, _event=None):
        if not self._enabled:
            return
        self._hovering = True
        self.configure(cursor="hand2")
        growth = 0 if REDUCE_MOTION else self.growth
        self._animate_to(self.base_width + growth, self.base_height + growth)

    def _on_leave(self, _event=None):
        if not self._enabled:
            return
        self._hovering = False
        self.configure(cursor="")
        self._animate_to(self.base_width, self.base_height)

    def _animate_to(self, target_w, target_h):
        self._token += 1
        token = self._token
        start_w, start_h = self.current_w, self.current_h
        step_delay = max(1, self.duration_ms // self.steps)

        def step(i):
            if token != self._token:
                return
            t = _ease_out_quad(i / self.steps)
            w = start_w + (target_w - start_w) * t
            h = start_h + (target_h - start_h) * t
            self.current_w, self.current_h = w, h
            c_start = self.hover_color_start if self._hovering else self.color_start
            c_end = self.hover_color_end if self._hovering else self.color_end
            self._set_bg_photo(_render_gradient_pill(round(w), round(h), c_start, c_end, self.corner_radius))
            if i < self.steps:
                self.after(step_delay, lambda: step(i + 1))

        step(self.steps if REDUCE_MOTION else 0)

    def _on_click(self, _event=None):
        if self._enabled and self._command:
            self._command()

    def set_enabled(self, enabled):
        self._enabled = enabled
        if not enabled:
            self.itemconfig(self._bg_item, image=self._disabled_photo)
            self.configure(cursor="")
        else:
            self.current_w, self.current_h = float(self.base_width), float(self.base_height)
            self._set_bg_photo(_render_gradient_pill(self.base_width, self.base_height,
                                                       self.color_start, self.color_end, self.corner_radius))

    def set_text(self, text):
        self.itemconfig(self._text_item, text=text)


# --------------------------------------------------------------------------
# Gradient divider (thin accent line under headers)
# --------------------------------------------------------------------------
class GradientDivider(tk.Canvas):
    def __init__(self, parent, height=3, color_start=None, color_end=None, bg=None):
        bg = bg or config.COLORS["bg"]
        super().__init__(parent, height=height, bg=bg, highlightthickness=0, bd=0)
        self._color_start = color_start or config.COLORS["accent"]
        self._color_end = color_end or config.COLORS["accent2"]
        self._height = height
        self.bind("<Configure>", self._redraw)

    def _redraw(self, event):
        width = max(1, event.width)
        self.delete("all")
        c0, c1 = _hex_to_rgb(self._color_start), _hex_to_rgb(self._color_end)
        step = max(1, width // 240)
        for x in range(0, width, step):
            t = x / max(1, width - 1)
            color = _rgb_to_hex(_lerp_rgb(c0, c1, t))
            self.create_rectangle(x, 0, x + step, self._height, fill=color, width=0)


# --------------------------------------------------------------------------
# Animated spinner
# --------------------------------------------------------------------------
class Spinner(ctk.CTkLabel):
    def __init__(self, parent, size=20, color="accent2"):
        super().__init__(parent, text="")
        self._size = size
        self._color = color
        self._angle = 0
        self._running = False

    def start(self):
        if self._running:
            return
        self._running = True
        self._tick()

    def stop(self):
        self._running = False
        self.configure(image=None)  # type: ignore[arg-type]

    def _tick(self):
        if not self._running:
            return
        img = icon_mod.get_rotated_icon("loader-circle", self._color, self._size, self._angle)
        if img is not None:
            self.configure(image=img)
        if not REDUCE_MOTION:
            self._angle = (self._angle + 30) % 360
        self.after(90, self._tick)


# --------------------------------------------------------------------------
# Empty state block
# --------------------------------------------------------------------------
class EmptyState(ctk.CTkFrame):
    def __init__(self, parent, icon_name, title, subtitle=""):
        super().__init__(parent, fg_color="transparent")
        icon_img = icon_mod.get_icon(icon_name, "text_faint", 48)
        ctk.CTkLabel(self, text="", image=icon_img).pack(pady=(36, 12))
        ctk.CTkLabel(self, text=title, font=ctk.CTkFont(config.FONT_FAMILY, 15, "bold"),
                     text_color=config.COLORS["text_dim"]).pack()
        if subtitle:
            ctk.CTkLabel(self, text=subtitle, font=ctk.CTkFont(config.FONT_FAMILY, 12),
                         text_color=config.COLORS["text_faint"], wraplength=320,
                         justify="center").pack(pady=(4, 36))


# --------------------------------------------------------------------------
# ADHD-friendly caption bar
# --------------------------------------------------------------------------
class CaptionBar(ctk.CTkFrame):
    """One short phrase at a time, big and centered, with the word being
    spoken highlighted so attention has one clear place to land. Words
    already said stay bright, words still to come are dimmed.

    Built on a plain tk.Text (rather than a label) because one line has to
    mix colors and weights - e.g. bolding just the start of each word.
    """

    def __init__(self, parent, placeholder="Captions will appear here while the lesson plays."):
        super().__init__(parent, fg_color=config.COLORS["bg_alt"], corner_radius=12)
        self._placeholder = placeholder
        # width=1: let the layout decide the width - Text otherwise asks for 80
        # characters, which at caption sizes would squeeze its neighbours out.
        self._text = tk.Text(
            self, height=2, width=1, wrap="word", bd=0, highlightthickness=0, relief="flat",
            bg=config.COLORS["bg_alt"], fg=config.COLORS["text"], cursor="arrow",
            padx=12, pady=8, takefocus=0, exportselection=False,
            selectbackground=config.COLORS["bg_alt"], inactiveselectbackground=config.COLORS["bg_alt"],
        )
        self._text.pack(fill="both", expand=True, padx=8, pady=6)
        self._shown = None
        self._cue = None
        self._word_index = 0
        self.set_style(config.FONT_FAMILY, config.CAPTION_SIZES[config.DEFAULT_CAPTION_SIZE])

    def set_style(self, family, size, highlight=True, bold_starts=False):
        colors = config.COLORS
        base_weight = "normal" if bold_starts else "bold"
        self._family, self._size, self._bold_starts = family, size, bold_starts
        self._line_px = tk.font.Font(family=family, size=size, weight="bold").metrics("linespace")
        self._text.configure(font=(family, size, base_weight))
        self._text.tag_configure("center", justify="center")
        self._text.tag_configure("spoken", foreground=colors["text"], font=(family, size, base_weight))
        self._text.tag_configure("current", foreground=colors["accent2"] if highlight else colors["text"],
                                 font=(family, size, base_weight))
        self._text.tag_configure("upcoming", foreground=colors["text_dim"] if highlight else colors["text"],
                                 font=(family, size, base_weight))
        self._text.tag_configure("start", font=(family, size, "bold"))
        self._text.tag_configure("speaker", foreground=colors["accent2"], font=(family, max(10, size // 2), "bold"))
        self._text.tag_configure("placeholder", foreground=colors["text_faint"], font=(family, 12))
        self._text.tag_raise("start")
        # Redraw whatever was on screen in the new style.
        self._shown = None
        if self._cue is not None:
            self.show(self._cue, self._word_index)
        else:
            self.clear()

    def show(self, cue, word_index):
        key = (id(cue), word_index)
        if key == self._shown:
            return
        self._shown, self._cue, self._word_index = key, cue, word_index
        self._write(self._insert_cue, cue, word_index)

    def clear(self, message=None):
        """Show `message` in small faint text - the placeholder by default, or
        "" for a blank bar (e.g. a pause between phrases mid-lesson)."""
        message = self._placeholder if message is None else message
        if self._shown == ("clear", message):
            return
        self._shown, self._cue = ("clear", message), None
        self._write(lambda: message and self._text.insert("end", message, ("placeholder",)))

    def _insert_cue(self, cue, word_index):
        words = cue["words"]
        if cue.get("speaker"):
            self._text.insert("end", cue["speaker"].upper() + "   ", ("speaker",))
        for k, word in enumerate(words):
            tag = "spoken" if k < word_index else "current" if k == word_index else "upcoming"
            if self._bold_starts:
                head, tail = captions_mod.bold_split(word["w"])
                self._text.insert("end", head, (tag, "start"))
                self._text.insert("end", tail, (tag,))
            else:
                self._text.insert("end", word["w"], (tag,))
            if k < len(words) - 1:
                self._text.insert("end", " ", (tag,))

    def _write(self, fill, *args):
        text = self._text
        text.configure(state="normal")
        text.delete("1.0", "end")
        fill(*args)
        text.tag_add("center", "1.0", "end")
        # A one-line caption sits in the middle of the two-line box instead of at the top.
        lines = text.count("1.0", "end -1c", "displaylines")
        lines = lines[0] if isinstance(lines, tuple) else (lines or 0)
        text.tag_configure("center", spacing1=self._line_px // 2 if lines <= 1 else 0)
        text.configure(state="disabled")


# --------------------------------------------------------------------------
# Toast notifications (replace blocking messageboxes for non-fatal messages)
# --------------------------------------------------------------------------
class _Toast(ctk.CTkFrame):
    _KIND_STYLE = {
        "error": (config.COLORS["danger"], "circle-alert"),
        "warning": (config.COLORS["warning"], "triangle-alert"),
        "success": (config.COLORS["success"], "circle-check"),
        "info": (config.COLORS["accent2"], "circle-alert"),
    }

    def __init__(self, parent, message, kind, on_close, width=320):
        color, icon_name = self._KIND_STYLE.get(kind, self._KIND_STYLE["info"])
        super().__init__(parent, fg_color=config.COLORS["surface_hi"], corner_radius=12,
                          border_width=1, border_color=color, width=width)
        self.pack_propagate(False)
        self._on_close = on_close

        ctk.CTkFrame(self, fg_color=color, width=4, corner_radius=2).pack(
            side="left", fill="y", padx=(0, 10), pady=8)

        icon_img = icon_mod.get_icon(icon_name, "text", 20)
        if icon_img:
            ctk.CTkLabel(self, text="", image=icon_img).pack(side="left", padx=(2, 8), pady=10)

        ctk.CTkLabel(self, text=message, font=ctk.CTkFont(config.FONT_FAMILY, 12),
                     text_color=config.COLORS["text"], wraplength=270, justify="left",
                     anchor="w").pack(side="left", fill="both", expand=True, padx=(0, 6), pady=10)

        close_img = icon_mod.get_icon("x", "text_dim", 16)
        ctk.CTkButton(self, text="" if close_img else "x", image=close_img, width=24, height=24,
                      fg_color="transparent", hover_color=config.COLORS["surface"],
                      text_color=config.COLORS["text_dim"], command=self._close
                      ).pack(side="right", padx=6)

    def _close(self):
        try:
            self._on_close(self)
        except Exception:
            pass


class ToastManager:
    """Stacks dismissable toast notifications in a window's top-right corner."""

    def __init__(self, root, width=320):
        self.root = root
        self.width = width
        self.active = []

    def show(self, message, kind="info", duration_ms=5000):
        toast = _Toast(self.root, message, kind, on_close=self._remove, width=self.width)
        toast.place(relx=1.0, x=-20, y=20 + 66 * len(self.active), anchor="ne")
        toast.lift()
        self.active.append(toast)
        self.root.after(duration_ms, lambda: self._remove(toast))
        return toast

    def _remove(self, toast):
        if toast not in self.active:
            return
        idx = self.active.index(toast)
        self.active.remove(toast)
        for i, t in enumerate(self.active[idx:], start=idx):
            try:
                t.place_configure(y=20 + 66 * i)
            except Exception:
                pass
        try:
            toast.destroy()
        except Exception:
            pass


# --------------------------------------------------------------------------
# Misc helpers
# --------------------------------------------------------------------------
def bind_hover(widget, normal_color, hover_color):
    """Swap a widget's fg_color on mouse enter/leave (used for library card hover states)."""
    def _enter(_e):
        try:
            widget.configure(fg_color=hover_color)
        except Exception:
            pass

    def _leave(_e):
        try:
            widget.configure(fg_color=normal_color)
        except Exception:
            pass

    widget.bind("<Enter>", _enter)
    widget.bind("<Leave>", _leave)
