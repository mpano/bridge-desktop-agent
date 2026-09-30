"""Train an experimental openWakeWord classifier with local macOS synthetic speech.

No microphone, cloud service, downloaded audio, or runtime auto-installation. The
held-out speakers are excluded from fitting. This is not a production wake model.
"""

import argparse
import hashlib
import json
import subprocess
from pathlib import Path

import numpy as np
import onnx
from onnx import TensorProto, helper, numpy_helper
from openwakeword.model import Model
from openwakeword.utils import AudioFeatures
from scipy.io import wavfile
from sklearn.neural_network import MLPClassifier
from sklearn.preprocessing import StandardScaler

TRAIN_VOICES = [
    "Samantha",
    "Daniel",
    "Moira",
    "Rishi",
    "Tessa",
    "Fred",
    "Eddy (English (US))",
    "Flo (English (UK))",
]
TEST_VOICES = ["Grandma (English (US))", "Rocko (English (UK))"]
POSITIVES = ["Hey Bridge", "Hey, Bridge.", "Hey Bridge!", "Hey Bridge?"]
NEGATIVES = [
    "Hey fridge",
    "Hey Bridget",
    "Hey rich",
    "Hey beige",
    "Hey friend",
    "A bridge",
    "Bridge",
    "Bridges",
    "Fridge",
    "Hey Siri",
    "Hey Jarvis",
    "Hello there",
    "Open Spotify",
    "Open Chrome",
    "Play some music",
    "Pause the music",
    "Create a folder",
    "What's the weather today",
    "Tell me the time",
    "Yes please",
    "No thank you",
    "Turn off the lights",
    "Good morning",
    "That sounds great",
    "The bridge is over the river",
    "We should bridge the gap",
    "Put it in the fridge",
    "I am going to work",
    "Can you hear me",
    "What did you say",
    "Let's try again",
    "Read my email",
    "Open my project",
    "Search my files",
    "Take a screenshot",
    "Hey Brian",
    "Hey Brad",
    "Hey Brittany",
    "Hey Brady",
    "Hey Breeze",
    "Bridget called yesterday",
    "I need a break",
    "The meeting starts at nine",
    "That bridge looks beautiful",
    "Hey everybody",
    "Turn the volume down",
    "Can you open the browser",
    "This is a test",
    "Please wait a moment",
    "Thank you Bridge",
]

# Sound-alikes the detector must learn to reject ("bridge" without "hey", "hey br…").
# They get every speaking rate and every window, which cut synthetic false wakes from
# about 33% to 3% at the same recall threshold.
HARD_NEGATIVES = {
    "Bridge",
    "Bridges",
    "Fridge",
    "Hey fridge",
    "Hey Bridget",
    "Bridget called yesterday",
    "A bridge",
    "Hey Brian",
    "Hey Brad",
    "Hey Brittany",
    "Hey Brady",
    "Hey Breeze",
    "Thank you Bridge",
}
RATES = [140, 180, 220]
EXPORTS_PER_VOICE = len(POSITIVES) * len(RATES) + len(NEGATIVES) + len(HARD_NEGATIVES) * 2


def synthesize(directory, voice, phrase, rate):
    key = hashlib.sha256(f"{voice}|{phrase}|{rate}".encode()).hexdigest()[:24]
    path = directory / f"{key}.wav"
    if not path.exists():
        try:
            subprocess.run(
                [
                    "/usr/bin/say",
                    "-v",
                    voice,
                    "-r",
                    str(rate),
                    "-o",
                    str(path),
                    "--file-format=WAVE",
                    "--data-format=LEI16@16000",
                    phrase,
                ],
                check=True,
                capture_output=True,
                timeout=30,
            )
        except (subprocess.SubprocessError, OSError):
            path.unlink(missing_ok=True)  # Never cache an interrupted native export.
            raise
    sample_rate, data = wavfile.read(path)
    if sample_rate != 16000 or data.dtype != np.int16 or data.ndim != 1:
        raise ValueError("Expected mono 16 kHz PCM16 synthetic speech")
    if len(data) < 1600 or float(np.max(np.abs(data.astype(np.float32)))) < 100:
        raise ValueError(
            "Synthetic speech export is empty or silent. Run with access to macOS speech "
            "services and a fresh output directory; never train on these files."
        )
    return data


