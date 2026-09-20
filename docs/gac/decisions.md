# GAC and VRAM: decisions

> Part of [the GAC plan](README.md). Section numbers are kept from the
> original single-file plan. Every question is answered; nothing here is
> open.

---

## 10. Decisions

Answered in this file on 2026-09-17. Your words are kept as you wrote them.
Where the answer was *"Recommendation"* or left blank, the recommendation
stood and is marked as such; the two that changed the design have a **Reply:**
under them.

1. ~~**Who owns the resolution — the guest, the host window, or
   `config.json`?**~~

   Answer: recoomendation

   **Decided (you):** the guest sets it, the host only *asks*. `config.json`
   gives the power-on mode and the offered list; a resized window posts a
   preferred size; the kernel adopts it at the prompt (§5.5).

2. ~~**Where does the aperture live?**~~

   Answer: the thing is, i want to be able to upgrade to ram to lets say 1GB.
   but it still could live above right?

   **Reply: yes — and that turned out to be the more important answer, so the
   design changed.** It lives above, but it must not be the *literal*
   `0x08000000` I first wrote: the aperture is **wherever that machine's RAM
   ends**, computed in `RAM.__init__` from the `size` it was given. Three
   reasons, all checked rather than reasoned:

   - **RAM size is already per-instance.** `Machine(ram_size=…)` and
     `RAM(size)` take it as an argument, and the suite uses it:
     `tests/test_smoke.py` runs on `TEST_RAM = 0x10000` and on a 1024-byte
     RAM, `tests/test_bios2.py` on `SMALL_RAM = 1 << 18` *(checked)*. A module
     constant would be wrong for every one of those.
   - **1 GB works, and 2 GB is the ceiling.** Aperture at `0x40000000`, space
     2 GB, mask `0x7FFFFFFF` — all inside 32 bits. At 2 GB of RAM the mask is
     `0xFFFFFFFF`, which is the ceiling of a 32-bit machine anyway, not one
     this design imposes. The table is in §5.1.
   - **A machine with no VRAM keeps today's mask exactly.** Required, not
     tidy: `test_smoke.py:117` builds `RAM(1024)`, writes a word at 1022, and
     asserts both that it wraps and that the bytearray did not grow *(checked)*
     — a test pinning a real bug from `REFACTORING.md`. Doubling that
     machine's space would break it for a reason unrelated to graphics.

   **The one new hazard it creates,** now in §8: the guest must take the
   aperture base from `VRAM_INFO` and never from the predefined symbol. A
   `.bin` built on a 128 MB machine that hardcodes `0x08000000` runs fine
   there and writes into the middle of the heap on a 1 GB machine, silently,
   because that store succeeds. `disp_init()` is the only thing allowed to
   read it.

   **Decided (you):** derived from RAM size, RAM growable to 1 GB and beyond
   (§5.1). Whether `--ram` ships with this work is §11.

3. ~~**Is all of VRAM mapped, or a sliding window into it?**~~

   Answer: *(blank)*

   **Decided 2026-09-17 (left to me):** all of it, `VRAM_WINDOW` reserved and
   unimplemented (§5.1, §5.2). A windowing register costs something in every
   library function and buys nothing until VRAM is bigger than the aperture,
   which — now that the aperture is as big as RAM — it never will be.

4. ~~**Does `CH_DISPLAY` survive?**~~

   Answer: *(blank)*

   **Decided 2026-09-17 (left to me):** yes, unchanged, as a front for the
   scanout selector (§5.4). It is what keeps `user/*.asm`, every built `.bin`,
   `tests/golden/screen.bin` and a good fraction of 926 tests working;
   deprecated in the docs, deleted in a later tiny plan when nothing uses it.

5. ~~**Do `DISP_W`/`DISP_H` become runtime variables under the same names?**~~

   Answer: yes

   **Decided (you):** `#define DISP_W disp_w` (§5.6). All 22 files keep
   compiling; only the five that size arrays need editing, against new
   `DISPLAY_MAX_W`/`DISPLAY_MAX_H` caps.

6. ~~**What happens to a program that is running when the mode changes?**~~

   Answer: *(blank)*

   **Decided 2026-09-17 (left to me):** it does not change under it. Only the
   program that asked can change the mode and the kernel puts it back when
   that program ends — the rule `keepscreen` and the scanout base already
   follow *(checked: `k_tidy`)*. A `VEC_MODE` interrupt is a later option if
   you ever want a program to *react* rather than be protected.

7. ~~**The RGBA swizzle: host or client?**~~

   Answer: *(blank)*

   **Decided 2026-09-17 (left to me):** client, with a `format` field in
   `/info` so the two ends cannot disagree silently (§5.5). §4.4 measures the
   host-side swizzle at 19% of a core at 720p and 40% at 1080p, and both front
   ends are ours.

