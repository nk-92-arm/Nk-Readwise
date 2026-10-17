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
    return {l for l, n in c.items() if n >= max(3, 0.4 * len(chunks))}

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
        title, title_line = first_heading(md)
        if title_line:
            md = md.replace(title_line, "", 1)
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
    ap.add_argument("--token")
    args = ap.parse_args()
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
