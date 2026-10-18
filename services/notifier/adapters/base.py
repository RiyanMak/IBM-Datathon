"""The adapter interface: how the notifier talks to one chat product.

The core decides what to send and what a click means; an adapter only knows how to
draw a card and how to hear a click. No business logic in here or in any adapter.

    ref = adapter.post(card)               a new message; ref is a small JSON-able dict
    adapter.update(ref, card)              redraw that message in place
    adapter.upload(ref, filename, text)    attach a file (the case packet) to it
    adapter.on_click(callback)             start hearing clicks

callback(ring_id, version, decision, note, decided_by, channel) returns
{"ok": bool, "message": str}; when ok is False the adapter shows the message only to
the person who clicked.

post, update and upload raise when the chat product cannot be reached; the notifier
then leaves the case pending on Redis and tries again later. A chat adapter added
later implements these four methods and nothing else changes.
"""


class Adapter:
    name = "base"
    can_update = False
    can_upload = False

    def post(self, card):
        raise NotImplementedError

    def update(self, ref, card):
        raise NotImplementedError

    def upload(self, ref, filename, text):
        raise NotImplementedError

    def on_click(self, callback):
        """Default: this channel has no buttons."""

    def stop(self):
        """Default: nothing to close."""
