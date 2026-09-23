from __future__ import annotations

from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from voice.cancellation import CancellationToken, VoiceCancelled
from voice.web_backend import FakeDomBackend, page
from voice.web_confirm import ConfirmationStore
from voice.web_contracts import CommitState, WebErrorCode, WebOperation
from voice.web_goal_loop import WebGoalLoop
from voice.web_goal_router import WebGoalRouter
from voice.web_observation import ObservationBuilder
from voice.web_verifier import Criteria


OPERATION_IDS = [
    "click", "type_text", "select", "scroll_down", "scroll_up",
    "wait", "navigate", "done", "blocked", "none",
]


def probs(choice, ids, top=0.9):
    others = [item for item in ids if item != choice]
    rest = round((1.0 - top) / len(others), 6) if others else 0.0
    values = {item: rest for item in others}
    values[choice] = round(top - rest * len(others), 6) if others else top
    drift = round(1.0 - sum(values.values()), 6)
    values[choice] = round(values[choice] + drift, 6)
    return values


def op_answer(choice, confidence=0.9):
    return {"choice": choice, "confidence": confidence,
            "probabilities": probs(choice, OPERATION_IDS, confidence)}


def target_answer(choice, ids, confidence=0.9):
    return {"choice": choice, "confidence": confidence,
            "probabilities": probs(choice, ids, confidence)}


def scripted_router(script):
    """A WebGoalRouter whose ask returns canned answers, one per route() call."""

    calls = {"n": 0}

    def ask(state, questions):
        index = calls["n"]
        calls["n"] += 1
        return script[index] if index < len(script) else script[-1]

    return WebGoalRouter(ask=ask, api_key="test"), calls


def shopping_pages():
    return {
        "cart": page(
            "cart", "https://shop.example",
            title="购物车",
            text="购物车里有 2 件商品",
            elements=[
                {"backend_id": "n1", "role": "textbox", "label": "留言", "input_type": "text"},
                {"backend_id": "n2", "role": "button", "label": "提交订单"},
            ],
        ),
        "done": page(
            "done", "https://shop.example",
            title="订单确认",
            text="订单已提交，订单号 8842",
            elements=[],
        ),
    }


def make_loop(backend, script, **kwargs):
    router, calls = scripted_router(script)
    builder = ObservationBuilder(session_id="s", tab_id="t")
    loop = WebGoalLoop(
        backend=backend,
        router=router,
        builder=builder,
        confirm_store=kwargs.pop("confirm_store", None),
        dry_run=kwargs.pop("dry_run", True),
        candidates=kwargs.pop("candidates", {}),
        **kwargs,
    )
    return loop, calls


class DryRunClosedLoopTests(unittest.TestCase):
    def test_dry_run_click_is_planned_without_side_effects(self):
        backend = FakeDomBackend(shopping_pages(), "cart")
        script = [
            {"operation": op_answer("click"),
             "click_target": target_answer("e002", ["e001", "e002", "none"])},
        ]
        loop, _ = make_loop(backend, script, dry_run=True)
        result = loop.run("提交订单")
        self.assertEqual(result.status, "dry_run")
        self.assertEqual(len(result.steps), 1)
        self.assertEqual(result.steps[0].operation, WebOperation.CLICK)
        self.assertTrue(result.steps[0].dry_run)
        # Dry-run never mutated the page or committed anything.
        self.assertEqual(backend.current_token, "cart")
        self.assertEqual(result.commit_state, CommitState.NOT_COMMITTED)
        self.assertEqual(backend.act_log[-1]["dry_run"], True)

    def test_dry_run_is_the_default(self):
        backend = FakeDomBackend(shopping_pages(), "cart")
        script = [{"operation": op_answer("click"),
                   "click_target": target_answer("e002", ["e001", "e002", "none"])}]
        loop, _ = make_loop(backend, script)  # no dry_run passed
        self.assertTrue(loop.dry_run)
        self.assertEqual(loop.run("提交订单").status, "dry_run")


