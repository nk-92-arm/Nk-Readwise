#!/usr/bin/env python3
"""
pdf2reader — turn a PDF (consulting deck, research report, paper) into clean,
highlightable HTML for Readwise Reader, keeping charts/graphs as embedded images
and rebuilding tables as real HTML tables. Pushes via the Reader API.

Modes
  slides    Each page -> full-page image (keeps every chart and layout) followed by
            the page's text as headings / paragraphs / bullets / tables you can highlight.
  document  Reflowing text with figures cropped and embedded where they appear.
  auto      slides for landscape, low-text-density decks; document otherwise.

Usage
  python3 pdf2reader.py report.pdf                          # convert + push to Inbox
  python3 pdf2reader.py report.pdf --dry-run                # only write report.reader.html
  python3 pdf2reader.py deck.pdf --tags pdf-import,ai --title "..." --author "..."

Token lookup: --token, $READWISE_TOKEN, macOS Keychain item "readwise-token",
~/.config/pdf2reader/token     (get one at https://readwise.io/access_token)

Dependencies:  pip3 install pymupdf pymupdf4llm markdown
Optional:      brew install tesseract   (OCR for scanned PDFs)
"""
import argparse, base64, contextlib, hashlib, html, io, json, os, re, subprocess, sys
import urllib.request, urllib.error
from collections import Counter

try:
    import pymupdf as fitz
    import pymupdf4llm
    import markdown as mdlib
except ImportError as e:
    sys.exit(f"Missing dependency ({e.name}). Install with:  pip3 install pymupdf pymupdf4llm markdown")

API = "https://readwise.io/api/v3/save/"
MAX_HTML_BYTES = 9_000_000


# ---------------------------------------------------------------- helpers
def esc(s):
    return html.escape(s, quote=True)

@contextlib.contextmanager
def quiet():
    """pymupdf4llm prints parser chatter to stdout; keep our output clean."""
    with open(os.devnull, "w") as dn, contextlib.redirect_stdout(dn):
        yield

class ImageSink:
    """Embeds images as data URIs (private, default) or writes them into a folder
    that is published at a public base URL (e.g. a GitHub repo) and links them."""
    def __init__(self, assets_dir=None, base_url=None, doc_id=""):
        self.dir = os.path.join(assets_dir, doc_id) if assets_dir else None
        self.base = base_url.rstrip("/") + "/" + doc_id if base_url else None
        if self.dir:
            os.makedirs(self.dir, exist_ok=True)
    def put(self, jpg_bytes, name):
        if not self.dir:
            return "data:image/jpeg;base64," + base64.b64encode(jpg_bytes).decode()
        with open(os.path.join(self.dir, name), "wb") as f:
            f.write(jpg_bytes)
        return f"{self.base}/{name}"

def render_page(page, max_width, quality, sink):
    zoom = min(3.0, max_width / page.rect.width)
    pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
    return sink.put(pix.tobytes("jpg", jpg_quality=quality), f"p{page.number + 1:03d}.jpg")

def norm_line(s):
    s = re.sub(r"[*_#|`>]", "", s)
    s = re.sub(r"\d+", "", s)
    return re.sub(r"\s+", " ", s).strip().lower()

def running_lines(chunks):
    """Lines that repeat on many pages (running headers/footers, confidentiality notes)."""
    if len(chunks) < 4:
        return set()
    c = Counter()
    for ch in chunks:
        c.update({norm_line(l) for l in ch["text"].splitlines() if 0 < len(norm_line(l)) < 90})
    pages = len(chunks)
    return {l for l, n in c.items()
            if n >= max(3, 0.4 * pages) or (len(l) < 40 and n >= max(3, 0.25 * pages))}

