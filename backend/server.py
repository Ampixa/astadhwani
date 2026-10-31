"""Loopback-only Astadhwani studio. Model adapters are replaceable; audio stays local."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import hmac
import json
import logging
import math
import os
import queue
import secrets
import shutil
import socket
import sqlite3
import threading
import time
import uuid
from pathlib import Path

import numpy as np
import soundfile as sf
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from scipy.signal import resample_poly

ROOT = Path(__file__).resolve().parents[1]
MAX_UPLOAD = 25 * 1024 * 1024


class GenerateInput(BaseModel):
    text: str = Field(min_length=1, max_length=600)
    voice_id: str | None = None
    seed: int = Field(default=42, ge=0, le=2147483647)
    temperature: float = Field(default=0.8, ge=0.3, le=1.2)
    max_frames: int = Field(default=512, ge=64, le=1024)


class Wave1Engine:
    """The current Audio8-compatible adapter; replacing it need not change the API/UI."""

    def __init__(self, model_dir: Path, checksums: dict):
        self.model_dir = model_dir
        self.checksums = checksums
        self.model = None
        self.processor = None
        self.state = "idle"
        self.load_seconds = None

    def load(self):
        if self.model is not None:
            return
        if (
            not (self.model_dir / "model.safetensors").is_file()
            or not (self.model_dir / "codec.pth").is_file()
        ):
            raise ValueError(
                "The local checkpoint or codec is missing. Check the model directory in the launcher configuration."
            )
        self.state = "loading"
        for name, expected in self.checksums.items():
            with (self.model_dir / name).open("rb") as handle:
                actual = hashlib.file_digest(handle, "sha256").hexdigest()
            if actual != expected:
                raise ValueError(
                    "Model files do not match the configured checkpoint. Install a matching model profile before generating."
                )
        started = time.monotonic()
        import torch
        from transformers import AutoModel, AutoProcessor

        torch.set_num_threads(2)
        self.processor = AutoProcessor.from_pretrained(
            self.model_dir, trust_remote_code=True, local_files_only=True
        )
        self.model = AutoModel.from_pretrained(
            self.model_dir, trust_remote_code=True, local_files_only=True, dtype=torch.float32
        ).eval()
        self.model.load_codec(device="cpu")
        self.load_seconds = time.monotonic() - started
        self.state = "ready"

    def render(self, spec: dict, voice: dict | None, destination: Path, cancel: threading.Event):
        import torch
        from transformers import StoppingCriteria, StoppingCriteriaList

        self.load()
        if cancel.is_set():
            return None

        class CancelGeneration(StoppingCriteria):
            def __call__(self, input_ids, scores, **kwargs):
                return torch.full(
                    (input_ids.shape[0],),
                    cancel.is_set(),
                    device=input_ids.device,
                    dtype=torch.bool,
                )

        kwargs = {"text": [spec["text"]], "return_tensors": "pt"}
        if voice:
            # Studio references are canonical 44.1 kHz WAVs, avoiding an extra resampling backend.
            kwargs.update(reference_audio=[voice["path"]], reference_text=[voice["transcript"]])
        self.state = "generating"
        started = time.monotonic()
        with torch.inference_mode():
            inputs = self.processor(**kwargs)
            generated = self.model.generate(
                **inputs,
                max_new_tokens=spec["max_frames"],
                temperature=spec["temperature"],
                top_p=0.95,
                top_k=50,
                do_sample=True,
                generator=torch.Generator().manual_seed(spec["seed"]),
                return_dict_in_generate=True,
                stopping_criteria=StoppingCriteriaList([CancelGeneration()]),
            )
            if cancel.is_set():
                self.state = "ready"
                return None
            waves, lengths = self.model.decode_audio(generated.codes)
            wave = waves[0, : int(lengths[0])].float().cpu().numpy()
        if not wave.size or not np.isfinite(wave).all():
            raise ValueError(
                "The model returned empty or invalid audio. Try shorter text or another reference."
            )
        sf.write(destination, wave, int(self.model.config.codec_sample_rate), subtype="PCM_16")
        self.state = "ready"
        return {
            "duration": len(wave) / self.model.config.codec_sample_rate,
            "elapsed": time.monotonic() - started,
            "finished": bool(generated.finished[0]),
        }


class Studio:
    def __init__(self, data: Path, engine, profile: dict):
        self.data, self.engine, self.profile = data, engine, profile
        data.mkdir(parents=True, exist_ok=True)
        try:
            data.chmod(0o700)
        except OSError:
            pass
        for folder in ("voices", "audio", "temporary"):
            (data / folder).mkdir(exist_ok=True)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(data / "studio.sqlite3", check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS voices(id TEXT PRIMARY KEY, name TEXT NOT NULL, transcript TEXT NOT NULL, duration REAL, created REAL);
        CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY, text TEXT NOT NULL, voice_id TEXT, voice_name TEXT, seed INTEGER, temperature REAL, max_frames INTEGER, model_id TEXT, model_label TEXT, revision TEXT, status TEXT, created REAL, duration REAL, elapsed REAL, finished INTEGER, error TEXT);
        UPDATE jobs SET status='failed', error='The studio closed before this generation completed. You can generate it again.' WHERE status IN ('queued','loading','generating','cancelling');
        """)
        self.db.commit()
        for file in (data / "temporary").iterdir():
            if file.is_file():
                file.unlink()
        self.pending = queue.Queue()
        self.cancels = {}
        self.thread = threading.Thread(target=self.worker, daemon=True)
        self.thread.start()

    def rows(self, sql, args=()):
        with self.lock:
            return [dict(r) for r in self.db.execute(sql, args).fetchall()]

    def execute(self, sql, args=()):
        with self.lock:
            self.db.execute(sql, args)
            self.db.commit()

    def job(self, job_id):
        rows = self.rows("SELECT * FROM jobs WHERE id=?", (job_id,))
        if not rows:
            raise HTTPException(404, "Generation not found.")
        return rows[0]

    def worker(self):
        while True:
            job_id = self.pending.get()
            try:
                spec = self.job(job_id)
                event = self.cancels[job_id]
                if event.is_set():
                    self.execute("UPDATE jobs SET status='cancelled' WHERE id=?", (job_id,))
                    continue
                self.execute(
                    "UPDATE jobs SET status=? WHERE id=?",
                    ("loading" if self.engine.model is None else "generating", job_id),
                )
                self.engine.load()
                if not event.is_set():
                    self.execute("UPDATE jobs SET status='generating' WHERE id=?", (job_id,))
                voice = None
                if spec["voice_id"]:
                    values = self.rows("SELECT * FROM voices WHERE id=?", (spec["voice_id"],))
                    if not values:
                        raise ValueError("The reference voice is no longer available.")
                    voice = values[0] | {
                        "path": str(self.data / "voices" / f"{spec['voice_id']}.wav")
                    }
                dest = self.data / "audio" / f"{job_id}.wav"
                result = self.engine.render(spec, voice, dest, event)
                if event.is_set() or result is None:
                    dest.unlink(missing_ok=True)
                    self.execute("UPDATE jobs SET status='cancelled' WHERE id=?", (job_id,))
                else:
                    self.execute(
                        "UPDATE jobs SET status='completed',duration=?,elapsed=?,finished=? WHERE id=?",
                        (result["duration"], result["elapsed"], result["finished"], job_id),
                    )
            except Exception as error:
                logging.exception("Generation %s failed", job_id)
                self.engine.state = "ready" if self.engine.model is not None else "idle"
                self.execute(
                    "UPDATE jobs SET status='failed',error=? WHERE id=?", (str(error)[:400], job_id)
                )
            finally:
                with self.lock:
                    self.cancels.pop(job_id, None)
                self.pending.task_done()


