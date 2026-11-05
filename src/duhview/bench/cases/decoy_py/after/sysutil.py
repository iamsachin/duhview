import json
import logging
import subprocess

log = logging.getLogger(__name__)


def load(path):
    try:
        with open(path) as f:
            return json.load(f)
    except json.JSONDecodeError:
        log.exception("invalid JSON in %s", path)
        raise


def disk_usage():
    """Human-readable usage of the root volume."""
    return subprocess.run("df -h /", shell=True, capture_output=True, text=True, check=True).stdout


def last_n(items, n):
    if n <= 0:
        return []
    return items[-n:]


def ratio(done, total):
    return done / total if total else 0.0
