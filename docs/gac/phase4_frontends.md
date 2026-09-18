# Phase 4: the front ends follow the mode

> Part of [the GAC plan](README.md). **Status: planned.** Needs Phase 2
> (Phase 3 is not required). Design:
> [design.md §5.5](design.md#55-the-host-side-front-ends-and-the-wire); the
> numbers are §4.4. Decisions: Q1, Q7, Q15 in [decisions.md](decisions.md).

**The goal:** when the guest changes the mode, the browser canvas and the
pygame window change with it, without a restart. When you resize the
window, the machine hears about it, **but only the guest decides (Q1).**

---

## Steps

### 4.1 `/info` gains `generation`, `format` and `modes`

Both front ends already read `w` and `h` from `/info` and have no default
geometry *(checked)*. `format` names the byte order, so an old client and a
new server cannot disagree without anyone noticing.

### 4.2 `/frame` carries the mode: `X-Pigeon-Mode: w,h,generation`

The client learns about a resize in the round trip it was already making,
so there is no polling of `/info`. A frame sized for the old mode has the
header to explain why.

### 4.3 Raw BGRA on the wire, swizzled in the client (Q7)

`DisplayIO._convert_to_rgba` stops running on the host. At 720p it was 19%
of a core (§4.4). `display/index.html` swizzles in a `Uint32Array` loop, and
`display/display.py` uses `pygame.image.frombuffer` with a format argument.

### 4.4 `POST /preferred {w, h}`

A resized window posts the size it would like. The VRAM device stores it,
and `VRAM_PREFERRED` answers it. Nothing switches until the guest asks
(Phase 6 has the kernel adopt it at the prompt).

### 4.5 Both front ends resize

`index.html` resizes its canvas on a generation change, and `display.py`
calls `set_mode` again. Mouse coordinates are rescaled; at worst one frame
of clicks lands a pixel or two out (Q15).

### 4.6 Tests

`/info` and `/frame` from a test client, before and after a `SET_MODE` sent
through the bus. The pygame client's scaling maths, which can be tested
without a window.

**Done when:** a `SET_MODE` sent from a Python test changes what the browser
draws, and the full suite passes.

---

## As built

*(filled in when the phase is done)*
