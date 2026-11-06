# Command reference

Detailed options, matching rules and cleanup behavior. For installation and the recommended workflow, see the [README](../README.md). Run `redact-pdf COMMAND --help` for help in your terminal.

## `redact`: remove matches and metadata

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
| `-t, --term TEXT` | Literal text to match as whole words, case-insensitive. Repeatable. |
| `-f, --terms-file FILE` | One term per line; lines starting with `#` are ignored. |
| `-r, --regex PATTERN` | Regular expression to match. Repeatable. |
| `--no-email` | Don't redact email addresses (they are redacted by default). |
| `--no-email-domains` | Don't redact organisation names taken from email domains (see below). |
| `--no-email-names` | Don't redact person names taken from email addresses (see below). |
| `--label TEXT` | Text inside each box. Default: `REDACTED`. |
| `--no-label` | Plain black boxes. |
| `--keep-annotations` | Keep comments and form fields interactive (see below). |
| `--no-scrub` | Keep links, attachments, scripts and hidden text. |

Terms are matched case-insensitively, for accented letters too (`Müller` also finds `MÜLLER`). Accented letters match however the PDF stores them: `ö` as one character or as `o` plus a combining accent, which looks identical but is different text. A space in a term matches any whitespace, including a line break, and words hyphenated at the end of a line are found (`Mül-` / `ler`). Terms match as **whole words**: `-t "ETH"` removes "ETH", "eth" and "ETH-Zurich", but leaves "methodology", "Whether" and "ethz" intact. Letters, digits and combining accents prevent a word boundary; punctuation and underscores separate words. This applies to `-t`, term files and `verify`, including text outside the pages. Listing both `John Smith` and `Smith` works as expected.

Regular expressions are applied as written: they are case-sensitive unless you add `(?i)`, and `\b` word boundaries are respected (`-r "\bAnn\b"` leaves "Annual" alone).

For intentional matching inside longer words, use a regular expression: `-r "(?i)Smith"` also removes the "Smith" in "Smithson".

The input file is never modified; an existing output file is overwritten. In batch mode, files already ending in `_REDACTED` are skipped, so running the same command twice is safe.

### Built-in email detection

Email addresses are matched with a pragmatic pattern (`redact_pdf.EMAIL_PATTERN`) that handles what appears in real documents: dots, `+tags`, hyphens and underscores, subdomains (`mail.example.co.uk`), uppercase, and non-ASCII letters (`jürgen.müller@beispiel.de`). Surrounding punctuation such as `<...>`, `(...)`, a trailing full stop or a `mailto:` prefix is not included in the match. It deliberately does not match `user@localhost` or addresses with numeric "TLDs", which are rarely real email addresses in documents and mostly cause false positives. Repetitions are length-bounded, so matching stays fast even on unusual text.

### Organisation names from email domains

When emails are redacted, the organisation name in each address's domain is redacted too, everywhere in the same document: `jane@acme.com` adds `acme`, and `a@mail.acme.co.uk` also adds `acme`. These names are matched as **whole words**, so "ACME", "ACME-Group" and "www.acme.com" are redacted, but "Acmeplex" is not. A domain found on the last page is redacted on the first page too. In batch mode, each file only uses the domains found in that file.

