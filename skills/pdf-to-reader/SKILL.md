---
name: pdf-to-reader
description: Push a PDF (consulting deck, research report, paper) into Readwise Reader as clean highlightable text with charts kept as images and tables rebuilt. Triggers on "push to Readwise", "send to Reader", "PDF to Readwise", "in Readwise lesen", "ab in Reader", or a PDF attached with Readwise/Reader mentioned.
---

# PDF → Readwise Reader

Turns a PDF into an HTML article that Reader renders well:
- **Text** becomes real headings, paragraphs, bullets and tables, so every word is highlightable.
- **Charts and graphs** stay as images. For slide decks this is the full slide image above its text; for reports, figures are cropped inline.
- **Tables** are rebuilt as HTML tables. Chart labels that only look like tables are dropped, because the image already shows them.

The engine is `scripts/pdf2reader.py` (PyMuPDF + pymupdf4llm). Never hand-write the HTML; always run the script.

Defaults: location **Inbox** (`new`), tag **`pdf-import`**, mode **auto** (decks → `slides`, reports → `document`).

## 1. Get the PDF and metadata

- Use the attached file, a file from the user's connected folders, or a path they name.
- **Title:** the deck's cover title, or the report title, rather than the file name. Add the source and date if they are visible, e.g. "AI in Private Capital – Fund-level transformation (McKinsey, Oct 2026)".
- **Author:** the publisher, e.g. "QuantumBlack, AI by McKinsey", "Goldman Sachs Research".
- **Tags:** add 1–3 topic tags beyond `pdf-import` only if the user asks or uses tags in Reader. Check with `reader_list_tags` if unsure.

## 2. Pick the route

| Route | When | Images | Who pushes |
|---|---|---|---|
| **A · Local** | A shell on Niklas's Mac is available (Claude Code, or a working `device_bash` tool) | Embedded in the HTML, private | The script, via Reader API |
| **B · Repo** | Only the cloud workspace is available (claude.ai chat or mobile) | Public, in `nk-92-arm/nk-readwise/assets/` | Claude, via the Readwise connector |

**Confidentiality gate (Route B only).** Scan the first pages for "confidential", "proprietary", "strictly prohibited", "internal", "NDA", "draft". If any appear, stop. Tell Niklas that Route B publishes the slide images in a public repo, and ask whether to:
- use Route A from Claude Code on the Mac,
- use `--text-only` (Route B without images), or
- publish anyway.

Never publish a flagged document without an explicit yes.

### Route A · Local (Mac)

```bash
cd <repo>/skills/pdf-to-reader/scripts
pip3 install -r requirements.txt          # first time only
python3 pdf2reader.py "<file.pdf>" --title "<title>" --author "<author>"
```

The script reads the token from Keychain item `readwise-token` (setup in the README). It prints the Reader URL. If it exits with "No Readwise token found", show Niklas the one-line setup command from the README.

### Route B · Repo (cloud)

1. Attach and clone `nk-92-arm/nk-readwise` with push access, if it is not already in the session.
2. Install the dependencies: `pip install --break-system-packages -r skills/pdf-to-reader/scripts/requirements.txt`.
3. Pick a short, readable `<doc-id>` slug, e.g. `2026-10-mck-ai-private-capital`.
4. Convert:
   ```bash
   python3 skills/pdf-to-reader/scripts/pdf2reader.py "<file.pdf>" --dry-run \
     --title "<title>" --author "<author>" \
     --assets-dir assets --doc-id <doc-id> \
     --assets-base-url https://raw.githubusercontent.com/nk-92-arm/nk-readwise/main/assets \
     --out /tmp/<doc-id>.html
   ```
5. Commit `assets/<doc-id>/` with the message "Add images: <title>" and push to `main`.
6. Check that one image URL returns 200 (`curl -sI`) before continuing.
7. Read `/tmp/<doc-id>.html` and pass its full content to `reader_create_document`, with:
   - `html`: the file content
   - `title`, `author`
   - `url`: `https://nk-readwise.local/<doc-id>`
   - `category`: `article`
   - `tags`: `["pdf-import"]`

   Linked images keep this HTML small, typically 30–150 KB.

## 3. Original PDF too (optional, on by default)

Niklas also wants the original, native PDF in Reader for page-faithful viewing.
- **Route B, not confidential:** copy the PDF to `pdfs/<doc-id>.pdf`, then commit and push. Call `reader_create_document` with:
  - `url`: `https://raw.githubusercontent.com/nk-92-arm/nk-readwise/main/pdfs/<doc-id>.pdf`
  - `category`: `pdf`
  - the same title with " (PDF)" appended
  - the same tags
- **Confidential, or Route A:** there is no private upload API for PDFs. Tell Niklas to drag the file into Reader (read.readwise.io → Upload) or forward it to his Reader email address.

## 4. Verify and report

- Call `reader_get_document_details` on the new id.
- Confirm the content has the slide/section headings and `![` image references, and that the text reads in order.
- Reply in two or three lines: the Reader link, the mode used, the page count, and anything skipped (e.g. "text-only, images withheld").

## Known limits

- **Scanned PDFs:** OCR runs automatically if Tesseract is installed (`brew install tesseract`). Without it, scanned pages come through as images only.
- **Very dense multi-column slides:** text can occasionally merge across columns. The slide image above the text is the reference.
- **Re-pushing the same file:** Route A uses the file hash as the URL, so Reader keeps the existing doc (the script reports "Already in Reader"). To force a new copy, change `--doc-id` (Route B) or edit the file.
