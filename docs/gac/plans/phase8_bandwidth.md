# Phase 8: frames that did not change are not sent again

> Part of [the GAC plan](../README.md). **Status: planned, every question
> decided ([§7](#7-decisions)), 2026-09-19; built the same day ([§8](#8-as-built)).** Needs
> Phase 7 (built). Design: [design.md §5.5](../design.md#55-the-host-side-front-ends-and-the-wire)
> ("Damage tracking"); the numbers are §4.4. Decisions: Q10 in
> [decisions.md](../decisions.md). Facts marked *(checked)* were read in the
> code, and *(measured)* ones were run on this machine on 2026-09-19.
>
> This file replaces the original sketch, `phase8_damage.md`, which said
> "its exact shape is decided by what the measurements say after Phase 7".
> This is that shape.

**The goal:** a front end fetches a frame only when the picture changed, and
only the rows that changed. An idle screen costs nothing, and typing on a
720p console costs a band of 9 rows instead of the whole screen.

**What you will be able to see at the end of this phase:** nothing on the
screen. Every picture is the same. What changes is how much crosses between
the emulator and the window: from 226 MiB/s to almost nothing while the
screen sits still, and pygame's CPU use drops with it. **This phase is
optional.** §1 says what it is worth.

---

## 1. What the measurements say

The real servers, `demo` running at 1280 × 720, and a separate client process
fetching `/frame` *(measured)*:

| screen | client | the emulator runs at | the client gets |
|---|---|---|---|
| 1280 × 720 | none | 2,466,771 instructions/s | |
| 1280 × 720 | as pygame does: no pause | 2,306,632 (**−6.5%**) | 64 frames/s, **226 MiB/s** |
| 1280 × 720 | as the browser does: every 66 ms | 2,454,016 (−0.5%) | 12 frames/s, 41 MiB/s |
| 192 × 108 | as pygame does | 2,400,621 (−8%) | 100 frames/s, 8 MiB/s |

Three things follow:

1. **Bandwidth is the cost the design predicted, but it hardly slows the
   emulator.** Sending 226 MiB/s costs it 6.5%, because a socket write is mostly
   done outside Python's lock. The design's worry (§4.4) was right about the
   bytes, and too pessimistic about what they cost the machine.
2. **Most of what is sent is not new.** The emulator makes a frame 30 times a
   second (`DISPLAY_FPS` *(checked)*), and pygame fetches 64 a second: half
   are the same frame twice. When nothing on the screen moves, which at a
   prompt is nearly always, every frame is the same frame.
3. **Finding what changed is cheap on the host** *(measured, 1280 × 720)*.
   Comparing two frames is one byte comparison at C speed: 0.009 ms when they
   differ early, and the length of the frame, about 0.3 ms, when they are the
   same. Finding the band of rows that changed is **0.06 to 0.8 ms**. Both fit
   easily inside the 33 ms between frames.

So the saving is almost all on the wire and in the clients: pygame decodes
and scales every frame it is sent. The emulator gains the 6.5%.

---

## 2. The design, and how it differs from the sketch (Q1)

The sketch had the GAC keep the rectangles it drew (`DAMAGE`), and send the
whole frame whenever anything was written through the aperture. **The host can
see what changed without either:** it has the frame it last served, and the
one it is about to serve, and comparing them is cheaper than the bookkeeping.
It also catches every way a picture can change: the GAC, a store through the
aperture, the RAM screen at 192 × 108, `CH_DISPLAY`'s fill. The GAC would
only know about the first. So:

### 2.1 Frames get numbers

`DisplayIO.update()`, 30 times a second, compares the new snapshot with the
last one. **Only a different picture gets a new frame number,** along with
the band of rows that differ: `(first, last)`. A mode change is always a new
frame, and the whole of it.

### 2.2 `/frame?since=N`

- **No `since`:** the whole frame, as today. Old clients and every test
  keep working.
- **`since` is the current frame:** `204 No Content`. Nothing to send.
- **`since` is a recent frame in the same mode:** the rows that changed since
  then, the union of each frame's band after N (the server keeps the last 64
  bands), with `X-Pigeon-Rows: first,last`.
- **Too old, or another mode:** the whole frame.

Every reply carries `X-Pigeon-Frame: N` beside `X-Pigeon-Mode`.

### 2.3 The clients (Q3)

- **The browser** keeps its last frame, asks `since`, and on a band swizzles
  only those rows and puts them in with `putImageData(img, 0, first)`. It
  draws nothing on a 204.
- **pygame** does the same with a `bytearray`, and **fetches at its frame rate
  (30), not as fast as it can**. On a 204 it does not decode or scale again.

### 2.4 `GAC_DAMAGE` (Q4)

It stays reserved and answers 0, as it has since Phase 3. With the host
comparing frames, it has nothing left to do, and `docs/gac.md` says so.

---

## 3. Steps

1. **Measure, into `tools/bench.py`:** a "frames served" line at 1280 × 720:
   MiB/s with the screen idle, while typing, and with `cube` spinning, for
   the pygame client and the browser's rate. These are the numbers the phase
   is judged by, before and after.
2. **Frame numbers and bands** in `DisplayIO` (§2.1), with the comparison
   timed in the bench.
3. **`/frame?since=N`** (§2.2): the reply as a plain function, as
   `frame_reply` already is, and tested without a server.
4. **The browser** (§2.3): the page's logic gets `applyBand(frame, band,
   first, w)`, tested under node; the wiring reads `X-Pigeon-Rows`.
5. **pygame** (§2.3): `screen_mode.py` gets the same `apply_band`, tested
   against the page's through one table of cases, as Phase 4's logic was; the
   fetch loop is paced.
6. **Measure again**, and write the before and after into "As built".
7. **Full suite.**

**Done when:** the idle 720p screen costs next to nothing on the wire; typing
costs a few rows a key; `cube` still arrives whole and on time; the
pictures in both clients are the same as the emulator's (a test assembles a
band onto the previous frame and compares with the whole one); and the full
suite passes.

---

## 4. Tests

- **The server's replies:** an unchanged frame is a 204; a change in one row
  is that row, `X-Pigeon-Rows` says which, and the band put onto the old
  frame gives the new one byte for byte; several changes between polls are
  their union; a mode change or a `since` too old is the whole frame; no
  `since` is the whole frame, as before.
- **The clients' logic:** `applyBand` under node and `apply_band` in Python,
  on one table of cases, each checked against the whole frame.
- **The wiring:** read from the source, as Phase 4's was.
- **End to end:** the emulator's real servers and a client asking `since`,
  counting bytes: an idle console for 2 seconds sends almost nothing, and one
  key sends one band.

---

## 5. Risks

- **A client that misses a band would drift.** It never builds on a frame it
  does not have: it says which one it has (`since`), and the server answers
  from that one or sends the whole frame. A client that is wrong about what it
  has is the only way to drift, and each reply names the frame it makes, so
  the client adopts that number and nothing else.
- **Memory:** the last frame is kept already. The bands are 64 pairs of
  numbers.
- **The browser's timer is unchanged** (every 66 ms). A frame at 15 FPS is
  what it has always drawn, and asking `since` just makes most of them free.

---

## 6. Not in this phase

- **Compressing frames** (zlib, PNG). That is CPU in Python on every frame,
  to save bytes on localhost. Sending less is better than sending smaller.
- **A push channel** (WebSocket, server-sent events) instead of polling. With
  `since`, a poll that finds nothing is 204 and a few headers; a push channel
  would save those, at the cost of a second protocol in both clients.
- **Columns as well as rows.** Rows are one slice of memory each, and a band
  of them is one piece; a rectangle would be a copy per row. At 720p a row is
  5 KB, so a band of console text is already small.

---

## 7. Decisions

Answered 2026-09-19: *"i choose all recommendations. please build it!"*.
Every recommendation stands.

1. ~~**Find what changed on the host, by comparing frames, instead of the GAC
   tracking what it drew?**~~ **Recommendation:** yes (§2). It catches every
   kind of write, costs under a millisecond a frame, and needs no change to
   the devices. The sketch's plan sends the whole frame for any store through
   the aperture, which is how `cube`, `graph` and every per-pixel program
   draw.

   **Decided (you), 2026-09-19:** the recommendation.

2. ~~**Send a band of changed rows, not only "changed or not"?**~~
   **Recommendation:** yes. "Changed or not" alone is most of the win (an
   idle screen is free), but typing on a 720p console would still send
   3.6 MB a key. A band is 46 KB for a row of text, and it is little more
   code in each client.

   **Decided (you), 2026-09-19:** the recommendation.

3. ~~**pygame fetches at its frame rate, with `since`, instead of as fast as it
   can?**~~ **Recommendation:** yes. It fetches 64 times a second a picture that
   changes at most 30 times, and it decodes and scales each one.

   **Decided (you), 2026-09-19:** the recommendation.

4. ~~**What becomes of `GAC_DAMAGE`?**~~ **Recommendation:** it stays reserved,
   answering 0, and `docs/gac.md` says it is not used. Removing it would
   renumber nothing, since it is not in the middle of anything, but keeping the
   number reserved costs nothing either. Implementing it would be work
   nothing uses.

   **Decided (you), 2026-09-19:** the recommendation.

5. ~~**Do this phase at all?**~~ §1 says the emulator only gains about 6.5%, and
   the rest of the saving is in bandwidth on localhost and in the clients.
   **Recommendation:** yes, because the idle case is most of the time and
   today it costs 226 MiB/s for a picture that does not move. It is small,
   and host-side only. But it is the one phase that changes nothing you can
   see, so "no" is a fair answer, and the GAC plan is complete without it.

   **Decided (you), 2026-09-19:** the recommendation.

---

## 8. As built

Built 2026-09-19, as planned, every recommendation taken.

- **`emulator/devices/display_io.py`:** `update()` compares each snapshot with
  the last. An unchanged picture is not a new frame; a changed one gets the
  next number and its band `(first, last)`, found by `changed_rows()`. The
  last 64 bands are kept (`BANDS`). A mode change, or `clear()`, empties them
  and records the whole frame. `frame_state()` hands out the picture, its
  number and the bands under one lock, so a reply never mixes two frames.
- **`/frame?since=N`** is `frame_since()`, a plain function: 204 when N is
  the current frame; the union of the bands after N, with
  `X-Pigeon-Rows: first,last`, when every band since N is still kept;
  otherwise the whole frame, with `X-Pigeon-Rows: 0,h-1`. A `since` from the
  future, or one never served, is the whole frame too. Every reply says
  `X-Pigeon-Frame`. `/frame` without `since` is what it was.
- **The browser** keeps its RGBA frame, asks `since`, swizzles only the band
  (`applyBand`), and puts only those rows in. A 204 draws nothing. A new mode
  forgets the frame it had.
- **pygame** keeps a `bytearray`, patches it with `screen_mode.apply_band`,
  fetches at its frame rate, and caches the scaled surface by frame number,
  so an unchanged frame is neither decoded nor scaled again.
- **`GAC_DAMAGE`** stays reserved; `docs/gac.md` says the server compares
  frames instead.

### What it saved *(measured, 1280 × 720, the real servers)*

| case | before | after |
|---|---|---|
| idle, pygame's client | 226 MiB/s, the emulator −6.5% | 0.88 MiB/s: the first frame, then 204s |
| idle, the browser's rate | 41 MiB/s | 0.88 MiB/s, likewise |
| typing at the console | a whole frame each time | 1.04 MiB/s, about 33 KB a key |
| `demo` while typing | whole frames | 16 MiB/s: its band spans most of the screen |
| `cube` spinning | whole frames | 27 MiB/s pygame, 22 MiB/s the browser |

`cube` and `demo` redraw across the whole screen, so their bands are most of
it: they save what the repeated frames cost, not more. Rows, not rectangles
(§6), is what a smaller band for them would need.

The compare in `tools/bench.py`: an unchanged 720p frame 1.25 ms, one row
changed 1.65 ms, well inside the 33 ms between frames.

### Tests

`tests/test_frontends.py`: the replies without a server (204, a band that
rebuilds the new frame byte for byte, the union of several, too old, the
future, a mode change); `parseRows`/`parse_rows` and `applyBand`/`apply_band`
on one table, under node and in Python; the wiring from the source; and the
real servers end to end, where an unchanged screen answers 204 and one row
drawn answers `X-Pigeon-Rows: 50,50`. The full suite: 1,890 passed.
