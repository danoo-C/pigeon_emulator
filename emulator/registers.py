# registers.py
import string

REGISTER_NAMES = string.ascii_uppercase


class Registers:
    """The register file: `count` unsigned 32-bit registers named A, B, C...

    `values` is a plain list and is public on purpose -- the CPU's hot
    handlers index it directly, since read()/write()'s type dispatch cost
    about 4x a bare list index at a million instructions a second. Use
    read()/write() everywhere else: they mask to 32 bits and accept a
    letter name.
    """

    def __init__(self, count):
        if not 0 < count <= 26:
            raise ValueError(f"Register count must be 1-26 (A-Z), got {count}")
        self.register_count = count
        self.values = [0] * count
        self.name_to_index = {REGISTER_NAMES[i]: i for i in range(count)}

    def _resolve(self, r):
        """Accept either an int index or a letter name like 'A'."""
        if isinstance(r, str):
            if r not in self.name_to_index:
                raise ValueError(f"Unknown register: {r}")
            return self.name_to_index[r]
        if not (0 <= r < self.register_count):
            raise ValueError(
                f"Register index out of range: {r} (this CPU has "
                f"{self.register_count}: A-{REGISTER_NAMES[self.register_count - 1]})")
        return r

    def write(self, r, val):
        self.values[self._resolve(r)] = val & 0xFFFFFFFF

    def read(self, r):
        return self.values[self._resolve(r)]

    def __repr__(self):
        return " ".join(f"{REGISTER_NAMES[i]}={v:#010x}" for i, v in enumerate(self.values))