class RealActLoopTests(unittest.TestCase):
    def test_real_click_navigates_and_completes_only_when_verified(self):
        backend = FakeDomBackend(shopping_pages(), "cart")
        # Make the submit button navigate to the done page.
        backend._pages["cart"].elements[1]["navigates_to"] = "done"
        script = [
            {"operation": op_answer("click"),
             "click_target": target_answer("e002", ["e001", "e002", "none"])},
            {"operation": op_answer("done")},
        ]
        loop, _ = make_loop(backend, script, dry_run=False,
                            confirm_store=ConfirmationStore())
        result = loop.run(
            "提交订单",
            criteria=[Criteria("title", "订单确认")],
            confirm_provider=lambda proposal, obs, token_id: token_id,
        )
        self.assertEqual(result.status, "completed")
        self.assertEqual(backend.current_token, "done")
        self.assertTrue(result.completed)

    def test_model_done_without_evidence_is_unverified_not_completed(self):
        backend = FakeDomBackend(shopping_pages(), "cart")
        script = [{"operation": op_answer("done")}]
        loop, _ = make_loop(backend, script, dry_run=False)
        result = loop.run("提交订单", criteria=[Criteria("title", "订单确认")])
        # Still on the cart page: the DONE claim has no supporting evidence.
        self.assertEqual(result.status, "unverified")
        self.assertEqual(result.error_code, WebErrorCode.VERIFICATION_UNSATISFIED)
        self.assertFalse(result.completed)

    def test_commit_unknown_is_reported_and_never_retried(self):
        backend = FakeDomBackend(shopping_pages(), "cart")
        # navigate to an unknown destination -> fake backend returns UNKNOWN.
        script = [{"operation": op_answer("navigate"),
                   "text_candidate": {"choice": "c0", "confidence": 0.9,
                                      "probabilities": probs("c0", ["c0", "none"])}}]
        loop, _ = make_loop(
            backend, script, dry_run=False,
            candidates={"c0": "https://unresolved.example"},
        )
        result = loop.run("跳转到某处")
        self.assertEqual(result.status, "commit_unknown")
        self.assertEqual(result.commit_state, CommitState.UNKNOWN)
        self.assertEqual(result.error_code, WebErrorCode.COMMIT_UNKNOWN)
        # Exactly one act attempt: no auto-retry of an unobservable mutation.
        self.assertEqual(len(backend.act_log), 1)


class PolicyGateTests(unittest.TestCase):
    def test_sensitive_target_is_blocked_before_any_action(self):
        pages = {
            "login": page(
                "login", "https://bank.example", title="登录", text="",
                elements=[{"backend_id": "n1", "role": "textbox", "label": "Password",
                           "input_type": "password"}],
            ),
        }
        backend = FakeDomBackend(pages, "login")
        # Password inputs are filtered out, so no target exists -> chooser gets none.
        script = [{"operation": op_answer("done")}]
        loop, _ = make_loop(backend, script, dry_run=False)
        result = loop.run("登录")
        # The password field never became a candidate; done with no criteria completes,
        # but the key invariant is the backend recorded no mutating action.
        self.assertEqual(backend.act_log, [])

    def test_confirmation_required_when_no_provider(self):
        backend = FakeDomBackend(shopping_pages(), "cart")
        script = [{"operation": op_answer("type_text"),
                   "type_text_target": target_answer("e001", ["e001", "e002", "none"]),
                   "text_candidate": {"choice": "c0", "confidence": 0.9,
                                      "probabilities": probs("c0", ["c0", "none"])}}]
        loop, _ = make_loop(backend, script, dry_run=False,
                            candidates={"c0": "尽快发货"})
        result = loop.run("填写留言")
        self.assertEqual(result.status, "confirmation_required")
        self.assertEqual(result.error_code, WebErrorCode.CONFIRM_REQUIRED)
        self.assertEqual(backend.act_log, [])

    def test_confirmed_input_executes_after_reobserve_and_repolicy(self):
        backend = FakeDomBackend(shopping_pages(), "cart")
        script = [{"operation": op_answer("type_text"),
                   "type_text_target": target_answer("e001", ["e001", "e002", "none"]),
                   "text_candidate": {"choice": "c0", "confidence": 0.9,
                                      "probabilities": probs("c0", ["c0", "none"])}}]
        loop, _ = make_loop(backend, script, dry_run=False,
                            candidates={"c0": "尽快发货"},
                            confirm_store=ConfirmationStore())
        result = loop.run("填写留言",
                          confirm_provider=lambda p, o, t: t)
        # A confirmed third-party input is applied to the real (non-dry-run) page.
        self.assertNotEqual(result.status, "confirmation_required")
        self.assertTrue(any(step.operation == WebOperation.TYPE_TEXT for step in result.steps))
        self.assertFalse(any(step.dry_run for step in result.steps))
        self.assertEqual(backend._pages["cart"].elements[0]["value"], "尽快发货")

    def test_user_declines_confirmation_no_action(self):
        backend = FakeDomBackend(shopping_pages(), "cart")
        script = [{"operation": op_answer("type_text"),
                   "type_text_target": target_answer("e001", ["e001", "e002", "none"]),
                   "text_candidate": {"choice": "c0", "confidence": 0.9,
                                      "probabilities": probs("c0", ["c0", "none"])}}]
        loop, _ = make_loop(backend, script, dry_run=False,
                            candidates={"c0": "hi"},
                            confirm_store=ConfirmationStore())
        result = loop.run("填写留言", confirm_provider=lambda p, o, t: None)
        self.assertEqual(result.status, "confirmation_required")
        self.assertNotIn("value", backend._pages["cart"].elements[0])


