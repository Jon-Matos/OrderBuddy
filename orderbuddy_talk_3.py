# orderbuddy_talk_2.py — Voice ordering (fast-food), low-latency TTS, durable memory
# - Live mic via sounddevice.InputStream
# - Fast TTS: streams WAV bytes to ffplay (sub-second start), falls back to sounddevice if needed
# - Memory: uses memory_state.ConversationMemory for defaults & recent summary
# - NLU: fry->fries alias; generic "shake" asks flavor (remembers size); checkout phrases end session

import os, time, queue, argparse, threading, json, re, shutil, tempfile, subprocess, sys
from typing import Optional, List, Dict, Any
import sounddevice as sd            # 🎙 mic + playback
import soundfile as sf              # read WAVs for playback

# local
from realtime_stt import SAMPLE_RATE, FRAME_SIZE, CHANNELS, collect_utterances, Transcriber
from order_engine import Cart, LineItem
from menu import MENU, MODIFIERS
from memory_state import ConversationMemory

# ----------------- OpenAI/Trussed compatible client -----------------
API_BASE  = os.getenv("OB_API_BASE",  "https://api.openai.com/v1")
API_KEY   = os.getenv("OB_API_KEY",   os.getenv("OPENAI_API_KEY", ""))
MODEL     = os.getenv("OB_MODEL",     "gpt-4o-mini")
TTS_MODEL = os.getenv("OB_TTS_MODEL", "gpt-4o-mini-tts")
VOICE     = os.getenv("OB_VOICE",     "alloy")

try:
    from openai import OpenAI
    _client = OpenAI(base_url=API_BASE, api_key=API_KEY) if API_KEY else None
except Exception:
    _client = None

def _as_openai_tools(tools): return tools

def call_model(convo: list[dict], tools: list[dict]) -> dict:
    if _client is None:
        return {"role": "assistant", "content": "Okay."}
    resp = _client.chat.completions.create(
        model=MODEL, messages=convo, tools=_as_openai_tools(tools),
        tool_choice="auto", temperature=0.2,
    )
    msg = resp.choices[0].message
    if getattr(msg, "tool_calls", None):
        tc = msg.tool_calls[0]
        try:
            args = json.loads(tc.function.arguments or "{}")
        except Exception:
            args = {}
        return {"tool_name": tc.function.name, "args": args}
    return {"role": "assistant", "content": msg.content or "Okay."}

# ----------------- Low-latency TTS: stream -> ffplay, fallback to sounddevice -----------------
def _ffplay_available() -> bool:
    try:
        return bool(shutil.which("ffplay"))
    except Exception:
        return False