8. ~~**Does the GAC draw text?**~~

   Answer: yes

   **Decided (you):** `SET_FONT` + `TEXT`, with the guest's own 760-byte
   1-bit font uploaded once (§5.3). §4.3: without it a 1280 × 720 console
   redraw is about 34 seconds.

9. ~~**Alpha blending in the GAC?**~~

   Answer: i think now, it shouldnt be that hard

   **Reply: agreed, and it is in v1 — but the obvious implementation is 60×
   too slow, so the plan now names the implementation.** You are right about
   the writing: `dst = dst·(1−a) + src·a` is three lines. The cost is that a
   blend must *read* each destination byte before writing it, so it cannot be
   a slice assignment. Measured (§4.5), at 1280 × 720:

   | | |
   |---|---|
   | opaque fill today | 0.329 ms |
   | blended fill, naive per-pixel loop | **≈ 0.21 s** |
   | blended fill, three `bytes.translate` tables | **3.651 ms** |
   | constant-alpha blit, naive | ≈ 0.28 s |
   | constant-alpha blit, SWAR big-integer lanes | **18.5 ms** |

   So blending ships, done two ways, both pure Python with no new dependency:
   a **256-entry lookup table per channel** for every constant-colour
   operation (`FILL`, `FRAME`, `LINE`, `CIRCLE`, `DISC`, `TEXT`), because a
   fixed source colour makes the blend an affine byte-to-byte map; and a
   **SWAR big-integer multiply** for `BLIT_ALPHA` at one global alpha, which
   I verified against a per-pixel reference — the colour bytes match exactly.
   `a == 255` and `a == 0` short-circuit first, so **nothing that draws today
   gets one instruction slower.**

   **One piece is left out:** a blit honouring *per-pixel* source alpha. No
   table and no single multiply applies, and the naive loop is 0.28 s a
   screen. The flag is reserved, the command refuses it, and §9 says what the
   options are if you want soft-edged sprites later.

   **Decided (you):** blending in v1, as §5.3.1 specifies.

10. ~~**Damage rectangles now or later?**~~

    Answer: Recommendation

    **Decided (you):** later, step 8, but `DAMAGE` is reserved from the start
    and `RAM` sets `vram_dirty` from day one, so the write path is
    benchmarked once rather than twice (§5.5).

11. ~~**Synchronous GAC, or a command queue?**~~

    Answer: Recommendation

    **Decided (you):** synchronous, plus `BATCH` (§5.3). Blending arriving in
    v1 strengthens this: it is the one operation that reads the destination,
    so it is the one that could not be reordered against another touching the
    same pixels — free on a synchronous bus, a correctness problem on a queue.

12. ~~**How much VRAM, and what is the top mode?**~~

    Answer: Recommendation

    **Decided (you):** `"vram": "16M"`, top mode 1280 × 720, offered modes
    `[[192,108],[320,180],[640,360],[854,480],[1280,720]]` (§5.2, §5.5).

13. ~~**Channel numbers and names.**~~

    Answer: Recommendation

    **Decided (you):** `CH_VRAM = 9`, `CH_GAC = 10`.

14. ~~**Does `setmode` persist across a reboot?**~~

    Answer: Recommendation

    **Decided (you):** no. `config.json` gives the power-on mode;
    `/etc/display.conf` read by the kernel at boot is a later one-liner in
    `boot.conf`'s existing shape.

15. ~~**Should mouse coordinates carry the generation they were taken in?**~~

    Answer: Recommendation

    **Decided (you):** no. The front end recomputes its scale on a mode
    change; at worst one frame of clicks lands a pixel or two out.

---

## 11. Follow-up

~~One thing your Q2 answer raises that the plan cannot decide for you.~~

**Does `--ram` / a `ram` key in `config.json` ship with this work, or after
it?**

The design supports 1 GB from step 1 — the aperture rides on top of whatever
RAM it is given — but *exposing* a knob for it is a separate, small change
with one real consequence: `bytearray(1 GB)` per `Machine`, and the suite
constructs one in 29 places *(checked)*, several per test, in parallel under
`-n logical`. A default of 1 GB would be fatal; a flag that defaults to 128 MB
is harmless.

**Suggestion: ship the flag with step 1, defaulted to today's 128 MB,** so the
step that builds the derived aperture is also the step that can prove it on a
1 GB machine rather than only reasoning about it. Nothing in the memory map
below the top of RAM moves when RAM grows — `PROGRAM_LOAD_ADDR`, `HEAP_START`
and `DISPLAY_START` are all fixed low addresses, and only `STACK_TOP` is
derived from `RAM_SIZE` *(checked)* — so the change really is as small as it
sounds.

*Answer: okay do the suggestion*

**Decided (you), 2026-09-18:** the flag ships with Phase 1, defaulting to
128 MB. [Phase 1](phase1_aperture.md) steps 1.4 and 1.5.
