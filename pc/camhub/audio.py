"""Sound from the camera's microphone, for the Listen button.

One ffmpeg reads the camera's sound while anyone is listening and turns it into plain
16-bit samples, which every listener gets a copy of. The page plays them straight away,
so there's much less delay than a normal audio player would add. ffmpeg stops a few
seconds after the last listener leaves, so the camera isn't kept busy for nothing.
"""
import logging
import queue
import subprocess
import threading
import time

log = logging.getLogger("camhub.audio")

RATE = 8000           # samples a second; camera microphones rarely give more
CHUNK = RATE // 10    # 0.1 s of sound (2 bytes a sample)


class AudioRelay:
    def __init__(self, cam_id, url_fn):
        self.cam_id = cam_id
        self._url_fn = url_fn      # returns the address to read sound from
        self._lock = threading.Lock()
        self._listeners = set()
        self._proc = None
        self._idle_since = None
        self.problem = None        # why there's no sound, shown in the viewer

    def subscribe(self):
        q = queue.Queue(maxsize=50)
        with self._lock:
            self._listeners.add(q)
            self._idle_since = None
            if self._proc is None:
                self._start()
        return q

    def unsubscribe(self, q):
        with self._lock:
            self._listeners.discard(q)
            if not self._listeners:
                self._idle_since = time.monotonic()

    def stop(self):
        with self._lock:
            self._listeners.clear()
            self._kill()

    def _kill(self):
        if self._proc is not None and self._proc.poll() is None:
            self._proc.kill()
        self._proc = None

    def _start(self):
        from .camera import FFMPEG, NO_WINDOW, _input_args, redact

        url = self._url_fn()
        if not url:
            self.problem = "No camera address saved yet."
            return
        cmd = [FFMPEG, "-hide_banner", "-loglevel", "error", "-fflags", "nobuffer",
               *_input_args(url, wallclock=url.lower().startswith("rtsp")),
               "-map", "0:a:0", "-vn", "-ac", "1", "-ar", str(RATE), "-f", "s16le", "pipe:1"]
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    stdin=subprocess.DEVNULL, creationflags=NO_WINDOW)
        except OSError as e:
            self.problem = f"Couldn't start ffmpeg ({e})."
            return
        self._proc = proc
        self.problem = None
        log.info("[%s] Listening started", self.cam_id)

        def errors():
            for line in proc.stderr:
                text = redact(line.decode("utf-8", "replace").strip())
                if not text:
                    continue
                low = text.lower()
                if "matches no streams" in low or "does not contain any stream" in low:
                    self.problem = "The camera isn't sending any sound. Check its microphone is turned on in the camera's own app."
                else:
                    self.problem = self.problem or text
                log.warning("[%s] ffmpeg (sound): %s", self.cam_id, text)

        threading.Thread(target=errors, daemon=True).start()
        threading.Thread(target=self._pump, args=(proc,), name=f"audio-{self.cam_id}", daemon=True).start()

    def _pump(self, proc):
        while True:
            data = proc.stdout.read(CHUNK * 2)
            if not data:
                break
            with self._lock:
                for q in list(self._listeners):
                    try:
                        q.put_nowait(data)
                    except queue.Full:   # a slow listener: drop their oldest sound
                        try:
                            q.get_nowait()
                            q.put_nowait(data)
                        except (queue.Empty, queue.Full):
                            pass
                if not self._listeners and self._idle_since and time.monotonic() - self._idle_since > 5:
                    log.info("[%s] Listening stopped", self.cam_id)
                    self._kill()
                    return
        with self._lock:
            if self._proc is proc:
                self._proc = None
                for q in list(self._listeners):
                    try:
                        q.put_nowait(None)   # tells listeners the sound has ended
                    except queue.Full:
                        pass