def create_app(data: Path, model_dir: Path, web: Path, engine=None):
    profile = json.loads((ROOT / "backend" / "model-profile.json").read_text())
    studio = Studio(data, engine or Wave1Engine(model_dir, profile["artifact_checksums"]), profile)
    session = secrets.token_urlsafe(32)
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.state.studio = studio
    app.state.instance_id = secrets.token_hex(16)

    @app.middleware("http")
    async def local_only(request: Request, call_next):
        host = request.headers.get("host", "").split(":")[0]
        if host not in ("127.0.0.1", "localhost", "testserver"):
            return JSONResponse(
                {"detail": "This studio only accepts local connections."}, status_code=403
            )
        origin = request.headers.get("origin")
        if origin and origin != f"http://{request.headers.get('host')}":
            return JSONResponse({"detail": "Cross-origin access is not allowed."}, status_code=403)
        if request.url.path.startswith("/api/") and request.url.path != "/api/health":
            if not hmac.compare_digest(request.cookies.get("astadhwani_session", ""), session):
                return JSONResponse(
                    {"detail": "Open the studio page to start a local session."}, status_code=401
                )
            if (
                request.method not in ("GET", "HEAD")
                and request.headers.get("x-astadhwani") != "studio"
            ):
                return JSONResponse({"detail": "Invalid studio request."}, status_code=403)
        length = request.headers.get("content-length", "0")
        if length.isdigit() and int(length) > MAX_UPLOAD + 64 * 1024:
            return JSONResponse(
                {"detail": "Request is too large. Reference audio must be under 25 MiB."},
                status_code=413,
            )
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; media-src 'self' blob:; connect-src 'self'; frame-ancestors 'none'; object-src 'none'; base-uri 'none'"
        )
        return response

    @app.get("/api/health")
    def health():
        return {"status": "ok", "app": "astadhwani", "instance_id": app.state.instance_id}

    @app.get("/api/status")
    def status():
        return {
            "profile": profile,
            "engine": studio.engine.state,
            "model_available": (model_dir / "model.safetensors").is_file()
            and (model_dir / "codec.pth").is_file(),
            "load_seconds": studio.engine.load_seconds,
            "active": studio.rows(
                "SELECT * FROM jobs WHERE status IN ('queued','loading','generating','cancelling') ORDER BY created"
            ),
            "free_gib": round(shutil.disk_usage(data).free / 1024**3, 1),
        }

    @app.get("/api/voices")
    def voices():
        return studio.rows("SELECT * FROM voices ORDER BY created DESC")

    @app.post("/api/voices", status_code=201)
    async def add_voice(
        name: str = Form(...),
        transcript: str = Form(...),
        consent: bool = Form(...),
        audio: UploadFile = File(...),
    ):
        name, transcript = name.strip(), transcript.strip()
        if not name or len(name) > 60 or not transcript or len(transcript) > 1200 or not consent:
            raise HTTPException(
                422, "Provide a name, the exact transcript and permission to use this voice."
            )
        if shutil.disk_usage(data).free < 2 * 1024**3:
            raise HTTPException(507, "Keep at least 2 GiB of disk space free.")
        voice_id = uuid.uuid4().hex
        temp = data / "temporary" / voice_id
        target = data / "voices" / f"{voice_id}.wav"
        try:
            total = 0
            with temp.open("wb") as out:
                while chunk := await audio.read(1024 * 1024):
                    total += len(chunk)
                    if total > MAX_UPLOAD:
                        raise HTTPException(413, "Reference audio must be under 25 MiB.")
                    out.write(chunk)
            try:
                with sf.SoundFile(temp) as source:
                    duration = len(source) / source.samplerate
                    if not 1 <= duration <= 30 or source.channels > 2:
                        raise ValueError("Use a 1–30 second mono or stereo reference.")
                    rate = source.samplerate
                    wave = source.read(dtype="float32", always_2d=True).mean(axis=1)
                if not np.isfinite(wave).all() or np.sqrt(np.mean(wave**2)) < 0.0001:
                    raise ValueError("The reference is silent or contains invalid samples.")
                if rate != 44100:
                    divisor = math.gcd(rate, 44100)
                    wave = resample_poly(wave, 44100 // divisor, rate // divisor)
                sf.write(target, wave, 44100, subtype="PCM_16")
            except (ValueError, RuntimeError, OSError) as error:
                raise HTTPException(422, f"Cannot use that audio: {error}") from error
            studio.execute(
                "INSERT INTO voices VALUES(?,?,?,?,?)",
                (voice_id, name, transcript, duration, time.time()),
            )
            return studio.rows("SELECT * FROM voices WHERE id=?", (voice_id,))[0]
        except Exception:
            target.unlink(missing_ok=True)
            raise
        finally:
            temp.unlink(missing_ok=True)
            await audio.close()

    @app.get("/api/voices/{voice_id}/audio")
    def reference_audio(voice_id: str):
        if not studio.rows("SELECT id FROM voices WHERE id=?", (voice_id,)):
            raise HTTPException(404, "Voice not found.")
        return FileResponse(data / "voices" / f"{voice_id}.wav", media_type="audio/wav")

    @app.delete("/api/voices/{voice_id}")
    def delete_voice(voice_id: str):
        with studio.lock:
            if studio.rows(
                "SELECT id FROM jobs WHERE voice_id=? AND status IN ('queued','loading','generating','cancelling')",
                (voice_id,),
            ):
                raise HTTPException(
                    409, "Wait for the active generation before deleting this voice."
                )
            if not studio.rows("SELECT id FROM voices WHERE id=?", (voice_id,)):
                raise HTTPException(404, "Voice not found.")
            studio.execute("DELETE FROM voices WHERE id=?", (voice_id,))
            (data / "voices" / f"{voice_id}.wav").unlink(missing_ok=True)
        return {"deleted": True}

    @app.post("/api/generations", status_code=202)
    def generate(spec: GenerateInput):
        spec.text = " ".join(spec.text.split())
        if not spec.text:
            raise HTTPException(422, "Enter some text to speak.")
        if shutil.disk_usage(data).free < 2 * 1024**3:
            raise HTTPException(507, "Keep at least 2 GiB of disk space free.")
        with studio.lock:
            if studio.rows(
                "SELECT id FROM jobs WHERE status IN ('queued','loading','generating','cancelling')"
            ):
                raise HTTPException(
                    409, "One generation is already running. Wait or cancel it first."
                )
            voice_name = "Default voice"
            if spec.voice_id:
                found = studio.rows("SELECT name FROM voices WHERE id=?", (spec.voice_id,))
                if not found:
                    raise HTTPException(404, "Reference voice not found.")
                voice_name = found[0]["name"]
            job_id = uuid.uuid4().hex
            studio.execute(
                "INSERT INTO jobs(id,text,voice_id,voice_name,seed,temperature,max_frames,model_id,model_label,revision,status,created) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    job_id,
                    spec.text,
                    spec.voice_id,
                    voice_name,
                    spec.seed,
                    spec.temperature,
                    spec.max_frames,
                    profile["id"],
                    profile["label"],
                    profile["revision"],
                    "queued",
                    time.time(),
                ),
            )
            studio.cancels[job_id] = threading.Event()
            studio.pending.put(job_id)
        return studio.job(job_id)

    @app.get("/api/generations")
    def history():
        return studio.rows("SELECT * FROM jobs ORDER BY created DESC LIMIT 100")

    @app.get("/api/generations/{job_id}")
    def get_job(job_id: str):
        return studio.job(job_id)

    @app.post("/api/generations/{job_id}/cancel")
    def cancel_job(job_id: str):
        with studio.lock:
            job = studio.job(job_id)
            if job["status"] in ("queued", "loading", "generating", "cancelling"):
                studio.cancels[job_id].set()
                studio.execute("UPDATE jobs SET status='cancelling' WHERE id=?", (job_id,))
        return studio.job(job_id)

    @app.get("/api/generations/{job_id}/audio")
    def output(job_id: str, download: bool = False):
        job = studio.job(job_id)
        if job["status"] != "completed":
            raise HTTPException(409, "This audio is not ready.")
        return FileResponse(
            data / "audio" / f"{job_id}.wav",
            media_type="audio/wav",
            filename=f"astadhwani-{job_id[:8]}.wav" if download else None,
        )

    @app.delete("/api/generations/{job_id}")
    def delete_job(job_id: str):
        with studio.lock:
            job = studio.job(job_id)
            if job["status"] in ("queued", "loading", "generating", "cancelling"):
                raise HTTPException(
                    409, "Cancel and wait for generation to stop before deleting it."
                )
            studio.execute("DELETE FROM jobs WHERE id=?", (job_id,))
            (data / "audio" / f"{job_id}.wav").unlink(missing_ok=True)
        return {"deleted": True}

    if (web / "assets").is_dir():
        app.mount("/assets", StaticFiles(directory=web / "assets"), name="assets")

    @app.get("/")
    def home():
        if not (web / "index.html").is_file():
            raise HTTPException(503, "Build the web interface first.")
        response = FileResponse(web / "index.html", media_type="text/html")
        response.set_cookie("astadhwani_session", session, httponly=True, samesite="strict")
        return response

    return app


