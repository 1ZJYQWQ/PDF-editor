"""
PDF Viewer - 本地服务端

零第三方依赖，仅使用 Python 标准库。

功能：
  GET  /                       前端页面
  GET  /api/files              扫描并返回 PDF 文件列表
  GET  /api/file?path=...      流式返回 PDF 内容（支持 Range 请求）
  GET  /api/open?path=...      用系统默认程序打开 PDF
  GET  /api/annotations?doc=...  读取某文档的全部标注
  POST /api/annotations        写入某文档的标注
  GET  /api/notes?doc=...      导出该文档的标注为 Markdown
  GET  /api/edits?doc=...      读取某文档的编辑数据
  POST /api/edits              写入某文档的编辑数据
  GET  /api/pdfinfo?path=...   返回 PDF 的页数与各页尺寸（供编辑模式用）
  POST /api/export             应用编辑，产出一份新的 PDF 供下载
POST /api/redact             执行涂黑（真正的信息销毁，不可逆）—— 只另存为
  GET  /static/...             静态资源

用法：
  python server.py                 # 默认扫描 项目文档目录 与 用户文档目录
  python server.py --dir D:\\PDFs   # 指定要扫描的 PDF 目录（可多次）
  python server.py --port 8080     # 指定端口
"""

import argparse
import hashlib
import json
import mimetypes
import os
import re
import sys
import threading
import time
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pdfedit
import pdfredact

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")
ANNOT_DIR = os.path.join(BASE_DIR, "annotations")
EDIT_DIR = os.path.join(BASE_DIR, "edits")
EXPORT_DIR = os.path.join(BASE_DIR, "exports")

# 全局配置，由 main() 填充
SCAN_DIRS = []
PORT = 8000
PORT_SCAN_RANGE = 20     # 端口被占用时，向后顺延尝试的个数

MAX_LIST = 2000          # 单次最多返回的 PDF 数量
CHUNK = 64 * 1024        # 流式分块大小
MAX_BODY = 8 * 1024 * 1024   # 请求体上限

_annot_lock = threading.Lock()
_edit_lock = threading.Lock()


# --------------------------------------------------------------------------
# 工具函数
# --------------------------------------------------------------------------

def norm(path):
    """统一路径分隔符，便于跨平台比较。"""
    return os.path.normcase(os.path.abspath(path))


def is_within(path, roots):
    """校验 path 是否位于允许的根目录内，防止路径穿越。"""
    p = norm(path)
    for r in roots:
        rn = norm(r)
        if p == rn or p.startswith(rn + os.sep):
            return True
    return False


