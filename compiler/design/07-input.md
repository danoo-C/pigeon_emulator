# 7. `<pigeon/input.h>` — keyboard and mouse

Unlike the display, input is **not** memory-mapped. It goes through the IO
controller on channel 3, so this library is the one place the compiler needs
inline assembly.

## The two buffers

Input comes in two shapes because guest code asks two different questions.

| | FIFO (events) | Real-time (state) |
|---|---|---|
| Answers | "what happened, in order?" | "what is true right now?" |
| Loses data? | never — 256 deep, drops the oldest when full | n/a, always current |
| Read | destructively; drain in a loop | non-destructively; sample per frame |
| Commands | 3, 4, 5 | 1, 2, 6, 7 |
| Use it for | typing, clicks, menu navigation | hold-to-move, drag, modifier keys |

Text entry needs the FIFO: a key pressed and released between two polls must
still register. Hold-to-move needs the real-time state: replaying every edge
since boot to work out whether `W` is down is absurd.

## Commands — verified against the running machine

| Cmd | Name | Returns |
|---|---|---|
| 1 | `GET_MOUSE_POS` | one word: `x` in bits 31–16, `y` in bits 15–0 (verified: x=37, y=42 → `0x0025002a`) |
| 2 | `GET_MOUSE_BUTTONS` | one byte: bit0 left, bit1 right, bit2 middle, bit3 back, bit4 forward |
| 3 | `GET_KEYBOARD` | pops one byte from a 256-entry FIFO; `0x00` when empty |
| 4 | `GET_MOUSE_EVENT` | pops one press/release edge; `0x00` when empty |

A mouse event byte is `0x80` valid | `0x40` pressed | button index in bits 0–4.
Verified: press-then-release of button 0 yields `0xc0`, `0x80`, then `0x00`.

Both queues are FIFOs rather than live state, which is the right design — a
click that begins and ends between two guest polls is still delivered.

## The gaps — all fixed

> Everything below was a real, measured defect. All four are now closed and
> covered by `tests/test_input.py`; the descriptions are kept because they
> explain why the protocol looks the way it does.

### Gap 1 ✅ — non-ASCII keys arrived as the wrong character

The FIFO stores `code & 0xFF`, but the client forwards raw pygame keycodes,
which for non-printable keys are above 2³⁰. Measured on the running machine:

| Key | Sent | Guest receives |
|---|---|---|
| `a` | 97 | 97 ✅ |
| Enter | 13 | 13 ✅ |
| Right arrow | 1073741903 | **79 — `O`** |
| Up arrow | 1073741906 | **82 — `R`** |
| F1 | 1073741882 | **58 — `:`** |

The failure is worse than losing the key: arrows arrive as plausible letters, so
a program reading text gets silent garbage rather than an obvious error.

**Fixed by translating at the front end, not in the guest.** The keycode space
is defined once in `emulator/devices/keycodes.py`: printable ASCII passes
through unchanged, named keys take `0x80`–`0x9F`, which ASCII does not use. Each
front end owns its own mapping onto that space — `PYGAME_TO_PIGEON` in
`display/display.py`, `KEYS` in `display/index.html` — and a test parses the JS
table and asserts it matches the Python one, because two hand-maintained tables
drift.

`HID.push_key` now **rejects** anything above `0xFF` rather than masking, so a
front end that forgets to translate fails loudly instead of feeding the guest
plausible-looking letters. An unmapped key is dropped, never truncated.

### Gap 2 ✅ — the browser front-end sent no input at all

`display/index.html` calls `/frame`, `/clear` and `/info` and nothing else. In
the browser the guest sees no keyboard and no mouse. The endpoints already
exist; this is entirely a front-end omission.

The page now wires `mousemove`, `mousedown`/`mouseup`, `keydown`/`keyup`,
`contextmenu` (suppressed, so right-click reaches the guest) and `blur` (which
releases every key, so losing focus mid-keypress does not leave it stuck down in
the state bitmap forever).

Two things made this more than "add some listeners":

- **The HID port is not hardcoded.** `/info` now returns `hid_url`, so the page
  follows `config.json` — which matters, since the ports here are 1234/1235, not
  the old 8000/8001 defaults.
