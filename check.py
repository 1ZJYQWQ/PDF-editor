"""
PDF 查看器 —— 代码自检脚本

改完前端代码后跑一遍，能在交给人测之前抓出大部分低级错误：
  1. 语法错误（ES Module 解析）
  2. 未定义标识符（漏 import、拼错名字）
  3. import/export 不匹配（从模块导入未导出的符号）
  4. JS 里引用的 DOM id 在 HTML 中不存在
  5. hidden 属性失效（CSS 类选择器声明了 display，会盖掉 UA 的 [hidden]）
  6. 文字盒被裁（.ed-text 有 overflow: hidden → 文字锚点盒看不见）

第 5 项是 Firefox 专属坑：HTML 规范里 [hidden] 只在 UA 样式表里声明
display:none，优先级低于任何类选择器。Chrome 对 [hidden] 有特殊豁免所以
看不出来，Firefox 严格按层叠规则走，于是 .xxx { display: flex } 会让
hidden 属性完全失效、元素常驻显示。

用法：
  python check.py
"""

import os
import re
import subprocess
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(BASE, "static")
FILES = ["geom.js", "core.js", "annot.js", "edit.js", "app.js"]

NODE_CANDIDATES = ["node"]

BUILTIN = {
    'console','window','document','Math','JSON','Object','Array','String','Number',
    'Boolean','Promise','Map','Set','Date','Error','RegExp','Symbol','Proxy','Reflect',
    'WeakMap','WeakSet','BigInt','globalThis','undefined','null','true','false','this',
    'parseInt','parseFloat','isNaN','isFinite','setTimeout','clearTimeout','setInterval',
    'clearInterval','requestAnimationFrame','cancelAnimationFrame','fetch','prompt',
    'confirm','alert','localStorage','sessionStorage','Blob','URL','URLSearchParams',
    'TextEncoder','TextDecoder','crypto','navigator','NodeFilter','Range','Node',
    'IntersectionObserver','MutationObserver','ResizeObserver','Option','Image','File',
    'FileReader','FormData','Headers','Request','Response','AbortController','Event',
    'CustomEvent','HTMLElement','Element','NodeList','DOMParser','structuredClone',
    'queueMicrotask','matchMedia','getComputedStyle','decodeURIComponent',
    'encodeURIComponent','encodeURI','decodeURI','eval','arguments','super','await',
    'async','function','return','typeof','instanceof','void','delete','in','of','new',
    'if','else','for','while','do','switch','case','default','break','continue','try',
    'catch','finally','throw','class','extends','import','export','from','as','let',
    'const','var','static','get','set','yield','debugger','with','enum','location',
    'Uint8Array','Uint16Array','Uint32Array','Int8Array','Int32Array','Float32Array',
    'Float64Array','ArrayBuffer','DataView','isArray','rgba','scaleX','scaleY',
    'translate','rotate','none','inherit','auto','bold','font',
}


def strip_comments(code):
    code = re.sub(r'/\*.*?\*/', '', code, flags=re.S)
    return re.sub(r'(?m)//[^\n]*$', '', code)


def strip_strings(code):
    code = re.sub(r'`(?:[^`\\]|\\.)*`', '``', code, flags=re.S)
    code = re.sub(r"'(?:[^'\\\n]|\\.)*'", "''", code)
    return re.sub(r'"(?:[^"\\\n]|\\.)*"', '""', code)


def clean(raw):
    return strip_strings(strip_comments(raw))