def speak(text: str, voice: str = VOICE, out_path: str = "reply.wav", output_device: int | None = None):
    """
    Ultra-low-latency TTS:
      - If ffplay exists: stream WAV bytes directly to ffplay stdin (starts speaking almost immediately).
      - Else: generate full WAV then play via sounddevice.
    """
    text = (text or "").strip()
    if not text or _client is None:
        return

    # --- Fast path: stream bytes to ffplay stdin ---
    if _ffplay_available():
        try:
            # Launch ffplay that reads raw WAV bytes from stdin ("-")
            p = subprocess.Popen(
                ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet", "-"],
                stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
            try:
                with _client.audio.speech.with_streaming_response.create(
                    model=TTS_MODEL, voice=voice, input=text
                ) as resp:
                    for chunk in resp.iter_bytes(chunk_size=4096):
                        if p.stdin:
                            p.stdin.write(chunk)
                if p.stdin:
                    p.stdin.close()
                p.wait(timeout=60)
                return
            except AttributeError:
                # SDK/gateway without streaming; close process and fall back
                if p.stdin:
                    p.stdin.close()
                p.terminate()
        except Exception as e:
            print(f"[TTS stream->ffplay fallback] {e}")

    # --- Fallback: non-streaming TTS, then play via sounddevice ---
    try:
        audio = _client.audio.speech.create(
            model=TTS_MODEL, voice=voice, input=text, format="wav"
        )
        data = audio.read()
        with open(out_path, "wb") as f:
            f.write(data)
        wav, sr = sf.read(out_path, dtype="float32")
        sd.play(wav, sr, device=output_device)
        sd.wait()
        print(f"[TTS] played (sounddevice) | {text}")
    except Exception as e:
        print(f"[TTS fallback error] {e} | text='{text[:60]}…'")

# ----------------- Menu helpers / NLU -----------------
SIZE_KEYS = {"sm", "md", "lg"}

def _menu_summary():
    return ", ".join(f"{c}: {', '.join(v.keys())}" for c, v in MENU.items())

def base_system_prompt():
    return (
        "You are OrderBuddy, a friendly voice ordering agent for a fast-food menu. "
        "Do NOT assume missing details. Ask minimally for any missing size if needed. "
        "You can ADD items, EDIT existing items (by uid, position like first/second/last, or by group), "
        "and REMOVE the last item. Prefer one consolidated follow-up when multiple items need size. "
        "Menu: " + _menu_summary() + ". Sizes: sm/md/lg (only for fries, shakes, soda)."
    )

def _needs_size(item_name: str) -> bool:
    item_name = (item_name or "").lower().strip()
    for _cat, items in MENU.items():
        if item_name in items and isinstance(items[item_name], dict):
            return bool(SIZE_KEYS & set(items[item_name].keys()))
    return False

def parse_size(text: str) -> Optional[str]:
    t = f" {text.lower()} "
    if " small " in t or " sm " in t: return "sm"
    if " medium " in t or " md " in t: return "md"
    if " large " in t or " lg " in t: return "lg"
    return None

def extract_modifiers(text: str) -> List[str]:
    t = text.lower(); found = []
    for word in ["onions", "pickles", "cheese", "ketchup", "mustard"]:
        if re.search(rf"\bno {word}\b", t):
            m = f"no {word}"
            if m in MODIFIERS: found.append(m)
        if re.search(rf"\b(extra|add) {word}\b", t):
            m = f"extra {word}"
            if m in MODIFIERS: found.append(m)
    # de-dup preserve order
    out = []
    for m in found:
        if m not in out: out.append(m)
    return out

def all_item_names() -> List[str]:
    names = []
    for _cat, items in MENU.items():
        names.extend(list(items.keys()))
    # longest first so multi-word items match before single-word
    return sorted(names, key=lambda s: (-len(s), s))

ITEM_NAMES = all_item_names()
ITEM_PATTERN = r"(?:%s)" % "|".join(re.escape(n) for n in ITEM_NAMES)

ACTION_WORDS = {
    "add": {"add","order","get","include","plus","i'll take","i would like","i want"},
    "edit": {"change","make","switch","swap","turn","set","update","modify"},
    "remove": {"remove","delete","drop","undo"},
    "readback": {"readback","total","summary","what's","whats","review"},
    "checkout": {
        "that's it","that is it","that'll be it","that will be it",
        "i'm done","im done","i am done","no that's all","no thats all",
        "nope that's all","nope thats all","that's all","thats all",
        "place the order","complete the order","checkout","check out"
    },
}
ORDINAL_MAP = {"first":"first","1st":"first","second":"second","2nd":"second","last":"last"}

def split_clauses(text: str) -> list[str]:
    parts = re.split(r"\s*(?:,?\s+and\s+|,\s*then\s*|;\s*|\.\s+)\s*", text, flags=re.I)
    return [p.strip() for p in parts if p.strip()]

def detect_action(clause: str) -> str:
    t = clause.lower()
    for act, words in ACTION_WORDS.items():
        if any(re.search(rf"\b{w}\b", t) for w in words):
            return act
    return "add"

def extract_selector(clause: str) -> dict:
    t = clause.lower()
    sel = {}
    for k, v in ORDINAL_MAP.items():
        if re.search(rf"\b{k}\b", t): sel["nth"] = v
    if re.search(r"\b(all|both)\b", t): sel["all"] = True
    m = re.search(rf"\b{ITEM_PATTERN}\b", t)
    if m: sel["item"] = m.group(0)
    m2 = re.search(r"\[?(it\d+)\]?", t)
    if m2: sel["uid"] = m2.group(1)
    return sel

def extract_ops(clause: str) -> dict:
    t = clause.lower()
    ops = {}
    sz = parse_size(t)
    if sz:
        ops["size_or_variant"] = sz
    mods = extract_modifiers(t)
    if mods:
        ops["add_mod"] = mods
    m2 = re.search(r"\b(?:qty|quantity|make it|make them)\s*(\d+)\b", t)
    if m2: ops["qty"] = int(m2.group(1))
    m3 = re.search(rf"\bto {ITEM_PATTERN}\b", t)
    if m3: ops["replace_item"] = m3.group(0).removeprefix("to ").strip()
    return ops

def nlu_to_commands(user_text: str) -> dict:
    cmds = {"adds": [], "edits": [], "remove_last": False, "readback": False, "place_order": False}
    for clause in split_clauses(user_text):
        act = detect_action(clause)
        if act == "remove" and re.search(r"\blast\b", clause, re.I):
            cmds["remove_last"] = True; continue
        if act == "readback":
            cmds["readback"] = True; continue
        if act == "checkout":
            cmds["place_order"] = True; continue
        if act == "edit":
            where = extract_selector(clause); ops = extract_ops(clause)
            if where and ops: cmds["edits"].append({"where": where, "ops": ops})
            continue

        # --- Add ---
        qty = 1
        mqty = re.search(rf"\b(\d+)\s+(?:x\s*)?({ITEM_PATTERN})\b", clause, re.I)
        mone = re.search(rf"\b({ITEM_PATTERN})\b", clause, re.I)
        item = None
        if mqty:
            qty = int(mqty.group(1)); item = mqty.group(2).lower()
        elif mone:
            item = mone.group(1).lower()
        else:
            # singular alias: "a large fry" -> "fries"
            if re.search(r"\bfry\b", clause, re.I):
                item = "fries"

        if item:
            size = parse_size(clause)
            mods = extract_modifiers(clause)
            cmds["adds"].append({"item": item, "qty": qty, "size_or_variant": size, "mods": mods})
    return cmds

# ----------------- Tools (schema kept for LLM fallback) -----------------
ADD_ITEMS_PARAMETERS = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "item": {"type": "string"},
                    "qty": {"type": "integer", "minimum": 1, "default": 1},
                    "size_or_variant": {"type": "string"},
                },
                "required": ["item"]
            }
        }
    },
    "required": ["items"]
}
EDIT_ITEMS_PARAMETERS = {
    "type":"object",
    "properties":{"edits":{"type":"array","items":{"type":"object","properties":{"where":{"type":"object"},"ops":{"type":"object"}},"required":["where","ops"]}}},
    "required":["edits"]
}
TOOLS = [
    {"type":"function","function":{"name":"add_items","description":"Add multiple items to the cart","parameters": ADD_ITEMS_PARAMETERS}},
    {"type":"function","function":{"name":"edit_items","description":"Edit items in the cart by uid/position/group","parameters": EDIT_ITEMS_PARAMETERS}},
    {"type":"function","function":{"name":"remove_last","description":"Remove the most recent item","parameters":{"type":"object","properties":{}}}},
    {"type":"function","function":{"name":"readback","description":"Read cart with subtotal/tax/total","parameters":{"type":"object","properties":{}}}},
    {"type":"function","function":{"name":"place_order","description":"Finalize order and report total","parameters":{"type":"object","properties":{}}}},
]