- **CORS had to be widened.** The list was `["http://127.0.0.1", "http://localhost",
  f"http://{host}:{port}"]`, and a bare `http://127.0.0.1` does **not** match the
  origin `http://127.0.0.1:1234` — origins compare with the port. Every POST
  would have failed preflight. `Machine.start_servers` now passes the display
  origin explicitly.

### Gap 3 ✅ — no key-release events

Only `KEYDOWN` is forwarded, so a guest can see a key go down but never come up.
That rules out hold-to-move, the single most common thing a program wants from a
keyboard.

The mouse already solved this with an edge FIFO; the keyboard now matches.
`POST /key` takes `{"code", "pressed"}` (defaulting to `true`, so older callers
keep working), and that one change feeds all three consumers: the character
stream, the new edge FIFO, and the real-time held-key bitmap.

**Command 5, `GET_KEY_EVENT`**, sits alongside the character queue rather than
replacing it — text entry wants characters, games want edges, and they are
genuinely different. Two bytes, because one is not enough:

```
byte 0   bit 7 VALID   1 for a real event; a first byte of 0x00 means empty
         bit 6 PRESS   1 = went down, 0 = came up
byte 1   KEYCODE       the full 0x00-0xFF code
```

The VALID bit is why this is two bytes and not one: the character FIFO returns
`0x00` for "empty", which is indistinguishable from a real key whose code is 0.

### Gap 4 ✅ — the real-time mouse button mask was dead

Found while implementing the rest. **Nothing anywhere posted `/mouse_buttons`**,
and `push_mouse_event` did not touch the mask, so `GET_MOUSE_BUTTONS` returned 0
forever — half the real-time buffer had never worked. `push_mouse_event` now
maintains the mask from the edges it already receives, so it needed no front-end
change at all.

## API

```c
#ifndef PIGEON_INPUT_H
#define PIGEON_INPUT_H

/* --- mouse ------------------------------------------------------------ */
unsigned mouse_x(void);            /* 0 .. DISP_W-1 */
unsigned mouse_y(void);
unsigned mouse_buttons(void);      /* live bitmask, see MB_* below */

#define MB_LEFT    0x01
#define MB_RIGHT   0x02
#define MB_MIDDLE  0x04
#define MB_BACK    0x08
#define MB_FORWARD 0x10

/* One queued press/release edge, or 0 when the queue is empty. */
unsigned mouse_event(void);
#define ME_VALID(e)   ((e) & 0x80)
#define ME_PRESSED(e) ((e) & 0x40)
#define ME_BUTTON(e)  ((e) & 0x1F)

/* --- keyboard: FIFO --------------------------------------------------- */
int key_read(void);                /* next queued character, or -1 if none */

/* One queued press/release edge; 0 when the queue is empty. */
unsigned key_event(void);          /* (flags << 8) | code */
#define KE_VALID(e)   ((e) & 0x8000)
#define KE_PRESSED(e) ((e) & 0x4000)
#define KE_CODE(e)    ((e) & 0xFF)

/* --- keyboard: real-time ---------------------------------------------- */
int  key_down(int code);           /* is this key held right now? */
void key_bitmap(unsigned char out[32]);   /* all 256 keys at once */

#define KEY_BACKSPACE 0x08
#define KEY_TAB       0x09
#define KEY_ENTER     0x0D
#define KEY_ESC       0x1B
#define KEY_DELETE    0x7F
#define KEY_LEFT      0x80
#define KEY_RIGHT     0x81
#define KEY_UP        0x82
#define KEY_DOWN      0x83
#define KEY_HOME      0x84
#define KEY_END       0x85
#define KEY_PGUP      0x86
#define KEY_PGDN      0x87
#define KEY_INSERT    0x88
#define KEY_F(n)      (0x90 + (n) - 1)     /* KEY_F(1) .. KEY_F(12) */

#endif
```

## Implementation

Every call programs the IO header and fires the channel. The header is at
`IO_START` (`0x400`) with the offsets the assembler already predefines:

