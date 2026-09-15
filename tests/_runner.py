"""Minimal test runner, so the suite works without pytest installed.

pytest collects the test_* functions in these modules normally. This lets
`python3 tests/test_golden.py` do the same on a machine that has no pytest
(this repo's environment is PEP 668-managed, so there may not be one).
"""
import contextlib
import traceback


def run_module(namespace, title):
    tests = sorted((n, f) for n, f in namespace.items()
                   if n.startswith("test_") and callable(f))
    failures = []
    print(f"\n{title}  ({len(tests)} tests)")
    for name, fn in tests:
        cases = getattr(fn, "_cases", [((), {})])
        for args, kwargs in cases:
            label = f"{name}{'[' + '-'.join(map(str, args)) + ']' if args else ''}"
            try:
                with timer_clock(fn):
                    fn(*args, **kwargs)
                print(f"  PASS  {label}")
            except Exception as e:
                failures.append((label, e, traceback.format_exc()))
                print(f"  FAIL  {label}: {e}")
    if failures:
        print(f"\n{len(failures)} failure(s):")
        for label, _, tb in failures:
            print(f"\n--- {label} ---\n{tb}")
    else:
        print(f"  all {sum(len(getattr(f, '_cases', [1])) for _, f in tests)} passed")
    return 1 if failures else 0


STEP = 0.05     # seconds the stepping clock moves per read


class SteppingClock:
    """A clock for the timer device that moves STEP seconds each time it
    is read, so a two-second wait is 40 looks.

    A guest waiting on a timer polls its status in a loop: the BIOS spins
    two seconds before it jumps to a program, and bios2 counts down before
    it boots. On the wall clock that is seconds of CPU in every test that
    boots, and a test that gives up after a number of instructions passes
    or fails with the speed of the host (tests/test_display.py's boot()
    says how)."""

    def __init__(self):
        self.now = 0.0

    def __call__(self):
        self.now += STEP
        return self.now


def real_clock(fn):
    """Keep the timer device on the wall clock for this test: one that
    measures real seconds, or reads a countdown off the screen."""
    fn._real_clock = True
    return fn


@contextlib.contextmanager
def timer_clock(fn):
    """The stepping clock for the length of one test, unless it is
    @real_clock. run_module uses it, and so does tests/conftest.py."""
    from emulator.devices import timer
    if getattr(fn, "_real_clock", False):
        yield
        return
    saved = timer.clock
    timer.clock = SteppingClock()
    try:
        yield
    finally:
        timer.clock = saved


def cases(*argsets):
    """Parametrize a test.

    Delegates to pytest.mark.parametrize when pytest is installed, so the
    same test files work under both runners; otherwise it records the
    argument sets for run_module above.
    """
    def decorate(fn):
        fn._cases = [(a if isinstance(a, tuple) else (a,), {}) for a in argsets]
        try:
            import inspect

            import pytest
        except ImportError:
            return fn
        names = list(inspect.signature(fn).parameters)
        if not names:
            return fn
        # With a single argname pytest passes the value straight through, so
        # it must not be wrapped in a 1-tuple; with several it unpacks them.
        values = list(argsets) if len(names) == 1 else [
            a if isinstance(a, tuple) else (a,) for a in argsets]
        return pytest.mark.parametrize(",".join(names), values)(fn)
    return decorate
