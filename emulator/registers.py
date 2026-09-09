# registers.py
import string


class Registers:
    def __init__(self, count):
        if count > 26:
            raise ValueError("Only 26 letter names available (A-Z)")
        self.register_count = count
        self.values = [0] * count
        self.name_to_index = {string.ascii_uppercase[i]: i for i in range(count)}

    def _resolve(self, r):
        """Accept either an int index or a letter name like 'A'."""
        if isinstance(r, str):
            if r not in self.name_to_index:
                raise ValueError(f"Unknown register: {r}")
            return self.name_to_index[r]
        if not (0 <= r < self.register_count):
            raise ValueError(f"Register index out of range: {r}")
        return r

    def write(self, r, val):
        idx = self._resolve(r)
        self.values[idx] = val & 0xFFFFFFFF

    def read(self, r):
        idx = self._resolve(r)
        return self.values[idx]

    def __repr__(self):
        names = string.ascii_uppercase
        return " ".join(f"{names[i]}={v:#010x}" for i, v in enumerate(self.values))
