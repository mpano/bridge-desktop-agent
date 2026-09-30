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

The default maximum recording length is 15 seconds and can be changed with
`VOICE_RECORD_SECONDS`.

Bridge plays a short start sound before opening the microphone. By default, capture ends after
one second of quiet following sustained audio activity, or after five seconds without activity.
This is local RMS energy detection, not a neural speech detector: music/noise can keep capture
open until the maximum. Existing `.env` values still override defaults.

```dotenv
VOICE_RECORD_SECONDS=15
VOICE_AUTO_STOP=true
VOICE_SILENCE_SECONDS=1.0
VOICE_SPEECH_WAIT_SECONDS=5.0
VOICE_ACTIVITY_THRESHOLD=0.012
VOICE_START_SOUND=true
VOICE_SHORTCUT_ENABLED=true
```

Raise the activity threshold for noisy rooms or lower it for quiet microphones. Set
`VOICE_AUTO_STOP=false` for fixed-length capture. No-activity clips are not sent to transcription.

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
requires both flags below. The menu-bar panel's **Enable** wake-word action is itself an
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

For the custom Hey Bridge phrase, train/export `hey_bridge.onnx` using
`wakeword/bridge_custom_model.yml`, then install it explicitly:

```bash
bridge --install-wakeword /path/to/hey_bridge.onnx
```

The default installed location is
`~/Library/Application Support/Bridge/wakewords/hey_bridge.onnx`. You can override it with
`VOICE_WAKE_WORD_MODEL_PATH` when testing another reviewed ONNX model. The word detected is
determined by the installed model; Bridge never relabels another wake-word model as “Hey Bridge”.

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
**Enable** in the panel's wake-word card to opt in, and **Stop**
to release the microphone. There is no launchd daemon, login-item voice activation, or
spoken approval path. **Command–Shift–Space** starts one voice command while the menu-bar app
is running. It is a press-once shortcut, not hold-to-record. It does not interrupt active work
or approve pending actions. Set `VOICE_SHORTCUT_ENABLED=false` to disable it. If registration
conflicts with another application, the panel reports the failure and **Speak now** remains
available. Dashboard microphone capture is still future work.

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

Install the optional dependencies, stop any other Bridge instance, then launch:

```bash
python -m pip install -e '.[voice,wakeword,menubar]'
bridge --menubar
```

Click **Bridge** in the menu bar to open the native control panel:

The microphone orb shows the current voice phase; its motion is a status animation, not a
measured audio-level display. It becomes static with macOS Reduce Motion. The dark panel scrolls
on smaller screens, so its help and connection controls remain reachable. Result badges use the
actual service result: a transcript alone never produces a completed badge.

1. Press **Speak now** and speak when the panel says **Speak now**. This uses the same
   capture/transcription path as `--voice-once`, without requiring a wake-word model.
2. Watch recording → transcribing → working → speaking. Your transcript and response remain
   visible; long messages are abbreviated, with full results in the dashboard.
3. Click **Wake-word setup** for the **Hey Bridge** training and installation guide.
   Install a model trained for the phrase `hey bridge`, then press **Enable**. Say **Hey Bridge**,
   wait for the start sound, then give the command. With `VOICE_START_SOUND=false`, wake-word
   mode says **Yes?** instead.

The trained model is not bundled. A working microphone alone does not make wake-word detection
available. Renaming a pretrained model to `hey_bridge.onnx` does not change its trained phrase.
The new default path is separate from the old `bridge.onnx`; older models remain untouched.
`VOICE_WAKE_WORD_MODEL_PATH` still supports explicitly configured alternative models, whose
phrase is shown generically rather than guessed from a filename.

**Stop** ends listening and requests cancellation of an in-flight voice task. It cannot undo
completed actions. A bounded recording or local transcription may need time to finish cleanup.
Closing the panel keeps an enabled listener running; **Bridge ●** marks an active voice session.
Merely launching Bridge never starts the microphone. Microphone permission for Terminal and
the app launcher may differ; the panel links to **Microphone settings**.

Menu-bar voice submits commands through Bridge's already-running authenticated localhost API,
so there is one Agent/workflow database owner. Confirmation-required actions remain pending for
review in Bridge and are never approved by spoken input. When a task needs approval, listening
pauses and the panel offers **Review action**. Its native sheet shows the action and complete,
scrollable arguments. **Decline** is the default; **Approve once** requires an explicit click.
Reviews expire and apply to one exact action; the server independently validates the token and
security policy. Further approvals require another review. The dashboard remains available for
full results and recovery. After reviewing, explicitly enable listening again.
Errors distinguish missing models, shared assets, capture permission, and
transcription setup. Unexpected exceptions use a generic message without raw credentials.

See `docs/WAKEWORD_BRIDGE.md` for training and evaluating the custom “Hey Bridge” wake-word model.