def lock_workspace(data: Path):
    data.mkdir(parents=True, exist_ok=True)
    handle = (data / ".engine.lock").open("a+")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as error:
        handle.close()
        raise RuntimeError(
            "This workspace already has a local engine. Use the URL in .engine.json instead of starting a second worker."
        ) from error
    return handle


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument(
        "--data", type=Path, default=Path.home() / "Library/Application Support/Astadhwani/Studio"
    )
    parser.add_argument(
        "--model",
        type=Path,
        default=Path(os.environ.get("ASTADHWANI_MODEL_DIR", ROOT / "models/wave1")),
    )
    parser.add_argument("--web", type=Path, default=ROOT / "builds/web/site")
    args = parser.parse_args()
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    os.environ["HF_HOME"] = str(args.data / "model-cache")
    os.environ["TOKENIZERS_PARALLELISM"] = "false"
    workspace_lock = lock_workspace(args.data)
    sock = socket.socket()
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", args.port))
    sock.listen(16)
    port = sock.getsockname()[1]
    import uvicorn

    app = create_app(args.data, args.model, args.web)
    info = args.data / ".engine.json"
    info.write_text(
        json.dumps(
            {
                "origin": f"http://127.0.0.1:{port}",
                "instance_id": app.state.instance_id,
                "pid": os.getpid(),
            }
        )
    )
    info.chmod(0o600)
    print(f"ASTADHWANI_READY=http://127.0.0.1:{port}", flush=True)
    try:
        uvicorn.Server(uvicorn.Config(app, log_level="warning", access_log=False)).run(
            sockets=[sock]
        )
    finally:
        info.unlink(missing_ok=True)
        workspace_lock.close()


if __name__ == "__main__":
    main()
