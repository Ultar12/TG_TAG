import os
import threading
import uuid
from pathlib import Path

import torch
import torchaudio as ta
from flask import Flask, jsonify, request, send_file
from chatterbox.mtl_tts import ChatterboxMultilingualTTS

app = Flask(__name__)
DATA_DIR = Path(os.environ.get("VOICE_DATA_DIR", "/data/voices"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
LANGUAGE_ID = os.environ.get("VOICE_LANGUAGE_ID", "en")
DEVICE = os.environ.get("VOICE_DEVICE", "cuda" if torch.cuda.is_available() else "cpu")
MODEL_VERSION = os.environ.get("CHATTERBOX_MODEL_VERSION", "v3")
MODEL = None
MODEL_LOCK = threading.Lock()


def get_model():
    global MODEL
    if MODEL is None:
        with MODEL_LOCK:
            if MODEL is None:
                MODEL = ChatterboxMultilingualTTS.from_pretrained(device=DEVICE, t3_model=MODEL_VERSION)
    return MODEL


def require_token():
    expected = os.environ.get("VOICE_ENGINE_TOKEN", "").strip()
    if expected and request.headers.get("X-Voice-Engine-Token") != expected:
        return jsonify({"error": "unauthorized"}), 401
    return None


@app.get("/health")
def health():
    return jsonify({"ok": True, "device": DEVICE, "model": MODEL_VERSION})


@app.post("/clone")
def clone():
    denied = require_token()
    if denied:
        return denied
    sample = request.files.get("sample")
    if not sample:
        return jsonify({"error": "sample is required"}), 400
    voice_id = uuid.uuid4().hex
    voice_dir = DATA_DIR / voice_id
    voice_dir.mkdir(parents=True, exist_ok=False)
    source = voice_dir / "reference.wav"
    sample.save(source)
    try:
        waveform, sample_rate = ta.load(str(source))
        if waveform.shape[0] > 1:
            waveform = waveform.mean(dim=0, keepdim=True)
        if sample_rate != 24000:
            waveform = ta.functional.resample(waveform, sample_rate, 24000)
        ta.save(str(source), waveform, 24000)
    except Exception as exc:
        source.unlink(missing_ok=True)
        voice_dir.rmdir()
        return jsonify({"error": f"invalid audio sample: {exc}"}), 400
    return jsonify({"voice_id": voice_id})


@app.post("/synthesize")
def synthesize():
    denied = require_token()
    if denied:
        return denied
    payload = request.get_json(silent=True) or {}
    voice_id = str(payload.get("voice_id", "")).strip()
    text = str(payload.get("text", "")).strip()
    language_id = str(payload.get("language_id", LANGUAGE_ID)).strip() or LANGUAGE_ID
    if not voice_id or not text:
        return jsonify({"error": "voice_id and text are required"}), 400
    reference = DATA_DIR / voice_id / "reference.wav"
    if not reference.is_file():
        return jsonify({"error": "voice profile not found"}), 404
    output = DATA_DIR / voice_id / f"speech-{uuid.uuid4().hex}.wav"
    try:
        model = get_model()
        with MODEL_LOCK:
            wav = model.generate(text, language_id=language_id, audio_prompt_path=str(reference))
        ta.save(str(output), wav.cpu(), model.sr)
        return send_file(str(output), mimetype="audio/wav", as_attachment=True, download_name="speech.wav")
    except Exception as exc:
        output.unlink(missing_ok=True)
        return jsonify({"error": f"synthesis failed: {exc}"}), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("VOICE_ENGINE_PORT", "8000")))
