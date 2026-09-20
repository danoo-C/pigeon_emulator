# pigeon-cc: the eight things it got silently wrong

> **Status: BUILT, 2026-09-20.** All six phases landed the day this was
> written, and then **C7 and C8** — the two things §9 found on the way.
> §8's questions were answered with their recommendations and §9 records
> what it all cost. Every row in §1 was **compiled and executed on the
> emulator** — *(run)* means the program ran and its `A` was read — and each
> is now a test in `tests/test_compiler.py` asserting the right answer.
> Facts marked *(checked)* were read in the code.
>
> This came out of checking [the font plan](fonts/build.md) and
> [the GUI plan](gui/README.md) against the machine. Both are written *around*
> these bugs — [gui/constraints.md](gui/constraints.md) is mostly a list of
> ways to avoid them — and both say the same thing in passing: **a rejected
> program is a nuisance, a silently wrong one is a trap.** This is the plan
> for that.

---

## 1. What is wrong

Six behaviours. **None of them warns. All of them compile.**

| # | written | should be | actually is | |
|---|---|---|---|---|
| **1** | `struct P y; y = x;` then `y.c` | 3 | **9** — the old value | §2 |
| **2** | `int f(struct P p)` called `f(q)`, then `p.c` | 3 | **0** | §2 |
| **3** | `struct P mk(void)` returning a struct, then `.c` | 3 | **0** | §2 |
| **4** | `int a = 2*3+1;` at file scope | 7 | **0** | §3 |
| **5** | `int a = -1;` at file scope | −1 | **0** | §3 |
| **6** | `int a = sizeof(struct P);` at file scope | 8 | **0** | §3 |
| **7** | `int v = -20; v / 2` | −10 | **2147483638** | §5 |
| **8** | `int a = -7; a % 3` | −1 | **0** | §5 |
| **9** | `int a = -8; a >> 1` | −4 | **2147483644** | §4 |

*(run, all nine.)*

**The ones that are fine** and should stay that way: a brace initialiser
*does* fold — `int t[] = {2*3+1, -1};` gives 7 and −1 *(run)* — `char *p =
"hi"` works, `unsigned a = 0x80000000` works, and signed **comparison** is
correct, because `_compare` biases both sides by the sign bit before an
unsigned `CMP` *(checked: `codegen.py:368-384`)*.

That last one is the shape of this whole plan. **The compiler already knows
how to give an unsigned machine signed semantics; it was done for `<` and not
for `/`, `%` or `>>`.**

And the things that error cleanly — `enum`, `switch`, `static` locals,
self-referential structs, `goto`, floating point *(run)* — are **not in this
plan**. A missing feature you are told about is a design decision. §7.

---

## 2. Structs are moved four bytes at a time

There is **no struct copy anywhere in code generation**, which is why the
same bug shows up three times:

- **Assignment** goes through `_store_to`, which picks its instruction from
  `_store_op(type)` — `MW` for a one-byte type and `MWW`, one 32-bit word,
  for everything else *(checked: `codegen.py:697-712`, `_store_op`)*. A
  12-byte struct is stored as its first word.
- **Arguments** go to the callee's frame one word per slot — `slot(index)`
  writes exactly `MWW` *(checked: `codegen.py:602-612`)*, and the ABI is a
  word per parameter, at most eight *(checked: `typesys.py:128`)*.
- **Returns** come back in `A`, one word.

The analyzer does not object: `_x_Assign` rejects assignment to an array but
says nothing about a struct *(checked: `analyzer.py:377-386`)*.

**The fix, in two parts.**

**(a) Reject what stays unsupported, loudly.** By-value parameters and
by-value returns are an ABI change — several words a slot, a hidden pointer
for the return, and the eight-argument limit reinterpreted — for a feature
**nothing in the tree uses or wants**: `math.h` takes `vec3 *`, `bmp.c` takes
`bmp_header *out`, `fs.c` is pointers throughout, and the GUI library is
designed to be *(checked)*. So they become errors:

```
    t.c:4:12: a struct cannot be passed by value; take a pointer instead
    t.c:9:1:  a struct cannot be returned by value; fill one through a pointer
```

