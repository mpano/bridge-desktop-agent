# Custom “Bridge” wake word

Bridge uses openWakeWord for local wake-word detection. A model file is **not fabricated or
silently downloaded by the app**. Custom wake-word models need to be trained and tested for
false activations before distribution.

The upstream openWakeWord project provides an automated custom-model training workflow and a
Google Colab path. Train a model with the target phrase `bridge`, then export the ONNX model.

Recommended starting values:

```yaml
model_name: "bridge"
target_phrase:
  - "bridge"
custom_negative_phrases:
  - "fridge"
  - "abridge"
  - "bridges"
n_samples: 20000
n_samples_val: 2000
model_type: "dnn"
layer_size: 32
steps: 50000
max_negative_weight: 1500
target_false_positives_per_hour: 0.2
```

The upstream example recommends at least 20,000 positive samples and notes that larger sets can
improve quality. Treat those values as a starting point, not a guarantee.

After training, install the exported `bridge.onnx` with:

```bash
bridge --install-wakeword /path/to/bridge.onnx
```

Bridge copies it to:

```text
~/Library/Application Support/Bridge/wakewords/bridge.onnx
```

Then install the wake-word runtime:

```bash
python -m pip install -e '.[voice,wakeword]'
```

openWakeWord also needs its shared ONNX feature models. Follow the setup instructions in
`docs/VOICE.md`.

You can then launch the macOS menu bar:

```bash
bridge --menubar
```

Choose **Start listening for “Bridge”**. Clicking that item is an explicit session-level opt-in;
the microphone listener does not start merely because Bridge launches.

## Why the model is not committed yet

A binary named `bridge.onnx` without real training/evaluation would give a false sense of
reliability. Before bundling a default model, evaluate:

- false activations per hour in quiet rooms, music, TV, calls, and coding environments;
- recall for different speakers, accents, distances, and microphone devices;
- common confusions such as “fridge”, “abridge”, and “bridges”;
- threshold behavior around the default 0.5 score.

Once a tested model is available, place it under a versioned release asset or a reviewed
application resource and update the default installer accordingly.
