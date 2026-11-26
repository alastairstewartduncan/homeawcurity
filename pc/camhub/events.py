"""A small publish/subscribe hub for live events (recording started, person seen)."""
import collections
import itertools
import queue
import threading
import time


class EventBus:
    def __init__(self, keep=50):
        self._subs = set()
        self._lock = threading.Lock()
        # Numbers carry on rising after a restart, so a phone catching up after a power cut
        # still gets the events it missed.
        self._seq = itertools.count(int(time.time() * 1000))
        self.recent = collections.deque(maxlen=keep)
        # Optional function that adds details to each event before it goes out
        # (home/away marks motion events with whether to alert).
        self.enrich = None

    def publish(self, event):
        event = dict(event)
        if self.enrich is not None:
            try:
                event = self.enrich(event) or event
            except Exception:
                pass
        event["seq"] = next(self._seq)
        with self._lock:
            self.recent.append(event)
            subs = list(self._subs)
        for q in subs:
            try:
                q.put_nowait(event)
            except queue.Full:
                pass

    def subscribe(self):
        q = queue.Queue(maxsize=200)
        with self._lock:
            self._subs.add(q)
        return q

    def unsubscribe(self, q):
        with self._lock:
            self._subs.discard(q)

    def since(self, seq):
        with self._lock:
            return [e for e in self.recent if e["seq"] > seq]