**(b) Make assignment work,** because `y = x` is what people write and there
is nothing to gain by refusing it. It is a bounded change: at
`_x_Assign`, when the type is a struct, emit the address of each side and an
inline word loop — the same shape `memcpy` compiles to. The one restriction
is that **a struct assignment is a statement, not a value**: `a = b = c` on
structs errors, because the expression model keeps its value in `A` and a
struct does not fit there.

> **Recommendation: (a) first, (b) second, as separate steps.** (a) is small
> and turns three traps into three sentences on the same day. (b) is the real
> fix for the one of the three that is worth having, and it removes its own
> diagnostic when it lands.

---

## 3. A global initialiser that is not a bare literal becomes zero

`_globals` emits `.word` for an `A.IntLiteral`, `.asciz` for a string and a
brace list element by element — **and `.space` for everything else**
*(checked: `codegen.py:176-202`)*. `.space` is zero. Nothing is reported.

So `int a = 2*3+1;`, `int a = -1;` and `int a = sizeof(struct P);` are all 0,
and so is `#define N (4*2)` `int a = N;` — which is the nastiest of them,
because the expression is invisible at the point of the bug *(run)*.

**The fix is already written, one function away.** `_init_list` folds every
brace-initialiser element through `parser._fold_constant` and **errors** when
it cannot *(checked: `analyzer.py:142-170`)*, which is exactly why
`int t[] = {2*3+1, -1};` is right while `int a = 2*3+1;` is wrong. The
scalar path simply never learnt it.

So:

1. Fold a scalar global's initialiser the way an array element's is folded.
2. Extend `_fold_constant` to the cases a global will meet: `sizeof`, a cast
   of a constant, and a character literal. It already does `+ - * / % << >>
   & | ^ ~ !` and unary minus *(checked: `parser.py`, `_fold_constant`)*.
3. **Error on anything not foldable**, with the message `_init_list` already
   uses — `a global initialiser must be a compile-time constant`. That is
   what catches `int b = a;`, which is not a constant in C either and is
   silently 0 today *(run)*.

**Size: small, and mostly deletion of a special case.** This is the single
highest value-per-line item in the plan, because a wrong constant is a bug
that reads as correct code forever.

---

## 4. `>>` on a signed value shifts zeros in

`>>` lowers straight to `SHR`, which is a logical shift *(checked:
`codegen.py:29-30, 518`; `instruction_set.py:439-449`)*. There is no `SAR` in
the instruction set *(checked)*, so the sign has to be put back by hand.

`math.c` has known this all along and says so: *"The only broken primitive is
the SHIFT … Everything signed funnels through `ishr()`"* *(checked:
`lib/pigeon/math.c:5-11`)*.

```c
int ishr(int x, int n) {
    if (x >= 0) return x >> n;
    return ~((~x) >> n);          /* proven, and already tested */
}
```

**The fix:** when the operand type is signed, emit the arithmetic shift
instead of the bare `SHR`. Branchless, three extra instructions:

```
    mask   = 0 - (x >>> 31)          ; all ones if negative, else 0
    result = (x >>> n) | (mask << (32 - n))
```

and `n = 0` needs no special case, because **`SHL` by 32 or more yields 0 on
this machine** *(checked: `instruction_set.py:427-437`)*.

`codegen` already has the signedness it needs at the comparison sites via
`node.operand_type` *(checked: `codegen.py:375`)*; the analyzer attaches the
same field for `>>` and `/`.

---

## 5. `/` and `%` are the machine's unsigned divide

`DIV` is `v[src1] // b` on unsigned words *(checked:
`instruction_set.py:156-170`)* and `/` lowers straight to it; `%` is
`a - (a / b) * b` over the same *(checked: `codegen.py:509-518`)*. So every
signed division with a negative operand is wrong, in the loudest possible way
— `-20 / 2` is 2,147,483,638 *(run)*.

**And the language design claims a diagnostic that does not exist.**
[design/02-language.md](../compiler/design/02-language.md) says *"The
compiler emits a diagnostic when it can see a signed division whose operands
may be negative"*. There is none, for any of the cases *(run)*. That sentence
is itself a bug: it is the reason someone would not check.

**The fix, again already written**, in `math.c`:

