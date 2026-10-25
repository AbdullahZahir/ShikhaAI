"""
Quiz page - timed multiple-choice quizzes to check what a learner took in.

A quiz is built from a lesson's saved script (so it only asks about what
the lesson actually taught) or from any typed topic. Questions for a lesson
are cached next to its media file (<id>.quiz.json), so retaking a quiz is
instant and free; "New questions" asks the model for a fresh set.

Same threading rules as app.py: the OpenAI call runs on a worker thread and
reports back through a queue polled from the main thread, and no widget is
touched from the worker.

Accommodations (from the Accessibility page): extended time multiplies
every time limit, a calm timer swaps the countdown for a quiet bar with no
color warnings, and the quiz can be paused at any point - pausing hides the
question, so a break is a real break.
"""

import json
import math
import os
import queue
import random
import threading
import time
import tkinter as tk

import customtkinter as ctk

from . import ai_services, components, config, icons

_LETTERS = "ABCD"


def _font(size=13, weight="normal"):
    return ctk.CTkFont(family=config.FONT_FAMILY, size=size, weight=weight)


def _icon(name, color="text", size=18):
    return icons.get_icon(name, color, size)


def _quiz_cache_path(media_path):
    return os.path.splitext(media_path)[0] + ".quiz.json"


def _format_seconds(seconds):
    seconds = max(0, int(math.ceil(seconds)))
    return f"{seconds // 60}:{seconds % 60:02d}"


