import csv
import json

import pytest

from regcomments.api import ApiError, NotFound, RegulationsGovClient
from regcomments.cli import main
from regcomments.downloader import (
    CommentDownloader,
    Options,
    choose_formats,
    parse_target,
    plan_files,
    resolve_target,
)
from regcomments.textutil import (
    _to_us_eastern_fallback,
    html_to_text,
    parse_api_datetime,
    safe_filename,
    to_eastern_filter_value,
)


# --------------------------------------------------------------- fakes


class FakeResponse:
    def __init__(self, status=200, payload=None, headers=None, content=b""):
        self.status_code = status
        self._payload = payload
        self.headers = headers or {}
        self.content = content
        self.text = json.dumps(payload) if payload is not None else ""

    def json(self):
        return self._payload

    def iter_content(self, chunk_size=1):
        for i in range(0, len(self.content), chunk_size):
            yield self.content[i:i + chunk_size]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeApi:
    """Mimics the subset of the Regulations.gov v4 API used by the tool."""

    def __init__(self, documents=None, dockets=None, comments=None, attachments=None, page_cap=20):
        self.documents = documents or {}
        self.dockets = dockets or {}
        self.comments = comments or {}  # id -> attributes
        self.attachments = attachments or {}  # comment id -> list of attachment dicts
        self.page_cap = page_cap
        self.calls = []
        self.queued = []  # responses to return before normal handling

    def get(self, url, params=None, headers=None, timeout=None, **kw):
        self.calls.append((url, dict(params or {})))
        if self.queued:
            return self.queued.pop(0)
        path = url.split("/v4", 1)[1]
        if path.startswith("/documents/"):
            doc_id = path.split("/")[-1]
            if doc_id not in self.documents:
                return FakeResponse(404, {"errors": []})
            return FakeResponse(200, {"data": {"id": doc_id, "type": "documents",
                                               "attributes": self.documents[doc_id]}})
        if path.startswith("/dockets/"):
            dk = path.split("/")[-1]
            if dk not in self.dockets:
                return FakeResponse(404, {"errors": []})
            return FakeResponse(200, {"data": {"id": dk, "type": "dockets", "attributes": self.dockets[dk]}})
        if path.startswith("/comments/"):
            cid = path.split("/")[-1]
            if cid not in self.comments:
                return FakeResponse(404, {"errors": []})
            atts = self.attachments.get(cid, [])
            return FakeResponse(200, {
                "data": {"id": cid, "type": "comments", "attributes": self.comments[cid],
                         "relationships": {"attachments": {"data": [{"id": a["id"], "type": "attachments"}
                                                                    for a in atts]}}},
                "included": [{"type": "attachments", **a} for a in atts],
            })
        if path == "/comments":
            return self._list(params)
        raise AssertionError(f"unexpected path {path}")

    def _list(self, params):
        items = []
        for cid, attrs in self.comments.items():
            if "filter[docketId]" in params and attrs.get("docketId") != params["filter[docketId]"]:
                continue
            if "filter[commentOnId]" in params and attrs.get("commentOnId") != params["filter[commentOnId]"]:
                continue
            items.append((attrs["lastModifiedDate"], cid, attrs))
        items.sort()
        ge = params.get("filter[lastModifiedDate][ge]")
        if ge:
            items = [i for i in items if to_eastern_filter_value(i[0]) >= ge]
        size = int(params["page[size]"])
        page = int(params["page[number]"])
        total = len(items)
        capped = items[: size * self.page_cap]
        chunk = capped[(page - 1) * size: page * size]
        total_pages = (len(capped) + size - 1) // size
        return FakeResponse(200, {
            "data": [{"id": cid, "type": "comments",
                      "attributes": {"lastModifiedDate": lm, "title": a.get("title"),
                                     "postedDate": a.get("postedDate")}}
                     for lm, cid, a in chunk],
            "meta": {"totalElements": total, "pageNumber": page, "hasNextPage": page < total_pages},
        }, headers={"X-RateLimit-Remaining": "999"})


class FakeFiles:
    def __init__(self, missing=()):
        self.fetched = []
        self.missing = set(missing)

    def get(self, url, stream=False, timeout=None, headers=None):
        self.fetched.append(url)
        if url in self.missing:
            return FakeResponse(404)
        return FakeResponse(200, content=f"FILE {url}".encode())


def make_client(api, **kw):
    return RegulationsGovClient("KEY", session=api, sleep=lambda s: None, log=lambda m: None, **kw)


