"""Pre-render the report pages every item cites into web-sized images.

The annotator has to be able to check whether the quoted sentence really appears
on the cited page, so the page image is part of the task, not a nice-to-have.
app.py can also render on demand, but doing it up-front keeps the first view of
each task fast (the source PDFs are 20-40 MB each).

    python render_pages.py                 # every active item
    python render_pages.py --company ESUN
    python render_pages.py --top 300       # only the highest-priority items
"""

import argparse
import json
import os

import common
import config

pymupdf = common.Pymupdf()
if pymupdf is None:  # pragma: no cover
    raise SystemExit("PyMuPDF is required: pip install pymupdf")


def RenderPage(doc, company, roc_year, page, dpi, overwrite=False):
    """Render one 1-indexed page; return 'written' | 'cached' | 'missing'."""
    out = common.PageImagePath(company, roc_year, page)
    if os.path.isfile(out) and not overwrite:
        return "cached"
    if page < 1 or page > doc.page_count:
        return "missing"
    os.makedirs(os.path.dirname(out), exist_ok=True)
    pix = doc.load_page(page - 1).get_pixmap(dpi=dpi)
    pix.save(out, jpg_quality=config.PAGE_JPEG_QUALITY)
    return "written"


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db", default=config.DB_PATH)
    ap.add_argument("--company", nargs="*")
    ap.add_argument("--top", type=int, default=0, help="only the N highest-priority items")
    ap.add_argument("--dpi", type=int, default=config.PAGE_DPI)
    ap.add_argument("--overwrite", action="store_true")
    args = ap.parse_args()

    conn = common.Connect(args.db)
    sql = "SELECT company, declared_year, source_page, payload FROM item WHERE active = 1"
    params = []
    if args.company:
        sql += f" AND company IN ({','.join('?' * len(args.company))})"
        params.extend(args.company)
    sql += " ORDER BY priority DESC"
    if args.top:
        sql += f" LIMIT {int(args.top)}"

    # company -> roc_year -> set(pages)
    wanted = {}
    for row in conn.execute(sql, params):
        # The commitment's own source page, shown at stage 1.
        src = common.CitedPages({"evidence_page": row["source_page"]})
        if src and isinstance(row["declared_year"], int):
            wanted.setdefault(row["company"], {}).setdefault(row["declared_year"], set()).update(src)
        for entry in common.YearRows(json.loads(row["payload"])):
            if entry["pages"]:
                wanted.setdefault(row["company"], {}).setdefault(entry["year"], set()).update(
                    entry["pages"]
                )

    stats = {"written": 0, "cached": 0, "missing": 0, "no_pdf": 0}
    for company in sorted(wanted):
        for roc_year in sorted(wanted[company]):
            pdf = common.PdfPath(company, roc_year)
            pages = sorted(wanted[company][roc_year])
            if not os.path.isfile(pdf):
                stats["no_pdf"] += len(pages)
                print(f"  ! missing pdf: {pdf}")
                continue
            with pymupdf.open(pdf) as doc:
                for page in pages:
                    stats[RenderPage(doc, company, roc_year, page, args.dpi, args.overwrite)] += 1
            print(f"{company} {roc_year}: {len(pages)} pages")

    print(f"\nwritten={stats['written']} cached={stats['cached']} "
          f"out_of_range={stats['missing']} no_pdf={stats['no_pdf']}")
    print(f"images under {config.PAGE_IMAGE_DIR}")


if __name__ == "__main__":
    main()