def augment(audio, rng, variation):
    # Fixed pre/post-roll provides full phrase windows with small alignment variation.
    leading = int((0.8 + rng.uniform(0, 0.25)) * 16000)
    gain = rng.uniform(0.35, 1.0) if variation else 0.75
    signal = audio.astype(np.float32) * gain
    if variation:
        delay = rng.integers(160, 1200)
        echoed = np.pad(signal, (delay, 0))[: len(signal)]
        signal = signal + echoed * rng.uniform(0.05, 0.25)
    padded = np.pad(signal, (leading, 16000))
    if variation:
        padded += rng.normal(0, rng.uniform(8, 80), len(padded))
    return (
        np.clip(padded, -32768, 32767).astype(np.int16),
        leading / 16000,
        (leading + len(audio)) / 16000,
    )


def stream_windows(features, clip):
    """Feature windows exactly as the live detector computes them: one per 80 ms chunk.

    Training on whole-clip (batch) features and detecting on streaming features made the
    detector fire on sound-alikes far more often than evaluation suggested.
    """
    features.reset()
    windows, ends = [], []
    for offset in range(0, len(clip) - 1279, 1280):
        features._streaming_features(clip[offset : offset + 1280])
        windows.append(features.get_features(16)[0].reshape(-1))
        ends.append((offset + 1280) / 16000)
    return windows[5:], ends[5:]  # openWakeWord ignores the first few frames.


def add_clip(features, clip, end, label, training, labels):
    for window, window_end in zip(*stream_windows(features, clip), strict=True):
        # Positives: windows that have just heard the whole phrase.
        if label and not (end - 0.08 <= window_end <= end + 0.55):
            continue
        training.append(window)
        labels.append(label)


def load_recording(path):
    sample_rate, data = wavfile.read(path)
    if sample_rate != 16000 or data.dtype != np.int16 or data.ndim != 1:
        raise ValueError(f"{path.name}: expected mono 16 kHz PCM16 audio")
    return data


def trim_to_speech(audio):
    """Cut a recording to where the voice is, so windows line up like synthetic clips."""
    level = np.abs(audio.astype(np.float32))
    loud = np.flatnonzero(level > max(400.0, float(level.max()) * 0.15))
    if len(loud) == 0:
        return None
    return audio[max(0, loud[0] - 1600) : loud[-1] + 1600]


def add_recordings(folder, label, features, rng, training, labels):
    """Real recordings count more than synthetic ones: each is augmented several times."""
    if folder is None:
        return 0
    count = 0
    for path in sorted(folder.glob("*.wav")):
        audio = load_recording(path)
        if label:
            audio = trim_to_speech(audio)
            if audio is None:
                continue
        count += 1
        for variation in range(6 if label else 2):
            if label:
                clip, _start, end = augment(audio, rng, variation % 2)
            else:
                clip, end = audio, 0.0
            add_clip(features, clip, end, label, training, labels)
    return count


