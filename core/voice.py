"""Voice I/O — always-on VAD capture, whisper STT, queued TTS.

Heavy dependencies (numpy, sounddevice, faster-whisper) are imported
only when voice mode actually starts, so text-only sessions stay light
and fast.

TTS backends: edge-tts (neural, needs network) with Windows SAPI
(pyttsx3, fully offline) as fallback; "auto" prefers edge and degrades
gracefully. Playback uses the Windows MCI API via ctypes — no pygame.
The mic pauses while AXIOM speaks to avoid feeding itself.
"""

from __future__ import annotations

import ctypes
import hashlib
import os
import queue
import re
import threading
import time
from typing import Callable, Optional

from .config import DATA_DIR

_TTS_CACHE = os.path.join(DATA_DIR, "tts_cache")
_SENTENCE_RX = re.compile(r"[^.!?\n]+[.!?\n]+")

_MD_FENCE_RX = re.compile(r"```.*?(?:```|$)", re.DOTALL)
_MD_LINK_RX = re.compile(r"\[([^\]]+)\]\([^)]+\)")
_MD_NOISE_RX = re.compile(r"[*_`#|>~]+")


def _clean_for_speech(text: str) -> str:
    """Strip markdown so TTS doesn't read symbols aloud."""
    text = _MD_FENCE_RX.sub(" ", text)
    text = _MD_LINK_RX.sub(r"\1", text)
    text = _MD_NOISE_RX.sub(" ", text)
    return re.sub(r"\s{2,}", " ", text).strip()

_HALLUCINATIONS = {
    "thank you", "thanks for watching", "thanks for listening", "please subscribe",
    "like and subscribe", "see you next time", "bye", "bye bye", "goodbye", "you",
    "okay", "ok", "um", "uh", "hmm", "ah", "oh", "yeah", "yes", "no",
    "subtitles by", "transcribed by", "caption", "captions", "subtitle", "subtitles",
}


# ── MP3 playback via Windows MCI (zero dependencies) ─────────────

def _mci(command: str) -> None:
    buf = ctypes.create_unicode_buffer(256)
    ctypes.windll.winmm.mciSendStringW(command, buf, 254, None)


def _play_mp3(path: str, stop_flag: threading.Event) -> None:
    alias = f"arc_{int(time.time() * 1000) % 1_000_000}"
    try:
        _mci(f'open "{path}" type mpegvideo alias {alias}')
        _mci(f"play {alias}")
        status = ctypes.create_unicode_buffer(64)
        while not stop_flag.is_set():
            ctypes.windll.winmm.mciSendStringW(f"status {alias} mode", status, 62, None)
            if status.value not in ("playing", ""):
                break
            time.sleep(0.05)
    finally:
        _mci(f"close {alias}")


