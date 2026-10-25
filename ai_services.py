"""
All calls out to OpenAI, plus the moviepy assembly step, live here.
"""

import base64
import json
import os
import re
import uuid

from openai import OpenAI

from moviepy import (
    AudioFileClip, ImageClip, CompositeVideoClip,
    concatenate_videoclips, concatenate_audioclips, vfx,
)

from . import config


class GenerationError(Exception):
    """Raised whenever a generation step fails, with a user-facing message."""


class GenerationCancelled(Exception):
    """Raised when the user hits Cancel mid-pipeline."""


def _check_cancel(cancel_event):
    if cancel_event is not None and cancel_event.is_set():
        raise GenerationCancelled()


def get_client():
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise GenerationError(
            "No OpenAI API key found. Add OPENAI_API_KEY=sk-... to your .env file "
            "and restart the app."
        )
    return OpenAI(api_key=api_key)


# Reasoning models (GPT-5 family, o-series) spend part of their token budget
# thinking before they answer, so they get extra room on top of the answer's.
_REASONING_PREFIXES = ("gpt-5", "o1", "o3", "o4")
_REASONING_HEADROOM = 2000


def _chat(client, model, messages, max_tokens, **kwargs):
    """One chat completion, returning the reply text.

    Uses `max_completion_tokens`, which every current chat model accepts -
    GPT-5-family models reject the older `max_tokens` outright.
    """
    if model.startswith(_REASONING_PREFIXES):
        kwargs.setdefault("reasoning_effort", "low")
        max_tokens += _REASONING_HEADROOM
    response = client.chat.completions.create(
        model=model, messages=messages, max_completion_tokens=max_tokens, **kwargs,
    )
    return (response.choices[0].message.content or "").strip()


# --------------------------------------------------------------------------
# Lesson style (from the Accessibility page)
# --------------------------------------------------------------------------
def lesson_style_rules(style):
    """Extra writing rules for the neurodivergent-friendly lesson options.

    `style` is a dict of booleans: literal_language, predictable_structure,
    bite_size (calm_hosts only affects podcasts - see generate_podcast_script).
    """
    style = style or {}
    rules = []
    if style.get("literal_language"):
        rules.append(
            "Use clear, literal language: no idioms, sarcasm, exaggeration or figures of "
            "speech. Say exactly what you mean, and explain each new word in plain terms "
            "the first time it appears."
        )
    if style.get("predictable_structure"):
        rules.append(
            "Use a predictable structure: begin by saying what the lesson will cover, go "
            "through the ideas in clearly signposted steps (first, next, then, finally), "
            "and finish with a short recap of the main points."
        )
    if style.get("bite_size"):
        rules.append(
            "Keep it short and focused: about 150 words in total, one main idea per "
            "paragraph, and short sentences."
        )
    return rules


def _with_rules(text, rules):
    return text + ("\n\n" + "\n".join(f"- {r}" for r in rules) if rules else "")


# --------------------------------------------------------------------------
# Text chunking helpers
# --------------------------------------------------------------------------
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")


def _split_sentences(text):
    sentences = [s.strip() for s in _SENTENCE_SPLIT_RE.split(text.strip()) if s.strip()]
    return sentences or ([text.strip()] if text.strip() else [])


def chunk_text_by_length(text, max_chars):
    """Greedily pack whole sentences into chunks under max_chars."""
    sentences = _split_sentences(text)
    chunks, current = [], ""
    for sentence in sentences:
        candidate = f"{current} {sentence}".strip() if current else sentence
        if len(candidate) > max_chars and current:
            chunks.append(current)
            current = sentence
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks or [text[:max_chars]]


def _split_into_n_groups(items, n):
    n = max(1, min(n, len(items)))
    k, m = divmod(len(items), n)
    groups, idx = [], 0
    for i in range(n):
        size = k + (1 if i < m else 0)
        groups.append(items[idx: idx + size])
        idx += size
    return [g for g in groups if g]


