"""
ADHD-friendly captions.

Each lesson's captions are stored as word-level timings in a small JSON
sidecar next to its media file (<id>.captions.json). They're grouped into
short on-screen phrases at display time rather than when saved, so the
"words per caption" setting can change without regenerating anything.

Where the timings come from:
  - Videos: the narration is transcribed with word timestamps, then the
    script's own words (with their punctuation and casing, which the
    transcription drops) are lined up against those timings.
  - Podcasts: every dialogue turn is recorded as its own clip, so each
    turn's start/end is known exactly; words are spread across their turn.
  - Older lessons with no sidecar: estimated from the script and the media
    length, so captions still work for anything already in the Library.
"""

import bisect
import difflib
import json
import os
import re

SIDECAR_SUFFIX = ".captions.json"

# How long a caption stays up after its last word if the next one hasn't
# started yet - long enough not to flicker during short pauses.
HOLD_SECONDS = 1.2

_TOKEN_RE = re.compile(r"\S+")
_MARKUP_CHARS = "*#_`>|~"


# --------------------------------------------------------------------------
# Sidecar files
# --------------------------------------------------------------------------
def sidecar_path(media_path):
    return os.path.splitext(media_path)[0] + SIDECAR_SUFFIX


def save(media_path, words):
    with open(sidecar_path(media_path), "w", encoding="utf-8") as f:
        json.dump({"version": 1, "words": words}, f)


def load(media_path):
    """Return the saved word timings for a lesson, or None if there aren't any."""
    path = sidecar_path(media_path)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            words = json.load(f).get("words") or []
    except (OSError, json.JSONDecodeError, AttributeError):
        return None
    return words or None


# --------------------------------------------------------------------------
# Tokens
# --------------------------------------------------------------------------
def _norm(token):
    return re.sub(r"[^\w]", "", token.lower())


def tokenize(text):
    """Split text into display words, dropping markdown leftovers like '**' or '#'."""
    tokens = []
    for raw in _TOKEN_RE.findall(text or ""):
        token = raw.strip(_MARKUP_CHARS)
        if token and _norm(token):
            tokens.append(token)
    return tokens


def _weight(token):
    """Rough relative speaking time for a word: its length, plus a pause after punctuation."""
    weight = len(_norm(token)) + 2
    if token.endswith((".", "!", "?")):
        weight += 6
    elif token.endswith((",", ";", ":")):
        weight += 3
    return weight


def _word(token, start, end, speaker=None):
    word = {"w": token, "s": round(start, 3), "e": round(end, 3)}
    if speaker:
        word["speaker"] = speaker
    return word


# --------------------------------------------------------------------------
# Building word timings
# --------------------------------------------------------------------------
def estimate_words(text, start, end, speaker=None):
    """Spread `text`'s words across [start, end] in proportion to their length."""
    tokens = tokenize(text)
    if not tokens or end <= start:
        return []
    weights = [_weight(t) for t in tokens]
    scale = (end - start) / sum(weights)
    words, t = [], start
    for token, weight in zip(tokens, weights):
        words.append(_word(token, t, t + weight * scale, speaker))
        t += weight * scale
    return words


def estimate_turns(turns, start, end):
    """Like estimate_words, for a list of (speaker, text) podcast turns sharing one span."""
    weights = [sum(_weight(t) for t in tokenize(text)) for _speaker, text in turns]
    total = sum(weights)
    if not total or end <= start:
        return []
    words, t = [], start
    for (speaker, text), weight in zip(turns, weights):
        span = (end - start) * weight / total
        words.extend(estimate_words(text, t, t + span, speaker))
        t += span
    return words


def words_from_timeline(timeline):
    """Word timings from (speaker, text, start, end) turns, as recorded while
    the podcast audio was assembled."""
    words = []
    for speaker, text, start, end in timeline:
        words.extend(estimate_words(text, start, end, speaker))
    return words


