# orderbuddy_talk_2.py — Voice ordering (fast-food), low-latency TTS, durable memory
# - Live mic via sounddevice.InputStream
# - Fast TTS: streaming to file + immediate playback with ffplay when available
# - Memory: uses memory_state.ConversationMemory for defaults & recent summary

import os, time, queue, argparse, uuid, threading, json, re, shutil, tempfile, subprocess, sys
from typing import Optional, List, Dict, Any
import sounddevice as sd  # 🎙 mic
import soundfile as sf          # audio file read
import random
from datetime import datetime

# === Avatar bridge toggles ===
AVATAR_ENABLED = os.getenv("AVATAR_ENABLED", "1") == "1"
AVATAR_BASE = os.getenv("AVATAR_BASE", "http://localhost:3000")

# local
from realtime_stt import SAMPLE_RATE, FRAME_SIZE, CHANNELS, collect_utterances, Transcriber
from order_engine import Cart, LineItem
from menu import MENU, MODIFIERS
from memory_state import ConversationMemory  # durable memory (prefs + summary)

# ----------------- OpenAI/Trussed compatible client -----------------
API_BASE  = os.getenv("OB_API_BASE",  "https://fauengtrussed.fau.edu/provider/generic")
API_KEY   = os.getenv("OB_API_KEY",   os.getenv("OPENAI_API_KEY", ""))
MODEL     = os.getenv("OB_MODEL",     "gpt-4o-mini")
TTS_MODEL = os.getenv("OB_TTS_MODEL", "gpt-4o-mini-tts")
VOICE     = os.getenv("OB_VOICE",     "alloy")
IGNORE_CHITCHAT = os.getenv("OB_IGNORE_CHITCHAT", "1") == "1"
INTENT_DEBUG    = os.getenv("OB_INTENT_DEBUG", "0") == "1"

try:
    from openai import OpenAI
    _client = OpenAI(base_url=API_BASE, api_key=API_KEY) if API_KEY else None
except Exception:
    _client = None

try:
    from dynamo_db_rough import (
        log_voice_event as ddb_log_voice_event,
        log_order_event as ddb_log_order_event,
    )
except Exception:
    ddb_log_voice_event = None
    ddb_log_order_event = None

SESSION_ID: Optional[str] = None

def _log_voice(event_type: str, **kwargs):
    if not SESSION_ID or ddb_log_voice_event is None:
        return
    try:
        ddb_log_voice_event(SESSION_ID, event_type, **kwargs)
    except Exception as e:
        if INTENT_DEBUG:
            print(f"[Log warn voice] {e}")

def _log_order(event_type: str, **kwargs):
    if not SESSION_ID or ddb_log_order_event is None:
        return
    try:
        ddb_log_order_event(SESSION_ID, event_type, **kwargs)
    except Exception as e:
        if INTENT_DEBUG:
            print(f"[Log warn order] {e}")

# Thread-safe writer so multiple runs won't collide
_ORDERS_LOCK = threading.Lock()
ORDERS_PATH = os.getenv("ORDERS_PATH", "orders.jsonl")  # change path via env if you want

def _safe_get(obj, *names, default=None):
    for n in names:
        if hasattr(obj, n):
            return getattr(obj, n)
        if isinstance(obj, dict) and n in obj:
            return obj[n]
    return default

def _cart_items_to_list(cart):
    items = _safe_get(cart, "items", "line_items", default=[]) or []
    out = []
    for li in items:
        out.append({
            "qty":  _safe_get(li, "qty", "quantity", default=1),
            "item": _safe_get(li, "name", "item", "title", default="item"),
            "size": _safe_get(li, "size_or_variant", "size", "variant", default=None),
            "mods": _safe_get(li, "modifiers", "mods", default=None),
            "price_each": _safe_get(li, "price", "unit_price", default=None),
            "uid":  _safe_get(li, "uid", "id", default=None),
        })
    return out

def _cart_total(cart):
    # Try common total fields/methods
    for cand in ("total", "grand_total", "total_due", "compute_total", "totals"):
        if hasattr(cart, cand):
            val = getattr(cart, cand)
            try:
                v = val() if callable(val) else val
                if isinstance(v, (int, float)):
                    return float(v)
                if isinstance(v, dict) and "total" in v and isinstance(v["total"], (int, float)):
                    return float(v["total"])
            except Exception:
                pass
    return None

