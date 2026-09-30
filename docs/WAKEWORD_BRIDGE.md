# Custom “Hey Bridge” wake word

The app is configured for **Hey Bridge**, but a trained model is not bundled. The native panel
keeps **Enable** unavailable until the configured model file exists. **Speak now** works without
one. The app does not download a model or start training when you open it.

## Train the phrase

Use the [upstream automatic training notebook](https://colab.research.google.com/github/dscripka/openWakeWord/blob/main/notebooks/automatic_model_training.ipynb)
in a separate Colab/Linux training environment. Upstream currently documents Linux support for
its automatic training pipeline; running the inference app on macOS does not imply that this
training pipeline runs natively on macOS. The notebook has older dependency pins, so its setup
must succeed in your selected runtime before generating data. This repository has not executed
or validated that hosted training run.

1. Open the notebook and follow its environment/data preparation sections. Review its downloads
   and runtime requirements before starting. Do not install training dependencies into Bridge's
   application virtual environment.
2. In the training configuration, set `model_name` to `hey_bridge` and `target_phrase` to
   `["hey bridge"]`. Use `wakeword/bridge_custom_model.yml` as the project configuration;
   set its dataset and generator paths to the actual paths prepared in the notebook.
3. Generate positive and negative samples, augment them, and train using the upstream workflow.
   Our configuration includes confusable negatives such as “hey fridge” and “hey Bridget”.
   Sample counts and score targets are initial training parameters, not measured guarantees.
4. Export/download **hey_bridge.onnx**, compatible with openWakeWord's ONNX feature pipeline.
   A renamed Jarvis model will still detect Jarvis. A model trained only for “Bridge” has not
   established recognition of the requested two-word phrase.

The full configuration is in [bridge_custom_model.yml](../wakeword/bridge_custom_model.yml).
See also the [upstream configuration reference](https://github.com/dscripka/openWakeWord/blob/main/examples/custom_model.yml).

## Install and use

After a real model has been trained and exported:

```bash
bridge --install-wakeword /path/to/hey_bridge.onnx
bridge --menubar
```

The installer copies the file privately to:

```text
~/Library/Application Support/Bridge/wakewords/hey_bridge.onnx
```

The original `bridge.onnx` location is not reused or automatically migrated. If `.env` contains
`VOICE_WAKE_WORD_MODEL_PATH`, it overrides the default; remove that override to use the installed
Hey Bridge model. Changes to `.env` require relaunching Bridge. The installer checks the file
extension and copies the model; its name alone cannot certify what phrase it recognizes.

The runtime requires the `voice,wakeword,menubar` extras and shared ONNX feature assets described
in [Voice setup](VOICE.md). From the panel press **Enable**, say **Hey Bridge**, wait for the start sound,
then give the command. Starting listening remains an explicit session-level action. The microphone
stays off at launch. Approvals require explicit review in the panel or dashboard, never speech.
With `VOICE_START_SOUND=false`, wake-word mode says **Yes?** instead of playing the cue.

## Local experimental training

`scripts/train_wakeword_local.py` provides a separate macOS-only prototype workflow using
installed system speech voices and the existing openWakeWord feature assets. It exports speech
to files without recording the microphone or playing it aloud. Install the `wakeword-training`
extra, then run from a terminal with access to macOS speech services:

```bash
python -m pip install -e '.[wakeword-training]'
python scripts/train_wakeword_local.py --output .wakeword-training/local
```

The script rejects empty/silent exports, fits on eight synthetic voices, and evaluates on two
held-out voices. It writes `hey_bridge.onnx` and `evaluation.json` to the output directory;
it never installs the model or enables listening. Its prototype gate requires at least 80%
positive-clip detection and no more than 5% negative-clip activation at threshold 0.5.
These are synthetic clip metrics, **not** false activations per hour or evidence of reliable
human speech recognition. Even a passing prototype needs the real-world evaluation below.
Do not install a failed prototype. Training output is ignored by Git.

Development status: a local run produced real speech clips, but the full training/evaluation
has not completed. Concurrent native speech exports timed out; the script uses sequential
exports. No evaluated model was installed from this attempt. **Hey Bridge reliability remains
unfinished**; use **Speak now** or **Command–Shift–Space** in the meantime.

## Evaluate before relying on hands-free use

Test the trained model with the actual microphone and speakers who will use it:

- Measure missed activations at normal speaking distance and with different accents.
- Measure false activations during silence, music, TV, conversations, and work calls.
- Try similar phrases such as “hey fridge”, “hey Bridget”, “a bridge”, and “fridge”.
- Begin at the default threshold of 0.5 and assess both missed and false detections before tuning.
- Exercise one SAFE command, a pending approval, and Stop without repeating an action.

Do not treat a successful export or model load as a successful recognition test. Store the tested
artifact and evaluation results together before distributing a default model.