Mail providers are skipped, so `joe@gmail.com` does not add "gmail" (and `web.de` doesn't remove every "web"). The list is `redact_pdf.FREEMAIL_DOMAINS`; it also covers `example.com` and the other reserved documentation domains. The report shows which names were added and where they came from. Use `--no-email-domains` to turn this off.

### Person names from email addresses

Also when emails are redacted, the names belonging to each address are redacted everywhere in the same document, as whole words. Names come from two places:

| Source | Example | Redacted |
| --- | --- | --- |
| Display name next to the address | `Lukas König <lk@acme.ch>`, `"König, Lukas" <…>`, `Alberto Ornaghi (ALoR) <…>` | "Lukas König", "Lukas", "König", exactly as written |
| Parts of the address | `lukas.koenig@acme.ch` | "lukas koenig", "lukas", "koenig", accent-insensitive: "koenig" also finds "König" and "Konig" |

A display name on the line before its address is found too, even across a page break. This is a heuristic, so check the `names from` lines in the report:

- A single name word must start with a capital letter (`format_c@` does not remove "format" in running text, but does remove "Format" or "FORMAT"). Full names such as "john smith" match in any case.
- Role accounts (`info@`, `support@`, `buchhaltung@` …), mailing lists (`project-users@`, `@lists.…`), titles (Dr., Herr) and email header words are skipped. Parts shorter than 3 letters, usually initials, are ignored.
- Names that are also common capitalised words (Will, Mark, German nouns) are redacted wherever they appear capitalised. Nicknames and names that never appear next to an address are not found; add them as terms.

Use `--no-email-names` to turn this off.

### Comments and form fields

By default, annotations (comments, sticky notes, highlights, stamps) and form fields are merged into the page before searching. Their visible text then gets redacted like any other text, and their hidden data, such as comment text in pop-ups, author names and timestamps, disappears. The downside is that comments can no longer be edited and forms are no longer fillable.

With `--keep-annotations` they stay interactive, and their authors and timestamps are removed. Matches in their hidden text (pop-up comments, form values) are replaced, but the text they **draw on the page is not** redacted in that case.

## `verify`: check for leftovers

```bash
redact-pdf verify in_REDACTED.pdf -f terms.txt
```

Searches the text of every page and lists any remaining matches, including email addresses unless `--no-email` is given. Text strings outside the pages (metadata, bookmarks, layer names, link targets and so on) are checked too and listed as `outside page text`. Images that still hold content in areas no page shows are listed as `image content outside the visible page` ([details](#hidden-image-areas)). Useful after manual edits or on files redacted with other tools.

## `apply-boxes`: make manual boxes real

```bash
redact-pdf apply-boxes reviewed.pdf          # -> reviewed_FINAL.pdf
redact-pdf apply-boxes reviewed.pdf final.pdf

# Faster after marking blots or highlights in Firefox and saving normally
redact-pdf apply-boxes reviewed.pdf --annotations-only
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

Use `--annotations-only` when your manual marks are annotations, as with Firefox's **Draw** and **Highlight** tools saved with **Save**. Each pen stroke becomes the same box as in the default mode, including its pen width; separate blots stay separate. This mode skips searching existing page drawings for boxes, so earlier automatic redactions are not detected and applied again. It still removes content under the manual marks, cleans metadata and hidden image areas, and verifies the result.

Boxes already drawn into page content, such as Xournal++ exports, are not applied or checked in this mode. Use the default mode for those files, including mixed files where some manual marks are annotations and others are page drawings. Text boxes and comments are merged into the page but are not treated as redaction marks in `--annotations-only` mode. If no annotation marks are found, the command warns and exits with code 1.

`apply-boxes` exits with code 1 and a warning when it finds no boxes, or when something lies over text without hiding it: a see-through fill, the highlighter or a pen scribble. Such marks look like redactions on screen but leave the text readable and copyable; redraw them as filled boxes.

A drawing such as a signature is deleted when it lies mostly under a box. A longer drawing that only passes through a box, such as a table border, is kept so the page doesn't lose its lines; the part under the box stays in the file.

## `remove-pages`: drop pages completely

```bash
redact-pdf remove-pages report.pdf 92-100              # -> report_TRIMMED.pdf
redact-pdf remove-pages report.pdf 1,3,92-100 out.pdf  # lists and ranges
redact-pdf remove-pages report.pdf 92-                 # page 92 to the end
```

Removes the pages and everything only they use: text, images, fonts, comments and form fields. Bookmarks to removed pages are dropped (their titles would otherwise stay in the file), as are links and named jump targets that led there. The remaining pages follow on directly: old page 101 becomes page 92. Page numbers are given as in the input file.

Deleting a page from the page list alone is not enough, because a jump target, the "open at" action or a form field can still point to it and keep it, with all its content, in the file. `remove-pages` clears those references and checks afterwards that no removed page is left. Metadata and hidden content are removed as in `redact`; `--no-scrub` keeps links, attachments and scripts.

Numbers printed on the pages themselves (a footer reading "Page 101") are not changed. Custom page labels (viewer numbering such as i, ii, 1, 2) are kept as defined.

## `flatten`: burn in manual edits

```bash
redact-pdf flatten reviewed.pdf          # -> reviewed_FLATTENED.pdf
redact-pdf flatten reviewed.pdf final.pdf --dpi 300
```

Renders every page, including annotations, to an image. Nothing hidden survives, but the result is no longer text-searchable and much larger. Prefer `apply-boxes`; use `flatten` if you need every page as a plain image, for example for scans.

## Exit codes

| Code | Meaning |
| --- | --- |
| `0` | Success; output is clean |
| `1` | Matches remain in the output (or in the file passed to `verify`); for `apply-boxes`: no boxes found, text left under a box, or a mark that does not hide text; for `remove-pages`: a removed page is still in the file |
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

### Hidden image areas

A page often shows only part of a stored image. Word, browsers and Confluence crop images by hiding part of them, and a long screenshot split across two pages is usually stored *whole* with each page, each showing its half. The hidden part is still in the file, and any PDF tool can extract it (`pdfimages -all file.pdf`).

This also undermines redaction: a box blanks the pixels under it only in the copy on its own page. A logo blacked out on page 4 stays intact in the copy stored with page 5.

`redact` and `apply-boxes` therefore replace every image area that no page shows with white, including the image's transparency mask, after redacting. What the pages show is unchanged: lossless images are rewritten pixel for pixel, and JPEGs are re-compressed at high quality (differences of a few colour levels at most). Images whose hidden area is already blank are left as they are. `verify` reports images that still hold content in such areas.

**Why not exiftool?** For PDFs, exiftool only knows the document info dictionary and document-level XMP; the other places in the table are invisible to it. Its PDF edits are also incremental: `exiftool -all= file.pdf` appends an update that hides the old values, but leaves them in the file, and exiftool itself warns that deleted tags can be recovered. exiftool is still a handy way to *check* the result:

```bash
exiftool -a -G1 in_REDACTED.pdf
```

## Limitations

- **Text in images is not detected.** There is no OCR by design; use the [manual review workflow](../README.md#recommended-workflow).
- **Inline images and stencil masks** (rare; mostly small icons and 1-bit scans) are not checked for hidden areas. `flatten` re-renders everything if you need certainty.
- **Vector text** (text converted to outlines, common in logos) is not text and is not found.
- **Exact matches only.** Line breaks, hyphenation and ligatures are handled, but letter-spaced text (`J o h n`) or a name split across table cells can prevent a match. Add fragments (e.g. the surname alone) as extra terms.
- **Obfuscated emails** such as `name [at] example [dot] com` are not detected; add them as terms or a custom regex.
- **Spelling variants are not matched.** `Müller` does not find `Mueller`, and `Straße` does not find `STRASSE`; list the variants you expect.
- **Image metadata is only stripped from JPEGs**, by far the most common case. Rarer formats such as JPEG 2000 are left unchanged; `flatten` re-renders everything if you need certainty.
- **Encrypted PDFs** must be decrypted first, e.g. `qpdf --decrypt --password=... in.pdf out.pdf`.

Always review the output before sharing. This tool reduces effort; it does not replace a human check.

