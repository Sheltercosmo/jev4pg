"""Controlled filtering fixtures; labels never enter provider contexts."""

import random

SPLITS = {
    "development": (104729, 40), "validation": (130363, 80), "test": (155921, 400),
    "development_v2": (194911, 400), "validation_v2": (201107, 400), "test_v2": (216091, 400),
}
COUNTRIES = [
    ("France", True),
    ("Germany", True),
    ("Poland", True),
    ("Spain", True),
    ("Norway", False),
    ("Switzerland", False),
    ("Canada", False),
    ("Japan", False),
]
ROLES = [
    ("software engineer", True),
    ("backend developer", True),
    ("application programmer", True),
    ("software developer", True),
    ("sales representative", False),
    ("school teacher", False),
    ("registered nurse", False),
    ("civil engineer", False),
]
CONDITIONS = {
    "role": "The person's occupation involves developing computer software.",
    "country": "The person's country is a member of the European Union.",
    "phrase": "The note contains the exact phrase 'priority support'.",
}
CHINESE = {
    "role": "此人的职业是开发计算机软件。",
    "country": "此人所在的国家是欧盟成员国。",
    "phrase": "备注中包含完整短语“priority support”。",
}


def cases(split):
    seed, size = SPLITS[split]
    rng = random.Random(seed)
    rows, labels = [], []
    for index in range(size):
        role, software = rng.choice(ROLES)
        country, eu = rng.choice(COUNTRIES)
        priority = rng.choice([True, False])
        rows.append(
            {
                "record": f"{split}-{index}",
                "occupation": role,
                "country": country,
                "note": "Please arrange priority support."
                if priority
                else "Standard support is sufficient.",
            }
        )
        labels.append({"role": software, "country": eu, "phrase": priority})
    yield "single", rows, labels, {"role": CONDITIONS["role"]}
    yield "three_predicates", rows, labels, CONDITIONS
    yield "paraphrased", rows, labels, {
        "role": "Does this person build software for a living?",
        "country": "Is this person based in an EU member country?",
        "phrase": "Does the note literally say 'priority support'?",
    }
    yield "chinese", rows, labels, CHINESE
    renamed = [
        {"编号": r["record"], "职业": r["occupation"], "国家": r["country"], "备注": r["note"]}
        for r in rows
    ]
    yield "renamed", renamed, labels, CHINESE
    # An identical context is still a separate source row in the output.
    unique = [
        {"occupation": r["occupation"], "country": r["country"], "note": r["note"]}
        for r in rows[:8]
    ]
    duplicates = [unique[i % len(unique)] for i in range(size * 5)]
    repeated_labels = [labels[i % len(unique)] for i in range(size * 5)]
    yield "repeated_contexts", duplicates, repeated_labels, CONDITIONS
    wide = [
        {**r, "background": "Historical account activity and routine service records. " * 20}
        for r in rows
    ]
    yield "wide", wide, labels, {"role": CONDITIONS["role"]}


def questions(conditions):
    return {
        key: {"type": "noul", "instructions": condition} for key, condition in conditions.items()
    }