def place_order_to_jsonl(cart, meta=None):
    """
    Serialize the current cart and append to orders.jsonl (or ORDERS_PATH).
    Returns the generated order_id.
    """
    order_id = str(uuid.uuid4())
    doc = {
        "order_id": order_id,
        "created_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "items": _cart_items_to_list(cart),
        "total": _cart_total(cart),
        "meta": meta or {},  # you can pass user/session info here if you have it
    }
    line = json.dumps(doc, ensure_ascii=False)
    with _ORDERS_LOCK:
        with open(ORDERS_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    return order_id

def casual(qs: list[str]) -> str:
    """Pick a natural-sounding variant."""
    return random.choice(qs)


# ===== Size reply handling (so "medium" after a size question works) =====
_AWAITING_SIZES_FOR = []  # list[str] of item names we just asked size for

_SIZE_MAP = {
    "small": "sm", "sm": "sm", "little": "sm",
    "medium": "md", "med": "md", "md": "md", "regular": "md", "reg": "md", "mid": "md",
    "large": "lg", "lg": "lg", "big": "lg", "biggie": "lg",
}

def _extract_size_code(text: str):
    t = (text or "").lower()
    # simple exact tokens
    for w, code in _SIZE_MAP.items():
        if re.search(rf"\b{re.escape(w)}\b", t):
            return code
    # digits like "size 2" not supported here; extend if needed
    return None

def _try_apply_size_response(cart, text, speak_fn):
    code = _extract_size_code(text)
    if not code:
        return False

    # prefer items we just asked about
    target = None
    try:
        from order_engine import options_for
    except Exception:
        options_for = None

    if _AWAITING_SIZES_FOR:
        # pick most recent matching item w/o size
        for li in reversed(cart.items):
            name = getattr(li, "item", "")
            if name in _AWAITING_SIZES_FOR and not getattr(li, "size_or_variant", None):
                target = name
                break

    if not target:
        # fallback: most recent item needing a size
        try:
            target = cart.set_size_on_last_unset(code)
        except Exception:
            target = None
    else:
        # apply directly to target
        # find that li and set
        for li in reversed(cart.items):
            if getattr(li, "item", "") == target and not getattr(li, "size_or_variant", None):
                li.size_or_variant = code
                try:
                    from order_engine import pick_price
                    li.price = pick_price(li.item, code)
                except Exception:
                    pass
                break

    if target:
        try:
            publish_panel_state(cart)
        except Exception:
            pass
        try:
            speak_fn(f"Got it—{_verbal_size(code)} {target}.")
        except Exception:
            pass
        # clear awaiting since we handled it
        _AWAITING_SIZES_FOR.clear()
        return True

    return False

def _verbal_size(code: str) -> str:
    return {"sm":"small","md":"medium","lg":"large"}.get(code, code)
# ===== /Size reply handling =====
def ask_size_for(item_name: str) -> str:
    try:
        _AWAITING_SIZES_FOR.clear(); _AWAITING_SIZES_FOR.append(item_name)
    except Exception:
        pass
    return casual([
        f"What size {item_name} would you like?",
        f"Which size for the {item_name}?",
        f"What size do you want for the {item_name}?",
        f"What size should I make those {item_name}?",
    ])

def ask_multi_sizes(pending_items: list[str]) -> str:
    # e.g., ["fries", "shake"]
    try:
        _AWAITING_SIZES_FOR.clear(); _AWAITING_SIZES_FOR.extend([str(x) for x in (pending_items or [])])
    except Exception:
        pass
    if len(pending_items) == 1:
        it = pending_items[0]
        return casual([
            f"Before I add that, what size {it} would you like?",
            f"Got it—what size do you want for the {it}?",
            f"Sure thing. Which size for the {it}?",
        ])
    # multiple items
    nice = ", ".join(pending_items[:-1]) + f", and {pending_items[-1]}" if len(pending_items) > 1 else pending_items[0]
    return casual([
        f"Before I add those, what sizes would you like for the {nice}?",
        f"Quick check—what sizes should I make the {nice}?",
        f"Sounds good. Which sizes for the {nice}?",
    ])

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

# ----------------- Avatar-first speech, with local TTS fallback -----------------
def _ffplay_available() -> bool:
    try:
        return bool(shutil.which("ffplay"))
    except Exception:
        return False

def speak(text: str, voice: str = VOICE, out_path: str = "reply.wav", output_device: int | None = None):
    """
    Avatar-first speech:
      1) Try HeyGen Node bridge (/session/start then /speak)
      2) On failure or disabled: local TTS with winsound/sounddevice/ffplay (your original paths)
    """
    text = (text or "").strip()
    if not text:
        return
    _log_voice("AssistantSpeech", llm_response=text)

    # 1) HeyGen bridge
    if AVATAR_ENABLED:
        try:
            requests.post(f"{AVATAR_BASE}/session/start", timeout=3)
            r = requests.post(f"{AVATAR_BASE}/speak", json={"text": text}, timeout=10)
            if r.ok:
                return  # avatar will speak; skip local audio
            else:
                print(f"[Avatar warn] {r.status_code}: {r.text}")
        except Exception as e:
            print(f"[Avatar bridge unavailable → local TTS] {e}")

    # 2) Local TTS (your original behavior)
    data = None
    if _client is None:
        print(f"[TTS stub] {text}")
    else:
        try:
            audio = _client.audio.speech.create(
                model=TTS_MODEL,
                voice=voice,
                input=text,
                format="wav",
            )
            data = audio.read()
        except TypeError:
            audio = _client.audio.speech.create(
                model=TTS_MODEL,
                voice=voice,
                input=text,
                response_format="wav",
            )
            data = audio.read()
        except Exception as e:
            print(f"[TTS warn] {e} | {text}")

    if data:
        try:
            with open(out_path, "wb") as f:
                f.write(data)
        except Exception as e:
            print(f"[TTS file write error] {e}")

    try:
        import sys
        if sys.platform.startswith("win"):
            import winsound
            winsound.PlaySound(out_path, winsound.SND_FILENAME)
            print(f"[TTS] played (winsound) | {text}")
            return
    except Exception as e:
        print(f"[TTS winsound fail] {e}")

    try:
        # Try sounddevice (the path you had working before)
        data2, samplerate = sf.read(out_path, dtype="float32")
        sd.play(data2, samplerate, device=output_device)
        sd.wait()
        print(f"[TTS] played (sounddevice) | {text}")
        return
    except Exception:
        pass

    try:
        import subprocess, shutil
        if shutil.which("afplay"):
            subprocess.run(["afplay", out_path])
            print(f"[TTS] played (afplay) | {text}")
            return
        if shutil.which("aplay"):
            subprocess.run(["aplay", out_path])
            print(f"[TTS] played (aplay) | {text}")
            return
        if shutil.which("ffplay"):
            subprocess.run(["ffplay", "-nodisp", "-autoexit", out_path])
            print(f"[TTS] played (ffplay) | {text}")
            return
    except Exception:
        pass

    print(f"[TTS] saved {out_path} | {text}")

# ----------------- Tiny durable memory wrapper -----------------
# (You are already importing ConversationMemory from memory_state.py)

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
    """
    Map many ways of saying size -> 'sm' | 'md' | 'lg'
    Accepts: small/sm/s/kid/kids/little, medium/med/md/regular/reg/m,
             large/lg/l/big/xl/extra large
    """
    t = (text or "").lower()

    # small
    if re.search(r"\b(small|sm|s|kid|kids|little)\b", t):
        return "sm"

    # medium
    if re.search(r"\b(medium|med|md|regular|reg|m)\b", t):
        return "md"

    # large
    if re.search(r"\b(large|lg|l|big|xl|extra\s+large)\b", t):
        return "lg"

    return None


def extract_modifiers(text: str) -> List[str]:
    t = (text or "").lower()
    out: List[str] = []
    # canonical forms for supported modifiers
    canon = {
        "onion": "onions", "onions": "onions",
        "pickle": "pickles", "pickles": "pickles",
        "cheese": "cheese", "ketchup": "ketchup", "mustard": "mustard",
    }
    keys = set(canon.keys())

    def add_many(words, prefix):
        for w in words:
            w2 = (w or "").strip()
            if not w2: continue
            # keep letters only (strip punctuation)
            import re as _re
            w2 = _re.sub(r"[^a-z]", "", w2)
            if w2 in keys:
                mod = f"{prefix} {canon[w2]}"
                if mod in MODIFIERS and mod not in out:
                    out.append(mod)

    # 1) List phrases: "no pickles and onions", "without onions, ketchup"
    for m in re.finditer(r"\b(?:no|without|hold(?:\s+the)?)\s+([a-z ,]+?)(?=$|[.!?]|,?\s*(?:please|thanks)\b)", t):
        list_part = m.group(1)
        words = re.split(r"\s*(?:,|and|or)\s*", list_part)
        add_many(words, "no")

    # 2) List phrases: "extra pickles and onions", "add cheese, ketchup"
    for m in re.finditer(r"\b(?:extra|add)\s+([a-z ,]+?)(?=$|[.!?]|,?\s*(?:please|thanks)\b)", t):
        list_part = m.group(1)
        words = re.split(r"\s*(?:,|and|or)\s*", list_part)
        add_many(words, "extra")

    # 3) Per-word fallbacks
    for w in list(keys):
        if re.search(rf"\bno\s+{re.escape(w)}\b", t):
            add_many([w], "no")
        if re.search(rf"\b(?:extra|add)\s+{re.escape(w)}\b", t):
            add_many([w], "extra")

    return out
def normalize_food_aliases(text: str) -> str:
    try:
        t = text
        # shakes
        t = re.sub(r"\b(milk\s*shake|milkshake)s?\b", "shake", t, flags=re.I)
        # burgers
        t = re.sub(r"\bcheese\s+burger\b", "cheeseburger", t, flags=re.I)
        t = re.sub(r"\bdouble\s+cheese\s*burger\b", "double cheeseburger", t, flags=re.I)
        t = re.sub(r"\bbig[-\s]*mac\b", "big mac", t, flags=re.I)
        t = re.sub(r"\bquarter[-\s]*pounder\b", "quarter pounder", t, flags=re.I)
        # fries
        t = re.sub(r"\bfry\b", "fries", t, flags=re.I)
        # soda
        t = re.sub(r"\b(pop|cola|coke)\b", "soda", t, flags=re.I)
        return t
    except Exception:
        return text
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
    "readback": {"readback","total","summary","review"},
    "checkout": {
        "that'll be all", "that's it","that is it","that'll be it","that will be it",
        "i'm done","im done","i am done","no that's all","no thats all",
        "nope that's all","nope thats all","that's all","thats all",
        "place the order","complete the order","checkout","check out"
    },
}
ORDINAL_MAP = {"first":"first","1st":"first","second":"second","2nd":"second","last":"last"}
WORD_NUMBER_MAP = {
    "a": 1, "an": 1, "one": 1,
    "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10,
    "eleven": 11, "twelve": 12, "dozen": 12,
    "couple": 2, "pair": 2,
}
_WORD_NUMBER_PATTERN = "|".join(re.escape(w) for w in sorted(WORD_NUMBER_MAP.keys(), key=lambda s: (-len(s), s)))

