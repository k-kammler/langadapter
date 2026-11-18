"""Consumers' requirement profiles: reading them from installed distributions
without importing the consumer, their validation, and their composition
through the dependency closure (chains, extras, conflicting tiers, cycles,
markers, profile files)."""
import importlib
import json
import sys

import pytest

from langadapter import consumers


def _profile(consumer, title, *levels, schema=1):
    return {"schema": schema, "consumer": consumer, "title": title,
            "levels": [{"level": number, "title": name,
                        "families": [dict(zip(("family", "tier", "improves", "fallback"), f))
                                     for f in families]}
                       for number, name, families in levels]}


def _dist(root, name, version="1.0", *, requires=(), extras=(), profile=None):
    """A distribution in ``root``: its metadata, and with ``profile`` the
    file ``langadapter_profile.json`` in its package, listed in its RECORD.
    Its package raises when imported."""
    package = name.replace("-", "_")
    info = root / f"{package}-{version}.dist-info"
    info.mkdir(parents=True)
    lines = ["Metadata-Version: 2.1", f"Name: {name}", f"Version: {version}"]
    lines += [f"Provides-Extra: {extra}" for extra in extras]
    lines += [f"Requires-Dist: {requirement}" for requirement in requires]
    (info / "METADATA").write_text("\n".join(lines) + "\n", encoding="utf-8")
    if profile is not None:
        (info / "RECORD").write_text(f"{package}/__init__.py,,\n{package}/langadapter_profile.json,,\n",
                                     encoding="utf-8")
        (root / package).mkdir()
        (root / package / "__init__.py").write_text(
            "raise RuntimeError('a consumer was imported')\n", encoding="utf-8")
        (root / package / "langadapter_profile.json").write_text(json.dumps(profile),
                                                                encoding="utf-8")


TESTUARY = _profile("testuary", "Testuary",
                    (1, "Run, coverage, mutation", [("run tests", "required"),
                                                    ("coverage", "required"),
                                                    ("mutation", "required")]),
                    (2, "Load repair", [("load repair", "optional",
                                         "keeps suites that fail to load",
                                         "a suite that does not load is discarded")]))
EXAMPLEGEN = _profile("examplegen", "examplegen",
                      (1, "Timing", [("timing", "required"),
                                     ("coverage", "optional", "faster runs", "coverage is measured "
                                      "by the slower route")]))


def _environment(tmp_path):
    # importlib.metadata keeps a directory's listing until the directory's
    # modification time changes, which two writes in one clock tick leave as
    # it was.
    importlib.invalidate_caches()
    return consumers.Installed([tmp_path])


def test_a_profile_is_read_without_importing_its_consumer(tmp_path):
    _dist(tmp_path, "testuary", "0.2.0", profile=TESTUARY)
    found = consumers.profile("testuary", _environment(tmp_path))
    assert (found.title, found.version, found.distribution) == ("Testuary", "0.2.0", "testuary")
    assert found.need("run tests").tier == consumers.REQUIRED
    assert found.need("merging").tier == consumers.NOT_USED
    assert "testuary" not in sys.modules or not str(
        getattr(sys.modules["testuary"], "__file__", "")).startswith(str(tmp_path))


def test_a_profile_that_names_another_consumer_is_that_module_s_problem(tmp_path):
    _dist(tmp_path, "testuary", "0.2.0", profile=dict(TESTUARY, consumer="other"))
    _dist(tmp_path, "examplegen", profile=EXAMPLEGEN)
    found, problems = consumers.discover(_environment(tmp_path))
    assert list(found) == ["examplegen"]
    assert [p.consumer for p in problems] == ["testuary"]
    assert "the module testuary's profile names the consumer other" in problems[0].message


