import pytest

from commonroom_protocol import PROTOCOL_VERSION, negotiate, parse_version


def test_parse_version():
    assert parse_version("1.2") == (1, 2)
    assert parse_version("3") == (3, 0)
    assert parse_version(2) == (2, 0)
    for bad in ("", "1.2.3", "v1", "a.b", None, True, 1.5):
        with pytest.raises(ValueError):
            parse_version(bad)


def test_negotiate_matches_on_major():
    assert negotiate("1.0", ["1.3"]) == "1.3"  # older minor client talks to newer minor server
    assert negotiate("1.7", ["1.3"]) == "1.3"  # newer minor client talks to older minor server
    assert negotiate(["2.0", "1.1"], ["2.1", "1.4"]) == "2.1"
    assert negotiate(["1.1"], ["2.1", "1.4"]) == "1.4"
    assert negotiate("2.0", ["1.0"]) is None
    assert negotiate(["junk", "1"], ["1.0"]) == "1.0"
    assert negotiate(PROTOCOL_VERSION) == PROTOCOL_VERSION