def chunk_script_into_scenes(script, max_scenes):
    """Turn a narration script into a handful of scene descriptions for illustration.

    The original app generated one DALL-E image per non-empty *line*, which
    for a normally-formatted script could mean dozens of images (slow and
    expensive). This groups the script into at most `max_scenes` chunks.
    """
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", script) if p.strip()]
    if not paragraphs:
        paragraphs = [line.strip() for line in script.split("\n") if line.strip()]
    if not paragraphs:
        paragraphs = [script.strip()]

    if len(paragraphs) > max_scenes:
        groups = _split_into_n_groups(paragraphs, max_scenes)
        return [" ".join(g) for g in groups]

    if len(paragraphs) < max(2, max_scenes // 2):
        sentences = _split_sentences(script)
        target = min(max_scenes, max(2, len(sentences) // 2 or 1))
        if len(sentences) >= target:
            groups = _split_into_n_groups(sentences, target)
            return [" ".join(g) for g in groups]

    return paragraphs


# --------------------------------------------------------------------------
# Podcast dialogue parsing
# --------------------------------------------------------------------------
_LINE_RE = re.compile(r"^\**\s*(Shimmer|Echo)\s*\**\s*:\s*(.*)$", re.IGNORECASE)


def _clean_dialogue_piece(text):
    """Strip stray markdown emphasis markers left over from '**Shimmer:** ...' style lines."""
    return text.strip().strip("*").strip()


def parse_podcast_lines(script):
    """Parse 'Shimmer: ...' / 'Echo: ...' turns, tolerating markdown & wrapping."""
    turns = []
    speaker, buffer = None, []

    def flush():
        if speaker and buffer:
            text = " ".join(buffer).strip()
            if text:
                turns.append((speaker, text))

    for raw_line in script.split("\n"):
        line = raw_line.strip()
        if not line:
            continue
        match = _LINE_RE.match(line)
        if match:
            flush()
            speaker = match.group(1).capitalize()
            piece = _clean_dialogue_piece(match.group(2))
            buffer = [piece] if piece else []
        elif speaker:
            buffer.append(_clean_dialogue_piece(line))
    flush()
    return turns


# --------------------------------------------------------------------------
# Script generation
# --------------------------------------------------------------------------
def generate_script(topic, level, model, style=None):
    client = get_client()
    try:
        text = _chat(
            client, model,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are a skilled educational scriptwriter. Write clear, "
                        "accurate, engaging narration scripts broken into short "
                        "paragraphs, one paragraph per visual scene."
                    ),
                },
                {
                    "role": "user",
                    "content": _with_rules(
                        f"Write a narration script appropriate for a {level} student "
                        f"on this topic:\n\n{topic}\n\n"
                        "Keep paragraphs short (2-4 sentences) since each paragraph "
                        "will be paired with one illustration.",
                        lesson_style_rules(style),
                    ),
                },
            ],
            max_tokens=900,
        )
        if not text:
            raise GenerationError("The model returned an empty script. Try again.")
        return text
    except GenerationError:
        raise
    except Exception as exc:
        raise GenerationError(f"Couldn't generate the script: {exc}") from exc


def generate_podcast_script(topic, level, model, style=None):
    style = style or {}
    rules = lesson_style_rules({**style, "bite_size": False})
    if style.get("calm_hosts"):
        rules.append(
            "Keep both hosts calm and gentle: no shouting, teasing, interrupting or "
            "sudden jokes. Any humor is light and easy to follow."
        )
    exchanges = "10-14" if style.get("bite_size") else "20-30"
    client = get_client()
    try:
        text = _chat(
            client, model,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You write natural, funny, engaging two-host educational "
                        "podcast scripts. Every line must start with either "
                        "'Shimmer:' or 'Echo:' (no other prefixes or markdown)."
                    ),
                },
                {
                    "role": "user",
                    "content": _with_rules(
                        "Create a podcast-style dialogue between two AI hosts, "
                        "Shimmer and Echo. Shimmer is energetic and playful; Echo "
                        "is calm and thoughtful. The topic is: "
                        f"{topic} (for a {level} student). Make it engaging with "
                        "back-and-forth exchanges, light humor, and real-life "
                        f"examples. Aim for roughly {exchanges} exchanges total. Every "
                        "line must start with 'Shimmer:' or 'Echo:'.",
                        rules,
                    ),
                },
            ],
            max_tokens=1800,
        )
        if not parse_podcast_lines(text):
            raise GenerationError(
                "The model didn't return recognizable Shimmer/Echo dialogue. Try again."
            )
        return text
    except GenerationError:
        raise
    except Exception as exc:
        raise GenerationError(f"Couldn't generate the podcast script: {exc}") from exc


