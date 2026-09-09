"""Minimal test runner, so the suite works without pytest installed.

pytest collects the test_* functions in these modules normally. This lets
`python3 tests/test_golden.py` do the same on a machine that has no pytest
(this repo's environment is PEP 668-managed, so there may not be one).
"""
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
