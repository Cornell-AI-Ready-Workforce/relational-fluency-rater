"""The instrument: the 21 ESCI items a rater scores on every encounter.

Copied from step 1's scenario bank (relational-fluency scenarios/v3/*.yaml,
each form's `esci_items`), keyed exactly as there, because the analysis
database's rf.rating rows reference rf.esci_item (construct, item_key) and the
loader fills that table from the same YAML. A key that differs here is a rating
no foreign key accepts. tests/test_items.py compares this table with the YAML
when step 1 is checked out beside this repository.

EVERY ITEM ON EVERY ENCOUNTER. Showing only the construct's five items would
tell the rater which competency the scenario targets, which is the one thing
the rating must be blind to. So a rater scores all 21 on each encounter, in an
order fixed per rater and encounter (server/rating.py), and "N/A" is the answer
for an item the conversation gave no chance to show.

Labels are the instrument's words with the reverse-key marker "(R)" removed
from what the rater sees; `reverse_keyed` keeps the fact for scoring.
"""
from __future__ import annotations

from typing import Dict, List, Tuple

# construct -> [(item_key, label, reverse_keyed)]
ITEMS: Dict[str, List[Tuple[str, str, bool]]] = {
    "conflict_management": [
        ("resolve_not_fester", "Tries to resolve conflict instead of allowing it to fester", False),
        ("de_escalate", "Resolves conflict by de-escalating the emotions in a situation", False),
        ("fester_r", "Allows conflict to fester", True),
        ("talk_openly", "Tries to resolve conflict by openly talking about disagreements with those involved", False),
        ("bring_into_open", "Resolves conflict by bringing it into the open", False),
    ],
    "influence": [
        ("multiple_approaches", "Convinces others by using multiple approaches", False),
        ("self_interest", "Convinces others by appealing to their self-interest", False),
        ("anticipates", "Anticipates how others will respond when trying to convince them", False),
        ("behind_scenes", "Convinces others by developing behind-the-scenes support", False),
        ("through_discussion", "Convinces others through discussion", False),
    ],
    "inspirational_leadership": [
        ("builds_pride", "Leads by building pride in the group", False),
        ("inspires", "Leads by inspiring people", False),
        ("not_inspire_r", "Does not inspire followers", True),
        ("brings_out_best", "Leads by bringing out the best in people", False),
        ("compelling_vision", "Leads by articulating a compelling vision", False),
    ],
    "teamwork": [
        ("not_cooperate_r", "Does not cooperate with others", True),
        ("supportive", "Works well in teams by being supportive", False),
        ("encourages_cooperation", "Works well in teams by encouraging cooperation", False),
        ("solicits_input", "Works well in teams by soliciting others' input", False),
        ("respectful", "Works well in teams by being respectful of others", False),
        ("encourages_participation", "Works well in teams by encouraging participation of everyone present", False),
    ],
}

# The frequency scale, as the instrument has it; "na" is this app's own answer
# for "no chance to show it", kept apart from 1 (had the chance, did not).
SCALE: List[Tuple[int, str]] = [(1, "Never"), (2, "Rarely"), (3, "Sometimes"), (4, "Often"),
                                (5, "Consistently")]
NA = "na"
NA_LABEL = "N/A: no chance to show it"


def all_items() -> List[Dict[str, object]]:
    """Every item, construct order, as the API serves it."""
    return [{"construct": c, "key": k, "label": label, "reverse_keyed": r}
            for c, rows in ITEMS.items() for k, label, r in rows]


def item_keys() -> Dict[str, str]:
    """item_key -> construct, for validating a submission."""
    return {k: c for c, rows in ITEMS.items() for k, _, _ in rows}
