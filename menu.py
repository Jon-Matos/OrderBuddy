# menu.py — fast-food menu (burgers, fries, shakes, soda) + helpers

TAX_RATE = 0.07

# Canonical menu (sizes/variants -> price)
MENU = {
    "burgers": {
        "hamburger": 2.49,
        "cheeseburger": 2.99,
        "double cheeseburger": 3.99,
        "big mac": 5.49,
        "quarter pounder": 5.19,
    },
    "sides": {
        "fries": {"sm": 2.19, "md": 2.49, "lg": 2.79},
    },
    "shakes": {
        "chocolate shake": {"sm": 3.19, "md": 3.69, "lg": 4.19},
        "vanilla shake":   {"sm": 3.19, "md": 3.69, "lg": 4.19},
        "strawberry shake":{"sm": 3.19, "md": 3.69, "lg": 4.19},
    },
    "drinks": {
        "soda": {"sm": 1.49, "md": 1.79, "lg": 1.99},
    }
}

# Allowed size aliases
ALIASES = {
    "small": "sm", "sm": "sm",
    "medium": "md", "md": "md",
    "large": "lg", "lg": "lg",
}

# Allowed modifiers for simple customization
# (We keep these lightweight and text-only; they do not change price.)
MODIFIERS = {
    "no onions", "extra onions",
    "no pickles", "extra pickles",
    "no cheese", "extra cheese",
    "no ketchup", "extra ketchup",
    "no mustard", "extra mustard",
}

def norm(s: str) -> str:
    return " ".join((s or "").lower().strip().split())

def find_item_category(item_name: str) -> str | None:
    n = norm(item_name)
    for cat, items in MENU.items():
        if isinstance(items, dict):
            if n in items:
                return cat
    return None

def options_for(item: str) -> list[str]:
    """Return available option keys (sizes/flavors) for an item."""
    item = norm(item)
    cat = find_item_category(item)
    if not cat:
        return []
    opts = MENU[cat][item]
    return list(opts.keys()) if isinstance(opts, dict) else []

SIZE_KEYS = {"sm", "md", "lg"}

def size_keys_for(item: str) -> set[str]:
    return set(options_for(item)) & SIZE_KEYS

def variant_keys_for(item: str) -> set[str]:
    return set(options_for(item)) - SIZE_KEYS

def pick_price(item: str, size_or_variant: str | None) -> float:
    """Return unit price for item at given size/variant."""
    item = norm(item)
    cat = find_item_category(item)
    if not cat:
        return 0.0
    options = MENU[cat][item]
    if isinstance(options, dict):
        key = size_or_variant
        if key not in options:
            return 0.0
        return float(options[key])
    try:
        return float(options)
    except Exception:
        return 0.0
