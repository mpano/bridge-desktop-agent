"""Personal "Hey Bridge" training: record the user, retrain the detector, install it.

Recordings stay in ~/Library/Application Support/Bridge/wakewords/voice. Training runs the
repository's scripts/train_wakeword_local.py, so it needs the developer environment with
`pip install -e '.[wakeword-training]'`.
"""

import asyncio
import importlib.util
import json
import re
import shutil
import sys
import threading
import wave
from collections.abc import Callable
from pathlib import Path

from app.desktop.voice import VoiceState
from app.voice.errors import VoiceError
from app.voice.paths import BRIDGE_SUPPORT_DIR, install_wake_word_model

PROJECT = Path(__file__).resolve().parents[2]
SCRIPT = PROJECT / "scripts/train_wakeword_local.py"
WORK = PROJECT / ".wakeword-training/run2"  # Keeps the synthetic-speech cache between runs.
VOICE = BRIDGE_SUPPORT_DIR / "wakewords/voice"
PHRASE_TAKES = 6
NEGATIVE_SENTENCE = "The bridge by the river is busy today."


def training_unavailable() -> str | None:
    if getattr(sys, "frozen", False) or not SCRIPT.is_file():
        return "Voice training needs the developer version of Bridge (run from the project)."
    missing = [
        name
        for name in ("sklearn", "onnx", "scipy", "openwakeword", "sounddevice")
        if importlib.util.find_spec(name) is None
    ]
    if missing:
        return "Install training tools first: pip install -e '.[voice,wakeword-training]'"
    return None


def peak(path: Path) -> int:
    import numpy as np

    with wave.open(str(path)) as audio:
        samples = np.frombuffer(audio.readframes(audio.getnframes()), dtype=np.int16)
    return int(np.abs(samples.astype(np.int32)).max()) if len(samples) else 0


async def record(recorder, seconds: float, target: Path) -> Path:
    path = await recorder.record_wav(seconds)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(path), target)
    return target


async def train(
    update: Callable[[VoiceState, str], None],
    stop: threading.Event,
    recorder=None,
    cue=None,
    run_training: Callable | None = None,
) -> Path | None:
    from app.voice.audio import MicrophoneRecorder
    from app.voice.cue import StartCue

    recorder = recorder or MicrophoneRecorder()
    cue = cue or StartCue()
    positive, background = VOICE / "phrase", VOICE / "background"
    for folder in (positive, background):
        shutil.rmtree(folder, ignore_errors=True)
        folder.mkdir(parents=True, exist_ok=True)

    take = 1
    retries = 0
    while take <= PHRASE_TAKES:
        if stop.is_set():
            update(VoiceState.STOPPED, "Voice training cancelled.")
            return None
        update(
            VoiceState.RECORDING, f"After the sound, say “Hey Bridge” ({take} of {PHRASE_TAKES})."
        )
        await cue.play()
        clip = await record(recorder, 2.2, positive / f"take-{take}.wav")
        if peak(clip) < 700:
            clip.unlink(missing_ok=True)
            retries += 1
            if retries > 4:
                raise VoiceError("I couldn’t hear you. Check the microphone and try again.")
            update(VoiceState.RECORDING, "I didn’t catch that — a little louder, please.")
            await asyncio.sleep(1.2)
            continue
        take += 1
        await asyncio.sleep(0.4)

    update(VoiceState.RECORDING, "Great! Now stay quiet for 4 seconds…")
    await asyncio.sleep(0.8)
    await record(recorder, 4, background / "quiet.wav")
    if stop.is_set():
        return None
    update(VoiceState.RECORDING, f"Last one — after the sound, say: “{NEGATIVE_SENTENCE}”")
    await asyncio.sleep(1.5)
    await cue.play()
    await record(recorder, 4.5, background / "sentence.wav")

    update(VoiceState.PROCESSING, "Learning your voice… this takes a few minutes the first time.")
    model = await (run_training or run_script)(positive, background, update, stop)
    if model is None:
        update(VoiceState.STOPPED, "Voice training cancelled.")
        return None
    installed = install_wake_word_model(model)
    update(
        VoiceState.READY,
        "“Hey Bridge” is ready. Turn on the wake-word switch to start listening.",
    )
    return installed


async def run_script(positive: Path, background: Path, update, stop) -> Path | None:
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        str(SCRIPT),
        "--output",
        str(WORK),
        "--personal",
        str(positive),
        "--background",
        str(background),
        cwd=str(PROJECT),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        while line := await process.stdout.readline():
            if stop.is_set():
                process.terminate()
                await process.wait()
                return None
            text = line.decode(errors="replace")
            done = re.search(r"exports: (\d+)/(\d+)", text)
            if done:
                percent = min(95, int(int(done.group(1)) * 100 / max(1, int(done.group(2)))))
                update(VoiceState.PROCESSING, f"Learning your voice… {percent}%")
            elif "Added your recordings" in text:
                update(VoiceState.PROCESSING, "Learning your voice… almost done")
        await process.wait()
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
    model = WORK / "hey_bridge.onnx"
    if process.returncode != 0 or not model.is_file():
        raise VoiceError("Training failed. See ~/Library/Logs/Bridge/bridge.log and try again.")
    report = json.loads((WORK / "evaluation.json").read_text())
    if report.get("synthetic_negative_clip_activation_rate", 1) > 0.2:
        raise VoiceError("The new model wakes up too easily, so it wasn’t installed. Try again.")
    return model
