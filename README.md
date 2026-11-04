# redact-pdf

![Python](https://img.shields.io/badge/python-3.10%2B-blue)
![License](https://img.shields.io/badge/license-AGPL--3.0-green)

Remove names, addresses, email addresses and other known strings from PDFs **for real**: the text is deleted from the file, not just hidden behind a black box. Each match is replaced by a black box labelled `REDACTED`, all metadata is stripped (including the places exiftool doesn't show), and a built-in check confirms nothing was left behind.

```console
$ redact-pdf redact contract.pdf -t "John Smith" -t "ACME GmbH"
contract_REDACTED.pdf: 5 redaction(s)
  page 1: 'John Smith' x3, 'ACME GmbH' x1, 'john.smith@acme.de' x1
  metadata removed: JPEG EXIF/XMP/comments x1, XMP packets x2, document info x1
  review manually, pages with images: 1 (1 img)
```

## Why?

Drawing a black rectangle over text in a PDF editor usually **does not remove it**. The text is still in the file and can be copied, searched, or revealed by deleting the rectangle. This has caused real-world data leaks in court filings and government documents.

`redact-pdf` uses [PyMuPDF](https://pymupdf.readthedocs.io/)'s redaction API, which deletes the matched glyphs from the page content and blanks any image pixels underneath. It then removes the hidden data that commonly identifies a document's author, origin or history.

## Features

- **True redaction** of literal strings (case-insensitive) and regular expressions
- **Email addresses removed by default**, no configuration needed
- **Minimal typing**: `redact-pdf redact file.pdf` writes `file_REDACTED.pdf`
- **Deep metadata removal**: document info, XMP everywhere, EXIF inside images, private application data, annotation authors, document ID ([details](#metadata-removal))
- **Comments and form fields** are merged into the page, so their visible text is redacted too
- **Bookmarks** are redacted along with the page text
- **Readable output**: black boxes with a configurable label, auto-sized to fit
- **Term lists**: keep your terms in a file, one per line
- **Batch mode**: redact a whole directory in one call
- **Self-check**: re-scans the output and exits non-zero if anything remains
- **Review helper**: lists pages containing images, which need a manual look
- **Flatten**: burn in boxes drawn by hand in any PDF annotator
- **No OCR, no cloud, no external tools**: runs locally, single Python dependency
- **Usable as a library** with a typed Python API

## Installation

You need [uv](https://docs.astral.sh/uv/). If you don't have it yet:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

uv downloads a suitable Python and installs all dependencies itself; you don't need to install PyMuPDF or any system packages separately.

**Install as a command-line tool** (recommended):

```bash
uv tool install git+https://github.com/marcobertenghi/redact-pdf
redact-pdf --help
```

This puts `redact-pdf` in an isolated environment and on your `PATH`. Update with `uv tool upgrade redact-pdf`, remove with `uv tool uninstall redact-pdf`.

**Run once without installing:**

```bash
uvx --from git+https://github.com/marcobertenghi/redact-pdf redact-pdf --help
```

**From a local clone** (for development):

```bash
git clone https://github.com/marcobertenghi/redact-pdf
cd redact-pdf
uv sync
uv run redact-pdf --help
```

## Usage

### `redact`: remove matches and metadata

```bash
# Email addresses and metadata only -> in_REDACTED.pdf
redact-pdf redact in.pdf

# Plus literal terms
redact-pdf redact in.pdf -t "John Smith" -t "ACME GmbH"

# Terms from a file (see examples/terms.example.txt)
redact-pdf redact in.pdf -f terms.txt

# Extra regular expressions, e.g. German IBANs
redact-pdf redact in.pdf -r "DE\d{2}(?: ?\d{4}){4} ?\d{2}"

    # Choose the output name yourself
redact-pdf redact in.pdf shared/contract.pdf -f terms.txt

# Every PDF in a folder -> *_REDACTED.pdf next to each file
redact-pdf redact ./documents -f terms.txt

# ...or into another folder
redact-pdf redact ./documents ./redacted -f terms.txt
```

| Option | Description |
| --- | --- |
| `OUTPUT` | Optional. File or directory. Default: `<input>_REDACTED.pdf` next to the input. |
| `-t, --term TEXT` | Literal text to match, case-insensitive. Repeatable. |
| `-f, --terms-file FILE` | One term per line; lines starting with `#` are ignored. |
| `-r, --regex PATTERN` | Regular expression to match. Repeatable. |
| `--no-email` | Don't redact email addresses (they are redacted by default). |
| `--label TEXT` | Text inside each box. Default: `REDACTED`. |
| `--no-label` | Plain black boxes. |
| `--keep-annotations` | Keep comments and form fields interactive (see below). |
| `--no-scrub` | Keep links, attachments, scripts and hidden text. |

Longer terms are matched first, so listing both `John Smith` and `Smith` works as expected. The input file is never modified; an existing output file is overwritten. In batch mode, files already ending in `_REDACTED` are skipped, so running the same command twice is safe.

#### Built-in email detection

Email addresses are matched with a pragmatic pattern (`redact_pdf.EMAIL_PATTERN`) that handles what appears in real documents: dots, `+tags`, hyphens and underscores, subdomains (`mail.example.co.uk`), uppercase, and non-ASCII letters (`jürgen.müller@beispiel.de`). Surrounding punctuation such as `<...>`, `(...)`, a trailing full stop or a `mailto:` prefix is not included in the match. It deliberately does not match `user@localhost` or addresses with numeric "TLDs", which are rarely real email addresses in documents and mostly cause false positives. Repetitions are length-bounded, so matching stays fast even on unusual text.

#### Comments and form fields

By default, annotations (comments, sticky notes, highlights, stamps) and form fields are merged into the page before searching. Their visible text then gets redacted like any other text, and their hidden data, such as comment text in pop-ups, author names and timestamps, disappears. The downside is that comments can no longer be edited and forms are no longer fillable.

With `--keep-annotations` they stay interactive and only their authors and timestamps are removed. Their text is **not** searched or redacted in that case.

### `verify`: check for leftovers

```bash
redact-pdf verify in_REDACTED.pdf -f terms.txt
```

Searches the text layer and lists any remaining matches, including email addresses unless `--no-email` is given. Useful after manual edits or on files redacted with other tools.

### `flatten`: burn in manual edits

```bash
redact-pdf flatten reviewed.pdf          # -> reviewed_FLATTENED.pdf
redact-pdf flatten reviewed.pdf final.pdf --dpi 300
```

Renders every page, including annotations, to an image. Nothing hidden survives, but the result is no longer text-searchable and is larger.

### Exit codes

| Code | Meaning |
| --- | --- |
| `0` | Success; output is clean |
| `1` | Matches remain in the output (or in the file passed to `verify`) |
| `2` | Error, e.g. missing file, encrypted PDF, invalid arguments |

This makes the tool easy to use in scripts and CI pipelines.

## Metadata removal

Metadata is stripped on every `redact` run, directly in Python; no external tool is needed. This covers:

| Where | What it can reveal |
| --- | --- |
| Document info dictionary | Author, title, company, creating application, dates |
| XMP packets on the document, pages, images and fonts | Same as above, plus edit history and document IDs |
| EXIF, XMP, IPTC and comments inside embedded JPEGs | Camera model, GPS location, photographer, original timestamps |
| JPEG thumbnails and data appended to images | A preview of the uncropped photo, secondary phone-camera images |
| Application private data (`/PieceInfo`) | Leftover data from Illustrator, Word and others, sometimes whole drafts |
| Modification dates on pages | When parts of the document were last edited |
| Annotation authors and timestamps | Names of reviewers and when they commented |
| Trailer document ID | Links the output to the original file |
| Earlier revisions | Content from before previous edits (the file is fully rewritten) |

JPEG images are cleaned losslessly: the compressed image data is copied byte for byte, and only metadata segments are dropped. Colour profiles needed for correct rendering are kept.

**Why not exiftool?** For PDFs, exiftool only knows the document info dictionary and document-level XMP; the other places in the table are invisible to it. Its PDF edits are also incremental: `exiftool -all= file.pdf` appends an update that hides the old values, but leaves them in the file, and exiftool itself warns that deleted tags can be recovered. exiftool is still a handy way to *check* the result:

```bash
exiftool -a -G1 in_REDACTED.pdf
```

## Recommended workflow

Automatic redaction only finds text that exists *as text* in the PDF. Text inside images (screenshots, scans, signatures, logos) and variants you didn't list need a manual pass.

1. **Redact automatically.**
   ```bash
   redact-pdf redact in.pdf -f terms.txt
   ```
   Note the pages listed under *review manually*.

2. **Review the flagged pages** in `in_REDACTED.pdf`. Look for text inside images, and for variants missing from your list: initials, nicknames, misspellings, email handles. Add those to `terms.txt` and run step 1 again; that's faster than fixing them by hand.

3. **Cover what's left by hand.** On Linux, [Xournal++](https://xournalpp.github.io/) is lightweight and works well:
   ```bash
   sudo apt install xournalpp   # Debian, Ubuntu, Kali
   ```
   One-time setup: select the **Draw Rectangle** tool, enable *Tools → Pen Options → Fill*, set *Tools → Pen Options → Fill Opacity* to **100%** (otherwise boxes are see-through), and choose black. Draw your boxes, then *File → Export as PDF* to `reviewed.pdf`.

4. **Flatten.** This step is required after step 3: Xournal++ and similar tools only draw *on top* of the original, which is still intact underneath.
   ```bash
   redact-pdf flatten reviewed.pdf     # -> reviewed_FLATTENED.pdf
   ```

5. **Check the result.** Run `redact-pdf verify` on unflattened files. Open the final file, select all text on a page and paste it somewhere to confirm nothing unexpected comes along.

If you used Xournal++, delete the `.xopp` project file it may have saved; it references your original document.

## Limitations

- **Text in images is not detected.** There is no OCR by design; use the manual workflow above.
- **Vector text** (text converted to outlines, common in logos) is not text and is not found.
- **Exact matches only.** Hyphenated line breaks, unusual spacing or ligatures can prevent a match. Add fragments (e.g. the surname alone) as extra terms.
- **Obfuscated emails** such as `name [at] example [dot] com` are not detected; add them as terms or a custom regex.
- **Case-insensitivity applies to ASCII.** For accented names, list the variants you expect (`Müller`, `MÜLLER`).
- **Image metadata is only stripped from JPEGs**, by far the most common case. Rarer formats such as JPEG 2000 are left unchanged; `flatten` re-renders everything if you need certainty.
- **Encrypted PDFs** must be decrypted first, e.g. `qpdf --decrypt --password=... in.pdf out.pdf`.

Always review the output before sharing. This tool reduces effort; it does not replace a human check.

## Development

```bash
uv sync                    # create .venv with runtime and dev dependencies
uv run pytest              # tests
uv run pytest --cov=redact_pdf
uv run ruff check .        # lint
uv run ruff format .       # format
uv run mypy src            # type check
```

Project layout:

```
redact-pdf/
├── src/redact_pdf/
│   ├── core.py        # redaction, verification and flattening
│   ├── metadata.py    # deep metadata removal, lossless JPEG cleaning
│   ├── cli.py         # command-line interface
│   └── __main__.py    # python -m redact_pdf
├── tests/             # pytest suite with generated sample PDFs
├── examples/          # example term list
└── pyproject.toml
```

Contributions are welcome. Please open an issue first for larger changes, and make sure `ruff`, `mypy` and `pytest` pass. Never commit real documents or term lists; `.gitignore` excludes `*.pdf` and `terms.txt` by default.

## License

[GNU AGPL-3.0-or-later](LICENSE). This project depends on PyMuPDF, which is licensed under the AGPL (or a commercial license from Artifex), so `redact-pdf` uses the same license.

## Disclaimer

This software is provided as is, without warranty. You are responsible for verifying that redacted documents meet your legal and privacy requirements (e.g. GDPR).
