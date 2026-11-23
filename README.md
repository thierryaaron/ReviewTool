# ReviewTool

Reviews a manuscript before submission using the Claude API and writes a referee-style report in Markdown.

## Setup

```bash
pip install -r requirements.txt
```

Create an API key in the [Claude Console](https://platform.claude.com/) and make it available to the tool:

```bash
export ANTHROPIC_API_KEY="your key"
```

Word files are converted to PDF with LibreOffice, so install it if you review `.docx` files. Without it the tool sends the text only and the reviewers cannot see figures or page numbers.

## Use

```bash
python -m reviewtool manuscripts/paper.pdf
```

The report is written to `reviews/paper-review.md`. The raw findings from each reviewer are saved beside it as `reviews/paper-findings.json`. Both `manuscripts/` and `reviews/` are ignored by git, so drafts stay out of the repository.

Useful options:

| Option | Effect |
| --- | --- |
| `--notes "target journal: ..."` | Context passed to the reviewers |
| `--model claude-sonnet-5-5` | Use a cheaper model (default `claude-opus-5-5`) |
| `--effort xhigh` | Think harder, at higher cost (default `high`) |
| `--groups 8` | Split a long paper into more parts (default 5) |
| `--workers 1` | Run reviewers one at a time if you hit rate limits |
| `--no-web` | Skip the reviewer that searches for outside sources |
| `--no-code` | Audit the numbers without the code execution tool |
| `--no-fallback` | Do not retry on another model if the first declines a request |

## How it works

1. **Plan.** One call reads the manuscript, summarises it, lists the claims the argument depends on and splits the paper into section groups.
2. **Review.** Each section group gets a close reading by its own reviewer. A numbers reviewer redoes the paper's arithmetic with a code execution tool. An outside-sources reviewer searches the web for omitted literature, policy and standards. These run in parallel.
3. **Quote check.** Every passage a reviewer quoted is looked up in the manuscript text. Passages that cannot be found are flagged.
4. **Report.** A final call merges duplicate findings, drops those the manuscript does not bear out and writes the report.

Every call begins with the same system prompt and manuscript. The API stores that shared opening after the first call (prompt caching) and charges later calls about a twentieth of the normal input price to reuse it.

## Cost

The tool prints the tokens used and an estimated cost at the end of each run. Web searches and code execution are billed separately by the API and are left out of that estimate.

## Limits

- The report is a machine review. Check each finding against the manuscript before acting on it, and check each cited source before citing it.
- The manuscript is sent to Anthropic's API. Check your co-authors and any confidentiality terms allow that.
- PDFs over 32 MB or 600 pages are rejected by the API.

## Tests

```bash
python -m unittest discover -s tests
```

The tests use a stand-in for the API and need no key.