def split_clauses(text: str) -> list[str]:
    parts = re.split(r"\s*(?:,?\s+and\s+|,\s*then\s*|;\s*|\.\s+)\s*", text, flags=re.I)
    return [p.strip() for p in parts if p.strip()]

def detect_action(clause: str) -> str:
    t = clause.lower()
    for act, words in ACTION_WORDS.items():
        if any(re.search(rf"\b{w}\b", t) for w in words):
            return act
    return "add"

def _plural_item_match(clause: str) -> Optional[str]:
    t = clause.lower()
    for nm in ITEM_NAMES:
        if nm.endswith("s"):
            continue
        plural = nm + "s"
        if re.search(r"\b" + re.escape(plural) + r"\b", t):
            return nm
    if re.search(r"\bfry\b", t):
        return "fries"
    return None

def _quantity_from_clause(clause: str, item_token: str) -> int:
    text = (clause or "").lower()
    token = (item_token or "").lower()
    idx = text.find(token) if token else -1
    prefix = text if idx == -1 else text[:idx]
    if prefix:
        digit_matches = list(re.finditer(r"\b(\d+)\b", prefix))
        if digit_matches:
            return int(digit_matches[-1].group(1))
        compact = list(re.finditer(r"(\d+)\s*x\b", prefix))
        if compact:
            return int(compact[-1].group(1))
        word_matches = list(re.finditer(rf"\b({_WORD_NUMBER_PATTERN})\b", prefix))
        if word_matches:
            return WORD_NUMBER_MAP.get(word_matches[-1].group(1), 1)
    return 1

