# The `.pf` font file, and the tool that writes it

> Part of [the font plan](README.md). Facts marked *(checked)* were read in the code on 2026-09-20, and *(measured)* ones were run.

## 1. Where fonts come from

There is no font file format on this machine and no font on the disc — the
5 × 7 is a C array compiled into `display.c` *(checked)*. A GUI needs at least
one more, and the user should be able to add their own, so:

- **A `.pf` font file**, the simplest thing that can work: a small header
  (magic, glyph_w, glyph_h, cell_w, cell_h, first, count) then
  `count * glyph_h * bpr` bytes, exactly the layout `SET_FONT` already wants
  in RAM. Loading is `fs_load_alloc` then one `SET_FONT` at that address — no
  decoding pass at all, which is the point of matching the layout.
- **`tools/make_font.py`** to write them, the way `tools/make_badge.py`
  writes the badge, so a font can be edited as readable text rather than a
  binary blob.
- **`/etc/font/` on the disc** with at least `mono8x16.pf`, and the 5 × 7 kept
  in `display.c` as the built-in that needs no disc.

[questions.md §1 Q4](questions.md) asks whether the built-in should *also* become a file.

---

## 2. Taking a real font off your PC: yes, and with no new dependency

You asked if you could load a real font from your PC and have it turned into
a `.pf`. **Yes — and the tooling is already installed.**

`pygame.freetype` is part of `requirements-client.txt` for the pygame front
end *(checked)*, and it rasterises TrueType and OpenType with full
antialiasing. Verified on this machine *(measured, 2026-09-20)*:

| requested size | advance | distinct coverage levels |
|---|---|---|
| 10 | 6 px | 79 |
| 13 | 8 px | 103 |
| 16 | 9 px | 136 |
| **20** | **12 px** | **146** |
| 24 | 13 px | 156 |

— from `/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf`, one of **26**
TrueType faces already on this machine. Note that size 20 gives a 12-pixel
advance, which is exactly the 98-column cell §8 recommends.

So `tools/make_font.py` becomes:

```sh
python3 tools/make_font.py --ttf /usr/share/fonts/.../DejaVuSansMono.ttf \
        --size 20 --first 0x20 --count 95 -o user/os/etc/font/mono12x24.pf
```

It rasterises each glyph to a coverage bitmap, checks the face is monospaced
(or takes the widest advance and warns), and writes the `.pf`. **No new
dependency**, because the one it needs is already there for the display
client — though it would become a dependency of the *build* rather than of
the emulator, which [questions.md §2 Q4](questions.md) asks about.

And the same tool grows a `--draw` mode for authoring a font by hand, which
is what you asked for separately: glyphs as text, `#` for ink, edited in
`edit.c` and compiled to `.pf`.

---
