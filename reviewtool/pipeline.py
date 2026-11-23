"""The review pipeline.

1. Map. One call reads the manuscript, summarises it and splits it into section groups.
2. Review. Each section group gets its own close reading. Two more reviewers run beside
   them: one audits the numbers with a code execution tool, the other checks the paper
   against outside sources with web search. Two writing reviewers assess the structure
   and the prose. These calls run in parallel.
3. Check. Every quoted passage is looked up in the manuscript text.
4. Synthesis. One call merges the findings, drops what does not hold up and writes the report.

Every call starts with the same system prompt and manuscript, so after the first call the
manuscript is read from the prompt cache at a fraction of the normal input price. The cache
is keyed on everything that precedes the manuscript in the request, and that includes the
tool list and any output schema. For that reason the reviewers are asked for JSON in the
prompt, with no output schema, and only the two tool-using reviewers pay for their own copy.
"""

from __future__ import annotations

import json
import re
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

import anthropic

from . import prompts
from .extract import Manuscript, QuoteChecker

# US dollars per million tokens: input, output, cache read. Used for the cost estimate only.
PRICES = {
    "claude-fable-5-2": (8.00, 40.00, 0.20),
    "claude-opus-5-5": (4.00, 20.00, 0.20),
    "claude-sonnet-5-5": (2.00, 10.00, 0.10),
    "claude-haiku-5-5": (0.10, 0.50, 0.01),
}
CACHE_WRITE_1H_MULTIPLIER = 2.0

# Models that can decline a request through a safety classifier and accept a server-side fallback.
FALLBACK_MODELS = {"claude-fable-5-2", "claude-fable-5-1", "claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5"}
FALLBACK_BETA = "server-side-fallback-2026-07-01"

WEB_SEARCH_TOOL = {"type": "web_search_20260209", "name": "web_search", "max_uses": 15}
CODE_EXECUTION_TOOL = {"type": "code_execution_20260521", "name": "code_execution"}

MAX_PAUSE_RESUMES = 8


class ReviewError(Exception):
    pass


@dataclass
class Options:
    model: str = "claude-opus-5-5"
    effort: str = "high"
    max_groups: int = 5
    workers: int = 3
    web: bool = True
    code: bool = True
    fallback: bool = True
    writing: bool = True
    style: str = ""
    notes: str = ""


@dataclass
class Usage:
    input: int = 0
    output: int = 0
    cache_read: int = 0
    cache_write: int = 0
    calls: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def add(self, usage) -> None:
        with self._lock:
            self.calls += 1
            self.input += usage.input_tokens or 0
            self.output += usage.output_tokens or 0
            self.cache_read += getattr(usage, "cache_read_input_tokens", 0) or 0
            self.cache_write += getattr(usage, "cache_creation_input_tokens", 0) or 0

    def cost(self, model: str) -> float | None:
        if model not in PRICES:
            return None
        p_in, p_out, p_read = PRICES[model]
        return (
            self.input * p_in
            + self.output * p_out
            + self.cache_read * p_read
            + self.cache_write * p_in * CACHE_WRITE_1H_MULTIPLIER
        ) / 1_000_000


@dataclass
class Result:
    report: str
    plan: dict
    findings: list[dict]
    failed_passes: list[str]
    cache_log: list[tuple[str, int, int]]
    usage: Usage


def _log(message: str) -> None:
    sys.stderr.write(message + "\n")
    sys.stderr.flush()


