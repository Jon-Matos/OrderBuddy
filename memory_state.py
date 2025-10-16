# memory_state.py — tiny durable memory for OrderBuddy
import json, os, time
from typing import Dict, Any, List

MEMO_PATH = ".orderbuddy_memory.json"

class ConversationMemory:
    def __init__(self, user_id: str = "default"):
        self.user_id = user_id
        self.prefs: Dict[str, Any] = {"default_milk": None, "default_size": None}
        self.summary: List[str] = []  # rolling bullets for LLM
        self.load()

    def load(self):
        if os.path.exists(MEMO_PATH):
            try:
                data = json.load(open(MEMO_PATH, "r", encoding="utf-8"))
                user = data.get(self.user_id, {})
                self.prefs.update(user.get("prefs", {}))
                self.summary = user.get("summary", [])
            except Exception:
                pass

    def save(self):
        data = {}
        if os.path.exists(MEMO_PATH):
            try:
                data = json.load(open(MEMO_PATH, "r", encoding="utf-8"))
            except Exception:
                data = {}
        data[self.user_id] = {"prefs": self.prefs, "summary": self.summary[-40:]}
        with open(MEMO_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)

    def learn_prefs_from_item(self, item):
        # opportunistic: if user keeps asking for same milk/size, remember
        if getattr(item, "milk", None):
            self.prefs["default_milk"] = item.milk
        if getattr(item, "size_or_variant", None) in {"sm", "md", "lg"}:
            self.prefs["default_size"] = item.size_or_variant

    def add_summary_bullet(self, text: str):
        ts = time.strftime("%H:%M")
        self.summary.append(f"[{ts}] {text}")
        self.summary = self.summary[-50:]
        self.save()

    def system_suffix(self) -> str:
        # Feed to system prompt to stabilize memory/context
        pref_bits = []
        if self.prefs.get("default_milk"):
            pref_bits.append(f"default milk: {self.prefs['default_milk']}")
        if self.prefs.get("default_size"):
            pref_bits.append(f"default drink size: {self.prefs['default_size']}")
        memo = "; ".join(pref_bits) or "no defaults known"
        recent = " | ".join(self.summary[-5:]) or "(no recent summary)"
        return f"Memory: {memo}. Recent: {recent}."
