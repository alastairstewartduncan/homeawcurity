"""A small publish/subscribe hub for live events (recording started, person seen)."""
import collections
import itertools
import queue
import threading


class EventBus:
    def __init__(self, keep=50):
        self._subs = set()
        self._lock = threading.Lock()
        self._seq = itertools.count(1)
        self.recent = collections.deque(maxlen=keep)

    def publish(self, event):
        event = dict(event, seq=next(self._seq))
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