class Reviewer:
    def __init__(self, client: anthropic.Anthropic, manuscript: Manuscript, options: Options):
        self.client = client
        self.manuscript = manuscript
        self.options = options
        self.usage = Usage()
        self.assessments: dict[str, str] = {}
        self.cache_log: list[tuple[str, int, int]] = []
        self._warm: dict[str, threading.Event] = {}
        self._warm_lock = threading.Lock()

    def _call(self, label: str, instruction: str, *, tools: list[dict] | None = None,
              max_tokens: int = 64000) -> str:
        # max_tokens covers the model's thinking as well as its answer, so it is set generously.
        opts = self.options
        messages: list[dict] = [
            {"role": "user", "content": [*self.manuscript.blocks, {"type": "text", "text": instruction}]}
        ]
        kwargs: dict = {
            "model": opts.model,
            "max_tokens": max_tokens,
            "system": [{"type": "text", "text": prompts.SYSTEM}],
            "thinking": {"type": "adaptive"},
            "output_config": {"effort": opts.effort},
        }
        if tools:
            kwargs["tools"] = tools
        if opts.fallback and opts.model in FALLBACK_MODELS:
            kwargs["betas"] = [FALLBACK_BETA]
            kwargs["fallbacks"] = "default"

        # Calls with the same tool list share a cache entry, but an entry can only be read
        # once the call that writes it has begun to answer. The first call for each tool
        # list goes ahead, and the others wait for it so that they read instead of write.
        key = json.dumps(tools or [], sort_keys=True)
        with self._warm_lock:
            warm = self._warm.get(key)
            first = warm is None
            if first:
                warm = self._warm[key] = threading.Event()
        if not first:
            warm.wait(timeout=300)

        message = None
        read = written = 0
        try:
            for _ in range(MAX_PAUSE_RESUMES + 1):
                with self.client.beta.messages.stream(messages=messages, **kwargs) as stream:
                    for _event in stream:
                        warm.set()
                    message = stream.get_final_message()
                self.usage.add(message.usage)
                read += getattr(message.usage, "cache_read_input_tokens", 0) or 0
                written += getattr(message.usage, "cache_creation_input_tokens", 0) or 0
                if message.stop_reason != "pause_turn":
                    break
                # A long tool-using turn paused on the server. Sending it back resumes it.
                messages.append({"role": "assistant", "content": message.content})
        finally:
            warm.set()
        self.cache_log.append((label, read, written))

        if message.stop_reason == "refusal":
            details = getattr(message, "stop_details", None)
            category = getattr(details, "category", None) or "unspecified"
            raise ReviewError(f"{label}: the model declined the request (category: {category}).")
        if message.stop_reason == "pause_turn":
            raise ReviewError(f"{label}: the tool-using turn did not finish.")
        if message.stop_reason == "max_tokens":
            raise ReviewError(f"{label}: the output was cut off at the token limit.")

        texts = [block.text for block in message.content if block.type == "text" and block.text.strip()]
        if not texts:
            raise ReviewError(f"{label}: the model returned no text.")
        # With tools, the model may write notes between tool calls. The answer is the last text block.
        return texts[-1]

    def _call_json(self, label: str, instruction: str, shape: str, **kwargs) -> dict:
        instruction += prompts.JSON_ONLY.format(shape=shape)
        error: Exception | None = None
        for _attempt in range(2):
            text = self._call(label, instruction, **kwargs)
            match = re.search(r"\{.*\}", text, re.DOTALL)
            try:
                data = json.loads(match.group(0)) if match else None
            except json.JSONDecodeError as exc:
                error, data = exc, None
            if isinstance(data, dict):
                return data
            _log(f"  {label}: the reply was not valid JSON, asking again.")
        raise ReviewError(f"{label}: the output was not valid JSON ({error}).")

    def _findings_pass(self, label: str, instruction: str, tools: list[dict] | None = None) -> list[dict]:
        _log(f"  started: {label}")
        data = self._call_json(label, instruction, prompts.FINDINGS_SHAPE, tools=tools)
        findings = []
        for item in data.get("findings") or []:
            if not isinstance(item, dict):
                continue
            finding = {key: str(item.get(key, "")) for key in prompts.FINDING_KEYS}
            finding["sources"] = [s for s in item.get("sources") or [] if isinstance(s, dict)]
            finding["reviewer"] = label
            findings.append(finding)
        if data.get("assessment"):
            self.assessments[label] = str(data["assessment"])
        _log(f"  finished: {label} ({len(findings)} findings)")
        return findings

    def run(self) -> Result:
        opts = self.options

        _log("Reading the manuscript and planning the review...")
        plan = self._call_json(
            "plan", prompts.MAP.format(n_groups=opts.max_groups), prompts.MAP_SHAPE
        )
        groups = [
            {k: str(g.get(k, "")) for k in ("name", "pages", "focus")}
            for g in plan.get("section_groups") or [] if isinstance(g, dict) and g.get("name")
        ][: opts.max_groups]
        if not groups:
            raise ReviewError("The plan contained no section groups.")
        plan["headline_claims"] = [c for c in plan.get("headline_claims") or [] if isinstance(c, dict)]
        plan.setdefault("field", "not stated")
        group_list = "; ".join(f'"{g["name"]}" (pages {g["pages"]})' for g in groups)
        fields = prompts.finding_fields()

        passes: list[tuple[str, str, list[dict] | None]] = []
        for group in groups:
            passes.append((
                f"section: {group['name']}",
                prompts.SECTION.format(
                    name=group["name"], pages=group["pages"], focus=group["focus"],
                    groups=group_list, finding_fields=fields,
                ),
                None,
            ))
        claims = "\n".join(f"- {c.get('claim', '')} ({c.get('location', '')})" for c in plan["headline_claims"]) or "- none listed"
        passes.append((
            "numbers",
            prompts.NUMBERS.format(claims=claims, groups=group_list, finding_fields=fields),
            [CODE_EXECUTION_TOOL] if opts.code else None,
        ))
        if opts.web:
            passes.append((
                "outside sources",
                prompts.CONTEXT.format(field=plan["field"], groups=group_list, finding_fields=fields),
                [WEB_SEARCH_TOOL],
            ))
        if opts.writing:
            passes.append((
                "structure",
                prompts.STRUCTURE.format(groups=group_list, finding_fields=fields),
                None,
            ))
            passes.append((
                "prose",
                prompts.PROSE.format(style=opts.style, groups=group_list, finding_fields=fields),
                None,
            ))

        _log(f"Running {len(passes)} reviewers...")
        findings: list[dict] = []
        failed: list[str] = []
        with ThreadPoolExecutor(max_workers=max(1, opts.workers)) as pool:
            futures = [
                (label, pool.submit(self._findings_pass, label, text, tools))
                for label, text, tools in passes
            ]
            for label, future in futures:
                try:
                    findings.extend(future.result())
                except (ReviewError, anthropic.APIError) as exc:
                    # One failed reviewer should not cost the whole review. It is named in the report.
                    failed.append(label)
                    _log(f"  failed: {label}: {exc}")
        if not findings and failed:
            raise ReviewError("Every reviewer failed. See the messages above.")

        checker = QuoteChecker(self.manuscript.text)
        for finding in findings:
            finding["quote_found"] = checker.found(finding.get("quote", ""))
        missing = sum(1 for f in findings if f["quote_found"] is False)
        _log(f"Collected {len(findings)} findings. {missing} quote passages were not found in the text.")

        _log("Writing the report...")
        notes = f"Notes from the authors: {opts.notes}\n\n" if opts.notes else ""
        plan_for_report = {**plan, "section_groups": groups}
        report = self._call(
            "synthesis",
            prompts.SYNTHESIS.format(
                notes=notes,
                plan=json.dumps(plan_for_report, indent=1, ensure_ascii=False),
                assessments=json.dumps(self.assessments, indent=1, ensure_ascii=False) if self.assessments else "none",
                findings=json.dumps(findings, indent=1, ensure_ascii=False),
            ),
            max_tokens=64000,
        )
        if failed:
            report += (
                "\n\n---\n\n**Incomplete review.** These reviewers failed and their part of the "
                "manuscript was not checked: " + ", ".join(failed) + "."
            )
        return Result(report=report, plan=plan_for_report, findings=findings, failed_passes=failed,
                      cache_log=self.cache_log, usage=self.usage)
