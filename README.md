# redact-pdf

![Python](https://img.shields.io/badge/python-3.10%2B-blue)
![License](https://img.shields.io/badge/license-AGPL--3.0-green)

Remove names, addresses, email addresses and other known strings from PDFs **for real**: the text is deleted from the file, not just hidden behind a black box. Each match is replaced by a black box labelled `REDACTED`, all metadata is stripped (including the places exiftool doesn't show), and a built-in check confirms nothing was left behind.

```console
$ redact-pdf redact contract.pdf -t "John Smith"
contract_REDACTED.pdf: 5 redaction(s)
  email domains: 'acme' (from john.smith@acme.de)
  names from john.smith@acme.de: 'john smith', 'john', 'smith'
  page 1: 'john.smith@acme.de' x1, 'John Smith' x2, 'acme' x2
  outside page text: 'John Smith' x1
  metadata removed: document info x1
  review manually, pages with images: 1 (1 img)
```

## Why?

Drawing a black rectangle over text in a PDF editor usually **does not remove it**. The text is still in the file and can be copied, searched, or revealed by deleting the rectangle. This has caused real-world data leaks in court filings and government documents.

`redact-pdf` uses [PyMuPDF](https://pymupdf.readthedocs.io/)'s redaction API, which deletes the matched glyphs from the page content and blanks any image pixels underneath. It then removes the hidden data that commonly identifies a document's author, origin or history.

## Features

- **True redaction** of literal strings (case-insensitive, including `Ü`/`ü`) and regular expressions
- **Email addresses removed by default**, no configuration needed
- **Organisation names from email domains** removed too: `jane@acme.com` also removes "ACME" everywhere in the document
- **Person names from email addresses** (best effort): `Lukas Körnich <lukas.koernich@acme.ch>` also removes "Lukas" and "Körnich" everywhere in the document
- **Minimal typing**: `redact-pdf redact file.pdf` writes `file_REDACTED.pdf`
- **Deep metadata removal**: document info, XMP everywhere, EXIF inside images, private application data, annotation authors, document ID ([details](#metadata-removal))
- **Comments and form fields** are merged into the page, so their visible text is redacted too
- **Text outside the page** is redacted too: bookmarks, layer names, page labels, link targets, alternate text for screen readers
- **Readable output**: black boxes with a configurable label, auto-sized to fit
- **Term lists**: keep your terms in a file, one per line
- **Batch mode**: redact a whole directory in one call
- **Self-check**: re-scans the output, including text outside the pages, and exits non-zero if anything remains
- **Review helper**: lists pages containing images, which need a manual look
- **Apply manual boxes**: marks drawn by hand (in Firefox or any PDF viewer) become real redactions, without turning pages into images
- **Flatten**: alternatively, burn everything into images
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
| `--no-email-domains` | Don't redact organisation names taken from email domains (see below). |
| `--no-email-names` | Don't redact person names taken from email addresses (see below). |
| `--label TEXT` | Text inside each box. Default: `REDACTED`. |
| `--no-label` | Plain black boxes. |
| `--keep-annotations` | Keep comments and form fields interactive (see below). |
| `--no-scrub` | Keep links, attachments, scripts and hidden text. |

Terms are matched case-insensitively, for accented letters too (`Müller` also finds `MÜLLER`). Accented letters match however the PDF stores them: `ö` as one character or as `o` plus a combining accent, which looks identical but is different text. A space in a term matches any whitespace, including a line break, and words hyphenated at the end of a line are found (`Mül-` / `ler`). Terms also match inside longer words, so `Smith` removes `Smithson` as well; listing both `John Smith` and `Smith` works as expected.

Regular expressions are applied as written: they are case-sensitive unless you add `(?i)`, and `\b` word boundaries are respected (`-r "\bAnn\b"` leaves "Annual" alone).

The input file is never modified; an existing output file is overwritten. In batch mode, files already ending in `_REDACTED` are skipped, so running the same command twice is safe.

#### Built-in email detection

Email addresses are matched with a pragmatic pattern (`redact_pdf.EMAIL_PATTERN`) that handles what appears in real documents: dots, `+tags`, hyphens and underscores, subdomains (`mail.example.co.uk`), uppercase, and non-ASCII letters (`jürgen.müller@beispiel.de`). Surrounding punctuation such as `<...>`, `(...)`, a trailing full stop or a `mailto:` prefix is not included in the match. It deliberately does not match `user@localhost` or addresses with numeric "TLDs", which are rarely real email addresses in documents and mostly cause false positives. Repetitions are length-bounded, so matching stays fast even on unusual text.

#### Organisation names from email domains

When emails are redacted, the organisation name in each address's domain is redacted too, everywhere in the same document: `jane@acme.com` adds `acme`, and `a@mail.acme.co.uk` also adds `acme`. These names are matched as **whole words**, so "ACME", "ACME-Group" and "www.acme.com" are redacted, but "Acmeplex" is not. A domain found on the last page is redacted on the first page too. In batch mode, each file only uses the domains found in that file.

Mail providers are skipped, so `joe@gmail.com` does not add "gmail" (and `web.de` doesn't remove every "web"). The list is `redact_pdf.FREEMAIL_DOMAINS`; it also covers `example.com` and the other reserved documentation domains. The report shows which names were added and where they came from. Use `--no-email-domains` to turn this off.

#### Person names from email addresses

Also when emails are redacted, the names belonging to each address are redacted everywhere in the same document, as whole words. Names come from two places:

| Source | Example | Redacted |
| --- | --- | --- |
| Display name next to the address | `Lukas Körnich <lk@acme.ch>`, `"Körnich, Lukas" <…>`, `Alberto Ornaghi (ALoR) <…>` | "Lukas Körnich", "Lukas", "Körnich", exactly as written |
| Parts of the address | `lukas.koernich@acme.ch` | "lukas koernich", "lukas", "koernich", accent-insensitive: "koernich" also finds "Körnich" and "Kornich" |

A display name on the line before its address is found too, even across a page break. This is a heuristic, so check the `names from` lines in the report:

- A single name word must start with a capital letter (`format_c@` does not remove "format" in running text, but does remove "Format" or "FORMAT"). Full names such as "john smith" match in any case.
- Role accounts (`info@`, `support@`, `buchhaltung@` …), mailing lists (`project-users@`, `@lists.…`), titles (Dr., Herr) and email header words are skipped. Parts shorter than 3 letters, usually initials, are ignored.
- Names that are also common capitalised words (Will, Mark, German nouns) are redacted wherever they appear capitalised. Nicknames and names that never appear next to an address are not found; add them as terms.

Use `--no-email-names` to turn this off.

#### Comments and form fields

By default, annotations (comments, sticky notes, highlights, stamps) and form fields are merged into the page before searching. Their visible text then gets redacted like any other text, and their hidden data, such as comment text in pop-ups, author names and timestamps, disappears. The downside is that comments can no longer be edited and forms are no longer fillable.

With `--keep-annotations` they stay interactive, and their authors and timestamps are removed. Matches in their hidden text (pop-up comments, form values) are replaced, but the text they **draw on the page is not** redacted in that case.

### `verify`: check for leftovers

```bash
redact-pdf verify in_REDACTED.pdf -f terms.txt
```

Searches the text of every page and lists any remaining matches, including email addresses unless `--no-email` is given. Text strings outside the pages (metadata, bookmarks, layer names, link targets and so on) are checked too and listed as `outside page text`. Useful after manual edits or on files redacted with other tools.

### `apply-boxes`: make manual boxes real

```bash
redact-pdf apply-boxes reviewed.pdf          # -> reviewed_FINAL.pdf
redact-pdf apply-boxes reviewed.pdf final.pdf
```

Marks drawn in a PDF viewer such as Firefox only *cover* content; the text, image pixels and drawings underneath stay in the file. `apply-boxes` deletes what is under each box, like `redact` does for matched text. Pages are not turned into images, so the rest of the text stays searchable and the file stays small: a 23-page document with 9 boxes went from 387 KB to 392 KB, where `flatten` produced 7.1 MB.

```console
$ redact-pdf apply-boxes reviewed.pdf
reviewed_FINAL.pdf: 3 box(es) applied
  annotation boxes, pages: 1 (2), 2 (1)
  metadata removed: document info x1
```

What counts as a box:

| Annotations (Firefox, Okular, Acrobat) | Drawn into the page (e.g. Xournal++ exports) |
| --- | --- |
| Highlights, freehand drawing, rectangles, circles, polygons, strike-outs and redaction marks, in any colour and opacity. Highlights remove exactly the highlighted text; freehand drawing removes a rectangle around each stroke, as wide as the pen. | Opaque, filled shapes of any colour, including white "whiteout" boxes, that lie over text, images or drawings. A shape that is not a rectangle is applied as its bounding rectangle. |

Filled design elements are left alone: a black header bar with white text on it covers nothing, so it is not a box. Comments and form fields are merged into the page, and metadata is removed as in `redact`.

`apply-boxes` exits with code 1 and a warning when it finds no boxes, or when something lies over text without hiding it: a see-through fill, the highlighter or a pen scribble. Such marks look like redactions on screen but leave the text readable and copyable; redraw them as filled boxes.

A drawing such as a signature is deleted when it lies mostly under a box. A longer drawing that only passes through a box, such as a table border, is kept so the page doesn't lose its lines; the part under the box stays in the file.

### `flatten`: burn in manual edits

```bash
redact-pdf flatten reviewed.pdf          # -> reviewed_FLATTENED.pdf
redact-pdf flatten reviewed.pdf final.pdf --dpi 300
```

Renders every page, including annotations, to an image. Nothing hidden survives, but the result is no longer text-searchable and much larger. Prefer `apply-boxes`; use `flatten` if you need every page as a plain image, for example for scans.

### Exit codes

| Code | Meaning |
| --- | --- |
| `0` | Success; output is clean |
| `1` | Matches remain in the output (or in the file passed to `verify`); for `apply-boxes`: no boxes found, text left under a box, or a mark that does not hide text |
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
| Page thumbnails | A preview of each page *before* redaction (removed even with `--no-scrub`) |
| Accessibility tags and marked content (`/Alt`, `/ActualText`) | Alternate text for screen readers, which repeats the page text and shows up when text is copied |
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

3. **Mark what's left in Firefox.** Open `in_REDACTED.pdf` in Firefox; its built-in PDF viewer has the tools you need, top right:
   - **Highlight** for text: select the words. Exactly the highlighted characters are removed, nothing next to them.
   - **Draw** for images, signatures and scans: set *Thickness* to the maximum and scribble a short blot over each item. Each stroke removes a rectangle around it, as wide as the pen, so gaps inside a blot are covered too. Use one blot per item; a single long stroke across the page removes everything in its rectangle.

   Colour and opacity don't matter; every mark is applied and becomes a black box. Save with **Save** (Ctrl+S) as `reviewed.pdf`. Don't use *Print → Save to PDF*: it turns highlights into see-through drawings that `apply-boxes` cannot use.

   Other tools work too: annotations from Okular or Acrobat, or filled boxes drawn into the page and exported, e.g. from Xournal++ with fill opacity at 100%.

4. **Apply the boxes.** This step is required after step 3: Firefox and other viewers only draw *on top* of the original, which is still intact underneath.
   ```bash
   redact-pdf apply-boxes reviewed.pdf     # -> reviewed_FINAL.pdf
   ```
   If it warns that it found no boxes, check that you saved the file with your marks. If it warns about shapes that do not hide text, redraw them and save again.

5. **Check the result.** Run `redact-pdf verify` on the final file. Open it, select all text on a page and paste it somewhere to confirm nothing unexpected comes along.

## Limitations

- **Text in images is not detected.** There is no OCR by design; use the manual workflow above.
- **Vector text** (text converted to outlines, common in logos) is not text and is not found.
- **Exact matches only.** Line breaks, hyphenation and ligatures are handled, but letter-spaced text (`J o h n`) or a name split across table cells can prevent a match. Add fragments (e.g. the surname alone) as extra terms.
- **Obfuscated emails** such as `name [at] example [dot] com` are not detected; add them as terms or a custom regex.
- **Spelling variants are not matched.** `Müller` does not find `Mueller`, and `Straße` does not find `STRASSE`; list the variants you expect.
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
│   ├── boxes.py       # apply-boxes: manual boxes become real redactions
│   ├── matching.py    # finding terms by character position, email domains
│   ├── names.py       # person names taken from email addresses
│   ├── pdfstrings.py  # text outside the page content: string objects, marked content
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
