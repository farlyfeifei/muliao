from __future__ import annotations

from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from voice.web_confirm import ConfirmationStore
from voice.web_contracts import (
    ActionProposal,
    EffectClass,
    Observation,
    WebErrorCode,
    WebOperation,
    WebTarget,
    text_hash,
)


class MutableClock:
    def __init__(self, value: float = 100.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


def make_observation(**overrides) -> Observation:
    base = {
        "observation_id": "obs-1",
        "page_revision": "rev-1",
        "session_id": "sess-1",
        "tab_id": "tab-1",
        "origin": "https://shop.example",
        "title": "Cart",
        "targets": (WebTarget("e1", role="button", label="Place order"),),
    }
    base.update(overrides)
    return Observation(**base)


def submit_proposal() -> ActionProposal:
    return ActionProposal("obs-1", "rev-1", WebOperation.CLICK, target_id="e1")


def type_proposal(candidate="c0") -> ActionProposal:
    return ActionProposal(
        "obs-1", "rev-1", WebOperation.TYPE_TEXT, target_id="e1", text_candidate_id=candidate
    )


def store_with_clock(clock=None):
    clock = clock or MutableClock()
    return ConfirmationStore(clock=clock), clock


class IssueTests(unittest.TestCase):
    def test_issue_binds_plan_and_stores_only_hash(self):
        store, _ = store_with_clock()
        observation = make_observation()
        token = store.issue(
            operation_run_id="run-1",
            observation=observation,
            proposal=type_proposal(),
            effect_class=EffectClass.INPUT,
            input_text="给客服的留言",
        )
        self.assertEqual(token.input_text_hash, text_hash("给客服的留言"))
        self.assertNotIn("留言", token.input_text_hash)
        self.assertEqual(token.origin, "https://shop.example")
        self.assertEqual(token.page_revision, "rev-1")

    def test_only_mutating_operations_can_be_confirmed(self):
        store, _ = store_with_clock()
        with self.assertRaisesRegex(ValueError, "mutating"):
            store.issue(
                operation_run_id="run-1",
                observation=make_observation(),
                proposal=ActionProposal("obs-1", "rev-1", WebOperation.SCROLL_DOWN),
            )

    def test_rejects_nonpositive_ttl(self):
        store, _ = store_with_clock()
        with self.assertRaisesRegex(ValueError, "ttl"):
            store.issue(
                operation_run_id="run-1",
                observation=make_observation(),
                proposal=submit_proposal(),
                ttl=0,
            )


class ConsumeTests(unittest.TestCase):
    def issue(self, store, **kwargs):
        observation = kwargs.pop("observation", make_observation())
        proposal = kwargs.pop("proposal", submit_proposal())
        token = store.issue(
            operation_run_id=kwargs.pop("run", "run-1"),
            observation=observation,
            proposal=proposal,
            effect_class=kwargs.pop("effect", EffectClass.SUBMIT),
            input_text=kwargs.pop("input_text", None),
            ttl=kwargs.pop("ttl", None),
        )
        return token, observation, proposal

    def test_happy_path_consumes_once(self):
        store, _ = store_with_clock()
        token, observation, proposal = self.issue(store)
        ok, code = store.consume(token.token_id, observation, proposal)
        self.assertTrue(ok)
        self.assertEqual(code, "")

    def test_one_time_reuse_is_rejected(self):
        store, _ = store_with_clock()
        token, observation, proposal = self.issue(store)
        self.assertTrue(store.consume(token.token_id, observation, proposal)[0])
        ok, code = store.consume(token.token_id, observation, proposal)
        self.assertFalse(ok)
        self.assertEqual(code, WebErrorCode.CONFIRM_USED)

    def test_unknown_token_is_mismatch(self):
        store, _ = store_with_clock()
        ok, code = store.consume("nope", make_observation(), submit_proposal())
        self.assertFalse(ok)
        self.assertEqual(code, WebErrorCode.CONFIRM_MISMATCH)

    def test_expiry(self):
        clock = MutableClock()
        store = ConfirmationStore(clock=clock)
        token, observation, proposal = self.issue(store, ttl=30.0)
        clock.advance(31.0)
        ok, code = store.consume(token.token_id, observation, proposal)
        self.assertFalse(ok)
        self.assertEqual(code, WebErrorCode.CONFIRM_EXPIRED)

    def test_not_expired_within_ttl(self):
        clock = MutableClock()
        store = ConfirmationStore(clock=clock)
        token, observation, proposal = self.issue(store, ttl=30.0)
        clock.advance(29.0)
        self.assertTrue(store.consume(token.token_id, observation, proposal)[0])

    def test_page_revision_change_invalidates_grant_as_stale(self):
        store, _ = store_with_clock()
        token, observation, proposal = self.issue(store)
        # Re-observe after confirmation produced a new document revision.
        reobserved = make_observation(observation_id="obs-2", page_revision="rev-2")
        fresh_proposal = ActionProposal("obs-2", "rev-2", WebOperation.CLICK, target_id="e1")
        ok, code = store.consume(token.token_id, reobserved, fresh_proposal)
        self.assertFalse(ok)
        self.assertEqual(code, WebErrorCode.TARGET_STALE)

    def test_origin_change_invalidates_grant(self):
        store, _ = store_with_clock()
        token, observation, proposal = self.issue(store)
        moved = make_observation(origin="https://other.example")
        ok, code = store.consume(token.token_id, moved, proposal)
        self.assertFalse(ok)
        self.assertEqual(code, WebErrorCode.ORIGIN_CHANGED)

    def test_different_target_is_mismatch(self):
        store, _ = store_with_clock()
        observation = make_observation(
            targets=(WebTarget("e1", role="button", label="A"), WebTarget("e2", role="button", label="B"))
        )
        token = store.issue(
            operation_run_id="run-1",
            observation=observation,
            proposal=ActionProposal("obs-1", "rev-1", WebOperation.CLICK, target_id="e1"),
            effect_class=EffectClass.SUBMIT,
        )
        ok, code = store.consume(
            token.token_id, observation,
            ActionProposal("obs-1", "rev-1", WebOperation.CLICK, target_id="e2"),
        )
        self.assertFalse(ok)
        self.assertEqual(code, WebErrorCode.CONFIRM_MISMATCH)

    def test_changed_input_text_is_mismatch(self):
        store, _ = store_with_clock()
        token, observation, proposal = self.issue(
            store, proposal=type_proposal(), effect=EffectClass.INPUT, input_text="原始留言"
        )
        ok, code = store.consume(token.token_id, observation, proposal, input_text="被篡改的留言")
        self.assertFalse(ok)
        self.assertEqual(code, WebErrorCode.CONFIRM_MISMATCH)

    def test_matching_input_text_consumes(self):
        store, _ = store_with_clock()
        token, observation, proposal = self.issue(
            store, proposal=type_proposal(), effect=EffectClass.INPUT, input_text="原始留言"
        )
        ok, _ = store.consume(token.token_id, observation, proposal, input_text="原始留言")
        self.assertTrue(ok)


class RevokeTests(unittest.TestCase):
    def test_revoke_single_run(self):
        store, _ = store_with_clock()
        t1 = store.issue(operation_run_id="run-1", observation=make_observation(), proposal=submit_proposal())
        store.issue(operation_run_id="run-2", observation=make_observation(), proposal=submit_proposal())
        self.assertEqual(store.revoke("run-1"), 1)
        ok, code = store.consume(t1.token_id, make_observation(), submit_proposal())
        self.assertFalse(ok)
        self.assertEqual(code, WebErrorCode.CONFIRM_MISMATCH)

    def test_revoke_all(self):
        store, _ = store_with_clock()
        store.issue(operation_run_id="run-1", observation=make_observation(), proposal=submit_proposal())
        store.issue(operation_run_id="run-2", observation=make_observation(), proposal=submit_proposal())
        self.assertEqual(store.revoke(), 2)
        self.assertIsNone(store.pending("run-1"))

    def test_pending_returns_token_without_plaintext(self):
        store, _ = store_with_clock()
        store.issue(
            operation_run_id="run-1",
            observation=make_observation(),
            proposal=type_proposal(),
            effect_class=EffectClass.INPUT,
            input_text="秘密留言",
        )
        token = store.pending("run-1")
        self.assertIsNotNone(token)
        self.assertEqual(token.input_text_hash, text_hash("秘密留言"))
        self.assertNotIn("秘密", repr(token))


class DescribeTests(unittest.TestCase):
    def test_describe_names_origin_and_action_without_full_url_or_plaintext(self):
        store, _ = store_with_clock()
        observation = make_observation(origin="https://shop.example")
        token = store.issue(
            operation_run_id="run-1",
            observation=observation,
            proposal=type_proposal(),
            effect_class=EffectClass.INPUT,
            input_text="我的银行卡密码是1234",
        )
        text = store.describe_for_user(token, observation)
        self.assertIn("shop.example", text)
        self.assertIn("填写", text)
        self.assertIn("Place order", text)
        self.assertNotIn("1234", text)
        self.assertNotIn("密码", text)
        # origin only, never a path/query
        self.assertNotIn("?", text)


if __name__ == "__main__":
    unittest.main()