def extract_selector(clause: str) -> dict:
    t = clause.lower()
    sel = {}
    for k, v in ORDINAL_MAP.items():
        if re.search(rf"\b{k}\b", t): sel["nth"] = v
    if re.search(r"\b(all|both)\b", t): sel["all"] = True

    # existing exact-name match
    m = re.search(rf"\b{ITEM_PATTERN}\b", t)
    if m: 
        sel["item"] = m.group(0)


    # plural fallback: match pluralized last token (e.g., "quarter pounders")
    if "item" not in sel:
        for nm in ITEM_NAMES:
            if re.search(r"\b" + re.escape(nm) + r"s\b", t):
                sel["item"] = nm
                break
    # NEW: fallbacks for singulars/aliases (e.g., "fry" → "fries")
    if "item" not in sel:
        if re.search(r"\bfry\b", t):
            sel["item"] = "fries"

    # keep existing UID and size logic
    m2 = re.search(r"\[?(it\d+)\]?", t)
    if m2: sel["uid"] = m2.group(1)

    # already present in your file from earlier patch:
    sz = parse_size(t)
    if sz: sel["size_or_variant"] = sz
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
    text_norm = normalize_food_aliases(user_text)
    cmds = {"adds": [], "edits": [], "remove_last": False, "readback": False, "place_order": False, "removes": []}
    for clause in split_clauses(text_norm):
        act = detect_action(clause)
        if act == "remove" and re.search(r"\blast\b", clause, re.I):
            cmds["remove_last"] = True; continue
        if act == "remove":
            where = extract_selector(clause)
            # include modifiers in selector for targeted removals
            mods_rm = extract_modifiers(clause)
            if mods_rm:
                where["mods"] = mods_rm
            mqty = re.search(r"\b(?:remove|delete|drop|take off|take out|cancel)\s*(\d+)\b", clause, re.I)
            qty = int(mqty.group(1)) if mqty else None
            if qty is None:
                # word numbers: a/an/one/two/three...ten
                mword = re.search(rf"\b(?:remove|delete|drop|take off|take out|cancel)\s+({_WORD_NUMBER_PATTERN})\b", clause, re.I)
                if mword:
                    word = mword.group(1).lower()
                    qty = WORD_NUMBER_MAP.get(word)
            if where:
                cmds["removes"].append({"where": where, "qty": qty})
            continue
        if act == "readback":
            cmds["readback"] = True; continue
        if act == "checkout":
            cmds["place_order"] = True; continue
        if act == "edit":
            where = extract_selector(clause); ops = extract_ops(clause)
            if where and ops: cmds["edits"].append({"where": where, "ops": ops})
            continue

        # --- Add ---
        selector = extract_selector(clause)
        item = selector.get("item")
        if item:
            item = item.lower()
        else:
            item = _plural_item_match(clause)

        if item:
            qty = _quantity_from_clause(clause, item)
            size = parse_size(clause)
            mods = extract_modifiers(clause)
            cmds["adds"].append({"item": item, "qty": qty, "size_or_variant": size, "mods": mods})
    return cmds