def make_fetcher(files):
    from regcomments.downloader import FileFetcher

    return FileFetcher(session_factory=lambda: files, sleep=lambda s: None)


def ff(url, fmt, size=10):
    return {"fileUrl": url, "format": fmt, "size": size}


# --------------------------------------------------------------- textutil


def test_html_to_text():
    raw = "<p>Dear Agency,</p><p>I&#39;m opposed &amp; here&rsquo;s why:<br/>Reason one</p><ul><li>a</li><li>b</li></ul>"
    text = html_to_text(raw)
    assert text.startswith("Dear Agency,")
    assert "I'm opposed & here’s why:\nReason one" in text
    assert "- a" in text and "- b" in text
    assert html_to_text(None) == ""
    assert html_to_text("See attached file(s)") == "See attached file(s)"


def test_safe_filename():
    assert safe_filename('Comments: "Rule" re/ACA?') == "Comments_ _Rule_ re_ACA"
    assert safe_filename("CON") == "_CON"
    assert len(safe_filename("x" * 300)) == 80
    assert safe_filename(None) == ""


def test_eastern_conversion_matches_api_docs_example():
    # Example from the Regulations.gov API docs: 15:58:52Z -> 11:58:52 Eastern (EDT).
    assert to_eastern_filter_value("2020-08-10T15:58:52Z") == "2020-08-10 11:58:52"
    assert to_eastern_filter_value("2021-01-15T17:00:00Z") == "2021-01-15 12:00:00"


@pytest.mark.parametrize("stamp", [
    "2020-08-10T15:58:52Z", "2021-01-15T17:00:00Z", "2023-03-12T06:59:59Z",
    "2023-03-12T07:00:00Z", "2023-11-05T05:59:59Z", "2023-11-05T06:00:00Z",
])
def test_eastern_fallback_agrees_with_zoneinfo(stamp):
    from zoneinfo import ZoneInfo

    dt = parse_api_datetime(stamp)
    expected = dt.astimezone(ZoneInfo("America/New_York")).replace(tzinfo=None)
    assert _to_us_eastern_fallback(dt) == expected


# --------------------------------------------------------------- targets


def test_parse_target():
    assert parse_target("https://www.regulations.gov/document/EPA-HQ-OAR-2021-0317-0001") == \
        ("document", "EPA-HQ-OAR-2021-0317-0001")
    assert parse_target("https://www.regulations.gov/docket/EPA-HQ-OAR-2021-0317/comments") == \
        ("docket", "EPA-HQ-OAR-2021-0317")
    assert parse_target("regulations.gov/comment/FDA-2021-N-0471-0042") == ("comment", "FDA-2021-N-0471-0042")
    assert parse_target(" CFPB-2023-0038-0001 ") == (None, "CFPB-2023-0038-0001")


def test_resolve_target_document_docket_comment():
    api = FakeApi(
        documents={"AG-2024-0001-0001": {"objectId": "09000abc", "title": "Proposed Rule"}},
        dockets={"AG-2024-0001": {"title": "Docket"}},
        comments={"AG-2024-0001-0005": {"docketId": "AG-2024-0001", "lastModifiedDate": "2024-01-01T00:00:00Z"}},
    )
    client = make_client(api)
    t = resolve_target(client, "AG-2024-0001-0001")
    assert (t.kind, t.filter_key, t.filter_value) == ("document", "commentOnId", "09000abc")
    t = resolve_target(client, "AG-2024-0001")
    assert (t.kind, t.filter_key, t.filter_value) == ("docket", "docketId", "AG-2024-0001")
    t = resolve_target(client, "AG-2024-0001-0005")
    assert t.kind == "comment"
    with pytest.raises(NotFound):
        resolve_target(client, "NOPE-0000")
    with pytest.raises(NotFound):
        resolve_target(client, "AG-2024-0001", kind="document")


# --------------------------------------------------------------- api client


def _many_comments(n, docket="D-1", same_time_every=1):
    comments = {}
    for i in range(n):
        # Several comments share each timestamp to exercise boundary de-duplication.
        sec = i // same_time_every
        comments[f"{docket}-{i:05d}"] = {
            "docketId": docket,
            "lastModifiedDate": f"2024-06-01T{12 + sec // 3600:02d}:{(sec // 60) % 60:02d}:{sec % 60:02d}Z",
        }
    return comments


