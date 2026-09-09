# Emulator performance — a plan

> **Steps 0–3 are done.** Measured end to end on the same program with the
> same harness: **1,306,245 → 3,010,111 IPS, a 2.30× speedup**, with the
> instruction count identical before and after (1,530,743) and all 281
> tests green. That beat the 1.59× this plan predicted for these steps —
> see [Results](#results-steps-0-3) at the end for why.
>
> **Steps 4 and 5 are closed — PyPy superseded both.** The afternoon's
> experiment recommended at the bottom of this file was worth it:
> `.pypy/bin/pypy3` runs the unmodified interpreter at **36.1M IPS on the
> cube (12.7×) and 21.7M on demo (8.3×)**, against 2.05× for the
> hand-written closure cache step 5 proposed — and it changes no code, so
> it cannot be subtly wrong. See [PyPy](#pypy-closes-steps-4-and-5).
>
> Separately, the real win turned out not to be interpreter speed at all.
> 87% of the cube's instructions were `disp_clear` and `disp_present`
> looping over every pixel; those are now hardware (`CH_DISPLAY`), which
> took the cube from **832,403 to 85,447 instructions per frame** — 9.7×
> less work to draw the same picture, and it compounds with PyPy instead
> of competing with it.

Every number here was measured on this machine, running `build/demo.bin`
(the compiled C demo) through the full `Machine` path — real mixed code,
not a synthetic loop.

## First, a correction

`REFACTORING.md` quotes **2,526,000 IPS**. That is a tight `ADD`/`CMP`/`JL`
loop with no memory traffic and no IO. Real code does not look like that:

| | IPS | of bare |
|---|---|---|
| bare `CPU.run()` loop, synthetic | 2,221,000 | 100% |
| + `Machine.step()` | 1,853,000 | 83% |
| **+ a real program (`demo.bin`)** | **1,259,000** | **57%** |

So ~1.26M is the honest figure, and it matches what you are seeing. The
benchmark in `tools/bench.py` should be reporting the real one; that is
task 0 below.

---

## Where the time actually goes

`cProfile`, 600,000 instructions of `demo.bin`:

| Function | tottime | calls | note |
|---|---|---|---|
| `cpu.run` | 0.339 s | 600,000 | fetch + dispatch |
| **`registers._resolve`** | **0.220 s** | **1,005,319** | 1.67 calls per instruction |
| `machine.step` | 0.163 s | 600,000 | wrapper doing one `if` |
| `registers.read` | 0.149 s | 563,919 | |
| `registers.write` | 0.140 s | 441,400 | |
| `reg_or_imm` | 0.107 s | 478,792 | a call to pick one of two values |
| `isinstance` | 0.102 s | 1,005,319 | inside `_resolve` |
| `ram.write_word` / `read_word` | 0.146 s | 244,247 | |

**Register access is ~33% of total runtime** — `_resolve` + `isinstance` +
`read` + `write` = 0.61 s of 1.87 s. `_resolve` exists to accept `'A'` as
well as `0`, and the CPU **only ever passes integers**. That check runs a
million times to support a convenience the hot path never uses.

---

## The plan

Ordered by payoff per unit of risk. Each was prototyped and measured, and
the cumulative figures come from running them together.

### 0. ✅ Make the benchmark honest — no speedup, prevents self-deception

`tools/bench.py` reported only the synthetic loop. It now prints both, and
skips the BIOS when timing the real program — the BIOS waits two *real*
seconds on the timer, so including it measures the wall clock rather than
the interpreter. That mistake cost me a confusing measurement mid-way
through this work: 70% of what I was timing was the BIOS spin.

### 1. ✅ DONE — Handlers index the register list directly

*1,382,000 → 1,913,000 IPS*

```python
# now
def op_add(cpu, dst, src1, src2, imm):
    a = cpu.reg.read(src1)                 # -> _resolve -> isinstance
    b = reg_or_imm(cpu, src2, imm)         # -> another call, another _resolve
    cpu.reg.write(dst, a + b)              # -> _resolve again

# after
def op_add(cpu, dst, src1, src2, imm):
    v = cpu.reg.values
    v[dst] = (v[src1] + (imm if src2 == NONE_REG else v[src2])) & 0xFFFFFFFF
```

`registers.py` already documents `values` as public for exactly this. Keep
`read()`/`write()` for everything outside the hot path — the debugger, the
tests, `__repr__` — where the letter-name convenience is worth having.

Bounds are still enforced: an out-of-range index raises `IndexError`
immediately, which is the same failure `_resolve` produced, just without
paying for the check a million times a second.

Apply to the ~12 hot handlers (`MOV ADD SUB MUL DIV AND OR XOR CMP MR MW
MRW MWW`); the rest can stay as they are.

**Risk: low.** Pure local rewrites, each covered by `tests/test_smoke.py`.

### 2. ✅ DONE — Fuse the run loop

*1,913,000 → 2,195,000 IPS*

`Machine.step()` is a method call per instruction that does one `if`. Inline
it into `Machine.run()`:

```python
cpu, ram, controller = self.cpu, self.ram, self.io_controller
mem, handlers, unpack = ram.mem, _HANDLERS, _UNPACK
while True:
    if cpu.halted: break
    pc = cpu.pc
    opcode, dst, src1, src2, imm = unpack(mem, pc)
    cpu.pc = pc + INSTR_SIZE
    handlers[opcode](cpu, dst, src1, src2, imm)
    if ram.io_pending: controller.update()
```

Keep `step()` as it is — the debugger and every test use it. This is about
`run()` not paying for the abstraction 2 million times a second.

**Risk: low.** Two code paths to keep in agreement; the suite covers both.

*Done.* One thing the prototype hid: the real loop also did
`self.instruction_count += 1` and `self.total_instructions += 1` every
instruction, and two attribute round-trips through the instance dict cost
more than most handlers. Counting in a local and writing back at each
sample point took this from 1.05× to **1.31×**. The partial window at the
end is added back on exit, so the totals stay exact — a test asserts the
instruction count is identical through `step()` and `run()`.

### 3. ✅ DONE — `struct` for word access

`RAM.read_word` / `write_word` are on the `MRW`/`MWW` path:

| | ops/s | |
|---|---|---|
| `int.from_bytes(mem[a:a+4], "little")` | 4,767,000 | |
| `struct.unpack_from("<I", mem, a)` | 9,040,000 | **1.90×** |
| `mem[a:a+4] = v.to_bytes(4, "little")` | 4,836,000 | |
| `struct.pack_into("<I", mem, a, v)` | 11,186,000 | **2.31×** |

Keep the wrap-around branch for accesses that straddle the top of memory —
that fix in `REFACTORING.md` §1.1 stays. Only the common in-range path
changes.

**Risk: low.** `tests/test_smoke.py` already pins the wrapping behaviour.

### 4. Decoded-instruction cache — **1.83×** cumulative ✅ measured

*2,195,000 → 2,528,000 IPS*

Decode each PC once, keep `(handler, dst, src1, src2, imm)` in a dict:

```python
entry = cache.get(pc)
if entry is None:
    entry = cache[pc] = (handlers[op], dst, src1, src2, imm)
handler, dst, src1, src2, imm = entry
```

This removes the `unpack_from` and the table index from every repeat
execution — and loops execute the same addresses thousands of times.

**Risk: medium — this is the first one that can be *wrong* rather than
just slow.** The cache must be invalidated when the underlying bytes
change, and on this machine they do: the BIOS DMAs the program into RAM at
`0x20000` and then jumps into it. Guest code could also write over itself
(`user/checkerboard.asm` does exactly that, by accident).

Invalidate on any write below `HEAP_START` — one comparison in
`write_byte`/`write_word`, and `load_bytes` clears the cache outright.
Writes to that region are rare after boot, so a blunt full flush is fine
and much easier to be sure of than tracking ranges.

`tests/test_loader.py` boots a program the BIOS wrote and runs it, so a
missing invalidation shows up there rather than in the field.

### 5. Specialised closures — **2.01×** ✅ measured

*1,357,000 → 2,721,000 IPS*

Instead of caching operands, compile each PC into a closure with the
operand *mode* already resolved — no `NONE_REG` test, no operand fetch, no
argument passing:

```python
if opcode == OP_ADD:
    if src2 == NONE_REG:
        def run(): v[dst] = (v[src1] + imm) & MASK; cpu.pc = nxt
    else:
        def run(): v[dst] = (v[src1] + v[src2]) & MASK; cpu.pc = nxt
```

The prototype specialised only 9 opcodes — everything else fell back to the
generic handler — and still reached **2.01×**. Full coverage would go
further.

It subsumes #4 (same invalidation requirement, same cache) and largely
subsumes #1 and #2. But it is a real rewrite of the execution core, and it
puts the ISA's semantics in two places at once: the readable handler in
`instruction_set.py` and the specialised closure. Those can drift, and a
drift is a wrong answer, not a crash.

**Do #1–#4 first.** They are ~1.8× for a few days of low-risk edits.
Then decide whether the last 0.2× is worth the structural cost — and if it
is, generate the closures *from* the handler table rather than hand-writing
them twice.

---

## Results, steps 0-3

Measured with the same program and harness before and after, BIOS excluded:

| | IPS | vs before |
|---|---|---|
| before (git HEAD) | 1,306,245 | 1.00× |
| **after steps 1–3** | **3,010,111** | **2.30×** |

Identical instruction counts (1,530,743) either side, so the work done is
the same. All 281 tests green at every step.

**That is well above the 1.59× predicted.** Two reasons, and the second is
the more interesting one:

1. The prototypes measured each change against the *unoptimised* baseline
   in isolation. Composed, they multiply rather than add — removing the
   register overhead makes the dispatch a larger share of what is left, so
   fusing the loop then wins more than it did alone.
2. Step 3 was worth far more than the "low risk, modest win" I had it down
   as. `struct` for word access took 2.39M → 2.89M on its own (**1.21×**),
   because a compiled C program is much heavier on `MRW`/`MWW` than the
   arithmetic loop I profiled against. The instruction *mix* matters as
   much as the per-instruction cost.

Remaining, still open:

| Step | est. IPS | vs now |
|---|---|---|
| 4. decoded cache | ~3.5M | ~1.2× |
| 5. specialised closures | ~4.0M | ~1.3× |

Those estimates are extrapolated from the earlier prototypes and have not
been re-measured on top of steps 1–3; the real gain is likely smaller,
since both target overhead that step 1 already removed a chunk of.

---

## What not to do

**Do not optimise the display path.** `_convert_to_rgba` is already 130×
faster than it was (2.22 ms → 0.017 ms/frame) and runs 30 times a second.
It is not on the critical path any more.

**Do not chase the synthetic benchmark.** It flatters every change that
helps `ADD`/`CMP`/`JL` and ignores memory traffic, which is where real
programs spend their time.

**Do not add a JIT.** Generating Python source and `exec`ing it per basic
block is the next rung, and it would help — but the machine is an
interpreter for a hobby ISA, and the debuggability you would spend is worth
more than the 2× you would gain.

**PyPy was worth an afternoon's experiment**, and it paid — see below.

---

## PyPy closes steps 4 and 5

Measured after a 5 s warm-up, 5 s window, through `Machine.run()`:

| | CPython 3.13 | PyPy 7.3.23 | |
|---|---|---|---|
| synthetic ADD/CMP/JL | 3,555,910 | 92,032,800 | 25.9× |
| **cube.bin** | **2,851,966** | **36,081,024** | **12.7×** |
| demo.bin | 2,607,862 | 21,658,242 | 8.3× |

Run it with `./run-pypy.sh cube --run`. Two things to know:

**Warm-up is not optional when measuring.** `tools/bench.py`'s 0.4 s window
closes before the JIT has done anything, and reports PyPy as *slower*
(1.5M IPS). The IPS readout climbs ~7M → ~40M over about ten seconds.

**IPS is the wrong metric now, and it was already misleading.** The
codegen change that made every local access one instruction shorter
*lowered* reported IPS (2.67M → 2.54M) while *raising* the frame rate
(3.00 → 3.99 FPS), because the cheap `MOV`s went away and the remaining
mix is denser in `MRW`/`MWW`. Measure instructions per frame, or frames
per second. Not instructions per second.

So step 5 (specialised closures) is **not worth doing**: 2.05× measured,
for a rewrite of the execution core that puts the ISA's semantics in two
places where a drift is a wrong answer rather than a crash. PyPy is 12.7×
for nothing. Step 4 (decoded cache) is subsumed.

What is still open, and now matters more than either: under PyPy,
`Machine.run()`'s own per-instruction bookkeeping — the countdown, the
clock sample, the `io_pending` check — is roughly 45% of runtime (a lean
loop over the same handlers reaches 66M IPS against `run()`'s 36M). That
was noise on CPython. It is also a much safer target than a closure cache.

---

## Verification

Correctness first, at every step — the suite is the whole reason this is
safe to attempt:

```bash
python3 -m pytest tests/ -q          # all 281 must stay green
python3 tests/test_golden.py         # the assembler is byte-exact
python3 tests/test_loader.py         # the BIOS writes code, then runs it
python3 tools/bench.py               # before and after, both figures
```

The tests that matter most for this work:

- `test_smoke.py` — one per instruction, so a rewritten handler that gets
  the semantics subtly wrong fails immediately.
- `test_loader.py` — boots a program the BIOS wrote into RAM, which is the
  exact case a stale decode cache breaks.
- `test_libs.py` / `test_compiler.py` — 85 programs compiled and *run*; a
  broken flag or a wrong wrap shows up as a wrong answer.

Measure after each step, not at the end. If a change does not move the
real-program number, revert it — an optimisation that costs readability and
buys nothing is a straight loss.
