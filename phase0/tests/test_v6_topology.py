# -*- coding: utf-8 -*-
from v6_topology import (
    canonical_cycle,
    compute_topology_fingerprint,
    encode_faces,
    faces_all_reversed,
    reverse_winding,
)


def test_canonical_cycle_lex_smallest_rotation():
    assert canonical_cycle([3, 1, 4, 2]) == [1, 4, 2, 3]
    # 最小値が2つあるときは辞書順
    assert canonical_cycle([2, 1, 3, 1]) == [1, 2, 1, 3]
    assert canonical_cycle([]) == []
    assert canonical_cycle([7]) == [7]


def test_fingerprint_invariant_to_rotation_not_reverse():
    a = [[0, 1, 2, 3], [4, 5, 6]]
    b = [[1, 2, 3, 0], [5, 6, 4]]
    c = [[0, 3, 2, 1], [4, 6, 5]]
    assert compute_topology_fingerprint(a) == compute_topology_fingerprint(b)
    assert compute_topology_fingerprint(a) != compute_topology_fingerprint(c)


def test_fingerprint_changes_when_topology_changes():
    a = [[0, 1, 2, 3]]
    split = [[0, 1, 2], [0, 2, 3]]
    assert compute_topology_fingerprint(a) != compute_topology_fingerprint(split)


def test_faces_all_reversed():
    before = [[0, 1, 2, 3], [10, 11, 12]]
    after = [reverse_winding(f) for f in before]
    assert faces_all_reversed(before, after)
    assert not faces_all_reversed(before, before)
    assert not faces_all_reversed(before, [[0, 1, 2, 3]])


def test_encode_faces_stable():
    text = encode_faces([[3, 1, 2]])
    assert "1,2,3" in text
    assert text.startswith("F=1")


def test_v6_evaluate_without_maya():
    from v6_topology_fingerprint import evaluate

    status, _summary = evaluate({
        "invariants": [
            {"command": {"ok": True}, "unchanged": True},
        ],
        "expected_changes": [
            {"command": {"ok": True}, "changed": True},
        ],
    })
    assert status == "PASS"
    status, _summary = evaluate({
        "invariants": [
            {"command": {"ok": True}, "unchanged": False},
        ],
        "expected_changes": [
            {"command": {"ok": True}, "changed": True},
        ],
    })
    assert status == "FAIL"