# ----------------- Pending queues -----------------
PENDING = {"active": False, "queue": []}  # each: {"it": {item dict}, "need_size": bool}

# Shake flavor pending (remembers size if user said "large shake" first)
FLAVOR_PENDING = {"active": False, "size": None}
FLAVOR_WORDS = ("vanilla", "chocolate", "strawberry")

def add_item_to_cart(cart: Cart, it: dict):
    li = LineItem(
        item=it["item"],
        qty=it.get("qty",1),
        size_or_variant=it.get("size_or_variant"),
        mods=it.get("mods") or [],
    )
    cart.add(li)

# ----------------- STT helpers -----------------
def _make_transcriber(args):
    try:
        return Transcriber(model=args.stt_model, device=args.device, language=args.lang)
    except TypeError:
        try:
            return Transcriber(args.stt_model)
        except TypeError:
            return Transcriber()

def _start_transcriber_if_needed(tr):
    try:
        if hasattr(tr, "start") and callable(tr.start):
            tr.start()
    except Exception as e:
        print(f"[STT start warn] {e}")

def _get_text_nowait(tr):
    for name in ("get_text_nowait", "get_text", "next_text", "read_text_nowait"):
        fn = getattr(tr, name, None)
        if callable(fn):
            try: return fn()
            except Exception: pass
    return None