def clean_md(text, drop):
    text = re.sub(r"<!-- Start of picture text -->.*?<!-- End of picture text -->", "", text, flags=re.S)
    text = re.sub(r"<!--.*?-->", "", text, flags=re.S)
    text = text.replace("­", "")
    text = re.sub(r"[-]", "", text)                 # private-use glyphs
    out = []
    for l in text.splitlines():
        n = norm_line(l)
        if n and n in drop:
            continue
        if re.fullmatch(r"\s*\**\s*\d{1,3}\s*\**\s*", l):        # bare page numbers
            continue
        if re.fullmatch(r"\s*#+\s*\**\s*\**\s*", l):            # empty headings
            continue
        out.append(l.rstrip())
    text = "\n".join(out)
    text = re.sub(r"\*\*\s*\*\*", "", text)                      # empty bold
    text = re.sub(r"\*\*\s+([,.;:])", r"**\1", text)             # "**bold** ," -> "**bold**,"
    # column-merge artefacts inside bold runs: "How to get thereAI adoption" -> "there AI adoption"
    text = re.sub(r"\*\*(.+?)\*\*", lambda m: "**" + re.sub(r"(?<=[a-z]{3})(?=[A-Z])", " ", m.group(1)) + "**", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()

def drop_chart_tables(h):
    """Remove 'tables' that are really chart labels scraped off a graph (the slide image
    already shows them). Real tables - even numeric ones - are kept."""
    def judge(m):
        raw = re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", m.group(0), re.S)
        stacked = sum(len(re.findall(r"\d+\s*%", c)) >= 2 for c in raw)       # "3% 21%" in one cell
        cells = [re.sub(r"<[^>]+>", " ", c).strip() for c in raw]
        cells = [c for c in cells if c]
        if not cells:
            return ""
        numeric = sum(bool(re.fullmatch(r"[\d\s%<>+\-.,–x×]+", c)) for c in cells) / len(cells)
        return "" if stacked >= 2 or numeric > 0.75 else m.group(0)
    return re.sub(r"<table>.*?</table>", judge, h, flags=re.S)

def md_to_html(text):
    h = mdlib.markdown(text, extensions=["tables", "sane_lists"])
    return re.sub(r"(<t[dh][^>]*>)(.*?)(</t[dh]>)", lambda m: m.group(1) + m.group(2).replace("<br>", " ") + m.group(3), h, flags=re.S)

def demote_headings(h, top=2):
    """Inside a page section, shift headings so the section title stays the top level."""
    def rep(m):
        lvl = min(6, int(m.group(2)) + top)
        return f"<{m.group(1)}h{lvl}"
    return re.sub(r"<(/?)h([1-6])", rep, h)

def page_title(page):
    """Slide title = the largest *wordy* text near the top of the page (by font size).
    Big KPI numbers ("2-3x", "~20-50%") or step labels ("1 2 3") are skipped."""
    spans = []
    for b in page.get_text("dict")["blocks"]:
        for l in b.get("lines", []):
            for sp in l["spans"]:
                t = sp["text"].strip()
                if t and sp["bbox"][1] < page.rect.height * 0.6:
                    spans.append((round(sp["size"], 1), sp["bbox"][1], sp["bbox"][0], t))
    if not spans:
        return None
    body = Counter(x[0] for x in spans).most_common(1)[0][0]
    for size in sorted({x[0] for x in spans}, reverse=True):
        if size < 14 and size < body * 1.25:
            return None
        group = [x for x in spans if abs(x[0] - size) <= 0.6]
        if min(x[1] for x in group) > page.rect.height * 0.35:     # must start in the top third
            continue
        group = sorted(group, key=lambda x: (round(x[1] / 4), x[2]))
        t = re.sub(r"\s+", " ", " ".join(x[3] for x in group)).strip()
        t = re.sub(r"(\w)- (\w)", r"\1-\2", t)
        if len(re.findall(r"[A-Za-zÄÖÜäöüß]{3,}", t)) >= 3 and len(t) < 220:
            return t
    return None

def strip_title_from_md(md, title):
    """Remove the line(s) of md that repeat the title (they may carry extra merged text)."""
    key = norm_line(title)[:40]
    lines = md.splitlines()
    for idx, l in enumerate(lines):
        n = norm_line(l)
        if key and n.startswith(key):
            rest = n[len(norm_line(title)):].strip()
            if len(rest) < 3:
                del lines[idx]
            break
    return "\n".join(lines)

def merge_continuation_rows(md):
    """Tables from slides often split one wrapped cell into several rows.
    Merge a row into the previous one when most of its cells continue lowercase text."""
    out, block = [], []
    def flush():
        if not block:
            return
        rows = [[c.strip() for c in l.strip()[1:-1].split("|")] for l in block]
        if len(rows) < 3:
            out.extend(block); block.clear(); return
        head, sep, body = rows[0], rows[1], rows[2:]
        merged = []
        for r in body:
            cells = [re.sub(r"[*_<>br/]+", "", c).strip() for c in r]
            filled = [c for c in cells if c]
            lower = sum(c[:1].islower() for c in filled)
            if merged and filled and lower >= max(1, 0.6 * len(filled)) and len(r) == len(merged[-1]):
                merged[-1] = [(a + " " + b).strip() for a, b in zip(merged[-1], r)]
            else:
                merged.append(r)
        for r in [head, sep] + merged:
            out.append("|" + "|".join(r) + "|")
        block.clear()
    for l in md.splitlines():
        if l.strip().startswith("|"):
            block.append(l)
        else:
            flush(); out.append(l)
    flush()
    return "\n".join(out)

def first_heading(md):
    for l in md.splitlines():
        s = l.strip()
        if s.startswith("#"):
            t = re.sub(r"[#*_]", "", s).strip()
            if t:
                return t, l
    for l in md.splitlines():
        m = re.fullmatch(r"\s*\*\*(.+?)\*\*\s*", l)
        if m and len(m.group(1)) < 160:
            return m.group(1).strip(), l
    return None, None


# ---------------------------------------------------------------- OCR for image-only slides
def has_tesseract():
    try:
        return subprocess.run(["tesseract", "--version"], capture_output=True).returncode == 0
    except FileNotFoundError:
        return False

def ocr_page(page, lang="eng"):
    """OCR an image-only page. Dark backgrounds are inverted first (much better accuracy
    on light-on-dark slides). Returns (title, [paragraph dicts]) using Tesseract layout data."""
    import csv
    from PIL import Image, ImageOps, ImageStat
    zoom = max(1.0, 2600 / page.rect.width)
    pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
    im = Image.open(io.BytesIO(pix.tobytes("png"))).convert("L")
    if ImageStat.Stat(im).mean[0] < 110:
        im = ImageOps.invert(im)
    im = ImageOps.autocontrast(im)
    buf = io.BytesIO(); im.save(buf, "PNG")
    r = subprocess.run(["tesseract", "stdin", "stdout", "--psm", "3", "-l", lang, "tsv"],
                       input=buf.getvalue(), capture_output=True)
    rows = list(csv.DictReader(io.StringIO(r.stdout.decode(errors="replace")), delimiter="\t"))
    paras, order = {}, []
    for w in rows:
        if w.get("level") != "5" or not (w.get("text") or "").strip():
            continue
        if float(w.get("conf") or -1) < 55:
            continue
        k = (int(w["block_num"]), int(w["par_num"]))
        if k not in paras:
            paras[k] = []; order.append(k)
        paras[k].append(w)
    H = im.height
    out = []
    for k in order:
        ws = paras[k]
        text = " ".join(w["text"] for w in ws)
        text = re.sub(r"\s*[|=]+\s*(?=\S)", " · ", text).strip(" ·|=-_~—")
        text = re.sub(r"\s+[^\w\s(\"'“‘%$€]{1,2}(?=\s|$)", "", text).strip()
        text = re.sub(r"^[^\w(\"'“‘$€]+\s*", "", text)                 # leading "© ", "[ ", "\ "
        words = re.findall(r"[A-Za-zÄÖÜäöüß]{2,}", text)
        if not re.search(r"[A-Za-zÄÖÜäöüß]{3,}|\d", text) or len("".join(words)) < 0.5 * len(re.sub(r"\W", "", text)):
            continue                                              # mostly symbols / OCR noise
        top = min(int(w["top"]) for w in ws)
        if top > H * 0.93 and len(text) < 40:
            continue                                              # footer / watermark
        hs = sorted(int(w["height"]) for w in ws)
        out.append(dict(text=text, h=hs[len(hs) // 2], top=top, words=len(words)))
    if not out:
        return None, []
    body_h = sorted(p["h"] for p in out)[len(out) // 2]
    cands = [p for p in out if p["top"] < H * 0.45 and p["words"] >= 3 and p["h"] >= body_h * 1.25]
    title = max(cands, key=lambda p: p["h"]) if cands else None
    if not title:   # fallback: first prominent multi-word line near the top
        title = next((p for p in sorted(out, key=lambda p: p["top"])
                      if p["top"] < H * 0.3 and p["words"] >= 4 and p["h"] >= body_h), None)
    if title:
        out.remove(title)
    for p in out:
        p["kind"] = "kpi" if p["h"] >= body_h * 2 and p["words"] <= 3 else \
                    "h" if p["h"] >= body_h * 1.25 and len(p["text"]) < 90 else "p"
    return (title["text"] if title else None), out

def ocr_html(paras):
    h = []
    for p in paras:
        t = esc(p["text"])
        h.append(f"<p><strong>{t}</strong></p>" if p["kind"] == "kpi" else
                 f"<h4>{t}</h4>" if p["kind"] == "h" else f"<p>{t}</p>")
    return "\n".join(h)


# ---------------------------------------------------------------- mode detection
def detect_mode(doc):
    n = min(doc.page_count, 8)
    landscape = sum(doc[i].rect.width > doc[i].rect.height for i in range(n)) / max(n, 1)
    chars = sum(len(doc[i].get_text()) for i in range(n)) / max(n, 1)
    return "slides" if landscape > 0.6 and chars < 3000 else "document"


# ---------------------------------------------------------------- builders
def build_slides(doc, path, args):
    with quiet(), contextlib.redirect_stderr(open(os.devnull, "w")):
        chunks = pymupdf4llm.to_markdown(path, page_chunks=True, ignore_images=True,
                                         table_strategy=None if args.no_tables else "lines_strict")
    drop = running_lines(chunks)
    parts = []
    for i, ch in enumerate(chunks):
        page = doc[i]
        md = clean_md(ch["text"], drop)
        no_text = len(re.sub(r"\W", "", md)) < 3
        if no_text and (i == len(chunks) - 1 or (not page.get_images() and not page.get_drawings())):
            continue   # blank page or logo-only closing slide
        image_only = len(page.get_text().strip()) < 20
        if image_only and args.ocr:
            title, ocr_paras = ocr_page(page, args.ocr_lang)
            if not title and i == 0:          # stylised cover text rarely OCRs well
                title, ocr_paras = args.doc_title, []
            heading = title or f"Page {i + 1}"
            parts.append(f"<section>\n<h2>{i + 1}. {esc(heading)}</h2>")
            if not args.text_only:
                parts.append(f'<figure><img src="{render_page(page, args.max_width, args.quality, args.sink)}" '
                             f'alt="Slide {i + 1}: {esc(heading)}"></figure>')
            parts.append(ocr_html(ocr_paras))
            parts.append("</section>\n<hr>")
            continue
        title = page_title(page)
        if title:
            md = strip_title_from_md(md, title)
        else:
            title, title_line = first_heading(md)
            if title_line:
                md = md.replace(title_line, "", 1)
        md = merge_continuation_rows(md)
        heading = title or f"Page {i + 1}"
        parts.append(f"<section>\n<h2>{i + 1}. {esc(heading)}</h2>")
        if not args.text_only:
            parts.append(f'<figure><img src="{render_page(page, args.max_width, args.quality, args.sink)}" '
                         f'alt="Slide {i + 1}: {esc(heading)}"></figure>')
        if md.strip():
            parts.append(demote_headings(drop_chart_tables(md_to_html(md)), top=2))
        parts.append("</section>\n<hr>")
    return "\n".join(parts)

def build_document(doc, path, args):
    with quiet(), contextlib.redirect_stderr(open(os.devnull, "w")):
        kw = dict(page_chunks=True, table_strategy=None if args.no_tables else "lines_strict")
        if args.text_only:
            kw["ignore_images"] = True
        elif args.sink.dir:
            kw.update(write_images=True, image_path=args.sink.dir, image_format="jpg", dpi=args.dpi)
        else:
            kw.update(embed_images=True, image_format="jpg", dpi=args.dpi)
        chunks = pymupdf4llm.to_markdown(path, **kw)
    if args.sink.dir:   # rewrite local image paths to public URLs
        for ch in chunks:
            ch["text"] = re.sub(r"\]\(([^)]+\.jpg)\)",
                                lambda m: "](" + args.sink.base + "/" + os.path.basename(m.group(1)) + ")", ch["text"])
    drop = running_lines(chunks)
    md = "\n\n".join(clean_md(ch["text"], drop) for ch in chunks)
    h = md_to_html(md)
    # mark images as figures
    h = re.sub(r"<p>\s*(<img [^>]+>)\s*</p>", r"<figure>\1</figure>", h)
    return demote_headings(h, top=1)

def wrap(title, author, body_html, source_name):
    # Reader shows the document title itself; no <title>/<h1> here, or it appears twice.
    by = f" · {esc(author)}" if author else ""
    return (f"<html><head><meta charset='utf-8'></head><body>"
            f"<p><em>Converted from {esc(source_name)}{by}</em></p>\n{body_html}</body></html>")


# ---------------------------------------------------------------- token + push
def get_token(cli):
    if cli:
        return cli
    if os.environ.get("READWISE_TOKEN"):
        return os.environ["READWISE_TOKEN"].strip()
    if sys.platform == "darwin":
        try:
            r = subprocess.run(["security", "find-generic-password", "-s", "readwise-token", "-w"],
                               capture_output=True, text=True, timeout=10)
            if r.returncode == 0 and r.stdout.strip():
                return r.stdout.strip()
        except Exception:
            pass
    p = os.path.expanduser("~/.config/pdf2reader/token")
    if os.path.exists(p):
        return open(p).read().strip()
    sys.exit("No Readwise token found. Get one at https://readwise.io/access_token, then:\n"
             "  security add-generic-password -a \"$USER\" -s readwise-token -w <TOKEN>")

def push(token, payload):
    req = urllib.request.Request(API, data=json.dumps(payload).encode(), method="POST",
                                 headers={"Authorization": f"Token {token}",
                                          "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, {"error": e.read().decode(errors="replace")[:500]}


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("pdf")
    ap.add_argument("--mode", choices=["auto", "slides", "document"], default="auto")
    ap.add_argument("--title"); ap.add_argument("--author")
    ap.add_argument("--tags", default="pdf-import", help="comma separated")
    ap.add_argument("--location", choices=["new", "later", "shortlist", "archive"], default="new")
    ap.add_argument("--max-width", type=int, default=1400, help="slide image width (px)")
    ap.add_argument("--dpi", type=int, default=150, help="figure resolution in document mode")
    ap.add_argument("--quality", type=int, default=72, help="JPEG quality")
    ap.add_argument("--text-only", action="store_true", help="skip images")
    ap.add_argument("--no-tables", action="store_true", help="do not rebuild tables as HTML")
    ap.add_argument("--dry-run", action="store_true", help="write HTML only, do not push")
    ap.add_argument("--out", help="HTML output path (default: next to the PDF)")
    ap.add_argument("--assets-dir", help="write images here instead of embedding (e.g. <repo>/assets)")
    ap.add_argument("--assets-base-url", help="public URL that --assets-dir is served at")
    ap.add_argument("--doc-id", help="subfolder name for this document's images (default: file hash)")
    ap.add_argument("--no-ocr", action="store_true", help="skip OCR for image-only pages")
    ap.add_argument("--ocr-lang", default="eng", help="Tesseract language(s), e.g. eng+deu")
    ap.add_argument("--token")
    args = ap.parse_args()
    args.ocr = not args.no_ocr and has_tesseract()
    if bool(args.assets_dir) != bool(args.assets_base_url):
        sys.exit("--assets-dir and --assets-base-url go together")

    path = os.path.abspath(args.pdf)
    doc = fitz.open(path)
    name = os.path.basename(path)
    mode = detect_mode(doc) if args.mode == "auto" else args.mode
    meta = doc.metadata or {}
    title = args.title or meta.get("title") or os.path.splitext(name)[0]
    author = args.author or meta.get("author") or None
    digest = hashlib.sha1(open(path, "rb").read()).hexdigest()[:16]
    args.doc_title = title
    args.sink = ImageSink(args.assets_dir, args.assets_base_url, args.doc_id or digest)

    def build():
        body = build_slides(doc, path, args) if mode == "slides" else build_document(doc, path, args)
        return wrap(title, author, body, name)

    out_html = build()
    while not args.sink.dir and len(out_html.encode()) > MAX_HTML_BYTES and args.max_width > 700:   # shrink until it fits
        args.max_width = int(args.max_width * 0.8)
        args.dpi = int(args.dpi * 0.8)
        args.quality = max(50, args.quality - 8)
        out_html = build()

    out = args.out or os.path.splitext(path)[0] + ".reader.html"
    with open(out, "w", encoding="utf-8") as f:
        f.write(out_html)
    print(f"mode={mode} pages={doc.page_count} html={len(out_html.encode())/1e6:.2f}MB "
          f"images={'linked from ' + args.sink.base if args.sink.dir else 'embedded'} -> {out}")

    if args.dry_run:
        return
    payload = dict(url=f"https://pdf2reader.local/{digest}", html=out_html, title=title,
                   tags=[t.strip() for t in args.tags.split(",") if t.strip()],
                   location=args.location, category="article", should_clean_html=False)
    if author:
        payload["author"] = author
    status, resp = push(get_token(args.token), payload)
    if status in (200, 201):
        print(("Already in Reader (same file): " if status == 200 else "Saved to Reader: ") + resp.get("url", ""))
    else:
        sys.exit(f"Reader API error {status}: {resp}")

if __name__ == "__main__":
    main()
