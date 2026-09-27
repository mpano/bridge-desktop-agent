# Bridge Voice

Bridge voice is optional. Installing or running Bridge normally does not start the microphone.

## Install

```bash
python -m pip install -e '.[voice]'
```

macOS must allow the process running Bridge to use the microphone:

System Settings > Privacy & Security > Microphone

## Push-to-talk / one command

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
VOICE_TTS_VOICE=
VOICE_TTS_RATE=
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

Wake-word detection uses openWakeWord locally. Bridge does not bundle or silently download a
wake-word model. Supply a model file you have reviewed. This makes it possible to train/use a
custom "Bridge" wake word later without sending ambient microphone audio to a cloud service.

After the wake word is detected, Bridge records one bounded command, transcribes it, routes the
text through the normal Agent, ToolRegistry, SecurityPolicy, and Executor, then speaks the result.

Voice never bypasses confirmations. If a requested action needs approval, Bridge says that the
action needs confirmation and leaves approval to the normal UI/CLI confirmation path. Voice
commands cannot automatically approve their own high-risk actions.

## Stop behavior

`Ctrl-C` stops the voice service. Background mode is a long-running process, not an automatic
login item. Bridge will not add itself to login items or start listening after reboot unless a
future explicit user-facing setting installs/enables that behavior.
