"""
编辑 API 端到端测试。

用真实 HTTP 请求打服务端（而非直接调函数），
这样能覆盖路由、序列化、响应头等真实链路。
"""
import json
import os

import testscratch as ts  # noqa: E402
import sys
import re
import urllib.parse
import urllib.request

sys.stdout.reconfigure(encoding="utf-8")

# 本机测试必须绕开系统代理，否则请求会被代理拦成 502
_proxy_handler = urllib.request.ProxyHandler({})
_opener = urllib.request.build_opener(_proxy_handler)
urllib.request.install_opener(_opener)

# 服务地址：默认 8000，可由环境变量覆盖（run_tests.py 会自己起服务并注入）
BASE = os.environ.get("PDFVIEWER_TEST_BASE") or "http://127.0.0.1:8000"
DOC = ts.FIXTURE_PDF
DOC_ID = None

passed = failed = 0


def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  OK   {name}")
    else:
        failed += 1
        print(f"  FAIL {name}  {detail}")


def get(route, **params):
    url = BASE + route
    if params:
        url += "?" + urllib.parse.urlencode(params)
    with urllib.request.urlopen(url, timeout=20) as r:
        return r.status, r.read()


def post_json(route, payload):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        BASE + route, data=data,
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.status, r.read()


print("=" * 58)
print("  编辑 API 端到端测试")
print("=" * 58)

# ---- 1. 基础连通 ----
print("\n[1] 连通性与文件识别")
st, body = get("/api/files")
files = json.loads(body).get("files", [])
check("文件列表可读", st == 200 and len(files) > 0)
target = next((f for f in files if f["path"] == DOC), None)
if not target:
    target = next((f for f in files if "测试" in f["name"]), None)
check("找到测试文档", target is not None)
if target:
    DOC_ID = target["docId"]
    print(f"       docId={DOC_ID}  name={target['name']}")

# ---- 2. PDF 信息 ----
print("\n[2] /api/pdfinfo 页数与尺寸")
st, body = get("/api/pdfinfo", path=DOC)
info = json.loads(body)
check("pdfinfo 返回 ok", st == 200 and info.get("ok"))
check("页数为 5", info.get("count") == 5, f"实际 {info.get('count')}")
if info.get("pages"):
    p0 = info["pages"][0]
    check("首页有尺寸", p0["width"] > 0 and p0["height"] > 0,
          f"{p0['width']}x{p0['height']}")
    print(f"       首页: {p0['width']:.0f} x {p0['height']:.0f} pt, "
          f"rotate={p0['rotate']}")

# ---- 3. 编辑数据读写 ----
print("\n[3] 编辑数据读写（/api/edits）")
st, body = get("/api/edits", doc=DOC_ID)
d = json.loads(body)
check("读取编辑数据 ok", st == 200 and d.get("ok"))

test_edits = {
    "docId": DOC_ID,
    "doc": {"name": "测试文档"},
    "pages": {"rotate": {"0": 90}, "delete": [], "insert": []},
    "objects": [
        {"page": 1, "kind": "whiteout", "x": 60, "y": 700, "w": 200, "h": 20},
        {"page": 1, "kind": "text", "x": 60, "y": 690, "text": "API test",
         "size": 14, "color": "#cc0000", "font": "Helvetica"},
    ],
}
st, body = post_json("/api/edits", test_edits)
r = json.loads(body)
check("写入编辑数据 ok", st == 200 and r.get("ok"),
      f"status={st} body={body[:200]}")
check("对象数正确", r.get("objects") == 2, f"实际 {r.get('objects')}")

st, body = get("/api/edits", doc=DOC_ID)
d = json.loads(body)
check("回读对象数一致", len(d.get("objects", [])) == 2)
check("回读旋转一致", str(d.get("pages", {}).get("rotate", {}).get("0")) == "90")

# ---- 4. 非法数据被拦截 ----
print("\n[4] 非法数据拦截")
bad = {
    "docId": DOC_ID,
    "pages": {"rotate": {"0": 45}},            # 45 不是 90 的倍数
    "objects": [
        {"page": 0, "kind": "text", "text": "坏页号"},          # page=0 非法
        {"page": 1, "kind": "hack", "text": "非法类型"},         # kind 非法
        {"page": 1, "kind": "text", "text": ""},               # 空文本
        {"page": 1, "kind": "text", "text": "合法", "size": 99999},  # 尺寸超限
    ],
}
st, body = post_json("/api/edits", bad)
r = json.loads(body)
check("非法项被丢弃", r.get("dropped", 0) >= 3, f"dropped={r.get('dropped')}")
check("仅保留合法对象", r.get("objects") == 1, f"实际 {r.get('objects')}")

