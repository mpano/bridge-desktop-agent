# Bridge Voice

Bridge voice is optional. Installing or running Bridge normally does not start the microphone.

## Install

```bash
python -m pip install -e '.[voice]'
```

macOS must allow the process running Bridge to use the microphone:

System Settings > Privacy & Security > Microphone

## One command

This mode records a short bounded clip only after you start it:

```bash
python -m app.main --voice-once
# or, after editable installation:
bridge --voice-once
```

The default recording length is 6 seconds and can be changed with
`VOICE_RECORD_SECONDS`.

## Speech to text

The default is local:

```dotenv
VOICE_STT_PROVIDER=local
VOICE_LOCAL_WHISPER_MODEL=small
```

Local mode uses `faster-whisper`; recorded command audio stays on the Mac. The temporary
WAV file is deleted after transcription.

Model loading is offline by default. For an initial model download, explicitly run
with `VOICE_ALLOW_MODEL_DOWNLOAD=true`, then return it to `false` for normal use.
Alternatively set `VOICE_LOCAL_WHISPER_MODEL` to a directory containing model
weights you already installed. This flag only permits downloading model weights;
it does not upload microphone recordings. Loading/inference is performed on a worker
thread, and stopping waits for that worker before removing its audio file.

Remote STT is opt-in:

```dotenv
VOICE_STT_PROVIDER=openai
VOICE_OPENAI_STT_MODEL=gpt-transcribe
```

When remote STT is selected, the recorded command audio is uploaded to the configured
OpenAI transcription API. This is separate from whether Bridge's main LLM provider is local
or remote.

## Text to speech

Responses use the macOS `say` command locally. Bridge never invokes a shell for TTS.

Optional settings:

```dotenv
# Optional: omit these to use the system defaults.
VOICE_TTS_VOICE=Samantha
VOICE_TTS_RATE=180
```

## Wake word and background service

Background microphone listening is OFF by default. The terminal `--voice-service` mode
requires both flags below. The menu-bar **Start listening for “Bridge”** action is itself an
explicit session-level opt-in and enables them only for that listener session:

```dotenv
VOICE_BACKGROUND_ENABLED=true
VOICE_WAKE_WORD_ENABLED=true
VOICE_WAKE_WORD_THRESHOLD=0.5
```

Then run:

```bash
bridge --voice-service
```

Install wake-word dependencies separately:

```bash
python -m pip install -e '.[voice,wakeword]'
```

openWakeWord needs shared `melspectrogram.onnx` and `embedding_model.onnx` assets
in its package's `resources/models` directory, in addition to your wake-word model.
Provision these explicitly once using the upstream downloader (requires network
access and downloads pretrained wake models too):

```bash
python -c 'from openwakeword.utils import download_models; download_models()'
```

See [openWakeWord's setup instructions](https://github.com/dscripka/openWakeWord)
for model licensing and supported pretrained wake phrases.

For the custom Bridge phrase, train/export `bridge.onnx` using
`wakeword/bridge_custom_model.yml`, then install it explicitly:

```bash
bridge --install-wakeword /path/to/bridge.onnx
```

The default installed location is
`~/Library/Application Support/Bridge/wakewords/bridge.onnx`. You can override it with
`VOICE_WAKE_WORD_MODEL_PATH` when testing another reviewed ONNX model. The word detected is
determined by the installed model; Bridge never relabels another wake-word model as “Bridge”.

Wake-word detection stays local. Bridge does not silently download or fabricate a custom
wake-word model, and ambient microphone audio is not sent to a cloud wake-word service.

After the wake word is detected, Bridge records one bounded command, transcribes it, routes the
text through the normal Agent, ToolRegistry, SecurityPolicy, and Executor, then speaks the result.

Voice never bypasses confirmations. The terminal displays the action and exact
arguments, then asks for a typed `y` using the same approval handler as the text CLI.
Listening pauses while you review; Enter, EOF, or any answer other than `y`
declines. Approval expiry and single-use tokens still apply. Spoken words cannot
approve an action. Background action results are printed in the terminal, and TTS
failure does not mark a completed action as failed or retry it.

Standalone `--voice-once` and `--voice-service` modes run in a terminal and own the
same single-process database lock as the CLI/dashboard service. The menu-bar integration
avoids that conflict by submitting recognized commands through Bridge's already-running
authenticated localhost API, keeping one Agent and workflow database owner.

The menu-bar listener is still explicitly session-scoped: launching Bridge starts the local
dashboard service, but it does **not** start microphone listening. Choose
**Start listening for “Bridge”** from the menu to opt in, and **Stop Voice Listening**
to release the microphone. There is no launchd daemon, login-item voice activation, or
spoken approval path. A global push-to-talk shortcut and dashboard microphone capture are
still future work.

## Stop behavior

`Ctrl-C` stops the voice service. Background mode is a long-running process, not an automatic
login item. Bridge will not add itself to login items or start listening after reboot unless a
future explicit user-facing setting installs/enables that behavior.

Shutdown waits for a bounded recording (at most 30 seconds) or current local
transcription to release its resources. Forced process termination can leave a
private temporary WAV; orderly shutdown removes it. Microphone denial, missing
models, and transcription failures stop the listener with an actionable message
instead of retrying indefinitely. Normal CLI/API startup never starts the microphone.
The recording duration limit bounds normal capture, not a hung native audio driver;
Python cannot forcibly interrupt a blocked native call inside its worker thread.

## Verification

Automated tests mock microphones, STT models, wake detection, and speech processes.
Before relying on hands-free operation on your Mac, verify microphone permission,
one-shot recognition, a SAFE command, typed approval/decline, speech output,
wake activation with your chosen model, and Ctrl-C shutdown. These hardware and
model-quality checks cannot be established by the mock test suite.


## Bridge menu-bar voice

After installing the voice and wake-word extras and a reviewed custom model:

```bash
python -m pip install -e '.[voice,wakeword,menubar]'
bridge --install-wakeword /path/to/bridge.onnx
bridge --menubar
```

The menu shows **Start listening for “Bridge”** and **Stop Voice Listening**. Starting it is an
explicit session-level opt-in. Merely launching Bridge does not start the microphone.

Menu-bar voice submits commands through Bridge's already-running authenticated localhost API,
so there is one Agent/workflow database owner. Confirmation-required actions remain pending for
review in Bridge and are never approved by spoken input.

See `docs/WAKEWORD_BRIDGE.md` for training and evaluating the custom “Bridge” wake-word model.