class QuizPage(ctk.CTkFrame):
    def __init__(self, parent, app):
        super().__init__(parent, fg_color="transparent")
        self.app = app
        self.queue = queue.Queue()
        self.worker = None

        self.quiz = None            # {"topic", "level", "lesson", "questions"} for the current attempt
        self.index = 0
        self.answers = []           # one {"choice": int | None, "timed_out": bool} per question
        self.pending_choice = None  # "At the end" mode: picked but not yet confirmed with Next
        self.locked = False         # current question is answered (or timed out)
        self.paused = False
        self.time_limit = 0         # seconds per question, extended time included; 0 = untimed
        self.remaining = 0.0
        self._deadline = None
        self._timer_job = None
        self._active_seconds = 0.0  # time spent on questions, not counting pauses
        self._segment_start = None
        self._end_armed = False
        self._lessons = {}          # option-menu label -> library entry

        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(2, weight=1)

        header = ctk.CTkFrame(self, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", padx=28, pady=(24, 8))
        ctk.CTkLabel(
            header, text="Quiz time", font=_font(22, "bold"), text_color=config.COLORS["text"],
        ).pack(side="left")
        ctk.CTkLabel(
            header, text="Check what stuck, one question at a time", font=_font(12),
            text_color=config.COLORS["text_dim"],
        ).pack(side="right")

        components.GradientDivider(self, height=3).grid(row=1, column=0, sticky="ew", padx=28, pady=(0, 14))

        self.scroll = ctk.CTkScrollableFrame(
            self, fg_color="transparent", scrollbar_button_color=config.COLORS["surface_hi"],
        )
        self.scroll.grid(row=2, column=0, sticky="nsew")
        self.scroll.grid_columnconfigure(0, weight=1)

        self.setup_view = self._build_setup_view(self.scroll)
        self.question_view = self._build_question_view(self.scroll)
        self.results_view = self._build_results_view(self.scroll)
        self._show_view(self.setup_view)

        app.bind("<KeyPress>", self._on_key, add="+")
        self.after(100, self._poll_queue)

    # ------------------------------------------------------------------
    # Shared helpers
    # ------------------------------------------------------------------
    def _card(self, parent, **kwargs):
        defaults = dict(fg_color=config.COLORS["surface"], corner_radius=16)
        defaults.update(kwargs)
        return ctk.CTkFrame(parent, **defaults)

    def _option_menu(self, parent, values, variable, command=None, width=150):
        return ctk.CTkOptionMenu(
            parent, values=values, variable=variable, command=command, width=width, font=_font(13),
            fg_color=config.COLORS["bg_alt"], button_color=config.COLORS["surface_hi"],
            button_hover_color=config.COLORS["accent"], dropdown_fg_color=config.COLORS["surface"],
            text_color=config.COLORS["text"],
        )

    def _show_view(self, view):
        for v in (self.setup_view, self.question_view, self.results_view):
            if v is view:
                v.grid(row=0, column=0, sticky="ew")
            else:
                v.grid_remove()
        try:
            self.scroll._parent_canvas.yview_moveto(0)
        except Exception:
            pass

    def _settings(self):
        return self.app.settings

    # ------------------------------------------------------------------
    # Page lifecycle (called by the app)
    # ------------------------------------------------------------------
    def on_show(self):
        self._refresh_lessons()
        self._refresh_results()
        self._refresh_time_note()

    def on_hide(self):
        """Leaving the page mid-question pauses the quiz rather than letting the clock run."""
        if self._question_active() and not self.paused and not self.locked:
            self._pause()

    def apply_prefs(self):
        """Reading font / size changes from the Accessibility page."""
        self.question_label.configure(font=self.app.reading_font(18, "bold"))
        self.feedback_text.configure(font=self.app.reading_font(13))
        for row in self.choice_rows:
            row["label"].configure(font=self.app.reading_font(14))
        self._refresh_time_note()
        if self._question_active():
            self._update_timer_display()

    def use_lesson(self, lesson):
        """Pre-select a lesson (from the Create page's "Quiz me" or a Library card)."""
        if self._question_active():
            self.app.toasts.show("Finish or end the current quiz first.", kind="warning")
            return
        self._refresh_lessons()
        self.source_switch.set("From a lesson")
        self._on_source_switch("From a lesson")
        for label, entry in self._lessons.items():
            if entry["path"] == lesson["path"]:
                self.lesson_var.set(label)
                break
        self._show_view(self.setup_view)

    # ------------------------------------------------------------------
    # Setup view
    # ------------------------------------------------------------------
    def _build_setup_view(self, parent):
        view = ctk.CTkFrame(parent, fg_color="transparent")
        view.grid_columnconfigure(0, weight=1)

        card = self._card(view)
        card.grid(row=0, column=0, sticky="ew", padx=28, pady=(0, 12))
        card.grid_columnconfigure(0, weight=1)

        top = ctk.CTkFrame(card, fg_color="transparent")
        top.grid(row=0, column=0, sticky="ew", padx=20, pady=(18, 10))
        top.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(top, text="Quiz on", font=_font(12, "bold"),
                     text_color=config.COLORS["text_dim"]).grid(row=0, column=0, sticky="w")
        self.source_switch = ctk.CTkSegmentedButton(
            top, values=["From a lesson", "From a topic"], font=_font(13, "bold"),
            fg_color=config.COLORS["bg_alt"], selected_color=config.COLORS["accent"],
            selected_hover_color=config.COLORS["accent_hover"], unselected_color=config.COLORS["bg_alt"],
            text_color=config.COLORS["text"], command=self._on_source_switch,
        )
        self.source_switch.set("From a lesson")
        self.source_switch.grid(row=0, column=1, sticky="e")

        # Both source panels share one grid cell; only the selected one is gridded
        # so the card doesn't keep the taller panel's height.
        source_area = ctk.CTkFrame(card, fg_color="transparent")
        source_area.grid(row=1, column=0, sticky="ew", padx=20)
        source_area.grid_columnconfigure(0, weight=1)

        self.lesson_frame = ctk.CTkFrame(source_area, fg_color="transparent")
        self.lesson_frame.grid(row=0, column=0, sticky="nsew")
        self.lesson_frame.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(self.lesson_frame, text="Lesson", font=_font(12, "bold"),
                     text_color=config.COLORS["text_dim"]).grid(row=0, column=0, sticky="w")
        self.lesson_var = tk.StringVar(value="")
        self.lesson_menu = self._option_menu(self.lesson_frame, [""], self.lesson_var, width=300)
        self.lesson_menu.grid(row=1, column=0, sticky="ew", pady=(4, 4))
        self.lesson_hint = ctk.CTkLabel(
            self.lesson_frame, text="Questions only cover what the lesson taught.", font=_font(11),
            text_color=config.COLORS["text_faint"], anchor="w",
        )
        self.lesson_hint.grid(row=2, column=0, sticky="w", pady=(0, 14))

        self.topic_frame = ctk.CTkFrame(source_area, fg_color="transparent")
        self.topic_frame.grid(row=0, column=0, sticky="nsew")
        self.topic_frame.grid_columnconfigure((0, 1), weight=1)
        ctk.CTkLabel(self.topic_frame, text="Topic", font=_font(12, "bold"),
                     text_color=config.COLORS["text_dim"]).grid(row=0, column=0, columnspan=2, sticky="w")
        self.topic_entry = ctk.CTkEntry(
            self.topic_frame, placeholder_text="e.g. The water cycle", font=_font(14), height=42,
            corner_radius=10, fg_color=config.COLORS["bg_alt"], border_color=config.COLORS["border"],
            text_color=config.COLORS["text"],
        )
        self.topic_entry.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(2, 12))
        self.topic_entry.bind("<Return>", lambda e: self._on_start_clicked())
        ctk.CTkLabel(self.topic_frame, text="Grade category", font=_font(12, "bold"),
                     text_color=config.COLORS["text_dim"]).grid(row=2, column=0, sticky="w")
        ctk.CTkLabel(self.topic_frame, text="Specific grade", font=_font(12, "bold"),
                     text_color=config.COLORS["text_dim"]).grid(row=2, column=1, sticky="w", padx=(16, 0))
        self.level_var = tk.StringVar(value="Elementary")
        self.specific_var = tk.StringVar(value=config.GRADE_OPTIONS["Elementary"][0])
        self._option_menu(self.topic_frame, list(config.GRADE_OPTIONS.keys()), self.level_var,
                          command=self._on_level_change).grid(row=3, column=0, sticky="ew", pady=(4, 14))
        self.specific_menu = self._option_menu(self.topic_frame, config.GRADE_OPTIONS["Elementary"],
                                               self.specific_var)
        self.specific_menu.grid(row=3, column=1, sticky="ew", padx=(16, 0), pady=(4, 14))

        # ---- quiz options ----
        options = ctk.CTkFrame(card, fg_color="transparent")
        options.grid(row=2, column=0, sticky="ew", padx=20)
        options.grid_columnconfigure((0, 1, 2), weight=1)
        settings = self._settings()
        for col, (label, key, values) in enumerate([
            ("Questions", "quiz_questions", config.QUIZ_QUESTION_COUNTS),
            ("Time per question", "quiz_time_limit", list(config.QUIZ_TIME_LIMITS.keys())),
            ("Show answers", "quiz_feedback", config.QUIZ_FEEDBACK_MODES),
        ]):
            ctk.CTkLabel(options, text=label, font=_font(12, "bold"), text_color=config.COLORS["text_dim"]
                         ).grid(row=0, column=col, sticky="w", padx=(0 if col == 0 else 16, 0))
            var = tk.StringVar(value=str(settings.get(key)))
            self._option_menu(
                options, values, var, width=160,
                command=lambda value, k=key: (self._settings().set(k, value), self._refresh_time_note()),
            ).grid(row=1, column=col, sticky="ew", padx=(0 if col == 0 else 16, 0), pady=(4, 4))
            setattr(self, f"{key}_var", var)

        self.time_note = ctk.CTkLabel(options, text="", font=_font(11), text_color=config.COLORS["text_faint"],
                                      anchor="w", justify="left")
        self.time_note.grid(row=2, column=0, columnspan=3, sticky="w", pady=(2, 14))

        action_row = ctk.CTkFrame(card, fg_color="transparent")
        action_row.grid(row=3, column=0, sticky="ew", padx=20, pady=(0, 18))
        action_row.grid_columnconfigure(2, weight=1)
        self.start_btn = components.GradientButton(
            action_row, text="Start quiz", icon_name="list-checks", width=160, height=42,
            mask_color=config.COLORS["surface"], command=self._on_start_clicked,
        )
        self.start_btn.grid(row=0, column=0, sticky="w")
        self.spinner = components.Spinner(action_row, size=18, color="accent2")
        self.status_label = ctk.CTkLabel(action_row, text="", font=_font(12),
                                         text_color=config.COLORS["text_dim"], anchor="e")
        self.status_label.grid(row=0, column=2, sticky="e")

        # ---- recent results ----
        results_card = self._card(view)
        results_card.grid(row=1, column=0, sticky="ew", padx=28, pady=(0, 24))
        results_card.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(results_card, text=" Recent results", image=_icon("circle-check", "text_dim", 16),
                     compound="left", font=_font(13, "bold"), text_color=config.COLORS["text_dim"]
                     ).grid(row=0, column=0, sticky="w", padx=18, pady=(14, 6))
        self.results_list = ctk.CTkFrame(results_card, fg_color="transparent")
        self.results_list.grid(row=1, column=0, sticky="ew", padx=12, pady=(0, 12))
        self.results_list.grid_columnconfigure(0, weight=1)

        self._on_source_switch("From a lesson")
        return view

    def _on_source_switch(self, value):
        show, hide = ((self.lesson_frame, self.topic_frame) if value == "From a lesson"
                      else (self.topic_frame, self.lesson_frame))
        hide.grid_remove()
        show.grid()

    def _on_level_change(self, level):
        options = config.GRADE_OPTIONS[level]
        self.specific_menu.configure(values=options)
        self.specific_var.set(options[0])

    def _refresh_lessons(self):
        entries = [dict(e, kind="videos") for e in self.app.history.get("videos")]
        entries += [dict(e, kind="podcasts") for e in self.app.history.get("podcasts")]
        entries.sort(key=lambda e: e["created_at"], reverse=True)

        self._lessons = {}
        for entry in entries:
            kind = "Video" if entry["kind"] == "videos" else "Podcast"
            label = f'{entry["topic"]}  ·  {entry["level"]}  ·  {kind}'
            if label in self._lessons:
                label += f'  ·  {entry["created_at"].replace("T", " ")}'
            self._lessons[label] = entry

        labels = list(self._lessons.keys())
        if labels:
            self.lesson_menu.configure(values=labels, state="normal")
            if self.lesson_var.get() not in self._lessons:
                self.lesson_var.set(labels[0])
            self.lesson_hint.configure(text="Questions only cover what the lesson taught.")
        else:
            self.lesson_menu.configure(values=["No lessons yet"], state="disabled")
            self.lesson_var.set("No lessons yet")
            self.lesson_hint.configure(
                text="Make a video or podcast on the Create page first - or switch to \"From a topic\".")

    def _base_time_limit(self):
        return config.QUIZ_TIME_LIMITS.get(self._settings().get("quiz_time_limit"), 30)

    def _extra_time(self):
        return config.QUIZ_EXTRA_TIME.get(self._settings().get("quiz_extra_time"), 1.0)

    def _refresh_time_note(self):
        base, extra = self._base_time_limit(), self._extra_time()
        if base == 0:
            note = "Untimed - learners can take as long as they need on every question."
        elif extra > 1:
            note = (f"Extended time is on ({self._settings().get('quiz_extra_time')}): "
                    f"{base} seconds becomes {round(base * extra)}. Change it on the Accessibility page.")
        else:
            note = "Need more time? Turn on extended time on the Accessibility page."
        self.time_note.configure(text=note)

    def _refresh_results(self):
        for widget in self.results_list.winfo_children():
            widget.destroy()
        results = self.app.quiz_results.recent(8)
        if not results:
            ctk.CTkLabel(self.results_list, text="No quizzes taken yet. Scores will show up here.",
                         font=_font(12), text_color=config.COLORS["text_faint"]
                         ).grid(row=0, column=0, sticky="w", padx=6, pady=(0, 6))
            return
        for i, r in enumerate(results):
            row = ctk.CTkFrame(self.results_list, fg_color=config.COLORS["bg_alt"], corner_radius=10)
            row.grid(row=i, column=0, sticky="ew", pady=3)
            row.grid_columnconfigure(0, weight=1)
            ctk.CTkLabel(row, text=r["topic"], font=_font(13, "bold"), text_color=config.COLORS["text"],
                         anchor="w").grid(row=0, column=0, sticky="w", padx=12, pady=(8, 0))
            ctk.CTkLabel(row, text=f'{r["level"]}  ·  {r["created_at"].replace("T", " ")}', font=_font(11),
                         text_color=config.COLORS["text_dim"], anchor="w"
                         ).grid(row=1, column=0, sticky="w", padx=12, pady=(0, 8))
            pct = round(100 * r["score"] / r["total"]) if r["total"] else 0
            color = config.COLORS["success"] if pct >= 70 else config.COLORS["warning"] if pct >= 40 \
                else config.COLORS["danger"]
            ctk.CTkLabel(row, text=f'{r["score"]}/{r["total"]}  ({pct}%)', font=_font(14, "bold"),
                         text_color=color).grid(row=0, column=1, rowspan=2, padx=14)

    # ------------------------------------------------------------------
    # Starting a quiz
    # ------------------------------------------------------------------
    def _on_start_clicked(self, force_new=False):
        if self.worker and self.worker.is_alive():
            return
        num = int(self._settings().get("quiz_questions"))

        if self.source_switch.get() == "From a lesson":
            entry = self._lessons.get(self.lesson_var.get())
            if not entry:
                self.app.toasts.show("Pick a lesson first, or switch to \"From a topic\".", kind="warning")
                return
            context = {"topic": entry["topic"], "level": entry["level"], "lesson": entry}
            cached = None if force_new else self._load_cached_questions(entry["path"])
            if cached and len(cached) >= num:
                self._begin(context, cached[:num])
                return
        else:
            topic = self.topic_entry.get().strip()
            if not topic:
                self.app.toasts.show("Please enter a topic.", kind="warning")
                return
            context = {"topic": topic, "level": self.specific_var.get() or self.level_var.get(), "lesson": None}

        self._generate(context, num)

    def _generate(self, context, num):
        lesson_text = None
        if context["lesson"]:
            script_path = os.path.splitext(context["lesson"]["path"])[0] + ".txt"
            try:
                with open(script_path, "r", encoding="utf-8") as f:
                    lesson_text = f.read()
            except OSError:
                pass  # no saved script - fall back to quizzing on the topic itself

        tier = config.MODEL_TIERS[self._settings().get("tier")]
        style = self.app.lesson_style()
        self.start_btn.set_enabled(False)
        self.start_btn.set_text("Writing...")
        self.spinner.grid(row=0, column=1, sticky="w", padx=(10, 0))
        self.spinner.start()
        self.status_label.configure(text=f"Writing {num} questions...")
        self.worker = threading.Thread(
            target=self._run_generate, args=(context, num, lesson_text, tier["chat_model"], style), daemon=True,
        )
        self.worker.start()

    def _run_generate(self, context, num, lesson_text, model, style):
        try:
            questions = ai_services.generate_quiz(
                context["level"], model, num, lesson_text=lesson_text, topic=context["topic"], style=style,
            )
            self.queue.put(("quiz_ready", context, questions))
        except ai_services.GenerationError as exc:
            self.queue.put(("quiz_error", str(exc)))
        except Exception as exc:  # last-resort safety net so the UI never hard-freezes
            self.queue.put(("quiz_error", f"Unexpected error: {exc}"))

    def _poll_queue(self):
        try:
            while True:
                self._handle_message(self.queue.get_nowait())
        except queue.Empty:
            pass
        self.after(100, self._poll_queue)

    def _handle_message(self, message):
        self.start_btn.set_enabled(True)
        self.start_btn.set_text("Start quiz")
        self.spinner.stop()
        self.spinner.grid_forget()
        if message[0] == "quiz_ready":
            _, context, questions = message
            self.status_label.configure(text="")
            if context["lesson"]:
                self._save_cached_questions(context["lesson"]["path"], questions)
            self._begin(context, questions)
        else:
            self.status_label.configure(text="Error.")
            self.app.toasts.show(message[1], kind="error", duration_ms=9000)

    @staticmethod
    def _load_cached_questions(media_path):
        try:
            with open(_quiz_cache_path(media_path), "r", encoding="utf-8") as f:
                return ai_services._clean_quiz_questions(json.load(f)) or None
        except (OSError, json.JSONDecodeError):
            return None

    @staticmethod
    def _save_cached_questions(media_path, questions):
        try:
            with open(_quiz_cache_path(media_path), "w", encoding="utf-8") as f:
                json.dump({"questions": questions}, f, indent=2)
        except OSError:
            pass  # caching is a nice-to-have; the quiz itself still works

    def _begin(self, context, questions, shuffle_order=False):
        """Start an attempt. Answer choices are reshuffled every attempt so a
        retake tests the idea rather than the remembered letter."""
        order = random.sample(questions, len(questions)) if shuffle_order else list(questions)
        attempt = []
        for q in order:
            perm = random.sample(range(4), 4)
            attempt.append({**q, "choices": [q["choices"][p] for p in perm], "answer": perm.index(q["answer"])})

        self.quiz = {**context, "source_questions": questions, "questions": attempt}
        self.answers = [None] * len(attempt)
        self.index = 0
        base = self._base_time_limit()
        self.time_limit = round(base * self._extra_time()) if base else 0
        self._active_seconds = 0.0
        self.quiz_title.configure(text=f'{context["topic"]}  ·  {context["level"]}')
        self._show_view(self.question_view)
        self._render_question()

    # ------------------------------------------------------------------
    # Question view
    # ------------------------------------------------------------------
    def _build_question_view(self, parent):
        view = ctk.CTkFrame(parent, fg_color="transparent")
        view.grid_columnconfigure(0, weight=1)

        card = self._card(view)
        card.grid(row=0, column=0, sticky="ew", padx=28, pady=(0, 24))
        card.grid_columnconfigure(0, weight=1)
        self.question_card = card

        top = ctk.CTkFrame(card, fg_color="transparent")
        top.grid(row=0, column=0, sticky="ew", padx=20, pady=(18, 4))
        top.grid_columnconfigure(0, weight=1)
        self.quiz_title = ctk.CTkLabel(top, text="", font=_font(12), text_color=config.COLORS["text_faint"],
                                       anchor="w")
        self.quiz_title.grid(row=0, column=0, sticky="w")
        self.progress_label = ctk.CTkLabel(top, text="", font=_font(13, "bold"),
                                           text_color=config.COLORS["text_dim"], anchor="w")
        self.progress_label.grid(row=1, column=0, sticky="w")
        self.timer_label = ctk.CTkLabel(top, text="", image=_icon("timer", "accent2", 20), compound="left",
                                        font=_font(18, "bold"), text_color=config.COLORS["text"])
        self.timer_label.grid(row=0, column=1, rowspan=2, sticky="e")

        self.timer_bar = ctk.CTkProgressBar(card, height=8, corner_radius=4,
                                            progress_color=config.COLORS["accent2"],
                                            fg_color=config.COLORS["bg_alt"])
        self.timer_bar.grid(row=1, column=0, sticky="ew", padx=20, pady=(6, 12))

        # Question + choices live in one frame so pausing can hide them together.
        self.question_body = ctk.CTkFrame(card, fg_color="transparent")
        self.question_body.grid(row=2, column=0, sticky="ew", padx=20)
        self.question_body.grid_columnconfigure(0, weight=1)

        self.question_label = ctk.CTkLabel(self.question_body, text="", font=self.app.reading_font(18, "bold"),
                                           text_color=config.COLORS["text"], anchor="w", justify="left")
        self.question_label.grid(row=0, column=0, sticky="ew", pady=(0, 12))

        self.choice_rows = []
        for i in range(4):
            row = ctk.CTkFrame(self.question_body, fg_color=config.COLORS["bg_alt"], corner_radius=12,
                               border_width=2, border_color=config.COLORS["bg_alt"])
            row.grid(row=1 + i, column=0, sticky="ew", pady=4)
            row.grid_columnconfigure(1, weight=1)
            badge = ctk.CTkLabel(row, text=_LETTERS[i], width=32, height=32, corner_radius=8,
                                 fg_color=config.COLORS["surface_hi"], font=_font(13, "bold"),
                                 text_color=config.COLORS["text"])
            badge.grid(row=0, column=0, padx=(12, 12), pady=10)
            label = ctk.CTkLabel(row, text="", font=self.app.reading_font(14), text_color=config.COLORS["text"],
                                 anchor="w", justify="left")
            label.grid(row=0, column=1, sticky="ew", padx=(0, 14), pady=10)
            for widget in (row, badge, label):
                widget.bind("<Button-1>", lambda e, idx=i: self._choose(idx))
                widget.bind("<Enter>", lambda e, idx=i: self._hover_choice(idx, True))
                widget.bind("<Leave>", lambda e, idx=i: self._hover_choice(idx, False))
                widget.configure(cursor="hand2")
            self.choice_rows.append({"row": row, "badge": badge, "label": label})

        self.feedback_frame = ctk.CTkFrame(self.question_body, fg_color="transparent")
        self.feedback_title = ctk.CTkLabel(self.feedback_frame, text="", compound="left", font=_font(14, "bold"),
                                           text_color=config.COLORS["text"], anchor="w", justify="left")
        self.feedback_title.pack(anchor="w")
        self.feedback_text = ctk.CTkLabel(self.feedback_frame, text="", font=self.app.reading_font(13),
                                          text_color=config.COLORS["text_dim"], anchor="w", justify="left")
        self.feedback_text.pack(anchor="w", pady=(2, 0))

        self.paused_frame = ctk.CTkFrame(card, fg_color=config.COLORS["bg_alt"], corner_radius=12)
        ctk.CTkLabel(self.paused_frame, text="", image=_icon("pause", "accent2", 48)).pack(pady=(36, 10))
        ctk.CTkLabel(self.paused_frame, text="Paused - take a break", font=_font(16, "bold"),
                     text_color=config.COLORS["text"]).pack()
        ctk.CTkLabel(self.paused_frame, text="The timer is stopped and the question is hidden until you resume.",
                     font=_font(12), text_color=config.COLORS["text_dim"]).pack(pady=(4, 36))

        bottom = ctk.CTkFrame(card, fg_color="transparent")
        bottom.grid(row=4, column=0, sticky="ew", padx=20, pady=(14, 18))
        bottom.grid_columnconfigure(2, weight=1)
        self.pause_btn = ctk.CTkButton(
            bottom, text=" Pause", image=_icon("pause", "text", 16), width=100, height=36,
            fg_color=config.COLORS["surface_hi"], hover_color=config.COLORS["border"], command=self._toggle_pause,
        )
        self.pause_btn.grid(row=0, column=0, padx=(0, 6))
        components.add_hover_grow(self.pause_btn, 100, 36)
        self.end_btn = ctk.CTkButton(
            bottom, text="End quiz", width=96, height=36, fg_color=config.COLORS["surface_hi"],
            hover_color=config.COLORS["danger_hover"], command=self._on_end_clicked,
        )
        self.end_btn.grid(row=0, column=1, padx=6)
        components.add_hover_grow(self.end_btn, 96, 36)
        ctk.CTkLabel(bottom, text="Keys: 1-4 or A-D to answer, Enter for next", font=_font(11),
                     text_color=config.COLORS["text_faint"]).grid(row=0, column=2)
        self.next_btn = ctk.CTkButton(
            bottom, text="Next  ", image=_icon("chevron-right", "on_accent", 16), compound="right",
            width=110, height=36, font=_font(13, "bold"), fg_color=config.COLORS["accent"],
            hover_color=config.COLORS["accent_hover"], command=self._next,
        )
        self.next_btn.grid(row=0, column=3)
        components.add_hover_grow(self.next_btn, 110, 36)

        card.bind("<Configure>", self._on_question_card_resize)
        return view

    def _on_question_card_resize(self, event):
        # Event sizes are in screen pixels; wraplength is in CTk's scaled units.
        width = max(200, event.width / ctk.ScalingTracker.get_widget_scaling(self))
        self.question_label.configure(wraplength=width - 60)
        self.feedback_title.configure(wraplength=width - 60)
        self.feedback_text.configure(wraplength=width - 60)
        for row in self.choice_rows:
            row["label"].configure(wraplength=width - 140)

    def _question_active(self):
        return self.quiz is not None and self.question_view.winfo_manager() != ""

    def _feedback_now(self):
        return self._settings().get("quiz_feedback") != "At the end"

    def _render_question(self):
        q = self.quiz["questions"][self.index]
        total = len(self.quiz["questions"])
        self.locked = False
        self.paused = False
        self.pending_choice = None
        self._end_armed = False
        self.end_btn.configure(text="End quiz")
        self.progress_label.configure(text=f"Question {self.index + 1} of {total}")
        self.question_label.configure(text=q["question"])
        for i, row in enumerate(self.choice_rows):
            row["label"].configure(text=q["choices"][i])
            self._style_choice(i, "normal")
        self.feedback_frame.grid_forget()
        self.paused_frame.grid_forget()
        self.question_body.grid()
        self.pause_btn.configure(state="normal", text=" Pause", image=_icon("pause", "text", 16))
        self.next_btn.configure(state="disabled",
                                text="Finish  " if self.index == total - 1 else "Next  ")
        self._start_timer()

    def _style_choice(self, i, state):
        colors = config.COLORS
        row = self.choice_rows[i]
        border, fill, badge, badge_text, text = {
            "normal": (colors["bg_alt"], colors["bg_alt"], colors["surface_hi"], colors["text"], colors["text"]),
            "hover": (colors["surface_hi"], colors["surface_hi"], colors["border"], colors["text"], colors["text"]),
            "selected": (colors["accent"], colors["accent_soft"], colors["accent"], "#FFFFFF", colors["text"]),
            "correct": (colors["success"], colors["bg_alt"], colors["success"], colors["bg"], colors["text"]),
            "wrong": (colors["danger"], colors["bg_alt"], colors["danger"], "#FFFFFF", colors["text"]),
            "muted": (colors["bg_alt"], colors["bg_alt"], colors["surface_hi"], colors["text_faint"],
                      colors["text_faint"]),
        }[state]
        row["state"] = state
        row["row"].configure(border_color=border, fg_color=fill)
        row["badge"].configure(fg_color=badge, text_color=badge_text)
        row["label"].configure(text_color=text)

    def _hover_choice(self, i, entering):
        if self.locked or self.paused:
            return
        state = self.choice_rows[i].get("state")
        if entering and state == "normal":
            self._style_choice(i, "hover")
        elif not entering and state == "hover":
            self._style_choice(i, "normal")

    def _choose(self, i):
        if not self._question_active() or self.locked or self.paused:
            return
        if self._feedback_now():
            self._lock_answer(i, timed_out=False)
            return
        # "At the end" mode: the choice can still change until Next is pressed.
        self.pending_choice = i
        for k in range(4):
            self._style_choice(k, "selected" if k == i else "normal")
        self.next_btn.configure(state="normal")

    def _lock_answer(self, choice, timed_out):
        q = self.quiz["questions"][self.index]
        self.locked = True
        self._stop_timer()
        self.answers[self.index] = {"choice": choice, "timed_out": timed_out}
        self.pause_btn.configure(state="disabled")
        self.next_btn.configure(state="normal")

        if self._feedback_now():
            for k in range(4):
                if k == q["answer"]:
                    self._style_choice(k, "correct")
                elif k == choice:
                    self._style_choice(k, "wrong")
                else:
                    self._style_choice(k, "muted")
            right = f'{_LETTERS[q["answer"]]}: {q["choices"][q["answer"]]}'
            if choice == q["answer"]:
                title, icon, color = "Correct!", "circle-check", "success"
            elif timed_out:
                title, icon, color = f"Time's up. The answer was {right}", "timer", "warning"
            else:
                title, icon, color = f"Not quite. The answer is {right}", "circle-alert", "danger"
            self.feedback_title.configure(text=" " + title, image=_icon(icon, color, 16),
                                          text_color=config.COLORS[color])
            self.feedback_text.configure(text=q.get("explanation", ""))
            self.feedback_frame.grid(row=5, column=0, sticky="ew", pady=(12, 0))

    def _on_time_up(self):
        choice = self.pending_choice
        self._lock_answer(choice, timed_out=choice is None)
        if not self._feedback_now():
            # "At the end" mode: whatever was picked in time still counts, but
            # the answer stays hidden until the results screen.
            for k in range(4):
                self._style_choice(k, "selected" if k == choice else "muted")
            self.feedback_title.configure(text=" Time's up - press Next to keep going.",
                                          image=_icon("timer", "warning", 16), text_color=config.COLORS["warning"])
            self.feedback_text.configure(text="")
            self.feedback_frame.grid(row=5, column=0, sticky="ew", pady=(12, 0))

    def _next(self):
        if not self._question_active() or self.paused:
            return
        if not self.locked:
            if self.pending_choice is None:
                return
            self._lock_answer(self.pending_choice, timed_out=False)
        if self.index + 1 >= len(self.quiz["questions"]):
            self._finish()
        else:
            self.index += 1
            self._render_question()

    def _on_end_clicked(self):
        # Two-step so a stray click doesn't throw away a quiz in progress.
        if not self._end_armed:
            self._end_armed = True
            self.end_btn.configure(text="End now?")
            self.after(3000, self._disarm_end)
            return
        self._stop_timer()
        if not self.locked and self.pending_choice is not None:
            self.answers[self.index] = {"choice": self.pending_choice, "timed_out": False}
        self._finish()

    def _disarm_end(self):
        self._end_armed = False
        try:
            self.end_btn.configure(text="End quiz")
        except Exception:
            pass

    def _on_key(self, event):
        if self.app.current_page != "quiz" or not self._question_active():
            return
        try:
            focused = self.focus_get()
        except KeyError:  # Tk can't name some transient popup widgets
            focused = None
        if isinstance(focused, (tk.Entry, ctk.CTkEntry)):
            return
        key = (event.keysym or "").lower()
        if key in ("1", "2", "3", "4"):
            self._choose(int(key) - 1)
        elif key in ("a", "b", "c", "d"):
            self._choose("abcd".index(key))
        elif key in ("return", "kp_enter"):
            if self.next_btn.cget("state") == "normal":
                self._next()

    # ------------------------------------------------------------------
    # Timer
    # ------------------------------------------------------------------
    def _start_timer(self):
        self._stop_timer()
        self._segment_start = time.monotonic()
        if self.time_limit <= 0:
            self._deadline = None
            self._update_timer_display()
            return
        self.remaining = float(self.time_limit)
        self._deadline = time.monotonic() + self.remaining
        self._tick()

    def _stop_timer(self):
        if self._timer_job is not None:
            try:
                self.after_cancel(self._timer_job)
            except Exception:
                pass
            self._timer_job = None
        if self._deadline is not None:
            self.remaining = max(0.0, self._deadline - time.monotonic())
        self._deadline = None
        if self._segment_start is not None:
            self._active_seconds += time.monotonic() - self._segment_start
            self._segment_start = None

    def _tick(self):
        self._timer_job = None
        if self._deadline is None:
            return
        self.remaining = max(0.0, self._deadline - time.monotonic())
        self._update_timer_display()
        if self.remaining <= 0:
            self._on_time_up()
            return
        self._timer_job = self.after(100, self._tick)

    def _update_timer_display(self):
        calm = bool(self._settings().get("quiz_calm_timer"))
        if self.time_limit <= 0:
            self.timer_label.configure(text=" No time limit", font=_font(13), text_color=config.COLORS["text_dim"])
            self.timer_bar.grid_remove()
            return
        self.timer_bar.grid()
        fraction = self.remaining / self.time_limit if self.time_limit else 0
        self.timer_bar.set(fraction)
        hurry = fraction <= 0.25 and not calm
        self.timer_bar.configure(progress_color=config.COLORS["warning"] if hurry else config.COLORS["accent2"])
        if calm:
            # No ticking numbers to watch - just the bar, quietly shrinking.
            self.timer_label.configure(text="", font=_font(18, "bold"))
        else:
            self.timer_label.configure(text=" " + _format_seconds(self.remaining), font=_font(18, "bold"),
                                       text_color=config.COLORS["warning"] if hurry else config.COLORS["text"])

    def _toggle_pause(self):
        if self.paused:
            self._resume()
        else:
            self._pause()

    def _pause(self):
        if self.locked:
            return
        self._stop_timer()
        self.paused = True
        self.question_body.grid_remove()
        self.paused_frame.grid(row=2, column=0, sticky="ew", padx=20)
        self.pause_btn.configure(text=" Resume", image=_icon("play", "text", 16))
        self.next_btn.configure(state="disabled")

    def _resume(self):
        self.paused = False
        self.paused_frame.grid_forget()
        self.question_body.grid()
        self.pause_btn.configure(text=" Pause", image=_icon("pause", "text", 16))
        if self.pending_choice is not None:
            self.next_btn.configure(state="normal")
        self._segment_start = time.monotonic()
        if self.time_limit > 0:
            self._deadline = time.monotonic() + self.remaining
            self._tick()

    # ------------------------------------------------------------------
    # Results view
    # ------------------------------------------------------------------
    def _build_results_view(self, parent):
        view = ctk.CTkFrame(parent, fg_color="transparent")
        view.grid_columnconfigure(0, weight=1)

        summary = self._card(view)
        summary.grid(row=0, column=0, sticky="ew", padx=28, pady=(0, 12))
        summary.grid_columnconfigure(1, weight=1)
        self.score_label = ctk.CTkLabel(summary, text="", font=_font(44, "bold"),
                                        text_color=config.COLORS["accent2"])
        self.score_label.grid(row=0, column=0, rowspan=2, padx=(26, 20), pady=20)
        self.score_message = ctk.CTkLabel(summary, text="", font=_font(18, "bold"),
                                          text_color=config.COLORS["text"], anchor="w")
        self.score_message.grid(row=0, column=1, sticky="sw", pady=(22, 0))
        self.score_stats = ctk.CTkLabel(summary, text="", font=_font(12), text_color=config.COLORS["text_dim"],
                                        anchor="w", justify="left")
        self.score_stats.grid(row=1, column=1, sticky="nw", pady=(2, 22))

        buttons = ctk.CTkFrame(summary, fg_color="transparent")
        buttons.grid(row=2, column=0, columnspan=2, sticky="w", padx=20, pady=(0, 20))
        retake_btn = ctk.CTkButton(
            buttons, text=" Retake quiz", image=_icon("rotate-ccw", "on_accent", 16), width=130, height=36,
            fg_color=config.COLORS["accent"], hover_color=config.COLORS["accent_hover"], command=self._retake,
        )
        retake_btn.pack(side="left", padx=(0, 6))
        components.add_hover_grow(retake_btn, 130, 36)
        new_btn = ctk.CTkButton(
            buttons, text=" New questions", image=_icon("refresh-cw", "text", 16), width=140, height=36,
            fg_color=config.COLORS["surface_hi"], hover_color=config.COLORS["border"], command=self._new_questions,
        )
        new_btn.pack(side="left", padx=6)
        components.add_hover_grow(new_btn, 140, 36)
        self.rewatch_btn = ctk.CTkButton(
            buttons, text=" Rewatch lesson", image=_icon("play", "text", 16), width=140, height=36,
            fg_color=config.COLORS["surface_hi"], hover_color=config.COLORS["border"], command=self._rewatch,
        )
        self.rewatch_btn.pack(side="left", padx=6)
        components.add_hover_grow(self.rewatch_btn, 140, 36)
        back_btn = ctk.CTkButton(
            buttons, text=" Back", image=_icon("chevron-left", "text", 16), width=90, height=36,
            fg_color=config.COLORS["surface_hi"], hover_color=config.COLORS["border"],
            command=lambda: (self.on_show(), self._show_view(self.setup_view)),
        )
        back_btn.pack(side="left", padx=6)
        components.add_hover_grow(back_btn, 90, 36)

        review = self._card(view)
        review.grid(row=1, column=0, sticky="ew", padx=28, pady=(0, 24))
        review.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(review, text=" Review answers", image=_icon("list-checks", "text_dim", 16), compound="left",
                     font=_font(13, "bold"), text_color=config.COLORS["text_dim"]
                     ).grid(row=0, column=0, sticky="w", padx=18, pady=(14, 6))
        self.review_list = ctk.CTkFrame(review, fg_color="transparent")
        self.review_list.grid(row=1, column=0, sticky="ew", padx=12, pady=(0, 12))
        self.review_list.grid_columnconfigure(0, weight=1)
        review.bind("<Configure>", self._on_review_resize)
        self._review_labels = []
        return view

    def _on_review_resize(self, event):
        width = event.width / ctk.ScalingTracker.get_widget_scaling(self)
        for label in self._review_labels:
            try:
                label.configure(wraplength=max(200, width - 70))
            except Exception:
                pass

    def _finish(self):
        self._stop_timer()
        questions = self.quiz["questions"]
        answers = [a or {"choice": None, "timed_out": False} for a in self.answers]
        score = sum(1 for q, a in zip(questions, answers) if a["choice"] == q["answer"])
        timed_out = sum(1 for a in answers if a["timed_out"])
        skipped = sum(1 for a in answers if a["choice"] is None and not a["timed_out"])
        total = len(questions)

        self.app.quiz_results.add(
            self.quiz["topic"], self.quiz["level"], score, total, self.time_limit, self._active_seconds, timed_out,
        )

        pct = round(100 * score / total) if total else 0
        if pct >= 90:
            message = "Excellent work!"
        elif pct >= 70:
            message = "Great job!"
        elif pct >= 50:
            message = "Good effort - a quick review will help."
        else:
            message = "Keep going - another pass through the lesson will help."
        self.score_label.configure(text=f"{score}/{total}")
        self.score_message.configure(text=f"{pct}%  ·  {message}")
        stats = [f"Time spent: {_format_seconds(self._active_seconds)}"]
        if timed_out:
            stats.append(f"{timed_out} ran out of time")
        if skipped:
            stats.append(f"{skipped} not answered")
        self.score_stats.configure(text="  ·  ".join(stats))
        if self.quiz["lesson"]:
            self.rewatch_btn.pack(side="left", padx=6)
        else:
            self.rewatch_btn.pack_forget()

        self._render_review(questions, answers)
        self._show_view(self.results_view)

    def _render_review(self, questions, answers):
        for widget in self.review_list.winfo_children():
            widget.destroy()
        self._review_labels = []
        wrap = max(200, self.review_list.winfo_width() / ctk.ScalingTracker.get_widget_scaling(self) - 40)
        for i, (q, a) in enumerate(zip(questions, answers)):
            item = ctk.CTkFrame(self.review_list, fg_color=config.COLORS["bg_alt"], corner_radius=10)
            item.grid(row=i, column=0, sticky="ew", pady=4)
            correct = a["choice"] == q["answer"]
            icon = "circle-check" if correct else "timer" if a["timed_out"] else "circle-alert"
            color = "success" if correct else "warning" if a["timed_out"] else "danger"
            title = ctk.CTkLabel(item, text=f" {i + 1}. {q['question']}", image=_icon(icon, color, 16),
                                 compound="left", font=self.app.reading_font(14, "bold"),
                                 text_color=config.COLORS["text"], anchor="w", justify="left", wraplength=wrap)
            title.pack(anchor="w", padx=12, pady=(10, 2))
            lines = []
            if a["choice"] is None:
                lines.append(("Time ran out before an answer" if a["timed_out"] else "Not answered", "warning"))
            else:
                lines.append((f'Your answer: {_LETTERS[a["choice"]]}. {q["choices"][a["choice"]]}',
                              "success" if correct else "danger"))
            if not correct:
                lines.append((f'Correct answer: {_LETTERS[q["answer"]]}. {q["choices"][q["answer"]]}', "text"))
            if q.get("explanation"):
                lines.append((q["explanation"], "text_dim"))
            labels = [title]
            for text, color_key in lines:
                label = ctk.CTkLabel(item, text=text, font=self.app.reading_font(12),
                                     text_color=config.COLORS[color_key], anchor="w", justify="left",
                                     wraplength=wrap)
                label.pack(anchor="w", padx=(38, 12), pady=(0, 2))
                labels.append(label)
            ctk.CTkFrame(item, fg_color="transparent", height=6).pack()
            self._review_labels.extend(labels)

    def _retake(self):
        self._begin(
            {k: self.quiz[k] for k in ("topic", "level", "lesson")}, self.quiz["source_questions"],
            shuffle_order=True,
        )

    def _new_questions(self):
        context = {k: self.quiz[k] for k in ("topic", "level", "lesson")}
        self._show_view(self.setup_view)
        self._generate(context, len(self.quiz["source_questions"]))

    def _rewatch(self):
        lesson = self.quiz and self.quiz["lesson"]
        if lesson:
            self.app._play_from_library(lesson["kind"], lesson)
