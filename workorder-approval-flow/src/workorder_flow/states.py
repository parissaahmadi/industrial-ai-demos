r"""The life cycle of a work request and who may take each step.

This module is data plus two pure functions. It knows nothing about SQLite or HTTP, so the whole
approval matrix can be tested exhaustively.

    draft --submit--> submitted --approve (cost <= limit)--> approved
      |                  |   \--approve (cost > limit)--> reviewed --approve--> approved
      |                  |-- reject --> rejected              |-- reject --> rejected
      |                  \-- return --> returned <-----------/ return
      |                                   |  (owner edits, then submit again: revision + 1)
      \-- withdraw (owner, any non-final state) --> withdrawn
"""

from __future__ import annotations

from dataclasses import dataclass

from .errors import Forbidden, InvalidTransition

SUBMITTER, REVIEWER, SUPERVISOR = "submitter", "reviewer", "supervisor"
ROLES = (SUBMITTER, REVIEWER, SUPERVISOR)

DRAFT, SUBMITTED, REVIEWED, RETURNED = "draft", "submitted", "reviewed", "returned"
APPROVED, REJECTED, WITHDRAWN = "approved", "rejected", "withdrawn"
STATUSES = (DRAFT, SUBMITTED, REVIEWED, RETURNED, APPROVED, REJECTED, WITHDRAWN)
FINAL = frozenset({APPROVED, REJECTED, WITHDRAWN})
EDITABLE = frozenset({DRAFT, RETURNED})
WITHDRAWABLE = frozenset({DRAFT, SUBMITTED, REVIEWED, RETURNED})

APPROVE, REJECT, RETURN = "approve", "reject", "return"
DECISIONS = (APPROVE, REJECT, RETURN)

# Stage -> roles that may decide at that stage.
STAGE_ROLES: dict[str, frozenset[str]] = {
    SUBMITTED: frozenset({REVIEWER, SUPERVISOR}),
    REVIEWED: frozenset({SUPERVISOR}),
}
# Outside a review stage nobody can decide anything, but who is *told so* is still limited.
DECIDER_ROLES = frozenset({REVIEWER, SUPERVISOR})


@dataclass(frozen=True)
class Outcome:
    status: str
    set_reviewed_by: bool = False  # the decider becomes the first-stage reviewer
    clear_reviewed_by: bool = False


def decide(status: str, action: str, cost: int, reviewer_limit: int) -> Outcome:
    """Pure transition function for review decisions. Raises ``InvalidTransition``."""
    if action not in DECISIONS:
        raise InvalidTransition(f"unknown decision {action!r}; expected one of {', '.join(DECISIONS)}")
    if status not in STAGE_ROLES:
        raise InvalidTransition(
            f"a request in status '{status}' cannot be decided"
            + (" (it is final)" if status in FINAL else f" (decisions apply to: {', '.join(STAGE_ROLES)})")
        )
    if action == REJECT:
        return Outcome(REJECTED)
    if action == RETURN:
        return Outcome(RETURNED, clear_reviewed_by=True)
    # approve
    if status == REVIEWED or cost <= reviewer_limit:
        return Outcome(APPROVED)
    return Outcome(REVIEWED, set_reviewed_by=True)


def check_decider(*, role: str, status: str, actor: str, owner: str, reviewed_by: str | None) -> None:
    """Role and four-eyes rules for a decision. Raises ``Forbidden``."""
    allowed = STAGE_ROLES.get(status, DECIDER_ROLES)
    if role not in allowed:
        need = " or ".join(sorted(allowed))
        raise Forbidden(f"a request in status '{status}' can only be decided by a {need}; you are a {role}")
    if actor == owner:
        raise Forbidden("you cannot decide your own request (four-eyes rule)")
    if status == REVIEWED and actor == reviewed_by:
        raise Forbidden("you already reviewed this request; a different supervisor must give the final approval")
