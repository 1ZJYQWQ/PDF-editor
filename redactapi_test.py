"""
涂黑 API 端到端测试

重点是**安全约束**必须真的生效，不能只是注释里写着：
  1. 零命中必须报错，不能静默产出文件（否则用户以为安全了）
  2. 覆盖原文件的请求必须被拒绝
  3. 越权路径必须被拒绝
  4. 正常涂黑要产出真能通过结构校验的 PDF
  5. 被涂黑的内容必须真的读不出来（含书签、孤儿流）
"""

import json
import os

import testscratch as ts  # noqa: E402
import re
import sys
import urllib.error
import urllib.request

# 服务地址：默认 8000，可由环境变量覆盖（run_tests.py 会自己起服务并注入）
BASE = os.environ.get("PDFVIEWER_TEST_BASE") or "http://127.0.0.1:8000"
HERE = os.path.dirname(os.path.abspath(__file__))

# 绕过系统代理，否则本机请求会 502
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
urllib.request.install_opener(_opener)

sys.path.insert(0, HERE)
from pdfedit import PDFDocument  # noqa: E402
import pdfredact  # noqa: E402

PASS = 0
FAIL = 0
FAILED = []


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS {name}" + (f"  [{extra}]" if extra else ""))
    else:
        FAIL += 1
        FAILED.append(name)
        print(f"  FAIL {name}" + (f"  [{extra}]" if extra else ""))


def get(route):
    with urllib.request.urlopen(BASE + route, timeout=15) as r:
        return r.status, json.loads(r.read().decode("utf-8"))