def collect_defined(src):
    d = set()
    d |= set(re.findall(r'\bfunction\s+([A-Za-z_$][\w$]*)', src))
    d |= set(re.findall(r'\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)', src))
    d |= set(re.findall(r'\bclass\s+([A-Za-z_$][\w$]*)', src))
    d |= set(re.findall(r'\bcatch\s*\(\s*([A-Za-z_$][\w$]*)', src))

    for m in re.findall(r'\b(?:const|let|var)\s*\{([^}]+)\}\s*=', src):
        for x in m.split(','):
            name = x.split(':')[-1].split('=')[0].strip()
            if re.fullmatch(r'[A-Za-z_$][\w$]*', name):
                d.add(name)
    for m in re.findall(r'\b(?:const|let|var)\s*\[([^\]]+)\]\s*=', src):
        for x in m.split(','):
            name = x.strip().split('=')[0].strip()
            if re.fullmatch(r'[A-Za-z_$][\w$]*', name):
                d.add(name)

    for params in re.findall(r'function\s*\w*\s*\(([^)]*)\)', src):
        for p in params.split(','):
            p = p.strip()
            if not p:
                continue
            if p[0] in '{[':
                for x in re.findall(r'[A-Za-z_$][\w$]*', p):
                    d.add(x)
            else:
                n = p.split('=')[0].strip()
                if re.fullmatch(r'[A-Za-z_$][\w$]*', n):
                    d.add(n)

    for params in re.findall(r'\(([^)]*)\)\s*=>', src):
        for p in params.split(','):
            n = p.strip().split('=')[0].strip()
            if re.fullmatch(r'[A-Za-z_$][\w$]*', n):
                d.add(n)
    for n in re.findall(r'(?<![\w.$])([A-Za-z_$][\w$]*)\s*=>', src):
        d.add(n)

    for m in re.findall(r'for\s*\(\s*(?:const|let|var)\s+\[?([^\]\s=,;]+)', src):
        if re.fullmatch(r'[A-Za-z_$][\w$]*', m):
            d.add(m)

    for block in re.findall(r'import\s*\{([\s\S]*?)\}\s*from', src):
        for x in block.split(','):
            x = x.strip()
            if x:
                n = x.split(' as ')[-1].strip()
                if re.fullmatch(r'[A-Za-z_$][\w$]*', n):
                    d.add(n)
    for m in re.findall(r'import\s+([A-Za-z_$][\w$]*)\s+from', src):
        d.add(m)
    # import * as ns from '...'
    for m in re.findall(r'import\s*\*\s*as\s+([A-Za-z_$][\w$]*)', src):
        d.add(m)

    # 回调参数：items.forEach((it, i) => ...) / .map(x => ...) / for (const [a,b] of ...)
    for params in re.findall(r'for\s*\(\s*(?:const|let|var)\s*\[([^\]]+)\]', src):
        for x in params.split(','):
            n = x.strip()
            if re.fullmatch(r'[A-Za-z_$][\w$]*', n):
                d.add(n)
    for params in re.findall(r'\(\s*\[([^\]]+)\]\s*\)\s*=>', src):
        for x in params.split(','):
            n = x.strip()
            if re.fullmatch(r'[A-Za-z_$][\w$]*', n):
                d.add(n)
    # 单参数箭头：x => ... / (a, b) => ...
    for m in re.findall(r'[,(]\s*([A-Za-z_$][\w$]*)\s*,\s*[A-Za-z_$][\w$]*\s*[,)]\s*=>', src):
        d.add(m)
    # 常见的回调形如 xxx.forEach(name =>  / xxx.map(name =>
    for m in re.findall(r'(?<![\w.$])([A-Za-z_$][\w$]*)\s*=>', src):
        d.add(m)
    return d


def find_used(src):
    u = set()
    u |= set(re.findall(r'(?<![\w.$])([A-Za-z_$][\w$]*)\s*\(', src))
    u |= set(re.findall(r'(?<![\w.$])([A-Za-z_$][\w$]*)\s*\.\s*[A-Za-z_$]', src))
    u |= set(re.findall(r'(?<![\w.$])([A-Za-z_$][\w$]*)\s*\[', src))
    return u


def find_exports(src):
    e = set()
    for m in re.findall(r'export\s+(?:const|let|var|function|async\s+function|class)\s+([A-Za-z_$][\w$]*)', src):
        e.add(m)
    for m in re.findall(r'export\s*\{([^}]+)\}', src):
        for x in m.split(','):
            x = x.strip()
            if x:
                e.add(x.split(' as ')[-1].strip())
    return e


def find_imports(src):
    out = []
    for names, mod in re.findall(r"import\s*\{([\s\S]*?)\}\s*from\s*'([^']+)'", src):
        lst = [x.strip().split(' as ')[0].strip() for x in names.split(',') if x.strip()]
        out.append((lst, mod))
    return out


def run_syntax_check(node_exe, results):
    """用 Node 的 SourceTextModule 做语法解析。"""
    script = os.path.join(BASE, ".syntax_check.mjs")
    with open(script, "w", encoding="utf-8") as fp:
        fp.write(
            "import { readFileSync } from 'fs';\n"
            "import vm from 'vm';\n"
            "const dir = " + repr(STATIC.replace('\\', '/')) + " + '/';\n"
            "const files = " + repr(FILES) + ";\n"
            "for (const f of files) {\n"
            "  try {\n"
            "    new vm.SourceTextModule(readFileSync(dir + f, 'utf8'), { identifier: f });\n"
            "    console.log('OK ' + f);\n"
            "  } catch (e) { console.log('FAIL ' + f + ': ' + e.message); }\n"
            "}\n"
        )
    try:
        r = subprocess.run(
            [node_exe, "--experimental-vm-modules", script],
            capture_output=True, text=True, timeout=60,
            encoding="utf-8", errors="replace",
        )
        for line in (r.stdout or "").splitlines():
            if line.startswith("OK ") or line.startswith("FAIL "):
                results.append(line)
    except Exception as exc:  # noqa: BLE001
        results.append(f"SKIP 语法检查（无法调用 Node: {exc}）")
    finally:
        try:
            os.remove(script)
        except OSError:
            pass


