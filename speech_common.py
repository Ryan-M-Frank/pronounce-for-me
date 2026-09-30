"""Small platform-independent speech utilities."""
from datetime import datetime

def log(msg: str) -> None:
    """Print if we have a console (we may be running under pythonw.exe)."""
    try:
        print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)
    except Exception:
        pass


def parse_rate(value: object) -> int:
    """Speaking-rate setting -> percent change from normal (0 = normal).

    "-5%" style strings are used as-is. Bare numbers are the classic SAPI
    -10..10 scale, one unit being roughly 10%.
    """
    if isinstance(value, bool):
        return 0
    if isinstance(value, (int, float)):
        return int(round(value * 10))
    text = str(value).strip()
    if not text:
        return 0
    if text.endswith("%"):
        return int(round(float(text[:-1])))
    return int(round(float(text) * 10))
