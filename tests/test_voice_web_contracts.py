from __future__ import annotations

from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from voice.web_contracts import (
    ActionProposal,
    CandidateSource,
    Channel,
    CommitState,
    ConfirmationToken,
    EffectClass,
    MAX_TARGETS,
    Observation,
    PolicyDecision,
    TextCandidate,
    VerificationCheck,
    VerificationResult,
    WebErrorCode,
    WebGoalError,
    WebOperation,
    WebTarget,
    text_hash,
)


def make_observation(**overrides) -> Observation:
    base = {
        "observation_id": "obs-1",
        "page_revision": "rev-1",
        "session_id": "sess-1",
        "tab_id": "tab-1",
        "origin": "https://example.com",
        "title": "Example",
        "targets": (WebTarget("e1", role="button", label="Submit"),),
    }
    base.update(overrides)
    return Observation(**base)


class ConstantTests(unittest.TestCase):
    def test_namespaces_are_stable(self):
        self.assertEqual(Channel.BROWSER, "browser")
        self.assertIn(WebOperation.TYPE_TEXT, WebOperation.ALL)
        self.assertIn(WebOperation.TYPE_TEXT, WebOperation.TEXT_BEARING)
        self.assertIn(WebOperation.CLICK, WebOperation.TARGETED)
        self.assertNotIn(WebOperation.SCROLL_DOWN, WebOperation.TARGETED)
        self.assertEqual(
            set(PolicyDecision.ALL),
            {"allow", "require_confirmation", "needs_input", "block"},
        )
        self.assertIn("unknown", CommitState.ALL)


class HashTests(unittest.TestCase):
    def test_text_hash_is_deterministic_and_not_the_plaintext(self):
        digest = text_hash("给客服的留言")
        self.assertEqual(digest, text_hash("给客服的留言"))
        self.assertNotEqual(digest, text_hash("别的留言"))
        self.assertEqual(len(digest), 64)
        self.assertNotIn("留言", digest)


class WebTargetTests(unittest.TestCase):
    def test_requires_non_empty_id(self):
        with self.assertRaisesRegex(ValueError, "target_id"):
            WebTarget("   ")

    def test_label_is_truncated_and_cleaned(self):
        target = WebTarget("e1", label="  很长  的标签  " + "x" * 200)
        self.assertLessEqual(len(target.label), 60)
        self.assertNotIn("  ", target.label)

    def test_as_jev_element_has_no_coordinates(self):
        line = WebTarget("e7", role="textbox", label="评论", editable=True).as_jev_element()
        self.assertIn("e7", line)
        self.assertIn("role=textbox", line)
        self.assertIn("editable", line)
        self.assertNotIn("x=", line)
        self.assertNotIn("y=", line)


class ObservationTests(unittest.TestCase):
    def test_rejects_duplicate_target_ids(self):
        with self.assertRaisesRegex(ValueError, "duplicate target_id"):
            make_observation(targets=(WebTarget("e1"), WebTarget("e1")))

    def test_requires_identity_fields(self):
        for field_name in ("observation_id", "page_revision", "session_id", "tab_id"):
            with self.subTest(field=field_name):
                with self.assertRaisesRegex(ValueError, field_name):
                    make_observation(**{field_name: ""})

    def test_truncates_targets_and_counts_omitted(self):
        many = tuple(WebTarget(f"e{i}") for i in range(MAX_TARGETS + 5))
        observation = make_observation(targets=many)
        self.assertEqual(len(observation.targets), MAX_TARGETS)
        self.assertEqual(observation.omitted_target_count, 5)

    def test_target_lookup(self):
        observation = make_observation()
        self.assertIsNotNone(observation.target("e1"))
        self.assertIsNone(observation.target("e999"))

    def test_jev_state_excludes_full_url_and_carries_origin_only(self):
        observation = make_observation(
            title="Example",
            text="可见正文",
        )
        state = observation.to_jev_state(
            "提交评论",
            candidates={"c0": "你好"},
            recent_actions=[{"said": "打开网页"}],
        )
        page = state["page"]
        self.assertEqual(page["origin"], "https://example.com")
        self.assertEqual(page["title"], "Example")
        self.assertNotIn("url", page)
        self.assertEqual(state["candidates"], {"c0": "你好"})
        self.assertEqual(state["elements"], ["e1 | role=button | label=Submit | state=enabled,visible"])

    def test_recent_actions_are_capped_at_three(self):
        state = make_observation().to_jev_state(
            "g",
            recent_actions=[{"i": i} for i in range(10)],
        )
        self.assertEqual(len(state["recent_actions"]), 3)


class TextCandidateTests(unittest.TestCase):
    def test_rejects_unknown_source(self):
        with self.assertRaisesRegex(ValueError, "candidate source"):
            TextCandidate("c0", "text", source="model_generated")

    def test_accepts_the_three_legal_sources(self):
        for source in CandidateSource.ALL:
            TextCandidate("c0", "x", source=source)


