"""
A tiny procedural ambient-pad synthesizer for optional background music.

This does NOT download or bundle any third-party music track - everything
is generated on the fly from sine waves, so there are no licensing
questions to worry about. It won't sound like a produced music library, but
a soft, slow-moving pad under narration is enough to make a video feel less
bare, and it costs nothing (no API call, no asset).
"""

import math
import random
import wave

import numpy as np

SAMPLE_RATE = 44100

# A handful of pleasant, non-dissonant chords (major 7th / add9 flavored)
# to pick from at random, in Hz.
_CHORDS = [
    [261.63, 329.63, 392.00, 493.88],  # C E G B      (Cmaj7)
    [220.00, 277.18, 329.63, 415.30],  # A C# E G#    (Amaj7)
    [246.94, 311.13, 369.99, 466.16],  # B D# F# A#   (Bmaj7)
    [293.66, 349.23, 440.00, 523.25],  # D F A C      (Dm7)
    [196.00, 246.94, 293.66, 369.99],  # G B D F#     (Gmaj7)
]


def generate_ambient_bed(duration_seconds, out_path, seed=None, volume=0.12):
    """Write a `duration_seconds`-long ambient pad loop to `out_path` (mono WAV)."""
    rng = random.Random(seed)
    chord = rng.choice(_CHORDS)
    n_samples = max(1, int(duration_seconds * SAMPLE_RATE))
    t = np.arange(n_samples) / SAMPLE_RATE

    signal = np.zeros(n_samples, dtype=np.float64)
    for freq in chord:
        phase = rng.uniform(0, 2 * math.pi)
        detune = rng.uniform(-0.15, 0.15)          # tiny detune per voice = warmth
        lfo_rate = rng.uniform(0.03, 0.08)         # slow amplitude wobble = movement
        wobble = 1.0 + 0.08 * np.sin(2 * math.pi * lfo_rate * t)
        signal += np.sin(2 * math.pi * (freq + detune) * t + phase) * wobble

    fade_seconds = min(3.0, duration_seconds / 4) if duration_seconds > 0 else 0
    fade_samples = int(fade_seconds * SAMPLE_RATE)
    envelope = np.ones(n_samples, dtype=np.float64)
    if 0 < fade_samples < n_samples // 2:
        ramp = np.linspace(0.0, 1.0, fade_samples)
        envelope[:fade_samples] = ramp
        envelope[-fade_samples:] = ramp[::-1]

    peak = np.max(np.abs(signal))
    peak = peak if peak > 1e-6 else 1.0
    normalized = np.clip((signal / peak) * envelope * volume, -1.0, 1.0)
    pcm16 = (normalized * 32767).astype(np.int16)

    with wave.open(out_path, "w") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(pcm16.tobytes())
    return out_path


def mix_with_background(voice_clip, bed_path):
    """Layer a generated ambient bed under a voice AudioClip. Returns a new clip;
    caller is responsible for closing both the returned clip and `voice_clip`."""
    from moviepy import AudioFileClip, CompositeAudioClip

    bed = AudioFileClip(bed_path)
    if bed.duration > voice_clip.duration:
        bed = bed.subclipped(0, voice_clip.duration)
    mixed = CompositeAudioClip([voice_clip, bed])
    return mixed, bed


def add_background_music(voice_path, out_path, seed=None, volume=0.12):
    """Convenience wrapper: read `voice_path`, mix in a synthesized ambient bed,
    write the combined audio to `out_path`. Cleans up all intermediate clips."""
    import os as _os
    from moviepy import AudioFileClip

    voice = AudioFileClip(voice_path)
    bed_path = out_path + "._bed_tmp.wav"
    mixed = bed = None
    try:
        generate_ambient_bed(voice.duration, bed_path, seed=seed, volume=volume)
        mixed, bed = mix_with_background(voice, bed_path)
        mixed.write_audiofile(out_path, codec="libmp3lame")
        return out_path
    finally:
        voice.close()
        if bed is not None:
            bed.close()
        if mixed is not None:
            mixed.close()
        try:
            _os.remove(bed_path)
        except OSError:
            pass
