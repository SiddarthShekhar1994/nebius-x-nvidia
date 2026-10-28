"""Which model tier handles which kind of task."""

TASK_TIERS = {
    "classify": "nano",
    "triage": "nano",
    "radar": "nano",
    "tool_call": "super",
    "draft": "super",
    "plan": "ultra",
    "skill_write": "ultra",
}

TASK_TYPES = tuple(TASK_TIERS)


class UnknownTaskType(ValueError):
    pass


def tier_for(task_type: str) -> str:
    try:
        return TASK_TIERS[task_type]
    except KeyError:
        raise UnknownTaskType(
            f"unknown task_type {task_type!r}; expected one of {', '.join(TASK_TYPES)}"
        ) from None
