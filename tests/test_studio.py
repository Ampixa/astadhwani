import io
import sys
import time
from pathlib import Path

import numpy as np
import soundfile as sf
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from server import create_app


class Engine:
    model = object()
    state = "ready"
    load_seconds = 0
    calls = []

    def load(self):
        pass

    def render(self, spec, voice, destination, cancel):
        self.calls.append((spec, voice))
        # Wait for cancellation to be observable without a model dependency.
        for _ in range(20):
            if cancel.is_set():
                return None
            time.sleep(0.005)
        sf.write(destination, np.sin(np.arange(4410) * 0.08).astype("float32") * 0.1, 44100)
        return {"duration": 0.1, "elapsed": 0.1, "finished": True}


def setup(tmp_path):
    web = tmp_path / "web"
    web.mkdir()
    (web / "index.html").write_text("<html>studio</html>")
    model = tmp_path / "model"
    model.mkdir()
    client = TestClient(create_app(tmp_path / "data", model, web, engine=Engine()))
    client.get("/")
    client.headers["X-Astadhwani"] = "studio"
    return client


def wav(silent=False):
    b = io.BytesIO()
    sf.write(
        b, np.zeros(32000) if silent else 0.1 * np.sin(np.arange(32000) * 0.04), 16000, format="WAV"
    )
    return b.getvalue()


def wait(client, id):
    for _ in range(100):
        j = client.get("/api/generations/" + id).json()
        if j["status"] not in ("queued", "loading", "generating", "cancelling"):
            return j
        time.sleep(0.02)
    raise AssertionError("worker did not settle")


def test_local_session_and_cross_origin_protection(tmp_path):
    c = setup(tmp_path)
    assert c.get("/api/status").status_code == 200
    assert c.get("/api/status", headers={"Origin": "https://other.example"}).status_code == 403
    assert c.get("/api/status", headers={"Host": "evil.example"}).status_code == 403
    c.cookies.clear()
    assert c.get("/api/status").status_code == 401


def test_voice_resampling_cloning_and_history_delete(tmp_path):
    c = setup(tmp_path)
    assert (
        c.post(
            "/api/voices",
            data={"name": "test", "transcript": "नमस्कार", "consent": "false"},
            files={"audio": ("a.wav", wav())},
        ).status_code
        == 422
    )
    assert (
        c.post(
            "/api/voices",
            data={"name": "test", "transcript": "नमस्कार", "consent": "true"},
            files={"audio": ("a.wav", wav(True))},
        ).status_code
        == 422
    )
    r = c.post(
        "/api/voices",
        data={"name": "test", "transcript": "नमस्कार", "consent": "true"},
        files={"audio": ("a.wav", wav())},
    )
    assert r.status_code == 201, r.text
    voice = r.json()
    audio = c.get(f"/api/voices/{voice['id']}/audio")
    assert sf.info(io.BytesIO(audio.content)).samplerate == 44100
    j = c.post("/api/generations", json={"text": "नयाँ शब्द", "voice_id": voice["id"]}).json()
    assert c.post("/api/generations", json={"text": "again"}).status_code == 409
    assert c.delete("/api/voices/" + voice["id"]).status_code == 409
    done = wait(c, j["id"])
    assert done["status"] == "completed", done
    assert done["model_id"] == "wave1-ckpt6000"
    assert c.get(f"/api/generations/{j['id']}/audio").status_code == 200
    assert c.delete("/api/voices/" + voice["id"]).status_code == 200
    assert c.get(f"/api/generations/{j['id']}/audio").status_code == 200
    assert c.delete("/api/generations/" + j["id"]).status_code == 200
    assert c.get("/api/generations/" + j["id"]).status_code == 404


def test_cancel_does_not_leave_audio_or_lock_worker(tmp_path):
    c = setup(tmp_path)
    j = c.post("/api/generations", json={"text": "cancel me"}).json()
    assert c.post(f"/api/generations/{j['id']}/cancel").status_code == 200
    assert wait(c, j["id"])["status"] == "cancelled"
    assert c.get(f"/api/generations/{j['id']}/audio").status_code == 409
    nextjob = c.post("/api/generations", json={"text": "next"}).json()
    assert wait(c, nextjob["id"])["status"] == "completed"
    assert c.post("/api/generations", json={"text": "   "}).status_code == 422


def test_checkpoint_mismatch_is_rejected_before_loading_code(tmp_path):
    from server import Wave1Engine

    (tmp_path / "model.safetensors").write_bytes(b"wrong weights")
    (tmp_path / "codec.pth").write_bytes(b"codec")
    e = Wave1Engine(tmp_path, {"model.safetensors": "not-the-right-hash"})
    import pytest

    with pytest.raises(ValueError, match="do not match"):
        e.load()
    assert e.model is None


def test_workspace_lock_prevents_two_model_workers_and_releases_cleanly(tmp_path):
    import pytest
    from server import lock_workspace

    first = lock_workspace(tmp_path)
    try:
        with pytest.raises(RuntimeError, match="already has a local engine"):
            lock_workspace(tmp_path)
    finally:
        first.close()
    second = lock_workspace(tmp_path)
    second.close()
