"""Command-line interface: ``regcomments <docket-or-document-id-or-url>``."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from . import __version__
from .api import ApiError, NotFound, RegulationsGovClient
from .downloader import DEFAULT_FORMAT_PREFERENCE, CommentDownloader, Options, resolve_target

API_KEY_ENV = "REGULATIONS_GOV_API_KEY"


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="regcomments",
        description=(
            "Bulk-download public comments from Regulations.gov: the text typed into the "
            "comment form AND the comment letters/attachments commenters uploaded."
        ),
        epilog=(
            "Examples:\n"
            "  regcomments EPA-HQ-OAR-2021-0317-0001          # comments on one proposed rule\n"
            "  regcomments EPA-HQ-OAR-2021-0317               # every comment in a docket\n"
            "  regcomments https://www.regulations.gov/document/FDA-2021-N-0471-0001\n"
            "  regcomments CFPB-2023-0038-0001 --limit 25 --all-formats\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("target", help="Document ID, docket ID, comment ID, or a regulations.gov URL")
    p.add_argument("--api-key", default=os.environ.get(API_KEY_ENV),
                   help=f"Regulations.gov API key (default: ${API_KEY_ENV}; falls back to the very "
                        "limited DEMO_KEY). Free key: https://open.gsa.gov/api/regulationsgov/")
    kind = p.add_mutually_exclusive_group()
    kind.add_argument("--docket", dest="kind", action="store_const", const="docket",
                      help="Treat TARGET as a docket ID (all comments in the docket)")
    kind.add_argument("--document", dest="kind", action="store_const", const="document",
                      help="Treat TARGET as a document ID (comments on that rule/notice only)")
    p.add_argument("-o", "--out", type=Path, default=Path("downloads"),
                   help="Output directory (default: ./downloads)")
    p.add_argument("--all-formats", action="store_true",
                   help="Download every available format of each attachment (e.g. both .docx "
                        "and .pdf) instead of just one")
    p.add_argument("--prefer", default=",".join(DEFAULT_FORMAT_PREFERENCE),
                   help="Comma-separated format preference when only one format is downloaded "
                        "(default: %(default)s)")
    p.add_argument("--limit", type=int, help="Stop after this many comments (useful for a test run)")
    p.add_argument("--workers", type=int, default=4,
                   help="Parallel attachment downloads (default: %(default)s)")
    p.add_argument("--delay", type=float, default=0.0,
                   help="Minimum seconds between API calls (default: 0; the tool waits "
                        "automatically when the hourly rate limit is hit)")
    p.add_argument("--overwrite", action="store_true",
                   help="Re-download comments already saved by a previous run")
    p.add_argument("--list-only", action="store_true",
                   help="Only list comment IDs into index.csv; download nothing")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    api_key = args.api_key
    if not api_key:
        api_key = "DEMO_KEY"
        print(
            f"warning: no API key given (--api-key or ${API_KEY_ENV}); using DEMO_KEY, which allows "
            "only a few dozen requests per hour. Get a free key at "
            "https://open.gsa.gov/api/regulationsgov/",
            file=sys.stderr,
        )

    client = RegulationsGovClient(api_key, min_interval=args.delay)
    options = Options(
        out_dir=args.out,
        all_formats=args.all_formats,
        format_preference=tuple(f.strip().lower().lstrip(".") for f in args.prefer.split(",") if f.strip()),
        workers=args.workers,
        limit=args.limit,
        overwrite=args.overwrite,
        list_only=args.list_only,
    )
    downloader = CommentDownloader(client, options)
    try:
        target = resolve_target(client, args.target, args.kind)
        if target.kind == "document":
            title = target.info.get("title") or ""
            print(f"Document {target.id} ({target.info.get('documentType', 'document')}): {title}",
                  file=sys.stderr)
        base = downloader.run(target)
    except KeyboardInterrupt:
        print("\nInterrupted. Run the same command again to resume where you left off.", file=sys.stderr)
        return 130
    except (NotFound, ApiError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    s = downloader.stats
    print(
        f"\nDone. {s.listed} comments listed, {s.processed} downloaded, {s.skipped} already present.\n"
        f"Files: {s.files_downloaded} downloaded, {s.files_failed} failed, "
        f"{s.files_restricted} restricted by the agency.\n"
        f"API requests used: {client.request_count}\n"
        f"Output: {base.resolve()}  (see index.csv)",
        file=sys.stderr,
    )
    if s.files_failed:
        print("Some files failed; re-run the same command to retry them.", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
