# Phase 7: `setmode`, and `# graphics` scripts that pick a mode

> Part of [the GAC plan](README.md). **Status: planned.** Needs Phase 6.
> Design: [design.md §6](design.md#6-what-has-to-change-file-by-file)'s
> "New programs". Decisions: Q14 in [decisions.md](decisions.md).

**The goal:** the end-to-end demo from [README.md §3](README.md#3-the-goal):

```sh
2:/> setmode 640 360
2:/> graphics -clear 0xFF101018 -disc 320 180 60 0xFFFF0000 -wait
```

---

## Steps

### 7.1 `/bin/setmode.bin`

`setmode` prints the current mode, `setmode 640 360` switches, and
`setmode -list` lists the offered modes. It goes on the OS disc through
`pigeon_compiler_init.txt`.

### 7.2 `graphics.bin` at any mode

The `graphics.bin` command draws through the GAC at whatever mode is
current, and its `-clear` and bounds follow `disp_w`/`disp_h`.

### 7.3 `# graphics` scripts that pick a mode

A script can switch at its top and gets the mode back when it ends (Q6).
`/docs/gui.pgs` is the natural demo to move up to a bigger screen, and
`BATCH` answers the slide-show redraw its comments complain about.

### 7.4 The manuals

`docs/gac.md` and `docs/vram.md` get written. `graphics.md` §1 and
`README.md`'s channel table get updated: **`0xAARRGGBB` now blends**, and
`0x80…` means "half there", not "half invisible" (§8).

### 7.5 Tests

`setmode` through the shell, a `# graphics` script at 640 × 360, and the
mode restored afterwards.

**Done when:** the demo above runs in the browser, and the full suite
passes.

---

## As built

*(filled in when the phase is done)*
