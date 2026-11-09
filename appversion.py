
import re

VERSION = "1.1.0-pre"

_NUM = re.compile(r"^\d+")

_TAIL = r"(?:[-_ ]?(pre|rc|beta|alpha)[-_ ]?\d*)?"
_VER_V = re.compile(r"[vV](\d+(?:\.\d+){0,2})" + _TAIL)
_VER_D = re.compile(r"(?<![0-9.])(\d+\.\d+(?:\.\d+)?)" + _TAIL)

def find_version(text):
    s = str(text or "")
    m = _VER_V.search(s) or _VER_D.search(s)
    if not m:
        return ""
    return m.group(1) + (("-" + m.group(2).lower()) if m.group(2) else "")

def version_of(rel):
    if not isinstance(rel, dict):
        return ""
    for key in ("tag_name", "name", "body"):
        v = find_version(rel.get(key) or "")
        if v:
            return v
    return ""

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
        ver = version_of(rel)
        if not ver:
            continue
        _, ver_pre = parse_version(ver)
        if (ver_pre or rel.get("prerelease")) and not cur_pre:
            continue
        if is_newer(ver, current):
            return rel
    return None