```c
#define IO_BASE     0x400u
#define IO_CHANNEL  (*(volatile unsigned *)(IO_BASE +  0))
#define IO_RW       (*(volatile unsigned *)(IO_BASE +  4))
#define IO_COMMAND  (*(volatile unsigned *)(IO_BASE +  8))
#define IO_LENGTH   (*(volatile unsigned *)(IO_BASE + 12))
#define IO_ADDRESS  (*(volatile unsigned *)(IO_BASE + 16))
#define IO_RETLEN   (*(volatile unsigned *)(IO_BASE + 20))
#define IO_DATA     ((volatile unsigned char *)(IO_BASE + 24))

#define CH_HID 3

static unsigned hid_read(unsigned command, unsigned length) {
    IO_RW      = 0;              /* read */
    IO_COMMAND = command;
    IO_LENGTH  = length;
    IO_ADDRESS = 0;
    IO_CHANNEL = CH_HID;         /* writing the channel fires it */
    return *(volatile unsigned *)IO_DATA;
}

unsigned mouse_x(void)       { return hid_read(1, 4) >> 16; }
unsigned mouse_y(void)       { return hid_read(1, 4) & 0xFFFF; }
unsigned mouse_buttons(void) { return hid_read(2, 1) & 0xFF; }
unsigned mouse_event(void)   { return hid_read(4, 1) & 0xFF; }

int key_read(void) {
    unsigned k = hid_read(3, 1) & 0xFF;
    return k ? (int)k : -1;
}

unsigned key_event(void)     { return hid_read(5, 2) & 0xFFFF; }
int      key_down(int code)  { return hid_read_at(6, 1, code) & 1; }

void key_bitmap(unsigned char out[32]) {
    hid_read(7, 32);                       /* 32 bytes land in the window */
    for (unsigned i = 0; i < 32; i++) out[i] = IO_DATA[i];
}
```

`hid_read_at` is `hid_read` with the key code written to `IO_ADDRESS` — command
6 is the one that reads a parameter rather than just returning state.

Replies are a **fixed size per command**, never `length` bytes. That matters:
`length` used to drive the FIFO commands, so a word-sized read of command 3
popped *four* keys and looked at one.

Two things make this work, and both are properties of the machine rather than
of the code:

**The command completes before the next instruction.** Writing `IO_CHANNEL` sets
a pending flag that the main loop services immediately after the store retires,
so the reply is in the data window by the time the next C statement runs. There
is no polling and no completion flag to wait on. *(This is a consequence of the
`io_pending` design in `emulator/ram.py`; a guest that ran the IO bus
asynchronously would need a status check here.)*

**`volatile` is doing real work.** These are device registers, not memory, and
the sequence of stores is the protocol — the channel store must come last. The
compiler must not reorder or elide them, which is exactly what `volatile` means
and the one place [02-language.md](02-language.md)'s subset genuinely needs it.

`hid_read` returns the first word of the data window even for one-byte replies;
the callers mask. That is a word load (`MRW`) rather than a byte load, which is
fine because the controller zero-fills the rest of the reply.

## Reading input in a loop

Drain the queues each frame, then sample live state:

```c
unsigned char held[32];

for (;;) {
    /* 1. drain the FIFOs -- discrete things that must not be missed */
    int k;
    while ((k = key_read()) >= 0) {
        if (k == KEY_ESC) return 0;
        type_character(k);
    }
    unsigned e;
    while ((e = mouse_event()) != 0)
        handle_click(ME_BUTTON(e), ME_PRESSED(e), mouse_x(), mouse_y());

    /* 2. sample the real-time state -- what is true this instant */
    key_bitmap(held);
    if (held[KEY_LEFT  >> 3] & (1 << (KEY_LEFT  & 7))) player_x--;
    if (held[KEY_RIGHT >> 3] & (1 << (KEY_RIGHT & 7))) player_x++;

    draw(player_x, mouse_x(), mouse_y(), mouse_buttons());
}
```

**Drain in a `while`, not once per frame.** Both FIFOs hold 256 entries and drop
the *oldest* when full, so a slow frame silently loses the earliest input rather
than the most recent.

**Use the right buffer for the job.** Polling `key_read()` to steer a character
gives you the keyboard's auto-repeat rate, not smooth motion; testing the bitmap
gives one step per frame for as long as the key is held. Conversely, sampling
the bitmap for typing drops any key pressed and released inside one frame.