# ----------------- Main -----------------
def main():
    ap = argparse.ArgumentParser(description="OrderBuddy (fast-food) — low-latency voice")
    ap.add_argument("--stt-model", dest="stt_model", default="small")
    ap.add_argument("--device", default="auto")
    ap.add_argument("--lang", default=None)
    ap.add_argument("--input-device", type=int, default=None)
    ap.add_argument("--output-device", type=int, default=None)
    ap.add_argument("--voice", default=VOICE)
    args = ap.parse_args()

    mem = ConversationMemory(user_id="local-user")
    cart = Cart()
    convo = [{"role": "system", "content": base_system_prompt() + " " + mem.system_suffix()}]

    stream_q: "queue.Queue[bytes]" = queue.Queue()
    stop = threading.Event()

    # 🎙️ Live mic -> queue
    def audio_callback(indata, frames, time_info, status):
        if status: print(f"[Audio status] {status}")
        stream_q.put(indata.copy().flatten())

    print("🎙️  Microphone on — speak, then pause briefly. Ctrl+C to quit.")
    with sd.InputStream(
        samplerate=SAMPLE_RATE, channels=CHANNELS, dtype="float32",
        blocksize=FRAME_SIZE, callback=audio_callback, device=args.input_device,
    ):
        transcriber = _make_transcriber(args)
        _start_transcriber_if_needed(transcriber)

        # segment mic audio and feed STT
        def collector_worker():
            for utter in collect_utterances(stream_q, stop):
                try: transcriber.submit(utter)
                except Exception as e: print(f"[STT submit warn] {e}")

        t_collector = threading.Thread(target=collector_worker, daemon=True)
        t_collector.start()

        try:
            speak("Hi! What can I get you today?", voice=args.voice, output_device=args.output_device)

            while not stop.is_set():
                text = _get_text_nowait(transcriber)
                if not text:
                    time.sleep(0.03)  # tighter polling
                    continue

                user_text = (text or "").strip()
                print(f"\n🗣️  You: {user_text}")

                # ---- If we're waiting for a shake flavor, resolve it first
                if FLAVOR_PENDING["active"]:
                    low = user_text.lower()
                    chosen = next((w for w in FLAVOR_WORDS if w in low), None)
                    if chosen:
                        item_name = f"{chosen} shake"
                        size = FLAVOR_PENDING.get("size")
                        add_item_to_cart(cart, {"item": item_name, "qty": 1, "size_or_variant": size})
                        FLAVOR_PENDING.update({"active": False, "size": None})
                        if cart.items: mem.learn_prefs_from_item(cart.items[-1])
                        spoken = cart.summary_speech()  # total + follow-up
                        print(f"🤖 OrderBuddy: {spoken}")
                        speak(spoken, voice=args.voice, output_device=args.output_device)
                        mem.add_summary_bullet("Added shake; " + spoken)
                        continue
                    else:
                        q = "What flavor of shake would you like: chocolate, vanilla, or strawberry?"
                        print(f"🤖 OrderBuddy: {q}")
                        speak(q, voice=args.voice, output_device=args.output_device)
                        continue

                # ---- Generic "shake" mention without flavor -> ask flavor once (remember size)
                if re.search(r"\bshake\b", user_text, re.I) and not re.search(r"(vanilla|chocolate|strawberry)\s+shake", user_text, re.I):
                    maybe_size = parse_size(user_text)
                    FLAVOR_PENDING.update({"active": True, "size": maybe_size})
                    q = "What flavor of shake would you like: chocolate, vanilla, or strawberry?"
                    print(f"🤖 OrderBuddy: {q}")
                    speak(q, voice=args.voice, output_device=args.output_device)
                    continue

                # ---- Local NLU
                parsed = nlu_to_commands(user_text)

                # Checkout (stop asking anything else)
                if parsed.get("place_order"):
                    final = f"Okay! Your total is ${cart.total():.2f}. Placing your order now."
                    print(f"🤖 OrderBuddy: {final}")
                    speak(final, voice=args.voice, output_device=args.output_device)
                    mem.add_summary_bullet("Order placed.")
                    break

                did_local = False

                # Edits
                if parsed["edits"]:
                    changed = cart.apply_edit(parsed["edits"])
                    if changed:
                        did_local = True

                # Remove last
                if parsed["remove_last"]:
                    _ = cart.remove_last()
                    did_local = True

                # Adds
                pending_adds = []
                for it in parsed["adds"]:
                    need_size = _needs_size(it["item"]) and (it.get("size_or_variant") is None)
                    if not need_size:
                        add_item_to_cart(cart, it)
                        if cart.items: mem.learn_prefs_from_item(cart.items[-1])
                        did_local = True
                    else:
                        pending_adds.append({"it": it, "need_size": True})

                if pending_adds:
                    asks = []
                    for pa in pending_adds:
                        asks.append(f"{pa['it']['item']}: size (small/medium/large)")
                    q = "Before I add those, could you confirm — " + "; ".join(asks) + "?"
                    print(f"🤖 OrderBuddy: {q}")
                    speak(q, voice=args.voice, output_device=args.output_device)
                    PENDING.update({"active": True, "queue": pending_adds})
                    continue

                # Pending size resolution (if any from earlier)
                if PENDING["active"]:
                    size = parse_size(user_text)
                    unresolved = []
                    for pa in PENDING["queue"]:
                        it = pa["it"]
                        if pa["need_size"] and not size:
                            unresolved.append(pa); continue
                        if size and pa["need_size"]:
                            it["size_or_variant"] = size
                        mods = extract_modifiers(user_text)
                        if mods:
                            it["mods"] = list({*(it.get("mods") or []), *mods})
                        add_item_to_cart(cart, it)
                        if cart.items: mem.learn_prefs_from_item(cart.items[-1])

                    if unresolved:
                        asks = []
                        for pa in unresolved:
                            nm = pa["it"]["item"]; bits=[]
                            if pa["need_size"] and not size: bits.append("size (small/medium/large)")
                            asks.append(f"{pa['it'].get('qty',1)} {nm}: " + " & ".join(bits))
                        q = "Thanks — just need: " + "; ".join(asks) + "."
                        print(f"🤖 OrderBuddy: {q}")
                        speak(q, voice=args.voice, output_device=args.output_device)
                        PENDING["queue"] = unresolved
                        continue
                    else:
                        PENDING.update({"active": False, "queue": []})
                        spoken = cart.summary_speech()
                        print(f"🤖 OrderBuddy: {spoken}")
                        speak(spoken, voice=args.voice, output_device=args.output_device)
                        mem.add_summary_bullet("Added items; " + spoken)
                        continue

                # Readback
                if parsed["readback"]:
                    rb = cart.summary_speech()
                    print(f"🤖 OrderBuddy: {rb}")
                    speak(rb, voice=args.voice, output_device=args.output_device)
                    mem.add_summary_bullet("Readback: " + rb)
                    continue

                if did_local:
                    spoken = cart.summary_speech()
                    print(f"🤖 OrderBuddy: {spoken}")
                    speak(spoken, voice=args.voice, output_device=args.output_device)
                    mem.add_summary_bullet(spoken)
                    # refresh system prompt with memory
                    continue

                # LLM fallback (tools)
                convo.append({"role": "user", "content": user_text})
                result = call_model(convo, TOOLS)
                if result.get("tool_name") == "add_items":
                    for it in result["args"]["items"]:
                        add_item_to_cart(cart, it)
                    spoken = cart.summary_speech()
                elif result.get("tool_name") == "edit_items":
                    changed = cart.apply_edit(result["args"]["edits"])
                    spoken = cart.summary_speech() if changed else "I couldn't find those items to edit."
                elif result.get("tool_name") == "remove_last":
                    cart.remove_last()
                    spoken = cart.summary_speech()
                elif result.get("tool_name") == "readback":
                    spoken = cart.summary_speech()
                elif result.get("tool_name") == "place_order":
                    spoken = "Order placed. " + cart.summary_speech()
                else:
                    spoken = result.get("content", "Okay.")

                print(f"🤖 OrderBuddy: {spoken}")
                speak(spoken, voice=args.voice, output_device=args.output_device)
                mem.add_summary_bullet(spoken)

        except KeyboardInterrupt:
            pass
        finally:
            stop.set()
            stream_q.put(None)
            try: transcriber.stop()
            except Exception: pass
            t_collector.join(timeout=2.0)
            print("✅ Exited cleanly.")

if __name__ == "__main__":
    # Small timer improvement on Windows
    if sys.platform.startswith("win"):
        try:
            import ctypes
            ctypes.windll.winmm.timeBeginPeriod(1)
        except Exception:
            pass
    main()
