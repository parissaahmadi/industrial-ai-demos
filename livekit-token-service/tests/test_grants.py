import pytest

from livekit_tokens.grants import CAMERA, MICROPHONE, PARTICIPANT_ROLES, SCREEN_SHARE, grant_for

# Everything a role may ever carry. Anything else (roomCreate, roomAdmin, roomList, roomRecord, ingressAdmin,
# canUpdateOwnMetadata, kind, ...) must never appear in a token this service signs.
ALLOWED_KEYS = {"roomJoin", "room", "canPublish", "canSubscribe", "canPublishData", "hidden", "canPublishSources"}


@pytest.mark.parametrize("role", PARTICIPANT_ROLES)
def test_only_allow_listed_grant_keys(role):
    claim = grant_for(role, "assist-x").to_claim()
    assert set(claim) <= ALLOWED_KEYS
    assert claim["roomJoin"] is True and claim["room"] == "assist-x"
    assert claim["canSubscribe"] is True


def test_technician_shows_the_machine_but_cannot_share_a_screen():
    c = grant_for("technician", "r").to_claim()
    assert c["canPublish"] and c["canPublishData"] and not c["hidden"]
    assert c["canPublishSources"] == [CAMERA, MICROPHONE]


def test_expert_can_also_share_a_screen():
    c = grant_for("expert", "r").to_claim()
    assert c["canPublish"] and set(c["canPublishSources"]) == {MICROPHONE, CAMERA, SCREEN_SHARE}


def test_observer_only_listens_and_is_hidden():
    c = grant_for("observer", "r").to_claim()
    assert (c["canPublish"], c["canPublishData"], c["hidden"], c["canSubscribe"]) == (False, False, True, True)
    assert "canPublishSources" not in c


@pytest.mark.parametrize("role", ["admin", "supervisor", "", "Technician", "root"])
def test_unknown_participant_role_is_an_error(role):
    with pytest.raises(ValueError):
        grant_for(role, "r")