# --------------------------------------------------------------------------
# Image generation (GPT Image family)
# --------------------------------------------------------------------------
# Sensory-friendly illustrations (Accessibility > "Calm illustrations").
_CALM_VISUALS_STYLE = (
    "Use soft, muted colors, a simple uncluttered composition with plenty of empty "
    "space, and gentle lighting. No busy patterns, flashing effects or scary imagery. "
)


def _generate_one_image_bytes(client, model, quality, scene_text, calm_visuals=False):
    prompt = (
        "A clean, family-friendly educational illustration for this scene "
        f"(no text or captions in the image). {_CALM_VISUALS_STYLE if calm_visuals else ''}"
        f"Scene: {scene_text}"
    )[:32000]
    result = client.images.generate(model=model, prompt=prompt, size="1024x1024", quality=quality, n=1)
    return base64.b64decode(result.data[0].b64_json)


def generate_images(script, work_dir, model, quality, max_scenes, progress_cb=None, cancel_event=None,
                    calm_visuals=False):
    """Returns (image_paths, scene_texts) - scene_texts is kept so a single
    scene's image can be regenerated later without redoing the chunking."""
    client = get_client()
    scenes = chunk_script_into_scenes(script, max_scenes)
    image_paths = []

    for i, scene_text in enumerate(scenes):
        _check_cancel(cancel_event)
        if progress_cb:
            progress_cb(i, len(scenes), f"Illustrating scene {i + 1} of {len(scenes)}...")
        path = os.path.join(work_dir, f"frame_{i:03d}.png")
        try:
            image_bytes = _generate_one_image_bytes(client, model, quality, scene_text, calm_visuals)
            with open(path, "wb") as f:
                f.write(image_bytes)
            image_paths.append(path)
        except GenerationCancelled:
            raise
        except Exception as exc:
            raise GenerationError(f"Image generation failed on scene {i + 1}: {exc}") from exc

    return image_paths, scenes


def regenerate_scene_image(scene_text, image_path, model, quality, calm_visuals=False):
    """Redo a single storyboard scene's illustration in place (same file path)."""
    client = get_client()
    try:
        image_bytes = _generate_one_image_bytes(client, model, quality, scene_text, calm_visuals)
        with open(image_path, "wb") as f:
            f.write(image_bytes)
        return image_path
    except Exception as exc:
        raise GenerationError(f"Couldn't regenerate that scene's image: {exc}") from exc


# --------------------------------------------------------------------------
# Text-to-speech
# --------------------------------------------------------------------------
def _speak_chunk(client, model, voice, text, work_dir, instructions=None):
    kwargs = dict(model=model, voice=voice, input=text)
    if instructions:
        kwargs["instructions"] = instructions
    try:
        response = client.audio.speech.create(**kwargs)
    except TypeError:
        # Older SDK / model combinations may not accept `instructions`.
        kwargs.pop("instructions", None)
        response = client.audio.speech.create(**kwargs)
    tmp_path = os.path.join(work_dir, f"_speech_{uuid.uuid4().hex}.mp3")
    with open(tmp_path, "wb") as f:
        f.write(response.content)
    return tmp_path


def generate_voice_sample(text, voice, tts_model, work_dir):
    """One short TTS call, used by the Settings 'preview voice' button."""
    client = get_client()
    try:
        return _speak_chunk(
            client, tts_model, voice, text, work_dir,
            instructions="Narrate warmly and clearly, at a natural pace.",
        )
    except Exception as exc:
        raise GenerationError(f"Couldn't generate voice preview: {exc}") from exc


def generate_narration_audio(script, out_path, work_dir, model, voice, progress_cb=None, cancel_event=None):
    """Narrate a (possibly long) script, chunked to stay under TTS input limits."""
    client = get_client()
    chunks = chunk_text_by_length(script, config.NARRATION_CHUNK_CHARS)
    clips, tmp_files = [], []
    try:
        for i, chunk in enumerate(chunks):
            _check_cancel(cancel_event)
            if progress_cb:
                progress_cb(i, len(chunks), f"Narrating part {i + 1} of {len(chunks)}...")
            try:
                tmp_path = _speak_chunk(
                    client, model, voice, chunk, work_dir,
                    instructions="Narrate clearly and warmly, at a steady pace suitable for students.",
                )
            except GenerationCancelled:
                raise
            except Exception as exc:
                raise GenerationError(f"Narration failed on part {i + 1}: {exc}") from exc
            tmp_files.append(tmp_path)
            clips.append(AudioFileClip(tmp_path))

        final = clips[0] if len(clips) == 1 else concatenate_audioclips(clips)
        final.write_audiofile(out_path, codec="libmp3lame")
        if final is not clips[0]:
            final.close()
        return out_path
    finally:
        for c in clips:
            try:
                c.close()
            except Exception:
                pass
        for p in tmp_files:
            try:
                os.remove(p)
            except OSError:
                pass


