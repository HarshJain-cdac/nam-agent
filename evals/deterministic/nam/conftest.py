"""The NAM evals' shared setting: need capture's detail questions (PR flow
1(e), policy/guided_forms.json "capture") are off unless a test asks for
them with @pytest.mark.capture_details.

Most evals walk a case to a later stage and answer only the question that
tells the past tenders apart; what they check comes after capture. The evals
of the details themselves (test_nam_capture_flow.py) turn them on.
"""

from __future__ import annotations

import pytest

from nam_agent.nam.playbooks import need_guided_form


def pytest_configure(config):
    config.addinivalue_line("markers", "capture_details: need capture asks the guided "
                                       "form's detail questions (PR flow 1(e))")


_REAL = need_guided_form.policy


@pytest.fixture(autouse=True, scope="session")
def _no_capture_details():
    """Off for the whole session, module-scoped fixtures included."""
    real = _REAL()
    without = {k: v for k, v in real.items() if k != "capture"}
    patch = pytest.MonkeyPatch()
    patch.setattr(need_guided_form, "policy", lambda: without)
    yield
    patch.undo()


@pytest.fixture(autouse=True, scope="session")
def _whole_flow():
    """The evals walk cases to the PR, so the chat's scope is the whole flow
    (nam_agent/nam/scope.py); the evals of the tender-only scope set it back."""
    patch = pytest.MonkeyPatch()
    patch.setenv("NAM_STOP_AT", "pr")
    yield
    patch.undo()


@pytest.fixture(autouse=True)
def _capture_details(request, monkeypatch):
    if request.node.get_closest_marker("capture_details"):
        monkeypatch.setattr(need_guided_form, "policy", _REAL)
