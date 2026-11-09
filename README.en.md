# PDF Viewer

Local-only, offline PDF tool with **zero third-party dependencies**.

[简体中文](README.md) | English

---

## Why it's worth a look

1. **Real redaction** — text is removed from the content stream, not painted over.
   `strings` won't find it afterwards.
2. **Chinese text** — a hand-rolled TrueType subsetter embeds the glyphs you use.
   Selectable and copyable, but not editable (the outlines are baked in).
3. **Edit a paragraph and re-wrap** — rewrite a paragraph, it re-flows; what doesn't
   fit continues onto the next page.
4. **Annotations are native PDF comments** — still editable and deletable in Firefox
   or Acrobat after export.

---

## What it does

| | |
|---|---|
| **Read** | Zoom, paging, single / two-page view, thumbnails, outline, search, bookmarks, invert colors |
| **Tabs** | Several documents in one window, each keeping its own scroll position, zoom, edit mode and undo stack (Ctrl+Tab to cycle, Ctrl+W to close) |
| **Annotate** | Highlight / underline / wavy / strikethrough / note / box → written as native PDF comments |
| **Edit** | Shape objects (rectangle / ellipse / line / text / whiteout), **insert images / signatures** (PNG/JPEG, transparency preserved), page management, replace in place, paragraph reflow, document-wide find & replace |
| **Tables** | Edit cell by cell (a longer value grows the row and the borders follow), rows detected by ruling lines, insert / delete a row (content moves, borders redrawn) |
| **Redact** | Removes text (not whiteout). Frame an area, or give it a list of terms for the whole document: **delete and reflow** (following text closes the gap) or **redact in place** (layout unchanged, black box). Zero matches is an error, not a silent no-op |
| **Create** | Blank PDF from scratch (A4/A3/A5/Letter/Legal, landscape, 1–200 pages), opens straight into edit mode |
| **Export** | One click, annotations and edits included, never overwrites; export a page range, split into chunks, merge documents |

---

## Getting started

**Windows installer** — `PDFViewer-1.1.0-pre-electron-setup.exe`. Ships its own
browser engine, so it runs in a real desktop window without Edge. Installs with a
desktop shortcut, can be removed from Apps & Features, and can be set as the
default PDF handler. Choose "install for me only" (`%LOCALAPPDATA%\Programs`,
no UAC prompt) or "for all users".

**Windows portable** — `PDFViewer-1.1.0-pre-win64.zip`. Unzip and run `PDFViewer.exe`.
Don't drag out just the exe; the `_internal` folder next to it is the runtime.

Edit data and export staging live in `%APPDATA%\PDFViewer`; deleting the program
folder leaves them alone. The uninstaller asks before removing them.

No code-signing certificate, so the first launch is blocked by Windows once:
**More info → Run anyway**.

To make it your default PDF app: right-click a PDF → Open with → Choose another app →
pick `PDFViewer.exe`. Double-clicking a PDF then opens that file directly.

**Or run from source** (any platform):

```bash
python server.py --dir "path/to/your/pdfs"
```

The default browser opens `http://127.0.0.1:8000` (the port walks forward if taken).
Nothing to `pip install`.

Useful commands:

```bash
python run_tests.py        # full test suite (17 packages)
python check.py            # front-end static checks
python fontcheck.py x.ttf  # vet a font before swapping it in
pip install pyinstaller && python -m PyInstaller pdfviewer.spec   # build the exe
```

The installer needs Inno Setup (`winget install JRSoftware.InnoSetup`), then:
`ISCC.exe pdfviewer.iss` → `dist\PDFViewer-1.1.0-pre-setup.exe`.

---

## Known limits

| | |
|---|---|
| Scanned documents | Not supported. Redaction can't touch text inside images — that needs OCR |
| Whole-document reflow | Not supported (tables can be edited cell by cell and rows can be deleted; reflowing a whole document needs a real layout engine) |
| Forms / encryption | Not supported (a signature can be pasted as a transparent PNG, but that is a picture, not a verifiable signature field) |
| Insert images | PNG / JPEG only. CMYK JPEG, 16-bit and interlaced PNGs are rejected with a hint to re-save as a plain PNG |
| Edited paragraphs | Glyphs are substituted with a standard font, so they differ subtly from the surrounding text |

---

## Layout

```
server.py      HTTP routes + API + path allowlist
launcher.py    exe entry point (start server, open window)
appenv.py      path rules for source vs. frozen (resources read-only, data in AppData)
pdfedit.py     PDF read/write engine (parse / serialize / append to content stream)
pdfredact.py   redaction engine (content-stream interpreter, whole-glyph removal, leak cleanup)
pdfreflow.py   paragraph reflow (line breaking, page-local shifting, cross-page continuation)
pdfbatch.py    batch: find & replace / delete-or-redact by term / range export / split / merge
pdfannot.py    annotations → native PDF comments
pdffont.py     TrueType subsetting
static/        front end (native ES modules, no framework, no build step)
```

Architecture notes, data model and testing philosophy: **[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)**.

---

## Fonts

`assets/fonts/NotoSansSC-Regular.ttf` (Source Han Sans, OFL, glyf outlines).
A replacement font must be OFL-embeddable, use glyf outlines, and have a regular
weight as its default instance. Override the path with `PDFVIEWER_CJK_FONT`.
`fontcheck.py` tells you more.