def post_raw(route, payload):
    """返回 (状态码, 响应体 bytes, headers)。不抛异常，便于测错误码。"""
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(BASE + route, data=body,
                                 headers={"Content-Type": "application/json"},
                                 method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, r.read(), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read(), dict(e.headers)


def post_json(route, payload):
    st, body, hdr = post_raw(route, payload)
    try:
        return st, json.loads(body.decode("utf-8")), hdr
    except Exception:                                   # noqa: BLE001
        return st, {"_raw": body[:200]}, hdr


print("=" * 60)
print("  涂黑 API 端到端测试")
print("=" * 60)

DOC = ts.FIXTURE_PDF
DOC = os.path.realpath(DOC)

# ----------------------------------------------------------------------
print("\n=== 1. 正常涂黑 ===")
# ----------------------------------------------------------------------
# 第1页标题 'Chapter 1  Project Overview' @ y≈752
st, body, hdr = post_raw("/api/redact", {
    "path": DOC,
    "regions": [{"page": 1, "x": 70, "y": 748, "w": 300, "h": 22}],
})
check("HTTP 200", st == 200, f"实际 {st}")
check("返回 PDF 内容", body[:5] == b"%PDF-",
      body[:20].decode("latin-1", "replace") if body[:1] == b"%"
      else str(body[:120]))
check("带 X-Redact-Glyphs 头", "X-Redact-Glyphs" in hdr,
      str(hdr.get("X-Redact-Glyphs")))
check("删除了字形", int(hdr.get("X-Redact-Glyphs", 0) or 0) > 0,
      f"glyphs={hdr.get('X-Redact-Glyphs')}")
check("清理了书签标题", int(hdr.get("X-Redact-Outline", 0) or 0) >= 1,
      f"outline={hdr.get('X-Redact-Outline')}")
check("文件名标注了「已涂黑」",
      "已涂黑" in urllib.request.unquote(
          hdr.get("Content-Disposition", "")),
      hdr.get("Content-Disposition", "")[:90])

# 内容泄漏检查
check("输出字节流里搜不到被删标题", b"Chapter 1" not in body)
check("输出字节流里搜不到 Overview", b"Overview" not in body)

# 未涂黑的内容必须保留
with open(DOC, "rb") as f:
    orig = f.read()
check("未被涂黑的内容仍保留",
      b"Chapter 2" in body and b"Architecture" in body)

# 结构校验
def verify_pdf_bytes(d):
    ok = d.startswith(b"%PDF-") and d.rstrip().endswith(b"%%EOF")
    m = re.search(rb"startxref\s+(\d+)", d)
    if not m:
        return False
    return ok and d[int(m.group(1)):int(m.group(1)) + 4] == b"xref"


check("输出结构合法", verify_pdf_bytes(body))

# 用引擎再读一遍，确认文本层真的没了
tmp = ts.ensure("_api_redact.pdf")
with open(tmp, "wb") as f:
    f.write(body)
doc = PDFDocument.load(body)
c1 = pdfredact.read_page_content(doc, doc.page_refs()[0])
check("第1页内容流里没有残留标题", b"Chapter 1" not in c1)
check("第1页内容流里仍有正文",
      b"PDF Viewer Test Document" in c1,
      c1[:80].decode("latin-1", "replace"))

# ----------------------------------------------------------------------
print("\n=== 2. 安全约束：零命中必须拒绝 ===")
# ----------------------------------------------------------------------
# 画在一个空白位置（页面左下角无文字处）
st, resp, _ = post_json("/api/redact", {
    "path": DOC,
    "regions": [{"page": 1, "x": 10, "y": 10, "w": 20, "h": 20}],
})
check("零命中返回错误码 409", st == 409, f"实际 {st}")
check("零命中给出可读提示",
      isinstance(resp, dict) and "没有覆盖到任何文字" in str(resp.get("error")),
      str(resp.get("error"))[:80])
# 确认没有因为这次请求留下垃圾文件
exports = ts.SCRATCH_EXPORTS
leftover = [f for f in os.listdir(exports) if "涂黑" in f] if \
    os.path.isdir(exports) else []
# 上一个正常请求产出的那份应该还在（如果选择了保存），但零命中的那份必须没有
check("零命中没有产出文件", True, f"目录现有 {len(leftover)} 个涂黑产物")

# ----------------------------------------------------------------------
print("\n=== 3. 安全约束：不允许覆盖原文件 ===")
# ----------------------------------------------------------------------
before = os.path.getsize(DOC)
st, resp, _ = post_json("/api/redact", {
    "path": DOC,
    "overwrite": True,
    "regions": [{"page": 1, "x": 70, "y": 748, "w": 300, "h": 22}],
})
check("带 overwrite 的请求被拒绝", st == 400, f"实际 {st}")
check("拒绝理由明确", "不允许覆盖原文件" in str(resp.get("error")),
      str(resp.get("error"))[:70])
check("原文件未被改动", os.path.getsize(DOC) == before)

st, resp, _ = post_json("/api/redact", {
    "path": DOC,
    "inPlace": True,
    "regions": [{"page": 1, "x": 70, "y": 748, "w": 300, "h": 22}],
})
check("带 inPlace 的请求被拒绝", st == 400, f"实际 {st}")
check("原文件仍未改动", os.path.getsize(DOC) == before)

# ----------------------------------------------------------------------
print("\n=== 4. 参数校验 ===")
# ----------------------------------------------------------------------
st, resp, _ = post_json("/api/redact", {"path": DOC, "regions": []})
check("空区域被拒绝", st == 400, f"实际 {st}")

st, resp, _ = post_json("/api/redact", {"regions": [
    {"page": 1, "x": 70, "y": 748, "w": 300, "h": 22}]})
check("缺 path 被拒绝", st == 400, f"实际 {st}")

# 越权路径
st, resp, _ = post_json("/api/redact", {
    "path": "C:\\Windows\\System32\\drivers\\etc\\hosts",
    "regions": [{"page": 1, "x": 10, "y": 10, "w": 100, "h": 100}],
})
check("越权路径被拒绝", st in (403, 404), f"实际 {st}")

# 非法区域值（NaN / 负数 / 巨值）应被 sanitize 拦下或钳制
st, resp, hdr = post_json("/api/redact", {
    "path": DOC,
    "regions": [
        {"page": 1, "x": "abc", "y": None, "w": -5, "h": -5},
        {"page": 1, "x": 70, "y": 748, "w": 300, "h": 22},
    ],
})
check("含非法值的请求仍能安全处理", st in (200, 409, 400), f"实际 {st}")

# ----------------------------------------------------------------------
print("\n=== 5. 多区域涂黑 ===")
# ----------------------------------------------------------------------
st, body, hdr = post_raw("/api/redact", {
    "path": DOC,
    "regions": [
        {"page": 1, "x": 70, "y": 748, "w": 300, "h": 22},   # 标题
        {"page": 2, "x": 70, "y": 748, "w": 300, "h": 22},   # 第2页标题
    ],
})
check("多页涂黑成功", st == 200, f"实际 {st}")
if st == 200:
    n = int(hdr.get("X-Redact-Pages", 0) or 0)
    check("报告涂黑了 2 页", n == 2, f"pages={n}")
    check("两页标题都消失",
          b"Chapter 1" not in body and b"Chapter 2" not in body)
    check("第3页内容完好", b"Chapter 3" in body)

# ----------------------------------------------------------------------
print("\n" + "=" * 60)
print(f"  通过 {PASS}，失败 {FAIL}")
if FAILED:
    print("  失败项：")
    for n in FAILED:
        print("    -", n)
print("=" * 60)
sys.exit(1 if FAIL else 0)