def test_a_module_no_distribution_lists_is_found_by_its_dir(tmp_path, monkeypatch):
    """A named consumer whose package holds the profile but whose
    distribution lists no file, read without importing it, and used alone."""
    package_dir = tmp_path / "walktool"
    package_dir.mkdir()
    (package_dir / "__init__.py").write_text("raise RuntimeError('a consumer was imported')\n",
                                        encoding="utf-8")
    (package_dir / "langadapter_profile.json").write_text(
        json.dumps(dict(TESTUARY, consumer="walktool", title="Walk tool")), encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    assert consumers.profile("walktool").title == "Walk tool"
    result = consumers.composed(["walktool"])
    assert result.need("mutation").tier == consumers.REQUIRED
    assert any("no installed distribution lists" in note for note in result.notes)
    assert "walktool" not in sys.modules


def test_a_consumer_that_is_not_installed_is_said_plainly(tmp_path):
    with pytest.raises(consumers.ProfileError) as raised:
        consumers.profile("examplegen", _environment(tmp_path))
    message = str(raised.value)
    assert "No installed package provides the consumer examplegen" in message
    assert "pip install examplegen" in message
    result = consumers.composed(["examplegen"], environment=_environment(tmp_path))
    assert result.needs == {} and result.problems[0].consumer == "examplegen"


@pytest.mark.parametrize("change, problem", [
    (lambda p: p["levels"][0]["families"].append({"family": "running", "tier": "required"}),
     "no family"),
    (lambda p: p["levels"][0]["families"][0].update(tier="must"), "the tier 'must'"),
    (lambda p: p.update(schema=2), "newer than this LangAdapter"),
    (lambda p: p["levels"][1]["families"][0].pop("fallback"), "its fallback"),
    (lambda p: p["levels"][1]["families"].append({"family": "coverage", "tier": "required"}),
     "appears twice"),
])
def test_a_profile_that_breaks_the_schema_is_named(tmp_path, change, problem):
    broken = json.loads(json.dumps(TESTUARY))
    change(broken)
    with pytest.raises(consumers.ProfileError, match=problem):
        consumers.parse(broken)
    _dist(tmp_path, "testuary", profile=broken)
    found, problems = consumers.discover(_environment(tmp_path))
    assert found == {} and problem.split()[0] in problems[0].message


RUN = {"title": "Run the consumer", "text": ["It runs on recorded answers."],
       "command": ["tool", "--demo"], "install": "pip install tool"}


def test_a_profile_gives_its_closing_tutorial_step_as_data(tmp_path):
    _dist(tmp_path, "testuary", profile=dict(TESTUARY, tutorial=RUN))
    run = consumers.profile("testuary", _environment(tmp_path)).tutorial
    assert run == consumers.TutorialRun("Run the consumer", ("It runs on recorded answers.",),
                                        ("tool", "--demo"), "pip install tool")
    assert consumers.parse(TESTUARY).tutorial is None


@pytest.mark.parametrize("change", [
    lambda r: r.update(title=" "),
    lambda r: r.update(command=[]),
    lambda r: r.update(command="tool --demo"),
    lambda r: r.update(text="one paragraph"),
    lambda r: r.pop("install"),
    lambda r: r.update(command=["tool", 1]),
])
def test_a_tutorial_run_that_breaks_the_schema_is_named(change):
    broken = dict(RUN)
    change(broken)
    with pytest.raises(consumers.ProfileError, match="the tutorial run names a title"):
        consumers.parse(dict(TESTUARY, tutorial=broken))
    with pytest.raises(consumers.ProfileError, match="the tutorial run names a title"):
        consumers.parse(dict(TESTUARY, tutorial=["tool"]))


def test_a_two_level_chain_brings_in_the_dependency_s_needs(tmp_path):
    _dist(tmp_path, "testuary", "0.2.0", profile=TESTUARY)
    _dist(tmp_path, "examplegen", requires=("testuary>=0.2",), profile=EXAMPLEGEN)
    result = consumers.composed(["examplegen"], environment=_environment(tmp_path))
    timing, mutation = result.need("timing"), result.need("mutation")
    assert timing.tier == mutation.tier == consumers.REQUIRED
    assert [s.text() for s in timing.sources] == ["required by examplegen"]
    assert [s.text() for s in mutation.sources] == ["required by Testuary, via examplegen"]
    assert mutation.level == "Testuary level 1" and timing.level == "examplegen level 1"


def test_a_chain_of_any_depth_composes_the_same_way(tmp_path):
    _dist(tmp_path, "x", requires=("y",), profile=_profile("x", "X", (1, "X", [("timing", "required")])))
    _dist(tmp_path, "y", requires=("lib",), profile=_profile("y", "Y", (1, "Y", [("merging", "required")])))
    _dist(tmp_path, "lib", requires=("z",))
    _dist(tmp_path, "z", profile=_profile("z", "Z", (1, "Z", [("coverage", "required")])))
    result = consumers.composed(["x"], environment=_environment(tmp_path))
    assert [s.text() for s in result.need("coverage").sources] == ["required by Z, via Y, via X"]


def test_an_extra_counts_when_every_distribution_it_requires_is_installed(tmp_path):
    _dist(tmp_path, "testuary", profile=TESTUARY)
    _dist(tmp_path, "examplegen", extras=("generation",),
          requires=('testuary; extra == "generation"', 'gen-helper; extra == "generation"'),
          profile=EXAMPLEGEN)
    without = consumers.composed(["examplegen"], environment=_environment(tmp_path))
    assert without.need("mutation").tier == consumers.NOT_USED
    assert any("extra generation is not installed" in note for note in without.notes)
    # An extra that brings in no profile-publishing distribution changes
    # nothing, so it gets no note.
    _dist(tmp_path, "tool", extras=("dev",), requires=('ruff; extra == "dev"',),
          profile=_profile("tool", "Tool", (1, "Run", [("run tests", "required")])))
    assert not any("extra dev" in note
                   for note in consumers.composed(["tool"], environment=_environment(tmp_path)).notes)
    _dist(tmp_path, "gen-helper")
    with_extra = consumers.composed(["examplegen"], environment=_environment(tmp_path))
    assert with_extra.need("mutation").tier == consumers.REQUIRED


def test_the_strictest_tier_wins_and_both_sources_show(tmp_path):
    _dist(tmp_path, "testuary", profile=TESTUARY)
    _dist(tmp_path, "examplegen", requires=("testuary",), profile=EXAMPLEGEN)
    coverage = consumers.composed(["examplegen"], environment=_environment(tmp_path)).need("coverage")
    assert coverage.tier == consumers.REQUIRED
    assert [s.text() for s in coverage.sources] == ["optional for examplegen",
                                                    "required by Testuary, via examplegen"]


def test_a_cycle_and_a_marker_that_does_not_hold_are_followed_once_or_not(tmp_path):
    _dist(tmp_path, "a", requires=("b",), profile=_profile("a", "A", (1, "A", [("timing", "required")])))
    _dist(tmp_path, "b", requires=("a", 'old; python_version < "3.0"'),
          profile=_profile("b", "B", (1, "B", [("coverage", "required")])))
    _dist(tmp_path, "old", profile=_profile("old", "Old", (1, "Old", [("merging", "required")])))
    result = consumers.composed(["a"], environment=_environment(tmp_path))
    assert result.need("coverage").tier == consumers.REQUIRED
    assert result.need("merging").tier == consumers.NOT_USED


def test_several_consumers_and_a_profile_file(tmp_path):
    _dist(tmp_path, "testuary", profile=TESTUARY)
    mine = tmp_path / "mine.json"
    mine.write_text(json.dumps(_profile("mine", "Mine", (1, "M", [("merging", "required")]))),
                    encoding="utf-8")
    result = consumers.composed(["testuary"], files=[mine], environment=_environment(tmp_path))
    assert result.need("merging").tier == result.need("mutation").tier == consumers.REQUIRED
    assert any("the tools it builds on are not added" in note for note in result.notes)
    assert result.need("mutation").level == "Testuary level 1"


def _editable(root, name, profile):
    """An editable install: the dist-info and a ``.pth`` file naming
    ``root/src`` in ``root/site``, the package (whose import raises) with its
    profile in ``root/src``."""
    site, src = root / "site", root / "src"
    info = site / f"{name}-1.0.dist-info"
    info.mkdir(parents=True)
    (info / "METADATA").write_text(f"Metadata-Version: 2.1\nName: {name}\nVersion: 1.0\n",
                                   encoding="utf-8")
    (info / "RECORD").write_text(f"_editable_{name}.pth,,\n", encoding="utf-8")
    (site / f"_editable_{name}.pth").write_text(f"# the source directory\n{src}\n", encoding="utf-8")
    package_dir = src / name
    package_dir.mkdir(parents=True)
    (package_dir / "__init__.py").write_text("raise RuntimeError('a consumer was imported')\n",
                                        encoding="utf-8")
    (package_dir / "langadapter_profile.json").write_text(json.dumps(profile), encoding="utf-8")
    return site, src


def test_an_editable_install_is_read_from_its_package_without_importing_it(tmp_path, monkeypatch):
    site, src = _editable(tmp_path, "edittool", dict(TESTUARY, consumer="edittool",
                                                     title="Edit tool"))
    monkeypatch.syspath_prepend(str(src))
    monkeypatch.syspath_prepend(str(site))
    found, problems = consumers.discover(consumers.Installed())
    assert (found["edittool"].title, found["edittool"].distribution) == ("Edit tool", "edittool")
    assert "edittool" not in sys.modules
    assert not [p for p in problems if p.consumer == "edittool"]


def _package(package_dir, consumer):
    """A package whose import raises, with the profile of ``consumer``."""
    package_dir.mkdir(parents=True)
    (package_dir / "__init__.py").write_text("raise RuntimeError('a consumer was imported')\n",
                                        encoding="utf-8")
    (package_dir / "langadapter_profile.json").write_text(
        json.dumps(dict(TESTUARY, consumer=consumer, title=consumer)), encoding="utf-8")


def test_a_finder_style_editable_install_is_found_where_python_imports_it(tmp_path, monkeypatch):
    """setuptools' editable install whose finder serves the package: its
    .pth holds only an import line and its RECORD lists no package file."""
    site, src = tmp_path / "site", tmp_path / "src"
    info = site / "findertool-1.0.dist-info"
    info.mkdir(parents=True)
    (info / "METADATA").write_text("Metadata-Version: 2.1\nName: findertool\nVersion: 1.0\n",
                                   encoding="utf-8")
    (info / "RECORD").write_text("__editable__.findertool-1.0.pth,,\n", encoding="utf-8")
    (info / "top_level.txt").write_text("findertool\n", encoding="utf-8")
    (site / "__editable__.findertool-1.0.pth").write_text(
        "import __editable___findertool_finder; __editable___findertool_finder.install()\n",
        encoding="utf-8")
    _package(src / "findertool", "findertool")
    monkeypatch.syspath_prepend(str(src))
    monkeypatch.syspath_prepend(str(site))
    found, _ = consumers.discover(consumers.Installed())
    assert found["findertool"].distribution == "findertool"
    assert "findertool" not in sys.modules


def test_a_pth_dir_s_package_counts_only_for_its_own_distribution(tmp_path):
    """A .pth that adds a shared directory credits its distribution only with
    the package of its own name, and a consumer counts once."""
    site, shared = tmp_path / "site", tmp_path / "shared"
    info = site / "edittool-1.0.dist-info"
    info.mkdir(parents=True)
    (info / "METADATA").write_text("Metadata-Version: 2.1\nName: edittool\nVersion: 1.0\n",
                                   encoding="utf-8")
    (info / "RECORD").write_text("plugins.pth,,\n", encoding="utf-8")
    (site / "plugins.pth").write_text(f"{shared}\n", encoding="utf-8")
    _package(shared / "edittool", "edittool")
    _package(shared / "othertool", "othertool")
    _dist(site, "testuary", "0.2.0", profile=TESTUARY)
    _package(shared / "testuary", "testuary")
    found, problems = consumers.discover(consumers.Installed([site]))
    assert sorted(found) == ["edittool", "testuary"] and problems == ()
    assert found["testuary"].source == "testuary/langadapter_profile.json"


def test_a_zip_install_is_read_from_the_archive(tmp_path):
    import zipfile

    archive = tmp_path / "ziptool.zip"
    with zipfile.ZipFile(archive, "w") as zipped:
        zipped.writestr("ziptool-1.0.dist-info/METADATA",
                        "Metadata-Version: 2.1\nName: ziptool\nVersion: 1.0\n")
        zipped.writestr("ziptool-1.0.dist-info/RECORD", "ziptool/langadapter_profile.json,,\n")
        zipped.writestr("ziptool/langadapter_profile.json",
                        json.dumps(dict(TESTUARY, consumer="ziptool")))
    found, problems = consumers.discover(consumers.Installed([archive]))
    assert found["ziptool"].version == "1.0" and problems == ()


def test_a_stale_copy_later_on_the_path_does_not_win(tmp_path):
    first, stale = tmp_path / "first", tmp_path / "stale"
    _dist(first, "testuary", "0.2.0", profile=TESTUARY)
    _dist(stale, "testuary", "0.1.0", profile=dict(TESTUARY, title="Old Testuary"))
    environment = consumers.Installed([first, stale])
    assert [d.version for d in environment.distributions()] == ["0.2.0"]
    found, _ = consumers.discover(environment)
    assert (found["testuary"].version, found["testuary"].title) == ("0.2.0", "Testuary")


def test_another_package_s_broken_metadata_breaks_nothing(tmp_path):
    _dist(tmp_path, "testuary", "0.2.0", requires=["badmarker; python_version > 'x'",
                                                    "other; os_name == 'never'"],
          profile=TESTUARY)
    broken = tmp_path / "broken-1.0.dist-info"
    broken.mkdir()
    (broken / "METADATA").write_text("Metadata-Version: 2.1\nName: broken\nVersion: 1.0\n",
                                     encoding="utf-8")
    # A RECORD that cannot be read.
    (broken / "RECORD").mkdir()
    found, problems = consumers.discover(_environment(tmp_path))
    assert "testuary" in found
    result = consumers.composed(["testuary"], environment=_environment(tmp_path))
    assert result.needs["run tests"].tier == consumers.REQUIRED