class CancellationTests(unittest.TestCase):
    def test_cancel_before_route_commits_nothing(self):
        backend = FakeDomBackend(shopping_pages(), "cart")
        script = [{"operation": op_answer("click"),
                   "click_target": target_answer("e002", ["e001", "e002", "none"])}]
        loop, _ = make_loop(backend, script, dry_run=False)
        token = CancellationToken()
        token.cancel()
        with self.assertRaises(VoiceCancelled):
            loop.run("提交订单", cancellation=token)
        self.assertEqual(backend.act_log, [])


class StaleTargetTests(unittest.TestCase):
    def test_target_not_in_fresh_observation_is_rejected(self):
        backend = FakeDomBackend(shopping_pages(), "cart")
        # Chooser picks e5 which the observation (only e1,e2) never contained.
        script = [{"operation": op_answer("click"),
                   "click_target": target_answer("e005", ["e001", "e002", "none"])}]
        loop, _ = make_loop(backend, script, dry_run=False)
        result = loop.run("点那个")
        self.assertFalse(result.completed)
        self.assertEqual(backend.act_log, [])


class BudgetTests(unittest.TestCase):
    def test_step_limit_stops_a_non_progressing_loop(self):
        backend = FakeDomBackend(shopping_pages(), "cart")
        # The chooser keeps saying click e1 (a read button, allowed) forever.
        script = [{"operation": op_answer("click"),
                   "click_target": target_answer("e001", ["e001", "e002", "none"])}]
        loop, _ = make_loop(backend, script, dry_run=False, max_steps=3)
        result = loop.run("一直点")
        self.assertEqual(result.status, "step_limit")
        self.assertEqual(result.error_code, WebErrorCode.BUDGET_EXHAUSTED)
        self.assertLessEqual(len(result.steps), 3)


class UnsupportedSurfaceTests(unittest.TestCase):
    def test_iframe_is_reported_not_degraded(self):
        pages = {
            "p": page("p", "https://x.example", title="t", text="",
                      elements=[{"unsupported_surface": "iframe"},
                                {"backend_id": "n1", "role": "button", "label": "OK"}]),
        }
        backend = FakeDomBackend(pages, "p")
        script = [{"operation": op_answer("done")}]
        loop, _ = make_loop(backend, script, dry_run=False)
        result = loop.run("g")
        self.assertTrue(any(item.kind == "iframe" for item in result.unsupported))


if __name__ == "__main__":
    unittest.main()
