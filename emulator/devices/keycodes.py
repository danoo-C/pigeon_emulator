"""The pigeon keycode space -- the one definition every front end maps onto.

Guest code sees a single byte per key. Printable ASCII passes straight
through, so `if (key == 'a')` works in C. Everything else gets a code in
0x80-0x9F, which ASCII does not use.

This exists because the key FIFO stores a byte and the pygame client used
to send raw pygame keycodes, which are above 2**30 for anything that is
not a printable character. Masking those to a byte turned the Right arrow
(1073741903) into 79 -- the letter 'O'. Arrows arrived as plausible
letters, so programs acted on them silently.

Deliberately no pygame import: this module is the shared vocabulary, and
the emulator must not depend on a front end being installed. Each front
end owns its own translation table and imports the constants from here.
`display/index.html` mirrors this table in JavaScript, and
tests/test_input.py asserts the two agree.
"""

# --- control keys: the ASCII ones keep their ASCII values ---------------
KEY_BACKSPACE = 0x08
KEY_TAB       = 0x09
KEY_ENTER     = 0x0D
KEY_ESC       = 0x1B
KEY_SPACE     = 0x20
KEY_DELETE    = 0x7F

# --- named keys: 0x80-0x9F, outside ASCII -------------------------------
KEY_LEFT      = 0x80
KEY_RIGHT     = 0x81
KEY_UP        = 0x82
KEY_DOWN      = 0x83
KEY_HOME      = 0x84
KEY_END       = 0x85
KEY_PGUP      = 0x86
KEY_PGDN      = 0x87
KEY_INSERT    = 0x88
KEY_LSHIFT    = 0x89
KEY_RSHIFT    = 0x8A
KEY_LCTRL     = 0x8B
KEY_RCTRL     = 0x8C
KEY_LALT      = 0x8D
KEY_RALT      = 0x8E

# F1..F12 occupy 0x90-0x9B, so KEY_F(n) is a simple offset.
KEY_F1        = 0x90


def key_f(n: int) -> int:
    """F1..F12 -> 0x90..0x9B."""
    if not 1 <= n <= 12:
        raise ValueError(f"function key out of range: F{n}")
    return KEY_F1 + n - 1


# Highest code the space uses. Anything above 0xFF cannot reach the guest
# at all -- the FIFO and the state bitmap are both byte-indexed.
KEY_MAX = 0xFF

#: name -> code, for the front ends and for the JS-table consistency test.
NAMED_KEYS = {
    name[4:].lower(): value
    for name, value in sorted(globals().items())
    if name.startswith("KEY_") and isinstance(value, int) and name != "KEY_MAX"
}
NAMED_KEYS.update({f"f{n}": key_f(n) for n in range(1, 13)})


def is_printable(code: int) -> bool:
    """True for the ASCII range that passes through untranslated."""
    return 0x20 <= code <= 0x7E


def describe(code: int) -> str:
    """A readable name for a code -- used in logs and test failures."""
    if is_printable(code):
        return repr(chr(code))
    for name, value in NAMED_KEYS.items():
        if value == code:
            return name.upper()
    return f"0x{code:02X}"