# Used instead of each host's usual delivery when "Calm podcast hosts" is on.
_CALM_HOST_INSTRUCTIONS = (
    "Speak warmly and gently at a steady, even pace. Keep your volume and pitch "
    "consistent - no sudden changes, shouting or exaggerated excitement."
)


def generate_podcast_audio(script, out_path, work_dir, model, progress_cb=None, cancel_event=None,
                           timeline=None, calm_hosts=False):
    """Turn parsed Shimmer/Echo dialogue into one concatenated podcast mp3.

    If `timeline` is a list, a (speaker, text, start_seconds, end_seconds)
    tuple is appended to it for every turn - used to time the captions.
    """
    client = get_client()
    turns = parse_podcast_lines(script)
    if not turns:
        raise GenerationError("Could not find any Shimmer/Echo dialogue lines in the script.")

    clips, tmp_files = [], []
    elapsed = 0.0
    try:
        for i, (speaker, text) in enumerate(turns):
            _check_cancel(cancel_event)
            if progress_cb:
                progress_cb(i, len(turns), f"Recording line {i + 1} of {len(turns)} ({speaker})...")
            host = config.PODCAST_HOSTS.get(speaker, config.PODCAST_HOSTS["Echo"])
            instructions = _CALM_HOST_INSTRUCTIONS if calm_hosts else host["instructions"]
            turn_start = elapsed
            for sub_text in chunk_text_by_length(text, config.PODCAST_LINE_CHUNK_CHARS):
                try:
                    tmp_path = _speak_chunk(
                        client, model, host["voice"], sub_text, work_dir,
                        instructions=instructions,
                    )
                except GenerationCancelled:
                    raise
                except Exception as exc:
                    raise GenerationError(f"Narration failed on line {i + 1}: {exc}") from exc
                tmp_files.append(tmp_path)
                clips.append(AudioFileClip(tmp_path))
                elapsed += clips[-1].duration
            if timeline is not None:
                timeline.append((speaker, text, turn_start, elapsed))

        if not clips:
            raise GenerationError("No audio was generated for this podcast.")
        final = concatenate_audioclips(clips)
        final.write_audiofile(out_path, codec="libmp3lame")
        final.close()
        return out_path
    finally:
        for c in clips:
            try:
                c.close()
            except Exception:
                pass
        for p in tmp_files:
            try:
                os.remove(p)
            except OSError:
                pass


def audio_duration(path):
    clip = AudioFileClip(path)
    try:
        return clip.duration
    finally:
        clip.close()


# --------------------------------------------------------------------------
# Caption timing
# --------------------------------------------------------------------------
def transcribe_word_timings(audio_path, model, cancel_event=None):
    """Word-level timestamps for a narration track: [{"w", "s", "e"}, ...].

    Costs a fraction of a cent per minute of audio. The words come back
    without punctuation - captions.align_to_script() puts the script's own
    wording back on top of these timings.
    """
    _check_cancel(cancel_event)
    client = get_client()
    try:
        with open(audio_path, "rb") as f:
            result = client.audio.transcriptions.create(
                model=model, file=f, response_format="verbose_json",
                timestamp_granularities=["word"],
            )
    except Exception as exc:
        raise GenerationError(f"Couldn't time the captions: {exc}") from exc
    words = [
        {"w": w.word.strip(), "s": float(w.start), "e": float(w.end)}
        for w in (getattr(result, "words", None) or []) if w.word.strip()
    ]
    if not words:
        raise GenerationError("Couldn't time the captions: the transcription had no words.")
    return words


# --------------------------------------------------------------------------
# Quiz questions
# --------------------------------------------------------------------------
def _clean_quiz_questions(data):
    """Keep only well-formed questions: text, exactly 4 distinct choices, a valid answer."""
    questions = []
    for q in (data or {}).get("questions", []):
        try:
            question = str(q["question"]).strip()
            choices = [str(c).strip() for c in q["choices"]]
            answer = int(q["answer"])
        except (KeyError, TypeError, ValueError):
            continue
        if not question or len(choices) != 4 or not all(choices) or len(set(choices)) != 4:
            continue
        if not 0 <= answer < 4:
            continue
        questions.append({
            "question": question,
            "choices": choices,
            "answer": answer,
            "explanation": str(q.get("explanation", "")).strip(),
        })
    return questions


