# redact-pdf

![Python](https://img.shields.io/badge/python-3.11%2B-blue)
![License](https://img.shields.io/badge/license-AGPL--3.0-green)

**Permanent PDF redaction, with a practical workflow for manual review.**

Remove names, email addresses and other sensitive text while keeping the rest of your PDF readable and searchable. Processing stays on your computer.

```bash
redact-pdf redact contract.pdf -t "John Smith" -t "ACME"
# Writes contract_REDACTED.pdf
```

Matched text is deleted from the file and replaced with labelled black boxes. The tool also cleans metadata and hidden content, then checks the saved output for remaining matches. Text inside images needs a manual review.

## What it does, and why it matters

| It does | Why it matters |
| --- | --- |
| **Deletes matched text** from the file and puts a labelled box in its place | A black rectangle drawn in a PDF editor only covers the text. Select, copy and paste still reveal it. |
| **Matches whole words** across line breaks, end-of-line hyphens and accented spellings | A name split over two lines is otherwise missed, and a short term like "ETH" would otherwise damage "methodology". |
| **Removes email addresses by default**, with the person and organisation names found in them | `lukas.koernich@acme.ch` gives away a name and an employer that also appear elsewhere, without the address. |
| **Labels each box** `EMAIL`, `NAME`, `ORG` or `REDACTED` | Readers can still follow the document and see what kind of information was removed. |
| **Strips metadata everywhere**: document info, XMP, EXIF in photos, application data, the document ID | Author, company, edit history and photo locations are stored outside the page text, mostly where common metadata tools don't look. |
| **Redacts text outside the pages**: bookmarks, layer names, alternate text | These repeat what the pages say and survive a redaction that only looks at pages. |
| **Removes text nobody sees**: invisible OCR layers and text outside the visible page | Cropping a page hides content without deleting it, and any text extraction tool prints it. |
| **Blanks hidden image areas**: cropped images and screenshots split across pages | The whole picture stays in the file, including the part under a box drawn on the other page. |
| **Removes links, attachments and scripts**, and rewrites the whole file | Link targets expose internal addresses, and earlier versions of a page can otherwise be recovered. |
| **Turns Firefox highlights and pen blots into real redactions** | Text inside images needs a human eye. Pages stay text instead of becoming pictures, so the file stays small and searchable. |
| **Removes whole pages** with everything only they use | A page taken out of the page list can stay in the file through a bookmark or jump target. |
| **Checks its own output** and exits non-zero if anything remains | You learn about a leftover before the recipient does, and scripts can rely on the exit code. |
| **Runs locally**, with one dependency, and never overwrites the input | Sensitive documents don't leave your computer, and the original is always still there. |

## Install

Install [uv](https://docs.astral.sh/uv/getting-started/installation/), then:

```bash
uv tool install git+https://github.com/marcobertenghi/redact-pdf
redact-pdf --help
```

Requires Python 3.11+. uv can download a suitable Python automatically. The sole runtime dependency is [PyMuPDF Lite](https://pypi.org/project/pymupdf-lite/); no separate PDF tools are needed.

Update with `uv tool upgrade redact-pdf`. Uninstall with `uv tool uninstall redact-pdf`.

## Recommended workflow

### 1. Redact known text

```bash
redact-pdf redact in.pdf -t "John Smith" -t "ACME"
# Or keep your terms in a file, one per line:
redact-pdf redact in.pdf -f terms.txt
```

Email addresses are included automatically. Check the report for names inferred from emails and pages containing images.

### 2. Review and mark what remains

Open `in_REDACTED.pdf` in Firefox and review the document, paying particular attention to images, signatures, logos and spelling variants.

- **Highlight** text you want removed.
- **Draw** a short blot over an image or other content. Set the pen thickness to the maximum and use one blot per item. Each stroke removes its bounding rectangle, including the pen width; separate blots leave the content between them intact.

Save as `reviewed.pdf` using **Save** (Ctrl+S), not **Print → Save to PDF**. The marks are still overlays at this point; the next step makes them permanent.

### 3. Apply the manual marks

```bash
redact-pdf apply-boxes reviewed.pdf --annotations-only
# Writes reviewed_FINAL.pdf
```

`--annotations-only` processes Firefox's saved highlights and pen strokes while skipping the slower search through existing page drawings. Metadata cleanup, hidden-image cleanup and verification still run.

For boxes exported into page content by tools such as Xournal++, omit `--annotations-only`. See [manual box handling](docs/reference.md#apply-boxes-make-manual-boxes-real) for supported marks and mixed files. If the command reports no marks or a warning, resolve it before sharing.

### 4. Check the final PDF

```bash
redact-pdf verify reviewed_FINAL.pdf -f terms.txt
```

Use the same terms or regex patterns as in step 1. If you used `-t` options, pass those to `verify` instead of `-f`. Open the final PDF and review it visually too: verification checks stored text and hidden image areas, not words visible inside images.

## More ways to use it

```bash
# Write to a specific file
redact-pdf redact in.pdf shared/contract.pdf -f terms.txt

# Process a folder into another folder
redact-pdf redact ./documents ./redacted -f terms.txt

# Add a case-insensitive regex
redact-pdf redact in.pdf -r "(?i)project[- ]alpha"

# Use one label in every box instead of EMAIL, NAME and ORG
redact-pdf redact in.pdf -f terms.txt --single-label

# Remove pages (1-based numbers, lists and ranges)
redact-pdf remove-pages report.pdf 1,3,92-100
# Writes report_TRIMMED.pdf

# Render every page to an image when an image-only PDF is needed
redact-pdf flatten reviewed.pdf --dpi 300
# Writes reviewed_FLATTENED.pdf; text is no longer searchable
```

The input is never overwritten. An existing output file is overwritten. Batch redaction skips files ending in `_REDACTED`.

## Matching and defaults

- **Terms match whole words, ignoring case.** `-t "ETH"` matches "ETH", "eth" and the "ETH" in "ETH-Zurich", while preserving "methodology", "Whether" and "ethz". Accented Unicode text, line breaks and end-of-line hyphenation are supported.
- **Regexes follow the pattern you provide.** Add `(?i)` for case-insensitive matching. Use a regex for intentional substring matching, such as `-r "(?i)Smith"` to match within "Smithson".
- **Email-derived names are best effort.** Check the report: a person's name can also be an ordinary word. Disable this behavior with `--no-email-names` or `--no-email-domains`; use `--no-email` to keep email addresses too.
- **Comments and forms are merged into the page** before automatic redaction, so they are no longer editable. The [`--keep-annotations` option](docs/reference.md#comments-and-form-fields) has different redaction coverage.

For all options, label customization, exit codes and cleanup details, see the [command reference](docs/reference.md) or run `redact-pdf COMMAND --help`.

## Limits

Text inside images and text converted to vector outlines need manual review. Letter-spaced text, unlisted spelling variants and obfuscated emails may also be missed. Encrypted PDFs must be decrypted first.

Hidden-image cleanup excludes inline images and stencil masks; embedded image metadata is cleaned only for JPEGs. See [limitations](docs/reference.md#limitations) for details.

**Always review the final document before sharing.** Automated redaction removes known matches; surrounding context can still identify a person or organisation.

## Development

```bash
git clone https://github.com/marcobertenghi/redact-pdf
cd redact-pdf
uv sync
uv run pytest
```

See the [development guide](docs/development.md) for linting, type checks, project structure and contribution notes.

## License

[GNU AGPL-3.0-or-later](LICENSE). PyMuPDF is available under the AGPL or a separate commercial license from Artifex.