def human_size(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024.0


def scan_pdfs(dirs):
    """递归扫描目录，收集 PDF 文件信息。"""
    found = []
    seen = set()
    for root_dir in dirs:
        if not os.path.isdir(root_dir):
            continue
        for dirpath, dirnames, filenames in os.walk(root_dir):
            # 跳过隐藏目录和常见缓存目录
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


# --------------------------------------------------------------------------
# 文档指纹与标注存储
# --------------------------------------------------------------------------

def doc_id(path, size=None):
    """
    由「文件名 + 文件大小」生成稳定指纹。
    不用完整路径，这样同一份文档换位置后标注依然能识别。
    """
    try:
        if size is None:
            size = os.path.getsize(path)
    except OSError:
        size = 0
    base = os.path.basename(path)
    raw = f"{base}|{size}".encode("utf-8")
    return hashlib.sha1(raw).hexdigest()[:16]


def annot_path(did):
    """标注文件路径，did 已做白名单校验。"""
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
    except Exception as exc:  # noqa: BLE001
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


# --------------------------------------------------------------------------
# 编辑数据存储
#
# 与标注分开存盘（不同目录、不同文件），理由：
#   - 标注是"阅读痕迹"，编辑是"内容改动"，生命周期不同
#   - 结构差异大，混在一个文件里读写都别扭
#   - 编辑一旦出错影响面更大，独立存放便于单独清理
#
# 数据结构：
#   {
#     "pages":   { "order": [...], "delete": [...], "rotate": {...},
#                  "insert": [...] },
#     "objects": [ { "page":1, "kind":"whiteout", "x":.., "y":.., ... } ]
#   }
# 前端提交的坐标必须是 **PDF 坐标**（原点左下、y 向上），
# 由前端完成屏幕坐标 -> PDF 坐标的翻转，服务端不做猜测。
# --------------------------------------------------------------------------

EDITABLE_KINDS = {"whiteout", "text", "rect", "ellipse", "line"}

# 涂黑区域也持久化在编辑数据里（这样刷新页面不会丢），
# 但它**不能**走 /api/export —— 普通导出是叠加式的，
# 无法删除底层文字，让涂黑框走那条路只会产出
# "看着是黑块、文字其实还在"的文件，正是要避免的事故。
# 所以这里单独列出，export 路径会把它过滤掉。
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
    except Exception as exc:  # noqa: BLE001
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
    """
    校验并规范化前端提交的编辑数据。

    原则：宁可丢弃可疑数据，也不能让非法值流进 PDF 写出阶段 ——
    写坏一份 PDF 比拒绝一次请求严重得多。
    返回 (干净的 pages, 干净的 objects, 被丢弃的数量)。
    """
    dropped = 0

    # ---- pages ----
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
            # 尺寸做合理区间约束，防止 0 或天文数字
            w = min(max(w, 10.0), 20000.0)
            h = min(max(h, 10.0), 20000.0)
            clean_pages["insert"].append({"at": max(0, at),
                                          "width": w, "height": h})

    # ---- objects ----
    clean_objs = []
    for obj in (objects or []):
        if not isinstance(obj, dict):
            dropped += 1
            continue
        kind = str(obj.get("kind") or "").lower()
        # 涂黑区域允许持久化（刷新不丢），但它只会被
        # /api/redact 消费；/api/export 会另行过滤掉。
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


def sanitize_redactions(regions):
    """
    校验并规范化前端提交的涂黑区域。

    与 sanitize_edits 同样的原则：宁可丢弃，也不能让非法值进入
    信息销毁流程 —— redaction 是**不可逆**的，处理错比拒绝更糟。
    返回 (干净区域列表, 被丢弃数量)。
    """
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
        # 尺寸做区间约束：负数和天文数字都会让判定失效
        w = min(max(w, 1.0), 20000.0)
        h = min(max(h, 1.0), 20000.0)
        clean.append({"page": page, "x": x, "y": y, "w": w, "h": h})

    return clean, dropped


def _safe_float(v, default=0.0):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    if f != f or f in (float("inf"), float("-inf")):     # NaN / Inf
        return default
    return f


def _safe_color(v):
    """颜色只接受 #rgb / #rrggbb 与 rgb(r,g,b) 两种形式。"""
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
    """只允许标准 14 字体之一 —— 其他字体我们无法嵌入，会渲染失败。"""
    allowed = {
        "Helvetica", "Helvetica-Bold", "Helvetica-Oblique", "Times-Roman",
        "Times-Bold", "Courier", "Courier-Bold",
    }
    s = str(v or "").strip()
    return s if s in allowed else "Helvetica"


def pdf_page_info(path):
    """读取 PDF 的页数与各页尺寸（点在 PDF 坐标下，原点左下）。"""
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
    """把标注导出为 Markdown 笔记。"""
    data = load_annotations(did)
    items = data.get("items", [])
    lines = [f"# {doc_name or did} 阅读笔记", ""]
    lines.append(f"> 导出时间：{time.strftime('%Y-%m-%d %H:%M')}")
    lines.append(f"> 共 {len(items)} 条标注")
    lines.append("")

    if not items:
        lines.append("_暂无标注_")
        return "\n".join(lines)

    # 按页分组
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


# --------------------------------------------------------------------------
# 请求处理
# --------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = "PDFViewer/1.0"
    protocol_version = "HTTP/1.1"

    # ---- 日志精简 ----
    def log_message(self, fmt, *args):
        msg = fmt % args
        # 过滤掉静态资源的噪声日志
        if "/static/" in msg or "/api/file" in msg:
            return
        sys.stderr.write(f"  {msg}\n")

    # ---- 响应助手 ----
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
        self.send_json({"ok": False, "error": message}, status=status)

    # ---- 路由 ----
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        route = parsed.path
        query = urllib.parse.parse_qs(parsed.query)

        try:
            if route == "/":
                return self.serve_file(os.path.join(STATIC_DIR, "index.html"))
            if route == "/api/files":
                return self.api_files(query)
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
            if route.startswith("/static/"):
                rel = urllib.parse.unquote(route[len("/static/"):])
                target = os.path.join(STATIC_DIR, rel.replace("/", os.sep))
                if not is_within(target, [STATIC_DIR]):
                    return self.send_error_json(403, "拒绝访问")
                return self.serve_file(target)
            return self.send_error_json(404, "未找到该路径")
        except BrokenPipeError:
            pass
        except Exception as exc:  # noqa: BLE001
            self.log_message("ERROR %s", exc)
            try:
                self.send_error_json(500, f"服务器错误: {exc}")
            except Exception:
                pass

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        route = parsed.path
        if route not in ("/api/annotations", "/api/edits", "/api/export",
                         "/api/redact"):
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
        return self._post_export(payload)

    # ---- POST: 标注 ----
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
        except Exception as exc:  # noqa: BLE001
            self.send_error_json(500, f"保存失败: {exc}")

    # ---- POST: 编辑数据 ----
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
        except Exception as exc:  # noqa: BLE001
            self.send_error_json(500, f"保存失败: {exc}")

    # ---- POST: 导出 PDF ----
    def _post_export(self, payload):
        """
        应用编辑并产出一份新 PDF，直接以附件下载。

        注意：永远不写回原文件 —— 原文件是用户的资产，
        我们只产出新文件。这样即使导出逻辑有 bug，
        用户的原始 PDF 也不会被破坏。
        """
        src = urllib.parse.unquote(str(payload.get("path") or ""))
        if not src:
            return self.send_error_json(400, "缺少 path 参数")

        try:
            real = os.path.realpath(src)
            if not os.path.isfile(real):
                return self.send_error_json(404, "源文件不存在")
            if not is_within(real, SCAN_DIRS):
                return self.send_error_json(403, "该文件不在允许的扫描目录内")
        except OSError:
            return self.send_error_json(404, "源文件不存在")

        pages, objects, dropped = sanitize_edits(
            payload.get("pages"), payload.get("objects"))

        # 防线：涂黑对象绝不能走普通导出。
        # 普通导出是叠加式的（往内容流后面追加绘制指令），
        # 它没法删除底层文字 —— 让涂黑框从这里出去，用户会拿到
        # 一份"看着是黑块、文字还能被复制"的文件。必须挡掉。
        # 前端已经不再提交它们，这里是服务端的兜底。
        before_n = len(objects)
        objects = [o for o in objects if o.get("kind") != REDACT_KIND]
        dropped += before_n - len(objects)

        os.makedirs(EXPORT_DIR, exist_ok=True)
        stem = re.sub(r'[\\/:*?"<>|]', "_", os.path.splitext(
            os.path.basename(real))[0])
        stamp = time.strftime("%Y%m%d-%H%M%S")
        out_name = f"{stem}-已编辑-{stamp}.pdf"
        out_path = os.path.join(EXPORT_DIR, out_name)

        try:
            with _edit_lock:
                result = pdfedit.apply_edits(
                    real, out_path,
                    {"pages": pages, "objects": objects})
        except Exception as exc:  # noqa: BLE001
            return self.send_error_json(500, f"导出失败: {exc}")

        try:
            with open(out_path, "rb") as fp:
                body = fp.read()
        except OSError as exc:
            return self.send_error_json(500, f"读取导出文件失败: {exc}")

        self.send_response(200)
        self.send_header("Content-Type", "application/pdf")
        self.send_header("Content-Length", str(len(body)))
        self.send_header(
            "Content-Disposition",
            "attachment; filename*=UTF-8''" + urllib.parse.quote(out_name))
        self.send_header("X-Export-Pages", str(result.get("pages", 0)))
        self.send_header("X-Export-Objects", str(result.get("objects", 0)))
        self.send_header("X-Export-Dropped", str(dropped))
        # 中文字体：嵌了多少字形、有没有写不进去的字。
        # **缺字必须让用户看到** —— 静默丢字会让人以为写进去了，实际没有。
        # 响应头只能是 latin-1，所以缺字用百分号编码传。
        self.send_header("X-Export-CJK-Glyphs",
                         str(result.get("cjk_glyphs", 0)))
        miss = str(result.get("cjk_missing") or "")
        if miss:
            self.send_header("X-Export-CJK-Missing",
                             urllib.parse.quote(miss, safe=""))
        self.end_headers()
        self.wfile.write(body)

    # ---- POST: 涂黑（redaction，不可逆） ----

    def _post_redact(self, payload):
        """
        执行真正的信息销毁，产出新 PDF 供下载。

        ============================ 安全约束 ============================

        Redaction 是本应用**唯一不可逆**的操作 —— 原文真的没了。
        因此这里有几条硬性约束，不因前端请求而放宽：

        1. **永远另存为**。本端点只产出新文件到 exports/ 目录，
           绝不接受"写回原文件"的请求参数。前端也不提供这个选项。
           即使有人手工构造请求带上覆盖参数，服务端也不认。

        2. **拒绝零命中导出**。如果所有涂黑区加起来一个字形都没删到，
           说明用户以为盖住了什么、实际什么都没盖住 —— 这时候
           静默产出一个文件是最危险的（他以为安全了）。直接报错，
           让他先确认区域位置。

        3. 元数据与书签标题一并清理（泄漏面在引擎里处理）。
        """
        src = urllib.parse.unquote(str(payload.get("path") or ""))
        if not src:
            return self.send_error_json(400, "缺少 path 参数")

        # 显式拒绝任何试图覆盖原文件的请求（防御性，缺省就没有这个参数）
        if payload.get("overwrite") or payload.get("inPlace"):
            return self.send_error_json(
                400, "涂黑操作不允许覆盖原文件，只支持另存为")

        try:
            real = os.path.realpath(src)
            if not os.path.isfile(real):
                return self.send_error_json(404, "源文件不存在")
            if not is_within(real, SCAN_DIRS):
                return self.send_error_json(403, "该文件不在允许的扫描目录内")
        except OSError:
            return self.send_error_json(404, "源文件不存在")

        regions, dropped = sanitize_redactions(payload.get("regions"))
        if not regions:
            return self.send_error_json(400, "没有有效的涂黑区域")

        os.makedirs(EXPORT_DIR, exist_ok=True)
        stem = re.sub(r'[\\/:*?"<>|]', "_", os.path.splitext(
            os.path.basename(real))[0])
        stamp = time.strftime("%Y%m%d-%H%M%S")
        out_name = f"{stem}-已涂黑-{stamp}.pdf"
        out_path = os.path.join(EXPORT_DIR, out_name)

        try:
            with _edit_lock:
                result = pdfredact.apply_redactions(real, out_path, regions)
        except ValueError as exc:
            return self.send_error_json(400, f"涂黑失败: {exc}")
        except Exception as exc:  # noqa: BLE001
            return self.send_error_json(500, f"涂黑失败: {exc}")

        # 零命中：不进文件，直接报错 —— 静默产出会让用户误以为安全
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
        self.send_header("Content-Length", str(len(body)))
        self.send_header(
            "Content-Disposition",
            "attachment; filename*=UTF-8''" + urllib.parse.quote(out_name))
        self.send_header("X-Redact-Pages", str(result.get("pages_redacted", 0)))
        self.send_header("X-Redact-Glyphs", str(result.get("removed_glyphs", 0)))
        self.send_header("X-Redact-Meta",
                         str(len(result.get("metadata_cleaned") or [])))
        self.send_header("X-Redact-Outline",
                         str(result.get("outline_titles_cleaned", 0)))
        self.send_header("X-Redact-Dropped", str(dropped))
        self.end_headers()
        self.wfile.write(body)

    def do_HEAD(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/api/file":
            query = urllib.parse.parse_qs(parsed.query)
            return self.api_file(query, head_only=True)
        self.send_error(404)

    # ---- API: 文件列表 ----
    def api_files(self, query):
        # 支持 ?dir= 临时指定扫描目录
        extra = query.get("dir", [])
        dirs = list(SCAN_DIRS)
        for d in extra:
            d = urllib.parse.unquote(d)
            if os.path.isdir(d) and norm(d) not in {norm(x) for x in dirs}:
                dirs.append(d)
        try:
            files = scan_pdfs(dirs)
        except Exception as exc:  # noqa: BLE001
            return self.send_error_json(500, f"扫描失败: {exc}")
        self.send_json({
            "ok": True,
            "dirs": dirs,
            "files": files,
            "count": len(files),
            "truncated": len(files) >= MAX_LIST,
        })

    # ---- API: PDF 内容（支持 Range） ----
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
        if not is_within(real, SCAN_DIRS):
            return self.send_error_json(403, "该文件不在允许的扫描目录内")

        ctype = mimetypes.guess_type(real)[0] or "application/pdf"
        range_header = self.headers.get("Range")

        # 处理 Range：大文件随机跳页的关键
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
        self.send_header("Cache-Control", "public, max-age=3600")
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

    # ---- API: 读取标注 ----
    def api_get_annotations(self, query):
        did = (query.get("doc") or [""])[0]
        if not valid_doc_id(did):
            return self.send_error_json(400, "doc 参数无效")
        data = load_annotations(did)
        if not data.get("ok"):
            return self.send_error_json(500, data.get("error", "读取失败"))
        self.send_json(data)

    # ---- API: 导出 Markdown 笔记 ----
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

    # ---- API: 读取编辑数据 ----
    def api_get_edits(self, query):
        did = (query.get("doc") or [""])[0]
        if not valid_doc_id(did):
            return self.send_error_json(400, "doc 参数无效")
        data = load_edits(did)
        if not data.get("ok"):
            return self.send_error_json(500, data.get("error", "读取失败"))
        self.send_json(data)

    # ---- API: PDF 页数/尺寸信息 ----
    def api_pdfinfo(self, query):
        paths = query.get("path", [])
        if not paths:
            return self.send_error_json(400, "缺少 path 参数")
        target = urllib.parse.unquote(paths[0])
        try:
            real = os.path.realpath(target)
            if not os.path.isfile(real):
                return self.send_error_json(404, "文件不存在")
            if not is_within(real, SCAN_DIRS):
                return self.send_error_json(403, "该文件不在允许的扫描目录内")
            info = pdf_page_info(real)
        except Exception as exc:  # noqa: BLE001
            return self.send_error_json(500, f"解析失败: {exc}")
        self.send_json(info)

    # ---- API: 交给系统默认程序打开 ----
    def api_open(self, query):
        paths = query.get("path", [])
        if not paths:
            return self.send_error_json(400, "缺少 path 参数")
        target = urllib.parse.unquote(paths[0])
        try:
            real = os.path.realpath(target)
            if not os.path.isfile(real):
                return self.send_error_json(404, "文件不存在")
            if not is_within(real, SCAN_DIRS):
                return self.send_error_json(403, "该文件不在允许的扫描目录内")
            if sys.platform == "win32":
                os.startfile(real)  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                import subprocess
                subprocess.Popen(["open", real])
            else:
                import subprocess
                subprocess.Popen(["xdg-open", real])
        except Exception as exc:  # noqa: BLE001
            return self.send_error_json(500, f"打开失败: {exc}")
        self.send_json({"ok": True})

    # ---- 静态文件 ----
    def serve_file(self, path):
        if not os.path.isfile(path):
            return self.send_error_json(404, "文件未找到")
        ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
        # .mjs 需要正确的 JS MIME 类型
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

        with open(path, "rb") as fp:
            body = fp.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        # 开发期不缓存页面，保证改动即时生效
        if path.endswith((".html", ".css", ".js", ".mjs")):
            self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)


# --------------------------------------------------------------------------
# 启动
# --------------------------------------------------------------------------

def default_dirs():
    """默认扫描目录：项目下的 documents/ 与用户文档目录。"""
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


class Server(ThreadingHTTPServer):
    """本地服务用的 HTTP 服务器。

    ★ 覆盖 allow_reuse_address 不是"调优"，是 Windows 上的正确性问题。
    标准库的 HTTPServer 声明 allow_reuse_address = 1，而 SO_REUSEADDR
    在 Windows 上的语义是"允许绑定一个已被其他 socket 占用的端口"
    （MSDN 文档化的行为；本机实测：已有实例正在监听 127.0.0.1 时，
    默认类 bind 依然成功且不报错，关掉之后才抛 WinError 10048）。

    后果是第二个实例静默绑上同一端口，两个进程抢着接受连接，
    连接落到谁手上不确定 —— 表现就是"有时连不上、像在丢包"，
    而且日志里什么都看不到。

    关掉它，让端口冲突变成显式 OSError，交给 main() 的顺延循环处理。
    Linux 上 SO_REUSEADDR 只影响 TIME_WAIT 端口，行为无害，故保留。
    """
    daemon_threads = True
    allow_reuse_address = (os.name != "nt")


def main():
    global SCAN_DIRS, PORT, EXPORT_DIR, EDIT_DIR, ANNOT_DIR

    ap = argparse.ArgumentParser(description="本地 PDF 查看器服务")
    ap.add_argument("--dir", action="append", default=[],
                    help="要扫描的 PDF 目录，可重复指定")
    ap.add_argument("--port", type=int, default=8000, help="监听端口，默认 8000")
    ap.add_argument("--host", default="127.0.0.1", help="监听地址，默认仅本机")
    ap.add_argument("--no-browser", action="store_true", help="启动后不自动打开浏览器")
    # 可写目录重定向：**测试必须用它们**，否则测试会往用户真实的
    # exports/ edits/ annotations/ 里写东西（导出文件、编辑数据）。
    ap.add_argument("--export-dir", default=None,
                    help="导出产物目录，默认 <项目>/exports")
    ap.add_argument("--edits-dir", default=None,
                    help="编辑数据目录，默认 <项目>/edits")
    ap.add_argument("--annot-dir", default=None,
                    help="标注数据目录，默认 <项目>/annotations")
    args = ap.parse_args()

    if args.export_dir:
        EXPORT_DIR = os.path.realpath(args.export_dir)
    if args.edits_dir:
        EDIT_DIR = os.path.realpath(args.edits_dir)
    if args.annot_dir:
        ANNOT_DIR = os.path.realpath(args.annot_dir)

    SCAN_DIRS = [os.path.realpath(d) for d in (args.dir or default_dirs())
                 if os.path.isdir(d)]

    if not SCAN_DIRS:
        print("错误：没有可用的扫描目录。")
        print("请用 --dir 指定存放 PDF 的文件夹，例如：")
        print("  python server.py --dir D:\\我的PDF")
        return 1

    # ---- 先绑定，再打印，再开浏览器 ----
    # 原来的顺序是反的：先打印 URL、先开浏览器，最后才 bind ——
    # 一旦端口被占，用户看到的地址是错的，浏览器也指向一个空端口。
    #
    # 端口可能被上一次没关干净的实例（或别的程序）占着。
    # localhost 上 bind 是微秒级，所以不做花哨的可用性探测 ——
    # 直接顺序试绑即可：bind 既是最可靠的判据，又是原子的，
    # 不存在"探测到空闲、绑定时已被抢"的竞态窗口。
    httpd = None
    taken = []
    for p in range(args.port, args.port + PORT_SCAN_RANGE):
        try:
            httpd = Server((args.host, p), Handler)
            break
        except OSError:
            taken.append(p)

    if httpd is None:
        # 顺延范围内全被占：退到 0 号端口，让系统随便派一个空闲的
        httpd = Server((args.host, 0), Handler)

    # 以**真实绑定到的**端口为准，而不是 args.port
    PORT = httpd.server_address[1]

    url = f"http://{args.host}:{PORT}/"
    print("=" * 58)
    print("  PDF 查看器已启动")
    print("=" * 58)
    if taken:
        # 静默换端口会让人对着旧地址怀疑人生，必须明说
        span = str(taken[0]) if len(taken) == 1 else f"{taken[0]}~{taken[-1]}"
        print(f"  注意     : 端口 {span} 已被占用，已自动改用 {PORT}")
        print( "             若是上一次没关掉的实例，可先结束它再用原端口")
        print()
    print(f"  访问地址 : {url}")
    print(f"  扫描目录 :")
    for d in SCAN_DIRS:
        print(f"    - {d}")
    print()
    print("  提示：在页面上点「添加目录」可临时扫描其他位置。")
    print("  按 Ctrl+C 停止服务。")
    print("=" * 58)

    if not args.no_browser:
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
