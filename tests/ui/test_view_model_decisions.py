from PySide6.QtCore import QCoreApplication
from claude_island.ui.world_view_model import WorldViewModel
from claude_island.core.pending_decisions import Decision, DecisionResult

_app = QCoreApplication.instance() or QCoreApplication([])


def _vm():
    calls = {"resolve": [], "focus": []}
    vm = WorldViewModel(
        resolve_fn=lambda did, dec: calls["resolve"].append((did, dec)) or True,
        focus_fn=lambda sid: calls["focus"].append(sid),
    )
    return vm, calls


def test_approve_once_builds_allow_decision():
    vm, calls = _vm()
    vm.approve("d1", False)
    did, dec = calls["resolve"][0]
    assert did == "d1"
    assert dec.result is DecisionResult.ALLOW and dec.remember is False


def test_approve_always_sets_remember():
    vm, calls = _vm()
    vm.approve("d2", True)
    assert calls["resolve"][0][1].remember is True


def test_deny_builds_deny_with_reason():
    vm, calls = _vm()
    vm.deny("d3")
    dec = calls["resolve"][0][1]
    assert dec.result is DecisionResult.DENY and dec.reason


def test_answer_question_relays_answer():
    vm, calls = _vm()
    vm.answerQuestion("d4", "用哪个库?", "date-fns")
    dec = calls["resolve"][0][1]
    assert dec.result is DecisionResult.ALLOW
    assert dec.answers == (("用哪个库?", "date-fns"),)


def test_focus_session_calls_focus_fn():
    vm, calls = _vm()
    vm.focusSession("sess-1")
    assert calls["focus"] == ["sess-1"]


def test_answer_question_multi_joins_labels():
    """answerQuestionMulti joins selected labels with ', ' and resolves as ALLOW."""
    vm, calls = _vm()
    vm.answerQuestionMulti("d5", "选哪个?", ["date-fns", "Day.js"])
    dec = calls["resolve"][0][1]
    assert dec.result is DecisionResult.ALLOW
    # The two selected labels must be joined into a single answer string.
    assert dec.answers == (("选哪个?", "date-fns, Day.js"),)


def test_answer_question_multi_single_selection():
    """Single-item multi-select still produces a valid answer (no trailing comma)."""
    vm, calls = _vm()
    vm.answerQuestionMulti("d6", "用哪个?", ["Luxon"])
    dec = calls["resolve"][0][1]
    assert dec.result is DecisionResult.ALLOW
    assert dec.answers == (("用哪个?", "Luxon"),)


def test_answer_question_multi_empty_selection_resolves_empty():
    """Empty selection is forwarded as an empty-string answer."""
    vm, calls = _vm()
    vm.answerQuestionMulti("d7", "pick?", [])
    dec = calls["resolve"][0][1]
    assert dec.result is DecisionResult.ALLOW
    assert dec.answers == (("pick?", ""),)