```c
int idiv(int a, int b) {                  /* and imod(a, b) = a - idiv(a,b)*b */
    int negative = 0;
    if (b == 0) return 0;
    if (a < 0) { a = -a; negative = 1; }
    if (b < 0) { b = -b; negative = !negative; }
    return negative ? -(a / b) : a / b;
}
```

`imod`'s form gives C's truncating remainder, so `-7 % 3` is −1 *(checked:
`math.c:46-61`)*.

Emitted as a **helper the code generator plants once and calls**, not inline:
it is a dozen instructions with a branch, `/` appears all over the tree, and
there is no linker to fold copies. `__divsi3` by name, as everyone else's
compiler calls it, emitted only if something used it.

> **Unsigned operands keep the single `DIV`.** This costs nothing where the
> types say it is safe, which is most of the hot arithmetic on this machine —
> including [the GUI's scaling](gui/scaling.md), which casts to `unsigned`
> deliberately and stays exactly as fast.

---

## 6. Why the existing tree does not break

Worth stating plainly, because "fixing arithmetic" sounds like it should
shake everything loose. It does not:

- **`ishr` stays correct.** For `x < 0` it computes `~((~x) >> n)` on a
  *non-negative* value `~x`, where arithmetic and logical shift agree. The
  fix changes nothing it does.
- **`idiv` stays correct.** It makes both operands non-negative before
  dividing, which is the case the old `DIV` already got right.
- **Anything that was right was right through non-negative operands**, and
  those paths are byte-identical after the fix.
- **What changes is the code that was quietly wrong** — which is the point.

The risk is not breakage but *drift*: some program may have been written to
match the broken behaviour without saying so. That is what §7's test pass is
for, and the tree is unusually well placed to find it — 43 test modules,
every one of which compiles C and **runs** it, plus `test_golden.py` pinning
the assembler byte for byte *(checked)*.

---

## 7. The phases

Each one is independently shippable, and each leaves the compiler stricter or
more correct than it found it.

| | what | size | § | |
|---|---|---|---|---|
| **C1** | **Diagnostics**: struct by value as a parameter or a return; a global initialiser that is not constant. Errors with a fix in the message | small | §2, §3 | ✅ |
| **C2** | **Global initialisers fold** — scalars through the folder, extended for `sizeof`, casts, `?:` and addresses | small | §3 | ✅ |
| **C3** | **Signed `>>`** | small | §4 | ✅ |
| **C4** | **Signed `/` and `%`** via a planted `__divsi3`, unsigned untouched | medium | §5 | ✅ |
| **C5** | **Struct assignment** copies the whole struct; `z = (y = x)` still errors | medium | §2 | ✅ |
| **C6** | **The documents**: `design/02-language.md`'s phantom diagnostic, `design/03-abi.md`, [gui/constraints.md](gui/constraints.md) §1, and `math.c`/`math.h`, whose opening comments were about these bugs | small | §5 | ✅ |
| **C7** | **`char` sign-extends on load**, and a cast to `char` narrows *and* extends | small | §9 | ✅ |
| **C8** | **`char *p = "hi";` is a pointer**, not a 3-byte array | small | §9 | ✅ |

**Order: C1, C2, C3, C4, C5, C6.** C1 costs a day and converts every trap in
§1 into a message; C2 is the largest correctness win for the least code; C3
and C4 are the arithmetic; C5 is the only one that adds a feature rather than
removing a lie.

### Tests

Every phase adds cases to `tests/test_compiler.py`, which compiles, links,
loads at `PROGRAM_LOAD_ADDR` and runs to `HALT`, so a pass means the whole
chain works *(checked: its docstring)*. The cases are §1's table verbatim —
each row a test asserting the **right** answer, which today would fail.
Errors get their own cases, asserting the message, the way the existing
"unsupported" tests do.

**And each of C3, C4 and C5 runs the whole suite before it lands**, including
`test_golden.py`: if the assembler's bytes move, something is wrong with the
change and not with the fixture.

---

## 8. Questions

Answer inline; **the recommendation stands where you leave it blank.**

> **All five answered with the recommendation, 2026-09-20**, and built.

### Q1. Does struct assignment get implemented, or just diagnosed?

§2. C1 makes `y = x` an error; C5 makes it work.

- **(a)** Both, as planned — diagnose in C1, implement in C5.
- **(b)** Diagnose only. The rule *"every function takes a pointer"* is
  already house style and every library follows it, so a struct copy is
  arguably a thing not to want.

**Recommendation: (a).** `y = x` is ordinary C, the fix is bounded, and it is
the one of the three struct bugs where refusing costs the programmer
something real.

**Decided (you):** the recommendation — **(a)**. `y = x;` and
`struct P y = x;` both copy every word; using the assignment as a *value*
(`z = (y = x)`) is refused, because the expression model keeps its result in
`A` and a struct does not fit there.

### Q2. Do by-value parameters and returns stay refused for good?

§2. They need an ABI change — several words a slot and a hidden return
pointer — for a feature nothing in the tree uses.

**Recommendation: yes, refused, and said so in
[design/03-abi.md](../compiler/design/03-abi.md).** A permanent, documented
"no" is worth more than an open question, and the error names the fix.

**Decided (you):** the recommendation — **refused, permanently**, and written
into [design/03-abi.md](../compiler/design/03-abi.md). The error names the
fix: *"parameter 'p' cannot take struct P by value; take 'struct P *'
instead"*.

### Q3. Is a non-constant global initialiser an error, or a zero with a warning?

§3. `int b = a;` is not constant in C. Today it is silently 0.

**Recommendation: an error**, matching what `_init_list` already does for
array elements. The compiler has no warning channel today, and inventing one
so this can stay wrong would be the wrong first use of it.

**Decided (you):** the recommendation — **an error**. `a global initialiser
must be a compile-time constant`, the message `_init_list` was already
using.

### Q4. Inline the signed divide, or plant a helper?

§5. A dozen instructions with a branch, at every `/` on signed operands.

**Recommendation: a helper**, `__divsi3`, emitted once and only when used.
There is no linker to fold duplicates, and `/` is common.

**Decided (you):** the recommendation — **a helper**, and `__modsi3` beside
it. They do **not** use the C calling convention: the operands are already in
`A` and `B` where a binary operator leaves them, the answer comes back in
`A`, and `C`, `D` and `E` are scratch — so nothing is saved around the call
and `F` is never touched. §9 has what it cost.

### Q5. When?

These are independent of [the font plan](fonts/README.md) and of
[the GUI library](gui/README.md) — the font work is C and the device, and
none of it is blocked by any of this.

- **(a)** After the fonts, before the GUI library (G1). The GUI is the code
  that leans hardest on these rules — its whole API shape is a response to
  them — so it is the thing that benefits most from them being true.
- **(b)** Now, before F1. Small, and everything after it is written on a
  compiler that does what it says.
- **(c)** Whenever a phase above irritates you enough.

**Recommendation: (a)**, with **C1 and C2 pulled forward to whenever there is
an idle afternoon** — they are the cheap ones, and they are the two that stop
a wrong program from looking right.

**Decided (you): (b), and then some** — *"now please fix the compiler"*. All
six phases were built before F1, so the font and GUI work starts on a
compiler that does what it says.

---

## 9. As built

**All six phases, 2026-09-20.** `tests/test_compiler.py` grew 47 cases — §1's
table verbatim, each asserting the right answer — and the whole suite passes:
**1,935 tests**, which compile and *run* every program in the tree, the
kernel and `bios2` among them.

### What changed

| file | what |
|---|---|
| `compiler/analyzer.py` | struct params and returns refused; `_const_init` folds a global's initialiser or refuses it; `_address_constant` for `&thing`, array and function names, strings; `operand_type` attached to `/ % << >>`, including the compound forms; `_check_struct_copy` |
| `compiler/codegen.py` | `HELPERS` — `__divsi3` and `__modsi3`, planted only when used, with dependencies pulled in; `_arithmetic_shift`; `_copy_words`; struct paths in `_e_Assign` and `_s_VarDecl`; `_static_value` for address initialisers |
| `compiler/ast_nodes.py` | `AddressLiteral` |
| `compiler/design/02-language.md`, `03-abi.md` | the phantom diagnostic replaced by what is now true; the struct ABI rule written down |
| `lib/pigeon/math.c`, `math.h` | their opening comments described these bugs as facts of the machine |
| `docs/gui/constraints.md`, `scaling.md` | §1 is now a before/after table; the rules it forced are relaxed or re-justified |

### Two things that came free

**A table of function pointers at file scope works now.** `cb table[] = {one,
two};` used to put a zero in every slot, so calling one jumped to address 0.
That falls straight out of C2's address constants, and it is the shape
[the GUI library](gui/api.md) would have wanted for its widget kinds.

**Pointer difference is signed.** `p - q` divides the byte distance by the
element size, and that divide was unsigned too, so `p - q` was a vast
positive number whenever `q` was the later pointer.

### What it cost

Images grew by a fraction of a percent — the helper is planted once, the
shift is three instructions, and unsigned arithmetic is untouched:

| program | before | C1–C6 | with C7, C8 | |
|---|---|---|---|---|
| `user/os/kernel.c` | 377,600 | 378,104 | 380,280 | +2,680 B, **+0.71%** |
| `user/graph.c` | 159,128 | 159,984 | 160,672 | +1,544 B, +0.97% |
| `user/cube.c` | 79,652 | 80,036 | 80,148 | +496 B, +0.62% |
| `firmware/bios2.c` | 108,484 | 108,692 | 109,540 | +1,056 B, +0.97% |

*(measured, built with their own libraries at their own origins.)* C7 is most
of the second column: two instructions at every `char` load, and the kernel's
console reads `char` grids constantly. Under one percent, and it buys code
that means what it says.

### Two more, found on the way — C7 and C8

Both were written up here as candidates and then **built the same day**, on
your *"fix those issues as well"*. Neither was in §1, and both are the same
kind of thing: a declaration or a load that quietly means something other
than what it says.

#### C7. `char` is signed, and now sign-extends when loaded

`char c = -1; (int)c` was **255** *(run)*, because `MR` loads a byte
zero-extended and nothing put the sign back — the mirror of §4's shift and
§5's divide. `(char)200` was 200 rather than −56 for the same reason;
`_e_Cast` carried a `sign extension is not modelled yet` placeholder.

**Two instructions do it**, and they are cheaper than a shift pair:

```
    XOR A, A, #0x80          ; bias the byte
    SUB A, A, #0x80          ; and take it away: 0..127 unchanged, 128..255 negative
```

Every load goes through one `_load()` now, so a `char` read as a global, a
local, a parameter, an array element, a struct member or through a pointer
all sign-extend, and a cast to `char` narrows *and* extends.
**`unsigned char` is untouched** and still costs one instruction, which is
why the byte-handling code in the tree — `bmp.c`'s `unsigned char *file`,
`fs.c`'s blocks — pays nothing.

#### C8. `char *p = "hi";` at file scope is a pointer again

It used to become `char[3]`: the parser cannot tell `char *p` from
`char p[]`, since both reach the same pointer type, so
`_size_from_initializer` rebuilt an array from either. `sizeof(p)` was 3,
`p = other;` was refused as an assignment to an array, and the image held
the *characters* where the pointer should be.

The declarator now records whether it actually parsed `[]`, and only then
does the initialiser set a length. `char v[] = "hi";` is a 3-byte array as
before; `char *p = "hi";` is a word holding the string's label.

**Nothing in the tree used the broken spelling at file scope** *(checked)* —
the two `char *x = "..."` in `pgs.c` and `kernel.c` are locals, which never
went through that path.

---

## 10. What this plan is not

- **Not new language features.** `enum`, `switch`, `static` locals,
  self-referential structs, `goto`, floating point and the rest all produce a
  clear error today *(run)*, and a missing feature you are told about is a
  decision, not a bug. If any of them is wanted, it is its own plan.
- **Not an optimiser.** Nothing here makes code faster; C4 makes signed
  division slower, on purpose, and unsigned division exactly as fast.
- **Not a linker.** One translation unit stays the model *(checked:
  `cc.py:115-140`)*.
- **Not a warning system.** Every diagnostic in this plan is an error. If a
  warning channel is ever wanted, it should arrive with a reason better than
  "so this can keep compiling".
