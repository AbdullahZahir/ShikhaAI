"""
ShikhaAI desktop app - modern UI built on CustomTkinter.

Architecture:
  - All slow work (OpenAI calls, image/audio generation, video assembly)
    happens on a background thread (`_run_pipeline` / `_run_regenerate_scene`
    / `_run_voice_preview`) so the UI never freezes. Progress updates flow
    back to the main thread through a queue.Queue, polled every 100ms via
    `self.after(...)`. Tk/CTk widgets must only ever be touched from the
    main thread, so no widget is touched from inside a worker thread.
  - A threading.Event lets the Cancel button stop the main generation
    pipeline between steps (an in-flight API call can't be aborted
    mid-request, but the loop won't start its next image/audio call once
    cancelled).
  - Video projects persist their scene images + narration under
    ~/.shikhaai/projects/<id>/ (see config.PROJECTS_DIR) so the storyboard
    strip and "regenerate this scene" feature work even after reopening the
    app and revisiting an item from the Library. Podcasts don't need this
    (no per-scene images), so their working files stay in a temp folder
    that's cleaned up immediately after generation.
  - Captions are saved as word timings next to each lesson (see
    captions.py) and drawn by a second `self.after(...)` loop that reads the
    player's position, so they stay in sync at any playback speed.
  - The Quiz page lives in quiz.py; the Accessibility page's options are
    stored in SettingsStore and applied live (see `_apply_accessibility`).
"""

import glob
import json
import os
import queue
import shutil
import sys
import threading
import tkinter as tk
import tkinter.font as tkfont
import uuid

import customtkinter as ctk

from . import ai_services, captions, components, config, icons, music, storage
from .player import VLCPlayer
from .quiz import QuizPage

ctk.set_appearance_mode("dark")


def _font(size=13, weight="normal"):
    return ctk.CTkFont(family=config.FONT_FAMILY, size=size, weight=weight)


def _icon(name, color="text", size=18):
    return icons.get_icon(name, color, size)


# Accessibility > Quick setup. Each preset only turns supports on (so they
# can be combined); "Reset to defaults" is the way back.
_A11Y_PRESETS = {
    "ADHD focus": {
        "captions_enabled": True, "caption_words": 3, "caption_highlight": True,
        "caption_bold_starts": True, "bite_size": True, "predictable_structure": True,
        "quiz_extra_time": "1.5x time",
    },
    "Autism / sensory-friendly": {
        "captions_enabled": True, "reduce_motion": True, "calm_visuals": True, "calm_hosts": True,
        "literal_language": True, "predictable_structure": True, "quiz_calm_timer": True,
    },
    "Dyslexia-friendly": {
        "captions_enabled": True, "caption_size": "Extra large", "script_text_size": "Large",
        "line_spacing": True, "quiz_extra_time": "1.5x time",
    },
}
_A11Y_KEYS = [
    "captions_enabled", "caption_size", "caption_words", "caption_highlight", "caption_bold_starts",
    "reduce_motion", "calm_visuals", "literal_language", "predictable_structure", "bite_size",
    "calm_hosts", "reading_font", "script_text_size", "line_spacing", "quiz_extra_time", "quiz_calm_timer",
]
# Fonts tried, in order, when the Dyslexia-friendly preset picks a reading font.
_DYSLEXIA_FONTS = ["Atkinson Hyperlegible", "Lexend", "OpenDyslexic", "Verdana"]