def align_to_script(script, timed_words):
    """Put the script's own words onto a transcription's word timings.

    Transcriptions drop punctuation and sometimes spell things differently
    ("1,000" vs "one thousand"), but their timing is accurate. Matching runs
    of words take the transcription's timing directly; anything that didn't
    match is spread across the gap between its matched neighbours.
    """
    tokens = tokenize(script)
    if not timed_words:
        return []
    if not tokens:
        return [_word(w["w"], w["s"], w["e"]) for w in timed_words]

    times = [None] * len(tokens)
    matcher = difflib.SequenceMatcher(
        None, [_norm(t) for t in tokens], [_norm(w["w"]) for w in timed_words], autojunk=False,
    )
    for block in matcher.get_matching_blocks():
        for k in range(block.size):
            timed = timed_words[block.b + k]
            times[block.a + k] = (timed["s"], timed["e"])

    audio_start = timed_words[0]["s"]
    audio_end = timed_words[-1]["e"]
    i = 0
    while i < len(tokens):
        if times[i] is not None:
            i += 1
            continue
        j = i
        while j < len(tokens) and times[j] is None:
            j += 1
        gap_start = times[i - 1][1] if i > 0 else audio_start
        gap_end = times[j][0] if j < len(tokens) else audio_end
        # Unmatched words squeezed between back-to-back matches still get a
        # brief moment on screen rather than zero-length timings.
        gap_end = max(gap_end, gap_start + 0.15 * (j - i))
        for k, w in enumerate(estimate_words(" ".join(tokens[i:j]), gap_start, gap_end)):
            times[i + k] = (w["s"], w["e"])
        i = j

    return [_word(token, s, e) for token, (s, e) in zip(tokens, times)]


# --------------------------------------------------------------------------
# On-screen phrases
# --------------------------------------------------------------------------
def build_cues(words, max_words=4):
    """Group words into short phrases: a new caption starts at a sentence end,
    after a comma once a couple of words are up, on a long pause, when the
    speaker changes, or once `max_words` is reached."""
    groups, current = [], []
    for word in words:
        if current and (
            len(current) >= max_words
            or word.get("speaker") != current[-1].get("speaker")
            or word["s"] - current[-1]["e"] > 0.8
        ):
            groups.append(current)
            current = []
        current.append(word)
        token = word["w"].rstrip("\"')]”’")
        if token.endswith((".", "!", "?", ":", ";")) or (token.endswith(",") and len(current) >= 2):
            groups.append(current)
            current = []
    if current:
        groups.append(current)
    return [
        {"start": g[0]["s"], "end": g[-1]["e"], "words": g, "speaker": g[0].get("speaker")}
        for g in groups
    ]


class CaptionTrack:
    """Fast "what's on screen at time t" lookups over a lesson's captions."""

    def __init__(self, words, max_words=4):
        self.cues = build_cues(words, max_words)
        self._starts = [cue["start"] for cue in self.cues]

    def at(self, t):
        """Return (cue_index, word_index) for playback time `t` (seconds), or
        (None, None) when nothing should be shown."""
        i = bisect.bisect_right(self._starts, t) - 1
        if i < 0:
            return None, None
        cue = self.cues[i]
        next_start = self._starts[i + 1] if i + 1 < len(self.cues) else float("inf")
        if t > min(cue["end"] + HOLD_SECONDS, next_start):
            return None, None
        word_index = 0
        for k, word in enumerate(cue["words"]):
            if word["s"] <= t:
                word_index = k
        return i, word_index


def bold_split(word):
    """Split a word for "bold word starts" focus reading: the first part of the
    word (about half its letters) is bolded to give the eye an anchor."""
    start = next((i for i, ch in enumerate(word) if ch.isalnum()), len(word))
    letters = sum(1 for ch in word if ch.isalnum())
    if letters == 0:
        return "", word
    count = 1 if letters <= 3 else (letters + 1) // 2
    end, seen = start, 0
    while end < len(word) and seen < count:
        if word[end].isalnum():
            seen += 1
        end += 1
    return word[:end], word[end:]
