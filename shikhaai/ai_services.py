"""
All calls out to OpenAI, plus the moviepy assembly step, live here.

Notable fixes vs. the original script:
  - gpt-3.5-turbo -> configurable current model (gpt-4.1-mini by default).
    gpt-3.5-turbo is a legacy model OpenAI steers people away from now.
  - dall-e-3 -> gpt-image-1-mini / gpt-image-1.5 (the GPT Image family).
    DALL-E 2 and 3 were removed from the OpenAI API on May 12, 2026, so the
    original code would fail outright on a fresh API key.
  - GPT Image models always return base64 (`b64_json`), never a URL, so the
    old "requests.get(url)" download step is gone - we decode bytes directly.
  - tts-1 -> gpt-4o-mini-tts, which supports an `instructions` field so each
    podcast host can actually sound different (energetic vs. calm) instead
    of just using a different preset voice.
  - Long scripts are now split into chunks before TTS. tts-1 has a 4096
    character limit and gpt-4o-mini-tts has a 2000 *token* limit; the
    original code sent the whole script in one call, which would silently
    fail or get truncated on anything longer than a couple of paragraphs.
  - The podcast line parser now tolerates markdown bold ("**Shimmer:**"),
    stray spaces, and multi-line turns, instead of requiring an exact
    "Shimmer:" prefix on every single line.
  - Every network call is wrapped so failures raise a friendly
    GenerationError instead of an unhandled traceback that freezes the UI.
"""

import base64
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
def generate_script(topic, level, model):
    client = get_client()
    try:
        response = client.chat.completions.create(
            model=model,
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
                    "content": (
                        f"Write a narration script appropriate for a {level} student "
                        f"on this topic:\n\n{topic}\n\n"
                        "Keep paragraphs short (2-4 sentences) since each paragraph "
                        "will be paired with one illustration."
                    ),
                },
            ],
            max_tokens=900,
        )
        text = (response.choices[0].message.content or "").strip()
        if not text:
            raise GenerationError("The model returned an empty script. Try again.")
        return text
    except GenerationError:
        raise
    except Exception as exc:
        raise GenerationError(f"Couldn't generate the script: {exc}") from exc


def generate_podcast_script(topic, level, model):
    client = get_client()
    try:
        response = client.chat.completions.create(
            model=model,
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
                    "content": (
                        "Create a podcast-style dialogue between two AI hosts, "
                        "Shimmer and Echo. Shimmer is energetic and playful; Echo "
                        "is calm and thoughtful. The topic is: "
                        f"{topic} (for a {level} student). Make it engaging with "
                        "back-and-forth exchanges, light humor, and real-life "
                        "examples. Aim for roughly 20-30 exchanges total. Every "
                        "line must start with 'Shimmer:' or 'Echo:'."
                    ),
                },
            ],
            max_tokens=1800,
        )
        text = (response.choices[0].message.content or "").strip()
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
def _generate_one_image_bytes(client, model, quality, scene_text):
    prompt = (
        "A clean, family-friendly educational illustration for this scene "
        f"(no text or captions in the image): {scene_text}"
    )[:32000]
    result = client.images.generate(model=model, prompt=prompt, size="1024x1024", quality=quality, n=1)
    return base64.b64decode(result.data[0].b64_json)


def generate_images(script, work_dir, model, quality, max_scenes, progress_cb=None, cancel_event=None):
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
            image_bytes = _generate_one_image_bytes(client, model, quality, scene_text)
            with open(path, "wb") as f:
                f.write(image_bytes)
            image_paths.append(path)
        except GenerationCancelled:
            raise
        except Exception as exc:
            raise GenerationError(f"Image generation failed on scene {i + 1}: {exc}") from exc

    return image_paths, scenes


def regenerate_scene_image(scene_text, image_path, model, quality):
    """Redo a single storyboard scene's illustration in place (same file path)."""
    client = get_client()
    try:
        image_bytes = _generate_one_image_bytes(client, model, quality, scene_text)
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


def generate_podcast_audio(script, out_path, work_dir, model, progress_cb=None, cancel_event=None):
    """Turn parsed Shimmer/Echo dialogue into one concatenated podcast mp3."""
    client = get_client()
    turns = parse_podcast_lines(script)
    if not turns:
        raise GenerationError("Could not find any Shimmer/Echo dialogue lines in the script.")

    clips, tmp_files = [], []
    try:
        for i, (speaker, text) in enumerate(turns):
            _check_cancel(cancel_event)
            if progress_cb:
                progress_cb(i, len(turns), f"Recording line {i + 1} of {len(turns)} ({speaker})...")
            host = config.PODCAST_HOSTS.get(speaker, config.PODCAST_HOSTS["Echo"])
            for sub_text in chunk_text_by_length(text, config.PODCAST_LINE_CHUNK_CHARS):
                try:
                    tmp_path = _speak_chunk(
                        client, model, host["voice"], sub_text, work_dir,
                        instructions=host["instructions"],
                    )
                except GenerationCancelled:
                    raise
                except Exception as exc:
                    raise GenerationError(f"Narration failed on line {i + 1}: {exc}") from exc
                tmp_files.append(tmp_path)
                clips.append(AudioFileClip(tmp_path))

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
