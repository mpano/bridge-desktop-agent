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

Background microphone listening is OFF by default. Bridge refuses to start wake-word mode
unless both flags are explicitly enabled:

```dotenv
VOICE_BACKGROUND_ENABLED=true
VOICE_WAKE_WORD_ENABLED=true
VOICE_WAKE_WORD_MODEL_PATH=/absolute/path/to/bridge-wakeword.onnx
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
for model licensing and supported pretrained wake phrases. Point
`VOICE_WAKE_WORD_MODEL_PATH` to an actual `.onnx` wake model, not a TFLite file.
The word it detects is determined by that model; the service does not train or
automatically recognize a custom “Bridge” phrase.

Wake-word detection uses openWakeWord locally. Bridge does not bundle or silently download a
wake-word model. Supply a model file you have reviewed. This makes it possible to train/use a
custom "Bridge" wake word later without sending ambient microphone audio to a cloud service.

After the wake word is detected, Bridge records one bounded command, transcribes it, routes the
text through the normal Agent, ToolRegistry, SecurityPolicy, and Executor, then speaks the result.

Voice never bypasses confirmations. The terminal displays the action and exact
arguments, then asks for a typed `y` using the same approval handler as the text CLI.
Listening pauses while you review; Enter, EOF, or any answer other than `y`
declines. Approval expiry and single-use tokens still apply. Spoken words cannot
approve an action. Background action results are printed in the terminal, and TTS
failure does not mark a completed action as failed or retry it.

Run voice modes in a terminal. They own the same single-process database lock as
the CLI/dashboard/menu-bar service; stop that other instance first. This milestone
does not provide dashboard microphone capture, a global push-to-talk shortcut, or
voice control inside an already-running menu-bar server. It is an explicitly
started terminal listener, not a launchd daemon or login item.

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
