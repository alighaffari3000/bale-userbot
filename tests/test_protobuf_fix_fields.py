"""The memoized `ProtoBuf._fix_fields` (see bale_userbot/patches.py).

Two promises: the decoder's output is exactly what BaleClient's own
`_fix_fields` produces, and the work no longer grows with the number of
elements times the size of the first one — the cost that froze a bot's event
loop on one history page from a photo-heavy group.
"""

import importlib.util
import random

import baleclient.utils.protobuf as protobuf_module
import pytest
from baleclient.utils.protobuf import ProtoBuf

import bale_userbot  # noqa: F401 — importing applies the wire fixes


def _unpatched_protobuf_class() -> type:
    """A fresh copy of BaleClient's ProtoBuf, untouched by bale-userbot."""
    spec = importlib.util.spec_from_file_location(
        "_original_baleclient_protobuf", protobuf_module.__file__
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.ProtoBuf


ORIGINAL = _unpatched_protobuf_class()()
PATCHED = ProtoBuf()


def _message(i: int, rng: random.Random, *, thumb: int = 0, subs: int = 3) -> dict:
    """Roughly the shape of a history entry: ids, a date, text, a sub-list."""
    message = {
        "1": i,
        "2": 1_790_000_000_000 + i,
        "3": {"15": {"1": f"پیام آزمایشی {i} " * rng.randint(1, 4)}},
        "4": [{"1": j, "2": "x" * rng.randint(0, 6)} for j in range(subs)],
    }
    if thumb:
        message["5"] = {"1": {"3": bytes(rng.getrandbits(8) for _ in range(thumb))}}
    return message


def _page(n: int, *, seed: int = 0, first_thumb: int = 0, subs: int = 3) -> bytes:
    rng = random.Random(seed)
    data = {"1": [_message(0, rng, thumb=first_thumb, subs=subs)]}
    data["1"] += [_message(i, rng, subs=subs) for i in range(1, n)]
    return ORIGINAL.encode(data)


#: One type per field number, as in a real schema — the library's own
#: encoder cannot write a repeated field whose elements disagree on a type.
_FIELD_KINDS = {
    1: "int",
    2: "text",
    3: "dict",
    4: "list",
    5: "bytes",
    6: "int",
    7: "text",
    8: "dict",
    9: "list",
    10: "bytes",
    11: "text",
}


def _random_dict(rng: random.Random, depth: int) -> dict:
    out: dict[str, object] = {}
    for k in rng.sample(range(1, 12), rng.randint(1, 4)):
        kind = _FIELD_KINDS[k]
        if kind in ("dict", "list") and depth <= 0:
            continue
        if kind == "int":
            out[str(k)] = rng.randint(0, 2**40)
        elif kind == "text":
            out[str(k)] = rng.choice(["متن " * rng.randint(1, 3), "ascii text"])
        elif kind == "bytes":
            out[str(k)] = bytes(rng.getrandbits(8) for _ in range(rng.randint(1, 40)))
        elif kind == "dict":
            out[str(k)] = _random_dict(rng, depth - 1)
        else:
            out[str(k)] = [
                _random_dict(rng, depth - 1) for _ in range(rng.randint(1, 4))
            ]
    return out or {"1": rng.randint(0, 9)}


@pytest.mark.parametrize(
    "raw",
    [
        _page(1),
        _page(30),
        _page(30, first_thumb=2_000),
        _page(12, subs=0),
        ORIGINAL.encode({"1": {"1": "hello"}}),
        ORIGINAL.encode({"1": [{"1": "a"}, {"1": "b"}], "2": 7}),
    ],
)
def test_same_output_as_the_original(raw):
    assert PATCHED.decode(raw) == ORIGINAL.decode(raw)


@pytest.mark.parametrize("seed", range(40))
def test_same_output_on_random_shapes(seed):
    raw = ORIGINAL.encode(_random_dict(random.Random(seed), 4))
    assert PATCHED.decode(raw) == ORIGINAL.decode(raw)


def test_bytearray_input_decodes_like_bytes():
    raw = _page(10, first_thumb=500)
    assert PATCHED.decode(bytearray(raw)) == PATCHED.decode(raw)


def test_parsing_does_not_grow_with_the_page(monkeypatch):
    calls = 0
    real_parse = protobuf_module._parse_protobuf_fields

    def counting_parse(data):
        nonlocal calls
        calls += 1
        return real_parse(data)

    monkeypatch.setattr(protobuf_module, "_parse_protobuf_fields", counting_parse)

    def parses_for(n: int) -> int:
        nonlocal calls
        calls = 0
        PATCHED.decode(_page(n, first_thumb=5_000, subs=5))
        return calls

    # Each element used to re-parse its parent: 4x the page, 4x the parses.
    assert parses_for(200) == parses_for(50)
