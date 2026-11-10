"""DETERMINISTIC EVAL: what ships, and what a test is allowed to read.

1. The NAM knowledge skills ship inside the package (nam_agent/nam/skills), so
   an installed harness finds them beside the code.
2. The test suite must not read the developer's `.env`: `nam_agent/config.py`
   calls load_dotenv() at import, so make_agent pins every switch that changes
   what a turn does.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

from nam_agent.memory import bundled_skill_dirs
from nam_agent.memory.procedural.loader import SkillLoader

REPO = Path(__file__).resolve().parents[2]


def _pyproject() -> dict:
    with (REPO / "pyproject.toml").open("rb") as fh:
        return tomllib.load(fh)


def test_the_bundled_skills_are_findable():
    dirs = bundled_skill_dirs()
    assert dirs and all(d.is_dir() for d in dirs)
    names = {s.name for s in SkillLoader(dirs).skills}
    assert {"nam-documents", "nrl-procurement"} == names, sorted(names)


def test_the_wheel_is_the_package_and_the_skills_are_in_it():
    wheel = _pyproject()["tool"]["hatch"]["build"]["targets"]["wheel"]
    assert wheel["packages"] == ["nam_agent"]
    assert (REPO / "nam_agent" / "nam" / "skills").is_dir()


def test_the_command_is_installed():
    assert _pyproject()["project"]["scripts"]["nam-agent"] == "nam_agent.__main__:main"


def test_evals_never_inherit_the_developers_env():
    import inspect

    from evals import helpers

    src = inspect.getsource(helpers.make_agent)
    for switch in ("fast_greetings", "domain_only", "kg_url", "nam_chat"):
        assert switch in src, f"make_agent no longer pins {switch!r}"
