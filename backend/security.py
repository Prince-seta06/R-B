import os, hmac, hashlib, secrets, time, logging, datetime as dt
from collections import defaultdict, deque
import jwt

log = logging.getLogger("rnb")

APP_ENV = os.getenv("APP_ENV", "dev").lower()          # set APP_ENV=production on a real server
IS_PROD = APP_ENV == "production"
COOKIE_SECURE = os.getenv("COOKIE_SECURE", "1" if IS_PROD else "0") == "1"
COOKIE_NAME = "rnb_session"
SESSION_HOURS = 12

_secret = os.getenv("JWT_SECRET")
if not _secret:
    if IS_PROD:
        raise RuntimeError("JWT_SECRET must be set when APP_ENV=production")
    _secret = secrets.token_urlsafe(48)  # dev only: random per start, so sessions end on restart
    log.warning("JWT_SECRET not set: using a random key for this run (sessions reset on restart)")
elif len(_secret) < 32 and IS_PROD:
    raise RuntimeError("JWT_SECRET must be at least 32 characters in production")
SECRET = _secret

ITERATIONS = 600_000  # OWASP 2023 guidance for PBKDF2-HMAC-SHA256


def hash_password(pw: str, salt: bytes | None = None, iterations: int = ITERATIONS) -> str:
    salt = salt or os.urandom(16)
    h = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt, iterations)
    return f"pbkdf2${iterations}${salt.hex()}${h.hex()}"


def verify_password(pw: str, stored: str) -> bool:
    try:
        _, it, salt_hex, h_hex = stored.split("$")
        h = hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(salt_hex), int(it))
        return hmac.compare_digest(h.hex(), h_hex)
    except ValueError:
        return False


_DUMMY = hash_password("not-a-real-password")


def burn_time(pw: str):
    """Unknown usernames cost the same as wrong passwords, so timing does not reveal valid usernames."""
    verify_password(pw, _DUMMY)


def make_token(user_id: int) -> str:
    exp = dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=SESSION_HOURS)
    return jwt.encode({"sub": str(user_id), "exp": exp}, SECRET, algorithm="HS256")


def read_token(token: str) -> int:
    return int(jwt.decode(token, SECRET, algorithms=["HS256"])["sub"])


class LoginLimiter:
    """In-memory: max_fails failures per (ip, username) in `window` seconds. With several server
    processes, or behind a proxy, back this with Redis / the proxy's real client IP instead."""

    def __init__(self, max_fails=5, window=900):
        self.max_fails, self.window = max_fails, window
        self.fails = defaultdict(deque)

    def _prune(self, key):
        q, now = self.fails[key], time.monotonic()
        while q and now - q[0] > self.window:
            q.popleft()
        return q

    def blocked(self, key) -> int:
        """Seconds until unblocked, or 0."""
        q = self._prune(key)
        if len(q) >= self.max_fails:
            return int(self.window - (time.monotonic() - q[0])) + 1
        return 0

    def record_fail(self, key):
        self._prune(key).append(time.monotonic())

    def reset(self, key):
        self.fails.pop(key, None)


limiter = LoginLimiter()
# Public grievance tracking: wrong Tracking ID / mobile pairs per client IP (stops guessing of ticket numbers).
track_limiter = LoginLimiter(max_fails=8, window=900)
