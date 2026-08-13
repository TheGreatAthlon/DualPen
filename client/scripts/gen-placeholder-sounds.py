"""One-off generator for placeholder presence-sound assets.

Produces short synthesized tones standing in for the real audio described in
client/public/assets/sounds/README.md, so the Web Audio playback pipeline
(loading, decoding, looping, mixing) is testable end-to-end before real
sound design happens. Run with: python client/scripts/gen-placeholder-sounds.py
"""

import math
import struct
import wave
from pathlib import Path

OUT_DIR = Path(__file__).resolve().parent.parent / "public" / "assets" / "sounds"
SAMPLE_RATE = 44100


def _write_wav(path: Path, samples: list[float]) -> None:
    with wave.open(str(path), "wb") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(SAMPLE_RATE)
        frames = b"".join(
            struct.pack("<h", max(-32768, min(32767, int(s * 32767))))
            for s in samples
        )
        f.writeframes(frames)


def _tone(freq: float, duration: float, amplitude: float = 0.3) -> list[float]:
    n = int(SAMPLE_RATE * duration)
    # Short fade in/out on every tone to avoid audible clicks at loop seams.
    fade = max(1, int(SAMPLE_RATE * 0.005))
    out = []
    for i in range(n):
        env = 1.0
        if i < fade:
            env = i / fade
        elif i > n - fade:
            env = (n - i) / fade
        out.append(amplitude * env * math.sin(2 * math.pi * freq * i / SAMPLE_RATE))
    return out


def _silence(duration: float) -> list[float]:
    return [0.0] * int(SAMPLE_RATE * duration)


def _click(freq: float, amplitude: float = 0.35) -> list[float]:
    return _tone(freq, 0.03, amplitude)


def build_idle_tick_loop() -> list[float]:
    # Slow analog-clock-style tick: one short click per ~1s cycle.
    return _click(900) + _silence(0.94)


def build_typing_loop() -> list[float]:
    # Faster, busier mechanical clicking than the idle tick.
    cycle: list[float] = []
    for _ in range(4):
        cycle += _click(1400, amplitude=0.3) + _silence(0.09)
    return cycle


def build_elsewhere_click() -> list[float]:
    # Soft, muted, one-shot - lower pitch, quieter than the same-line sounds.
    return _click(500, amplitude=0.15)


def build_chat_notify() -> list[float]:
    # Short two-note chime, tonally distinct from the click/tick sounds.
    return _tone(660, 0.09, amplitude=0.3) + _silence(0.02) + _tone(880, 0.14, amplitude=0.3)


def build_peer_joined() -> list[float]:
    # Short rising two-note whoosh-like chime - distinct contour from the
    # falling "left" sound below so join/leave are distinguishable by ear
    # alone, and from chat_message_notify's brighter two-note interval.
    return _tone(392, 0.07, amplitude=0.25) + _silence(0.015) + _tone(523, 0.1, amplitude=0.25)


def build_peer_left() -> list[float]:
    # Mirror of build_peer_joined() with the interval inverted (falling
    # instead of rising), so it reads as the "opposite" event at a glance.
    return _tone(523, 0.07, amplitude=0.25) + _silence(0.015) + _tone(392, 0.1, amplitude=0.25)


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    assets = {
        "collab_same_line_idle_tick_loop.wav": build_idle_tick_loop(),
        "collab_same_line_typing_loop.wav": build_typing_loop(),
        "collab_elsewhere_typing_click.wav": build_elsewhere_click(),
        "chat_message_notify.wav": build_chat_notify(),
        "collab_peer_joined_doc.wav": build_peer_joined(),
        "collab_peer_left_doc.wav": build_peer_left(),
    }
    for filename, samples in assets.items():
        _write_wav(OUT_DIR / filename, samples)
        print(f"wrote {filename} ({len(samples) / SAMPLE_RATE:.3f}s)")


if __name__ == "__main__":
    main()
