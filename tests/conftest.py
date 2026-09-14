"""pytest only. Each test runs with the timer device on a stepping clock,
as tests/_runner.py runs them without pytest: waiting on a timer costs no
real seconds, and the result doesn't depend on the speed of the host. A
test that measures real seconds is marked @real_clock and keeps the wall
clock."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import _runner                                                        # noqa: E402


@pytest.fixture(autouse=True)
def timer_clock(request):
    with _runner.timer_clock(request.function):
        yield