def test_iter_comments_pages_past_result_cap():
    api = FakeApi(comments=_many_comments(53, same_time_every=3), page_cap=4)
    client = make_client(api)
    client.page_size, client.max_pages = 5, 4  # 20 results per query window
    ids = [c["id"] for c in client.iter_comments("docketId", "D-1")]
    assert ids == sorted(_many_comments(53).keys())
    assert len(ids) == len(set(ids))
    windows = {p.get("filter[lastModifiedDate][ge]") for _, p in api.calls}
    assert len(windows) > 2  # needed several lastModifiedDate windows


def test_iter_comments_small_docket_single_request():
    api = FakeApi(comments=_many_comments(7))
    client = make_client(api)
    assert len(list(client.iter_comments("docketId", "D-1"))) == 7
    assert len(api.calls) == 1


def test_rate_limit_is_waited_out():
    api = FakeApi(dockets={"D-1": {}})
    api.queued = [FakeResponse(429, {"error": "OVER_RATE_LIMIT"}), FakeResponse(429, {}),
                  FakeResponse(503, {})]
    waits = []
    client = RegulationsGovClient("KEY", session=api, sleep=waits.append, log=lambda m: None)
    assert client.get_docket("D-1")["id"] == "D-1"
    assert waits == [60.0, 120.0, 2]


def test_bad_api_key():
    api = FakeApi()
    api.queued = [FakeResponse(403, {"error": {"code": "API_KEY_INVALID"}})]
    with pytest.raises(ApiError, match="API key"):
        make_client(api).get_docket("D-1")


# --------------------------------------------------------------- file planning


def test_choose_formats_prefers_pdf_unless_all():
    formats = [ff("u/attachment_1.docx", "docx"), ff("u/attachment_1.pdf", "pdf")]
    assert [f["format"] for f in choose_formats(formats, False, ("pdf", "docx"))] == ["pdf"]
    assert [f["format"] for f in choose_formats(formats, False, ("docx", "pdf"))] == ["docx"]
    assert len(choose_formats(formats, True, ("pdf",))) == 2
    assert choose_formats(None, False, ("pdf",)) == []


def test_plan_files_names_and_restricted():
    comment = {"id": "C-1", "attributes": {"fileFormats": [ff("https://d/C-1/content.pdf", "pdf")]}}
    atts = [
        {"id": "a2", "attributes": {"docOrder": 2, "title": "Exhibit: A/B",
                                    "fileFormats": [ff("https://d/C-1/attachment_2.xlsx", "xlsx")]}},
        {"id": "a1", "attributes": {"docOrder": 1, "title": "Comment Letter",
                                    "fileFormats": [ff("https://d/C-1/attachment_1.pdf", "pdf"),
                                                    ff("https://d/C-1/attachment_1.docx", "docx")]}},
        {"id": "a3", "attributes": {"docOrder": 3, "title": "CBI", "fileFormats": None,
                                    "restrictReason": "Confidential Business Information"}},
    ]
    files = plan_files(comment, atts, Options())
    assert [f.get("filename") for f in files] == [
        "submission.pdf", "attachment_1_Comment Letter.pdf", "attachment_2_Exhibit_ A_B.xlsx", None]
    assert files[-1]["status"] == "restricted"


# --------------------------------------------------------------- end to end


def _docket_fixture():
    comments = {
        "D-1-0002": {"docketId": "D-1", "commentOnId": "obj1", "lastModifiedDate": "2024-02-01T10:00:00Z",
                     "comment": "<p>I support this rule.</p><p>Thanks!</p>", "firstName": "Pat",
                     "lastName": "Doe", "postedDate": "2024-02-01T05:00:00Z", "title": "Comment from Pat Doe"},
        "D-1-0003": {"docketId": "D-1", "commentOnId": "obj1", "lastModifiedDate": "2024-02-02T10:00:00Z",
                     "comment": "See attached file(s)", "organization": "Acme Corp",
                     "title": "Comment from Acme Corp"},
        "D-1-0004": {"docketId": "D-1", "commentOnId": "obj1", "lastModifiedDate": "2024-02-03T10:00:00Z",
                     "comment": "", "title": "Mass mail"},
    }
    attachments = {
        "D-1-0003": [
            {"id": "x1", "attributes": {"docOrder": 1, "title": "Acme letter",
                                        "fileFormats": [ff("https://downloads.regulations.gov/D-1-0003/attachment_1.pdf", "pdf"),
                                                        ff("https://downloads.regulations.gov/D-1-0003/attachment_1.docx", "docx")]}},
            {"id": "x2", "attributes": {"docOrder": 2, "title": "Appendix",
                                        "fileFormats": [ff("https://downloads.regulations.gov/D-1-0003/attachment_2.pdf", "pdf")]}},
        ],
        "D-1-0004": [
            {"id": "x3", "attributes": {"docOrder": 1, "title": "letter",
                                        "fileFormats": [ff("https://downloads.regulations.gov/D-1-0004/attachment_1.pdf", "pdf")]}},
        ],
    }
    return FakeApi(documents={"D-1-0001": {"objectId": "obj1", "title": "Proposed Rule"}},
                   dockets={"D-1": {}}, comments=comments, attachments=attachments)


