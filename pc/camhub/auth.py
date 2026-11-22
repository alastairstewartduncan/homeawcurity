"""Password login. The password is stored only as a salted hash in config.json.

A successful login returns a signed token, used as a cookie by the browser and as a
Bearer token by the Android app's notification service. Changing the password
invalidates every token.
"""
import base64
import hashlib
import hmac
import secrets
import threading
import time

ITERATIONS = 200_000


def hash_password(password):
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, ITERATIONS)
    return f"pbkdf2${ITERATIONS}${base64.b64encode(salt).decode()}${base64.b64encode(dk).decode()}"


def check_password(password, stored):
    try:
        _, iters, salt, dk = stored.split("$")
        test = hashlib.pbkdf2_hmac("sha256", password.encode(), base64.b64decode(salt), int(iters))
        return hmac.compare_digest(test, base64.b64decode(dk))
    except (ValueError, TypeError):
        return False


def make_token(secret, days=365):
    expires = int(time.time()) + days * 86400
    payload = f"{expires}.{secrets.token_hex(8)}"
    sig = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{sig}"


def check_token(secret, token):
    try:
        expires, nonce, sig = token.split(".")
        payload = f"{expires}.{nonce}"
        good = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(sig, good) and int(expires) > time.time()
    except (ValueError, AttributeError):
        return False


class LoginLimiter:
    """Slows down password guessing: 5 failures from one address locks it for 15 minutes."""

    def __init__(self, max_failures=5, lock_seconds=900):
        self.max = max_failures
        self.lock_seconds = lock_seconds
        self._fails = {}
        self._lock = threading.Lock()

    def blocked(self, ip):
        with self._lock:
            n, until = self._fails.get(ip, (0, 0))
            return n >= self.max and until > time.time()

    def failed(self, ip):
        with self._lock:
            n, until = self._fails.get(ip, (0, 0))
            if until and until < time.time():
                n = 0
            self._fails[ip] = (n + 1, time.time() + self.lock_seconds)

    def succeeded(self, ip):
        with self._lock:
            self._fails.pop(ip, None)
