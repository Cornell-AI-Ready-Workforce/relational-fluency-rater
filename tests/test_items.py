"""The 21 ESCI items: one table, keyed as step 1's scenario bank keys them."""
from __future__ import annotations

import re

import pytest

from conftest import OLD_REPO
from server import items


def test_shape():
    rows = items.all_items()
    assert len(rows) == 21
    assert set(items.ITEMS) == {"conflict_management", "influence", "inspirational_leadership", "teamwork"}
    keys = [r["key"] for r in rows]
    assert len(keys) == len(set(keys))
    assert all(re.fullmatch(r"[a-z_]+", k) for k in keys)
    # Reverse-keyed items are the *_r ones and their labels do not say so.
    for r in rows:
        assert r["reverse_keyed"] == r["key"].endswith("_r"), r
        assert "(R)" not in r["label"]
    assert [v for v, _ in items.SCALE] == [1, 2, 3, 4, 5]


def test_keys_and_labels_match_step1_scenarios():
    yaml = pytest.importorskip("yaml")
    specs = sorted((OLD_REPO / "scenarios" / "v3").glob("*.yaml"))
    if not specs:
        pytest.skip(f"step 1 scenarios not at {OLD_REPO}")
    theirs = {}
    for p in specs:
        d = yaml.safe_load(p.read_text(encoding="utf-8"))
        theirs.setdefault(d["construct"], {}).update(d.get("esci_items") or {})
    ours = {c: {k: label for k, label, _ in rows} for c, rows in items.ITEMS.items()}
    assert set(ours) == set(theirs)
    for c in ours:
        assert set(ours[c]) == set(theirs[c]), c
        for k, label in ours[c].items():
            assert label == theirs[c][k].replace(" (R)", ""), (c, k)