# Heuristic filter to decide if an utterance is order-related.
def is_order_related(text: str) -> bool:
    t = (text or "").lower().strip()
    if not t:
        return False
    parsed = nlu_to_commands(t)
    if (
        parsed.get("adds") or parsed.get("edits") or parsed.get("removes") or
        parsed.get("remove_last") or parsed.get("readback") or parsed.get("place_order")
    ):
        return True
    if re.search(rf"\b{ITEM_PATTERN}\b", t):
        return True
    if parse_size(t):
        return True
    if extract_modifiers(t):
        return True
    if re.search(r"\b(order|get|add|include|i('m| am) done|that's it|thats it|checkout|check out|total|summary)\b", t):
        return True
    return False# ----------------- Tools (schema kept for LLM fallback) -----------------
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
    try:
        total = cart.total()
    except Exception:
        total = None
    _log_order("ItemAdded", item_name=li.item, quantity=li.qty, price=getattr(li, "price", None), total=total)

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

# ----------------- Main -----------------# === Current Order panel: tiny JSON server (http://localhost:5055/state) ===
import json as _json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading as _threading
ORDER_PANEL_PORT = int(os.getenv("ORDER_PANEL_PORT", "5055"))
_CURRENT_STATE = {"items": [], "subtotal": 0.0, "tax": 0.0, "total": 0.0, "last_updated": None}

def _safe_get(obj, *names, default=None):
    for n in names:
        if hasattr(obj, n): return getattr(obj, n)
        if isinstance(obj, dict) and n in obj: return obj[n]
    return default

