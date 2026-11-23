"""Command line entry point: python -m reviewtool manuscript.pdf"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import anthropic

from .extract import ExtractError, load
from .pipeline import PRICES, Options, Reviewer, ReviewError

NO_KEY = "error: no valid API key. Set ANTHROPIC_API_KEY to a key from the Claude Console."


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="reviewtool",
        description="Review a manuscript before submission using the Claude API.",
    )
    parser.add_argument("manuscript", type=Path, help="PDF, Word, Markdown, LaTeX or text file")
    parser.add_argument("-o", "--out", type=Path, default=Path("reviews"), help="output folder (default: reviews)")
    parser.add_argument("--model", default="claude-opus-5-5", help="model ID (default: claude-opus-5-5)")
    parser.add_argument("--effort", default="high", choices=["low", "medium", "high", "xhigh", "max"],
                        help="how hard the model thinks (default: high)")
    parser.add_argument("--groups", type=int, default=5, help="maximum number of section groups (default: 5)")
    parser.add_argument("--workers", type=int, default=3, help="reviewers running at once (default: 3)")
    parser.add_argument("--notes", default="", help='context for the reviewers, e.g. "target journal: Nature Climate Change"')
    parser.add_argument("--style", type=Path, default=Path(__file__).with_name("style.md"),
                        help="style guide for the writing reviewers (default: reviewtool/style.md)")
    parser.add_argument("--no-writing", action="store_true", help="skip the structure and prose reviewers")
    parser.add_argument("--no-web", action="store_true", help="skip the web search reviewer")
    parser.add_argument("--no-code", action="store_true", help="audit the numbers without the code execution tool")
    parser.add_argument("--no-fallback", action="store_true",
                        help="do not retry on another model if the first one declines a request")
    args = parser.parse_args(argv)

    if not args.manuscript.is_file():
        print(f"error: {args.manuscript} is not a file.", file=sys.stderr)
        return 2
    style = ""
    if not args.no_writing:
        if not args.style.is_file():
            print(f"error: style guide {args.style} is not a file.", file=sys.stderr)
            return 2
        style = args.style.read_text(encoding="utf-8")
    args.out.mkdir(parents=True, exist_ok=True)
    stem = args.manuscript.stem

    try:
        manuscript = load(args.manuscript, args.out / "converted")
    except ExtractError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if not manuscript.has_pages:
        print("note: the manuscript is being sent as plain text, so the reviewers cannot see "
              "figures or page numbers. Use a PDF for a fuller review.", file=sys.stderr)

    options = Options(
        model=args.model, effort=args.effort, max_groups=args.groups, workers=args.workers,
        web=not args.no_web, code=not args.no_code, fallback=not args.no_fallback, writing=not args.no_writing, style=style, notes=args.notes,
    )
    # The SDK retries rate limits and server errors itself. A new API account has low
    # rate limits, so allow more retries than the default of two.
    client = anthropic.Anthropic(max_retries=6)

    try:
        result = Reviewer(client, manuscript, options).run()
    except anthropic.AuthenticationError:
        print(NO_KEY, file=sys.stderr)
        return 1
    except TypeError as exc:
        # The SDK raises a plain TypeError when it finds no credentials at all.
        if "authentication" not in str(exc).lower():
            raise
        print(NO_KEY, file=sys.stderr)
        return 1
    except anthropic.PermissionDeniedError as exc:
        print(f"error: the API key may not use this model or feature: {exc.message}", file=sys.stderr)
        return 1
    except anthropic.NotFoundError:
        print(f"error: the model '{args.model}' was not found.", file=sys.stderr)
        return 1
    except anthropic.RateLimitError:
        print("error: rate limit reached after several retries. Wait a minute, or run with --workers 1.",
              file=sys.stderr)
        return 1
    except anthropic.BadRequestError as exc:
        print(f"error: the API rejected the request: {exc.message}", file=sys.stderr)
        return 1
    except anthropic.APIStatusError as exc:
        print(f"error: API error {exc.status_code}: {exc.message}", file=sys.stderr)
        return 1
    except anthropic.APIConnectionError:
        print("error: could not reach the API. Check the network connection.", file=sys.stderr)
        return 1
    except ReviewError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    report_path = args.out / f"{stem}-review.md"
    findings_path = args.out / f"{stem}-findings.json"
    report_path.write_text(result.report, encoding="utf-8")
    findings_path.write_text(
        json.dumps({"plan": result.plan, "findings": result.findings}, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    usage = result.usage
    print(f"\nReport:   {report_path}", file=sys.stderr)
    print(f"Findings: {findings_path}", file=sys.stderr)
    print(f"Tokens:   {usage.input:,} input, {usage.cache_read:,} read from cache, "
          f"{usage.cache_write:,} written to cache, {usage.output:,} output, over {usage.calls} calls",
          file=sys.stderr)
    cost = usage.cost(args.model)
    if cost is None:
        print(f"Cost:     no price on file for {args.model} (known: {', '.join(PRICES)})", file=sys.stderr)
    else:
        print(f"Cost:     about ${cost:.2f} in tokens, before any web search or code execution charges",
              file=sys.stderr)
    if result.failed_passes:
        print(f"warning: these reviewers failed: {', '.join(result.failed_passes)}", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