class VoiceIO:
    def __init__(self, cfg, on_utterance: Callable[[str], None],
                 on_state: Callable[[str], None] = lambda s: None) -> None:
        self.cfg = cfg
        self.on_utterance = on_utterance
        self.on_state = on_state          # idle | listening | transcribing | speaking

        self._mic_active = False
        self._mic_paused = False
        self._stream = None
        self._whisper = None
        self._whisper_ready = threading.Event()

        # VAD state
        self._speech_buf: list = []
        self._speaking = False
        self._speech_start = 0.0
        self._silence_start = 0.0
        self._noise_floor = 0.0      # ambient RMS, adapts continuously

        # TTS
        self._tts_queue: "queue.Queue[Optional[str]]" = queue.Queue()
        self._tts_stop = threading.Event()
        self._tts_thread: Optional[threading.Thread] = None
        self._stream_text = ""
        self._in_code = False          # inside a ``` fence in the stream
        self._edge_down_until = 0.0
        self._tts_loop = None          # persistent asyncio loop for edge-tts
        self._sapi_engine = None       # cached pyttsx3 engine

    # ── Lifecycle ────────────────────────────────────────────────

    def start(self) -> str:
        """Start mic + STT. Returns a human-readable status line."""
        try:
            import numpy  # noqa: F401
            import sounddevice as sd
        except ImportError as e:
            return f"Voice unavailable — missing dependency: {e.name}. Run: pip install sounddevice numpy faster-whisper"

        threading.Thread(target=self._load_whisper, daemon=True,
                         name="whisper-load").start()
        try:
            self._stream = sd.InputStream(
                channels=1, samplerate=16000, dtype="float32",
                blocksize=1024, callback=self._audio_callback)
            self._stream.start()
        except Exception as e:
            return f"Microphone failed: {e}"
        self._mic_active = True
        if self._tts_thread is None:
            self._tts_thread = threading.Thread(target=self._tts_worker, daemon=True,
                                                name="tts-worker")
            self._tts_thread.start()
        return "Voice online — speak anytime. (Whisper loads in the background on first use.)"

    def stop(self) -> None:
        self._mic_active = False
        if self._stream is not None:
            try:
                self._stream.stop(); self._stream.close()
            except Exception:
                pass
            self._stream = None
        self.stop_speaking()

    @property
    def active(self) -> bool:
        return self._mic_active

    def _load_whisper(self) -> None:
        if self._whisper is not None:
            return
        self.on_state("loading")
        try:
            from faster_whisper import WhisperModel
            self._whisper = WhisperModel(self.cfg.whisper_model,
                                         device=self.cfg.whisper_device,
                                         compute_type=self.cfg.whisper_compute)
            self._whisper_ready.set()
            self.on_state("idle")
            print(f"[voice] whisper '{self.cfg.whisper_model}' ready")
        except Exception as e:
            self.on_state("idle")
            print(f"[voice] whisper failed to load: {e}")

    # ── Microphone / VAD ─────────────────────────────────────────

    def _audio_callback(self, indata, frames, time_info, status) -> None:
        if not self._mic_active or self._mic_paused:
            return
        import numpy as np
        audio = indata[:, 0] if indata.ndim > 1 else indata.flatten()
        rms = float(np.sqrt(np.mean(audio ** 2)))
        now = time.time()

        # Adapt to ambient noise: configured threshold is the floor, but a
        # noisy room raises the bar so fans/AC don't trigger transcription.
        threshold = max(self.cfg.vad_energy_threshold, self._noise_floor * 3.0)
        if rms < threshold:
            self._noise_floor = 0.95 * self._noise_floor + 0.05 * rms

        if rms > threshold:
            if not self._speaking:
                self._speaking = True
                self._speech_start = now
                self.on_state("listening")
            self._speech_buf.append(audio.copy())
            self._silence_start = 0.0
        elif self._speaking:
            self._speech_buf.append(audio.copy())
            if self._silence_start == 0.0:
                self._silence_start = now
            elif now - self._silence_start > self.cfg.vad_silence_timeout:
                duration = now - self._speech_start
                buf, self._speech_buf = self._speech_buf, []
                self._speaking = False
                self._silence_start = 0.0
                if duration >= self.cfg.vad_min_speech_duration and buf:
                    data = np.concatenate(buf)
                    threading.Thread(target=self._transcribe, args=(data,),
                                     daemon=True).start()
                else:
                    self.on_state("idle")

    def _transcribe(self, audio) -> None:
        if not self._whisper_ready.wait(timeout=30) or self._whisper is None:
            self.on_state("idle")
            return
        self.on_state("transcribing")
        try:
            import numpy as np
            audio = np.clip(audio.astype(np.float32), -1.0, 1.0)
            segments, _info = self._whisper.transcribe(
                audio,
                beam_size=max(1, int(self.cfg.whisper_beam_size)),
                temperature=0.0,
                vad_filter=True,
                condition_on_previous_text=False,
                no_speech_threshold=0.6,
            )
            parts = []
            for seg in segments:
                text = seg.text.strip()
                if not text or getattr(seg, "no_speech_prob", 0.0) > 0.7:
                    continue
                if text.lower().strip(".,!? ") in _HALLUCINATIONS:
                    continue
                parts.append(text)
            result = " ".join(parts).strip()
            if len(result.strip(".,!?… ")) < 2 or result.lower().strip(".,!? ") in _HALLUCINATIONS:
                self.on_state("idle")
                return
            # New request supersedes whatever is still queued to be spoken.
            self.stop_speaking()
            self.on_utterance(result)
        except Exception as e:
            print(f"[voice] transcription error: {e}")
            self.on_state("idle")

    # ── TTS ──────────────────────────────────────────────────────

    def speak(self, text: str) -> None:
        text = _clean_for_speech(text)
        if text and self.cfg.tts_backend != "off":
            if self._tts_thread is None:
                self._tts_thread = threading.Thread(target=self._tts_worker,
                                                    daemon=True, name="tts-worker")
                self._tts_thread.start()
            self._tts_queue.put(text)

    def speak_token(self, token: str) -> None:
        """Streaming mode: queue complete sentences as they form.

        Code blocks are skipped — nobody wants Python read aloud."""
        self._stream_text += token
        consumed = 0
        for m in _SENTENCE_RX.finditer(self._stream_text):
            chunk = self._skip_code(m.group(0)).strip()
            consumed = m.end()
            if len(chunk) >= 6:
                self.speak(chunk)
        if consumed:
            self._stream_text = self._stream_text[consumed:]

    def _skip_code(self, chunk: str) -> str:
        """Drop text inside ``` fences, tracking state across chunks."""
        if "```" not in chunk:
            return "" if self._in_code else chunk
        segs = chunk.split("```")
        spoken = []
        for idx, seg in enumerate(segs):
            if not self._in_code:
                spoken.append(seg)
            if idx < len(segs) - 1:
                self._in_code = not self._in_code
        return " ".join(spoken)

    def flush_speech(self) -> None:
        tail = self._skip_code(self._stream_text).strip()
        self._stream_text = ""
        self._in_code = False
        if tail:
            self.speak(tail)

    def stop_speaking(self) -> None:
        self._tts_stop.set()
        try:
            while True:
                self._tts_queue.get_nowait()
        except queue.Empty:
            pass

    def _tts_worker(self) -> None:
        while True:
            text = self._tts_queue.get()
            if text is None:
                return
            self._tts_stop.clear()
            self._mic_paused = True
            self.on_state("speaking")
            try:
                self._speak_one(text)
            except Exception as e:
                print(f"[voice] TTS error: {e}")
            finally:
                if self._tts_queue.empty():
                    self._mic_paused = False
                    self.on_state("idle")

    def _speak_one(self, text: str) -> None:
        backend = self.cfg.tts_backend
        if backend in ("auto", "edge") and time.time() >= self._edge_down_until:
            path = self._edge_synth(text)
            if path:
                _play_mp3(path, self._tts_stop)
                return
            if backend == "edge":
                return
            self._edge_down_until = time.time() + 60  # back off, retry later
        self._sapi_speak(text)

    def _get_tts_loop(self):
        """Return a persistent asyncio event loop for edge-tts."""
        import asyncio
        if self._tts_loop is None or self._tts_loop.is_closed():
            self._tts_loop = asyncio.new_event_loop()
        return self._tts_loop

    def _edge_synth(self, text: str) -> Optional[str]:
        """Synthesize with edge-tts, cached by content hash."""
        try:
            import edge_tts
        except ImportError:
            return None
        os.makedirs(_TTS_CACHE, exist_ok=True)
        key = hashlib.md5(f"{self.cfg.tts_voice}|{self.cfg.tts_rate}|"
                          f"{self.cfg.tts_pitch}|{text}".encode()).hexdigest()
        path = os.path.join(_TTS_CACHE, f"{key}.mp3")
        if os.path.exists(path) and os.path.getsize(path) > 0:
            return path
        try:
            comm = edge_tts.Communicate(text, voice=self.cfg.tts_voice,
                                        rate=self.cfg.tts_rate, pitch=self.cfg.tts_pitch)
            loop = self._get_tts_loop()
            loop.run_until_complete(comm.save(path))
            self._prune_cache()
            return path if os.path.getsize(path) > 0 else None
        except Exception:
            try:
                os.remove(path)
            except OSError:
                pass
            return None

    @staticmethod
    def _prune_cache(max_files: int = 300) -> None:
        try:
            files = [os.path.join(_TTS_CACHE, f) for f in os.listdir(_TTS_CACHE)]
            if len(files) <= max_files:
                return
            files.sort(key=os.path.getmtime)
            for f in files[:len(files) - max_files]:
                os.remove(f)
        except OSError:
            pass

    def _sapi_speak(self, text: str) -> None:
        try:
            import pyttsx3
            if self._sapi_engine is None:
                self._sapi_engine = pyttsx3.init()
                self._sapi_engine.setProperty("rate", 170)
            self._sapi_engine.say(text)
            self._sapi_engine.runAndWait()
        except Exception as e:
            self._sapi_engine = None  # reset on failure so next call retries
            print(f"[voice] SAPI fallback failed: {e}")
