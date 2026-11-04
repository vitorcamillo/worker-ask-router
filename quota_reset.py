"""Metadados de cota: somente datas, nunca o diagnostico bruto do provedor."""

import datetime as dt
import email.utils
import json
import math
import os
import re
import sys
import time

MAX_RESET_SECONDS = 366 * 86400
MAX_BYTES = 65536
MARKER = "worker-ask-quota-reset: "


def timestamp(value, now):
    if isinstance(value, bool):
        return None
    try:
        if isinstance(value, str):
            try:
                number = float(value)
            except ValueError:
                try:
                    number = dt.datetime.fromisoformat(
                        value.replace("Z", "+00:00")
                    ).timestamp()
                except ValueError:
                    date = email.utils.parsedate_to_datetime(value)
                    number = (
                        date.replace(tzinfo=dt.timezone.utc)
                        if date.tzinfo is None
                        else date
                    ).timestamp()
        else:
            number = float(value)
        if math.isfinite(number) and now < number <= now + MAX_RESET_SECONDS:
            return number
    except (ValueError, TypeError, OverflowError, AttributeError, OSError):
        pass
    return None


def parse_reset(raw, now=None):
    """Extrai datas de diagnosticos classificados como cota pelo wrapper."""
    now = time.time() if now is None else now
    if isinstance(raw, bytes):
        raw = raw[:MAX_BYTES].decode("utf-8", "replace")
    if not isinstance(raw, str):
        return None
    raw = raw[:MAX_BYTES]
    candidates = []

    def add(value, relative=False, milliseconds=False):
        if relative:
            try:
                if isinstance(value, bool):
                    return
                value = now + float(value) / (1000 if milliseconds else 1)
            except (ValueError, TypeError, OverflowError):
                return
        parsed = timestamp(value, now)
        if parsed is not None:
            candidates.append(parsed)

    def visit(obj, depth=0):
        if depth > 8:
            return
        if isinstance(obj, dict):
            for key, value in list(obj.items())[:128]:
                key = str(key).lower().replace("-", "_")
                if key in (
                    "reset_at",
                    "resets_at",
                    "reset_timestamp",
                    "resetsat",
                    "nextresettime",
                ):
                    if (
                        key == "nextresettime"
                        and isinstance(value, (int, float))
                        and not isinstance(value, bool)
                    ):
                        value /= 1000
                    add(value)
                elif key in ("retry_after", "retryafter", "retryafterms"):
                    add(value, relative=True, milliseconds=key == "retryafterms")
                    if isinstance(value, str) and key != "retryafterms":
                        try:
                            date = email.utils.parsedate_to_datetime(value)
                            add(
                                (
                                    date.replace(tzinfo=dt.timezone.utc)
                                    if date.tzinfo is None
                                    else date
                                ).timestamp()
                            )
                        except (
                            ValueError,
                            TypeError,
                            OverflowError,
                            AttributeError,
                            OSError,
                        ):
                            pass
                elif isinstance(value, (dict, list)):
                    visit(value, depth + 1)
        elif isinstance(obj, list):
            for item in obj[:128]:
                visit(item, depth + 1)

    for line in [raw, *raw.splitlines()[:128]]:
        try:
            visit(json.loads(line))
        except (ValueError, RecursionError):
            pass
    for value in re.findall(
        r'(?i)(?:reset(?:s)?(?:\s+at|_at|_timestamp)?|try again at)\s*[:=]?\s*["\']?(\d{4}-\d\d-\d\d(?:[T ]\d\d:\d\d(?::\d\d(?:\.\d+)?)?(?:Z|[+-]\d\d:\d\d)?)?)',
        raw,
    ):
        add(value)
    for value in re.findall(r"(?im)^retry-after:\s*([^\r\n]+)", raw):
        if re.fullmatch(r"\d+(?:\.\d+)?", value.strip()):
            add(value, relative=True)
        else:
            add(value)
    return max(candidates) if candidates else None


def marker_reset(raw, now=None):
    now = time.time() if now is None else now
    for line in raw.decode("ascii", "ignore").splitlines():
        if line.startswith(MARKER):
            result = timestamp(line[len(MARKER) :], now)
            if result is not None:
                return result
    return None


def main():
    try:
        if len(sys.argv) > 1:
            chunks = []
            for arg in sys.argv[1:5]:
                fd = os.open(arg, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                try:
                    import stat

                    if not stat.S_ISREG(os.fstat(fd).st_mode):
                        continue
                    chunks.append(os.read(fd, MAX_BYTES))
                finally:
                    os.close(fd)
            raw = b"\n".join(chunks)[:MAX_BYTES]
        else:
            raw = sys.stdin.buffer.read(MAX_BYTES)
        reset = parse_reset(raw)
        if reset is not None:
            print(MARKER + str(reset))
    except (OSError, ValueError):
        pass


if __name__ == "__main__":
    main()
