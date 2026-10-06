"""Download the text and attached files of every comment on a rule or docket."""

from __future__ import annotations

import csv
import json
import os
import re
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

import requests

from .api import USER_AGENT, NotFound, RegulationsGovClient
from .textutil import html_to_text, safe_filename

COMMENT_URL = "https://www.regulations.gov/comment/{}"
DEFAULT_FORMAT_PREFERENCE = ("pdf", "docx", "doc", "rtf", "txt", "htm", "html", "xlsx", "xls", "pptx", "jpg", "png")
CSV_TEXT_LIMIT = 32000  # Excel cannot hold more than 32,767 characters in a cell

_URL_RE = re.compile(r"regulations\.gov/(document|docket|comment)/([A-Za-z0-9_.\-]+)", re.I)


@dataclass
class Target:
    kind: str  # "docket", "document" or "comment"
    id: str
    filter_key: str | None = None
    filter_value: str | None = None
    info: dict = field(default_factory=dict)


@dataclass
class Options:
    out_dir: Path = Path("downloads")
    all_formats: bool = False
    format_preference: tuple[str, ...] = DEFAULT_FORMAT_PREFERENCE
    workers: int = 4
    limit: int | None = None
    overwrite: bool = False
    list_only: bool = False


@dataclass
class Stats:
    listed: int = 0
    skipped: int = 0
    processed: int = 0
    files_downloaded: int = 0
    files_failed: int = 0
    files_restricted: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def add(self, **counts: int) -> None:
        with self.lock:
            for key, value in counts.items():
                setattr(self, key, getattr(self, key) + value)


# ------------------------------------------------------------------ targets


def parse_target(text: str) -> tuple[str | None, str]:
    """Split user input into ``(kind_or_None, id)``. Accepts IDs or regulations.gov URLs."""
    text = text.strip()
    match = _URL_RE.search(text)
    if match:
        return match.group(1).lower(), match.group(2)
    return None, text.rstrip("/").split("/")[-1]


def resolve_target(client: RegulationsGovClient, text: str, kind: str | None = None) -> Target:
    """Work out whether the input is a docket, a document (rule/notice) or a single comment."""
    url_kind, target_id = parse_target(text)
    kind = kind or url_kind

    if kind in (None, "document"):
        try:
            doc = client.get_document(target_id)
        except NotFound:
            if kind == "document":
                raise
        else:
            attrs = doc.get("attributes") or {}
            object_id = attrs.get("objectId")
            if not object_id:
                raise ValueError(f"Document {target_id} has no objectId; cannot look up its comments")
            return Target("document", doc.get("id", target_id), "commentOnId", object_id, attrs)

    if kind in (None, "docket"):
        try:
            docket = client.get_docket(target_id)
        except NotFound:
            if kind == "docket":
                raise
        else:
            return Target("docket", docket.get("id", target_id), "docketId", docket.get("id", target_id),
                          docket.get("attributes") or {})

    if kind in (None, "comment"):
        try:
            data, _ = client.get_comment(target_id)
        except NotFound:
            if kind == "comment":
                raise
        else:
            return Target("comment", data.get("id", target_id), info=data.get("attributes") or {})

    raise NotFound(
        f"'{target_id}' was not found on Regulations.gov as a document, docket or comment. "
        "Use an ID like EPA-HQ-OAR-2021-0317 (docket) or EPA-HQ-OAR-2021-0317-0001 (document)."
    )


# ------------------------------------------------------------ file planning


def _format_of(file_format: dict) -> str:
    fmt = (file_format.get("format") or "").lower().lstrip(".")
    if not fmt:
        path = urlparse(file_format.get("fileUrl") or "").path
        fmt = os.path.splitext(path)[1].lower().lstrip(".")
    return fmt or "bin"


def choose_formats(file_formats: list[dict] | None, all_formats: bool, preference) -> list[dict]:
    """Pick which renditions of a file to download.

    Regulations.gov often offers the same attachment in several formats (e.g.
    the original .docx plus a .pdf rendition). By default only the most
    preferred one is downloaded.
    """
    available = [f for f in file_formats or [] if f.get("fileUrl")]
    if all_formats or len(available) <= 1:
        return available
    order = {fmt: i for i, fmt in enumerate(preference)}
    return [min(available, key=lambda f: order.get(_format_of(f), len(order)))]