def test_end_to_end_download_and_resume(tmp_path):
    api = _docket_fixture()
    files = FakeFiles()
    client = make_client(api)
    dl = CommentDownloader(client, Options(out_dir=tmp_path, workers=2), fetcher=make_fetcher(files))
    base = dl.run(resolve_target(client, "https://www.regulations.gov/document/D-1-0001"))

    assert base == tmp_path / "D-1-0001"
    c2 = base / "comments" / "D-1-0002"
    text = (c2 / "comment.txt").read_text(encoding="utf-8")
    assert "Submitter: Pat Doe" in text and "I support this rule.\n\nThanks!" in text
    assert not (base / "comments" / "D-1-0004" / "comment.txt").exists()

    c3 = base / "comments" / "D-1-0003"
    assert (c3 / "attachment_1_Acme letter.pdf").read_bytes().startswith(b"FILE https://downloads")
    assert (c3 / "attachment_2_Appendix.pdf").exists()
    assert not (c3 / "attachment_1_Acme letter.docx").exists()  # one format by default
    meta = json.loads((c3 / "metadata.json").read_text(encoding="utf-8"))
    assert meta["complete"] and meta["organization"] == "Acme Corp"
    assert dl.stats.files_downloaded == 3 and dl.stats.processed == 3

    with open(base / "index.csv", encoding="utf-8-sig", newline="") as fh:
        rows = {r["comment_id"]: r for r in csv.DictReader(fh)}
    assert set(rows) == {"D-1-0002", "D-1-0003", "D-1-0004"}
    assert rows["D-1-0003"]["files_downloaded"] == "2"
    assert rows["D-1-0002"]["comment_text"].startswith("I support")

    # Second run: nothing re-fetched, only the listing call is made.
    api.calls.clear()
    files.fetched.clear()
    dl2 = CommentDownloader(client, Options(out_dir=tmp_path), fetcher=make_fetcher(files))
    dl2.run(resolve_target(client, "D-1-0001"))
    assert dl2.stats.skipped == 3 and files.fetched == []
    assert not any("/comments/" in url for url, _ in api.calls)


def test_failed_download_is_retried_on_next_run(tmp_path):
    api = _docket_fixture()
    bad = "https://downloads.regulations.gov/D-1-0004/attachment_1.pdf"
    client = make_client(api)
    dl = CommentDownloader(client, Options(out_dir=tmp_path), fetcher=make_fetcher(FakeFiles(missing={bad})))
    dl.run(resolve_target(client, "D-1", kind="docket"))
    assert dl.stats.files_failed == 1
    meta = json.loads((tmp_path / "D-1" / "comments" / "D-1-0004" / "metadata.json").read_text())
    assert meta["complete"] is False

    files = FakeFiles()
    dl2 = CommentDownloader(client, Options(out_dir=tmp_path), fetcher=make_fetcher(files))
    dl2.run(resolve_target(client, "D-1", kind="docket"))
    assert dl2.stats.skipped == 2 and files.fetched == [bad]


def test_limit_and_list_only(tmp_path):
    api = _docket_fixture()
    client = make_client(api)
    dl = CommentDownloader(client, Options(out_dir=tmp_path, list_only=True), fetcher=make_fetcher(FakeFiles()))
    base = dl.run(resolve_target(client, "D-1"))
    with open(base / "index.csv", encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.DictReader(fh))
    assert [r["comment_id"] for r in rows] == ["D-1-0002", "D-1-0003", "D-1-0004"]
    assert not any((base / "comments").iterdir())

    dl = CommentDownloader(client, Options(out_dir=tmp_path / "lim", limit=1), fetcher=make_fetcher(FakeFiles()))
    dl.run(resolve_target(client, "D-1"))
    assert dl.stats.processed == 1


def test_cli_reports_not_found(monkeypatch, capsys, tmp_path):
    api = FakeApi()
    monkeypatch.setattr("regcomments.api.requests.Session", lambda: api)
    assert main(["NOPE-1", "--api-key", "K", "-o", str(tmp_path)]) == 1
    assert "not found" in capsys.readouterr().err
