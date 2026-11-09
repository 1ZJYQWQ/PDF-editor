
import argparse
import hashlib
import json
import mimetypes
import os
import re
import shutil
import sys
import threading
import time
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pdfedit
import pdfredact
import appenv
import appversion

BASE_DIR = appenv.resource_dir()
DATA_DIR = appenv.data_dir()

STATIC_DIR = os.path.join(BASE_DIR, "static")
ASSETS_DIR = os.path.join(BASE_DIR, "assets")
ANNOT_DIR = os.path.join(DATA_DIR, "annotations")
EDIT_DIR = os.path.join(DATA_DIR, "edits")
EXPORT_DIR = os.path.join(DATA_DIR, "exports")

STAGE_DIR = os.path.join(DATA_DIR, "_stage")
STAGE_ID_RE = re.compile(r"^[0-9a-f]{8,32}$")
STAGE_KEEP_SEC = 6 * 3600

SCAN_DIRS = []

OPEN_FILES = set()

def stage_file(stage_id):
    return os.path.join(STAGE_DIR, stage_id + ".pdf")

STAGE_KEEP_VERSIONS = 20

def stage_version_path(stage_id, n):
    return os.path.join(STAGE_DIR, "%s.v%02d.pdf" % (stage_id, n))

def stage_versions(stage_id):
    out = []
    if not os.path.isdir(STAGE_DIR):
        return out
    prefix = stage_id + ".v"
    for name in os.listdir(STAGE_DIR):
        if not name.startswith(prefix) or not name.endswith(".pdf"):
            continue
        mid = name[len(prefix):-4]
        if mid.isdigit():
            out.append(int(mid))
    return sorted(out)

def backup_stage_version(stage_id):
    src = stage_file(stage_id)
    if not os.path.isfile(src):
        return None
    vers = stage_versions(stage_id)
    n = (vers[-1] + 1) if vers else 1
    dst = stage_version_path(stage_id, n)
    try:
        shutil.copy2(src, dst)
    except OSError:
        return None
    while len(vers) + 1 > STAGE_KEEP_VERSIONS:
        old = vers.pop(0)
        try:
            os.remove(stage_version_path(stage_id, old))
        except OSError:
            pass
    return n

def new_stage_id():
    return os.urandom(8).hex()

def allowed_roots(query=None, handler=None):
    if handler is not None:
        return handler._resolve_dirs(query or {}) + [STAGE_DIR, EXPORT_DIR]
    return list(SCAN_DIRS) + [STAGE_DIR, EXPORT_DIR]
PORT = 8000
PORT_SCAN_RANGE = 20

MAX_LIST = 2000
CHUNK = 64 * 1024
MAX_BODY = 8 * 1024 * 1024

_annot_lock = threading.Lock()
_edit_lock = threading.Lock()

UPDATE_REPO = "1ZJYQWQ/PDF-editor"
UPDATE_TTL = 6 * 3600
_update_cache = {"at": 0.0, "data": None}

def norm(path):
    return os.path.normcase(os.path.abspath(path))

def _clean_path(path):
    p = os.path.normcase(os.path.normpath(os.path.realpath(path)))
    if p.startswith("\\\\?\\"):
        p = p[len("\\\\?\\"):]
    return p

def is_within(path, roots):
    p = _clean_path(path)
    for r in roots:
        rn = _clean_path(r)
        if p == rn:
            return True
        try:
            if os.path.commonpath([rn, p]) == rn:
                return True
        except ValueError:
            continue
    return False

def is_allowed(real, query=None, handler=None):
    if is_within(real, allowed_roots(query, handler)):
        return True
    return any(_clean_path(real) == _clean_path(f) for f in OPEN_FILES)