def plan_files(comment: dict, attachments: list[dict], options: Options) -> list[dict]:
    """Build the list of files to fetch for one comment."""
    planned: list[dict] = []
    used_names: set[str] = set()

    def add(source: str, title: str | None, order, base: str, file_formats, restrict_reason):
        chosen = choose_formats(file_formats, options.all_formats, options.format_preference)
        if not chosen:
            planned.append({
                "source": source, "title": title, "order": order,
                "status": "restricted" if restrict_reason else "unavailable",
                "restrict_reason": restrict_reason,
            })
            return
        for ff in chosen:
            ext = _format_of(ff)
            name = f"{base}.{ext}"
            n = 2
            while name.lower() in used_names:
                name = f"{base}_{n}.{ext}"
                n += 1
            used_names.add(name.lower())
            planned.append({
                "source": source, "title": title, "order": order, "format": ext,
                "url": ff["fileUrl"], "size": ff.get("size"), "filename": name, "status": "pending",
            })

    attrs = comment.get("attributes") or {}
    # Some submissions (e.g. scanned paper letters) carry the file on the comment itself.
    if attrs.get("fileFormats"):
        add("comment", attrs.get("title"), 0, "submission", attrs["fileFormats"], attrs.get("restrictReason"))

    def sort_key(att):
        order = (att.get("attributes") or {}).get("docOrder")
        return (order is None, order if order is not None else 0)

    for index, att in enumerate(sorted(attachments, key=sort_key), start=1):
        a = att.get("attributes") or {}
        order = a.get("docOrder") or index
        title = a.get("title")
        label = safe_filename(title, 60)
        base = f"attachment_{order}" + (f"_{label}" if label else "")
        add("attachment", title, order, base, a.get("fileFormats"), a.get("restrictReason"))
    return planned


# --------------------------------------------------------------- downloads


class FileFetcher:
    """Downloads files from downloads.regulations.gov (no API key or quota needed)."""

    def __init__(self, session_factory: Callable[[], requests.Session] = requests.Session,
                 retries: int = 4, timeout: float = 120.0, sleep: Callable[[float], None] = time.sleep):
        self._local = threading.local()
        self.session_factory = session_factory
        self.retries = retries
        self.timeout = timeout
        self.sleep = sleep

    def _session(self) -> requests.Session:
        if not hasattr(self._local, "session"):
            self._local.session = self.session_factory()
        return self._local.session

    def fetch(self, url: str, dest: Path) -> int:
        tmp = dest.with_name(dest.name + ".part")
        last_error: Exception | None = None
        for attempt in range(self.retries + 1):
            if attempt:
                self.sleep(min(2 ** attempt, 60))
            try:
                with self._session().get(url, stream=True, timeout=self.timeout,
                                         headers={"User-Agent": USER_AGENT}) as resp:
                    if resp.status_code == 404:
                        raise FileNotFoundError(f"HTTP 404 for {url}")
                    if resp.status_code != 200:
                        raise IOError(f"HTTP {resp.status_code} for {url}")
                    size = 0
                    with open(tmp, "wb") as fh:
                        for chunk in resp.iter_content(chunk_size=1 << 16):
                            if chunk:
                                fh.write(chunk)
                                size += len(chunk)
                os.replace(tmp, dest)
                return size
            except FileNotFoundError:
                tmp.unlink(missing_ok=True)
                raise
            except (requests.RequestException, IOError) as exc:
                last_error = exc
                tmp.unlink(missing_ok=True)
        raise IOError(f"giving up on {url}: {last_error}")


# ------------------------------------------------------------------- main