class ActionProposalTests(unittest.TestCase):
    def test_click_requires_target(self):
        proposal = ActionProposal("obs-1", "rev-1", WebOperation.CLICK)
        self.assertEqual(proposal.validate(), WebErrorCode.INVALID_PROPOSAL)

    def test_type_text_without_candidate_needs_input_not_fabrication(self):
        proposal = ActionProposal("obs-1", "rev-1", WebOperation.TYPE_TEXT, target_id="e1")
        self.assertEqual(proposal.validate(), WebErrorCode.NEEDS_INPUT)

    def test_type_text_with_candidate_is_valid(self):
        proposal = ActionProposal(
            "obs-1", "rev-1", WebOperation.TYPE_TEXT,
            target_id="e1", text_candidate_id="c0",
        )
        self.assertIsNone(proposal.validate())

    def test_navigate_is_text_bearing(self):
        self.assertIsNone(
            ActionProposal("obs-1", "rev-1", WebOperation.NAVIGATE, text_candidate_id="c0").validate()
        )
        self.assertEqual(
            ActionProposal("obs-1", "rev-1", WebOperation.NAVIGATE).validate(),
            WebErrorCode.NEEDS_INPUT,
        )

    def test_select_requires_option(self):
        self.assertEqual(
            ActionProposal("obs-1", "rev-1", WebOperation.SELECT, target_id="e1").validate(),
            WebErrorCode.INVALID_PROPOSAL,
        )

    def test_unknown_operation_is_invalid(self):
        self.assertEqual(
            ActionProposal("obs-1", "rev-1", "execute_js").validate(),
            WebErrorCode.INVALID_PROPOSAL,
        )

    def test_binds_rejects_stale_observation_or_revision(self):
        observation = make_observation()
        good = ActionProposal("obs-1", "rev-1", WebOperation.CLICK, target_id="e1")
        self.assertTrue(good.binds(observation))
        self.assertFalse(
            ActionProposal("obs-1", "rev-2", WebOperation.CLICK, target_id="e1").binds(observation)
        )
        self.assertFalse(
            ActionProposal("obs-9", "rev-1", WebOperation.CLICK, target_id="e1").binds(observation)
        )

    def test_binds_rejects_target_missing_from_current_observation(self):
        observation = make_observation()
        stale = ActionProposal("obs-1", "rev-1", WebOperation.CLICK, target_id="e404")
        self.assertFalse(stale.binds(observation))


class ConfirmationTokenTests(unittest.TestCase):
    def token(self, **overrides) -> ConfirmationToken:
        base = {
            "token_id": "tok-1",
            "operation_run_id": "run-1",
            "session_id": "sess-1",
            "tab_id": "tab-1",
            "origin": "https://example.com",
            "observation_id": "obs-1",
            "page_revision": "rev-1",
            "target_id": "e1",
            "operation": WebOperation.CLICK,
            "input_text_hash": text_hash("hello"),
            "effect_class": EffectClass.SUBMIT,
        }
        base.update(overrides)
        return ConfirmationToken(**base)

    def test_requires_identity_fields(self):
        with self.assertRaisesRegex(ValueError, "token_id"):
            self.token(token_id="")

    def test_fingerprint_changes_with_every_bound_field(self):
        base = self.token().plan_fingerprint()
        for field_name, new_value in (
            ("operation_run_id", "run-2"),
            ("session_id", "sess-2"),
            ("tab_id", "tab-2"),
            ("origin", "https://other.com"),
            ("observation_id", "obs-2"),
            ("page_revision", "rev-2"),
            ("target_id", "e2"),
            ("operation", WebOperation.TYPE_TEXT),
            ("input_text_hash", text_hash("different")),
            ("effect_class", EffectClass.INPUT),
        ):
            with self.subTest(field=field_name):
                self.assertNotEqual(base, self.token(**{field_name: new_value}).plan_fingerprint())

    def test_fingerprint_for_matches_instance(self):
        token = self.token()
        self.assertEqual(
            token.plan_fingerprint(),
            ConfirmationToken.fingerprint_for(
                operation_run_id="run-1", session_id="sess-1", tab_id="tab-1",
                origin="https://example.com", observation_id="obs-1", page_revision="rev-1",
                target_id="e1", operation=WebOperation.CLICK,
                input_text_hash=text_hash("hello"), effect_class=EffectClass.SUBMIT,
            ),
        )

    def test_rejects_nonpositive_ttl(self):
        with self.assertRaisesRegex(ValueError, "expires_after_seconds"):
            self.token(expires_after_seconds=0)


class VerificationTests(unittest.TestCase):
    def test_three_states_and_satisfied_flag(self):
        satisfied = VerificationResult("satisfied", (VerificationCheck("url", "a", "a", True),))
        self.assertTrue(satisfied.satisfied)
        self.assertFalse(VerificationResult("unverified").satisfied)
        self.assertFalse(VerificationResult("contradicted").satisfied)

    def test_rejects_unknown_status_or_commit_state(self):
        with self.assertRaisesRegex(ValueError, "verification status"):
            VerificationResult("maybe")
        with self.assertRaisesRegex(ValueError, "commit state"):
            VerificationResult("satisfied", commit_state="probably")

    def test_check_requires_kind(self):
        with self.assertRaisesRegex(ValueError, "kind"):
            VerificationCheck("", "a", "a")


class WebGoalErrorTests(unittest.TestCase):
    def test_rejects_unknown_code(self):
        with self.assertRaisesRegex(ValueError, "error code"):
            WebGoalError("made_up_code")

    def test_accepts_known_codes_and_strips_detail(self):
        error = WebGoalError(WebErrorCode.TARGET_STALE, "  node  changed  ")
        self.assertEqual(error.code, "target_stale")
        self.assertEqual(error.detail, "node changed")


if __name__ == "__main__":
    unittest.main()
