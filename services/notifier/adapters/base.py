"""The interface every chat adapter implements. No business logic lives in an adapter.

The notifier core decides *what* to send and *what a click means*; an adapter only
knows *how* to talk to one chat product (Slack, Teams, the console). Replacing the
chat product means replacing one file.

    adapter.start(clicks)              begin listening; put click dicts on the queue
    ref = adapter.post(card)           send a new card, return an opaque message ref
    adapter.update(ref, card)          redraw that card in place
    adapter.upload(ref, filename, text)  attach a text file (the case packet) to it
    adapter.stop()                     stop listening

A card is the plain dict from services/notifier/cards.py. A click is a dict:

    {"ring_id": "R-0042", "version": "3", "decision": "freeze",
     "user_id": "U0123ABC", "user_name": "jane.doe", "note": ""}

`version` is the version the person saw on the card ("" if the adapter does not
know it; the notifier then uses the version it last posted). The notifier records
the click as decided_by "<kind>:<user_id>:<user_name>" (or "<kind>:<user_name>" when
user_id equals the kind, as in the console) and channel = <kind>. The kind is the
adapter's `kind` attribute ("slack", "teams", "console"); an adapter without one is
named from its class name (SlackAdapter -> "slack"). It is not called `channel`
because the Slack adapter uses that name for the Slack channel ID.

An adapter raises AdapterError when the chat product cannot be reached. The notifier
then leaves the case unacknowledged on Redis so it is sent once the connection is back.
"""
import queue  # noqa: F401  (named in the type of start's argument)


class AdapterError(Exception):
    """The chat product could not take the message (network down, rate limited, bad token)."""


class Adapter:
    kind = ""   # "slack" | "teams" | "console": written to the decision's `channel` field

    def start(self, clicks: "queue.Queue") -> None:
        """Begin receiving clicks; must not block. Default: no clicks (post-only adapter)."""
        return None

    def post(self, card: dict) -> str:
        raise NotImplementedError

    def update(self, message_ref: str, card: dict) -> None:
        raise NotImplementedError

    def upload(self, message_ref: str, filename: str, text: str) -> None:
        """Attach a text file to a posted card. Default: not supported, do nothing."""
        return None

    def stop(self) -> None:
        return None