def human_size(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024.0

def scan_pdfs(dirs):
    found = []
    seen = set()
    for root_dir in dirs:
        if not os.path.isdir(root_dir):
            continue
        for dirpath, dirnames, filenames in os.walk(root_dir):
            dirnames[:] = [
                d for d in dirnames
                if not d.startswith(".") and d.lower() not in
                {"node_modules", "__pycache__", "venv", ".git", "static", "annotations"}
            ]
            for name in filenames:
                if not name.lower().endswith(".pdf"):
                    continue
                full = os.path.join(dirpath, name)
                key = norm(full)
                if key in seen:
                    continue
                seen.add(key)
                try:
                    st = os.stat(full)
                except OSError:
                    continue
                found.append({
                    "path": full,
                    "name": os.path.splitext(name)[0],
                    "folder": os.path.relpath(dirpath, root_dir) if dirpath != root_dir else ".",
                    "root": root_dir,
                    "size": st.st_size,
                    "sizeText": human_size(st.st_size),
                    "mtime": st.st_mtime,
                    "docId": doc_id(full, st.st_size),
                })
                if len(found) >= MAX_LIST:
                    return found
    found.sort(key=lambda x: x["mtime"], reverse=True)
    return found

def doc_id(path, size=None):
    try:
        if size is None:
            size = os.path.getsize(path)
    except OSError:
        size = 0
    base = os.path.basename(path)
    raw = f"{base}|{size}".encode("utf-8")
    return hashlib.sha1(raw).hexdigest()[:16]

def annot_path(did):
    return os.path.join(ANNOT_DIR, f"{did}.json")

def valid_doc_id(did):
    return bool(did) and re.fullmatch(r"[0-9a-f]{16}", did or "")

def load_annotations(did):
    p = annot_path(did)
    if not os.path.isfile(p):
        return {"ok": True, "docId": did, "items": [], "updated": 0}
    try:
        with open(p, "r", encoding="utf-8") as fp:
            data = json.load(fp)
        if not isinstance(data, dict):
            raise ValueError("格式错误")
        data.setdefault("items", [])
        data["ok"] = True
        data["docId"] = did
        return data
    except Exception as exc:
        return {"ok": False, "error": f"读取标注失败: {exc}", "items": []}

def save_annotations(did, items, meta=None):
    os.makedirs(ANNOT_DIR, exist_ok=True)
    payload = {
        "docId": did,
        "updated": time.time(),
        "items": items,
    }
    if meta:
        payload["doc"] = meta
    tmp = annot_path(did) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fp:
        json.dump(payload, fp, ensure_ascii=False, indent=2)
    os.replace(tmp, annot_path(did))
    return payload

TAG_LABEL = {
    "highlight": "高亮",
    "underline": "下划线",
    "wavy": "波浪线",
    "strike": "删除线",
    "note": "笔记",
    "box": "区域",
}

EDITABLE_KINDS = {"whiteout", "text", "rect", "ellipse", "line"}

ANNOT_KINDS = {"highlight", "underline", "wavy", "strike", "box", "note"}

REDACT_KIND = "redact"

def edit_path(did):
    return os.path.join(EDIT_DIR, f"{did}.json")

def empty_edits(did):
    return {
        "ok": True,
        "docId": did,
        "pages": {"order": None, "delete": [], "rotate": {}, "insert": []},
        "objects": [],
        "updated": 0,
    }

def load_edits(did):
    p = edit_path(did)
    if not os.path.isfile(p):
        return empty_edits(did)
    try:
        with open(p, "r", encoding="utf-8") as fp:
            data = json.load(fp)
        if not isinstance(data, dict):
            raise ValueError("格式错误")
        data.setdefault("pages", {})
        data.setdefault("objects", [])
        data["ok"] = True
        data["docId"] = did
        return data
    except Exception as exc:
        return {"ok": False, "error": f"读取编辑数据失败: {exc}",
                "pages": {}, "objects": []}

def save_edits(did, pages, objects, meta=None):
    os.makedirs(EDIT_DIR, exist_ok=True)
    payload = {
        "docId": did,
        "updated": time.time(),
        "pages": pages or {},
        "objects": objects or [],
    }
    if meta:
        payload["doc"] = meta
    tmp = edit_path(did) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fp:
        json.dump(payload, fp, ensure_ascii=False, indent=2)
    os.replace(tmp, edit_path(did))
    return payload

def sanitize_edits(pages, objects):
    dropped = 0

    clean_pages = {"order": None, "delete": [], "rotate": {}, "insert": []}
    if isinstance(pages, dict):
        order = pages.get("order")
        if isinstance(order, list):
            nums = []
            for v in order:
                try:
                    i = int(v)
                except (TypeError, ValueError):
                    dropped += 1
                    continue
                if i < 0:
                    dropped += 1
                    continue
                nums.append(i)
            clean_pages["order"] = nums or None

        for v in (pages.get("delete") or []):
            try:
                i = int(v)
                if i >= 0:
                    clean_pages["delete"].append(i)
                else:
                    dropped += 1
            except (TypeError, ValueError):
                dropped += 1

        rots = pages.get("rotate") or {}
        if isinstance(rots, dict):
            for k, v in rots.items():
                try:
                    ki, vi = int(k), int(v)
                except (TypeError, ValueError):
                    dropped += 1
                    continue
                if ki < 0 or vi % 90 != 0:
                    dropped += 1
                    continue
                clean_pages["rotate"][str(ki)] = vi % 360

        for spec in (pages.get("insert") or []):
            if not isinstance(spec, dict):
                dropped += 1
                continue
            try:
                at = int(spec.get("at", 0))
            except (TypeError, ValueError):
                dropped += 1
                continue
            w = _safe_float(spec.get("width"), 595.0)
            h = _safe_float(spec.get("height"), 842.0)
            w = min(max(w, 10.0), 20000.0)
            h = min(max(h, 10.0), 20000.0)
            clean_pages["insert"].append({"at": max(0, at),
                                          "width": w, "height": h})

    clean_objs = []
    for obj in (objects or []):
        if not isinstance(obj, dict):
            dropped += 1
            continue
        kind = str(obj.get("kind") or "").lower()
        if kind not in EDITABLE_KINDS and kind != REDACT_KIND:
            dropped += 1
            continue
        try:
            page = int(obj.get("page", 1))
        except (TypeError, ValueError):
            dropped += 1
            continue
        if page < 1:
            dropped += 1
            continue

        out = {
            "page": page,
            "kind": kind,
            "x": _safe_float(obj.get("x"), 0.0),
            "y": _safe_float(obj.get("y"), 0.0),
            "w": _safe_float(obj.get("w"), 0.0),
            "h": _safe_float(obj.get("h"), 0.0),
        }

        if kind == "line":
            out["x2"] = _safe_float(obj.get("x2"), 0.0)
            out["y2"] = _safe_float(obj.get("y2"), 0.0)
        if kind in ("rect", "ellipse"):
            out["fill"] = bool(obj.get("fill"))
            out["stroke"] = bool(obj.get("stroke", True))
            out["lineWidth"] = min(max(_safe_float(obj.get("lineWidth"), 1.0),
                                       0.1), 40.0)
        if kind == "text":
            txt = str(obj.get("text") or "")
            if not txt:
                dropped += 1
                continue
            out["text"] = txt[:4000]
            out["size"] = min(max(_safe_float(obj.get("size"), 12.0),
                                  4.0), 200.0)
            out["font"] = _safe_font(obj.get("font"))

        out["color"] = _safe_color(obj.get("color"))
        clean_objs.append(out)

    return clean_pages, clean_objs, dropped

def sanitize_annotations(items, max_items=5000):
    dropped = 0
    clean = []
    for it in (items or [])[:max_items]:
        if not isinstance(it, dict):
            dropped += 1
            continue
        kind = str(it.get("type") or "").lower()
        if kind not in ANNOT_KINDS:
            dropped += 1
            continue
        try:
            page = int(it.get("page", 0))
        except (TypeError, ValueError):
            dropped += 1
            continue
        if page < 1:
            dropped += 1
            continue

        rects = []
        for r in (it.get("rects") or [])[:400]:
            if not isinstance(r, (list, tuple)) or len(r) != 4:
                continue
            try:
                x0, y0, x1, y1 = (float(v) for v in r)
            except (TypeError, ValueError):
                continue
            if not all(abs(v) < 1e6 for v in (x0, y0, x1, y1)):
                continue
            if abs(x1 - x0) < 0.1 or abs(y1 - y0) < 0.1:
                continue
            rects.append([round(min(x0, x1), 2), round(min(y0, y1), 2),
                          round(max(x0, x1), 2), round(max(y0, y1), 2)])
        if not rects:
            dropped += 1
            continue

        color = it.get("color")
        color = _safe_color(color) if isinstance(color, str) else "#f59e0b"
        clean.append({
            "page": page,
            "type": kind,
            "rects": rects,
            "color": color,
            "note": str(it.get("note") or "")[:2000],
        })
    return clean, dropped

def unique_out_path(path):
    if not os.path.exists(path):
        return path
    stem, ext = os.path.splitext(path)
    i = 2
    while True:
        cand = "%s-%d%s" % (stem, i, ext)
        if not os.path.exists(cand):
            return cand
        i += 1

def sanitize_redactions(regions):
    dropped = 0
    clean = []
    for r in (regions or []):
        if not isinstance(r, dict):
            dropped += 1
            continue
        try:
            page = int(r.get("page", 1))
        except (TypeError, ValueError):
            dropped += 1
            continue
        if page < 1:
            dropped += 1
            continue

        x = _safe_float(r.get("x"), 0.0)
        y = _safe_float(r.get("y"), 0.0)
        w = _safe_float(r.get("w"), 0.0)
        h = _safe_float(r.get("h"), 0.0)
        w = min(max(w, 1.0), 20000.0)
        h = min(max(h, 1.0), 20000.0)
        clean.append({"page": page, "x": x, "y": y, "w": w, "h": h})

    return clean, dropped

def _safe_float(v, default=0.0):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    if f != f or f in (float("inf"), float("-inf")):
        return default
    return f

def _safe_color(v):
    if not isinstance(v, str):
        return "#000000"
    s = v.strip()
    if re.fullmatch(r"#[0-9a-fA-F]{3}", s) or re.fullmatch(r"#[0-9a-fA-F]{6}", s):
        return s
    m = re.fullmatch(r"rgb\(\s*(\d{1,3})\s*,\s*(\d{1,3})\s*,\s*(\d{1,3})\s*\)", s)
    if m:
        vals = [min(255, int(x)) for x in m.groups()]
        return "#%02x%02x%02x" % tuple(vals)
    return "#000000"

def _safe_font(v):
    allowed = {
        "Helvetica", "Helvetica-Bold", "Helvetica-Oblique", "Times-Roman",
        "Times-Bold", "Courier", "Courier-Bold",
    }
    s = str(v or "").strip()
    return s if s in allowed else "Helvetica"

def pdf_page_info(path):
    with open(path, "rb") as fp:
        data = fp.read()
    doc = pdfedit.PDFDocument.load(data)
    refs = doc.page_refs()
    pages = []
    for i, ref in enumerate(refs):
        mb = doc.inherited(ref, "MediaBox")
        if isinstance(mb, list) and len(mb) == 4:
            x0 = pdfedit._num(mb[0])
            y0 = pdfedit._num(mb[1])
            x1 = pdfedit._num(mb[2])
            y1 = pdfedit._num(mb[3])
        else:
            x0, y0, x1, y1 = 0.0, 0.0, 595.0, 842.0
        rot = doc.inherited(ref, "Rotate", 0) or 0
        try:
            rot = int(rot) % 360
        except (TypeError, ValueError):
            rot = 0
        pages.append({
            "index": i,
            "number": i + 1,
            "x": x0, "y": y0,
            "width": x1 - x0,
            "height": y1 - y0,
            "rotate": rot,
        })
    return {"ok": True, "count": len(pages), "pages": pages}

def export_markdown(did, doc_name=""):
    data = load_annotations(did)
    items = data.get("items", [])
    lines = [f"# {doc_name or did} 阅读笔记", ""]
    lines.append(f"> 导出时间：{time.strftime('%Y-%m-%d %H:%M')}")
    lines.append(f"> 共 {len(items)} 条标注")
    lines.append("")

    if not items:
        lines.append("_暂无标注_")
        return "\n".join(lines)

    by_page = {}
    for it in items:
        by_page.setdefault(it.get("page", 1), []).append(it)

    for page in sorted(by_page):
        lines.append(f"## 第 {page} 页")
        lines.append("")
        for it in by_page[page]:
            tag = TAG_LABEL.get(it.get("type", ""), it.get("type", ""))
            text = (it.get("text") or "").replace("\n", " ").strip()
            color = it.get("color", "")
            head = f"- **[{tag}]**" + (f" `{color}`" if color else "")
            lines.append(head)
            if text:
                lines.append(f"  > {text}")
            if it.get("note"):
                lines.append(f"  ")
                lines.append(f"  {it['note']}")
            lines.append("")
    return "\n".join(lines)

class Handler(BaseHTTPRequestHandler):
    server_version = "PDFViewer/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        msg = fmt % args
        if "/static/" in msg or "/api/file" in msg:
            return
        sys.stderr.write(f"  {msg}\n")

    def send_json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_text(self, text, status=200, ctype="text/plain; charset=utf-8"):
        body = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_error_json(self, status, message):
        self.send_json({"ok": False, "error": message, "code": message}, status=status)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        route = parsed.path
        query = urllib.parse.parse_qs(parsed.query)

        try:
            if route == "/":
                return self.serve_file(os.path.join(STATIC_DIR, "index.html"))
            if route == "/api/ping":
                return self.send_json({
                    "app": "pdfviewer", "ok": True,
                    "version": appversion.VERSION,
                })
            if route == "/api/files":
                return self.api_files(query)
            if route == "/api/update":
                return self.api_update(query)
            if route == "/api/file":
                return self.api_file(query)
            if route == "/api/open":
                return self.api_open(query)
            if route == "/api/annotations":
                return self.api_get_annotations(query)
            if route == "/api/notes":
                return self.api_notes(query)
            if route == "/api/edits":
                return self.api_get_edits(query)
            if route == "/api/pdfinfo":
                return self.api_pdfinfo(query)
            if route == "/api/paragraphs":
                return self.api_paragraphs(query)
            if route.startswith("/static/"):
                rel = urllib.parse.unquote(route[len("/static/"):])
                target = os.path.join(STATIC_DIR, rel.replace("/", os.sep))
                if not is_within(target, [STATIC_DIR]):
                    return self.send_error_json(403, "拒绝访问")
                return self.serve_file(target)
            if route.startswith("/assets/"):
                rel = urllib.parse.unquote(route[len("/assets/"):])
                target = os.path.join(ASSETS_DIR, rel.replace("/", os.sep))
                if not is_within(target, [ASSETS_DIR]):
                    return self.send_error_json(403, "拒绝访问")
                return self.serve_file(target)
            return self.send_error_json(404, "未找到该路径")
        except BrokenPipeError:
            pass
        except Exception as exc:
            self.log_message("ERROR %s", exc)
            try:
                self.send_error_json(500, f"服务器错误: {exc}")
            except Exception:
                pass

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        route = parsed.path
        if route not in ("/api/annotations", "/api/edits", "/api/export",
                         "/api/redact", "/api/replace", "/api/reflow",
                         "/api/stage", "/api/stage/undo",
                         "/api/find", "/api/replace_all", "/api/redact_terms",
                         "/api/table/drop_row", "/api/table/insert_row",
                         "/api/new", "/api/allow",
                         "/api/split", "/api/merge"):
            return self.send_error_json(404, "未找到该路径")
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0 or length > MAX_BODY:
            return self.send_error_json(413, "请求体过大或为空")

        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except Exception:
            return self.send_error_json(400, "JSON 解析失败")

        if route == "/api/annotations":
            return self._post_annotations(payload)
        if route == "/api/edits":
            return self._post_edits(payload)
        if route == "/api/redact":
            return self._post_redact(payload)
        if route == "/api/replace":
            return self._post_replace(payload)
        if route == "/api/reflow":
            return self._post_reflow(payload)
        if route == "/api/stage":
            return self._post_stage(payload)
        if route == "/api/stage/undo":
            return self._post_stage_undo(payload)
        if route == "/api/table/drop_row":
            return self._post_drop_row(payload)
        if route == "/api/table/insert_row":
            return self._post_insert_row(payload)
        if route == "/api/new":
            return self._post_new(payload)
        if route == "/api/allow":
            return self._post_allow(payload)
        if route == "/api/find":
            return self._post_find(payload)
        if route == "/api/replace_all":
            return self._post_replace_all(payload)
        if route == "/api/redact_terms":
            return self._post_redact_terms(payload)
        if route == "/api/split":
            return self._post_split(payload)
        if route == "/api/merge":
            return self._post_merge(payload)
        return self._post_export(payload)

    def _post_annotations(self, payload):
        did = payload.get("docId")
        if not valid_doc_id(did):
            return self.send_error_json(400, "docId 无效")

        items = payload.get("items")
        if not isinstance(items, list):
            return self.send_error_json(400, "items 必须是数组")

        try:
            with _annot_lock:
                saved = save_annotations(did, items, payload.get("doc"))
            self.send_json({"ok": True, "count": len(items),
                            "updated": saved["updated"]})
        except Exception as exc:
            self.send_error_json(500, f"保存失败: {exc}")

    def _post_edits(self, payload):
        did = payload.get("docId")
        if not valid_doc_id(did):
            return self.send_error_json(400, "docId 无效")

        pages, objects, dropped = sanitize_edits(
            payload.get("pages"), payload.get("objects"))
        try:
            with _edit_lock:
                saved = save_edits(did, pages, objects, payload.get("doc"))
            self.send_json({
                "ok": True,
                "objects": len(objects),
                "pages": len(pages.get("insert") or []) + len(pages.get("delete") or []),
                "dropped": dropped,
                "updated": saved["updated"],
            })
        except Exception as exc:
            self.send_error_json(500, f"保存失败: {exc}")

    def _post_drop_row(self, payload):
        src = urllib.parse.unquote(str(payload.get("path") or ""))
        if not src:
            return self.send_error_json(400, "缺少 path 参数")
        if payload.get("overwrite") or payload.get("inPlace"):
            return self.send_error_json(400, "不允许覆盖原文件：只能另存为新文件")

        try:
            page = int(payload.get("page") or 1)
            para = int(payload.get("para") if payload.get("para") is not None else -1)
            gid = int(payload.get("gid") if payload.get("gid") is not None else -1)
        except (TypeError, ValueError):
            return self.send_error_json(400, "page / para / gid 参数无效")
        if page < 1 or para < 0 or gid < 0:
            return self.send_error_json(400, "page / para / gid 参数无效")

        stage = payload.get("stage")
        if stage is not None:
            if not isinstance(stage, str) or not STAGE_ID_RE.match(stage):
                return self.send_error_json(400, "stage 参数无效")

        try:
            if stage:
                real = stage_file(stage)
                if not os.path.isfile(real):
                    return self.send_error_json(
                        404, "预览会话已失效（暂存文件可能被清理），请重新开始")
            else:
                real = os.path.realpath(src)
                if not os.path.isfile(real):
                    return self.send_error_json(404, "源文件不存在")
                if not is_allowed(real):
                    return self.send_error_json(403, "该文件不在允许的扫描目录内")
        except OSError:
            return self.send_error_json(404, "源文件不存在")

        print("[drop-row] page=%d para=%d gid=%d stage=%s"
              % (page, para, gid, stage or "-"), file=sys.stderr, flush=True)

        os.makedirs(STAGE_DIR, exist_ok=True)
        if stage:
            out_id = stage
            out_path = os.path.join(STAGE_DIR, stage + ".next.pdf")
        else:
            out_id = new_stage_id()
            out_path = stage_file(out_id)

        hint = payload.get("name")
        if isinstance(hint, str) and hint.strip():
            stem_src = hint.strip()
        else:
            stem_src = os.path.splitext(os.path.basename(src))[0]
        base_stem = re.sub(r'[\\/:*?"<>|]', "_", stem_src)[:80] or "文档"

        try:
            import pdfreflow
            with _edit_lock:
                result = pdfreflow.delete_table_row(real, out_path, page, para, gid)
        except ValueError as exc:
            try:
                if os.path.exists(out_path):
                    os.remove(out_path)
            except OSError:
                pass
            return self.send_error_json(400, str(exc))
        except Exception as exc:
            try:
                if os.path.exists(out_path):
                    os.remove(out_path)
            except OSError:
                pass
            return self.send_error_json(500, "删行失败: %s" % exc)

        try:
            with open(out_path, "rb") as fp:
                body = fp.read()
        except OSError as exc:
            return self.send_error_json(500, "读取产物失败: %s" % exc)

        if stage:
            backup_stage_version(stage)
            try:
                os.replace(out_path, stage_file(stage))
            except OSError as exc:
                return self.send_error_json(500, "暂存覆盖失败: %s" % exc)

        self.send_response(200)
        self.send_header("Content-Type", "application/pdf")
        self.send_header("Connection", "close")
        self.close_connection = True
        self.send_header("Content-Length", str(len(body)))
        self.send_header(
            "Content-Disposition",
            "inline; filename*=UTF-8''" + urllib.parse.quote(
                "%s-删行.pdf" % base_stem))
        self.send_header("X-Stage-Id", out_id)
        self.send_header("X-Stage-Path", urllib.parse.quote(
            stage_file(out_id), safe=""))
        self.send_header("X-Drop-Text", urllib.parse.quote(
            str(result.get("text") or "")[:200], safe=""))
        self.send_header("X-Drop-Height", str(result.get("height") or 0))
        self.send_header("X-Drop-Rows", str(result.get("moved_rows") or 0))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _post_allow(self, payload):
        target = str(payload.get("path") or "").strip()
        if not target:
            return self.send_error_json(400, "缺少 path 参数")
        real = os.path.realpath(target)
        if not os.path.isfile(real):
            return self.send_error_json(404, "文件不存在")
        if not real.lower().endswith(".pdf"):
            return self.send_error_json(400, "只允许放行 PDF 文件")
        OPEN_FILES.add(real)
        self.send_json({"ok": True, "path": real})

    def _post_new(self, payload):
        size = payload.get("size") or "a4"
        landscape = bool(payload.get("landscape"))
        raw_pages = payload.get("pages")
        try:
            pages = 1 if raw_pages is None else int(raw_pages)
        except (TypeError, ValueError):
            return self.send_error_json(400, "页数必须是整数")

        try:
            import pdfnew
            body = pdfnew.build_blank_pdf(pages, size, landscape)
        except ValueError as exc:
            return self.send_error_json(400, str(exc))

        os.makedirs(STAGE_DIR, exist_ok=True)
        sid = new_stage_id()
        path = stage_file(sid)
        try:
            with open(path, "wb") as fp:
                fp.write(body)
        except OSError as exc:
            return self.send_error_json(500, "写入失败: %s" % exc)

        print("[new] size=%s pages=%d land=%s stage=%s"
              % (size, pages, landscape, sid), file=sys.stderr, flush=True)

        self.send_response(200)
        self.send_header("Content-Type", "application/pdf")
        self.send_header("Connection", "close")
        self.close_connection = True
        self.send_header("Content-Length", str(len(body)))
        self.send_header(
            "Content-Disposition",
            "inline; filename*=UTF-8''" + urllib.parse.quote("未命名.pdf"))
        self.send_header("X-Stage-Id", sid)
        self.send_header("X-Stage-Path", urllib.parse.quote(path, safe=""))
        self.send_header("X-New-Pages", str(pages))
        self.send_header("X-New-Name", urllib.parse.quote("未命名", safe=""))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _post_insert_row(self, payload):
        src = urllib.parse.unquote(str(payload.get("path") or ""))
        if not src:
            return self.send_error_json(400, "缺少 path 参数")
        if payload.get("overwrite") or payload.get("inPlace"):
            return self.send_error_json(400, "不允许覆盖原文件：只能另存为新文件")

        where = str(payload.get("where") or "below").strip().lower()
        if where not in ("above", "below"):
            return self.send_error_json(400, "where 只能是 above 或 below")

        try:
            page = int(payload.get("page") or 1)
            para = int(payload.get("para") if payload.get("para") is not None else -1)
            gid = int(payload.get("gid") if payload.get("gid") is not None else -1)
        except (TypeError, ValueError):
            return self.send_error_json(400, "page / para / gid 参数无效")
        if page < 1 or para < 0 or gid < 0:
            return self.send_error_json(400, "page / para / gid 参数无效")

        stage = payload.get("stage")
        if stage is not None:
            if not isinstance(stage, str) or not STAGE_ID_RE.match(stage):
                return self.send_error_json(400, "stage 参数无效")

        try:
            if stage:
                real = stage_file(stage)
                if not os.path.isfile(real):
                    return self.send_error_json(
                        404, "预览会话已失效（暂存文件可能被清理），请重新开始")
            else:
                real = os.path.realpath(src)
                if not os.path.isfile(real):
                    return self.send_error_json(404, "源文件不存在")
                if not is_allowed(real):
                    return self.send_error_json(403, "该文件不在允许的扫描目录内")
        except OSError:
            return self.send_error_json(404, "源文件不存在")

        print("[insert-row] page=%d para=%d gid=%d stage=%s"
              % (page, para, gid, stage or "-"), file=sys.stderr, flush=True)

        os.makedirs(STAGE_DIR, exist_ok=True)
        if stage:
            out_id = stage
            out_path = os.path.join(STAGE_DIR, stage + ".next.pdf")
        else:
            out_id = new_stage_id()
            out_path = stage_file(out_id)

        hint = payload.get("name")
        if isinstance(hint, str) and hint.strip():
            stem_src = hint.strip()
        else:
            stem_src = os.path.splitext(os.path.basename(src))[0]
        base_stem = re.sub(r'[\\/:*?"<>|]', "_", stem_src)[:80] or "文档"

        try:
            import pdfreflow
            with _edit_lock:
                result = pdfreflow.insert_table_row(real, out_path, page, para,
                                                    gid, where=where)
        except ValueError as exc:
            try:
                if os.path.exists(out_path):
                    os.remove(out_path)
            except OSError:
                pass
            return self.send_error_json(400, str(exc))
        except Exception as exc:
            try:
                if os.path.exists(out_path):
                    os.remove(out_path)
            except OSError:
                pass
            return self.send_error_json(500, "插入行失败: %s" % exc)

        try:
            with open(out_path, "rb") as fp:
                body = fp.read()
        except OSError as exc:
            return self.send_error_json(500, "读取产物失败: %s" % exc)

        if stage:
            backup_stage_version(stage)
            try:
                os.replace(out_path, stage_file(stage))
            except OSError as exc:
                return self.send_error_json(500, "暂存覆盖失败: %s" % exc)

        self.send_response(200)
        self.send_header("Content-Type", "application/pdf")
        self.send_header("Connection", "close")
        self.close_connection = True
        self.send_header("Content-Length", str(len(body)))
        self.send_header(
            "Content-Disposition",
            "inline; filename*=UTF-8''" + urllib.parse.quote(
                "%s-插行.pdf" % base_stem))
        self.send_header("X-Stage-Id", out_id)
        self.send_header("X-Stage-Path", urllib.parse.quote(
            stage_file(out_id), safe=""))
        self.send_header("X-Insert-Height", str(result.get("height") or 0))
        self.send_header("X-Insert-Rows", str(result.get("moved_rows") or 0))
        self.send_header("X-Insert-Lines", str(result.get("vlines") or 0))
        self.send_header("X-Insert-Where", where)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _post_find(self, payload):
        src = urllib.parse.unquote(str(payload.get("path") or ""))
        if not src:
            return self.send_error_json(400, "缺少 path 参数")

        term = payload.get("term")
        if not isinstance(term, str) or not term:
            return self.send_error_json(400, "查找内容不能为空")

        try:
            real = os.path.realpath(src)
            if not os.path.isfile(real):
                return self.send_error_json(404, "文件不存在")
            if not is_allowed(real):
                return self.send_error_json(403, "该文件不在允许的扫描目录内")
        except OSError:
            return self.send_error_json(404, "文件不存在")

        try:
            import pdfbatch
            res = pdfbatch.find_text(real, term,
                                     bool(payload.get("ignore_case")))
        except ValueError as exc:
            return self.send_error_json(400, str(exc))
        except Exception as exc:
            return self.send_error_json(500, "查找失败: %s" % exc)

        res["ok"] = True
        res["term"] = term
        self.send_json(res)

    def _post_replace_all(self, payload):
        src = urllib.parse.unquote(str(payload.get("path") or ""))
        if not src:
            return self.send_error_json(400, "缺少 path 参数")
        if payload.get("overwrite") or payload.get("inPlace"):
            return self.send_error_json(
                400, "不允许覆盖原文件：只能另存为新文件")

        term = payload.get("term")
        repl = payload.get("repl")
        if not isinstance(term, str) or not term:
            return self.send_error_json(400, "查找内容不能为空")
        if not isinstance(repl, str):
            return self.send_error_json(400, "替换内容无效")
        if len(term) > 500 or len(repl) > 2000:
            return self.send_error_json(400, "查找/替换内容太长了")

        targets = payload.get("targets")
        if targets is not None and not isinstance(targets, list):
            return self.send_error_json(400, "targets 必须是数组")

        stage = payload.get("stage")
        if stage is not None:
            if not isinstance(stage, str) or not STAGE_ID_RE.match(stage):
                return self.send_error_json(400, "stage 参数无效")

        try:
            if stage:
                real = stage_file(stage)
                if not os.path.isfile(real):
                    return self.send_error_json(
                        404, "预览会话已失效（暂存文件可能被清理），请重新开始")
            else:
                real = os.path.realpath(src)
                if not os.path.isfile(real):
                    return self.send_error_json(404, "源文件不存在")
                if not is_allowed(real):
                    return self.send_error_json(403, "该文件不在允许的扫描目录内")
        except OSError:
            return self.send_error_json(404, "源文件不存在")

        os.makedirs(STAGE_DIR, exist_ok=True)
        if stage:
            out_id = stage
            out_path = os.path.join(STAGE_DIR, stage + ".next.pdf")
        else:
            out_id = new_stage_id()
            out_path = stage_file(out_id)

        hint = payload.get("name")
        if isinstance(hint, str) and hint.strip():
            stem_src = hint.strip()
        else:
            stem_src = os.path.splitext(os.path.basename(src))[0]
        base_stem = re.sub(r'[\\/:*?"<>|]', "_", stem_src)[:80] or "文档"

        print("[replace_all] term=%r repl=%r stage=%s targets=%s"
              % (term[:20], repl[:20], stage or "-",
                 len(targets) if targets else "all"),
              file=sys.stderr, flush=True)

        try:
            import pdfbatch
            with _edit_lock:
                result = pdfbatch.replace_all(
                    real, out_path, term, repl,
                    ignore_case=bool(payload.get("ignore_case")),
                    targets=targets)
        except ValueError as exc:
            try:
                if os.path.exists(out_path):
                    os.remove(out_path)
            except OSError:
                pass
            return self.send_error_json(400, str(exc))
        except Exception as exc:
            try:
                if os.path.exists(out_path):
                    os.remove(out_path)
            except OSError:
                pass
            return self.send_error_json(500, "替换失败: %s" % exc)

        try:
            with open(out_path, "rb") as fp:
                body = fp.read()
        except OSError as exc:
            return self.send_error_json(500, "读取产物失败: %s" % exc)

        if stage:
            backup_stage_version(stage)
            try:
                os.replace(out_path, stage_file(stage))
            except OSError as exc:
                return self.send_error_json(500, "暂存覆盖失败: %s" % exc)

        self.send_response(200)
        self.send_header("Content-Type", "application/pdf")
        self.send_header("Connection", "close")
        self.close_connection = True
        self.send_header("Content-Length", str(len(body)))
        self.send_header(
            "Content-Disposition",
            "inline; filename*=UTF-8''" + urllib.parse.quote(
                "%s-替换.pdf" % base_stem))
        self.send_header("X-Stage-Id", out_id)
        self.send_header("X-Stage-Path", urllib.parse.quote(
            stage_file(out_id), safe=""))
        self.send_header("X-Replace-Edits", str(result["edits"]))
        self.send_header("X-Replace-Occurrences", str(result["occurrences"]))
        skipped = result.get("skipped") or []
        if skipped:
            self.send_header("X-Replace-Skipped",
                             urllib.parse.quote("；".join(skipped[:5]), safe=""))
        self.end_headers()
        self.wfile.write(body)

    def _post_delete_terms(self, real, payload):
        try:
            import pdfbatch
            os.makedirs(EXPORT_DIR, exist_ok=True)
            stem = re.sub(r'[\\/:*?"<>|]', "_", os.path.splitext(
                os.path.basename(real))[0])
            stamp = time.strftime("%Y%m%d-%H%M%S")
            out_path = unique_out_path(
                os.path.join(EXPORT_DIR, f"{stem}-已删除-{stamp}.pdf"))
            with _edit_lock:
                res = pdfbatch.delete_terms(
                    real, out_path, payload.get("terms"),
                    bool(payload.get("ignore_case", True)))
        except ValueError as exc:
            return self.send_error_json(400, str(exc))
        except Exception as exc:
            return self.send_error_json(500, "删除失败: %s" % exc)

        missing = res.get("missing") or []
        residue = sum(n for n in (res.get("residue") or {}).values() if n > 0)
        try:
            with open(out_path, "rb") as fp:
                body = fp.read()
        except OSError as exc:
            return self.send_error_json(500, "读取输出文件失败: %s" % exc)

        self.send_response(200)
        self.send_header("Content-Type", "application/pdf")
        self.send_header("Connection", "close")
        self.close_connection = True
        self.send_header("Content-Length", str(len(body)))
        self.send_header(
            "Content-Disposition",
            "attachment; filename*=UTF-8''" + urllib.parse.quote(
                os.path.basename(out_path)))
        self.send_header("X-Out-Path", urllib.parse.quote(out_path, safe=""))
        self.send_header("X-Delete-Edits", str(res.get("edits", 0)))
        self.send_header("X-Delete-Removed", str(res.get("removed", 0)))
        self.send_header("X-Delete-Total", str(res.get("total", 0)))
        self.send_header("X-Delete-Remaining", str(res.get("remaining", 0)))
        self.send_header("X-Delete-Residue", str(residue))
        if missing:
            self.send_header("X-Delete-Missing",
                             urllib.parse.quote("、".join(missing), safe=""))
        if res.get("skipped"):
            self.send_header("X-Delete-Skipped",
                             urllib.parse.quote(
                                 "；".join(res["skipped"][:3]), safe=""))
        self.end_headers()
        self.wfile.write(body)

    def _post_redact_terms(self, payload):
        src = urllib.parse.unquote(str(payload.get("path") or ""))
        if not src:
            return self.send_error_json(400, "缺少 path 参数")
        if payload.get("overwrite") or payload.get("inPlace"):
            return self.send_error_json(
                400, "涂黑操作不允许覆盖原文件，只支持另存为")

        try:
            real = os.path.realpath(src)
            if not os.path.isfile(real):
                return self.send_error_json(404, "源文件不存在")
            if not is_allowed(real):
                return self.send_error_json(403, "该文件不在允许的扫描目录内")
        except OSError:
            return self.send_error_json(404, "源文件不存在")

        terms = payload.get("terms")

        style = str(payload.get("style") or "delete").strip()
        if style not in ("delete", "redact"):
            return self.send_error_json(400, "style 只能是 delete / redact")
        if style == "delete":
            return self._post_delete_terms(real, payload)

        mode = payload.get("mode") or "term"
        if mode not in ("term", "line", "para"):
            return self.send_error_json(400, "mode 只能是 term / line / para")
        try:
            import pdfbatch
            regions, per_term = pdfbatch.term_regions(
                real, terms, bool(payload.get("ignore_case", True)), mode=mode)
        except ValueError as exc:
            return self.send_error_json(400, str(exc))
        except Exception as exc:
            return self.send_error_json(500, "定位关键词失败: %s" % exc)

        missing = [w for w, n in per_term.items() if n == 0]
        if not regions:
            return self.send_error_json(
                409, "这些词在文档里一处也没找到：%s。"
                     "（若是扫描件/图片里的文字，涂黑删不掉它）"
                     % "、".join(missing or ["（空）"]))

        os.makedirs(EXPORT_DIR, exist_ok=True)
        stem = re.sub(r'[\\/:*?"<>|]', "_", os.path.splitext(
            os.path.basename(real))[0])
        stamp = time.strftime("%Y%m%d-%H%M%S")
        out_path = unique_out_path(
            os.path.join(EXPORT_DIR, f"{stem}-已涂黑-{stamp}.pdf"))

        try:
            with _edit_lock:
                result = pdfredact.apply_redactions(real, out_path, regions)
        except ValueError as exc:
            return self.send_error_json(400, f"涂黑失败: {exc}")
        except Exception as exc:
            return self.send_error_json(500, f"涂黑失败: {exc}")

        if result.get("removed_glyphs", 0) <= 0:
            try:
                os.remove(out_path)
            except OSError:
                pass
            return self.send_error_json(
                409, "命中了位置，但一个字形都没删掉。"
                     "如果这些词是图片/扫描件里的，涂黑无法删除其内容")

        residue = 0
        try:
            for w in per_term:
                residue += len(pdfbatch.find_text(out_path, w, True)["hits"])
        except Exception:
            residue = -1

        try:
            with open(out_path, "rb") as fp:
                body = fp.read()
        except OSError as exc:
            return self.send_error_json(500, f"读取输出文件失败: {exc}")

        self.send_response(200)
        self.send_header("Content-Type", "application/pdf")
        self.send_header("Connection", "close")
        self.close_connection = True
        self.send_header("Content-Length", str(len(body)))
        self.send_header(
            "Content-Disposition",
            "attachment; filename*=UTF-8''" + urllib.parse.quote(
                os.path.basename(out_path)))
        self.send_header("X-Out-Path", urllib.parse.quote(out_path, safe=""))
        self.send_header("X-Redact-Regions", str(len(regions)))
        self.send_header("X-Redact-Glyphs", str(result.get("removed_glyphs", 0)))
        self.send_header("X-Redact-Residue", str(residue))
        if missing:
            self.send_header("X-Redact-Missing",
                             urllib.parse.quote("、".join(missing), safe=""))
        self.end_headers()
        self.wfile.write(body)

    def _readable(self, src):
        if not src:
            return None, self.send_error_json(400, "缺少 path 参数")
        try:
            real = os.path.realpath(src)
            if not os.path.isfile(real):
                return None, self.send_error_json(404, "文件不存在")
            if not is_allowed(real):
                return None, self.send_error_json(403, "该文件不在允许的扫描目录内")
        except OSError:
            return None, self.send_error_json(404, "文件不存在")
        return real, None

    def _post_split(self, payload):
        src = urllib.parse.unquote(str(payload.get("path") or ""))
        real, err = self._readable(src)
        if err:
            return err
        try:
            per = int(payload.get("per") or 1)
        except (TypeError, ValueError):
            return self.send_error_json(400, "每份页数无效")

        hint = payload.get("name")
        stem_src = (hint.strip() if isinstance(hint, str) and hint.strip()
                    else os.path.splitext(os.path.basename(real))[0])
        os.makedirs(EXPORT_DIR, exist_ok=True)
        try:
            import pdfbatch
            with _edit_lock:
                made = pdfbatch.split_document(real, EXPORT_DIR, per,
                                               base_name=stem_src)
        except ValueError as exc:
            return self.send_error_json(400, str(exc))
        except Exception as exc:
            return self.send_error_json(500, "拆分失败: %s" % exc)
        self.send_json({"ok": True, "files": made, "count": len(made)})

    def _post_merge(self, payload):
        paths = payload.get("paths")
        if not isinstance(paths, list) or len(paths) < 2:
            return self.send_error_json(400, "合并至少需要两份文档")
        if len(paths) > 50:
            return self.send_error_json(400, "一次最多合并 50 份")
        reals = []
        for p in paths:
            real, err = self._readable(urllib.parse.unquote(str(p or "")))
            if err:
                return err
            reals.append(real)

        hint = payload.get("name")
        stem = (hint.strip() if isinstance(hint, str) and hint.strip()
                else "合并文档")
        stem = re.sub(r'[\\/:*?"<>|]', "_", stem)[:60] or "合并文档"
        stamp = time.strftime("%Y%m%d-%H%M%S")
        os.makedirs(EXPORT_DIR, exist_ok=True)
        out_path = unique_out_path(
            os.path.join(EXPORT_DIR, f"{stem}-合并-{stamp}.pdf"))
        try:
            import pdfbatch
            with _edit_lock:
                result = pdfbatch.merge_documents(reals, out_path)
        except ValueError as exc:
            return self.send_error_json(400, str(exc))
        except Exception as exc:
            return self.send_error_json(500, "合并失败: %s" % exc)
        self.send_json({"ok": True, "path": out_path,
                        "name": os.path.basename(out_path),
                        "pages": result["pages"]})

    def _post_export(self, payload):
        src = urllib.parse.unquote(str(payload.get("path") or ""))
        if not src:
            return self.send_error_json(400, "缺少 path 参数")

        try:
            real = os.path.realpath(src)
            if not os.path.isfile(real):
                return self.send_error_json(404, "源文件不存在")
            if not is_allowed(real):
                return self.send_error_json(403, "该文件不在允许的扫描目录内")
        except OSError:
            return self.send_error_json(404, "源文件不存在")

        pages, objects, dropped = sanitize_edits(
            payload.get("pages"), payload.get("objects"))

        annots, ann_dropped = sanitize_annotations(payload.get("annotations"))
        dropped += ann_dropped

        before_n = len(objects)
        objects = [o for o in objects if o.get("kind") != REDACT_KIND]
        dropped += before_n - len(objects)

        os.makedirs(EXPORT_DIR, exist_ok=True)
        hint = payload.get("name")
        if isinstance(hint, str) and hint.strip():
            stem_src = hint.strip()
        else:
            stem_src = os.path.splitext(os.path.basename(real))[0]
        stem = re.sub(r'[\\/:*?"<>|]', "_", stem_src)[:80] or "文档"
        stamp = time.strftime("%Y%m%d-%H%M%S")
        out_path = unique_out_path(
            os.path.join(EXPORT_DIR, f"{stem}-已编辑-{stamp}.pdf"))
        out_name = os.path.basename(out_path)

        try:
            with _edit_lock:
                result = pdfedit.apply_edits(
                    real, out_path,
                    {"pages": pages, "objects": objects,
                     "annotations": annots})
        except Exception as exc:
            return self.send_error_json(500, f"导出失败: {exc}")

        rng = payload.get("range")
        if isinstance(rng, str) and rng.strip():
            try:
                import pdfbatch
                total = result.get("pages") or 0
                if not total:
                    total = len(pdfbatch.open_doc(out_path).page_refs())
                picked = pdfbatch.parse_pages(rng, total)
                if len(picked) != total:
                    tmp = out_path + ".range.pdf"
                    pdfbatch.export_pages(out_path, tmp, picked)
                    spec = re.sub(r'[\\/:*?"<>|]', "_", rng.strip())[:24]
                    new_path = unique_out_path(os.path.join(
                        EXPORT_DIR, f"{stem}-第{spec}页-{stamp}.pdf"))
                    os.replace(tmp, new_path)
                    try:
                        os.remove(out_path)
                    except OSError:
                        pass
                    out_path = new_path
                    out_name = os.path.basename(out_path)
                    result["pages"] = len(picked)
            except ValueError as exc:
                return self.send_error_json(400, str(exc))
            except Exception as exc:
                return self.send_error_json(500, f"按范围导出失败: {exc}")

        try:
            with open(out_path, "rb") as fp:
                body = fp.read()
        except OSError as exc:
            return self.send_error_json(500, f"读取导出文件失败: {exc}")

        self.send_response(200)
        self.send_header("Content-Type", "application/pdf")
        self.send_header("Connection", "close")
        self.close_connection = True
        self.send_header("Content-Length", str(len(body)))
        self.send_header(
            "Content-Disposition",
            "attachment; filename*=UTF-8''" + urllib.parse.quote(out_name))
        self.send_header("X-Export-Path", urllib.parse.quote(out_path, safe=""))
        self.send_header("X-Export-Pages", str(result.get("pages", 0)))
        self.send_header("X-Export-Objects", str(result.get("objects", 0)))
        self.send_header("X-Export-Dropped", str(dropped))
        self.send_header("X-Export-Annots",
                         str((result.get("annotations") or {}).get("written", 0)))
        self.send_header("X-Export-CJK-Glyphs",
                         str(result.get("cjk_glyphs", 0)))
        miss = str(result.get("cjk_missing") or "")
        if miss:
            self.send_header("X-Export-CJK-Missing",
                             urllib.parse.quote(miss, safe=""))
        self.end_headers()
        self.wfile.write(body)

    def api_paragraphs(self, query):
        paths = query.get("path", [])
        if not paths:
            return self.send_error_json(400, "缺少 path 参数")
        target = urllib.parse.unquote(paths[0])
        try:
            page = max(1, int((query.get("page") or ["1"])[0]))
        except (TypeError, ValueError):
            page = 1
        try:
            real = os.path.realpath(target)
            if not os.path.isfile(real):
                return self.send_error_json(404, "文件不存在")
            if not is_allowed(real, query, self):
                return self.send_error_json(403, "该文件不在允许的扫描目录内")
            import pdfreflow
            info = pdfreflow.list_paragraphs(real, page)
        except Exception as exc:
            return self.send_error_json(500, "解析段落失败: %s" % exc)
        self.send_json(info)

    def _post_reflow(self, payload):
        src = urllib.parse.unquote(str(payload.get("path") or ""))
        if not src:
            return self.send_error_json(400, "缺少 path 参数")
        if payload.get("overwrite") or payload.get("inPlace"):
            return self.send_error_json(
                400, "不允许覆盖原文件：只能另存为新文件")

        try:
            page = int(payload.get("page") or 1)
            para = int(payload.get("para") if payload.get("para") is not None else -1)
        except (TypeError, ValueError):
            return self.send_error_json(400, "page / para 参数无效")
        if page < 1 or para < 0:
            return self.send_error_json(400, "page / para 参数无效")

        text = payload.get("text")
        if not isinstance(text, str) or not text.strip():
            return self.send_error_json(400, "新文字不能为空")
        if len(text) > 20000:
            return self.send_error_json(400, "文字太长了（上限 20000 字）")

        cell = payload.get("cell")
        if cell is not None:
            if (not isinstance(cell, (list, tuple)) or len(cell) != 2
                    or not all(isinstance(v, int) and not isinstance(v, bool) and v >= 0
                               for v in cell)):
                return self.send_error_json(400, "cell 参数无效（应为 [行, 列] 两个非负整数）")
            cell = (cell[0], cell[1])

        orig = payload.get("orig")
        if orig is not None:
            if not isinstance(orig, str):
                return self.send_error_json(400, "orig 参数无效")
            if len(orig) > 20000:
                return self.send_error_json(400, "orig 太长了")

        page_text = payload.get("page_text")
        if page_text is not None:
            if not isinstance(page_text, str):
                return self.send_error_json(400, "page_text 参数无效")
            if len(page_text) > 200000:
                return self.send_error_json(400, "page_text 太长了")

        stage = payload.get("stage")
        if stage is not None:
            if not isinstance(stage, str) or not STAGE_ID_RE.match(stage):
                return self.send_error_json(400, "stage 参数无效")

        try:
            if stage:
                real = stage_file(stage)
                if not os.path.isfile(real):
                    return self.send_error_json(
                        404, "预览会话已失效（暂存文件可能被清理），请重新开始")
            else:
                real = os.path.realpath(src)
                if not os.path.isfile(real):
                    return self.send_error_json(404, "源文件不存在")
                if not is_allowed(real):
                    return self.send_error_json(403, "该文件不在允许的扫描目录内")
        except OSError:
            return self.send_error_json(404, "源文件不存在")

        print("[reflow] page=%d para=%d stage=%s cell=%s text=%r"
              % (page, para, stage or "-", cell, text[:24]),
              file=sys.stderr, flush=True)

        os.makedirs(STAGE_DIR, exist_ok=True)
        if stage:
            out_id = stage
            out_path = os.path.join(STAGE_DIR, stage + ".next.pdf")
        else:
            out_id = new_stage_id()
            out_path = stage_file(out_id)
        hint = payload.get("name")
        if isinstance(hint, str) and hint.strip():
            stem_src = hint.strip()
        else:
            stem_src = os.path.splitext(os.path.basename(src))[0]
        base_stem = re.sub(r'[\\/:*?"<>|]', "_", stem_src)[:80] or "文档"

        try:
            import pdfreflow
            with _edit_lock:
                try:
                    result = pdfreflow.apply_paragraph_edit(
                        real, out_path, page, para, text,
                        orig_text=orig, page_text=page_text, cell=cell)
                except ValueError as exc:
                    if cell is not None or "放不下" not in str(exc):
                        raise
                    result = pdfreflow.apply_paragraph_flow(
                        real, out_path, page, para, text,
                        orig_text=orig, page_text=page_text)
        except ValueError as exc:
            try:
                if os.path.exists(out_path):
                    os.remove(out_path)
            except OSError:
                pass
            return self.send_error_json(400, str(exc))
        except Exception as exc:
            try:
                if os.path.exists(out_path):
                    os.remove(out_path)
            except OSError:
                pass
            return self.send_error_json(500, "改段落失败: %s" % exc)

        try:
            with open(out_path, "rb") as fp:
                body = fp.read()
        except OSError as exc:
            return self.send_error_json(500, "读取产物失败: %s" % exc)

        if stage:
            backup_stage_version(stage)
            try:
                os.replace(out_path, stage_file(stage))
            except OSError as exc:
                return self.send_error_json(500, "暂存覆盖失败: %s" % exc)

        self.send_response(200)
        self.send_header("Content-Type", "application/pdf")
        self.send_header("Connection", "close")
        self.close_connection = True
        self.send_header("Content-Length", str(len(body)))
        self.send_header(
            "Content-Disposition",
            "inline; filename*=UTF-8''" + urllib.parse.quote(
                "%s-改段落.pdf" % base_stem))
        self.send_header("X-Stage-Id", out_id)
        self.send_header("X-Stage-Path", urllib.parse.quote(
            stage_file(out_id), safe=""))
        self.send_header("X-Reflow-Lines-Before", str(result["lines_before"]))
        self.send_header("X-Reflow-Lines-After", str(result["lines_after"]))
        self.send_header("X-Reflow-Shift", str(result["shift"]))
        self.send_header("X-Reflow-Font", urllib.parse.quote(str(result["font"]), safe=""))
        self.send_header("X-Reflow-Map", str(result.get("map_source") or ""))
        self.send_header("X-Reflow-Pages", str(result.get("pages_affected") or 1))
        self.send_header("X-Reflow-New-Pages", str(result.get("new_pages") or 0))
        self.send_header("X-Reflow-Last-Page", str(result.get("last_page") or page))
        self.send_header("X-Reflow-Mapped", str(result.get("mapped_chars") or 0))
        fb = str(result.get("fallback_chars") or "")
        if fb:
            self.send_header("X-Reflow-Fallback",
                             urllib.parse.quote(fb, safe=""))
        self.end_headers()
        self.wfile.write(body)

    def _post_stage(self, payload):
        sid = payload.get("id")
        if not isinstance(sid, str) or not STAGE_ID_RE.match(sid):
            return self.send_error_json(400, "id 无效")
        path = stage_file(sid)
        removed = False
        try:
            if os.path.isfile(path):
                os.remove(path)
                removed = True
        except OSError:
            pass
        try:
            nxt = os.path.join(STAGE_DIR, sid + ".next.pdf")
            if os.path.isfile(nxt):
                os.remove(nxt)
        except OSError:
            pass
        for n in stage_versions(sid):
            try:
                os.remove(stage_version_path(sid, n))
            except OSError:
                pass
        self.send_json({"ok": True, "removed": removed})

    def _post_stage_undo(self, payload):
        sid = payload.get("id")
        if not isinstance(sid, str) or not STAGE_ID_RE.match(sid):
            return self.send_error_json(400, "id 无效")

        with _edit_lock:
            vers = stage_versions(sid)
            if not vers:
                return self.send_error_json(
                    400, "这一版已经是最早的了，没有更早的版本可退回")
            last = vers[-1]
            src = stage_version_path(sid, last)
            dst = stage_file(sid)
            tmp = os.path.join(STAGE_DIR, sid + ".undo.pdf")
            try:
                shutil.copy2(src, tmp)
                os.replace(tmp, dst)
            except OSError as exc:
                try:
                    if os.path.isfile(tmp):
                        os.remove(tmp)
                except OSError:
                    pass
                return self.send_error_json(500, "撤销失败: %s" % exc)
            try:
                os.remove(src)
            except OSError:
                pass

        self.send_json({
            "ok": True,
            "stageId": sid,
            "stagePath": dst,
            "remaining": len(vers) - 1,
        })

    def _post_redact(self, payload):
        src = urllib.parse.unquote(str(payload.get("path") or ""))
        if not src:
            return self.send_error_json(400, "缺少 path 参数")

        if payload.get("overwrite") or payload.get("inPlace"):
            return self.send_error_json(
                400, "涂黑操作不允许覆盖原文件，只支持另存为")

        try:
            real = os.path.realpath(src)
            if not os.path.isfile(real):
                return self.send_error_json(404, "源文件不存在")
            if not is_allowed(real):
                return self.send_error_json(403, "该文件不在允许的扫描目录内")
        except OSError:
            return self.send_error_json(404, "源文件不存在")

        regions, dropped = sanitize_redactions(payload.get("regions"))
        if not regions:
            return self.send_error_json(400, "没有有效的涂黑区域")

        os.makedirs(EXPORT_DIR, exist_ok=True)
        base = os.path.splitext(os.path.basename(real))[0]
        given = str(payload.get("name") or "").strip()
        if given:
            base = os.path.splitext(os.path.basename(given))[0]
        stem = re.sub(r'[\\/:*?"<>|]', "_", base)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        out_path = unique_out_path(
            os.path.join(EXPORT_DIR, f"{stem}-已涂黑-{stamp}.pdf"))
        out_name = os.path.basename(out_path)

        try:
            with _edit_lock:
                result = pdfredact.apply_redactions(real, out_path, regions)
        except ValueError as exc:
            return self.send_error_json(400, f"涂黑失败: {exc}")
        except Exception as exc:
            return self.send_error_json(500, f"涂黑失败: {exc}")

        if result.get("removed_glyphs", 0) <= 0:
            try:
                os.remove(out_path)
            except OSError:
                pass
            return self.send_error_json(
                409,
                "所选区域没有覆盖到任何文字，可能位置不对。"
                "请调整涂黑框，确认盖住要删除的内容后再试"
                "（若该处本来就是图形/图片，涂黑无法删除其底层内容）")

        try:
            with open(out_path, "rb") as fp:
                body = fp.read()
        except OSError as exc:
            return self.send_error_json(500, f"读取输出文件失败: {exc}")

        self.send_response(200)
        self.send_header("Content-Type", "application/pdf")
        self.send_header("Connection", "close")
        self.close_connection = True
        self.send_header("Content-Length", str(len(body)))
        self.send_header(
            "Content-Disposition",
            "attachment; filename*=UTF-8''" + urllib.parse.quote(out_name))
        self.send_header("X-Out-Path", urllib.parse.quote(out_path, safe=""))
        self.send_header("X-Redact-Pages", str(result.get("pages_redacted", 0)))
        self.send_header("X-Redact-Glyphs", str(result.get("removed_glyphs", 0)))
        self.send_header("X-Redact-Meta",
                         str(len(result.get("metadata_cleaned") or [])))
        self.send_header("X-Redact-Outline",
                         str(result.get("outline_titles_cleaned", 0)))
        self.send_header("X-Redact-Dropped", str(dropped))
        self.end_headers()
        self.wfile.write(body)

    def _post_replace(self, payload):
        src = urllib.parse.unquote(str(payload.get("path") or ""))
        if not src:
            return self.send_error_json(400, "缺少 path 参数")

        if payload.get("overwrite") or payload.get("inPlace"):
            return self.send_error_json(
                400, "原位替换不允许覆盖原文件，只支持另存为")

        try:
            real = os.path.realpath(src)
            if not os.path.isfile(real):
                return self.send_error_json(404, "源文件不存在")
            if not is_allowed(real):
                return self.send_error_json(403, "该文件不在允许的扫描目录内")
        except OSError:
            return self.send_error_json(404, "源文件不存在")

        replacements = []
        for r in payload.get("replacements") or []:
            try:
                item = {
                    "page": int(r.get("page")),
                    "x": float(r.get("x")), "y": float(r.get("y")),
                    "w": float(r.get("w")), "h": float(r.get("h")),
                    "text": str(r.get("text") or ""),
                }
            except (TypeError, ValueError):
                continue
            if item["w"] <= 0 or item["h"] <= 0 or not item["text"]:
                continue
            replacements.append(item)
        if not replacements:
            return self.send_error_json(400, "没有有效的替换区域")

        os.makedirs(EXPORT_DIR, exist_ok=True)
        stem = re.sub(r'[\\/:*?"<>|]', "_", os.path.splitext(
            os.path.basename(real))[0])
        stamp = time.strftime("%Y%m%d-%H%M%S")
        out_path = unique_out_path(
            os.path.join(EXPORT_DIR, f"{stem}-已替换-{stamp}.pdf"))
        out_name = os.path.basename(out_path)

        try:
            with _edit_lock:
                result = pdfredact.apply_text_replace(
                    real, out_path, replacements)
        except ValueError as exc:
            return self.send_error_json(400, f"替换失败: {exc}")
        except Exception as exc:
            return self.send_error_json(500, f"替换失败: {exc}")

        if result.get("replaced_glyphs", 0) <= 0:
            try:
                os.remove(out_path)
            except OSError:
                pass
            return self.send_error_json(
                409,
                "所选区域没有覆盖到任何文字，可能位置不对。"
                "请调整选区，确认框住要修改的内容后再试")

        try:
            with open(out_path, "rb") as fp:
                body = fp.read()
        except OSError as exc:
            return self.send_error_json(500, f"读取输出文件失败: {exc}")

        self.send_response(200)
        self.send_header("Content-Type", "application/pdf")
        self.send_header("Connection", "close")
        self.close_connection = True
        self.send_header("Content-Length", str(len(body)))
        self.send_header(
            "Content-Disposition",
            "attachment; filename*=UTF-8''" + urllib.parse.quote(out_name))
        self.send_header("X-Out-Path", urllib.parse.quote(out_path, safe=""))
        self.send_header("X-Replace-Pages", str(result.get("pages_replaced", 0)))
        self.send_header("X-Replace-Glyphs", str(result.get("replaced_glyphs", 0)))
        self.end_headers()
        self.wfile.write(body)

    def do_HEAD(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/api/file":
            query = urllib.parse.parse_qs(parsed.query)
            return self.api_file(query, head_only=True)
        self.send_error(404)

    def _resolve_dirs(self, query):
        global SCAN_DIRS
        seen = {norm(x) for x in SCAN_DIRS}
        for d in (query.get("dir") or []):
            d = urllib.parse.unquote(d)
            if not os.path.isdir(d):
                continue
            rd = os.path.realpath(d)
            if norm(rd) not in seen:
                SCAN_DIRS.append(rd)
                seen.add(norm(rd))
        return list(SCAN_DIRS)

    def api_files(self, query):
        dirs = self._resolve_dirs(query)
        try:
            files = scan_pdfs(dirs)
        except Exception as exc:
            return self.send_error_json(500, f"扫描失败: {exc}")
        self.send_json({
            "ok": True,
            "dirs": dirs,
            "files": files,
            "count": len(files),
            "truncated": len(files) >= MAX_LIST,
        })

    def api_update(self, query):
        now = time.time()
        if _update_cache["data"] and now - _update_cache["at"] < UPDATE_TTL:
            return self.send_json(_update_cache["data"])

        url = ("https://api.github.com/repos/%s/releases?per_page=20"
               % UPDATE_REPO)
        req = urllib.request.Request(url, headers={
            "User-Agent": "PDFViewer/" + appversion.VERSION,
            "Accept": "application/vnd.github+json",
        })
        try:
            with urllib.request.urlopen(req, timeout=6) as resp:
                releases = json.loads(resp.read().decode("utf-8"))
        except Exception as exc:
            return self.send_json({
                "ok": False,
                "current": appversion.VERSION,
                "error": "检查更新失败：%s" % exc,
            })

        if not isinstance(releases, list):
            return self.send_json({
                "ok": False, "current": appversion.VERSION,
                "error": "Releases 返回格式不对",
            })

        hit = appversion.pick_latest(releases, appversion.VERSION) or {}
        data = {
            "ok": True,
            "current": appversion.VERSION,
            "repo": UPDATE_REPO,
            "hasUpdate": bool(hit),
            "latest": appversion.version_of(hit) or hit.get("tag_name") or "",
            "tag": hit.get("tag_name") or "",
            "name": hit.get("name") or "",
            "url": hit.get("html_url")
                   or ("https://github.com/%s/releases" % UPDATE_REPO),
            "published": hit.get("published_at") or "",
        }
        _update_cache["at"] = now
        _update_cache["data"] = data
        return self.send_json(data)

    def api_file(self, query, head_only=False):
        paths = query.get("path", [])
        if not paths:
            return self.send_error_json(400, "缺少 path 参数")
        target = urllib.parse.unquote(paths[0])

        try:
            real = os.path.realpath(target)
            size = os.path.getsize(real)
        except OSError:
            return self.send_error_json(404, "文件不存在")
        if not is_allowed(real, query, self):
            return self.send_error_json(403, "该文件不在允许的扫描目录内")

        ctype = mimetypes.guess_type(real)[0] or "application/pdf"
        range_header = self.headers.get("Range")

        start, end = 0, size - 1
        partial = False
        if range_header:
            m = re.match(r"bytes=(\d*)-(\d*)", range_header.strip())
            if m:
                g1, g2 = m.group(1), m.group(2)
                if g1:
                    start = int(g1)
                    if g2:
                        end = min(int(g2), size - 1)
                elif g2:
                    start = max(0, size - int(g2))
                if start > end or start >= size:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                partial = True

        length = end - start + 1

        self.send_response(206 if partial else 200)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-cache")
        if (query.get("download") or [""])[0] in ("1", "true"):
            fn = (query.get("name") or [""])[0]
            fn = urllib.parse.unquote(fn) if fn else os.path.basename(real)
            fn = re.sub(r'[\\/:*?"<>|\r\n]', "_", fn)[:120] or "document.pdf"
            self.send_header("Content-Disposition",
                             "attachment; filename*=UTF-8''" +
                             urllib.parse.quote(fn))
        if partial:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()

        if head_only:
            return

        with open(real, "rb") as fp:
            fp.seek(start)
            remaining = length
            while remaining > 0:
                chunk = fp.read(min(CHUNK, remaining))
                if not chunk:
                    break
                try:
                    self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    return
                remaining -= len(chunk)

    def api_get_annotations(self, query):
        did = (query.get("doc") or [""])[0]
        if not valid_doc_id(did):
            return self.send_error_json(400, "doc 参数无效")
        data = load_annotations(did)
        if not data.get("ok"):
            return self.send_error_json(500, data.get("error", "读取失败"))
        self.send_json(data)

    def api_notes(self, query):
        did = (query.get("doc") or [""])[0]
        if not valid_doc_id(did):
            return self.send_error_json(400, "doc 参数无效")
        name = (query.get("name") or [did])[0]
        md = export_markdown(did, urllib.parse.unquote(name))
        body = md.encode("utf-8")
        filename = f"{name}-notes.md".replace('"', "")
        self.send_response(200)
        self.send_header("Content-Type", "text/markdown; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header(
            "Content-Disposition",
            "attachment; filename*=UTF-8''" + urllib.parse.quote(filename),
        )
        self.end_headers()
        self.wfile.write(body)

    def api_get_edits(self, query):
        did = (query.get("doc") or [""])[0]
        if not valid_doc_id(did):
            return self.send_error_json(400, "doc 参数无效")
        data = load_edits(did)
        if not data.get("ok"):
            return self.send_error_json(500, data.get("error", "读取失败"))
        self.send_json(data)

    def api_pdfinfo(self, query):
        paths = query.get("path", [])
        if not paths:
            return self.send_error_json(400, "缺少 path 参数")
        target = urllib.parse.unquote(paths[0])
        try:
            real = os.path.realpath(target)
            if not os.path.isfile(real):
                return self.send_error_json(404, "文件不存在")
            if not is_allowed(real, query, self):
                return self.send_error_json(403, "该文件不在允许的扫描目录内")
            info = pdf_page_info(real)
        except Exception as exc:
            return self.send_error_json(500, f"解析失败: {exc}")
        self.send_json(info)

    def api_open(self, query):
        paths = query.get("path", [])
        if not paths:
            return self.send_error_json(400, "缺少 path 参数")
        target = urllib.parse.unquote(paths[0])
        try:
            real = os.path.realpath(target)
            if not os.path.isfile(real):
                return self.send_error_json(404, "文件不存在")
            if not is_allowed(real, query, self):
                return self.send_error_json(403, "该文件不在允许的扫描目录内")
            if sys.platform == "win32":
                os.startfile(real)
            elif sys.platform == "darwin":
                import subprocess
                subprocess.Popen(["open", real])
            else:
                import subprocess
                subprocess.Popen(["xdg-open", real])
        except Exception as exc:
            return self.send_error_json(500, f"打开失败: {exc}")
        self.send_json({"ok": True})

    def serve_file(self, path):
        if not os.path.isfile(path):
            return self.send_error_json(404, "文件未找到")
        ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
        if path.endswith(".mjs"):
            ctype = "text/javascript"
        elif path.endswith((".js",)):
            ctype = "text/javascript"
        elif path.endswith(".css"):
            ctype = "text/css"
        elif path.endswith(".html"):
            ctype = "text/html; charset=utf-8"
        elif path.endswith(".json"):
            ctype = "application/json; charset=utf-8"
        elif path.endswith(".ttf"):
            ctype = "font/ttf"
        elif path.endswith(".otf"):
            ctype = "font/otf"

        with open(path, "rb") as fp:
            body = fp.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        if path.endswith((".html", ".css", ".js", ".mjs")):
            self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

def default_dirs():
    if appenv.FROZEN:
        home = os.path.expanduser("~")
        for name in ("Documents", "Desktop", "Downloads"):
            d = os.path.join(home, name)
            if os.path.isdir(d):
                return [d]
        return [DATA_DIR]

    out = []
    local = os.path.join(BASE_DIR, "documents")
    if os.path.isdir(local):
        out.append(local)
    home_docs = os.path.join(os.path.expanduser("~"), "Documents")
    if os.path.isdir(home_docs):
        out.append(home_docs)
    if not out:
        out.append(BASE_DIR)
    return out

def find_running_instance(host, port, span):
    for p in range(port, port + span):
        try:
            with urllib.request.urlopen(
                    "http://%s:%d/api/ping" % (host, p), timeout=0.25) as r:
                if r.status == 200 and b"pdfviewer" in r.read(200):
                    return p
        except Exception:
            continue
    return None

def open_app_window(url):
    if os.name != "nt":
        return False
    roots = [os.environ.get("ProgramFiles(x86)"),
             os.environ.get("ProgramFiles"),
             os.environ.get("LOCALAPPDATA")]
    rel = [
        ("Microsoft", "Edge", "Application", "msedge.exe"),
        ("Google", "Chrome", "Application", "chrome.exe"),
    ]
    for root in roots:
        if not root:
            continue
        for parts in rel:
            exe = os.path.join(root, *parts)
            if not os.path.isfile(exe):
                continue
            try:
                import subprocess
                subprocess.Popen([exe, "--app=" + url, "--window-size=1360,960"],
                                 close_fds=True)
                return True
            except OSError:
                continue
    return False

class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = (os.name != "nt")

def run_startup_check():
    try:
        import check
    except Exception:
        return
    try:
        rc = check.main()
    except Exception as exc:
        print(f"  前端自检无法运行：{exc}")
        return
    if rc:
        print()
        print("  ⚠ 前端自检发现问题（见上）。不阻止启动，")
        print("    但页面可能异常，建议先修复；或用 --no-check 跳过自检。")

def clean_stage_orphans():
    if not os.path.isdir(STAGE_DIR):
        return 0
    limit = time.time() - STAGE_KEEP_SEC
    n = 0
    try:
        names = os.listdir(STAGE_DIR)
    except OSError:
        return 0
    for name in names:
        fp = os.path.join(STAGE_DIR, name)
        try:
            if os.path.isfile(fp) and os.path.getmtime(fp) < limit:
                os.remove(fp)
                n += 1
        except OSError:
            continue
    return n

def main():
    global SCAN_DIRS, PORT, EXPORT_DIR, EDIT_DIR, ANNOT_DIR, STAGE_DIR

    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(line_buffering=True, errors="replace")
        except Exception:
            pass

    ap = argparse.ArgumentParser(description="本地 PDF 查看器服务")
    ap.add_argument("--dir", action="append", default=[],
                    help="要扫描的 PDF 目录，可重复指定")
    ap.add_argument("--allow", action="append", default=[], metavar="FILE",
                    help="额外允许读取的文件（可多次）；只放行这一份，不放开所在目录")
    ap.add_argument("--port", type=int, default=8000, help="监听端口，默认 8000")
    ap.add_argument("--host", default="127.0.0.1", help="监听地址，默认仅本机")
    ap.add_argument("--no-browser", action="store_true", help="启动后不自动打开浏览器")
    ap.add_argument("--browser", action="store_true",
                    help="用默认浏览器打开，不开独立应用窗口（打包版默认开应用窗口）")
    ap.add_argument("--no-check", action="store_true",
                    help="启动时跳过前端静态自检（check.py）")
    ap.add_argument("--export-dir", default=None,
                    help="导出产物目录，默认 <项目>/exports")
    ap.add_argument("--edits-dir", default=None,
                    help="编辑数据目录，默认 <项目>/edits")
    ap.add_argument("--annot-dir", default=None,
                    help="标注数据目录，默认 <项目>/annotations")
    ap.add_argument("--stage-dir", default=None,
                    help="预览暂存目录，默认 <项目>/_stage（测试必须重定向）")
    args = ap.parse_args()

    for _f in getattr(args, "allow", []) or []:
        _r = os.path.realpath(_f)
        if os.path.isfile(_r):
            OPEN_FILES.add(_r)

    if args.export_dir:
        EXPORT_DIR = os.path.realpath(args.export_dir)
    if args.edits_dir:
        EDIT_DIR = os.path.realpath(args.edits_dir)
    if args.annot_dir:
        ANNOT_DIR = os.path.realpath(args.annot_dir)
    if args.stage_dir:
        STAGE_DIR = os.path.realpath(args.stage_dir)
    for _d in (EXPORT_DIR, EDIT_DIR, ANNOT_DIR, STAGE_DIR):
        os.makedirs(_d, exist_ok=True)

    if appenv.FROZEN:
        try:
            appenv.ensure_writable()
        except OSError as exc:
            print("错误：数据目录不可写 —— %s" % exc)
            print("       %s" % DATA_DIR)
            return 1

    dropped = clean_stage_orphans()
    if dropped:
        print("已清理 %d 个过期预览暂存文件" % dropped)

    SCAN_DIRS = [os.path.realpath(d) for d in (args.dir or default_dirs())
                 if os.path.isdir(d)]

    if not SCAN_DIRS:
        print("错误：没有可用的扫描目录。")
        print("请用 --dir 指定存放 PDF 的文件夹，例如：")
        print("  python server.py --dir D:\\我的PDF")
        return 1

    if not args.no_check and not appenv.FROZEN:
        run_startup_check()

    if args.port == 8000:
        running = find_running_instance(args.host, args.port, PORT_SCAN_RANGE)
        if running:
            url = f"http://{args.host}:{running}/"
            print("已经有一个 PDF 查看器在运行：%s" % url)
            print("（两个实例会抢同一份编辑数据，这里就不再开新的了）")
            if not args.no_browser:
                try:
                    webbrowser.open(url)
                except Exception:
                    pass
            return 0

    httpd = None
    taken = []
    for p in range(args.port, args.port + PORT_SCAN_RANGE):
        try:
            httpd = Server((args.host, p), Handler)
            break
        except OSError:
            taken.append(p)

    if httpd is None:
        httpd = Server((args.host, 0), Handler)

    PORT = httpd.server_address[1]

    url = f"http://{args.host}:{PORT}/"
    print("=" * 58)
    print("  PDF 查看器已启动")
    print("=" * 58)
    if taken:
        span = str(taken[0]) if len(taken) == 1 else f"{taken[0]}~{taken[-1]}"
        print(f"  注意     : 端口 {span} 已被占用，已自动改用 {PORT}")
        print( "             若是上一次没关掉的实例，可先结束它再用原端口")
        print()
    print(f"  访问地址 : {url}")
    print(f"  扫描目录 :")
    for d in SCAN_DIRS:
        print(f"    - {d}")
    if appenv.FROZEN:
        print(f"  数据目录 : {DATA_DIR}")
    print()
    print("  提示：在页面上点「添加目录」可临时扫描其他位置。")
    print("  关闭这个窗口即可停止程序（窗口里按 Ctrl+C 也行）。" if appenv.FROZEN
          else "  按 Ctrl+C 停止服务。")
    print("=" * 58)

    if not args.no_browser:
        opened = False
        if appenv.FROZEN and not args.browser:
            opened = open_app_window(url)
            if not opened:
                print("  提示：没找到 Edge / Chrome，改用默认浏览器打开。")
        if not opened:
            try:
                webbrowser.open(url)
            except Exception:
                pass

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
    finally:
        httpd.server_close()
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
