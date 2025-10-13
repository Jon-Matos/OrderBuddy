# realtime_stt.py
import argparse
import collections
import queue
import sys
import threading
import time

import numpy as np
import sounddevice as sd
import webrtcvad
from rich.console import Console
from rich.live import Live
from rich.panel import Panel
from faster_whisper import WhisperModel

console = Console()

# ---------------------------
# Audio/VAD configuration
# ---------------------------
SAMPLE_RATE = 16000           # Whisper likes 16 kHz
CHANNELS = 1
FRAME_MS = 30                 # 10 / 20 / 30 ms are valid for webrtcvad
FRAME_SIZE = int(SAMPLE_RATE * FRAME_MS / 1000)  # samples per frame
VAD_AGGRESSIVENESS = 2        # 0-3 (3 = most aggressive)
MAX_SEGMENT_SECONDS = 8.0     # cap a single utterance length
MIN_UTTERANCE_SECONDS = 0.6   # require at least this much voice
SILENCE_PAD_SECONDS = 0.25    # pad silence to avoid word truncation
TRAILING_SILENCE_MS = 400     # consider utterance ended after this much silence

# ---------------------------
# Model configuration
# ---------------------------
DEFAULT_MODEL = "medium"      
COMPUTE_TYPE = "auto"         # "float16"/"int8"/"int8_float16"/"auto"
LANG = None                   # e.g., "en" to lock; None = auto
BEAM_SIZE = 5

# ---------------------------
# Helpers
# ---------------------------
def bytes_to_float32(audio_bytes: bytes) -> np.ndarray:
    # sounddevice returns float32 already when dtype="float32"
    return np.frombuffer(audio_bytes, dtype=np.float32)

def float32_to_int16(x: np.ndarray) -> np.ndarray:
    # webrtcvad expects 16-bit mono PCM
    x = np.clip(x, -1.0, 1.0)
    return (x * 32768).astype(np.int16)

def frame_generator(stream_q, stop_event):
    """
    Pulls audio frames from the queue and yields (int16 bytes, timestamp).
    Each chunk from the input queue is float32; we slice to 30ms frames.
    """
    buf = np.zeros(0, dtype=np.float32)
    t0 = time.time()
    while not stop_event.is_set():
        try:
            chunk = stream_q.get(timeout=0.1)
            if chunk is None:  # sentinel
                break
            buf = np.concatenate([buf, chunk])
            # emit 30ms frames as they become available
            while len(buf) >= FRAME_SIZE:
                frame = buf[:FRAME_SIZE]
                buf = buf[FRAME_SIZE:]
                ts = time.time() - t0
                yield float32_to_int16(frame).tobytes(), ts
        except queue.Empty:
            continue

def collect_utterances(stream_q, stop_event):
    """
    Generator yielding numpy float32 utterance waveforms using VAD.
    """
    vad = webrtcvad.Vad(VAD_AGGRESSIVENESS)
    frames = frame_generator(stream_q, stop_event)
    ring = collections.deque(maxlen=int(SAMPLE_RATE * 2))  # rolling tail pad
    voiced = []
    last_voice_time = None
    utter_start_time = None

    silence_needed = TRAILING_SILENCE_MS / 1000.0
    max_frames = int(MAX_SEGMENT_SECONDS * 1000 / FRAME_MS)
    min_frames = int(MIN_UTTERANCE_SECONDS * 1000 / FRAME_MS)
    pad_frames = int(SILENCE_PAD_SECONDS * 1000 / FRAME_MS)

    for frame_bytes, _ts in frames:
        is_speech = vad.is_speech(frame_bytes, SAMPLE_RATE)
        frame_f32 = np.frombuffer(frame_bytes, dtype=np.int16).astype(np.float32) / 32768.0

        ring.extend(frame_f32)
        if is_speech:
            voiced.append(frame_f32)
            last_voice_time = time.time()
            if utter_start_time is None:
                utter_start_time = time.time()
        else:
            # no speech: still add, but as potential trailing silence
            if voiced:
                voiced.append(frame_f32)

        # finalize conditions
        # 1) long enough silence after speech
        if voiced and last_voice_time and (time.time() - last_voice_time) >= silence_needed:
            if len(voiced) >= min_frames:
                # pad small trailing silence for word completion
                pad = np.zeros(pad_frames * FRAME_SIZE, dtype=np.float32)
                utter = np.concatenate(voiced + [pad])
                yield utter
            # reset
            voiced = []
            last_voice_time = None
            utter_start_time = None

        # 2) too long utterance: cut to avoid heavy latency
        if voiced and (time.time() - utter_start_time) >= MAX_SEGMENT_SECONDS:
            utter = np.concatenate(voiced)
            yield utter
            voiced = []
            last_voice_time = None
            utter_start_time = None

    # drain on shutdown
    if voiced:
        yield np.concatenate(voiced)