def export_model(path, scaler, classifier):
    """ONNX graph: flatten → (dense+ReLU)×2 → dense → sigmoid. openWakeWord feeds [1,16,96]."""
    weights = [w.astype(np.float32) for w in classifier.coefs_]
    biases = [b.astype(np.float32) for b in classifier.intercepts_]
    # Fold standardization into the first layer: (x - mean) / scale · W + b.
    first = weights[0] / scaler.scale_[:, None].astype(np.float32)
    biases[0] = biases[0] - (scaler.mean_ / scaler.scale_).astype(np.float32) @ weights[0]
    weights[0] = first
    nodes = [helper.make_node("Flatten", ["features"], ["h0"], axis=1)]
    initializers = []
    for index, (weight, bias) in enumerate(zip(weights, biases, strict=True)):
        last = index == len(weights) - 1
        nodes += [
            helper.make_node("MatMul", [f"h{index}", f"w{index}"], [f"m{index}"]),
            helper.make_node("Add", [f"m{index}", f"b{index}"], [f"a{index}"]),
            helper.make_node(
                "Sigmoid" if last else "Relu", [f"a{index}"], ["score" if last else f"h{index + 1}"]
            ),
        ]
        initializers += [
            numpy_helper.from_array(weight, f"w{index}"),
            numpy_helper.from_array(bias.reshape(-1), f"b{index}"),
        ]
    graph = helper.make_graph(
        nodes,
        "hey_bridge",
        [helper.make_tensor_value_info("features", TensorProto.FLOAT, [1, 16, 96])],
        [helper.make_tensor_value_info("score", TensorProto.FLOAT, [1, 1])],
        initializers,
    )
    model = helper.make_model(graph, opset_imports=[helper.make_opsetid("", 17)], ir_version=8)
    helper.set_model_props(model, {"wake_phrase": "hey bridge", "quality": "synthetic-mlp"})
    onnx.checker.check_model(model)
    onnx.save(model, path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path(".wakeword-training"))
    parser.add_argument(
        "--personal", type=Path, help="Folder of 16 kHz mono WAVs of the user saying the phrase"
    )
    parser.add_argument(
        "--background", type=Path, help="Folder of 16 kHz mono WAVs without the phrase"
    )
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    audio_dir = output / "synthetic"
    audio_dir.mkdir(exist_ok=True)
    rng = np.random.default_rng(42)
    features = AudioFeatures(inference_framework="onnx", ncpu=1)
    training, labels, held_out = [], [], []
    exports = 0
    for voice in TRAIN_VOICES + TEST_VOICES:
        is_test = voice in TEST_VOICES
        for label, phrases in [(1, POSITIVES), (0, NEGATIVES)]:
            for phrase in phrases:
                hard = phrase in HARD_NEGATIVES
                rates = RATES if label or hard else [180]
                for rate in rates:
                    audio = synthesize(audio_dir, voice, phrase, rate)
                    exports += 1
                    if exports % 10 == 0:
                        total = EXPORTS_PER_VOICE * len(TRAIN_VOICES + TEST_VOICES)
                        print(f"Verified speech exports: {exports}/{total}", flush=True)
                    for variation in range(2):
                        clip, _start, end = augment(audio, rng, variation)
                        if is_test:
                            held_out.append((clip, label, voice))
                            continue
                        add_clip(features, clip, end, label, training, labels)
        print(f"Prepared voice: {voice} ({'held out' if is_test else 'training'})", flush=True)
    personal = add_recordings(args.personal, 1, features, rng, training, labels)
    background = add_recordings(args.background, 0, features, rng, training, labels)
    if personal or background:
        print(f"Added your recordings: {personal} phrase, {background} background", flush=True)
    x = np.asarray(training, dtype=np.float32)
    y = np.asarray(labels)
    scaler = StandardScaler().fit(x)
    # Balance classes by repeating positive windows; a small MLP separates "hey bridge"
    # from sound-alikes far better than a linear model (3.6% vs 15% synthetic false wakes).
    ratio = max(1, int((y == 0).sum() / max(1, y.sum())))
    index = np.concatenate([np.flatnonzero(y == 0), np.repeat(np.flatnonzero(y == 1), ratio)])
    classifier = MLPClassifier(
        hidden_layer_sizes=(64, 32), alpha=1e-2, max_iter=300, random_state=0, early_stopping=True
    )
    classifier.fit(scaler.transform(x)[index], y[index])
    path = output / "hey_bridge.onnx"
    export_model(path, scaler, classifier)
    print(f"Trained {len(y)} feature windows; evaluating held-out synthetic voices…", flush=True)
    detector = Model(wakeword_models=[str(path)], inference_framework="onnx")
    positives, negatives = [], []
    for clip, label, _voice in held_out:
        detector.reset()
        predictions = detector.predict_clip(clip, padding=0)
        peak = max(float(next(iter(score.values()))) for score in predictions)
        (positives if label else negatives).append(peak)
    report = {
        "phrase": "hey bridge",
        "quality": "personalized" if args.personal else "experimental; synthetic speech only",
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "training_voices": TRAIN_VOICES,
        "held_out_voices": TEST_VOICES,
        "training_windows": len(y),
        "positive_windows": int(y.sum()),
        "held_out_positive_clips": len(positives),
        "held_out_negative_clips": len(negatives),
        "threshold": 0.5,
        "synthetic_clip_recall": float(np.mean(np.asarray(positives) >= 0.5)),
        "synthetic_negative_clip_activation_rate": float(np.mean(np.asarray(negatives) >= 0.5)),
        "limitations": "Not tested on human speech, rooms, music or continuous background audio. "
        "These rates are clip metrics, not false activations per hour.",
    }
    # This gate deliberately controls installation recommendations, not just export.
    report["prototype_gate_passed"] = (
        report["synthetic_clip_recall"] >= 0.8
        and report["synthetic_negative_clip_activation_rate"] <= 0.05
    )
    (output / "evaluation.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2), flush=True)
    print("Model exported; no model was installed and microphone listening remains off.")


if __name__ == "__main__":
    main()
