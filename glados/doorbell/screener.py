"""
GLaDOS Doorbell Visitor Screening System

Handles the full screening flow:
  doorbell press → greeting → listen → transcribe → evaluate → reply → announce

Runs each session in a background thread so the HTTP endpoint returns immediately.
"""

from __future__ import annotations

import io
import json
import os
import struct
import subprocess
import threading
import time
import uuid
import wave
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import yaml
from loguru import logger

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
_AUDIO = Path(os.environ.get("GLADOS_AUDIO", "/app/audio_files"))
FFMPEG = os.environ.get("FFMPEG_PATH", "ffmpeg")   # ffmpeg on PATH in container
AUDIO_DIR = _AUDIO / "glados_doorbell"
SERVE_DIR = _AUDIO / "glados_ha"
CONFIG_PATH = Path(os.environ.get("GLADOS_CONFIG_DIR", "/app/configs")) / "doorbell.yaml"

# LLM system prompt for visitor evaluation
_EVAL_SYSTEM_PROMPT = """\
You are GLaDOS, an AI managing a smart home doorbell screening system.

A visitor has pressed the doorbell. Based on what they said, you must:
1. Classify them: delivery, guest, solicitor, service, unknown, no_response
2. Generate a SHORT reply to speak through the doorbell speaker (1-2 sentences, \
professional with subtle wit — you are an AI assistant, not a person)
3. Generate an indoor announcement for the residents (1 sentence, informative)
4. Decide if another conversational round is needed (true/false)

Reply ONLY with valid JSON, no markdown fencing:
{
  "classification": "delivery|guest|solicitor|service|unknown|no_response",
  "reply": "Your reply to the visitor",
  "announcement": "Indoor announcement for residents",
  "continue_conversation": false
}

Guidelines:
- delivery: "Thank you. You may leave the package at the door."
- guest: Ask who they are visiting if unclear, then announce.
- solicitor: Politely decline. "The residents are not interested."
- service: Ask for company name if unclear, then announce.
- unknown: Ask them to clarify.
- no_response: Note that someone rang but didn't respond.
- Keep replies professional but with a hint of artificial intelligence personality.
- Set continue_conversation=true only if you need more info (max 2 follow-ups)."""

_EVAL_USER_TEMPLATE = """\
Round {round} of doorbell screening.
{history_section}
Visitor's latest response: "{transcript}"

Evaluate and respond in JSON."""

_NO_RESPONSE_USER = """\
Round {round} of doorbell screening.
The visitor rang the doorbell but did not respond to the greeting after {timeout} seconds.

Evaluate and respond in JSON."""


