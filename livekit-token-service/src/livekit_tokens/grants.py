"""What a participant may do in a room, by their part in an assistance session.

LiveKit's ``video`` grant is the whole authorisation model of the media server, so this file is the security
boundary: it is an allow-list. Admin-level grants (``roomCreate``, ``roomAdmin``, ``roomList``, ``roomRecord``,
``ingressAdmin``) are never emitted, and a test enforces that.
"""

from __future__ import annotations

from dataclasses import dataclass

CAMERA, MICROPHONE, SCREEN_SHARE = "camera", "microphone", "screen_share"
PARTICIPANT_ROLES = ("technician", "expert", "observer")


@dataclass(frozen=True)
class VideoGrant:
    room: str
    can_publish: bool
    can_subscribe: bool
    can_publish_data: bool
    sources: tuple[str, ...]
    hidden: bool

    def to_claim(self) -> dict:
        claim = {
            "roomJoin": True,
            "room": self.room,
            "canPublish": self.can_publish,
            "canSubscribe": self.can_subscribe,
            "canPublishData": self.can_publish_data,
            "hidden": self.hidden,
        }
        if self.can_publish:
            claim["canPublishSources"] = list(self.sources)
        return claim


_RULES = {
    # field technician: shows the machine (camera), talks, receives the expert's media and annotations
    "technician": dict(can_publish=True, can_subscribe=True, can_publish_data=True, sources=(CAMERA, MICROPHONE), hidden=False),  # fmt: skip
    # remote expert: talks, may share a drawing or manual page, may send annotations
    "expert": dict(can_publish=True, can_subscribe=True, can_publish_data=True, sources=(MICROPHONE, CAMERA, SCREEN_SHARE), hidden=False),  # fmt: skip
    # supervisor / admin looking in: receives only, invisible to the others, cannot send anything
    "observer": dict(can_publish=False, can_subscribe=True, can_publish_data=False, sources=(), hidden=True),
}


def grant_for(participant_role: str, room: str) -> VideoGrant:
    if participant_role not in _RULES:
        raise ValueError(f"unknown participant role {participant_role!r}")
    return VideoGrant(room=room, **_RULES[participant_role])