def _cart_items_to_list_panel(cart):
    try:
        return _cart_items_to_list(cart)  # project's helper
    except Exception:
        items = _safe_get(cart, "items", default=[]) or []
        out = []
        for li in items:
            out.append({
                "qty":  _safe_get(li, "qty", "quantity", default=1),
                "item": _safe_get(li, "name", "item", "title", default="item"),
                "size": _safe_get(li, "size_or_variant", "size", "variant", default=None),
                "mods": _safe_get(li, "modifiers", "mods", default=None),
                "price_each": _safe_get(li, "price", "unit_price", default=None),
                "uid":  _safe_get(li, "uid", "id", default=None),
            })
        return out

def _total_from_cart_panel(cart):
    try:
        t = _cart_total(cart)
        if t is not None: return float(t)
    except Exception:
        pass
    try:
        # fallback: compute from items
        it = _cart_items_to_list_panel(cart)
        return sum((i.get("price_each") or 0.0) * (i.get("qty") or 1) for i in it)
    except Exception:
        return 0.0

def _dict_from_cart__panel(cart):
    items = _cart_items_to_list_panel(cart)
    total = _total_from_cart_panel(cart)
    # Prefer cart's explicit methods if present
    try: subtotal = float(cart.subtotal()) if hasattr(cart, "subtotal") else None
    except Exception: subtotal = None
    try: tax = float(cart.tax()) if hasattr(cart, "tax") else None
    except Exception: tax = None

    if subtotal is None:
        subtotal = sum((i.get("price_each") or 0.0) * (i.get("qty") or 1) for i in items) if items else 0.0
    if tax is None:
        try:
            from menu import TAX_RATE
            tax = round(subtotal * TAX_RATE, 2)
        except Exception:
            tax = 0.0

    if not total:
        total = round(subtotal + tax, 2)

    return {
        "items": items,
        "subtotal": round(subtotal, 2),
        "tax": round(tax, 2),
        "total": round(total, 2),
        "last_updated": datetime.utcnow().isoformat(timespec="seconds") + "Z",
    }

def publish_panel_state(cart):
    try:
        _CURRENT_STATE.update(_dict_from_cart__panel(cart))
    except Exception as e:
        print(f"[Panel publish warn] {e}")

class _PanelHandler(BaseHTTPRequestHandler):
    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")

    def do_OPTIONS(self):
        self.send_response(204); self._cors()
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        try:
            if self.path.startswith("/state"):
                body = _json.dumps(_CURRENT_STATE).encode("utf-8")
                self.send_response(200); self._cors()
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body); return
            if self.path.startswith("/health"):
                self.send_response(200); self._cors(); self.end_headers(); return
            self.send_response(404); self._cors(); self.end_headers()
        except BrokenPipeError:
            pass
        except Exception as e:
            try:
                self.send_response(500); self._cors(); self.end_headers()
            except Exception:
                pass
            print(f"[Panel handler error] {e}")

def start_panel_server():
    port = ORDER_PANEL_PORT
    try:
        srv = ThreadingHTTPServer(("0.0.0.0", port), _PanelHandler)
    except OSError:
        port += 1
        srv = ThreadingHTTPServer(("0.0.0.0", port), _PanelHandler)
    thread = _threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    print(f"[Panel] Current Order server on http://localhost:{port}/state")


