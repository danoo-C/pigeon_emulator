# pigeon-cc — design

A small C compiler targeting the pigeon machine, plus the two libraries that
make it useful: a memory-operations standard library and a display library.

This is a design, not an implementation. Everything here that could be checked
against the real machine **was** — the calling convention, the signed-comparison
and modulo lowerings, and the framebuffer colour format were each assembled and
run before being written down. Claims that were only reasoned about are marked
*unverified*.

| Document | What it covers |
|---|---|
| [01-overview.md](01-overview.md) | Goals, pipeline, and the five ISA facts that shape every decision |
| [02-language.md](02-language.md) | The C subset: types, operators, what is deliberately missing |
| [03-abi.md](03-abi.md) | Register roles, the software frame stack, the calling convention |
| [04-codegen.md](04-codegen.md) | How each construct lowers to pigeon assembly |
| [05-stdlib.md](05-stdlib.md) | `<pigeon/mem.h>` — memcpy/memset/memmove/memcmp, malloc/free |
| [06-display.md](06-display.md) | `<pigeon/display.h>` — pixels, rects, text, in plain C |
| [07-input.md](07-input.md) | `<pigeon/input.h>` — keyboard and mouse, **and the gaps found** |

---

## Input: was half-built, now complete

> **Status: all four gaps fixed.** The account below is kept because it
> explains the protocol's shape. See [07-input.md](07-input.md) for the API.

**What exists and works.** The machine has a full HID device on IO channel 3,
wired into `Machine` and reachable from guest code today:

| Command | Returns |
|---|---|
| 1 `GET_MOUSE_POS` | one word, `x` in the high 16 bits, `y` in the low 16 |
| 2 `GET_MOUSE_BUTTONS` | one byte bitmask: bit0 left, bit1 right, bit2 middle, bit3 back, bit4 forward |
| 3 `GET_KEYBOARD` | pops one byte from a 256-entry key FIFO, `0x00` when empty |
| 4 `GET_MOUSE_EVENT` | pops one button press/release edge, `0x00` when empty |

Both queues are FIFOs rather than live state, so a press-and-release that
happens between two guest polls is not lost. The pygame client forwards mouse
motion, three buttons (down *and* up), and key presses over HTTP. I verified all
four commands return the documented encodings.

**Gap 1 — non-ASCII keys are silently corrupted.** The key FIFO stores a *byte*
(`code & 0xFF`), but the client forwards raw pygame keycodes, which for anything
that isn't a printable character are above 2^30. Measured:

| Key | Client sends | Guest receives |
|---|---|---|
| `a` | 97 | 97 ✅ |
| Enter | 13 | 13 ✅ |
| Right arrow | 1073741903 | **79 — the letter `O`** |
| Up arrow | 1073741906 | **82 — the letter `R`** |
| F1 | 1073741882 | **58 — a colon** |

So arrow keys do not merely fail, they arrive as plausible-looking letters. Any
program reading the keyboard will act on them. Fixed by translating to a small
dedicated keycode space at the client, described in [07-input.md](07-input.md).

**Gap 2 — the browser front-end sends no input at all.** `display/index.html`
calls only `/frame`, `/clear` and `/info`. If you are using the browser rather
than the pygame client, the guest sees no keyboard or mouse whatsoever. Adding
it is about 30 lines of JavaScript against endpoints that already exist.

**Gap 3 — there are no key-release events.** The client handles `KEYDOWN` only,
so a guest can detect a key going down but never coming up, which rules out
"hold to move". The mouse already solves this with press/release edges; the
keyboard needs the same treatment.

[07-input.md](07-input.md) specifies the C API over what exists today, and the
protocol changes that close all three gaps.

---

## The shape of it

```
   program.c ──▶ pigeon-cc ──▶ program.asm ──▶ assembler/assembler.py ──▶ build/program.bin
                     │                                    ▲
                     └── libc sources (mem, display, input) ┘
```

The compiler emits **assembly text**, not machine code, and hands it to the
assembler that already exists and is covered by byte-exact golden tests. That
removes encoding, label resolution and the memory map from the compiler's
problem entirely.