def generate_quiz(level, model, num_questions, lesson_text=None, topic=None, style=None):
    """Multiple-choice questions for a lesson's script (or, failing that, a bare
    topic). Returns [{"question", "choices": [4 strings], "answer": 0-3,
    "explanation"}, ...]."""
    if lesson_text:
        source = (
            "Base every question only on what this lesson teaches, so a student who "
            f"followed it can answer them all:\n\n{lesson_text[:12000]}"
        )
    else:
        source = f"The topic is: {topic}"

    rules = [
        "Each question has exactly 4 answer choices and exactly one correct answer.",
        "Wrong choices should be believable but clearly wrong to a student who understood the lesson.",
        "Never use 'all of the above' or 'none of the above'.",
        "Keep questions and choices short and easy to read.",
        "Mix easier recall questions with a few that check understanding.",
    ]
    if (style or {}).get("literal_language"):
        rules.append("Use clear, literal wording - no idioms, trick questions or double negatives.")

    client = get_client()
    try:
        text = _chat(
            client, model,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You write fair, clear multiple-choice quiz questions that check "
                        "whether a student understood a lesson. Reply with JSON only."
                    ),
                },
                {
                    "role": "user",
                    "content": _with_rules(
                        f"Write {num_questions} multiple-choice questions for a {level} "
                        f"student.\n\n{source}\n\n"
                        'Reply as JSON: {"questions": [{"question": "...", "choices": '
                        '["...", "...", "...", "..."], "answer": <index 0-3 of the correct '
                        'choice>, "explanation": "<one short sentence saying why>"}]}',
                        rules,
                    ),
                },
            ],
            max_tokens=200 + 250 * num_questions,
            response_format={"type": "json_object"},
        )
        questions = _clean_quiz_questions(json.loads(text))
    except json.JSONDecodeError as exc:
        raise GenerationError("The quiz came back in an unexpected format. Try again.") from exc
    except Exception as exc:
        raise GenerationError(f"Couldn't generate the quiz: {exc}") from exc
    if not questions:
        raise GenerationError("The model didn't return any usable quiz questions. Try again.")
    return questions[:num_questions]


# --------------------------------------------------------------------------
# Video assembly (moviepy v2 API)
# --------------------------------------------------------------------------
def _ken_burns_clip(img_path, duration, frame_height=576, zoom_amount=0.12):
    """A slow, alternating pan/zoom on a still image instead of a static frame -
    the single biggest cheap trick for making a slideshow look like a video."""
    base = ImageClip(img_path).with_duration(duration).with_effects([vfx.Resize(height=frame_height)])
    w, h = base.size
    zoom_in = (hash(os.path.basename(img_path)) % 2 == 0)

    def factor(t):
        progress = t / duration if duration > 0 else 0
        return 1 + zoom_amount * progress if zoom_in else 1 + zoom_amount * (1 - progress)

    zoomed = base.resized(factor)
    return CompositeVideoClip([zoomed.with_position("center")], size=(w, h)).with_duration(duration)


def assemble_video(image_paths, audio_path, out_path, progress_cb=None, ken_burns=True):
    if progress_cb:
        progress_cb(0, 1, "Rendering final video...")
    if not image_paths:
        raise GenerationError("No images were generated to build a video from.")

    audio = AudioFileClip(audio_path)
    per_image_duration = audio.duration / len(image_paths)
    clips = []
    try:
        for img_path in image_paths:
            if ken_burns:
                clip = _ken_burns_clip(img_path, per_image_duration)
            else:
                clip = (
                    ImageClip(img_path)
                    .with_duration(per_image_duration)
                    .with_effects([vfx.Resize(height=576)])
                )
            clips.append(clip)

        video = concatenate_videoclips(clips, method="compose").with_audio(audio)
        try:
            video.write_videofile(out_path, fps=24, audio_codec="aac")
        finally:
            video.close()
        return out_path
    finally:
        audio.close()
        for clip in clips:
            try:
                clip.close()
            except Exception:
                pass
