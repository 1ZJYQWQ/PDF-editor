"""一条命令跑完全部测试。

为什么需要它
    引擎测试（pdffont / pdfembed / pdfedit / pdfredact）是自包含的，
    但两个 API 测试**需要有服务在跑**，而且原来把地址写死成
    127.0.0.1:8000 —— 得先手动起服务、端口还可能被占，很别扭。

    这里由脚本自己解决：
      · 用 `--port 0` 让系统分配空闲端口
        （顺便把服务端「端口自动分配」这条路也跑了一遍）
      · 从服务打印的「访问地址」里读出**真实端口**，不靠猜
      · 通过环境变量 PDFVIEWER_TEST_BASE 注入给 API 测试
      · 跑完自动收掉服务，不留残留进程

用法
    python run_tests.py            全部
    python run_tests.py --quick    只跑自包含的套件（不起服务）
    python run_tests.py --list     列出会跑哪些
"""
import os
import re
import subprocess
import sys
import time

import testscratch as ts

HERE = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable

# .mjs 用 node 跑（前端的纯逻辑测试）。路径与 check.py 保持一致。
NODE_CANDIDATES = ["node"]

ENGINE_TESTS = [
    ("pdffont_test.py", "TrueType 子集化"),
    ("pdfembed_test.py", "中文字体嵌入 Type0"),
    ("pdfedit_test.py", "PDF 编辑引擎"),
    ("pdfedit_verify.py", "输出 PDF 结构校验"),
    ("pdfredact_test.py", "涂黑引擎"),
]
API_TESTS = [
    ("editapi_test.py", "编辑 API 端到端"),
    ("redactapi_test.py", "涂黑 API 端到端"),
]
OTHER = [
    ("check.py", "前端静态自检"),
    ("webtest.mjs", "前端纯逻辑（node）"),
]


def interp_for(name):
    """按扩展名挑解释器：.mjs/.js 用 node，其余用当前 python。"""
    if name.endswith((".mjs", ".js")):
        for c in NODE_CANDIDATES:
            if os.path.sep in c:
                if os.path.exists(c):
                    return c
            else:
                return c
        return "node"
    return PY


def run_one(name, env=None, timeout=600):
    t0 = time.time()
    try:
        p = subprocess.run([interp_for(name), name], cwd=HERE,
                           capture_output=True, text=True,
                           encoding="utf-8", errors="replace",
                           env=env, timeout=timeout)
        return p.returncode, (p.stdout or "") + (p.stderr or ""), \
            time.time() - t0
    except subprocess.TimeoutExpired:
        return 124, "（超时）", time.time() - t0
    except FileNotFoundError as exc:
        return 127, "（跑不起来：%s）" % exc, time.time() - t0


def summary_of(out):
    """从输出里挑一条最能说明结果的摘要行。"""
    best = ""
    for line in out.splitlines():
        s = line.strip()
        if not s:
            continue
        if "通过" in s and "失败" in s:
            best = s
        elif "全部通过" in s or "可以交给用户测试" in s:
            best = s
        elif best == "" and ("FAIL" in s or s.startswith("- ")):
            best = s
    return best[:58]


def start_server():
    """起一个测试用服务。端口交给系统分配，再从它打印的地址里读出来。

    ★ 三个可写目录全部指到 _scratch/ —— 测试**绝不能**往用户真实的
      exports/ edits/ annotations/ 里写东西（导出产物、编辑数据）。
      扫描目录 documents/ 只作**只读**使用。

    返回 (进程, base_url)；起不来则返回 (None, None)。
    """
    ts.ensure_dirs()
    cmd = [PY, "-u", "server.py", "--port", "0", "--no-browser",
           "--dir", os.path.join(HERE, "documents"),
           "--export-dir", ts.SCRATCH_EXPORTS,
           "--edits-dir", ts.SCRATCH_EDITS,
           "--annot-dir", ts.SCRATCH_ANNOT]
    proc = subprocess.Popen(cmd, cwd=HERE, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True,
                            encoding="utf-8", errors="replace")
    base = None
    deadline = time.time() + 30
    while time.time() < deadline:
        line = proc.stdout.readline()
        if not line:
            if proc.poll() is not None:
                break
            continue
        m = re.search(r"访问地址\s*:\s*(http://[\d.]+:\d+)/", line)
        if m:
            base = m.group(1)
            break
    if base is None:
        try:
            proc.kill()
        except OSError:
            pass
        return None, None
    return proc, base


def main(argv):
    quick = "--quick" in argv
    if "--list" in argv:
        for n, d in ENGINE_TESTS + OTHER + API_TESTS:
            print("  %-22s %s" % (n, d))
        return 0

    print("=" * 68)
    print("  PDF 查看器 测试总入口")
    print("=" * 68)
    print("  临时文件目录: %s" % ts.SCRATCH)
    print("  说明: 测试的写入全部落在这里，**不碰** documents/ exports/")
    print("        edits/ annotations/ 这些用户目录（documents/ 只读扫描）")

    rows = []
    for name, desc in ENGINE_TESTS + OTHER:
        print("\n>>> %s  (%s)" % (name, desc))
        rc, out, dt = run_one(name)
        rows.append((name, rc, dt, summary_of(out)))
        if rc != 0:
            print(out[-2500:])

    if quick:
        print("\n（--quick：跳过需要服务的 API 测试）")
    else:
        print("\n--- 起测试用服务（端口由系统分配）---")
        proc, base = start_server()
        if proc is None:
            print("!! 服务起不来，跳过 API 测试")
            for name, _d in API_TESTS:
                rows.append((name, 125, 0.0, "服务未起来"))
        else:
            print("    服务地址: %s" % base)
            env = dict(os.environ, PDFVIEWER_TEST_BASE=base)
            try:
                for name, desc in API_TESTS:
                    print("\n>>> %s  (%s)" % (name, desc))
                    rc, out, dt = run_one(name, env=env)
                    rows.append((name, rc, dt, summary_of(out)))
                    if rc != 0:
                        print(out[-2500:])
            finally:
                proc.terminate()
                try:
                    proc.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    proc.kill()
                print("\n--- 测试服务已收掉 ---")

    print()
    print("=" * 68)
    print("  汇总")
    print("=" * 68)
    bad = 0
    for name, rc, dt, sm in rows:
        if rc != 0:
            bad += 1
        print("  %-4s %-22s %6.1fs  %s"
              % ("OK" if rc == 0 else "FAIL", name, dt, sm))
    print("=" * 68)
    print("  %d 个套件，%d 个失败" % (len(rows), bad))
    print("=" * 68)
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