# ---- 5. 导出 PDF ----
print("\n[5] 导出 PDF（/api/export）")
export_req = {
    "path": DOC,
    "pages": {"rotate": {"0": 90},
              "insert": [{"at": 2, "width": 595, "height": 842}]},
    "objects": [
        {"page": 1, "kind": "whiteout", "x": 60, "y": 700, "w": 200, "h": 20},
        {"page": 1, "kind": "text", "x": 60, "y": 685, "text": "Exported!",
         "size": 16, "color": "#0066cc"},
        {"page": 2, "kind": "rect", "x": 100, "y": 500, "w": 150, "h": 80,
         "color": "#ff0000", "stroke": True, "lineWidth": 2},
    ],
}
data = json.dumps(export_req).encode("utf-8")
req = urllib.request.Request(
    BASE + "/api/export", data=data,
    headers={"Content-Type": "application/json"}, method="POST")
with urllib.request.urlopen(req, timeout=60) as r:
    pdf_bytes = r.read()
    hdr_pages = r.headers.get("X-Export-Pages")
    hdr_objs = r.headers.get("X-Export-Objects")
    disp = r.headers.get("Content-Disposition") or ""
    ctype = r.headers.get("Content-Type")

check("返回 PDF 类型", ctype == "application/pdf", f"实际 {ctype}")
check("文件非空", len(pdf_bytes) > 1000, f"{len(pdf_bytes)} 字节")
check("以 PDF 开头", pdf_bytes.startswith(b"%PDF-"))
check("以 %%EOF 结尾", pdf_bytes.rstrip().endswith(b"%%EOF"))
check("页数 = 6（原5+插入1）", hdr_pages == "6", f"实际 {hdr_pages}")
check("对象数 = 3", hdr_objs == "3", f"实际 {hdr_objs}")
check("带下载文件名", "attachment" in disp, disp)
print(f"       导出 {len(pdf_bytes)} 字节，文件名头: {disp[:80]}")

outp = ts.ensure("exports", "_api_test.pdf")
with open(outp, "wb") as fp:
    fp.write(pdf_bytes)
print(f"       已存: {os.path.abspath(outp)}")

# ---- 6. 导出的 PDF 结构自校验 ----
print("\n[6] 导出文件结构校验")
m = re.search(rb"startxref\s+(\d+)\s+%%EOF\s*$", pdf_bytes)
check("有 startxref", m is not None)
if m:
    sx = int(m.group(1))
    check("startxref 指向 xref", pdf_bytes[sx:sx + 4] == b"xref",
          f"实际 {pdf_bytes[sx:sx+6]!r}")

seg = pdf_bytes[sx:] if m else b""
hm = re.match(rb"xref\s+(\d+)\s+(\d+)\s+", seg)
if hm:
    entries = re.findall(rb"(\d{10}) (\d{5}) ([nf])", seg[hm.end():])
    start_obj = int(hm.group(1))
    bad_off = 0
    for i, (off, gen, typ) in enumerate(entries):
        if typ == b"f":
            continue
        objnum = start_obj + i
        o = int(off)
        if pdf_bytes[o:o + len(b"%d 0 obj" % objnum)] != b"%d 0 obj" % objnum:
            bad_off += 1
    check("所有对象偏移正确", bad_off == 0, f"{bad_off} 个错误")

page_n = len(re.findall(rb"/Type\s*/Page(?![sA-Za-z])", pdf_bytes))
check("实际 Page 对象 = 6", page_n == 6, f"实际 {page_n}")

cnt = re.search(rb"/Type\s*/Pages[^>]*?/Count\s+(\d+)", pdf_bytes)
if not cnt:
    cnt = re.search(rb"/Count\s+(\d+)[^>]*?/Type\s*/Pages", pdf_bytes)
check("页面树 Count = 6", cnt and cnt.group(1) == b"6",
      f"实际 {cnt.group(1) if cnt else '?'}")

# ---- 7. 路径安全 ----
print("\n[7] 路径安全")
try:
    bad_req = json.dumps({"path": "C:/Windows/System32/drivers/etc/hosts"}).encode()
    req = urllib.request.Request(
        BASE + "/api/export", data=bad_req,
        headers={"Content-Type": "application/json"}, method="POST")
    urllib.request.urlopen(req, timeout=10)
    check("越权路径被拒", False, "竟然成功了")
except urllib.error.HTTPError as e:
    check("越权路径被拒", e.code in (403, 404), f"返回 {e.code}")
except Exception as e:
    check("越权路径被拒", False, str(e))

# ---- 汇总 ----
print()
print("=" * 58)
print(f"  通过 {passed} 项，失败 {failed} 项")
print("=" * 58)
sys.exit(1 if failed else 0)
