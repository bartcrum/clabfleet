import pytest


@pytest.fixture(autouse=True)
def _no_real_root_check(monkeypatch):
    """The GUI checks that containerlab gets root by running sudo; tests
    that are not about that must not run sudo on the machine they run on."""
    try:
        from clabfleet.gui import state
    except ImportError:  # GUI extras not installed
        return
    monkeypatch.setattr(state, "local_root_problem", lambda sudo: None)
