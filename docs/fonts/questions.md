# Font questions, and what was decided

> Part of [the font plan](README.md). **Status: every question is answered and
> folded in, 2026-09-20.** Your answers are kept verbatim under `Answer:`,
> with the decision that follows from each. Blank means the recommendation
> stood.

---

## 1. First pass: the device and the slots

Answer inline under each; the recommendation stands where you leave it blank.

1. **A slot argument on commands 9 and 10, or new commands 17 and 18?**
   §4.1 has both. **Recommendation: new commands** — nothing old changes
   shape, and a guest built before the feature gets "no such command" rather
   than a silent argument-length mismatch.

   **Decided (you):** the recommendation — new commands 17 and 18.

2. **How many font slots?** **Recommendation: 8**, with slot 0 reserved for
   the console's font so `disp_init()` keeps its current meaning.

   **Decided (you):** the recommendation — 8, slot 0 the console's.

3. **Should the device cap total font memory across slots?** Today the only
   bound is `cell_w * cell_h <= 4096` per font, so eight worst-case fonts
   would be ~32 MB on the host. **Recommendation: yes,** a total-bytes cap
   that refuses the upload rather than a slot count that pretends to be one.

   **Decided (you):** the recommendation — a total-bytes cap. §8 shows why
   this matters more than expected: a 16 × 32 font is 344 KB on the host.

4. **Should the built-in 5 × 7 also become a `.pf` file on the disc?**
   **Recommendation: no.** It must work with no disc and no kernel — bios2
   and the installer draw with it *(checked)* — so it stays compiled in, and
   `.pf` is for everything else.

   **Decided (you):** the recommendation — no, it stays compiled in.