def build_metadata(comment: dict) -> dict:
    attrs = comment.get("attributes") or {}
    cid = comment.get("id")
    return {
        "id": cid,
        "url": COMMENT_URL.format(cid),
        "docket_id": attrs.get("docketId"),
        "comment_on_document_id": attrs.get("commentOnDocumentId"),
        "title": attrs.get("title"),
        "first_name": attrs.get("firstName"),
        "last_name": attrs.get("lastName"),
        "organization": attrs.get("organization"),
        "city": attrs.get("city"),
        "state": attrs.get("stateProvinceRegion"),
        "country": attrs.get("country"),
        "posted_date": attrs.get("postedDate"),
        "received_date": attrs.get("receiveDate"),
        "tracking_number": attrs.get("trackingNbr"),
        "withdrawn": attrs.get("withdrawn"),
        "restrict_reason": attrs.get("restrictReason"),
        "text_file": None,
        "files": [],
        "complete": False,
        "downloaded_at": None,
        "api_attributes": attrs,
    }


def is_complete(comment_dir: Path) -> bool:
    meta = comment_dir / "metadata.json"
    if not meta.exists():
        return False
    try:
        data = json.loads(meta.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if not data.get("complete"):
        return False
    return all((comment_dir / f["filename"]).exists()
               for f in data.get("files", []) if f.get("status") == "downloaded")


def _write_json(path: Path, data) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


class CommentDownloader:
    def __init__(self, client: RegulationsGovClient, options: Options,
                 fetcher: FileFetcher | None = None, log: Callable[[str], None] | None = None):
        self.client = client
        self.options = options
        self.fetcher = fetcher or FileFetcher()
        self.log = log or client.log
        self.stats = Stats()

    def save_comment(self, comment: dict, attachments: list[dict], comment_dir: Path) -> tuple[dict, list[dict]]:
        """Write the comment text now and return (metadata, files still to download)."""
        comment_dir.mkdir(parents=True, exist_ok=True)
        meta = build_metadata(comment)
        text = html_to_text((comment.get("attributes") or {}).get("comment"))
        if text:
            header = [
                f"Comment ID: {meta['id']}",
                f"URL: {meta['url']}",
            ]
            submitter = " ".join(p for p in (meta["first_name"], meta["last_name"]) if p)
            if submitter:
                header.append(f"Submitter: {submitter}")
            if meta["organization"]:
                header.append(f"Organization: {meta['organization']}")
            if meta["posted_date"]:
                header.append(f"Posted: {meta['posted_date']}")
            (comment_dir / "comment.txt").write_text(
                "\n".join(header) + "\n\n" + text + "\n", encoding="utf-8")
            meta["text_file"] = "comment.txt"
        meta["comment_text"] = text
        meta["files"] = plan_files(comment, attachments, self.options)
        return meta, [f for f in meta["files"] if f["status"] == "pending"]

    def finish_comment(self, meta: dict, pending: list[dict], comment_dir: Path) -> dict:
        for f in pending:
            try:
                size = self.fetcher.fetch(f["url"], comment_dir / f["filename"])
                f["status"] = "downloaded"
                f["bytes"] = size
                self.stats.add(files_downloaded=1)
            except Exception as exc:  # noqa: BLE001 - record and continue
                f["status"] = "failed"
                f["error"] = str(exc)
                self.stats.add(files_failed=1)
                self.log(f"  ! {meta['id']}: could not download {f['url']}: {exc}")
        restricted = sum(1 for f in meta["files"] if f["status"] == "restricted")
        if restricted:
            self.stats.add(files_restricted=restricted)
        failed = any(f["status"] == "failed" for f in meta["files"])
        meta["complete"] = not failed
        meta["downloaded_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        _write_json(comment_dir / "metadata.json", meta)
        self.stats.add(processed=1)
        return meta

    def run(self, target: Target) -> Path:
        opts = self.options
        base = opts.out_dir / safe_filename(target.id)
        comments_dir = base / "comments"
        comments_dir.mkdir(parents=True, exist_ok=True)
        _write_json(base / "source.json", {
            "kind": target.kind, "id": target.id, "filter": {target.filter_key: target.filter_value},
            "attributes": target.info,
        })

        if target.kind == "comment":
            summaries = iter([{"id": target.id}])
            total = 1
        else:
            total = self.client.count_comments(target.filter_key, target.filter_value)
            summaries = self.client.iter_comments(target.filter_key, target.filter_value)
            self.log(f"{target.kind.title()} {target.id}: {total if total is not None else '?'} posted comments")
        if opts.limit is not None and total is not None:
            total = min(total, opts.limit)

        futures: list[Future] = []
        listed: dict[str, dict] = {}
        pool = ThreadPoolExecutor(max_workers=max(1, opts.workers))
        try:
            for summary in summaries:
                if opts.limit is not None and self.stats.listed >= opts.limit:
                    break
                cid = summary["id"]
                self.stats.add(listed=1)
                listed[cid] = summary.get("attributes") or {}
                if opts.list_only:
                    continue
                comment_dir = comments_dir / safe_filename(cid)
                if not opts.overwrite and is_complete(comment_dir):
                    self.stats.add(skipped=1)
                    continue
                comment, attachments = self.client.get_comment(cid)
                meta, pending = self.save_comment(comment, attachments, comment_dir)
                self.log(
                    f"[{self.stats.listed}/{total if total is not None else '?'}] {cid}: "
                    f"{'text' if meta['text_file'] else 'no text'}, {len(pending)} file(s) to download"
                )
                futures.append(pool.submit(self.finish_comment, meta, pending, comment_dir))
                futures = [f for f in futures if not f.done() or f.exception()]
            for fut in futures:
                fut.result()
        except BaseException:
            # Ctrl-C or a fatal error: drop queued work, let running downloads finish.
            pool.shutdown(wait=True, cancel_futures=True)
            raise
        finally:
            pool.shutdown(wait=True)
            write_index(base, comments_dir, listed if opts.list_only else None)
        return base


INDEX_COLUMNS = [
    "comment_id", "url", "posted_date", "received_date", "title", "first_name", "last_name",
    "organization", "city", "state", "country", "tracking_number", "comment_on_document_id",
    "text_file", "files_downloaded", "files_failed", "files_restricted", "file_paths", "comment_text",
]


def write_index(base: Path, comments_dir: Path, listed: dict[str, dict] | None = None) -> Path:
    """Rebuild index.csv from every comment's metadata.json."""
    rows = []
    for meta_path in sorted(comments_dir.glob("*/metadata.json")):
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        folder = meta_path.parent.name
        files = meta.get("files", [])
        downloaded = [f for f in files if f.get("status") == "downloaded"]
        text = meta.get("comment_text") or ""
        if len(text) > CSV_TEXT_LIMIT:
            text = text[:CSV_TEXT_LIMIT] + " [truncated; see text_file]"
        rows.append({
            "comment_id": meta.get("id"),
            "url": meta.get("url"),
            "posted_date": meta.get("posted_date"),
            "received_date": meta.get("received_date"),
            "title": meta.get("title"),
            "first_name": meta.get("first_name"),
            "last_name": meta.get("last_name"),
            "organization": meta.get("organization"),
            "city": meta.get("city"),
            "state": meta.get("state"),
            "country": meta.get("country"),
            "tracking_number": meta.get("tracking_number"),
            "comment_on_document_id": meta.get("comment_on_document_id"),
            "text_file": f"comments/{folder}/{meta['text_file']}" if meta.get("text_file") else "",
            "files_downloaded": len(downloaded),
            "files_failed": sum(1 for f in files if f.get("status") == "failed"),
            "files_restricted": sum(1 for f in files if f.get("status") == "restricted"),
            "file_paths": "; ".join(f"comments/{folder}/{f['filename']}" for f in downloaded),
            "comment_text": text,
        })
    known = {r["comment_id"] for r in rows}
    for cid, attrs in (listed or {}).items():
        if cid not in known:
            rows.append({"comment_id": cid, "url": COMMENT_URL.format(cid),
                         "title": attrs.get("title"), "posted_date": attrs.get("postedDate")})
    index = base / "index.csv"
    with open(index, "w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(fh, fieldnames=INDEX_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
    return index
