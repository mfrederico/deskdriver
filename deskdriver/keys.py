"""Key names -> Linux evdev keycodes, and characters -> X keysyms."""

# Linux input-event-codes.h
KEYCODES = {
    "esc": 1, "1": 2, "2": 3, "3": 4, "4": 5, "5": 6, "6": 7, "7": 8, "8": 9, "9": 10, "0": 11,
    "minus": 12, "equal": 13, "backspace": 14, "tab": 15,
    "q": 16, "w": 17, "e": 18, "r": 19, "t": 20, "y": 21, "u": 22, "i": 23, "o": 24, "p": 25,
    "leftbrace": 26, "rightbrace": 27, "enter": 28, "ctrl": 29,
    "a": 30, "s": 31, "d": 32, "f": 33, "g": 34, "h": 35, "j": 36, "k": 37, "l": 38,
    "semicolon": 39, "apostrophe": 40, "grave": 41, "shift": 42, "backslash": 43,
    "z": 44, "x": 45, "c": 46, "v": 47, "b": 48, "n": 49, "m": 50, "comma": 51, "dot": 52, "slash": 53,
    "rightshift": 54, "kpasterisk": 55, "alt": 56, "space": 57, "capslock": 58,
    "f1": 59, "f2": 60, "f3": 61, "f4": 62, "f5": 63, "f6": 64, "f7": 65, "f8": 66, "f9": 67, "f10": 68,
    "numlock": 69, "scrolllock": 70, "f11": 87, "f12": 88, "kpenter": 96, "rightctrl": 97, "kpslash": 98,
    "printscreen": 99, "rightalt": 100, "home": 102, "up": 103, "pageup": 104, "left": 105, "right": 106,
    "end": 107, "down": 108, "pagedown": 109, "insert": 110, "delete": 111, "super": 125, "rightsuper": 126,
    "menu": 127, "volumedown": 114, "volumeup": 115, "mute": 113,
}

ALIASES = {
    "control": "ctrl", "ctl": "ctrl", "option": "alt", "meta": "super", "win": "super", "cmd": "super",
    "command": "super", "return": "enter", "escape": "esc", "del": "delete", "ins": "insert",
    "pgup": "pageup", "pgdn": "pagedown", "pgdown": "pagedown", "arrowup": "up", "arrowdown": "down",
    "arrowleft": "left", "arrowright": "right", "spacebar": "space", "bksp": "backspace", "print": "printscreen",
    "prtsc": "printscreen", "lshift": "shift", "rshift": "rightshift", "lctrl": "ctrl", "rctrl": "rightctrl",
    "lalt": "alt", "ralt": "rightalt", "altgr": "rightalt", "lsuper": "super", "rsuper": "rightsuper",
}

MODIFIERS = {"ctrl", "shift", "alt", "super", "rightctrl", "rightshift", "rightalt", "rightsuper"}


def normalize(name: str) -> str:
    n = name.strip().lower()
    return ALIASES.get(n, n)


def parse_combo(combo: str) -> list[tuple[str, int]]:
    """'ctrl+shift+t' -> [('code', 29), ('code', 42), ('sym', ord('t'))].

    Named keys become evdev keycodes; single characters become keysyms, so they
    follow the active keyboard layout. A literal '+' is written as 'plus'.
    """
    parts = [p for p in combo.replace(" ", "").split("+")] if combo != "+" else ["+"]
    out: list[tuple[str, int]] = []
    for raw in parts:
        if raw == "":
            continue
        name = normalize(raw)
        if name == "plus":
            name = "+"
        if name in KEYCODES:
            out.append(("code", KEYCODES[name]))
        elif len(name) == 1:
            out.append(("sym", char_keysym(name)))
        else:
            raise ValueError(f"unknown key '{raw}'")
    if not out:
        raise ValueError("empty key combo")
    return out


def char_keysym(ch: str) -> int:
    """X11 keysym for a character: Latin-1 maps directly, everything else is 0x01000000 | codepoint."""
    cp = ord(ch)
    if 0x20 <= cp <= 0xFF:
        return cp
    return 0x01000000 | cp
