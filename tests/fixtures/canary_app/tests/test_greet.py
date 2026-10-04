import sys
from pathlib import Path

from hypothesis import given, strategies as st

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from greet import greeting  # noqa: E402


def test_greets_by_name():
    assert greeting("qq") == "hello, qq"


@given(st.text())
def test_greeting_is_one_bounded_line(name):
    out = greeting(name)
    assert out.startswith("hello, ") and "\n" not in out and len(out) <= len("hello, ") + 64
