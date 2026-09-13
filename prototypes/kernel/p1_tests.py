"""P1 (docs/kernel.md §16): the repository's own tests, with the six
prototype instructions and their syntax rows loaded.

Needs pytest, so run it with python3, not PyPy. About six minutes."""
import os
import sys

import proto
import pytest

os.chdir(proto.ROOT)
sys.exit(pytest.main(["-q", "-p", "no:cacheprovider", "tests"]))