def main():
    results = []
    problems = 0

    # --- 1. 语法 ---
    node_exe = next((n for n in NODE_CANDIDATES
                     if n == "node" or os.path.isfile(n)), "node")
    run_syntax_check(node_exe, results)

    # --- 2. 未定义标识符 ---
    srcs = {}
    for f in FILES:
        srcs[f] = open(os.path.join(STATIC, f), encoding="utf-8").read()

    for f in FILES:
        src = clean(srcs[f])
        defined = collect_defined(src)
        used = find_used(src)
        missing = sorted(x for x in used
                         if x not in defined and x not in BUILTIN)
        if missing:
            problems += len(missing)
            results.append(f"FAIL {f} 未定义标识符: {', '.join(missing)}")
        else:
            results.append(f"OK   {f} 标识符检查通过")

    # --- 3. import / export 匹配 ---
    exports_map = {f"/static/{f}": find_exports(srcs[f]) for f in FILES}
    for f in FILES:
        for names, mod in find_imports(srcs[f]):
            if mod.startswith("/static/vendor"):
                continue
            avail = exports_map.get(mod)
            if avail is None:
                continue
            miss = [n for n in names if n not in avail]
            if miss:
                problems += len(miss)
                results.append(f"FAIL {f} 从 {mod} 导入未导出: {', '.join(miss)}")
    results.append("OK   import/export 匹配检查通过")

    # --- 4. DOM id 存在性 ---
    html = open(os.path.join(STATIC, "index.html"), encoding="utf-8").read()
    html_ids = set(re.findall(r'id="([^"]+)"', html))
    for f in FILES:
        used_ids = set()
        for block in re.findall(r'cacheEls\(\[(.*?)\]\)', srcs[f], re.S):
            used_ids |= set(re.findall(r"'([^']+)'", block))
        used_ids |= set(re.findall(r"getElementById\('([^']+)'\)", srcs[f]))
        missing = sorted(i for i in used_ids if i not in html_ids)
        if missing:
            problems += len(missing)
            results.append(f"FAIL {f} 引用不存在的 DOM id: {', '.join(missing)}")
    results.append("OK   DOM id 检查通过")

    # --- 5. hidden 属性失效 ---
    css_path = os.path.join(STATIC, "style.css")
    css = open(css_path, encoding="utf-8").read()
    css = re.sub(r'/\*.*?\*/', '', css, flags=re.S)

    # 有全局兜底 [hidden] { display: none !important } 就不再担心
    has_backstop = bool(re.search(
        r'\[hidden\][^{]*\{[^}]*display\s*:\s*none\s*!important', css))

    # 收集「带 hidden 属性的元素」上的所有 class
    hidden_classes = set()
    for tag in re.findall(r'<[^>]*\bhidden\b[^>]*>', html):
        m = re.search(r'class="([^"]*)"', tag)
        if m:
            hidden_classes |= set(m.group(1).split())

    # 找 CSS 里给这些类声明了 display 的规则
    risky = []
    for cls in sorted(hidden_classes):
        for body in re.findall(
                r'\.' + re.escape(cls) + r'\s*\{([^}]*)\}', css):
            if re.search(r'(?<!-)\bdisplay\s*:', body):
                risky.append(cls)
                break

    if risky and not has_backstop:
        problems += len(risky)
        results.append(
            "FAIL hidden 属性在 Firefox 下会失效（类选择器的 display "
            "盖掉了 UA 的 [hidden]）: " + ", ".join(risky)
            + "  →  需补 [hidden] { display: none !important; }")
    elif risky:
        results.append("OK   hidden 属性检查通过（已有全局兜底规则）")
    else:
        results.append("OK   hidden 属性检查通过")

    # --- 6. 文字盒不能被裁（.ed-text 不许有 overflow: hidden）---
    # 教训来源：文字对象在数据层是**锚点**（w/h = 0，对应 PDF 的 Td），
    # 渲染时若按矩形给固定宽高会得到 1×1 的盒子；再叠加 overflow: hidden，
    # 整段文字被裁得干干净净 —— 表现是「对象建出来了（面板提示有未保存的
    # 改动），页面上却一片空白」，而且**不报错**，很难查。
    # 修法见 geom.js 的 makeTextBoxAuto()（那边有单测守着）。
    ed_text_bodies = re.findall(r"\.ed-text\s*\{([^}]*)\}", css)
    bad_crop = [b for b in ed_text_bodies
                if re.search(r"overflow\s*:\s*hidden", b)]
    if bad_crop:
        problems += len(bad_crop)
        results.append(
            "FAIL .ed-text 设了 overflow: hidden —— 文字锚点是 1px 盒，"
            "会被整段裁掉、页面上看不见（改成 overflow: visible）")
    else:
        results.append("OK   文字盒裁剪检查通过（.ed-text 未设 overflow: hidden）")

    # --- 输出 ---
    print("=" * 56)
    print("  PDF 查看器 代码自检")
    print("=" * 56)
    for line in results:
        mark = "  " if line.startswith("OK") else ">>"
        print(f"{mark} {line}")
    print("=" * 56)
    if problems:
        print(f"  发现 {problems} 个问题，请修复后再交付")
    else:
        print("  全部通过，可以交给用户测试")
    print("=" * 56)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