def main():
    ap = argparse.ArgumentParser(description="OrderBuddy (fast-food) — low-latency voice, avatar-first")
    ap.add_argument("--stt-model", dest="stt_model", default="small")
    ap.add_argument("--device", default="auto")
    ap.add_argument("--lang", default=None)
    ap.add_argument("--input-device", type=int, default=None)
    ap.add_argument("--output-device", type=int, default=None)
    ap.add_argument("--voice", default=VOICE)
    args = ap.parse_args()

    mem = ConversationMemory(user_id="local-user")
    cart = Cart()
    global SESSION_ID
    SESSION_ID = os.getenv("OB_SESSION_ID") or f"session-{uuid.uuid4()}"
    _log_voice("SessionStarted", llm_response="OrderBuddy session started.")
    try:
        _log_order("CartInitialized", total=cart.total())
    except Exception:
        pass
    start_panel_server()
    publish_panel_state(cart)
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
                try:
                    transcriber.submit(utter)
                except Exception as e:
                    print(f"[STT submit warn] {e}")
                    _log_voice("STT_SubmitError", error=str(e))

        t_collector = threading.Thread(target=collector_worker, daemon=True)
        t_collector.start()

        try:
            speak("Hi! Welcome to Owlsley's Restaurant. What can I get you today?", voice=args.voice, output_device=args.output_device)

            while not stop.is_set():
                text = _get_text_nowait(transcriber)
                if not text:
                    time.sleep(0.03)
                    continue

                user_text = (text or "").strip()
                _log_voice("STT_Transcribed", transcript=user_text)
                print(f"\n🗣️  You: {user_text}")
                # Size-only replies like "medium" → apply to last item that needs size
                if _try_apply_size_response(cart, user_text, lambda m: speak(m, voice=args.voice, output_device=args.output_device)):
                    continue
    

                # ---- If we're waiting for a shake flavor, resolve it first
                if FLAVOR_PENDING["active"]:
                    low = user_text.lower()
                    chosen = next((w for w in FLAVOR_WORDS if w in low), None)
                    if chosen:
                        item_name = f"{chosen} shake"
                        size = FLAVOR_PENDING.get("size")
                        add_item_to_cart(cart, {"item": item_name, "qty": 1, "size_or_variant": size})
                        publish_panel_state(cart)
                        FLAVOR_PENDING.update({"active": False, "size": None})
                        if cart.items: mem.learn_prefs_from_item(cart.items[-1])
                        spoken = cart.summary_speech()
                        print(f"🤖 OrderBuddy: {spoken}")
                        speak(spoken, voice=args.voice, output_device=args.output_device)
                        mem.add_summary_bullet("Added shake; " + spoken)
                        continue
                    else:
                        q = "What flavor of shake would you like: chocolate, vanilla, or strawberry?"
                        print(f"🤖 OrderBuddy: {q}")
                        speak(q, voice=args.voice, output_device=args.output_device)
                        continue

                # ---- Generic "shake" without flavor -> ask once (remember size)
                if re.search(r"\b(milk\s*shake|milkshake|shake)s?\b", user_text, re.I) and not re.search(r"(vanilla|chocolate|strawberry)\s+(?:milk\s*shake|milkshake|shake)s?\b", user_text, re.I):
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
                    # Persist the order to JSONL
                    order_id = place_order_to_jsonl(cart, meta={"channel": "drive_thru", "assistant": "orderbuddy"})
                    try:
                        _log_order("OrderPlaced", total=cart.total(), item_name=f"order:{order_id}")
                    except Exception:
                        pass

                    # Log the ID silently, but don't say it to the guest
                    print(f"🤖 OrderBuddy: order placed. id={order_id}")

                    # Speak a short confirmation only
                    msg = "All set — I’ve placed your order."
                    print(f"🤖 OrderBuddy (to guest): {msg}")
                    speak(msg, voice=args.voice, output_device=args.output_device)

                    # Start a fresh order without exiting
                    try:
                        _AWAITING_SIZES_FOR.clear()
                    except Exception:
                        pass
                    try:
                        PENDING.update({"active": False, "queue": []})
                        FLAVOR_PENDING.update({"active": False, "size": None})
                    except Exception:
                        pass

                    cart = Cart()
                    publish_panel_state(cart)
                    try:
                        _log_order("CartInitialized", total=cart.total())
                    except Exception:
                        pass

                    # Friendly new-order greeting
                    greet = "Hi! Welcome to Owlsley's Restaurant. What can I get you today?"
                    print(f"🤖 OrderBuddy: {greet}")
                    speak(greet, voice=args.voice, output_device=args.output_device)

                    mem.add_summary_bullet(f"Order placed (ID {order_id[:8].upper()}). New order started.")
                    continue


                did_local = False

                # Edits
                if parsed["edits"]:
                    changed = cart.apply_edit(parsed["edits"])
                    if changed:
                        publish_panel_state(cart)
                        did_local = True

                # Remove last
                if parsed["remove_last"]:
                    removed_item = cart.items[-1] if cart.items else None
                    removed = cart.remove_last()
                    publish_panel_state(cart)
                    if removed and removed_item:
                        try:
                            _log_order("ItemRemoved", item_name=removed_item.item, quantity=removed_item.qty, total=cart.total())
                        except Exception:
                            pass
                    did_local = True

                # Targeted removals
                if parsed.get("removes"):
                    for rm in parsed["removes"]:
                        where = rm.get("where", {}) or {}
                        qty = rm.get("qty")
                        try:
                            targets = list(cart.select(where))
                        except Exception:
                            targets = []
                        try:
                            affected = cart.remove(where, qty)
                            if affected:
                                label = where.get("item")
                                if not label and targets:
                                    label = getattr(targets[0], "item", None)
                                _log_order("ItemRemoved", item_name=label, quantity=qty, total=cart.total())
                        except Exception:
                            pass
                    publish_panel_state(cart)
                    did_local = True

                # Adds
                pending_adds = []
                for it in parsed["adds"]:
                    need_size = _needs_size(it["item"]) and (it.get("size_or_variant") is None)
                    if not need_size:
                        add_item_to_cart(cart, it)
                        publish_panel_state(cart)
                        if cart.items: mem.learn_prefs_from_item(cart.items[-1])
                        did_local = True
                    else:
                        pending_adds.append({"it": it, "need_size": True})

                if pending_adds:
                    # collect the item labels needing a size (e.g., "fries", "coffee")
                    need_labels = [pa['it']['item'] for pa in pending_adds]
                    q = ask_multi_sizes(need_labels)
                    print(f"🤖 OrderBuddy: {q}")
                    speak(q, voice=args.voice, output_device=args.output_device)
                    PENDING.update({"active": True, "queue": pending_adds})
                    continue

                # Pending size resolution
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
                        publish_panel_state(cart)
                        if cart.items: mem.learn_prefs_from_item(cart.items[-1])

                    if unresolved:
                        asks = []
                        for pa in unresolved:
                            nm = pa["it"]["item"]; bits=[]
                            if pa["need_size"] and not size: bits.append("size (small/medium/large)")
                            asks.append(f"{pa['it'].get('qty',1)} {nm}: " + " & ".join(bits))
                        need_labels = [pa['it']['item'] for pa in unresolved]
                        if len(need_labels) == 1:
                            q = ask_size_for(need_labels[0])
                        else:
                            q = ask_multi_sizes(need_labels)
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
                    continue
                # LLM fallback (tools)
                if IGNORE_CHITCHAT and not is_order_related(user_text):
                    if INTENT_DEBUG: print("[Intent] suppressed non-order utterance")
                    continue
                convo.append({"role": "user", "content": user_text})
                result = call_model(convo, TOOLS)
                if result.get("tool_name") == "add_items":
                    for it in result["args"]["items"]:
                        add_item_to_cart(cart, it)
                        publish_panel_state(cart)
                    spoken = cart.summary_speech()
                elif result.get("tool_name") == "edit_items":
                    changed = cart.apply_edit(result["args"]["edits"])
                    spoken = cart.summary_speech() if changed else "I couldn't find those items to edit."
                elif result.get("tool_name") == "remove_last":
                    cart.remove_last()
                    publish_panel_state(cart)
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