class DoorbellScreener:
    """Manages doorbell visitor screening sessions."""

    def __init__(self) -> None:
        self._config = self._load_config()
        self._lock = threading.Lock()
        self._last_session_time: float = 0.0
        self._active_session: str | None = None
        AUDIO_DIR.mkdir(parents=True, exist_ok=True)
        SERVE_DIR.mkdir(parents=True, exist_ok=True)
        logger.success("DoorbellScreener initialized")

    def _load_config(self) -> dict:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return yaml.safe_load(f)

    def reload_config(self) -> None:
        self._config = self._load_config()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start_session(
        self,
        speakers: list[str] | None = None,
        max_rounds: int | None = None,
    ) -> dict:
        """Start a screening session. Returns immediately with session info."""
        cfg = self._config

        if not cfg.get("enabled", True):
            return {"status": "disabled", "message": "Doorbell screening is disabled"}

        # Cooldown check
        cooldown = cfg.get("cooldown", 60)
        elapsed = time.time() - self._last_session_time
        if self._active_session:
            return {
                "status": "cooldown",
                "message": "A screening session is already active",
                "active_session": self._active_session,
            }
        if elapsed < cooldown and self._last_session_time > 0:
            return {
                "status": "cooldown",
                "message": f"Screening on cooldown ({cooldown - elapsed:.0f}s remaining)",
            }

        session_id = f"db_{int(time.time())}_{uuid.uuid4().hex[:6]}"
        indoor_speakers = speakers or cfg.get("indoor_speakers", [])
        rounds = max_rounds or cfg.get("max_rounds", 3)

        self._active_session = session_id
        self._last_session_time = time.time()

        thread = threading.Thread(
            target=self._run_session,
            args=(session_id, indoor_speakers, rounds),
            name=f"doorbell-{session_id}",
            daemon=True,
        )
        thread.start()

        return {"status": "screening_started", "session_id": session_id}

    # ------------------------------------------------------------------
    # Session runner (background thread)
    # ------------------------------------------------------------------

    def _run_session(
        self, session_id: str, indoor_speakers: list[str], max_rounds: int
    ) -> None:
        """Main screening loop — runs in background thread."""
        cfg = self._config
        doorbell_speaker = cfg.get("speaker", "media_player.front_bell_speaker")
        history: list[dict] = []

        logger.success("[{}] Doorbell screening session started", session_id)

        try:
            for round_num in range(1, max_rounds + 1):
                logger.success("[{}] === Round {} ===", session_id, round_num)

                # --- Capture + play greeting concurrently ---
                capture_path = AUDIO_DIR / f"capture_{session_id}_r{round_num}.wav"

                # Start ffmpeg capture FIRST (so it's recording before greeting ends)
                max_listen = cfg.get("max_listen_duration", 15)
                greeting_dur = cfg.get("greeting_duration", 5.0)
                # Total capture = greeting duration + listen window
                total_capture = greeting_dur + max_listen
                ffmpeg_proc = self._start_capture(str(capture_path), total_capture)

                if ffmpeg_proc is None:
                    logger.error("[{}] Failed to start audio capture", session_id)
                    self._announce_inside(
                        "Someone rang the doorbell, but I was unable to screen them.",
                        indoor_speakers,
                    )
                    break

                # Play greeting (round 1) or follow-up prompt
                if round_num == 1:
                    greeting_wav = AUDIO_DIR / cfg.get("greeting_wav", "greeting.wav")
                    if greeting_wav.exists():
                        self._play_on_speaker(greeting_wav, doorbell_speaker)
                    else:
                        logger.warning("[{}] Greeting WAV not found: {}", session_id, greeting_wav)
                else:
                    # Previous round's reply is already playing — just listen
                    pass

                # Wait for greeting to finish playing on the speaker
                time.sleep(greeting_dur if round_num == 1 else 1.0)

                # --- Monitor for speech ---
                listen_timeout = cfg.get("listen_timeout", 12)
                silence_gap = cfg.get("silence_gap", 2.0)

                speech_detected = self._wait_for_speech(listen_timeout)

                if speech_detected:
                    logger.success("[{}] Speech detected, waiting for silence", session_id)
                    self._wait_for_silence(silence_gap, max_listen)
                    logger.success("[{}] Silence detected, stopping capture", session_id)
                else:
                    logger.success("[{}] No speech detected after {}s", session_id, listen_timeout)

                # Stop ffmpeg capture
                self._stop_capture(ffmpeg_proc)

                # Small delay to let ffmpeg flush
                time.sleep(0.3)

                # Convert raw PCM to WAV if needed, or validate WAV
                wav_path = self._ensure_valid_wav(capture_path)
                if wav_path is None:
                    logger.error("[{}] No valid audio captured", session_id)
                    if round_num == 1:
                        self._announce_inside(
                            "Someone rang the doorbell, but audio capture failed.",
                            indoor_speakers,
                        )
                    break

                # --- Transcribe ---
                if speech_detected:
                    transcript = self._transcribe(wav_path, greeting_dur if round_num == 1 else 0)
                    logger.success("[{}] Transcript: '{}'", session_id, transcript)
                else:
                    transcript = ""

                # --- Evaluate via LLM ---
                evaluation = self._evaluate(transcript, round_num, history)
                logger.success("[{}] Evaluation: {}", session_id, evaluation)

                classification = evaluation.get("classification", "unknown")
                reply = evaluation.get("reply", "")
                announcement = evaluation.get("announcement", "")
                continue_conv = evaluation.get("continue_conversation", False)

                history.append({
                    "round": round_num,
                    "transcript": transcript,
                    "classification": classification,
                    "reply": reply,
                })

                # --- Reply to visitor through doorbell speaker ---
                if reply:
                    reply_wav = self._generate_tts(reply, f"reply_{session_id}_r{round_num}")
                    if reply_wav:
                        self._play_on_speaker(reply_wav, doorbell_speaker)
                        # Wait for reply to finish playing
                        reply_dur = self._wav_duration(reply_wav)
                        time.sleep(reply_dur + 1.0)

                # --- Announce inside ---
                if announcement:
                    self._announce_inside(announcement, indoor_speakers)

                # Check if conversation should continue
                if not continue_conv or round_num >= max_rounds:
                    logger.success(
                        "[{}] Session complete after round {} (classification: {})",
                        session_id, round_num, classification,
                    )
                    break

        except Exception as exc:
            logger.error("[{}] Screening session error: {}", session_id, exc, exc_info=True)
            try:
                self._announce_inside(
                    "Doorbell screening encountered an error. Someone may be at the door.",
                    indoor_speakers,
                )
            except Exception:
                pass
        finally:
            self._active_session = None
            # Cleanup capture files
            for f in AUDIO_DIR.glob(f"capture_{session_id}_*"):
                try:
                    f.unlink()
                except OSError:
                    pass
            logger.success("[{}] Session cleanup complete", session_id)

    # ------------------------------------------------------------------
    # Audio capture via ffmpeg
    # ------------------------------------------------------------------

    def _start_capture(self, output_path: str, max_duration: float) -> subprocess.Popen | None:
        """Start ffmpeg to capture audio from RTSPS stream as raw PCM."""
        cfg = self._config
        stream_url = cfg.get("camera_stream", "")
        if not stream_url:
            logger.error("No camera_stream configured")
            return None

        # Output raw PCM (avoids WAV header issues on early termination)
        raw_path = output_path + ".raw"

        cmd = [
            FFMPEG,
            "-rtsp_transport", "tcp",
            "-i", stream_url,
            "-vn",                     # no video
            "-acodec", "pcm_s16le",    # 16-bit PCM
            "-ar", "16000",            # 16kHz for Whisper
            "-ac", "1",                # mono
            "-t", str(max_duration),   # hard time limit
            "-f", "s16le",             # raw PCM format (no header)
            "-y",                      # overwrite
            raw_path,
        ]

        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                stdin=subprocess.PIPE,
            )
            logger.debug("ffmpeg capture started (PID {}, max {}s)", proc.pid, max_duration)
            return proc
        except Exception as exc:
            logger.error("Failed to start ffmpeg: {}", exc)
            return None

    def _stop_capture(self, proc: subprocess.Popen) -> None:
        """Stop ffmpeg capture."""
        if proc.poll() is not None:
            return  # already finished

        # On Windows, stdin 'q' is unreliable — just terminate
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=3)

    def _ensure_valid_wav(self, capture_path: Path) -> Path | None:
        """Convert raw PCM to a valid WAV file. Returns WAV path or None."""
        raw_path = Path(str(capture_path) + ".raw")
        if not raw_path.exists():
            # Maybe ffmpeg wrote a WAV directly
            if capture_path.exists() and capture_path.stat().st_size > 44:
                return capture_path
            return None

        raw_size = raw_path.stat().st_size
        if raw_size < 1600:  # less than 0.05s of audio at 16kHz
            logger.warning("Captured audio too short ({} bytes)", raw_size)
            raw_path.unlink(missing_ok=True)
            return None

        # Wrap raw PCM in WAV header
        try:
            raw_data = raw_path.read_bytes()
            with wave.open(str(capture_path), "wb") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)  # 16-bit
                wf.setframerate(16000)
                wf.writeframes(raw_data)
            raw_path.unlink(missing_ok=True)
            logger.debug("Wrapped {} bytes of PCM into WAV: {}", raw_size, capture_path)
            return capture_path
        except Exception as exc:
            logger.error("Failed to create WAV from raw PCM: {}", exc)
            return None

    # ------------------------------------------------------------------
    # HA speaking sensor monitoring
    # ------------------------------------------------------------------

    def _get_ha_state(self, entity_id: str) -> str:
        """Get current state of an HA entity."""
        from glados.core.config_store import cfg
        ha_url = cfg.ha_url.rstrip("/")
        ha_token = cfg.ha_token

        url = f"{ha_url}/api/states/{entity_id}"
        req = Request(url, headers={
            "Authorization": f"Bearer {ha_token}",
            "Content-Type": "application/json",
        })
        try:
            with urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read().decode())
                return data.get("state", "unknown")
        except Exception:
            return "unknown"

    def _wait_for_speech(self, timeout: float) -> bool:
        """Wait for visitor to start speaking. Returns True if speech detected."""
        cfg = self._config
        sensor = cfg.get("speaking_sensor", "binary_sensor.front_bell_speaking")

        deadline = time.time() + timeout
        while time.time() < deadline:
            state = self._get_ha_state(sensor)
            if state == "on":
                return True
            time.sleep(0.5)
        return False

    def _wait_for_silence(self, gap: float, max_wait: float) -> None:
        """Wait for visitor to stop speaking (silence gap after speech)."""
        cfg = self._config
        sensor = cfg.get("speaking_sensor", "binary_sensor.front_bell_speaking")

        last_speech = time.time()
        deadline = time.time() + max_wait

        while time.time() < deadline:
            state = self._get_ha_state(sensor)
            if state == "on":
                last_speech = time.time()
            elif time.time() - last_speech >= gap:
                return  # silence long enough
            time.sleep(0.5)

    # ------------------------------------------------------------------
    # STT transcription
    # ------------------------------------------------------------------

    def _transcribe(self, wav_path: Path, skip_seconds: float = 0) -> str:
        """Transcribe WAV file via Faster-Whisper STT.

        If skip_seconds > 0, trims that many seconds from the start of the audio
        (to skip over the greeting that the doorbell speaker played back).
        """
        from glados.core.config_store import cfg as store_cfg

        audio_path = wav_path
        trimmed_path = None

        # Trim greeting portion if needed
        if skip_seconds > 0:
            trimmed_path = wav_path.parent / f"{wav_path.stem}_trimmed.wav"
            try:
                with wave.open(str(wav_path), "rb") as wf:
                    rate = wf.getframerate()
                    channels = wf.getnchannels()
                    sampwidth = wf.getsampwidth()
                    total_frames = wf.getnframes()
                    skip_frames = int(skip_seconds * rate)
                    if skip_frames >= total_frames:
                        logger.warning("Skip duration exceeds audio length")
                        return ""
                    wf.setpos(skip_frames)
                    remaining = wf.readframes(total_frames - skip_frames)

                with wave.open(str(trimmed_path), "wb") as wf:
                    wf.setnchannels(channels)
                    wf.setsampwidth(sampwidth)
                    wf.setframerate(rate)
                    wf.writeframes(remaining)

                audio_path = trimmed_path
            except Exception as exc:
                logger.warning("Failed to trim audio: {}", exc)
                # Fall back to untrimmed

        stt_url = store_cfg.service_url("stt")
        stt_model = self._config.get("stt_model", "Systran/faster-whisper-small")
        url = f"{stt_url}/v1/audio/transcriptions"

        try:
            wav_data = audio_path.read_bytes()
            body, content_type = self._build_multipart(
                wav_data, "capture.wav", stt_model, "en"
            )

            req = Request(url, data=body, headers={
                "Content-Type": content_type,
            })

            with urlopen(req, timeout=30) as resp:
                result = json.loads(resp.read().decode())
                text = result.get("text", "").strip()
                return text
        except Exception as exc:
            logger.error("STT transcription failed: {}", exc)
            return ""
        finally:
            if trimmed_path and trimmed_path.exists():
                trimmed_path.unlink(missing_ok=True)

    @staticmethod
    def _build_multipart(
        wav_data: bytes, filename: str, model: str, language: str
    ) -> tuple[bytes, str]:
        """Build multipart/form-data body for STT API."""
        boundary = uuid.uuid4().hex

        parts = []

        # File part
        parts.append(
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
            f"Content-Type: audio/wav\r\n\r\n"
        )
        file_header = parts[-1].encode()

        # Model part
        model_part = (
            f"\r\n--{boundary}\r\n"
            f'Content-Disposition: form-data; name="model"\r\n\r\n'
            f"{model}"
        ).encode()

        # Language part
        lang_part = (
            f"\r\n--{boundary}\r\n"
            f'Content-Disposition: form-data; name="language"\r\n\r\n'
            f"{language}"
        ).encode()

        # Closing boundary
        closing = f"\r\n--{boundary}--\r\n".encode()

        body = file_header + wav_data + model_part + lang_part + closing
        content_type = f"multipart/form-data; boundary={boundary}"
        return body, content_type

    # ------------------------------------------------------------------
    # LLM evaluation
    # ------------------------------------------------------------------

    def _evaluate(self, transcript: str, round_num: int, history: list[dict]) -> dict:
        """Call LLM to evaluate visitor response and decide next action."""
        from glados.core.config_store import cfg as store_cfg

        # Doorbell screener LLM picks up whatever model the operator
        # selected on the LLM & Services page (Ollama Autonomy slot).
        # Legacy `llm.port` / `llm.model` overrides in the screener's
        # own yaml are still honored for operators pinning a different
        # endpoint, but there is no hard-coded fallback: if nothing is
        # configured, the service_model helper returns "" and the
        # request fails loud.
        llm_cfg = self._config.get("llm", {})
        llm_port = llm_cfg.get("port")
        explicit_model = (llm_cfg.get("model") or "").strip()
        model = explicit_model or store_cfg.service_model("ollama_autonomy")
        if llm_port:
            ollama_url = f"http://localhost:{llm_port}"
        else:
            ollama_url = store_cfg.service_url("ollama_autonomy")

        # Build conversation history for context
        history_section = ""
        if history:
            lines = []
            for h in history:
                lines.append(f"  Round {h['round']}: Visitor said: \"{h['transcript']}\"")
                lines.append(f"  You replied: \"{h['reply']}\"")
                lines.append(f"  Classification: {h['classification']}")
            history_section = "Previous rounds:\n" + "\n".join(lines)

        if transcript:
            user_msg = _EVAL_USER_TEMPLATE.format(
                round=round_num,
                history_section=history_section,
                transcript=transcript,
            )
        else:
            user_msg = _NO_RESPONSE_USER.format(
                round=round_num,
                timeout=self._config.get("listen_timeout", 12),
            )

        messages = [
            {"role": "system", "content": _EVAL_SYSTEM_PROMPT},
            {"role": "user", "content": user_msg},
        ]

        payload = {
            "model": model,
            "messages": messages,
            "stream": False,
            "format": "json",
            "options": {
                "temperature": 0.6,
                "num_predict": 256,
            },
        }

        url = f"{ollama_url}/api/chat"
        data = json.dumps(payload).encode()
        req = Request(url, data=data, headers={"Content-Type": "application/json"})

        try:
            with urlopen(req, timeout=30) as resp:
                result = json.loads(resp.read().decode())
                content = result.get("message", {}).get("content", "")
                # Parse JSON from LLM response
                return json.loads(content)
        except json.JSONDecodeError as exc:
            logger.error("LLM returned invalid JSON: {}", exc)
            return {
                "classification": "unknown",
                "reply": "I'm sorry, I wasn't able to process that. One moment please.",
                "announcement": "Someone is at the door. I was unable to evaluate their response.",
                "continue_conversation": False,
            }
        except Exception as exc:
            logger.error("LLM evaluation failed: {}", exc)
            return {
                "classification": "unknown",
                "reply": "",
                "announcement": "Someone rang the doorbell. Screening system encountered an error.",
                "continue_conversation": False,
            }

    # ------------------------------------------------------------------
    # TTS generation
    # ------------------------------------------------------------------

    def _generate_tts(self, text: str, name_prefix: str) -> Path | None:
        """Generate TTS audio and save to serve directory. Returns path."""
        from glados.core.config_store import cfg as store_cfg

        tts_url = store_cfg.service_url("tts")
        url = f"{tts_url}/v1/audio/speech"

        payload = {
            "input": text,
            "voice": "glados",
            "response_format": "wav",
        }

        data = json.dumps(payload).encode()
        req = Request(url, data=data, headers={"Content-Type": "application/json"})

        try:
            with urlopen(req, timeout=15) as resp:
                wav_data = resp.read()

            filename = f"{name_prefix}.wav"
            wav_path = SERVE_DIR / filename
            wav_path.write_bytes(wav_data)
            logger.debug("TTS generated: {} ({} bytes)", wav_path, len(wav_data))
            return wav_path
        except Exception as exc:
            logger.error("TTS generation failed: {}", exc)
            return None

    # ------------------------------------------------------------------
    # Audio playback via HA
    # ------------------------------------------------------------------

    def _play_on_speaker(self, wav_path: Path, speaker: str) -> None:
        """Play a WAV file on an HA media_player entity."""
        from glados.core.config_store import cfg as store_cfg

        # Determine the URL to serve the file
        if wav_path.parent == SERVE_DIR:
            # Already in serve directory
            media_url = f"http://{store_cfg.serve_host}:{store_cfg.serve_port}/{wav_path.name}"
        else:
            # Copy to serve directory first
            dest = SERVE_DIR / wav_path.name
            dest.write_bytes(wav_path.read_bytes())
            media_url = f"http://{store_cfg.serve_host}:{store_cfg.serve_port}/{dest.name}"

        ha_url = store_cfg.ha_url.rstrip("/")
        ha_token = store_cfg.ha_token

        url = f"{ha_url}/api/services/media_player/play_media"
        payload = {
            "entity_id": [speaker],
            "media_content_id": media_url,
            "media_content_type": "music",
        }

        data = json.dumps(payload).encode()
        req = Request(url, data=data, headers={
            "Authorization": f"Bearer {ha_token}",
            "Content-Type": "application/json",
        }, method="POST")

        try:
            with urlopen(req, timeout=10) as resp:
                logger.debug("Played {} on {} (HTTP {})", wav_path.name, speaker, resp.status)
        except (HTTPError, URLError, OSError) as exc:
            logger.error("Failed to play audio on {}: {}", speaker, exc)

    def _announce_inside(self, text: str, speakers: list[str]) -> None:
        """Generate TTS and play announcement on indoor speakers."""
        if not speakers:
            logger.warning("No indoor speakers configured for announcement")
            return

        wav_path = self._generate_tts(text, f"doorbell_announce_{uuid.uuid4().hex[:8]}")
        if wav_path is None:
            return

        for speaker in speakers:
            self._play_on_speaker(wav_path, speaker)

        # Cleanup after delay (give HA time to download and play)
        def _cleanup():
            time.sleep(30)
            try:
                wav_path.unlink(missing_ok=True)
            except OSError:
                pass

        threading.Thread(target=_cleanup, daemon=True).start()

    # ------------------------------------------------------------------
    # Utility
    # ------------------------------------------------------------------

    @staticmethod
    def _wav_duration(wav_path: Path) -> float:
        """Get duration of a WAV file in seconds."""
        try:
            with wave.open(str(wav_path), "rb") as wf:
                return wf.getnframes() / wf.getframerate()
        except Exception:
            return 3.0  # safe fallback
