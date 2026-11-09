# PDF Viewer

English | [简体中文](README.md)

Fully local, offline, **zero third-party dependencies** PDF tool

---

## Why It's Worth a Look

1. **Real redaction** — it deletes text from the content stream, not just covers it with a black box. Even `strings` won't find it.
2. **Write Chinese** — a self-written TTF subsetting engine embeds only the glyphs you use. Selectable and copyable, but not editable (the glyphs are welded in).
3. **Edit the original text and reflow it** — select a paragraph, rewrite it, re-wrap the lines, and overflow continues onto the next page.
4. **Annotations are native PDF comments** — after export you can edit and delete them in Firefox and Acrobat.

---

## What It Does

| | |
|---|---|
<<<<<<< HEAD
| **Read** | Zoom, paging, single / two-page view, thumbnails, outline, search, bookmarks, invert colors |
| **Tabs** | Several documents in one window, each keeping its own scroll position, zoom, edit mode and undo stack (Ctrl+Tab to cycle, Ctrl+W to close) |
| **Annotate** | Highlight / underline / wavy / strikethrough / note / box → written as native PDF comments |
| **Edit** | Shape objects (rectangle / ellipse / line / text / whiteout), **insert images / signatures** (PNG/JPEG, transparency preserved), page management, replace in place, paragraph reflow, document-wide find & replace |
| **Tables** | Edit cell by cell (a longer value grows the row and the borders follow), rows detected by ruling lines, insert / delete a row (content moves, borders redrawn) |
| **Redact** | Removes text (not whiteout). Frame an area, or give it a list of terms for the whole document: **delete and reflow** (following text closes the gap) or **redact in place** (layout unchanged, black box). Zero matches is an error, not a silent no-op |
| **Create** | Blank PDF from scratch (A4/A3/A5/Letter/Legal, landscape, 1–200 pages), opens straight into edit mode |
| **Export** | One click, annotations and edits included, never overwrites; export a page range, split into chunks, merge documents |
=======
| **Reading** | Zoom, paging, single-page / two-column, thumbnails, outline, search, reading bookmarks, invert colors |
| **Multiple tabs** | Several documents in one window, each remembering its own scroll position, zoom, edit state, and undo stack (Ctrl+Tab to cycle, Ctrl+W to close) |
| **Markup** | Highlight / underline / squiggly / strikeout / sticky note / box → written as native PDF comments (or maybe you just want to select text with the mouse?) |
| **Editing** | Graphic objects (rectangle/ellipse/line/text/whiteout), page management, in-place replacement, paragraph reflow, whole-document find & replace |
| **Tables** | Edit cell by cell (longer text automatically grows the row, borders follow), detect rows from table rules, insert/delete a row (content below shifts, borders filled in automatically) |
| **Redaction** | Delete text (not white it out). Works by box selection, or run a list of keywords across the whole document: **delete and reflow** (following text moves up) or **black out with traces** (layout unchanged, black boxes drawn). Zero hits is an error |
| **Creating** | Generate a blank PDF from scratch (A4/A3/A5/Letter/Legal, landscape optional, 1–200 pages) and go straight into edit mode |
| **Exporting** | One-click export with annotations and edits written in; never overwrites on name collision; export by page range, split by copy, merge multiple files |
>>>>>>> f5e76158ec59c2cd8b55e0ae9ba83a3fb1ee00a8

---

## Quick Start

**Windows installer**: `PDFViewer-1.1.0-pre-electron-setup.exe` ships its own browser engine in a standalone window (no Edge dependency). Installs a desktop icon, uninstalls via Apps & features, and can be set as the default PDF handler.
During install you can pick "Install for me only" (`%LOCALAPPDATA%\Programs`, no UAC prompt) or "Install for all users".

**Windows portable**: `PDFViewer-1.1.0-pre-win64.zip`, unzip and double-click `PDFViewer.exe`. Don't just drag the exe out on its own — the `_internal` folder next to it holds the runtime.

Edit data and export scratch files live in `%APPDATA%\PDFViewer`; deleting the program folder doesn't touch them.
The uninstaller asks whether to delete them too — unless you click Yes, they stay.
No code-signing certificate, so Windows will block it the first time: **More info → Run anyway**.

**Or run from source** (any platform):

```bash
python server.py --dir "your PDF directory"
```

Or just double-click `server.py`.
It opens `http://127.0.0.1:8000` in your default browser (if the port is taken it moves to the next one).
No `pip install` of anything required.

Common commands:

```bash
python run_tests.py        # all tests (17 suites)
python check.py            # frontend static self-check
python fontcheck.py x.ttf  # health-check a font before swapping
pip install pyinstaller && python -m PyInstaller pdfviewer.spec   # build the exe yourself
```

Building the installer needs Inno Setup separately (`winget install JRSoftware.InnoSetup`), then:
`ISCC.exe pdfviewer.iss` → `dist\PDFViewer-1.1.0-pre-setup.exe`.

---

## Known Limitations

| | |
|---|---|
<<<<<<< HEAD
| Scanned documents | Not supported. Redaction can't touch text inside images — that needs OCR |
| Whole-document reflow | Not supported (tables can be edited cell by cell and rows can be deleted; reflowing a whole document needs a real layout engine) |
| Forms / encryption | Not supported (a signature can be pasted as a transparent PNG, but that is a picture, not a verifiable signature field) |
| Insert images | PNG / JPEG only. CMYK JPEG, 16-bit and interlaced PNGs are rejected with a hint to re-save as a plain PNG |
| Edited paragraphs | Glyphs are substituted with a standard font, so they differ subtly from the surrounding text |
=======
| Scanned documents | Not supported. Redaction does nothing to text inside images; OCR required |
| Whole-document reflow | Not supported (you can edit table cells one by one and delete rows; full reflow needs a real layout engine) |
| Forms / encryption / signatures | Not supported |
| Edited paragraphs | The font is replaced with a standard font, so glyphs differ subtly from the surrounding text (or maybe delete the whole paragraph and reflow it?) |
>>>>>>> f5e76158ec59c2cd8b55e0ae9ba83a3fb1ee00a8

---

## Project Layout

```
server.py      HTTP routing + API + path allowlist
launcher.py    exe entry point (start server + open browser)
appenv.py      path rules for source vs. exe runs (resources read-only, data in AppData)
pdfedit.py     PDF read/write engine (parsing / serialization / content-stream appending)
pdfredact.py   redaction engine (content-stream interpreter + whole-glyph deletion + leak-surface scrubbing)
pdfreflow.py   paragraph reflow (line wrapping + in-page shifting + cross-page continuation)
pdfbatch.py    batch: find & replace / delete-or-redact by keyword / range export / split / merge
pdfannot.py    annotations → native PDF comments
pdffont.py     TrueType subsetting
static/        frontend (native ES modules, no framework, no build step)
```

For architecture details, core models, and testing philosophy, see **[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)**.

---

## Fonts

`assets/fonts/NotoSansSC-Regular.ttf` (Source Han Sans, OFL licensed, glyf outlines).
A replacement font must be OFL-embeddable, use glyf outlines, and have regular weight as its default instance.
Override the path with the `PDFVIEWER_CJK_FONT` environment variable. `fontcheck.py` can tell you more.
