"""测试临时目录 —— 统一约定。

★ 为什么必须有这个模块
    测试原来把临时文件写在 `documents/` 和 `exports/` 里 ——
    那是**用户的真实目录**（被扫描的 PDF、导出产物）。
    跑一次测试就往里扔十几个文件；API 测试更严重：它会通过
    /api/edits 写真实的编辑数据、通过 /api/export 产出真实的导出文件。

    这是设计问题，不是小毛病：用户不该为了"跑个测试"而担心
    自己的目录被塞东西或被覆盖。

现在的约定
    · 所有**写**操作走 `_scratch/`（已加 .gitignore，可整目录删除）
    · `documents/测试文档.pdf` **只读**，不写 —— 它是测试素材
    · 服务端也要用 `--export-dir/--edits-dir/--annot-dir` 指到 _scratch
      （见 run_tests.py）
    · 可用环境变量 PDFVIEWER_TEST_SCRATCH 覆盖位置
"""
import os

HERE = os.path.dirname(os.path.abspath(__file__))

# 临时文件的落地处
SCRATCH = os.environ.get("PDFVIEWER_TEST_SCRATCH") or \
    os.path.join(HERE, "_scratch")

# 只读的测试素材目录（用户目录，绝不往里写）
FIXTURE_DIR = os.path.join(HERE, "documents")

# 测试素材文件
FIXTURE_PDF = os.path.join(FIXTURE_DIR, "测试文档.pdf")

# 服务端在测试里应该使用的可写目录
SCRATCH_EXPORTS = os.path.join(SCRATCH, "exports")
SCRATCH_EDITS = os.path.join(SCRATCH, "edits")
SCRATCH_ANNOT = os.path.join(SCRATCH, "annotations")


def ensure(*parts):
    """取 _scratch 下的一个路径，并保证它的父目录存在。"""
    p = os.path.join(SCRATCH, *parts)
    d = os.path.dirname(p)
    if d and not os.path.isdir(d):
        os.makedirs(d, exist_ok=True)
    return p


def ensure_dirs():
    """把测试要用的可写目录都建好。"""
    for d in (SCRATCH, SCRATCH_EXPORTS, SCRATCH_EDITS, SCRATCH_ANNOT):
        os.makedirs(d, exist_ok=True)
