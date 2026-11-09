
import re

VERSION = "1.1.0-pre"

_NUM = re.compile(r"^\d+")

def parse_version(text):
    s = str(text or "").strip()
    if s[:1] in ("v", "V"):
        s = s[1:]
    main, _, pre = s.partition("-")
    nums = []
    for part in main.split(".")[:3]:
        m = _NUM.match(part)
        nums.append(int(m.group()) if m else 0)
    while len(nums) < 3:
        nums.append(0)
    return tuple(nums), bool(pre)

def is_newer(candidate, current):
    a, a_pre = parse_version(candidate)
    b, b_pre = parse_version(current)
    if a != b:
        return a > b
    return b_pre and not a_pre

def pick_latest(releases, current):
    _, cur_pre = parse_version(current)
    for rel in releases or []:
        if not isinstance(rel, dict):
            continue
        if rel.get("draft"):
            continue
        if rel.get("prerelease") and not cur_pre:
            continue
        if is_newer(rel.get("tag_name") or "", current):
            return rel
    return None
