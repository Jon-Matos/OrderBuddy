# order_engine.py — Cart with item IDs, sizes, simple text modifiers (no onions / extra pickles)
# Speech summary: only total + polite follow-up; no [itX] ids in any spoken labels.

from dataclasses import dataclass, field
from typing import List, Optional
import itertools

from menu import (
    MENU, TAX_RATE, ALIASES, norm, pick_price,
    options_for, size_keys_for, variant_keys_for,  # noqa: F401 (kept for future)
)
from menu import MODIFIERS  # allowed text modifiers for items

_uid_counter = itertools.count(1)
def new_uid() -> str:
    return f"it{next(_uid_counter)}"

@dataclass
class LineItem:
    uid: str = field(default_factory=new_uid)
    item: str = ""                     # e.g., "cheeseburger", "fries", "chocolate shake"
    qty: int = 1
    size_or_variant: Optional[str] = None  # sm/md/lg for fries/shakes/soda; None for burgers
    price: float = 0.0                 # unit price (filled on add())
    mods: List[str] = field(default_factory=list)  # text modifiers (no onions, extra pickles, ...)

    def label(self) -> str:
        bits = []
        if self.qty and self.qty != 1:
            bits.append(f"{self.qty}×")
        if self.size_or_variant:
            bits.append(self.size_or_variant)
        bits.append(self.item)
        if self.mods:
            bits.append("(" + ", ".join(self.mods) + ")")
        return " ".join(bits)

    def line_total(self) -> float:
        return round(self.price * max(self.qty, 1), 2)