# ---------------------------
# Transcription worker
# ---------------------------
class Transcriber(threading.Thread):
    def __init__(self, model_name=DEFAULT_MODEL, device="auto", compute_type=COMPUTE_TYPE, language=LANG):
        super().__init__(daemon=True)
        self._in_q = queue.Queue()
        self._out_q = queue.Queue()
        self._stop_event = threading.Event()   # renamed to avoid clashing with Thread internals
        self.model = WhisperModel(model_name, device=device, compute_type=compute_type)

        self.kwargs = dict(
            language=language,
            beam_size=BEAM_SIZE,
            vad_filter=True,              # extra VAD at model level
            no_speech_threshold=0.4,
            log_prob_threshold=-1.0
        )

    def run(self):
        while not self._stop_event.is_set():
            try:
                audio = self._in_q.get(timeout=0.1)
            except queue.Empty:
                continue
            if audio is None:
                break
            segments, info = self.model.transcribe(audio, **self.kwargs)
            text = "".join([s.text for s in segments]).strip()
            self._out_q.put(text)

    def submit(self, audio_f32: np.ndarray):
        self._in_q.put(audio_f32)

    def get_text_nowait(self):
        try:
            return self._out_q.get_nowait()
        except queue.Empty:
            return None

    def stop(self):
        self._stop_event.set()
        self._in_q.put(None)

# ---------------------------
# Live UI
# ---------------------------
def live_loop(transcriber: Transcriber, stop_event: threading.Event):
    buffer_lines = []
    with Live(Panel("\n".join(buffer_lines) or "[dim]Listening…[/dim]", title="OrderBuddy — Live STT", border_style="cyan"), refresh_per_second=8) as live:
        while not stop_event.is_set():
            text = transcriber.get_text_nowait()
            if text:
                buffer_lines.append(text)
                buffer_lines = buffer_lines[-50:]  # keep only the last N lines
                live.update(Panel("\n".join(buffer_lines) or "[dim]Listening…[/dim]", title="OrderBuddy — Live STT", border_style="cyan"))
            time.sleep(0.05)

# ---------------------------
# Main
# ---------------------------
def main():
    parser = argparse.ArgumentParser(description="Real-time microphone STT with VAD + faster-whisper")
    parser.add_argument("--model", default=DEFAULT_MODEL, help="Whisper model size (tiny/base/small/medium/large-v3, etc.)")
    parser.add_argument("--device", default="auto", help="auto/cpu/cuda")
    parser.add_argument("--lang", default=LANG, help="Force language code, e.g., en (default: auto)")
    parser.add_argument("--list-devices", action="store_true", help="List audio input devices and exit")
    parser.add_argument("--input-device", type=int, default=None, help="sounddevice input device index")
    args = parser.parse_args()

    if args.list_devices:
        print(sd.query_devices())
        return

    console.print(f"[bold]Loading Whisper model:[/bold] {args.model} (device={args.device})")
    transcriber = Transcriber(model_name=args.model, device=args.device, language=args.lang)
    transcriber.start()

    # audio stream -> queue of float32 chunks
    stream_q = queue.Queue()
    stop_event = threading.Event()

    def audio_callback(indata, frames, time_info, status):
        if status:
            console.log(f"[yellow]Audio status:[/yellow] {status}")
        # indata is float32 [-1,1], mono
        stream_q.put(indata.copy().flatten())

    # set up input stream
    console.print("[green]Starting microphone… Press Ctrl+C to stop.[/green]")
    with sd.InputStream(
        samplerate=SAMPLE_RATE,
        channels=CHANNELS,
        dtype="float32",
        blocksize=FRAME_SIZE,          # 30ms
        callback=audio_callback,
        device=args.input_device,
    ):
        # start VAD utterance collector
        collector_done = threading.Event()

        def collector_worker():
            for utter in collect_utterances(stream_q, stop_event):
                transcriber.submit(utter)
            collector_done.set()

        t_collector = threading.Thread(target=collector_worker, daemon=True)
        t_collector.start()

        # live UI loop
        try:
            live_loop(transcriber, stop_event)
        except KeyboardInterrupt:
            console.print("\n[red]Stopping…[/red]")
        finally:
            stop_event.set()
            stream_q.put(None)
            t_collector.join(timeout=2.0)
            transcriber.stop()
            transcriber.join(timeout=5.0)

    console.print("[bold green]Exited cleanly.[/bold green]")

if __name__ == "__main__":
    if sys.platform == "win32":
        # Low-latency timer improvements on Windows
        import ctypes
        timeBeginPeriod = ctypes.windll.winmm.timeBeginPeriod
        timeBeginPeriod(1)
    main()