class ShikhaAIApp(ctk.CTk):
    def __init__(self):
        super().__init__(fg_color=config.COLORS["bg"])
        self.title(config.APP_NAME)
        self.geometry(config.WINDOW_SIZE)
        self.minsize(*config.WINDOW_MIN_SIZE)
        self._set_icon()

        self.history = storage.HistoryStore()
        self.settings = storage.SettingsStore()
        self.quiz_results = storage.QuizResultsStore()
        self.toasts = components.ToastManager(self)
        components.REDUCE_MOTION = bool(self.settings.get("reduce_motion"))

        self.mode = "video"                 # "video" | "podcast"
        self.current_page = None
        self.cancel_event = threading.Event()
        self.worker_thread = None
        self.progress_queue = queue.Queue()
        self.last_video_path = None
        self.last_podcast_path = None
        self.current_video_project = None   # dict: dir/images/scenes/narration_path/out_path
        self.current_lesson = None          # library entry (+ "kind") the Create page is showing - for "Quiz me"
        self.storyboard_regen_buttons = []
        self._voice_preview_busy = False
        self.video_speed = 1.0

        # Captions, per player ("video" / "podcast"): the saved word timings,
        # the phrase track built from them, and - for older lessons with no
        # captions file - the media path still waiting on an estimate.
        self.caption_words = {"video": None, "podcast": None}
        self.caption_tracks = {"video": None, "podcast": None}
        self._caption_pending = {"video": None, "podcast": None}

        self.video_player = VLCPlayer()
        self.podcast_player = VLCPlayer()
        self.preview_player = VLCPlayer()    # dedicated to Settings > voice preview
        if not self.video_player.available:
            print(f"[ShikhaAI] Playback disabled: {self.video_player.error}", file=sys.stderr)

        self._build_layout()
        self._apply_accessibility()
        self._show_page("create")
        self.after(100, self._poll_queue)
        self.after(100, self._tick_captions)
        self.after(500, self._tick_caption_preview)

    # ------------------------------------------------------------------
    # Window chrome
    # ------------------------------------------------------------------
    def _set_icon(self):
        if sys.platform.startswith("win"):
            # Without this, Windows often shows the python.exe/pythonw.exe icon
            # in the taskbar and Alt-Tab switcher instead of the app's own icon,
            # regardless of what Tk's iconphoto/iconbitmap are set to - Windows
            # groups the taskbar button by this ID and falls back to the
            # interpreter's own icon if the process never claims one of its own.
            try:
                import ctypes
                ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("ShikhaAI.DesktopApp")
            except Exception:
                pass
            try:
                if os.path.exists(config.ICON_ICO):
                    self.iconbitmap(default=config.ICON_ICO)
            except Exception:
                pass
        try:
            if os.path.exists(config.ICON_PNG):
                img = tk.PhotoImage(file=config.ICON_PNG)
                self.iconphoto(True, img)
                self._icon_img_ref = img
        except Exception:
            pass  # a missing/broken icon should never crash startup

    def _load_brand_lockup(self, display_width=168):
        """Loads assets/brand_lockup.png (icon + wordmark) sized for the sidebar."""
        try:
            from PIL import Image
            pil_img = Image.open(config.BRAND_LOCKUP_PNG)
            ratio = pil_img.height / pil_img.width
            display_height = max(1, round(display_width * ratio))
            return ctk.CTkImage(light_image=pil_img, dark_image=pil_img,
                                 size=(display_width, display_height))
        except Exception:
            return None

    # ------------------------------------------------------------------
    # Layout
    # ------------------------------------------------------------------
    def _build_layout(self):
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)

        self._build_sidebar()

        self.pages_container = ctk.CTkFrame(self, fg_color=config.COLORS["bg"], corner_radius=0)
        self.pages_container.grid(row=0, column=1, sticky="nsew")
        self.pages_container.grid_rowconfigure(0, weight=1)
        self.pages_container.grid_columnconfigure(0, weight=1)

        self.pages = {}
        self.pages["create"] = self._build_create_page(self.pages_container)
        self.pages["quiz"] = QuizPage(self.pages_container, self)
        self.pages["library"] = self._build_library_page(self.pages_container)
        self.pages["accessibility"] = self._build_accessibility_page(self.pages_container)
        for page in self.pages.values():
            page.grid(row=0, column=0, sticky="nsew")

    def _build_sidebar(self):
        sidebar = ctk.CTkFrame(self, fg_color=config.COLORS["bg_alt"], corner_radius=0, width=220)
        sidebar.grid(row=0, column=0, sticky="nsew")
        sidebar.grid_propagate(False)
        sidebar.grid_rowconfigure(6, weight=1)

        brand = ctk.CTkFrame(sidebar, fg_color="transparent")
        brand.grid(row=0, column=0, sticky="ew", padx=20, pady=(26, 4))
        brand_img = self._load_brand_lockup(display_width=168)
        if brand_img:
            ctk.CTkLabel(brand, text="", image=brand_img).pack(anchor="w")
        else:
            # Fallback if the asset is ever missing - still shows the app name.
            ctk.CTkLabel(
                brand, text=config.APP_NAME, font=_font(20, "bold"),
                text_color=config.COLORS["text"],
            ).pack(anchor="w")
        ctk.CTkLabel(
            brand, text=config.APP_TAGLINE, font=_font(12),
            text_color=config.COLORS["text_dim"],
        ).pack(anchor="w", pady=(4, 0))

        self.nav_buttons = {}
        nav_items = [
            ("create", "Create", "sparkles"), ("quiz", "Quiz", "list-checks"),
            ("library", "Library", "library-big"), ("accessibility", "Accessibility", "person-standing"),
        ]
        for i, (key, label, icon_name) in enumerate(nav_items):
            btn = ctk.CTkButton(
                sidebar, text="  " + label, anchor="w", font=_font(14),
                image=_icon(icon_name, "text_dim", 18),
                fg_color="transparent", hover_color=config.COLORS["surface_hi"],
                text_color=config.COLORS["text_dim"], corner_radius=10, height=40,
                command=lambda k=key: self._show_page(k),
            )
            btn.grid(row=1 + i, column=0, sticky="ew", padx=14, pady=2)
            components.add_hover_grow(btn, 0, 40, growth=3, axis="height")
            self.nav_buttons[key] = btn

        engine_status = "Playback ready" if self.video_player.available else "Playback unavailable"
        status_dot = config.COLORS["success"] if self.video_player.available else config.COLORS["danger"]
        status_row = ctk.CTkFrame(sidebar, fg_color="transparent")
        status_row.grid(row=5, column=0, sticky="w", padx=20, pady=(4, 0))
        ctk.CTkLabel(status_row, text="●", text_color=status_dot, font=_font(10)).pack(side="left")
        ctk.CTkLabel(status_row, text=" " + engine_status, font=_font(11),
                     text_color=config.COLORS["text_faint"]).pack(side="left")

        settings_btn = ctk.CTkButton(
            sidebar, text="  Settings", anchor="w", font=_font(14),
            image=_icon("settings", "text_dim", 18),
            fg_color="transparent", hover_color=config.COLORS["surface_hi"],
            text_color=config.COLORS["text_dim"], corner_radius=10, height=40,
            command=self._open_settings,
        )
        settings_btn.grid(row=7, column=0, sticky="ew", padx=14, pady=(2, 18))
        components.add_hover_grow(settings_btn, 0, 40, growth=3, axis="height")
    def _show_page(self, key):
        if self.current_page == "quiz" and key != "quiz":
            self.pages["quiz"].on_hide()
        self.current_page = key
        for k, btn in self.nav_buttons.items():
            active = k == key
            btn.configure(
                fg_color=config.COLORS["accent_soft"] if active else "transparent",
                text_color=config.COLORS["text"] if active else config.COLORS["text_dim"],
            )
        self.pages[key].tkraise()
        if key == "library":
            self._refresh_library()
        elif key == "quiz":
            self.pages["quiz"].on_show()

    # ------------------------------------------------------------------
    # Shared helpers
    # ------------------------------------------------------------------
    def _card(self, parent, **kwargs):
        defaults = dict(fg_color=config.COLORS["surface"], corner_radius=16)
        defaults.update(kwargs)
        return ctk.CTkFrame(parent, **defaults)

    # ------------------------------------------------------------------
    # Create page
    # ------------------------------------------------------------------
    def _build_create_page(self, parent):
        page = ctk.CTkFrame(parent, fg_color="transparent")
        page.grid_columnconfigure(0, weight=1)
        page.grid_rowconfigure(2, weight=1)

        header = ctk.CTkFrame(page, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", padx=28, pady=(24, 8))
        header.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            header, text="Create something to learn from", font=_font(22, "bold"),
            text_color=config.COLORS["text"],
        ).grid(row=0, column=0, sticky="w")

        self.mode_switch = ctk.CTkSegmentedButton(
            header, values=["🎥  Video + Script", "🎧  Podcast"],
            font=_font(13, "bold"), fg_color=config.COLORS["surface"],
            selected_color=config.COLORS["accent"], selected_hover_color=config.COLORS["accent_hover"],
            unselected_color=config.COLORS["surface"], text_color=config.COLORS["text"],
            command=self._on_mode_switch,
        )
        self.mode_switch.set("🎥  Video + Script")
        self.mode_switch.grid(row=0, column=1, sticky="e")

        components.GradientDivider(page, height=3).grid(row=1, column=0, sticky="ew", padx=28, pady=(0, 14))

        # Everything below scrolls as one column - this is what keeps the
        # video controls and storyboard from ever fighting over a fixed
        # window height. If the window is short, you scroll; nothing is
        # silently clipped off the bottom.
        scroll = ctk.CTkScrollableFrame(
            page, fg_color="transparent", scrollbar_button_color=config.COLORS["surface_hi"],
        )
        scroll.grid(row=2, column=0, sticky="nsew")
        scroll.grid_columnconfigure(0, weight=1)

        # ---- input card ----
        input_card = self._card(scroll)
        input_card.grid(row=0, column=0, sticky="ew", padx=28, pady=(0, 12))
        input_card.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            input_card, text="Topic", font=_font(12, "bold"), text_color=config.COLORS["text_dim"],
        ).grid(row=0, column=0, sticky="w", padx=20, pady=(18, 2))
        self.topic_entry = ctk.CTkEntry(
            input_card, placeholder_text="e.g. Why do volcanoes erupt?",
            font=_font(14), height=42, corner_radius=10,
            fg_color=config.COLORS["bg_alt"], border_color=config.COLORS["border"],
            text_color=config.COLORS["text"],
        )
        self.topic_entry.grid(row=1, column=0, columnspan=2, sticky="ew", padx=20, pady=(0, 14))
        self.topic_entry.bind("<Return>", lambda e: self._on_generate_clicked())

        grade_row = ctk.CTkFrame(input_card, fg_color="transparent")
        grade_row.grid(row=2, column=0, columnspan=2, sticky="ew", padx=20, pady=(0, 4))
        grade_row.grid_columnconfigure((0, 1), weight=1)

        ctk.CTkLabel(grade_row, text="Grade category", font=_font(12, "bold"),
                     text_color=config.COLORS["text_dim"]).grid(row=0, column=0, sticky="w")
        ctk.CTkLabel(grade_row, text="Specific grade", font=_font(12, "bold"),
                     text_color=config.COLORS["text_dim"]).grid(row=0, column=1, sticky="w", padx=(16, 0))

        self.level_var = tk.StringVar(value="Elementary")
        self.specific_var = tk.StringVar(value=config.GRADE_OPTIONS["Elementary"][0])

        self.level_menu = ctk.CTkOptionMenu(
            grade_row, values=list(config.GRADE_OPTIONS.keys()), variable=self.level_var,
            command=self._on_level_change, font=_font(13), fg_color=config.COLORS["bg_alt"],
            button_color=config.COLORS["surface_hi"], button_hover_color=config.COLORS["accent"],
            dropdown_fg_color=config.COLORS["surface"], text_color=config.COLORS["text"],
        )
        self.level_menu.grid(row=1, column=0, sticky="ew", pady=(4, 18))

        self.specific_menu = ctk.CTkOptionMenu(
            grade_row, values=config.GRADE_OPTIONS["Elementary"], variable=self.specific_var,
            font=_font(13), fg_color=config.COLORS["bg_alt"], button_color=config.COLORS["surface_hi"],
            button_hover_color=config.COLORS["accent"], dropdown_fg_color=config.COLORS["surface"],
            text_color=config.COLORS["text"],
        )
        self.specific_menu.grid(row=1, column=1, sticky="ew", padx=(16, 0), pady=(4, 18))

        action_row = ctk.CTkFrame(input_card, fg_color="transparent")
        action_row.grid(row=3, column=0, columnspan=2, sticky="ew", padx=20, pady=(0, 18))
        action_row.grid_columnconfigure(3, weight=1)

        self.generate_btn = components.GradientButton(
            action_row, text="Generate", icon_name="sparkles", width=160, height=42,
            mask_color=config.COLORS["surface"], command=self._on_generate_clicked,
        )
        self.generate_btn.grid(row=0, column=0, sticky="w")

        self.cancel_btn = ctk.CTkButton(
            action_row, text="Cancel", font=_font(13), height=42, width=90,
            corner_radius=10, fg_color=config.COLORS["danger"], hover_color=config.COLORS["danger_hover"],
            command=self._on_cancel_clicked,
        )
        components.add_hover_grow(self.cancel_btn, 90, 42, growth=4)

        self.spinner = components.Spinner(action_row, size=18, color="accent2")

        self.status_label = ctk.CTkLabel(
            action_row, text="", font=_font(12), text_color=config.COLORS["text_dim"], anchor="e",
        )
        self.status_label.grid(row=0, column=3, sticky="e")

        self.progress_bar = ctk.CTkProgressBar(
            input_card, height=6, corner_radius=3, progress_color=config.COLORS["accent2"],
            fg_color=config.COLORS["bg_alt"],
        )
        self.progress_bar.set(0)
        # gridded/ungridded dynamically while a job is running

        # So the cost of a lesson is clear before anything is generated.
        self.cost_hint = ctk.CTkLabel(
            input_card, text="", image=_icon("zap", "text_faint", 16), compound="left",
            font=_font(11), text_color=config.COLORS["text_faint"], anchor="w",
        )
        self.cost_hint.grid(row=5, column=0, columnspan=2, sticky="w", padx=20, pady=(0, 14))
        self._refresh_cost_hint()

        # ---- output area ----
        output_row = ctk.CTkFrame(scroll, fg_color="transparent")
        output_row.grid(row=1, column=0, sticky="ew", padx=28, pady=(0, 12))
        output_row.grid_columnconfigure(0, weight=3)
        output_row.grid_columnconfigure(1, weight=2)
        output_row.grid_rowconfigure(0, weight=1)

        script_card = self._card(output_row)
        script_card.grid(row=0, column=0, sticky="nsew", padx=(0, 10))
        script_card.grid_rowconfigure(1, weight=1)
        script_card.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            script_card, text="Script", font=_font(13, "bold"), text_color=config.COLORS["text_dim"],
        ).grid(row=0, column=0, sticky="w", padx=18, pady=(14, 4))
        self.quiz_me_btn = ctk.CTkButton(
            script_card, text=" Quiz me", image=_icon("list-checks", "text", 16), width=96, height=28,
            font=_font(12), fg_color=config.COLORS["surface_hi"], hover_color=config.COLORS["border"],
            state="disabled", command=self._on_quiz_me_clicked,
        )
        self.quiz_me_btn.grid(row=0, column=1, sticky="e", padx=18, pady=(12, 4))
        components.add_hover_grow(self.quiz_me_btn, 96, 28, growth=3)
        self.script_box = ctk.CTkTextbox(
            script_card, font=self.reading_font(13), fg_color=config.COLORS["bg_alt"],
            text_color=config.COLORS["text"], corner_radius=10, wrap="word",
        )
        self.script_box.grid(row=1, column=0, columnspan=2, sticky="nsew", padx=18, pady=(0, 18))
        self.script_box.configure(state="disabled")

        self.media_card = self._card(output_row)
        self.media_card.grid(row=0, column=1, sticky="nsew", padx=(10, 0))
        self._build_media_panel(self.media_card)

        # ---- storyboard strip (video mode only) ----
        self.storyboard_card = self._card(scroll)
        self._build_storyboard_section(self.storyboard_card)
        self.storyboard_card.grid(row=2, column=0, sticky="ew", padx=28, pady=(0, 24))

        return page

    def _build_media_panel(self, parent):
        parent.grid_rowconfigure(1, weight=1)
        parent.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            parent, text="Preview", font=_font(13, "bold"), text_color=config.COLORS["text_dim"],
        ).grid(row=0, column=0, sticky="w", padx=18, pady=(14, 4))

        # Both sub-panels are gridded into the same cell and swapped with
        # tkraise() (rather than pack/pack_forget) because `parent` already
        # manages its other children with grid - Tk doesn't allow mixing
        # pack and grid for siblings under the same parent.
        self.video_panel_frame = ctk.CTkFrame(parent, fg_color="transparent")
        self.video_panel_frame.grid(row=1, column=0, sticky="nsew")
        self.video_surface = tk.Frame(self.video_panel_frame, bg="black", height=400)
        self.video_surface.pack(fill="x", padx=18)
        self.video_surface.pack_propagate(False)
        # Packed/unpacked by _apply_caption_prefs() depending on the captions setting.
        self.video_captions = components.CaptionBar(self.video_panel_frame)

        video_controls = ctk.CTkFrame(self.video_panel_frame, fg_color="transparent")
        video_controls.pack(fill="x", padx=18, pady=12)
        self.play_btn = ctk.CTkButton(
            video_controls, text=" Play", width=86, height=36, state="disabled",
            image=_icon("play", "on_accent", 16),
            fg_color=config.COLORS["accent"], hover_color=config.COLORS["accent_hover"],
            command=self._play_video,
        )
        self.play_btn.pack(side="left", padx=(0, 6))
        components.add_hover_grow(self.play_btn, 86, 36)
        self.pause_btn = ctk.CTkButton(
            video_controls, text=" Pause", width=96, height=36, state="disabled",
            image=_icon("pause", "text", 16),
            fg_color=config.COLORS["surface_hi"], hover_color=config.COLORS["border"],
            command=self._pause_video,
        )
        self.pause_btn.pack(side="left", padx=6)
        components.add_hover_grow(self.pause_btn, 96, 36)
        self.resume_btn = ctk.CTkButton(
            video_controls, text=" Resume", width=96, height=36, state="disabled",
            image=_icon("rotate-ccw", "text", 16),
            fg_color=config.COLORS["surface_hi"], hover_color=config.COLORS["border"],
            command=self._resume_video,
        )
        self.resume_btn.pack(side="left", padx=6)
        components.add_hover_grow(self.resume_btn, 96, 36)

        # Videos can be slowed down (or sped up) too, same as podcasts.
        video_speed_row = ctk.CTkFrame(self.video_panel_frame, fg_color="transparent")
        video_speed_row.pack(fill="x", padx=18, pady=(0, 14))
        ctk.CTkLabel(video_speed_row, text="Speed", font=_font(11), text_color=config.COLORS["text_faint"]).pack(side="left")
        for spd in ["0.75", "1.0", "1.25", "1.5", "2.0"]:
            spd_btn = ctk.CTkButton(
                video_speed_row, text=f"{spd}x", width=40, height=26, font=_font(11),
                fg_color=config.COLORS["surface_hi"], hover_color=config.COLORS["accent"],
                command=lambda s=spd: self._set_video_speed(s),
            )
            spd_btn.pack(side="left", padx=3)
            components.add_hover_grow(spd_btn, 40, 26, growth=3)
        self.video_cc_btn = self._build_cc_button(video_speed_row)

        # --- podcast sub-panel ---
        self.podcast_panel_frame = ctk.CTkFrame(parent, fg_color="transparent")
        self.podcast_panel_frame.grid(row=1, column=0, sticky="nsew")
        podcast_icon_area = ctk.CTkFrame(
            self.podcast_panel_frame, fg_color=config.COLORS["bg_alt"], corner_radius=12, height=400,
        )
        podcast_icon_area.pack(fill="x", padx=18)
        podcast_icon_area.pack_propagate(False)
        self.podcast_icon_area = podcast_icon_area
        self.podcast_label = ctk.CTkLabel(
            podcast_icon_area, text="", image=_icon("headphones", "accent2", 56),
        )
        self.podcast_label.pack(expand=True)
        self.podcast_captions = components.CaptionBar(self.podcast_panel_frame)
        self.podcast_name_label = ctk.CTkLabel(
            self.podcast_panel_frame, text="No podcast generated yet", font=_font(12),
            text_color=config.COLORS["text_dim"], wraplength=260,
        )
        self.podcast_name_label.pack(fill="x", padx=18, pady=(6, 0))

        podcast_controls = ctk.CTkFrame(self.podcast_panel_frame, fg_color="transparent")
        podcast_controls.pack(fill="x", padx=18, pady=8)
        self.play_podcast_btn = ctk.CTkButton(
            podcast_controls, text=" Play", width=86, height=36, state="disabled",
            image=_icon("play", "on_accent", 16),
            fg_color=config.COLORS["accent"], hover_color=config.COLORS["accent_hover"],
            command=self._play_podcast,
        )
        self.play_podcast_btn.pack(side="left", padx=(0, 6))
        components.add_hover_grow(self.play_podcast_btn, 86, 36)
        self.pause_podcast_btn = ctk.CTkButton(
            podcast_controls, text=" Pause", width=96, height=36, state="disabled",
            image=_icon("pause", "text", 16),
            fg_color=config.COLORS["surface_hi"], hover_color=config.COLORS["border"],
            command=self._pause_podcast,
        )
        self.pause_podcast_btn.pack(side="left", padx=6)
        components.add_hover_grow(self.pause_podcast_btn, 96, 36)
        self.resume_podcast_btn = ctk.CTkButton(
            podcast_controls, text=" Resume", width=96, height=36, state="disabled",
            image=_icon("rotate-ccw", "text", 16),
            fg_color=config.COLORS["surface_hi"], hover_color=config.COLORS["border"],
            command=self._resume_podcast,
        )
        self.resume_podcast_btn.pack(side="left", padx=6)
        components.add_hover_grow(self.resume_podcast_btn, 96, 36)

        speed_row = ctk.CTkFrame(self.podcast_panel_frame, fg_color="transparent")
        speed_row.pack(fill="x", padx=18, pady=(0, 14))
        ctk.CTkLabel(speed_row, text="Speed", font=_font(11), text_color=config.COLORS["text_faint"]).pack(side="left")
        self.podcast_speed = 1.0
        for spd in ["0.75", "1.0", "1.25", "1.5", "2.0"]:
            spd_btn = ctk.CTkButton(
                speed_row, text=f"{spd}x", width=40, height=26, font=_font(11),
                fg_color=config.COLORS["surface_hi"], hover_color=config.COLORS["accent"],
                command=lambda s=spd: self._set_podcast_speed(s),
            )
            spd_btn.pack(side="left", padx=3)
            components.add_hover_grow(spd_btn, 40, 26, growth=3)
        self.podcast_cc_btn = self._build_cc_button(speed_row)

        self._show_video_panel()

    def _build_cc_button(self, parent):
        """Quick on/off toggle for captions, next to the playback speed buttons."""
        btn = ctk.CTkButton(
            parent, text=" CC", image=_icon("captions", "text", 16), width=60, height=26, font=_font(11, "bold"),
            fg_color=config.COLORS["surface_hi"], hover_color=config.COLORS["accent_hover"],
            command=self._toggle_captions,
        )
        btn.pack(side="right")
        components.add_hover_grow(btn, 60, 26, growth=3)
        return btn

    def _show_video_panel(self):
        self.video_panel_frame.tkraise()

    def _show_podcast_panel(self):
        self.podcast_panel_frame.tkraise()

    # ------------------------------------------------------------------
    # Storyboard section
    # ------------------------------------------------------------------
    def _build_storyboard_section(self, parent):
        header = ctk.CTkFrame(parent, fg_color="transparent")
        header.pack(fill="x", padx=18, pady=(14, 6))
        icon_img = _icon("image", "text_dim", 16)
        ctk.CTkLabel(header, text=" Storyboard", image=icon_img, compound="left",
                     font=_font(13, "bold"), text_color=config.COLORS["text_dim"]).pack(side="left")

        self.storyboard_scroll = ctk.CTkScrollableFrame(
            parent, orientation="horizontal", fg_color="transparent", height=110,
            scrollbar_button_color=config.COLORS["surface_hi"],
        )
        self.storyboard_scroll.pack(fill="x", padx=10, pady=(0, 14))
        self._render_storyboard(None)

    def _render_storyboard(self, project):
        for widget in self.storyboard_scroll.winfo_children():
            widget.destroy()
        self.storyboard_regen_buttons = []

        if self.mode != "video":
            ctk.CTkLabel(
                self.storyboard_scroll, text="Storyboard is available in video mode.",
                font=_font(12), text_color=config.COLORS["text_faint"],
            ).pack(padx=10, pady=40)
            return

        if not project or not project.get("images"):
            components.EmptyState(
                self.storyboard_scroll, "image", "No storyboard yet",
                "Generate a video to see its individual scenes here.",
            ).pack(fill="both", expand=True)
            return

        self._thumb_photos = getattr(self, "_thumb_photos", {})
        for i, img_path in enumerate(project["images"]):
            cell = ctk.CTkFrame(self.storyboard_scroll, fg_color=config.COLORS["bg_alt"], corner_radius=10)
            cell.grid(row=0, column=i, padx=6, pady=6)

            thumb_img = self._load_thumbnail(img_path, i)
            thumb_btn = ctk.CTkButton(
                cell, text="", image=thumb_img, width=68, height=68, fg_color="transparent",
                hover_color=config.COLORS["surface_hi"], corner_radius=8,
                command=lambda idx=i: self._open_scene_viewer(idx),
            )
            thumb_btn.pack(padx=6, pady=(6, 2))

            row2 = ctk.CTkFrame(cell, fg_color="transparent")
            row2.pack(pady=(0, 6))
            ctk.CTkLabel(row2, text=f"Scene {i + 1}", font=_font(10), text_color=config.COLORS["text_faint"]).pack(side="left", padx=(0, 6))
            regen_btn = ctk.CTkButton(
                row2, text="", image=_icon("refresh-cw", "accent2", 14), width=22, height=22,
                fg_color="transparent", hover_color=config.COLORS["surface_hi"],
                command=lambda idx=i: self._on_regenerate_scene(idx),
            )
            regen_btn.pack(side="left")
            components.add_hover_grow(regen_btn, 22, 22, growth=3)
            self.storyboard_regen_buttons.append(regen_btn)

    def _load_thumbnail(self, img_path, cache_key):
        try:
            from PIL import Image
            pil_img = Image.open(img_path)
            pil_img.thumbnail((68, 68))
            photo = ctk.CTkImage(light_image=pil_img, dark_image=pil_img, size=pil_img.size)
            self._thumb_photos[cache_key] = photo
            return photo
        except Exception:
            return None

    def _open_scene_viewer(self, index):
        project = self.current_video_project
        if not project or index >= len(project["images"]):
            return
        from PIL import Image

        dialog = ctk.CTkToplevel(self)
        dialog.title(f"Scene {index + 1}")
        dialog.configure(fg_color=config.COLORS["bg"])
        dialog.transient(self)

        try:
            pil_img = Image.open(project["images"][index])
            pil_img.thumbnail((480, 480))
            big_photo = ctk.CTkImage(light_image=pil_img, dark_image=pil_img, size=pil_img.size)
            ctk.CTkLabel(dialog, text="", image=big_photo).pack(padx=16, pady=16)
        except Exception:
            ctk.CTkLabel(dialog, text="Couldn't load this image.").pack(padx=16, pady=16)

        scene_text = project["scenes"][index] if index < len(project.get("scenes", [])) else ""
        if scene_text:
            ctk.CTkLabel(dialog, text=scene_text, font=_font(12), text_color=config.COLORS["text_dim"],
                         wraplength=440, justify="left").pack(padx=16, pady=(0, 12))

        btn_row = ctk.CTkFrame(dialog, fg_color="transparent")
        btn_row.pack(pady=(0, 16))
        regen_scene_btn = ctk.CTkButton(
            btn_row, text=" Regenerate this scene", image=_icon("refresh-cw", "on_accent", 16),
            fg_color=config.COLORS["accent"], hover_color=config.COLORS["accent_hover"],
            command=lambda: (dialog.destroy(), self._on_regenerate_scene(index)),
        )
        regen_scene_btn.pack(side="left", padx=6)
        components.add_hover_grow(regen_scene_btn, 190, 28, growth=4)
        close_btn = ctk.CTkButton(
            btn_row, text="Close", fg_color=config.COLORS["surface_hi"],
            hover_color=config.COLORS["border"], command=dialog.destroy,
        )
        close_btn.pack(side="left", padx=6)
        components.add_hover_grow(close_btn, 70, 28, growth=4)

    def _on_regenerate_scene(self, index):
        if self.worker_thread and self.worker_thread.is_alive():
            self.toasts.show("Please wait for the current job to finish.", kind="warning")
            return
        project = self.current_video_project
        if not project:
            self.toasts.show("No storyboard loaded for this video.", kind="warning")
            return

        tier = config.MODEL_TIERS[self.settings.get("tier")]
        calm_visuals = bool(self.settings.get("calm_visuals"))
        ken_burns = not self.settings.get("reduce_motion")
        self._set_generating_ui(True, working_text="Regenerating...")
        self.worker_thread = threading.Thread(
            target=self._run_regenerate_scene, args=(project, index, tier, calm_visuals, ken_burns), daemon=True,
        )
        self.worker_thread.start()

    def _run_regenerate_scene(self, project, index, tier, calm_visuals, ken_burns):
        try:
            scene_text = project["scenes"][index]
            image_path = project["images"][index]
            self._progress_cb(0, 1, f"Regenerating scene {index + 1}...")
            ai_services.regenerate_scene_image(scene_text, image_path, tier["image_model"], tier["image_quality"],
                                               calm_visuals=calm_visuals)
            self._progress_cb(0, 1, "Rebuilding video...")
            ai_services.assemble_video(
                project["images"], project["narration_path"], project["out_path"],
                progress_cb=self._progress_cb, ken_burns=ken_burns,
            )
            self.progress_queue.put(("scene_regenerated", index, project["out_path"]))
        except ai_services.GenerationError as exc:
            self.progress_queue.put(("error", str(exc)))
        except Exception as exc:
            self.progress_queue.put(("error", f"Unexpected error: {exc}"))

    # ------------------------------------------------------------------
    # Library page
    # ------------------------------------------------------------------
    def _build_library_page(self, parent):
        page = ctk.CTkFrame(parent, fg_color="transparent")
        page.grid_columnconfigure(0, weight=1)
        page.grid_rowconfigure(3, weight=1)

        header = ctk.CTkFrame(page, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", padx=28, pady=(24, 8))
        ctk.CTkLabel(
            header, text="Your library", font=_font(22, "bold"), text_color=config.COLORS["text"],
        ).pack(side="left")

        self.library_switch = ctk.CTkSegmentedButton(
            page, values=["🎥 Videos", "🎧 Podcasts"], font=_font(13, "bold"),
            fg_color=config.COLORS["surface"], selected_color=config.COLORS["accent"],
            selected_hover_color=config.COLORS["accent_hover"], unselected_color=config.COLORS["surface"],
            text_color=config.COLORS["text"], command=lambda _v: self._refresh_library(),
        )
        self.library_switch.set("🎥 Videos")
        self.library_switch.grid(row=1, column=0, sticky="w", padx=28, pady=(0, 6))

        components.GradientDivider(page, height=3).grid(row=2, column=0, sticky="ew", padx=28, pady=(0, 10))

        toolbar = ctk.CTkFrame(page, fg_color="transparent")
        toolbar.grid(row=3, column=0, sticky="ew", padx=28, pady=(0, 10))
        toolbar.grid_columnconfigure(0, weight=1)

        search_frame = ctk.CTkFrame(toolbar, fg_color=config.COLORS["surface"], corner_radius=10, height=38)
        search_frame.grid(row=0, column=0, sticky="ew", padx=(0, 10))
        search_icon = _icon("search", "text_faint", 16)
        if search_icon:
            ctk.CTkLabel(search_frame, text="", image=search_icon).pack(side="left", padx=(10, 4))
        self.library_search_var = tk.StringVar(value="")
        search_entry = ctk.CTkEntry(
            search_frame, placeholder_text="Search your library...", textvariable=self.library_search_var,
            font=_font(12), fg_color="transparent", border_width=0, text_color=config.COLORS["text"],
        )
        search_entry.pack(side="left", fill="both", expand=True, padx=(0, 10), pady=6)
        search_entry.bind("<KeyRelease>", lambda e: self._refresh_library())

        self.library_grade_filter = tk.StringVar(value="All grades")
        ctk.CTkOptionMenu(
            toolbar, values=["All grades"] + list(config.GRADE_OPTIONS.keys()),
            variable=self.library_grade_filter, command=lambda _v: self._refresh_library(),
            font=_font(12), width=140, fg_color=config.COLORS["surface"],
            button_color=config.COLORS["surface_hi"], button_hover_color=config.COLORS["accent"],
            dropdown_fg_color=config.COLORS["surface"], text_color=config.COLORS["text"],
        ).grid(row=0, column=1, padx=(0, 10))

        sort_box = ctk.CTkFrame(toolbar, fg_color="transparent")
        sort_box.grid(row=0, column=2)
        sort_icon = _icon("arrow-up-down", "text_dim", 14)
        if sort_icon:
            ctk.CTkLabel(sort_box, text="", image=sort_icon).pack(side="left", padx=(0, 4))
        self.library_sort_var = tk.StringVar(value="Newest first")
        ctk.CTkOptionMenu(
            sort_box, values=["Newest first", "Oldest first", "A-Z"],
            variable=self.library_sort_var, command=lambda _v: self._refresh_library(),
            font=_font(12), width=130, fg_color=config.COLORS["surface"],
            button_color=config.COLORS["surface_hi"], button_hover_color=config.COLORS["accent"],
            dropdown_fg_color=config.COLORS["surface"], text_color=config.COLORS["text"],
        ).pack(side="left")

        self.library_scroll = ctk.CTkScrollableFrame(
            page, fg_color="transparent", scrollbar_button_color=config.COLORS["surface_hi"],
        )
        self.library_scroll.grid(row=4, column=0, sticky="nsew", padx=28, pady=(0, 24))
        self.library_scroll.grid_columnconfigure(0, weight=1)
        page.grid_rowconfigure(4, weight=1)

        return page

    def _refresh_library(self):
        for widget in self.library_scroll.winfo_children():
            widget.destroy()

        kind = "videos" if "Video" in self.library_switch.get() else "podcasts"
        items = self.history.get(kind)

        query = self.library_search_var.get().strip().lower()
        grade_filter = self.library_grade_filter.get()
        if query:
            items = [e for e in items if query in e["topic"].lower()]
        if grade_filter != "All grades":
            items = [e for e in items if e["level"] in config.GRADE_OPTIONS.get(grade_filter, [])]

        sort_mode = self.library_sort_var.get()
        if sort_mode == "Oldest first":
            items = list(reversed(items))
        elif sort_mode == "A-Z":
            items = sorted(items, key=lambda e: e["topic"].lower())
        # "Newest first" is already the default order from history.get()

        if not items:
            components.EmptyState(
                self.library_scroll, "inbox", "Nothing here yet",
                "Generate your first video or podcast and it'll show up here.",
            ).grid(row=0, column=0, sticky="nsew", pady=20)
            return

        for i, entry in enumerate(items):
            card = self._card(self.library_scroll, fg_color=config.COLORS["surface"])
            card.grid(row=i, column=0, sticky="ew", pady=6)
            card.grid_columnconfigure(0, weight=1)
            components.bind_hover(card, config.COLORS["surface"], config.COLORS["surface_hi"])

            text_col = ctk.CTkFrame(card, fg_color="transparent")
            text_col.grid(row=0, column=0, sticky="ew", padx=16, pady=12)
            ctk.CTkLabel(
                text_col, text=entry["topic"], font=_font(14, "bold"),
                text_color=config.COLORS["text"], anchor="w",
            ).pack(anchor="w")
            ctk.CTkLabel(
                text_col, text=f'{entry["level"]}  ·  {entry["created_at"]}', font=_font(11),
                text_color=config.COLORS["text_dim"], anchor="w",
            ).pack(anchor="w")

            quiz_btn = ctk.CTkButton(
                card, text=" Quiz", image=_icon("list-checks", "text", 14), width=90, height=32,
                fg_color=config.COLORS["accent_soft"], hover_color=config.COLORS["accent_hover"],
                text_color=config.COLORS["text"],
                command=lambda e=entry, k=kind: self._quiz_on_lesson(dict(e, kind=k)),
            )
            quiz_btn.grid(row=0, column=1, padx=(16, 0))
            components.add_hover_grow(quiz_btn, 90, 32, growth=4)

            play_btn = ctk.CTkButton(
                card, text=" Play", image=_icon("play", "on_accent", 14), width=90, height=32,
                fg_color=config.COLORS["accent"], hover_color=config.COLORS["accent_hover"],
                command=lambda e=entry, k=kind: self._play_from_library(k, e),
            )
            play_btn.grid(row=0, column=2, padx=16)
            components.add_hover_grow(play_btn, 90, 32, growth=4)

    def _play_from_library(self, kind, entry):
        script_path = os.path.splitext(entry["path"])[0] + ".txt"
        script_text = ""
        if os.path.exists(script_path):
            try:
                with open(script_path, "r", encoding="utf-8") as f:
                    script_text = f.read()
            except OSError:
                pass
        self._set_script_text(script_text or "(script not available for this item)")
        self._set_current_lesson(dict(entry, kind=kind))

        if kind == "videos":
            self.mode_switch.set("🎥  Video + Script")
            self._on_mode_switch("🎥  Video + Script")
            self._show_page("create")
            self._load_video(entry["path"])
            self._load_project_for_video(entry["path"])
        else:
            self.mode_switch.set("🎧  Podcast")
            self._on_mode_switch("🎧  Podcast")
            self._show_page("create")
            self._load_podcast(entry["path"], os.path.basename(entry["path"]))

    # ------------------------------------------------------------------
    # Quiz entry points
    # ------------------------------------------------------------------
    def _set_current_lesson(self, lesson):
        self.current_lesson = lesson
        self.quiz_me_btn.configure(state="normal" if lesson else "disabled")

    def _on_quiz_me_clicked(self):
        if self.current_lesson:
            self._quiz_on_lesson(self.current_lesson)

    def _quiz_on_lesson(self, lesson):
        self._show_page("quiz")
        self.pages["quiz"].use_lesson(lesson)

    # ------------------------------------------------------------------
    # Mode / grade dropdown handlers
    # ------------------------------------------------------------------
    def _on_mode_switch(self, value):
        self.mode = "video" if "Video" in value else "podcast"
        if self.mode == "video":
            self._show_video_panel()
        else:
            self._show_podcast_panel()
        self._render_storyboard(self.current_video_project if self.mode == "video" else None)

    def _on_level_change(self, level):
        options = config.GRADE_OPTIONS[level]
        self.specific_menu.configure(values=options)
        self.specific_var.set(options[0])

    # ------------------------------------------------------------------
    # Accessibility page
    # ------------------------------------------------------------------
    def _build_accessibility_page(self, parent):
        page = ctk.CTkFrame(parent, fg_color="transparent")
        page.grid_columnconfigure(0, weight=1)
        page.grid_rowconfigure(2, weight=1)
        self._a11y_vars = {}

        header = ctk.CTkFrame(page, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", padx=28, pady=(24, 8))
        ctk.CTkLabel(
            header, text="Accessibility & learning support", font=_font(22, "bold"),
            text_color=config.COLORS["text"],
        ).pack(side="left")
        ctk.CTkLabel(header, text="Changes save automatically", font=_font(12),
                     text_color=config.COLORS["text_dim"]).pack(side="right")

        components.GradientDivider(page, height=3).grid(row=1, column=0, sticky="ew", padx=28, pady=(0, 14))

        scroll = ctk.CTkScrollableFrame(
            page, fg_color="transparent", scrollbar_button_color=config.COLORS["surface_hi"],
        )
        scroll.grid(row=2, column=0, sticky="nsew")
        scroll.grid_columnconfigure((0, 1), weight=1, uniform="a11y")

        # ---- quick setup ----
        quick = self._a11y_card(
            scroll, 0, 0, "person-standing", "Quick setup",
            "Turn on a group of supports in one click, then fine-tune anything below. "
            "Presets add to each other, so they can be combined.", columnspan=2,
        )
        preset_row = ctk.CTkFrame(quick, fg_color="transparent")
        preset_row.pack(fill="x", padx=18, pady=(0, 16))
        for name, icon_name in [("ADHD focus", "zap"), ("Autism / sensory-friendly", "eye"),
                                ("Dyslexia-friendly", "type")]:
            preset_btn = ctk.CTkButton(
                preset_row, text=" " + name, image=_icon(icon_name, "text", 16), height=34, font=_font(12, "bold"),
                fg_color=config.COLORS["surface_hi"], hover_color=config.COLORS["accent"],
                command=lambda n=name: self._apply_a11y_preset(n),
            )
            preset_btn.pack(side="left", padx=(0, 8))
            components.add_hover_grow(preset_btn, 0, 34, growth=3, axis="height")
        reset_btn = ctk.CTkButton(
            preset_row, text=" Reset to defaults", image=_icon("rotate-ccw", "text_dim", 16), height=34,
            font=_font(12), fg_color="transparent", hover_color=config.COLORS["surface_hi"],
            text_color=config.COLORS["text_dim"], command=self._reset_a11y,
        )
        reset_btn.pack(side="right")

        # ---- ADHD-friendly captions ----
        cap = self._a11y_card(
            scroll, 1, 0, "captions", "ADHD-friendly captions",
            "A few words at a time, big and bold, with the word being spoken highlighted - so attention "
            "has one clear place to land. Shown under videos and podcasts.",
        )
        self.a11y_preview = components.CaptionBar(cap, placeholder=None)
        self.a11y_preview.pack(fill="x", padx=18, pady=(0, 6))
        self._caption_preview_t = 0.0
        self._a11y_switch(cap, "captions_enabled", "Show captions while lessons play")
        self._a11y_option(cap, "caption_size", "Caption size", list(config.CAPTION_SIZES.keys()))
        self._a11y_option(cap, "caption_words", "Words on screen at once", ["2", "3", "4", "5", "6", "8"], cast=int)
        self._a11y_switch(cap, "caption_highlight", "Highlight each word as it's spoken")
        self._a11y_switch(cap, "caption_bold_starts", "Bold word starts",
                          "Bolds the first part of every word to give the eye an anchor point.")
        self._a11y_spacer(cap)

        # ---- sensory & motion ----
        sense = self._a11y_card(
            scroll, 1, 1, "eye", "Sensory & motion",
            "Lower-stimulation settings for autistic learners, and anyone who finds busy screens or "
            "sudden sounds tiring.",
        )
        self._a11y_switch(sense, "reduce_motion", "Reduce motion",
                          "New videos use still scenes instead of slow pan-and-zoom, and nothing in the "
                          "app grows, spins or slides.")
        self._a11y_switch(sense, "calm_visuals", "Calm illustrations",
                          "Soft colors and simple, uncluttered scenes in new videos.")
        self._a11y_switch(sense, "calm_hosts", "Calm podcast hosts",
                          "Both hosts speak gently and evenly - no shouting, teasing or sudden jokes.")
        self._a11y_note(sense, "Background music is already off unless you turn it on in Settings.",
                        pady=(10, 0))
        self._a11y_spacer(sense)

        # ---- lesson style ----
        style = self._a11y_card(
            scroll, 2, 0, "sparkles", "Clear, predictable lessons",
            "Changes how new scripts, podcasts and quiz questions are written.",
        )
        self._a11y_switch(style, "literal_language", "Clear, literal language",
                          "No idioms, sarcasm or figures of speech, and every new word is explained "
                          "the first time it's used.")
        self._a11y_switch(style, "predictable_structure", "Predictable structure",
                          "Each lesson says what's coming, goes step by step, and ends with a short recap.")
        self._a11y_switch(style, "bite_size", "Bite-size lessons",
                          "About a minute long, one idea per scene - easier to stay with from start to finish.")
        self._a11y_spacer(style)

        # ---- reading ----
        read = self._a11y_card(
            scroll, 2, 1, "type", "Reading",
            "For dyslexic learners and anyone who reads more easily with bigger, roomier text. "
            "Applies to the script, captions and quizzes.",
        )
        self._a11y_option(read, "reading_font", "Reading font", self._reading_font_choices(), width=200)
        self._a11y_option(read, "script_text_size", "Script text size", list(config.SCRIPT_TEXT_SIZES.keys()))
        self._a11y_switch(read, "line_spacing", "Extra line spacing in the script")
        self._a11y_note(read, "Atkinson Hyperlegible, Lexend and OpenDyslexic are free fonts made for easier "
                              "reading. Install any of them and they'll appear in this list.", pady=(10, 0))
        self._a11y_spacer(read)

        # ---- quizzes ----
        quiz = self._a11y_card(
            scroll, 3, 0, "timer", "Quizzes",
            "Testing accommodations for timed quizzes.",
        )
        self._a11y_option(quiz, "quiz_extra_time", "Extended time", list(config.QUIZ_EXTRA_TIME.keys()))
        self._a11y_switch(quiz, "quiz_calm_timer", "Calm timer",
                          "A quiet progress bar instead of a ticking countdown, with no warning colors "
                          "as time runs low.")
        self._a11y_note(quiz, "Quizzes can always be paused - pausing hides the question, so a break is a real "
                              "break.", pady=(10, 0))
        self._a11y_spacer(quiz)

        about = self._a11y_card(
            scroll, 3, 1, "circle-alert", "A learning tool, not a clinical one",
            "ShikhaAI doesn't diagnose or treat any condition. These supports work best alongside the "
            "teachers, families and specialists who know each learner.",
        )
        self._a11y_spacer(about)
        ctk.CTkFrame(scroll, fg_color="transparent", height=12).grid(row=4, column=0)

        return page

    def _a11y_card(self, parent, row, column, icon_name, title, subtitle, columnspan=1):
        card = self._card(parent)
        card.grid(row=row, column=column, columnspan=columnspan, sticky="nsew", pady=(0, 12),
                  padx=(28 if column == 0 else 6, 28 if column + columnspan == 2 else 6))
        ctk.CTkLabel(
            card, text=" " + title, image=_icon(icon_name, "accent2", 20), compound="left",
            font=_font(14, "bold"), text_color=config.COLORS["text"],
        ).pack(anchor="w", padx=18, pady=(16, 2))
        self._a11y_note(card, subtitle, size=12, color="text_dim", pady=(0, 10))
        return card

    def _a11y_note(self, card, text, size=11, color="text_faint", padx=(18, 18), pady=(0, 2)):
        """A wrapping line of explanatory text that re-wraps as the card resizes."""
        label = ctk.CTkLabel(card, text=text, font=_font(size), text_color=config.COLORS[color],
                             justify="left", anchor="w", wraplength=360)
        label.pack(anchor="w", fill="x", padx=padx, pady=pady)

        def rewrap(event):
            scale = ctk.ScalingTracker.get_widget_scaling(card)
            label.configure(wraplength=max(160, event.width / scale - padx[0] - padx[1] - 8))
        card.bind("<Configure>", rewrap, add="+")
        return label

    def _a11y_spacer(self, card):
        ctk.CTkFrame(card, fg_color="transparent", height=14).pack()

    def _a11y_switch(self, card, key, label, hint=None):
        var = tk.BooleanVar(value=bool(self.settings.get(key)))
        ctk.CTkSwitch(
            card, text=label, variable=var, onvalue=True, offvalue=False, font=_font(12),
            progress_color=config.COLORS["accent"], text_color=config.COLORS["text_dim"],
            command=lambda: self._update_a11y({key: bool(var.get())}),
        ).pack(anchor="w", padx=18, pady=(8, 0))
        if hint:
            self._a11y_note(card, hint, padx=(66, 18))
        self._a11y_vars[key] = var

    def _a11y_option(self, card, key, label, values, cast=str, width=170):
        row = ctk.CTkFrame(card, fg_color="transparent")
        row.pack(fill="x", padx=18, pady=(8, 0))
        ctk.CTkLabel(row, text=label, font=_font(12), text_color=config.COLORS["text_dim"]).pack(side="left")
        var = tk.StringVar(value=str(self.settings.get(key)))
        ctk.CTkOptionMenu(
            row, values=values, variable=var, width=width, font=_font(12),
            fg_color=config.COLORS["bg_alt"], button_color=config.COLORS["surface_hi"],
            button_hover_color=config.COLORS["accent"], dropdown_fg_color=config.COLORS["surface"],
            text_color=config.COLORS["text"], command=lambda v: self._update_a11y({key: cast(v)}),
        ).pack(side="right")
        self._a11y_vars[key] = var

    def _apply_a11y_preset(self, name):
        changes = dict(_A11Y_PRESETS[name])
        if name == "Dyslexia-friendly":
            font = next((f for f in _DYSLEXIA_FONTS if f in self._installed_fonts()), None)
            if font:
                changes["reading_font"] = font
        self._update_a11y(changes)
        self.toasts.show(f"{name} supports turned on.", kind="success", duration_ms=2500)

    def _reset_a11y(self):
        self._update_a11y({k: storage.SettingsStore.DEFAULTS[k] for k in _A11Y_KEYS})
        self.toasts.show("Accessibility settings reset to defaults.", kind="info", duration_ms=2500)

    def _update_a11y(self, changes):
        """Save accessibility settings, sync the page's controls, and apply them right away."""
        self.settings.update(**changes)
        for key, value in changes.items():
            var = self._a11y_vars.get(key)
            if var is not None:
                var.set(value if isinstance(var, tk.BooleanVar) else str(value))
        self._apply_accessibility()

    def _installed_fonts(self):
        if not hasattr(self, "_font_families"):
            self._font_families = set(tkfont.families(self))
        return self._font_families

    def _reading_font_choices(self):
        installed = self._installed_fonts()
        choices = [f for f in config.READING_FONTS if f in installed]
        current = self.settings.get("reading_font")
        if current not in choices:
            choices.insert(0, current)
        return choices

    def reading_font(self, size=13, weight="normal"):
        """Font for text meant to be read (script, captions, quiz) - set on the Accessibility page."""
        return ctk.CTkFont(family=self.settings.get("reading_font") or config.FONT_FAMILY, size=size, weight=weight)

    def lesson_style(self):
        """The Accessibility page's lesson-writing options, in the shape ai_services expects."""
        return {k: bool(self.settings.get(k))
                for k in ("literal_language", "predictable_structure", "bite_size", "calm_hosts")}

    def _apply_accessibility(self):
        """Push the Accessibility settings out to everything they affect."""
        components.REDUCE_MOTION = bool(self.settings.get("reduce_motion"))
        self._apply_reading_prefs()
        self._apply_caption_prefs()
        self.pages["quiz"].apply_prefs()

    def _apply_reading_prefs(self):
        size = config.SCRIPT_TEXT_SIZES.get(self.settings.get("script_text_size"), 13)
        spacing = round(size * 0.5) if self.settings.get("line_spacing") else 0
        self.script_box.configure(font=self.reading_font(size), spacing1=spacing // 2, spacing2=spacing,
                                  spacing3=spacing // 2)

    def _apply_caption_prefs(self):
        enabled = bool(self.settings.get("captions_enabled"))
        style = dict(
            family=self.settings.get("reading_font") or config.FONT_FAMILY,
            size=config.CAPTION_SIZES.get(self.settings.get("caption_size"), 22),
            highlight=bool(self.settings.get("caption_highlight")),
            bold_starts=bool(self.settings.get("caption_bold_starts")),
        )
        for bar, above in ((self.video_captions, self.video_surface), (self.podcast_captions, self.podcast_icon_area)):
            bar.set_style(**style)
            if enabled and not bar.winfo_manager():
                bar.pack(fill="x", padx=18, pady=(10, 0), after=above)
            elif not enabled and bar.winfo_manager():
                bar.pack_forget()
        for btn in (self.video_cc_btn, self.podcast_cc_btn):
            btn.configure(fg_color=config.COLORS["accent"] if enabled else config.COLORS["surface_hi"])
        for kind in ("video", "podcast"):
            self._rebuild_caption_track(kind)

        self.a11y_preview.set_style(**style)
        self._preview_track = captions.CaptionTrack(
            captions.estimate_words("Plants make their own food from sunlight, water and air.", 0, 5.0),
            int(self.settings.get("caption_words")),
        )

    def _toggle_captions(self):
        self._update_a11y({"captions_enabled": not self.settings.get("captions_enabled")})

    def _tick_caption_preview(self):
        """Plays the sample caption on the Accessibility page, so style changes can be seen."""
        try:
            if self.current_page == "accessibility":
                self._caption_preview_t = (self._caption_preview_t + 0.15) % 6.5
                cue_index, word_index = self._preview_track.at(self._caption_preview_t)
                if cue_index is None:
                    self.a11y_preview.clear("")
                else:
                    self.a11y_preview.show(self._preview_track.cues[cue_index], word_index)
        finally:
            self.after(150, self._tick_caption_preview)

    # ------------------------------------------------------------------
    # Settings dialog
    # ------------------------------------------------------------------
    def _open_settings(self):
        dialog = ctk.CTkToplevel(self)
        dialog.title("Settings")
        dialog.geometry("440x620")
        dialog.configure(fg_color=config.COLORS["bg"])
        dialog.transient(self)
        dialog.grab_set()

        scroll = ctk.CTkScrollableFrame(dialog, fg_color="transparent")
        scroll.pack(fill="both", expand=True, padx=4, pady=4)

        ctk.CTkLabel(scroll, text="Settings", font=_font(18, "bold"),
                     text_color=config.COLORS["text"]).pack(anchor="w", padx=20, pady=(16, 10))

        ctk.CTkLabel(scroll, text="Model tier (cost vs. quality)", font=_font(12, "bold"),
                     text_color=config.COLORS["text_dim"]).pack(anchor="w", padx=20)
        tier_var = tk.StringVar(value=self.settings.get("tier"))
        tier_menu = ctk.CTkOptionMenu(
            scroll, values=list(config.MODEL_TIERS.keys()), variable=tier_var,
            fg_color=config.COLORS["surface"], button_color=config.COLORS["surface_hi"],
            dropdown_fg_color=config.COLORS["surface"], text_color=config.COLORS["text"],
        )
        tier_menu.pack(fill="x", padx=20, pady=(4, 4))
        tier_desc = ctk.CTkLabel(
            scroll, text=config.MODEL_TIERS[tier_var.get()]["description"], font=_font(11),
            text_color=config.COLORS["text_faint"], wraplength=380, justify="left",
        )
        tier_desc.pack(anchor="w", padx=20, pady=(0, 14))

        def _on_tier_change(value):
            tier_desc.configure(text=config.MODEL_TIERS[value]["description"])
        tier_menu.configure(command=_on_tier_change)

        ctk.CTkLabel(scroll, text="Narrator voice (video mode)", font=_font(12, "bold"),
                     text_color=config.COLORS["text_dim"]).pack(anchor="w", padx=20)
        voice_row = ctk.CTkFrame(scroll, fg_color="transparent")
        voice_row.pack(fill="x", padx=20, pady=(4, 4))
        voice_var = tk.StringVar(value=self.settings.get("narrator_voice"))
        ctk.CTkOptionMenu(
            voice_row, values=config.NARRATOR_VOICES, variable=voice_var,
            fg_color=config.COLORS["surface"], button_color=config.COLORS["surface_hi"],
            dropdown_fg_color=config.COLORS["surface"], text_color=config.COLORS["text"],
        ).pack(side="left", fill="x", expand=True, padx=(0, 8))

        preview_spinner = components.Spinner(voice_row, size=16, color="accent2")
        preview_btn = ctk.CTkButton(
            voice_row, text=" Preview", image=_icon("mic", "on_accent", 14), width=100, height=28,
            fg_color=config.COLORS["accent"], hover_color=config.COLORS["accent_hover"],
            command=lambda: self._preview_voice(voice_var.get(), tier_var.get(), preview_btn, preview_spinner),
        )
        preview_btn.pack(side="left")
        components.add_hover_grow(preview_btn, 100, 28, growth=4)
        preview_spinner.pack(side="left", padx=(6, 0))
        ctk.CTkLabel(scroll, text="Uses a tiny bit of API usage each time you preview.",
                     font=_font(10), text_color=config.COLORS["text_faint"]).pack(anchor="w", padx=20, pady=(0, 14))

        ctk.CTkLabel(scroll, text="Illustrated scenes per video", font=_font(12, "bold"),
                     text_color=config.COLORS["text_dim"]).pack(anchor="w", padx=20)
        scenes_var = tk.StringVar(value=str(self.settings.get("max_scenes")))
        ctk.CTkEntry(
            scroll, textvariable=scenes_var, width=80, fg_color=config.COLORS["surface"],
            text_color=config.COLORS["text"],
        ).pack(anchor="w", padx=20, pady=(4, 14))

        # --- background music (podcasts only - videos are narration-only) ---
        music_header = ctk.CTkFrame(scroll, fg_color="transparent")
        music_header.pack(fill="x", padx=20, pady=(0, 2))
        ctk.CTkLabel(music_header, text=" Podcast background music", image=_icon("music", "text_dim", 16),
                     compound="left", font=_font(12, "bold"), text_color=config.COLORS["text_dim"]).pack(side="left")

        music_enabled_var = tk.BooleanVar(value=bool(self.settings.get("music_enabled")))
        ctk.CTkSwitch(
            scroll, text="Add a quiet ambient music bed under podcast dialogue",
            variable=music_enabled_var, onvalue=True, offvalue=False, font=_font(12),
            progress_color=config.COLORS["accent"], text_color=config.COLORS["text_dim"],
        ).pack(anchor="w", padx=20, pady=(4, 4))
        ctk.CTkLabel(scroll, text="Off by default. Videos are always narration-only, no music. Fully "
                     "synthesized locally if turned on - no external audio files, no licensing concerns.",
                     font=_font(10), text_color=config.COLORS["text_faint"], wraplength=380,
                     justify="left").pack(anchor="w", padx=20, pady=(0, 6))

        volume_var = tk.DoubleVar(value=float(self.settings.get("music_volume")))
        ctk.CTkLabel(scroll, text="Volume", font=_font(11), text_color=config.COLORS["text_dim"]).pack(anchor="w", padx=20)
        ctk.CTkSlider(
            scroll, from_=0.0, to=0.3, variable=volume_var, progress_color=config.COLORS["accent"],
            button_color=config.COLORS["accent"], button_hover_color=config.COLORS["accent_hover"],
        ).pack(fill="x", padx=20, pady=(2, 18))

        def _save():
            try:
                max_scenes = max(1, min(12, int(scenes_var.get())))
            except ValueError:
                max_scenes = config.MAX_VIDEO_SCENES
            self.settings.update(
                tier=tier_var.get(), narrator_voice=voice_var.get(), max_scenes=max_scenes,
                music_enabled=bool(music_enabled_var.get()), music_volume=float(volume_var.get()),
            )
            self._refresh_cost_hint()
            dialog.destroy()
            self.toasts.show("Settings saved.", kind="success", duration_ms=2500)

        btn_row = ctk.CTkFrame(scroll, fg_color="transparent")
        btn_row.pack(fill="x", padx=20, pady=(10, 20))
        save_btn = ctk.CTkButton(btn_row, text="Save", command=_save, fg_color=config.COLORS["accent"],
                                  hover_color=config.COLORS["accent_hover"])
        save_btn.pack(side="right")
        components.add_hover_grow(save_btn, 70, 28, growth=4)
        cancel_settings_btn = ctk.CTkButton(btn_row, text="Cancel", command=dialog.destroy,
                                             fg_color=config.COLORS["surface_hi"], hover_color=config.COLORS["border"])
        cancel_settings_btn.pack(side="right", padx=8)
        components.add_hover_grow(cancel_settings_btn, 80, 28, growth=4)

    def _refresh_cost_hint(self):
        tier_name = self.settings.get("tier")
        tier = config.MODEL_TIERS.get(tier_name, config.MODEL_TIERS[config.DEFAULT_TIER])
        self.cost_hint.configure(text=f"  {tier_name} tier  ·  {tier['cost_hint']}  ·  change it in Settings")

    def _preview_voice(self, voice, tier_key, button, spinner):
        if self._voice_preview_busy:
            return
        self._voice_preview_busy = True
        spinner.start()
        tts_model = config.MODEL_TIERS[tier_key]["tts_model"]
        threading.Thread(target=self._run_voice_preview, args=(voice, tts_model, spinner), daemon=True).start()

    def _run_voice_preview(self, voice, tts_model, spinner):
        tmp_dir = os.path.join(config.APP_DIR, "tmp")
        os.makedirs(tmp_dir, exist_ok=True)
        try:
            path = ai_services.generate_voice_sample(
                "Hi there! This is a quick preview of this narrator voice.", voice, tts_model, tmp_dir,
            )
            self.progress_queue.put(("voice_preview_ready", path, spinner))
        except ai_services.GenerationError as exc:
            self.progress_queue.put(("voice_preview_error", str(exc), spinner))
        except Exception as exc:
            self.progress_queue.put(("voice_preview_error", f"Unexpected error: {exc}", spinner))

    # ------------------------------------------------------------------
    # Generation pipeline (runs on a background thread)
    # ------------------------------------------------------------------
    def _on_generate_clicked(self):
        if self.worker_thread and self.worker_thread.is_alive():
            return
        topic = self.topic_entry.get().strip()
        if not topic:
            self.toasts.show("Please enter a topic.", kind="warning")
            return

        level = self.specific_var.get() or self.level_var.get()
        mode = self.mode
        tier = config.MODEL_TIERS[self.settings.get("tier")]
        narrator_voice = self.settings.get("narrator_voice")
        max_scenes = self.settings.get("max_scenes")
        music_enabled = self.settings.get("music_enabled")
        music_volume = self.settings.get("music_volume")
        style = self.lesson_style()
        calm_visuals = bool(self.settings.get("calm_visuals"))
        ken_burns = not self.settings.get("reduce_motion")

        self.cancel_event.clear()
        self._set_generating_ui(True)
        self._set_script_text("Generating... please wait.")
        self._set_current_lesson(None)
        self._render_storyboard(None)
        self.current_video_project = None

        self.worker_thread = threading.Thread(
            target=self._run_pipeline,
            args=(mode, topic, level, tier, narrator_voice, max_scenes, music_enabled, music_volume,
                  style, calm_visuals, ken_burns),
            daemon=True,
        )
        self.worker_thread.start()

    def _on_cancel_clicked(self):
        self.cancel_event.set()
        self.status_label.configure(text="Cancelling...")

    def _set_generating_ui(self, generating, working_text="Working..."):
        if generating:
            self.generate_btn.set_enabled(False)
            self.generate_btn.set_text(working_text)
            self.cancel_btn.grid(row=0, column=1, sticky="w", padx=(8, 0))
            self.spinner.grid(row=0, column=2, sticky="w", padx=(10, 0))
            self.spinner.start()
            self.progress_bar.grid(row=4, column=0, columnspan=2, sticky="ew", padx=20, pady=(0, 18))
            self.progress_bar.set(0)
            for btn in self.storyboard_regen_buttons:
                try:
                    btn.configure(state="disabled")
                except Exception:
                    pass
        else:
            self.generate_btn.set_enabled(True)
            self.generate_btn.set_text("Generate")
            self.cancel_btn.grid_forget()
            self.spinner.stop()
            self.spinner.grid_forget()
            self.progress_bar.grid_forget()
            for btn in self.storyboard_regen_buttons:
                try:
                    btn.configure(state="normal")
                except Exception:
                    pass

    def _progress_cb(self, i, total, message):
        self.progress_queue.put(("progress", i, total, message))

    def _run_pipeline(self, mode, topic, level, tier, narrator_voice, max_scenes, music_enabled, music_volume,
                      style, calm_visuals, ken_burns):
        gen_id = uuid.uuid4().hex
        try:
            if mode == "video":
                project_dir = os.path.join(config.PROJECTS_DIR, gen_id)
                os.makedirs(project_dir, exist_ok=True)

                self._progress_cb(0, 1, "Writing script...")
                script = ai_services.generate_script(topic, level, tier["chat_model"], style=style)
                self.progress_queue.put(("script", script))

                images, scenes = ai_services.generate_images(
                    script, project_dir, tier["image_model"], tier["image_quality"], max_scenes,
                    progress_cb=self._progress_cb, cancel_event=self.cancel_event, calm_visuals=calm_visuals,
                )
                with open(os.path.join(project_dir, "scenes.json"), "w", encoding="utf-8") as f:
                    json.dump(scenes, f)

                narration_final = os.path.join(project_dir, "narration.mp3")
                ai_services.generate_narration_audio(
                    script, narration_final, project_dir, tier["tts_model"], narrator_voice,
                    progress_cb=self._progress_cb, cancel_event=self.cancel_event,
                )
                # Videos are narration-only by design (no background music option
                # here) - it competed with the narration and just sounded like noise.

                self._progress_cb(0, 1, "Timing captions...")
                caption_words = self._time_video_captions(script, narration_final, tier)

                out_path = os.path.join(config.VIDEOS_DIR, f"{gen_id}.mp4")
                ai_services.assemble_video(images, narration_final, out_path, progress_cb=self._progress_cb,
                                           ken_burns=ken_burns)

                sidecar = os.path.splitext(out_path)[0] + ".txt"
                with open(sidecar, "w", encoding="utf-8") as f:
                    f.write(script)
                captions.save(out_path, caption_words)

                entry = self.history.add("videos", topic, out_path, level)
                project = {
                    "dir": project_dir, "images": images, "scenes": scenes,
                    "narration_path": narration_final, "out_path": out_path,
                }
                self.progress_queue.put(("done_video", out_path, project, entry))
            else:
                work_dir = os.path.join(config.APP_DIR, "tmp", gen_id)
                os.makedirs(work_dir, exist_ok=True)
                try:
                    self._progress_cb(0, 1, "Writing podcast script...")
                    script = ai_services.generate_podcast_script(topic, level, tier["chat_model"], style=style)
                    self.progress_queue.put(("script", script))

                    raw_audio = os.path.join(work_dir, "dialogue_raw.mp3")
                    timeline = []
                    ai_services.generate_podcast_audio(
                        script, raw_audio, work_dir, tier["tts_model"],
                        progress_cb=self._progress_cb, cancel_event=self.cancel_event,
                        timeline=timeline, calm_hosts=style.get("calm_hosts"),
                    )

                    out_path = os.path.join(config.PODCASTS_DIR, f"{gen_id}.mp3")
                    if music_enabled:
                        self._progress_cb(0, 1, "Adding background music...")
                        music.add_background_music(raw_audio, out_path,
                                                    seed=hash(topic) % 100000, volume=music_volume)
                    else:
                        shutil.copyfile(raw_audio, out_path)

                    sidecar = os.path.splitext(out_path)[0] + ".txt"
                    with open(sidecar, "w", encoding="utf-8") as f:
                        f.write(script)
                    captions.save(out_path, captions.words_from_timeline(timeline))

                    entry = self.history.add("podcasts", topic, out_path, level)
                    self.progress_queue.put(("done_podcast", out_path, entry))
                finally:
                    self._cleanup_dir(work_dir)

        except ai_services.GenerationCancelled:
            self.progress_queue.put(("cancelled",))
        except ai_services.GenerationError as exc:
            self.progress_queue.put(("error", str(exc)))
        except Exception as exc:  # last-resort safety net so the UI never hard-freezes
            self.progress_queue.put(("error", f"Unexpected error: {exc}"))

    def _time_video_captions(self, script, narration_path, tier):
        """Word timings for a video's captions (worker thread). Falls back to an
        estimate from the script if transcription fails - captions should never
        cost the learner a finished video."""
        try:
            timed = ai_services.transcribe_word_timings(narration_path, tier["transcribe_model"], self.cancel_event)
            return captions.align_to_script(script, timed)
        except ai_services.GenerationError as exc:
            print(f"[ShikhaAI] Estimating caption timing instead: {exc}", file=sys.stderr)
            return captions.estimate_words(script, 0, ai_services.audio_duration(narration_path))

    @staticmethod
    def _cleanup_dir(work_dir):
        try:
            for name in os.listdir(work_dir):
                try:
                    os.remove(os.path.join(work_dir, name))
                except OSError:
                    pass
            os.rmdir(work_dir)
        except OSError:
            pass

    # ------------------------------------------------------------------
    # Queue polling (main thread only)
    # ------------------------------------------------------------------
    def _poll_queue(self):
        try:
            while True:
                message = self.progress_queue.get_nowait()
                self._handle_message(message)
        except queue.Empty:
            pass
        self.after(100, self._poll_queue)

    def _handle_message(self, message):
        kind = message[0]
        if kind == "progress":
            _, i, total, text = message
            self.progress_bar.set(0 if total <= 0 else min(1.0, (i + 1) / total))
            self.status_label.configure(text=text)
        elif kind == "script":
            self._set_script_text(message[1])
        elif kind == "done_video":
            _, out_path, project, entry = message
            self._set_generating_ui(False)
            self.status_label.configure(text="Done!")
            self.toasts.show("Video ready!", kind="success")
            self.last_video_path = out_path
            self.current_video_project = project
            self._set_current_lesson(dict(entry, kind="videos"))
            self._render_storyboard(project)
            self._load_video(out_path)
        elif kind == "done_podcast":
            _, out_path, entry = message
            self._set_generating_ui(False)
            self.status_label.configure(text="Done!")
            self.toasts.show("Podcast ready!", kind="success")
            self.last_podcast_path = out_path
            self._set_current_lesson(dict(entry, kind="podcasts"))
            self._load_podcast(out_path, os.path.basename(out_path))
        elif kind == "scene_regenerated":
            _, index, out_path = message
            self._set_generating_ui(False)
            self.status_label.configure(text="Scene updated!")
            self.toasts.show(f"Scene {index + 1} regenerated.", kind="success")
            self._thumb_photos = {}  # drop stale cached thumbnails so the new image is picked up
            self._render_storyboard(self.current_video_project)
            self._load_video(out_path)
        elif kind == "cancelled":
            self._set_generating_ui(False)
            self.status_label.configure(text="Cancelled.")
            self.toasts.show("Generation cancelled.", kind="info", duration_ms=3000)
        elif kind == "error":
            self._set_generating_ui(False)
            self.status_label.configure(text="Error.")
            self.toasts.show(message[1], kind="error", duration_ms=9000)
        elif kind == "voice_preview_ready":
            _, path, spinner = message
            self._voice_preview_busy = False
            try:
                spinner.stop()
            except Exception:
                pass
            if self.preview_player.available:
                self.preview_player.load(path)
                self.preview_player.play()
            else:
                self.toasts.show("Playback engine unavailable to preview audio.", kind="warning")
        elif kind == "voice_preview_error":
            _, err, spinner = message
            self._voice_preview_busy = False
            try:
                spinner.stop()
            except Exception:
                pass
            self.toasts.show(err, kind="error")

    def _set_script_text(self, text):
        self.script_box.configure(state="normal")
        self.script_box.delete("1.0", "end")
        self.script_box.insert("end", text)
        self.script_box.configure(state="disabled")

    # ------------------------------------------------------------------
    # Loading a video project (for storyboard) when replayed from Library
    # ------------------------------------------------------------------
    def _load_project_for_video(self, final_path):
        gen_id = os.path.splitext(os.path.basename(final_path))[0]
        project_dir = os.path.join(config.PROJECTS_DIR, gen_id)
        scenes_json = os.path.join(project_dir, "scenes.json")

        if not os.path.exists(scenes_json):
            self.current_video_project = None
            self._render_storyboard(None)
            return

        try:
            with open(scenes_json, "r", encoding="utf-8") as f:
                scenes = json.load(f)
        except (OSError, json.JSONDecodeError):
            scenes = []

        images = sorted(glob.glob(os.path.join(project_dir, "frame_*.png")))
        narration_path = os.path.join(project_dir, "narration.mp3")
        if not os.path.exists(narration_path):
            narration_path = None

        self._thumb_photos = {}
        self.current_video_project = {
            "dir": project_dir, "images": images, "scenes": scenes,
            "narration_path": narration_path, "out_path": final_path,
        }
        self._render_storyboard(self.current_video_project)

    # ------------------------------------------------------------------
    # Video playback
    # ------------------------------------------------------------------
    def _load_video(self, path):
        self.last_video_path = path
        self._load_captions("video", path)
        if not self.video_player.available:
            self.status_label.configure(text="Saved (playback engine unavailable - open the file manually).")
            return
        self.video_player.attach(self.video_surface)
        self.video_player.load(path)
        self.video_player.play()
        self.video_player.set_rate(self.video_speed)
        self.play_btn.configure(state="normal")
        self.pause_btn.configure(state="normal")
        self.resume_btn.configure(state="disabled")

    def _play_video(self):
        if not self.last_video_path:
            self.toasts.show("No video has been generated yet.", kind="warning")
            return
        self.video_player.attach(self.video_surface)
        self.video_player.load(self.last_video_path)
        self.video_player.play()
        self.video_player.set_rate(self.video_speed)
        self.pause_btn.configure(state="normal")
        self.resume_btn.configure(state="disabled")

    def _pause_video(self):
        self.video_player.pause()
        self.pause_btn.configure(state="disabled")
        self.resume_btn.configure(state="normal")

    def _resume_video(self):
        self.video_player.set_rate(self.video_speed)
        self.video_player.play()
        self.pause_btn.configure(state="normal")
        self.resume_btn.configure(state="disabled")

    def _set_video_speed(self, speed_str):
        self.video_speed = float(speed_str)
        self.video_player.set_rate(self.video_speed)

    # ------------------------------------------------------------------
    # Podcast playback
    # ------------------------------------------------------------------
    def _load_podcast(self, path, name):
        self.last_podcast_path = path
        self.podcast_name_label.configure(text=name)
        self._load_captions("podcast", path)
        if not self.podcast_player.available:
            self.status_label.configure(text="Saved (playback engine unavailable - open the file manually).")
            return
        self.podcast_player.load(path)
        self.podcast_player.play()
        self.podcast_player.set_rate(self.podcast_speed)
        self.play_podcast_btn.configure(state="normal")
        self.pause_podcast_btn.configure(state="normal")
        self.resume_podcast_btn.configure(state="disabled")

    def _play_podcast(self):
        if not self.last_podcast_path:
            self.toasts.show("No podcast has been generated yet.", kind="warning")
            return
        self.podcast_player.load(self.last_podcast_path)
        self.podcast_player.play()
        self.podcast_player.set_rate(self.podcast_speed)
        self.pause_podcast_btn.configure(state="normal")
        self.resume_podcast_btn.configure(state="disabled")

    def _pause_podcast(self):
        self.podcast_player.pause()
        self.pause_podcast_btn.configure(state="disabled")
        self.resume_podcast_btn.configure(state="normal")

    def _resume_podcast(self):
        self.podcast_player.set_rate(self.podcast_speed)
        self.podcast_player.play()
        self.pause_podcast_btn.configure(state="normal")
        self.resume_podcast_btn.configure(state="disabled")

    def _set_podcast_speed(self, speed_str):
        self.podcast_speed = float(speed_str)
        self.podcast_player.set_rate(self.podcast_speed)

    # ------------------------------------------------------------------
    # Captions (main thread only)
    # ------------------------------------------------------------------
    def _load_captions(self, kind, media_path):
        """Pick up a lesson's captions as it's loaded for playback. Older lessons
        have no captions file; those get an estimate once VLC knows the length."""
        words = captions.load(media_path)
        self.caption_words[kind] = words
        self._caption_pending[kind] = None if words else media_path
        self._rebuild_caption_track(kind)

    def _rebuild_caption_track(self, kind):
        words = self.caption_words[kind]
        self.caption_tracks[kind] = (
            captions.CaptionTrack(words, int(self.settings.get("caption_words"))) if words else None
        )

    def _estimate_captions(self, kind, media_path, duration):
        script_path = os.path.splitext(media_path)[0] + ".txt"
        try:
            with open(script_path, "r", encoding="utf-8") as f:
                script = f.read()
        except OSError:
            return None
        if kind == "podcast":
            turns = ai_services.parse_podcast_lines(script)
            return captions.estimate_turns(turns, 0, duration) or None
        return captions.estimate_words(script, 0, duration) or None

    def _tick_captions(self):
        try:
            self._update_captions()
        finally:
            self.after(100, self._tick_captions)

    def _update_captions(self):
        if not self.settings.get("captions_enabled"):
            return
        kind = self.mode
        player = self.video_player if kind == "video" else self.podcast_player
        bar = self.video_captions if kind == "video" else self.podcast_captions

        if self._caption_pending[kind]:
            length = player.get_length()
            if length > 0:
                media_path, self._caption_pending[kind] = self._caption_pending[kind], None
                self.caption_words[kind] = self._estimate_captions(kind, media_path, length)
                self._rebuild_caption_track(kind)

        t = player.get_time()
        if t is None or player.has_ended():
            bar.clear()
            return
        track = self.caption_tracks[kind]
        if track is None:
            if not self._caption_pending[kind]:
                bar.clear("Captions aren't available for this lesson.")
            return
        cue_index, word_index = track.at(t)
        if cue_index is None:
            bar.clear("")
        else:
            bar.show(track.cues[cue_index], word_index)