5. **What should the GUI's default font be?** **Recommendation: 8 × 16 on a
   9 × 18 cell**, the largest the device takes today, with the 5 × 7 kept as
   the "small" font for dense lists. A GUI that wants bigger waits for F4.

   **Decided (you):** the recommendation — **but [§8](#8-verified-what-each-size-actually-gives-you)
   revises it.** Measuring what 8 × 16 actually gives (142 columns at 720p)
   showed it is better than today and still about 40% denser than a normal
   terminal, so F4 moved up the plan and the default becomes **12 × 24** once
   it lands. 8 × 16 remains the F1 stopgap that needs no device change.
   [§10 Q5](#10-second-pass-questions) re-asks it with the numbers.


---

---

## 2. Second pass: sizes, antialiasing, real fonts

Raised by §8 and §9. Same rule: blank means the recommendation.

1. **Does `.pf` store 1-bit or 8-bit coverage?** 8-bit is 8× the file and the
   host memory (a 12 × 24 font goes from ~27 KB to ~214 KB on the host) and is
   the difference between "bigger" and "clean" (§9.2).
   **Recommendation: 8-bit coverage, with 1-bit as a flag in the header** —
   so the built-in 5 × 7 and any hand-drawn font stay cheap, and a rasterised
   TTF keeps its antialiasing.

   Answer: 

   **Decided (you):** blank, so the recommendation — **8-bit coverage, with a 1-bit flag in the header**, so a hand-drawn font stays cheap and a rasterised face keeps its antialiasing.

2. **Does antialiased `TEXT` go in, and when?** It needs the coverage format
   (Q1) and a new draw path in the device, and it is what makes text look
   modern. **Recommendation: yes, as its own phase F5, straight after F4** —
   F4 makes it big, F5 makes it clean, and neither is much use without the
   other.

   Answer:

   **Decided (you):** blank, so the recommendation — **yes, as phase F5**, after F4. [§3 Q2](#3-third-pass-what-your-answers-raised) asks you to confirm that order, since it is now the main visual win.

3. **What happens to antialiased text over an unknown background?** The
   table trick needs a known background (§9.2). **Recommendation: fall back
   to 1-bit for that call** — silently correct and fast, rather than slow or
   refused. The alternative is a slow per-pixel path behind a flag.

   Answer:

   **Decided (you):** blank, so the recommendation — **fall back to 1-bit** for text over an unknown background. Silently correct and fast.

4. **Is `pygame` allowed to be a build dependency?** `tools/make_font.py`
   would need it to read a TTF, and today pygame is only needed by the
   optional display client — the emulator, the tests and the toolchain need
   none of it *(checked: `requirements.txt`)*.
   **Recommendation: yes, but only for that tool** — it fails with a clear
   message telling you to `pip install pygame`, the generated `.pf` files are
   committed to the repo, and nobody who is not making a new font ever needs
   it.

   Answer: yeah, it is fine

   **Decided (you):** *"yeah, it is fine"* — `pygame` may be a build dependency of `tools/make_font.py` only. The emulator, the tests and the toolchain still need none of it, the generated `.pf` files are committed, and the tool fails with a clear message if pygame is missing.

5. **What is the default font, given §8?** **Recommendation: 12 × 24 on a
   13 × 26 cell (98 × 27 at 720p) once F4 lands**, with 8 × 16 as the F1
   stopgap and 5 × 7 kept for dense lists. Which real face to rasterise is a
   taste question — DejaVu Sans Mono is on the machine and is a reasonable
   default.

   Answer: but why are we thinking of cells and pixels? are we doing svg style?

   **Decided (you): this is your counter-question, and it has a file of its own —
   [vector.md](vector.md).** Short answer: **yes, we use real vector typefaces**,
   rasterised on the host, which is what every system does — they just do it at
   run time and cache the result, and a cache of rasterised glyphs *is* a bitmap
   font. Pixels are not a choice, because the screen is pixels.

   What *is* a choice, and what I assumed without asking, is **cells**: a cell
   means every character is the same width. That is right for a terminal and
   wrong for a GUI. [§3 Q1](#3-third-pass-what-your-answers-raised) asks whether
   you want proportional fonts as well.

   And the thing that actually makes text look old is neither of those: it is
   that **every pixel is currently 1-bit**, with no grey. That is F5.

   The default font stands at **12 × 24 from DejaVu Sans Mono**, monospaced,
   once F4 lands.

6. **Should the phases reorder, so F4 comes before the GUI?** §8 says
   comfortable text needs glyphs over 8 px, which is F4.
   **Recommendation: yes — F1, F2, F3, F4, F5, then G1.** It front-loads all
   the font work, which is the thing you actually complained about, and the
   GUI then starts on a machine whose text already looks right.

   Answer: first, get the font system working, i think we should move the whole font documetation into a seprate folder. and that neeeds to be built first

   **Decided (you):** *"first, get the font system working, i think we should move
   the whole font documentation into a separate folder. and that needs to be built
   first"* — both done. **The fonts are now their own plan, [docs/fonts/](README.md)**,
   no longer a file inside the GUI plan, and **they are built first**: F1 to F5
   before any GUI phase. The GUI plan depends on this one, not the other way
   round.

---

## 3. Third pass: what your answers raised

Answer inline; blank means the recommendation.

1. **Do you want proportional fonts at all?**
   Your counter-question — *"why are we thinking of cells and pixels?"* — is
   answered in [vector.md](vector.md), and the part of it that is genuinely
   undecided is this. A "cell" means every character is the same width, which
   is a terminal. Modern UI text is proportional: `i` narrow, `W` wide.

   It is not expensive — a 95-byte advance table on the font, a prefix sum in
   the device instead of `i * cell_w`, and a `disp_text_w()` the GUI wants
   anyway ([vector.md §3](vector.md#3-cells-is-the-real-choice-and-it-is-still-open)).
   But it **breaks the row-composition idiom** three programs use, so those
   would keep a monospaced font.

   **Recommendation: both, chosen per font, as phase F6** — monospaced for
   the console and anything with columns, proportional for the GUI's own
   chrome. If "everything looks like a terminal" suits you, say so and F6
   never happens; nothing else depends on it.

   Answer:

   **Decided (you):** the recommendation — **both, chosen per font, as phase F6.**
   The console and anything with columns keeps a monospaced font; the GUI's own
   chrome gets a proportional one. A `.pf` says which it is, and `TEXT` follows
   the font rather than a global setting.

2. **Which comes first, F4 (wide glyphs) or F5 (antialiasing)?**
   [vector.md §2](vector.md#2-pixels-in-the-sense-you-probably-meant-1-bit-vs-coverage)
   ranks antialiasing as the bigger visual win, but F4 is the one that gets
   you from 142 columns to 98.

   **Recommendation: F4 then F5**, because size is what you actually
   complained about and antialiasing on a 5 × 7 glyph helps very little —
   there is not enough edge for grey to do much. On a 12 × 24 it transforms
   it.

   Answer:

   **Decided (you):** the recommendation — **F4 then F5.** Size is what you
   complained about, and antialiasing a 5 × 7 glyph barely helps: there is not
   enough edge for grey to do anything with. On a 12 × 24 it transforms it.
