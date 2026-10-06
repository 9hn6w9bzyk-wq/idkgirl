# regcomments: bulk-download public comments from Regulations.gov

The Regulations.gov website and API export comments as a spreadsheet of metadata.
`regcomments` downloads what each commenter actually submitted:

- **The comment text** typed into the Regulations.gov comment box, saved as `comment.txt`.
- **The comment letters and other attachments** the commenter uploaded (PDF, Word, Excel, images, …),
  saved under their original format.
- **Scanned or mailed-in submissions** where the agency attached the file to the comment itself.

It works for any proposed rule, request for comment, notice or whole docket on Regulations.gov.

## Install

You need Python 3.9 or newer.

```bash
git clone https://github.com/9hn6w9bzyk-wq/idkgirl.git
cd idkgirl
pip install .
```

## Get a free API key

Sign up at <https://open.gsa.gov/api/regulationsgov/> (it takes a minute and the key arrives by email).
Then either pass it with `--api-key` or set it once:

```bash
export REGULATIONS_GOV_API_KEY=your-key-here        # macOS / Linux
setx REGULATIONS_GOV_API_KEY your-key-here          # Windows (open a new terminal afterwards)
```

Without a key the tool falls back to the shared `DEMO_KEY`, which only allows a few dozen requests per
hour. That is enough for a quick test, not for a real docket.

## Usage

Pass the document ID of the proposed rule or request for comment, a docket ID, or just paste the
Regulations.gov URL:

```bash
# Comments on one proposed rule / RFI (the ID shown on the document's Regulations.gov page)
regcomments EPA-HQ-OAR-2021-0317-0001

# Every comment in a docket (covers all documents in the docket)
regcomments EPA-HQ-OAR-2021-0317

# A Regulations.gov URL works too
regcomments https://www.regulations.gov/document/FDA-2021-N-0471-0001

# Try it on the first 25 comments first
regcomments CFPB-2023-0038-0001 --limit 25
```

Options:

| Option | What it does |
| --- | --- |
| `-o DIR`, `--out DIR` | Where to save files (default `./downloads`). |
| `--all-formats` | Many attachments are offered in more than one format (for example the original `.docx` plus a `.pdf` copy). By default one is downloaded, PDF first; this flag downloads every format. |
| `--prefer docx,pdf,...` | Change which format wins when only one is downloaded. |
| `--limit N` | Stop after N comments. |
| `--docket` / `--document` | Force how the ID is interpreted. It is detected automatically otherwise. |
| `--workers N` | Number of parallel file downloads (default 4). |
| `--delay SECONDS` | Wait at least this long between API calls. |
| `--list-only` | Only build `index.csv` of comment IDs; download nothing. |
| `--overwrite` | Re-download comments already saved by a previous run. |

You can also run it without installing: `python -m regcomments ...` from the repository folder.

## Output

```
downloads/EPA-HQ-OAR-2021-0317-0001/
├── index.csv                  # one row per comment: submitter, organization, dates, text, file paths
├── source.json                # details of the rule/docket that was downloaded
└── comments/
    ├── EPA-HQ-OAR-2021-0317-0123/
    │   ├── comment.txt                         # text typed into the comment form
    │   ├── attachment_1_Comment Letter.pdf     # the uploaded comment letter
    │   ├── attachment_2_Technical Appendix.xlsx
    │   └── metadata.json                       # all API fields + status of each file
    └── EPA-HQ-OAR-2021-0317-0124/
        └── ...
```

`index.csv` opens in Excel and includes the full comment text (cut at 32,000 characters, Excel's
per-cell limit; the `.txt` file always has the full text), the submitter's name and organization, and
relative paths to every downloaded file.

## Large dockets, rate limits and resuming

- The API needs one request per comment to get its text and attachment links. A standard API key
  allows about 1,000 requests per hour, so a docket with 10,000 comments takes roughly 10 hours. When
  the limit is reached the tool pauses and continues on its own.
- Attachment downloads come from `downloads.regulations.gov` and do not count against the API limit.
- **Stopping and resuming is safe.** Press Ctrl-C at any time and run the same command again: comments
  already saved are skipped without using any API requests, and files that failed are retried.
- Dockets with more than 5,000 comments are handled automatically. The API caps each query at 5,000
  results, so the tool re-queries by modification date to get the rest.

## Limitations

- Only comments the agency has **posted** publicly are available. Comments still under review or not
  posted are not returned by the API.
- Agencies sometimes withhold attachments (for example confidential business information or
  copyrighted material). These are listed in `metadata.json` with status `restricted` and the
  agency's reason, and counted in `index.csv`.
- For mass-mail campaigns, agencies often post one representative comment and report a count of
  duplicates. Only the posted copies can be downloaded.

## Development

```bash
pip install -e '.[test]'
pytest
```
