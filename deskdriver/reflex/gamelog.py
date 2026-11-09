"""Follow Path of Exile 2's Client.txt: the plain-text log the game writes and third-party tools may read.

It never touches the game process. It turns new lines into facts a decision model can use: the zone and its monster
level, the character's level, deaths, and what NPCs just said. Global/trade chat (lines with '#', '$', '%', '&') is
ignored.
"""
import os
import re

DEFAULT = os.path.expanduser("~/.local/share/Steam/steamapps/common/Path of Exile 2/logs/Client.txt")
_PREFIX = re.compile(r"^\S+ \S+ \d+ [0-9a-f]+ \[\w+ Client \d+\] ")
_GEN = re.compile(r'Generating level (\d+) area "([^"]+)"')
_SCENE = re.compile(r"\[SCENE\] Set Source \[(.+)\]")
_LEVEL = re.compile(r": (\S+) \((\w+)\) is now level (\d+)")
_SLAIN = re.compile(r": (\S+) has been slain\.")
_NPC = re.compile(r"^([A-Z][a-z]+(?: [A-Z][a-z]+)?): (.+)$")


class GameLog:
    def __init__(self, path: str = DEFAULT, character: str = ""):
        self.path, self.character = path, character
        self.pos = os.path.getsize(path) if os.path.exists(path) else 0
        self.zone = None; self.area_level = None; self.area_id = None
        self.char_level = None; self.deaths = 0; self.npc = []      # [(name, line)] newest last
        self.events = []                                                # human-readable, newest last
        self._prime()

    def _prime(self):
        # read the last ~2 MB once so the zone and level are known before anything new happens
        try:
            with open(self.path, "rb") as fh:
                fh.seek(max(0, self.pos - 2_000_000))
                for raw in fh.read().decode("utf-8", "replace").splitlines():
                    self._line(raw, quiet=True)
        except OSError:
            pass

    def _line(self, raw, quiet=False):
        line = _PREFIX.sub("", raw).strip()
        if not line or line[0] in "#$%&@":
            return
        if m := _GEN.search(line):
            self.area_level, self.area_id = int(m.group(1)), m.group(2)
        elif m := _SCENE.search(line):
            z = m.group(1)
            if z not in ("(null)", "(unknown)") and not z.startswith("Act "):
                if z != self.zone and not quiet:
                    self.events.append(f"entered {z} (monster level {self.area_level})")
                self.zone = z
        elif m := _LEVEL.search(line):
            if not self.character or m.group(1) == self.character:
                self.char_level = int(m.group(3))
                if not quiet:
                    self.events.append(f"reached level {self.char_level}")
        elif m := _SLAIN.search(line):
            if not self.character or m.group(1) == self.character:
                if not quiet:
                    self.deaths += 1; self.events.append("died")
        elif (m := _NPC.match(line)) and not line.startswith(("Unable", "Joined", "Tile", "Doodad", "Generating")):
            self.npc.append((m.group(1), m.group(2))); self.npc = self.npc[-6:]
            if not quiet:
                self.events.append(f'{m.group(1)} said "{m.group(2)}"')
        self.events = self.events[-12:]

    def poll(self):
        """Read whatever the game has appended since the last call."""
        try:
            size = os.path.getsize(self.path)
            if size < self.pos:
                self.pos = 0
            if size == self.pos:
                return
            with open(self.path, "rb") as fh:
                fh.seek(self.pos)
                chunk = fh.read()
            self.pos = size
            for raw in chunk.decode("utf-8", "replace").splitlines():
                self._line(raw)
        except OSError:
            pass

    def summary(self) -> str:
        bits = []
        if self.zone:
            bits.append(f"Zone: {self.zone} (monster level {self.area_level}).")
        if self.char_level:
            gap = (self.area_level or 0) - self.char_level
            bits.append(f"Character level {self.char_level}" + (f", {gap} below the monsters." if gap > 0 else "."))
        if self.npc:
            bits.append("Last NPC lines: " + " / ".join(f'{n}: "{t}"' for n, t in self.npc[-2:]))
        if self.events:
            bits.append("Recently: " + "; ".join(self.events[-4:]) + ".")
        return " ".join(bits)