# Intent filter (stricter) with debug and env toggle
def is_order_related(text: str) -> bool:
    t = (text or "").lower().strip()
    if not t:
        try:
            if INTENT_DEBUG: print("[Intent] ignore: empty")
        except Exception:
            pass
        return False
    # explicit non-order disclaimers
    if re.search(r"\b(?:not|don't|do not|isn't|is not|no)\b.*\border\b", t) or re.search(r"\bnot (?:part|for|about) (?:of )?my order\b", t) or re.search(r"\bnot ordering\b", t) or re.search(r"\bignore this\b", t):
        try:
            if INTENT_DEBUG: print("[Intent] ignore: explicit non-order disclaimer")
        except Exception:
            pass
        return False
    parsed = nlu_to_commands(t)
    if (parsed.get("adds") or parsed.get("edits") or parsed.get("removes") or
        parsed.get("remove_last") or parsed.get("readback") or parsed.get("place_order")):
        try:
            if INTENT_DEBUG: print("[Intent] allow: local NLU command")
        except Exception:
            pass
        return True
    try:
        import re
        from re import search as _s
    except Exception:
        pass
    if re.search(rf"\\b{ITEM_PATTERN}\\b", t):
        try:
            if INTENT_DEBUG: print("[Intent] allow: item mention")
        except Exception:
            pass
        return True
    if parse_size(t):
        try:
            if INTENT_DEBUG: print("[Intent] allow: size word")
        except Exception:
            pass
        return True
    if extract_modifiers(t):
        try:
            if INTENT_DEBUG: print("[Intent] allow: modifier")
        except Exception:
            pass
        return True
    if re.search(r"\b(checkout|check out|that(?:'')?s it|i(?:'')?m done|i am done|total|summary|readback)\b", t):
        try:
            if INTENT_DEBUG: print("[Intent] allow: keyword verb")
        except Exception:
            pass
        return True
    try:
        if INTENT_DEBUG: print(f"[Intent] ignore: '{t}'")
    except Exception:
        pass
    return False
