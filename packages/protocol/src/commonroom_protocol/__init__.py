"""Wire protocol constants shared by the server, the SDK and the viewer.

The specification lives in packages/protocol/README.md. This module holds the
names and version numbers so that every package agrees on them.
"""

# Protocol versions are "MAJOR.MINOR".
#   MINOR bumps are additive: new optional fields, new intents, new event types,
#   new failure reasons. Peers must ignore fields and event types they don't know.
#   MAJOR bumps are breaking: anything removed, renamed or changed in meaning.
# A client and server can talk when they share a MAJOR version. Each MAJOR has
# its own URL prefix (/v1/...), so a server can serve several majors at once.
PROTOCOL_MAJOR = 1
PROTOCOL_MINOR = 0
PROTOCOL_VERSION = f"{PROTOCOL_MAJOR}.{PROTOCOL_MINOR}"
SUPPORTED_VERSIONS = (PROTOCOL_VERSION,)  # newest first; one entry per supported MAJOR
API_PREFIX = f"/v{PROTOCOL_MAJOR}"

# Format version of world.yaml / objects.yaml files this code understands.
WORLD_FORMAT_VERSION = 1


def parse_version(value) -> tuple[int, int]:
    """Parse "1.2", "1" or 1 into (major, minor). Raises ValueError otherwise."""
    if isinstance(value, bool):
        raise ValueError(f"bad protocol version {value!r}")
    if isinstance(value, int):
        return value, 0
    if not isinstance(value, str):
        raise ValueError(f"bad protocol version {value!r}")
    parts = value.strip().split(".")
    if not 1 <= len(parts) <= 2 or not all(p.isdigit() for p in parts):
        raise ValueError(f"bad protocol version {value!r}")
    return int(parts[0]), int(parts[1]) if len(parts) == 2 else 0


def negotiate(offered, supported=SUPPORTED_VERSIONS) -> str | None:
    """Pick the version to speak. `offered` is one version or a list from the client.

    Returns the server's own version string for the highest MAJOR both sides
    support, or None if there is none. Unparseable entries are ignored.
    """
    if not isinstance(offered, (list, tuple)):
        offered = [offered]
    client_majors = set()
    for v in offered:
        try:
            client_majors.add(parse_version(v)[0])
        except ValueError:
            continue
    for v in sorted(supported, key=parse_version, reverse=True):
        if parse_version(v)[0] in client_majors:
            return v
    return None


# Intents: AI -> server. Every intent carries a client-chosen "id" that the
# server echoes back in the matching "ack" or "action_failed".
REGISTER = "register"
MOVE = "move"
SAY = "say"
WHISPER = "whisper"
SHOUT = "shout"
INTERACT = "interact"
PICK_UP = "pick_up"
PUT_DOWN = "put_down"
GIVE = "give"
LOOK = "look"
TYPING = "typing"

INTENTS = (REGISTER, MOVE, SAY, WHISPER, SHOUT, INTERACT, PICK_UP, PUT_DOWN, GIVE, LOOK, TYPING)

# Replies: server -> the AI that sent an intent.
WELCOME = "welcome"
ACK = "ack"
ACTION_FAILED = "action_failed"
ERROR = "error"

# Events: server -> every AI that can perceive them (and every viewer).
ENTERED_ROOM = "entered_room"
LEFT_ROOM = "left_room"
POSITION_CHANGED = "position_changed"
SPEECH = "speech"
OBJECT_CHANGED = "object_changed"
PRESENCE = "presence"
TIME_TICK = "time_tick"
SNAPSHOT = "snapshot"
TYPING_EVENT = "typing"

EVENTS = (ENTERED_ROOM, LEFT_ROOM, POSITION_CHANGED, SPEECH, OBJECT_CHANGED, PRESENCE, TIME_TICK, SNAPSHOT, TYPING_EVENT)

# Viewer stream: server -> viewer.
WORLD_STATE = "world_state"
UPDATE = "update"


class Reason:
    """Failure reasons carried by action_failed. Clients must expect races."""

    BAD_REQUEST = "bad_request"
    UNKNOWN_INTENT = "unknown_intent"
    NO_SUCH_EXIT = "no_such_exit"
    NO_SUCH_OBJECT = "no_such_object"
    NO_SUCH_ENTITY = "no_such_entity"
    NOT_HERE = "not_here"
    NOT_HELD = "not_held"
    ALREADY_HELD = "already_held"
    NOT_PORTABLE = "not_portable"
    HANDS_FULL = "hands_full"
    CONTAINER_CLOSED = "container_closed"
    CONTAINER_FULL = "container_full"
    NOT_A_CONTAINER = "not_a_container"
    WOULD_NEST = "would_nest"
    UNKNOWN_VERB = "unknown_verb"
    NOT_NOW = "not_now"
    NEEDS_ITEM = "needs_item"
    CONVERSATION_MOVED_ON = "conversation_moved_on"
    TEXT_TOO_LONG = "text_too_long"
    INTERNAL_ERROR = "internal_error"
    TIMEOUT = "timeout"
    DISCONNECTED = "disconnected"


MAX_TEXT_LENGTH = 2000