class Cart:
    def __init__(self):
        self.items: List[LineItem] = []

    def _validated_copy(self, li: LineItem) -> LineItem:
        li2 = LineItem(
            item=norm(li.item),
            qty=max(1, int(li.qty or 1)),
            size_or_variant=li.size_or_variant,
            mods=[m for m in (li.mods or []) if m in MODIFIERS],
        )
        # Validate size for sized items
        opts = set(options_for(li2.item))
        if li2.size_or_variant and li2.size_or_variant not in opts:
            li2.size_or_variant = None  # invalid -> force clarification upstream
        # Price
        li2.price = pick_price(li2.item, li2.size_or_variant)
        return li2

    # ---- Add / merge: only merge when attributes match (including mods)
    def add(self, li: LineItem):
        li = self._validated_copy(li)
        for e in self.items:
            if (e.item, e.size_or_variant, tuple(e.mods)) == (li.item, li.size_or_variant, tuple(li.mods)):
                e.qty += li.qty
                return
        self.items.append(li)

    def remove_last(self) -> bool:
        if not self.items:
            return False
        self.items.pop()
        return True

    # ---- Selection helpers for edits
    def select(self, where: dict) -> List[LineItem]:
        """
        where keys: uid, item, nth (first/second/last), all=True
        """
        if "uid" in where:
            return [i for i in self.items if i.uid == where["uid"]]

        candidates = self.items
        if "item" in where:
            it = norm(where["item"])
            candidates = [i for i in candidates if i.item == it]

        # NEW: optional size/variant filter
        sz = where.get('size_or_variant') or where.get('size') or where.get('variant')
        if sz:
            candidates = [i for i in candidates if i.size_or_variant == sz]

        # optional modifiers filter
        try:
            mods = where.get("mods") if isinstance(where, dict) else None
        except Exception:
            mods = None
        if mods:
            candidates = [i for i in candidates if all((m in (i.mods or [])) for m in mods)]
        if where.get("all"):
            return candidates

        nth = where.get("nth")
        if nth == "first" and candidates:
            return [candidates[0]]
        if nth == "second" and len(candidates) >= 2:
            return [candidates[1]]
        if nth == "last" and candidates:
            return [candidates[-1]]
        return candidates

    def apply_edit(self, edits: List[dict]) -> List[str]:
        """
        Each edit: {
          "where": {...},
          "ops": {
            "qty": +1/-1/setN,
            "size_or_variant": "lg",
            "replace_item": "hamburger",
            "add_mod": "no onions" | ["no onions", "extra pickles"],
            "remove_mod": "no onions" | ["no onions"],
            "clear_mods": true
          }
        }
        Returns human-readable labels of changed targets (no [itX] ids).
        """
        changed = []
        for cmd in edits:
            targets = self.select(cmd.get("where", {}))
            for li in targets:
                ops = cmd.get("ops", {})

                if "replace_item" in ops:
                    new_item = norm(ops["replace_item"])
                    li.item = new_item
                    # Reset size if invalid for the new item
                    opts = set(options_for(li.item))
                    if li.size_or_variant not in opts:
                        li.size_or_variant = None

                if "qty" in ops:
                    q = ops["qty"]
                    if isinstance(q, int):
                        li.qty = max(1, q)
                    elif isinstance(q, str) and q.startswith(("+", "-")):
                        li.qty = max(1, li.qty + int(q))

                if "size_or_variant" in ops:
                    if ops["size_or_variant"] in set(options_for(li.item)):
                        li.size_or_variant = ops["size_or_variant"]

                # Modifiers
                def _to_list(v):
                    if v is None:
                        return []
                    return v if isinstance(v, list) else [v]

                if "add_mod" in ops:
                    for m in _to_list(ops["add_mod"]):
                        if m in MODIFIERS and m not in li.mods:
                            li.mods.append(m)

                if "remove_mod" in ops:
                    for m in _to_list(ops["remove_mod"]):
                        if m in li.mods:
                            li.mods = [x for x in li.mods if x != m]

                if ops.get("clear_mods"):
                    li.mods = []

                li.price = pick_price(li.item, li.size_or_variant)
                changed.append(li.label())
        return changed

    
    def remove(self, where: dict, qty: int | None = None) -> int:
        """Remove items matching `where`. If qty is None: remove whole line(s).
        If qty is provided: decrease quantity across matching lines, removing lines as needed.
        Returns number of affected lines."""
        targets = list(self.select(where))
        affected = 0
        if qty is None:
            # remove all matching lines
            for li in targets:
                try:
                    self.items.remove(li)
                    affected += 1
                except ValueError:
                    pass
            return affected
        # decrement qty across matches (prefer most recent first)
        try: qty_left = max(1, int(qty))
        except Exception: qty_left = 1
        for li in reversed(targets):
            if qty_left <= 0: break
            current = max(1, int(getattr(li, "qty", 1)))
            if current <= qty_left:
                try:
                    self.items.remove(li)
                    affected += 1
                except ValueError:
                    pass
                qty_left -= current
            else:
                li.qty = current - qty_left
                affected += 1
                qty_left = 0
        return affected

    def subtotal(self) -> float:
        return round(sum(i.line_total() for i in self.items), 2)

    def tax(self) -> float:
        return round(self.subtotal() * TAX_RATE, 2)

    def total(self) -> float:
        return round(self.subtotal() + self.tax(), 2)

    def summary_speech(self) -> str:
        """
        Compact speech output:
          - No [itX] ids
          - No subtotal/tax breakdown
          - Just the total and a follow-up question
        """
        if not self.items:
            return "Your cart is empty."
        return f"That will be ${self.total():.2f}. Would you like anything else?"
    def needs_size_items(self):
        """Return list of item names in cart that require a size but don't have one."""
        out = []
        for li in self.items[::-1]:
            try:
                opts = set(options_for(li.item))
            except Exception:
                opts = set()
            if opts and not li.size_or_variant:
                out.append(li.item)
        return list(reversed(out))

    def set_size_on_last_unset(self, size_code: str):
        """Apply size to the most recent item that needs a size. Returns the item name if applied."""
        if not size_code:
            return None
        for li in reversed(self.items):
            try:
                opts = set(options_for(li.item))
            except Exception:
                opts = set()
            if not opts:
                continue
            if not li.size_or_variant and size_code in opts:
                li.size_or_variant = size_code
                try:
                    li.price = pick_price(li.item, size_code)
                except Exception:
                    pass
                return li.item
        return None

