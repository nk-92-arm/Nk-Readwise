# nk-readwise

Tools for getting documents into Readwise Reader in a form that's good to read and highlight.

## pdf-to-reader

Converts a PDF (consulting decks, research reports, papers) into a Reader article:
- highlightable text: headings, paragraphs, bullets and rebuilt tables
- charts and graphs kept as images: full slide images for decks, cropped figures for reports

The skill instructions are in `skills/pdf-to-reader/SKILL.md`. The engine is `skills/pdf-to-reader/scripts/pdf2reader.py`.

### One-time setup on the Mac (private route, images embedded)

```bash
git clone https://github.com/nk-92-arm/nk-readwise ~/Code/nk-readwise
pip3 install -r ~/Code/nk-readwise/skills/pdf-to-reader/scripts/requirements.txt
brew install tesseract        # optional: OCR for scanned PDFs

# Readwise token from https://readwise.io/access_token, stored in Keychain:
security add-generic-password -a "$USER" -s readwise-token -w "<YOUR_TOKEN>"
```

### Use

```bash
python3 ~/Code/nk-readwise/skills/pdf-to-reader/scripts/pdf2reader.py "~/Downloads/report.pdf"
python3 ... report.pdf --dry-run                 # just write report.reader.html to preview
python3 ... deck.pdf --title "..." --author "..." --tags pdf-import,ai --location later
python3 ... deck.pdf --mode slides|document      # override auto-detection
python3 ... deck.pdf --text-only                 # no images
```

A shell alias makes it a one-word command:

```bash
echo 'alias toreader="python3 ~/Code/nk-readwise/skills/pdf-to-reader/scripts/pdf2reader.py"' >> ~/.zshrc
```

### Cloud route (from claude.ai / mobile)

Claude converts the PDF in its workspace, then commits the images to `assets/<doc-id>/` in this public repo. The article goes into Reader via the Readwise connector, with images linked from `raw.githubusercontent.com`.

**Everything under `assets/` and `pdfs/` is public.** Use the Mac route for confidential material.

### Removing a document's public images

```bash
git rm -r assets/<doc-id> && git commit -m "Remove <doc-id>" && git push
```

Reader may keep its own cached copy, and git history still holds the files. For truly sensitive files, don't use the public route.
