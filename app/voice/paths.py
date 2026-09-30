import os
import shutil
import tempfile
from pathlib import Path

BRIDGE_SUPPORT_DIR = Path.home() / "Library/Application Support/Bridge"
DEFAULT_WAKE_WORD_MODEL = BRIDGE_SUPPORT_DIR / "wakewords/hey_bridge.onnx"


def wake_word_model_path(configured: Path | None) -> Path:
    return (configured or DEFAULT_WAKE_WORD_MODEL).expanduser().resolve()


def install_wake_word_model(source: Path) -> Path:
    source = source.expanduser().resolve()
    if not source.is_file() or source.suffix.lower() != ".onnx":
        raise ValueError("Bridge wake-word model must be an existing .onnx file.")
    destination = DEFAULT_WAKE_WORD_MODEL
    destination.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(destination.parent, 0o700)
    with tempfile.NamedTemporaryFile(
        prefix="bridge-wakeword-", suffix=".onnx", dir=destination.parent, delete=False
    ) as tmp:
        temporary = Path(tmp.name)
    try:
        shutil.copyfile(source, temporary)
        os.chmod(temporary, 0o600)
        os.replace(temporary, destination)
    finally:
        if temporary.exists():
            temporary.unlink()
    return destination
