"""The worked example of docs/extending/who-needs-what.md runs: three
installed packages, examplereport using examplegen using Testuary, each with its
own profile, and every line the page shows is a line of the check's report
for examplereport."""
from __future__ import annotations

import json
import re
from pathlib import Path

from langadapter import check, consumers

PAGE = Path(__file__).resolve().parents[1] / "docs" / "extending" / "who-needs-what.md"


def _dist(root, name, version="1.0", *, requires=(), profile=None):
    package = name.replace("-", "_")
    info = root / f"{package}-{version}.dist-info"
    info.mkdir(parents=True)
    lines = ["Metadata-Version: 2.1", f"Name: {name}", f"Version: {version}"]
    lines += [f"Requires-Dist: {requirement}" for requirement in requires]
    (info / "METADATA").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (info / "RECORD").write_text(f"{package}/langadapter_profile.json,,\n", encoding="utf-8")
    (root / package).mkdir()
    (root / package / "langadapter_profile.json").write_text(json.dumps(profile),
                                                            encoding="utf-8")


def _profile(consumer, title, levels):
    return {"schema": 1, "consumer": consumer, "title": title, "levels": levels}


def _chain(root):
    _dist(root, "testuary", "0.2.0", profile=_profile("testuary", "Testuary", [
        {"level": 1, "title": "Run, coverage, mutation", "families": [
            {"family": "run tests", "tier": "required"},
            {"family": "coverage", "tier": "required"},
            {"family": "mutation", "tier": "required"}]},
        {"level": 2, "title": "Load repair", "families": [
            {"family": "load repair", "tier": "optional",
             "improves": "keeps generated suites that fail to load",
             "fallback": "a generated suite that does not load is discarded"}]}]))
    _dist(root, "examplegen", requires=("testuary",), profile=_profile("examplegen", "examplegen", [
        {"level": 1, "title": "Timing", "families": [{"family": "timing", "tier": "required"}]}]))
    _dist(root, "examplereport", requires=("examplegen",), profile=_profile("examplereport", "examplereport", [
        {"level": 1, "title": "Metrics", "families": [
            {"family": "structural metrics", "tier": "optional",
             "improves": "reports show each file's size and complexity",
             "fallback": "reports show the test results only"}]}]))


def _shown_lines():
    """The report lines the worked example shows: its text block after the
    command."""
    text = PAGE.read_text(encoding="utf-8")
    section = text.split("## A worked example", 1)[1].split("\n## ", 1)[0]
    block = re.findall(r"```text\n(.*?)```", section, re.S)[0]
    return [line.strip() for line in block.splitlines() if line.strip()]


def test_the_worked_example_shows_lines_of_the_real_report(tmp_path):
    _chain(tmp_path)
    environment = consumers.Installed([tmp_path])
    composed = consumers.composed(["examplereport"], environment=environment)
    assert composed.problems == ()
    assert set(composed.needs) == {"run tests", "coverage", "mutation", "load repair",
                                   "timing", "structural metrics"}
    report = [line.strip() for line in check.report(
        "python", consumer="examplereport", environment=environment).lines()]
    shown = _shown_lines()
    assert shown
    for line in shown:
        assert line in report, line
