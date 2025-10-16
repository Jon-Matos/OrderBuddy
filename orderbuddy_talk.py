# orderbuddy_talk.py — STT -> GPT-4o-mini -> TTS voice reply (Trussed-ready)

import os, asyncio
import time
import queue
import argparse
import threading

import sounddevice as sd
import soundfile as sf
from openai import OpenAI

# ---- import your STT pieces (rename if your file is realtime_stt_new.py) ----
from realtime_stt import (
    SAMPLE_RATE, FRAME_SIZE, CHANNELS,
    collect_utterances, Transcriber
)
client = OpenAI(
    api_key=os.environ["OPENAI_API_KEY"],
    base_url=os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1"),
    timeout=60
)
# -------------------- OpenAI client (uses Trussed if provided) --------------------
def make_client():
    api_key = os.environ.get("OPENAI_API_KEY", "")
    base_url = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY not set")
    return OpenAI(api_key=api_key, base_url=base_url)

client = make_client()

# -------------------- Audio playback --------------------
def play_wav(path, device=None):
    data, sr = sf.read(path, dtype="float32")
    sd.play(data, sr, device=device)
    sd.wait()

def speak_tts(text: str, voice: str = "alloy", out_path: str = "reply.wav"):
    """
    Try FAU/Trussed TTS first. If that fails, try:
    1) edge-tts (free, no key) -> MP3
    2) pyttsx3 (offline) -> WAV
    (Optional) 3) gTTS (free, online) -> MP3
    Returns the path of the generated audio file, or None on total failure.
    """
    text = (text or "").strip()
    if not text:
        return None

    # ------------- Primary: your existing OpenAI/Trussed client -------------
    try:
        # Keep using your current client here
        with client.audio.speech.with_streaming_response.create(
            model="gpt-4o-mini-tts",
            voice=voice,        # your existing “alloy” is fine for Trussed/OpenAI
            input=text,
        ) as resp:
            resp.stream_to_file(out_path)
        return out_path
    except Exception as e:
        print(f"[TTS fallback] primary TTS failed: {e}")

    # ------------- Fallback 1: edge-tts (free, no API key) -------------------
    try:
        # Edge voices are different from OpenAI’s. Pick a common one:
        # See https://github.com/rany2/edge-tts for voice list tips
        import edge_tts

        # if user passed "alloy", map to a reasonable Edge voice
        edge_voice = os.getenv("EDGE_TTS_VOICE", "en-US-AriaNeural")

        async def _edge_to_file(t: str, v: str, mp3_path: str):
            communicate = edge_tts.Communicate(t, v)
            with open(mp3_path, "wb") as f:
                async for chunk in communicate.stream():
                    if chunk["type"] == "audio":
                        f.write(chunk["data"])

        edge_mp3 = os.path.splitext(out_path)[0] + ".mp3"
        asyncio.run(_edge_to_file(text, edge_voice, edge_mp3))
        return edge_mp3
    except Exception as e2:
        print(f"[TTS fallback] edge-tts failed: {e2}")

    # ------------- Fallback 2: pyttsx3 (offline, no internet) ----------------
    try:
        import pyttsx3
        engine = pyttsx3.init()
        # Optional: pick a different local voice if you like
        # for v in engine.getProperty("voices"): print(v.id)
        # engine.setProperty("voice", some_voice_id)

        wav_path = os.path.splitext(out_path)[0] + ".wav"
        engine.save_to_file(text, wav_path)
        engine.runAndWait()
        return wav_path
    except Exception as e3:
        print(f"[TTS fallback] pyttsx3 failed: {e3}")

    # ------------- (Optional) Fallback 3: gTTS (free, internet) --------------
    try:
        if os.getenv("ENABLE_GTTS", "0") == "1":
            from gtts import gTTS
            mp3_path = os.path.splitext(out_path)[0] + ".mp3"
            gTTS(text=text, lang="en").save(mp3_path)
            return mp3_path
    except Exception as e4:
        print(f"[TTS fallback] gTTS failed: {e4}")

    print("[TTS fallback] all TTS attempts failed")
    return None

def chat_reply(user_text: str, system_prompt: str):
    if not user_text.strip():
        return ""
    r = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_text},
        ],
        temperature=0.5,
    )
    return (r.choices[0].message.content or "").strip()

# -------------------- Main --------------------
def main():
    p = argparse.ArgumentParser(description="OrderBuddy voice chat (STT + GPT-4o-mini + TTS)")
    p.add_argument("--stt-model", default="small", help="tiny/base/small/medium/large-v3")
    p.add_argument("--device", default="auto", help="STT compute device: auto/cpu/cuda")
    p.add_argument("--lang", default=None, help="Force language code (e.g., en). None = auto")
    p.add_argument("--input-device", type=int, default=None, help="Mic device index for sounddevice")
    p.add_argument("--output-device", type=int, default=None, help="Playback device index (e.g., headphones)")
    p.add_argument("--voice", default="alloy", help="TTS voice: alloy/verse/serene/sage/…")
    p.add_argument("--system", default=(
        "You are OrderBuddy, a friendly, concise voice assistant for fast, clear conversations. "
        "Keep replies brief (1–2 sentences) and easy to speak aloud."
    ))
    args = p.parse_args()

    # ---- start STT transcriber ----
    transcriber = Transcriber(model_name=args.stt_model, device=args.device, language=args.lang)
    transcriber.start()

    # audio input -> queue
    stream_q = queue.Queue()
    stop = threading.Event()

    def audio_callback(indata, frames, time_info, status):
        if status:
            print(f"[Audio status] {status}")
        stream_q.put(indata.copy().flatten())

    print("🎙️  Microphone on — speak, then pause briefly. Ctrl+C to quit.")
    try:
        with sd.InputStream(
            samplerate=SAMPLE_RATE,
            channels=CHANNELS,
            dtype="float32",
            blocksize=FRAME_SIZE,
            callback=audio_callback,
            device=args.input_device,
        ):
            def collector_worker():
                for utter in collect_utterances(stream_q, stop):
                    transcriber.submit(utter)

            t_collector = threading.Thread(target=collector_worker, daemon=True)
            t_collector.start()

            while not stop.is_set():
                text = transcriber.get_text_nowait()
                if not text:
                    time.sleep(0.05)
                    continue

                user_text = text.strip()
                print(f"\n🗣️  You: {user_text}")

                try:
                    reply = chat_reply(user_text, args.system)
                except Exception as e:
                    print(f"OpenAI chat error: {e}")
                    reply = "I hit an error talking to the model."

                print(f"🤖 OrderBuddy: {reply}")

                try:
                    out = speak_tts(reply, voice=args.voice, out_path="reply.wav")
                    if out:
                        play_wav(out, device=args.output_device)
                except Exception as e:
                    print(f"TTS/playback error: {e}")

    except KeyboardInterrupt:
        print("\nStopping…")
    finally:
        stop.set()
        stream_q.put(None)
        transcriber.stop()
        transcriber.join(timeout=5.0)
        print("✅ Exited cleanly.")

if __name__ == "__main__":
    main()
