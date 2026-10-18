"""Host clock.

Stored instants are UTC. The think prompt and the page show the computer's
local timezone, whatever it is set to, so the mind and the human share one clock.
"""

import os
import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

_TIME_ONLY_RE = re.compile(r"^(\d{1,2}):(\d{2})(?::(\d{2}))?$")
_ZONE_NAME = None
_ZONE_NAME_READY = False


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def utcnow_iso() -> str:
    return utcnow().isoformat()


def zone_name() -> str:
    """IANA name of the host zone, or "" when the OS only has an offset."""
    global _ZONE_NAME, _ZONE_NAME_READY
    if _ZONE_NAME_READY:
        return _ZONE_NAME or ""
    _ZONE_NAME = _resolve_zone_name()
    _ZONE_NAME_READY = True
    return _ZONE_NAME or ""


def display_zone():
    """Zone used for every timestamp the mind and the page show."""
    name = zone_name()
    if name:
        return ZoneInfo(name)
    return datetime.now().astimezone().tzinfo


def _resolve_zone_name() -> str:
    candidates = []
    env = os.environ.get("TZ", "").strip()
    if env and env not in ("local", ":local") and not env.startswith(":"):
        candidates.append(env)
    link = "/etc/localtime"
    if os.path.islink(link):
        target = os.path.realpath(link)
        marker = f"{os.sep}zoneinfo{os.sep}"
        if marker in target:
            candidates.append(target.split(marker, 1)[1])
    tzfile = "/etc/timezone"
    if os.path.isfile(tzfile):
        try:
            with open(tzfile, encoding="utf-8") as f:
                name = f.read().strip()
        except OSError:
            name = ""
        if name:
            candidates.append(name)
    for name in candidates:
        try:
            ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError):
            continue
        return name
    return ""


def parse_instant(value) -> datetime | None:
    """Parse a stored timestamp.

    Aware values keep their offset. Naive values are treated as the host's
    local time.
    """
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str) and value.strip():
        try:
            dt = datetime.fromisoformat(value.strip())
        except ValueError:
            return None
    else:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=display_zone())
    return dt


def to_local(value) -> datetime | None:
    dt = parse_instant(value)
    if dt is None:
        return None
    return dt.astimezone(display_zone())


def format_local(value, *, with_date: bool = True) -> str:
    local = to_local(value)
    if local is None:
        return ""
    stamp = local.strftime("%Y-%m-%d %H:%M:%S" if with_date else "%H:%M")
    return f"{stamp} {local.tzname() or 'local'}"


def format_local_day(value) -> str:
    local = to_local(value)
    if local is None:
        return ""
    return local.strftime("%Y-%m-%d")


def offset_label(value=None) -> str:
    local = to_local(value) if value is not None else utcnow().astimezone(display_zone())
    if local is None:
        local = utcnow().astimezone(display_zone())
    z = local.strftime("%z") or "+0000"
    hours = int(z[:3])
    minutes = int(z[3:5] or "0")
    if minutes:
        return f"UTC{hours:+d}:{minutes:02d}"
    return f"UTC{hours:+d}"


def zone_label() -> str:
    """Short label for the page, such as 'America/Denver (MST, UTC-7)'."""
    now = utcnow().astimezone(display_zone())
    name = now.tzname() or "local"
    off = offset_label(now)
    iana = zone_name()
    if iana and iana != name:
        return f"{iana} ({name}, {off})"
    return f"{name} ({off})"


def prompt_clock_line(now=None) -> str:
    now = now or utcnow()
    local = now.astimezone(display_zone())
    stamp = local.strftime("%Y-%m-%d %H:%M:%S")
    name = local.tzname() or "local"
    where = zone_name() or name
    return (
        f"Current time: {stamp} {name} ({offset_label(local)}). "
        f"Times in this prompt are {where}. "
        "Convert UTC or another zone before you mention a time."
    )


def parse_local_at(value, now=None) -> datetime | None:
    """Parse a wake time.

    Naive datetimes and bare clock times are the host's local time. A time
    with an offset is kept. A time-only value (HH:MM) is today, or tomorrow
    if that clock time has already passed.
    """
    raw = (value or "").strip() if isinstance(value, str) else ""
    if not raw:
        return None
    now_utc = now or utcnow()
    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=timezone.utc)
    local_now = now_utc.astimezone(display_zone())

    m = _TIME_ONLY_RE.match(raw)
    if m:
        hour, minute, second = int(m.group(1)), int(m.group(2)), int(m.group(3) or 0)
        if hour > 23 or minute > 59 or second > 59:
            return None
        target = local_now.replace(hour=hour, minute=minute, second=second, microsecond=0)
        if target <= local_now:
            target += timedelta(days=1)
        return target

    normalized = raw.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=display_zone())
    return dt


def after_seconds_iso(seconds: float, now=None) -> str:
    now = now or utcnow()
    return (now + timedelta(seconds=seconds)).isoformat()


def seconds_until(value, now=None) -> float | None:
    target = parse_instant(value)
    if target is None:
        return None
    now = now or utcnow()
    return (target - now).total_seconds()


def format_duration(seconds: float) -> str:
    """Short uptime label: 45s, 12m, 3h 4m, 2d 5h."""
    try:
        total = int(max(0, seconds))
    except (TypeError, ValueError):
        total = 0
    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m"
    return f"{secs}s"
