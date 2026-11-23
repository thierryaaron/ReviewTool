"""Read equations out of a Word file and write them as LaTeX.

Word stores equations in a format called OMML. This covers the constructs that
appear in most papers: fractions, subscripts and superscripts, sums and
integrals, brackets, roots, accents and function names. Anything else is
written out as its plain text content.
"""

from __future__ import annotations

import re
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

M = "{http://schemas.openxmlformats.org/officeDocument/2006/math}"
W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"

SYMBOLS = {
    "∑": r"\sum", "∏": r"\prod", "∫": r"\int", "×": r"\times", "·": r"\cdot", "≤": r"\le",
    "≥": r"\ge", "≠": r"\ne", "≈": r"\approx", "±": r"\pm", "−": "-", "∞": r"\infty",
    "∂": r"\partial", "→": r"\to", "∈": r"\in", "Δ": r"\Delta", "α": r"\alpha", "β": r"\beta",
    "γ": r"\gamma", "δ": r"\delta", "ε": r"\varepsilon", "η": r"\eta", "θ": r"\theta",
    "λ": r"\lambda", "μ": r"\mu", "π": r"\pi", "ρ": r"\rho", "σ": r"\sigma", "τ": r"\tau",
    "φ": r"\varphi", "ω": r"\omega", "Σ": r"\Sigma", "Ω": r"\Omega",
}


def _text(s: str) -> str:
    out = []
    for ch in s:
        out.append(SYMBOLS[ch] + " " if ch in SYMBOLS else ch)
    return "".join(out)


def _child(node, name):
    return node.find(M + name)


def _arg(node, name) -> str:
    child = _child(node, name)
    return _children(child) if child is not None else ""


def _children(node) -> str:
    return "".join(_convert(c) for c in node)


def _prop(node, pr_name: str, name: str, default: str) -> str:
    pr = _child(node, pr_name)
    el = pr.find(M + name) if pr is not None else None
    return el.get(M + "val", default) if el is not None else default


def _convert(node) -> str:
    tag = node.tag.replace(M, "")
    if tag == "r":
        return _text("".join(t.text or "" for t in node.findall(M + "t")))
    if tag == "f":
        return rf"\frac{{{_arg(node, 'num')}}}{{{_arg(node, 'den')}}}"
    if tag == "sSub":
        return f"{{{_arg(node, 'e')}}}_{{{_arg(node, 'sub')}}}"
    if tag == "sSup":
        return f"{{{_arg(node, 'e')}}}^{{{_arg(node, 'sup')}}}"
    if tag == "sSubSup":
        return f"{{{_arg(node, 'e')}}}_{{{_arg(node, 'sub')}}}^{{{_arg(node, 'sup')}}}"
    if tag == "nary":
        op = _text(_prop(node, "naryPr", "chr", "∫")).strip()
        sub, sup = _arg(node, "sub"), _arg(node, "sup")
        return op + (f"_{{{sub}}}" if sub else "") + (f"^{{{sup}}}" if sup else "") + " " + _arg(node, "e")
    if tag == "d":
        left = _prop(node, "dPr", "begChr", "(")
        right = _prop(node, "dPr", "endChr", ")")
        inner = ", ".join(_children(e) for e in node.findall(M + "e"))
        return rf"\left{left or '.'} {inner} \right{right or '.'}"
    if tag == "rad":
        deg = _arg(node, "deg")
        return rf"\sqrt[{deg}]{{{_arg(node, 'e')}}}" if deg else rf"\sqrt{{{_arg(node, 'e')}}}"
    if tag == "func":
        return rf"\operatorname{{{_arg(node, 'fName')}}} {_arg(node, 'e')}"
    if tag == "acc":
        accent = {"̂": r"\hat", "̅": r"\bar", "̃": r"\tilde", "̇": r"\dot"}.get(_prop(node, "accPr", "chr", "̂"), r"\hat")
        return rf"{accent}{{{_arg(node, 'e')}}}"
    if tag == "bar":
        return rf"\overline{{{_arg(node, 'e')}}}"
    if tag in {"limLow", "limUpp"}:
        mark = "_" if tag == "limLow" else "^"
        return f"{_arg(node, 'e')}{mark}{{{_arg(node, 'lim')}}}"
    if tag in {"eqArr", "m"}:
        rows = node.findall(M + "e") or node.findall(M + "mr")
        return r" \\ ".join(_children(r) for r in rows)
    if tag.endswith("Pr"):
        return ""  # formatting properties carry no content
    return _children(node)


def equations(path: Path) -> list[tuple[str, str]]:
    """Return (preceding text, LaTeX) for each equation in the document body, in order."""
    with zipfile.ZipFile(path) as z:
        root = ET.fromstring(z.read("word/document.xml"))
    results: list[tuple[str, str]] = []
    last_text = ""
    for para in root.iter(W + "p"):
        maths = [m for m in para.iter(M + "oMath")]
        plain = "".join(t.text or "" for t in para.iter(W + "t")).strip()
        for m in maths:
            latex = re.sub(r"\s+", " ", _convert(m)).strip()
            # Skip single symbols set in equation style within running text.
            if len(latex) >= 8:
                results.append(((plain or last_text)[-120:], latex))
        if plain:
            last_text = plain
    return results
