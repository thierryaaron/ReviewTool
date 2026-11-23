"""Tests that run without an API key. A stub stands in for the Anthropic client."""

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from reviewtool import prompts
from reviewtool.extract import QuoteChecker, load
from reviewtool.pipeline import Options, Reviewer

PLAN = {
    "title": "A test paper",
    "field": "testing",
    "summary": "It tests.",
    "strengths": ["Short."],
    "section_groups": [
        {"name": "Introduction", "pages": "1", "focus": "framing"},
        {"name": "Results", "pages": "2", "focus": "numbers"},
    ],
    "headline_claims": [{"claim": "Emissions fell by 37.8%", "location": "abstract"}],
}


def finding(quote):
    return {
        "title": "Check this", "kind": "substantive", "certainty": "potential", "severity": "minor",
        "section_group": "Results", "location": "p. 2", "quote": quote, "issue": "x",
        "suggestion": "y", "sources": [],
    }


def message(text, stop_reason="end_turn"):
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text)],
        stop_reason=stop_reason,
        stop_details=None,
        usage=SimpleNamespace(input_tokens=10, output_tokens=5,
                              cache_read_input_tokens=100, cache_creation_input_tokens=0),
    )


class StubStream:
    def __init__(self, msg):
        self.msg = msg

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def __iter__(self):
        return iter(["event"])

    def get_final_message(self):
        return self.msg


class StubClient:
    """Answers each call according to the instruction it was given."""

    def __init__(self):
        self.calls = []
        self.paused_once = False
        self.beta = SimpleNamespace(messages=SimpleNamespace(stream=self.stream))

    def stream(self, **kwargs):
        kwargs = {**kwargs, "messages": list(kwargs["messages"])}
        self.calls.append(kwargs)
        instruction = kwargs["messages"][0]["content"][-1]["text"]
        if instruction.startswith("Read the whole manuscript"):
            return StubStream(message(json.dumps(PLAN)))
        if instruction.startswith("Review one part"):
            reply = "```json\n" + json.dumps({"findings": [finding("emissions fell by 37.8%")]}) + "\n```"
            return StubStream(message(reply))
        if instruction.startswith("Audit the manuscript's numbers"):
            if not self.paused_once:
                self.paused_once = True
                return StubStream(message("working", stop_reason="pause_turn"))
            return StubStream(message(json.dumps({"findings": [finding("a passage that is not in the paper at all")]})))
        if instruction.startswith("Assess how the manuscript is organised"):
            return StubStream(message(json.dumps({"assessment": "Well ordered.", "findings": []})))
        if instruction.startswith("Assess the manuscript's prose"):
            self.prose_instruction = instruction
            f = {**finding("emissions fell by 37.8%"), "kind": "naturalness"}
            return StubStream(message(json.dumps({"assessment": "Reads stiffly.", "findings": [f]})))
        if instruction.startswith("Check the manuscript against"):
            self.context_calls = getattr(self, "context_calls", 0) + 1
            return StubStream(message("not json"))
        self.synthesis_instruction = instruction
        return StubStream(message("# Review: A test paper"))


class PipelineTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        path = Path(self.tmp.name) / "paper.md"
        path.write_text("Abstract. Emissions fell by 37.8% over the decade.", encoding="utf-8")
        self.manuscript = load(path, Path(self.tmp.name) / "converted")

    def tearDown(self):
        self.tmp.cleanup()

    def test_full_run(self):
        client = StubClient()
        result = Reviewer(client, self.manuscript, Options(workers=2, style="STYLE-GUIDE-TEXT")).run()

        # plan, two sections, numbers twice (paused then resumed), outside sources twice
        # (asked again after a reply that was not JSON), structure, prose, synthesis
        self.assertEqual(len(client.calls), 10)
        self.assertEqual(client.context_calls, 2)
        self.assertFalse(any("format" in c["output_config"] for c in client.calls))
        self.assertIn("STYLE-GUIDE-TEXT", client.prose_instruction)
        self.assertIn("Reads stiffly.", client.synthesis_instruction)
        self.assertIn("Well ordered.", client.synthesis_instruction)
        self.assertEqual(result.failed_passes, ["outside sources"])
        self.assertIn("Incomplete review", result.report)
        self.assertEqual(len(result.findings), 4)
        self.assertEqual(sorted(str(f["quote_found"]) for f in result.findings), ["False", "True", "True", "True"])
        self.assertEqual(result.usage.calls, 10)

        # Every call shares the same cached prefix: system prompt, then the manuscript.
        for call in client.calls:
            self.assertEqual(call["system"][0]["text"], prompts.SYSTEM)
            self.assertEqual(call["messages"][0]["content"][0], self.manuscript.blocks[0])
            self.assertEqual(call["fallbacks"], "default")

        resumed = [c for c in client.calls if len(c["messages"]) == 2]
        self.assertEqual(len(resumed), 1)
        self.assertEqual(resumed[0]["messages"][1]["role"], "assistant")

    def test_tools_and_flags(self):
        client = StubClient()
        Reviewer(client, self.manuscript, Options(web=False, code=False, fallback=False, writing=False, model="claude-haiku-5-5")).run()
        # plan, two sections, numbers twice (paused then resumed), synthesis
        self.assertEqual(len(client.calls), 6)
        self.assertFalse(any("tools" in c for c in client.calls))
        self.assertFalse(any("fallbacks" in c for c in client.calls))


class QuoteCheckerTest(unittest.TestCase):
    def test_matching(self):
        checker = QuoteChecker("Reported Scope 1 and 2 emissions fell by 37.8% between\n2015/16 and 2024/25.")
        self.assertTrue(checker.found("emissions fell by 37.8% between 2015/16"))
        self.assertTrue(checker.found("Reported Scope 1 and 2 emissions ... between 2015/16 and 2024/25"))
        self.assertFalse(checker.found("emissions rose by 37.8% between 2015/16"))
        self.assertIsNone(checker.found(""))



class EquationTest(unittest.TestCase):
    def test_word_equation_to_latex(self):
        from xml.etree import ElementTree as ET
        from reviewtool.omml import M, _convert

        ns = M.strip("{}")
        xml = (
            f'<m:oMath xmlns:m="{ns}">'
            "<m:sSubSup><m:e><m:r><m:t>E</m:t></m:r></m:e><m:sub><m:r><m:t>t</m:t></m:r></m:sub>"
            "<m:sup><m:r><m:t>*</m:t></m:r></m:sup></m:sSubSup><m:r><m:t>=</m:t></m:r>"
            '<m:nary><m:naryPr><m:chr m:val="∑"/></m:naryPr><m:sub><m:r><m:t>f</m:t></m:r></m:sub><m:sup/>'
            "<m:e><m:f><m:num><m:r><m:t>Q</m:t></m:r></m:num><m:den><m:r><m:t>2</m:t></m:r></m:den></m:f></m:e>"
            "</m:nary></m:oMath>"
        )
        self.assertEqual(_convert(ET.fromstring(xml)), r"{E}_{t}^{*}=\sum_{f} \frac{Q}{2}")


if __name__ == "__main__":
    unittest.main()
