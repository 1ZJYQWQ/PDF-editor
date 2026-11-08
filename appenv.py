
import os
import sys

FROZEN = bool(getattr(sys, "frozen", False))

def resource_dir():
    if FROZEN:
        return getattr(sys, "_MEIPASS",
                       os.path.dirname(os.path.abspath(sys.executable)))
    return os.path.dirname(os.path.abspath(__file__))

def data_dir():
    if not FROZEN:
        return os.path.dirname(os.path.abspath(__file__))
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    return os.path.join(base, "PDFViewer")

def ensure_writable(*subs):
    root = data_dir()
    os.makedirs(root, exist_ok=True)
    for sub in subs:
        os.makedirs(os.path.join(root, sub), exist_ok=True)
    probe = os.path.join(root, ".write-test")
    with open(probe, "wb") as fp:
        fp.write(b"ok")
    os.remove(probe)
    return root
