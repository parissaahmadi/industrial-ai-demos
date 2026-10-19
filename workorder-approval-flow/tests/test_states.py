import itertools

import pytest

from workorder_flow import states as st
from workorder_flow.errors import Forbidden, InvalidTransition

L = 50_000_000


def test_every_status_is_either_active_or_final():
    assert set(st.STATUSES) == {st.DRAFT, st.SUBMITTED, st.REVIEWED, st.RETURNED, st.APPROVED, st.REJECTED, st.WITHDRAWN}
    assert st.FINAL == {st.APPROVED, st.REJECTED, st.WITHDRAWN}
    assert not (st.EDITABLE & st.FINAL) and not (st.WITHDRAWABLE & st.FINAL)


@pytest.mark.parametrize(
    "status,action,cost,expected",
    [
        ("submitted", "approve", 0, "approved"),
        ("submitted", "approve", L, "approved"),  # the limit itself is still the reviewer's call
        ("submitted", "approve", L + 1, "reviewed"),  # one rial more needs a supervisor
        ("submitted", "reject", 10, "rejected"),
        ("submitted", "return", L + 1, "returned"),
        ("reviewed", "approve", L + 1, "approved"),
        ("reviewed", "reject", L + 1, "rejected"),
        ("reviewed", "return", L + 1, "returned"),
    ],
)
def test_decision_outcomes(status, action, cost, expected):
    assert st.decide(status, action, cost, L).status == expected


def test_first_stage_approval_of_a_costly_request_remembers_the_reviewer_and_return_forgets_it():
    assert st.decide("submitted", "approve", L + 1, L).set_reviewed_by is True
    assert st.decide("submitted", "approve", 5, L).set_reviewed_by is False
    assert st.decide("reviewed", "return", L + 1, L).clear_reviewed_by is True
    assert st.decide("submitted", "return", 5, L).clear_reviewed_by is True


@pytest.mark.parametrize("status", ["draft", "returned", "approved", "rejected", "withdrawn"])
@pytest.mark.parametrize("action", ["approve", "reject", "return"])
def test_no_decisions_outside_the_two_review_stages(status, action):
    with pytest.raises(InvalidTransition, match="cannot be decided"):
        st.decide(status, action, 1, L)


def test_final_states_say_so():
    with pytest.raises(InvalidTransition, match="final"):
        st.decide("approved", "approve", 1, L)


def test_unknown_action():
    with pytest.raises(InvalidTransition, match="unknown decision"):
        st.decide("submitted", "shrug", 1, L)


def check(role, status, actor="x", owner="o", reviewed_by=None):
    st.check_decider(role=role, status=status, actor=actor, owner=owner, reviewed_by=reviewed_by)


def test_role_matrix_is_exhaustive():
    """Every role x stage combination, written out: who may decide where."""
    allowed = {("reviewer", "submitted"), ("supervisor", "submitted"), ("supervisor", "reviewed")}
    for role, status in itertools.product(st.ROLES, ("submitted", "reviewed")):
        if (role, status) in allowed:
            check(role, status)
        else:
            with pytest.raises(Forbidden, match="can only be decided"):
                check(role, status)


@pytest.mark.parametrize("role,status", sorted({("reviewer", "submitted"), ("supervisor", "submitted"), ("supervisor", "reviewed")}))
def test_nobody_decides_their_own_request(role, status):
    with pytest.raises(Forbidden, match="own request"):
        check(role, status, actor="same", owner="same")


def test_the_final_approver_must_differ_from_the_first_reviewer():
    with pytest.raises(Forbidden, match="already reviewed"):
        check("supervisor", "reviewed", actor="sup.e", reviewed_by="sup.e")
    check("supervisor", "reviewed", actor="sup.f", reviewed_by="sup.e")
    # the first stage has no such restriction (nothing was reviewed yet)
    check("supervisor", "submitted", actor="sup.e", reviewed_by="sup.e")


@pytest.mark.parametrize("status", ["draft", "returned", "approved", "rejected", "withdrawn"])
def test_outside_the_review_stages_submitters_are_still_told_it_is_not_theirs_to_decide(status):
    with pytest.raises(Forbidden, match="reviewer or supervisor"):
        check("submitter", status)
    check("reviewer", status)  # role check passes; the transition itself will be refused by decide()
