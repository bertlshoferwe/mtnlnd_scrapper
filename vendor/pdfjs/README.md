# pdf.js (vendored)

Front-end PDF viewer for the dashboard's document preview modal. Bundled in
the repo (served by Flask at `/vendor/pdfjs/…`) rather than pulled from a CDN
so it works offline / behind a strict network and has no external dependency.

- **Version:** pdfjs-dist 6.3.289 — `legacy/build` (broadest browser support)
- **Source:** https://cdn.jsdelivr.net/npm/pdfjs-dist@6.3.289/legacy/build/
- **Files:** `pdf.min.mjs` (main), `pdf.worker.min.mjs` (worker)

To update: bump the version, re-download both files from the same path, keep
the filenames. The loader is `loadPdfLib()` in `templates/index.html`.

Not vendored: `cmaps/` and `standard_fonts/`. PDFs that don't embed their
fonts or use CJK encodings may render with substitute glyphs; the modal's
"open it in your browser" fallback covers those. Add those dirs here (and set
`cMapUrl` / `standardFontDataUrl` on `getDocument`) if it becomes a problem.
