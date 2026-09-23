from __future__ import annotations

from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from voice.web_contracts import MAX_TARGETS, WebOperation
from voice.web_observation import ObservationBuilder, UnsupportedSurface


def builder() -> ObservationBuilder:
    return ObservationBuilder(session_id="sess-1", tab_id="tab-1")


def build(b, elements, *, token="doc-1", **kwargs):
    return b.build(
        observation_id=kwargs.pop("observation_id", "obs-1"),
        origin=kwargs.pop("origin", "https://example.com"),
        title=kwargs.pop("title", "Example"),
        text=kwargs.pop("text", ""),
        raw_elements=elements,
        document_token=token,
        **kwargs,
    )


class BuilderIdentityTests(unittest.TestCase):
    def test_requires_session_and_tab(self):
        with self.assertRaisesRegex(ValueError, "session_id"):
            ObservationBuilder(session_id="", tab_id="t")
        with self.assertRaisesRegex(ValueError, "session_id"):
            ObservationBuilder(session_id="s", tab_id="  ")

    def test_build_requires_a_document_first(self):
        with self.assertRaisesRegex(ValueError, "begin_document"):
            builder().build(
                observation_id="obs-1",
                origin="https://example.com",
                raw_elements=(),
            )

    def test_same_document_token_keeps_revision_stable(self):
        b = builder()
        first = b.begin_document("doc-1")
        second = b.begin_document("doc-1")
        self.assertEqual(first, second)

    def test_navigation_mints_a_new_page_revision(self):
        b = builder()
        first = b.begin_document("doc-1")
        second = b.begin_document("doc-2")
        self.assertNotEqual(first, second)
        self.assertEqual(b.page_revision, second)

    def test_revision_differs_across_tabs_for_same_document(self):
        a = ObservationBuilder(session_id="s", tab_id="tab-1").begin_document("doc")
        c = ObservationBuilder(session_id="s", tab_id="tab-2").begin_document("doc")
        self.assertNotEqual(a, c)


class NumberingTests(unittest.TestCase):
    def test_target_ids_are_sequential_and_observation_local(self):
        b = builder()
        observation, _ = build(
            b,
            [
                {"role": "button", "label": "Save"},
                {"role": "link", "label": "Home"},
            ],
        )
        self.assertEqual([t.target_id for t in observation.targets], ["e001", "e002"])

    def test_numbering_restarts_after_navigation(self):
        b = builder()
        build(b, [{"role": "button", "label": "A"}], token="doc-1")
        second, _ = build(b, [{"role": "button", "label": "B"}], token="doc-2")
        self.assertEqual([t.target_id for t in second.targets], ["e001"])

    def test_targets_are_capped_and_overflow_is_counted(self):
        b = builder()
        many = [{"role": "button", "label": f"b{i}"} for i in range(MAX_TARGETS + 7)]
        observation, _ = build(b, many)
        self.assertEqual(len(observation.targets), MAX_TARGETS)
        self.assertEqual(observation.omitted_target_count, 7)


class PrivacyFilterTests(unittest.TestCase):
    def test_password_and_file_inputs_never_become_targets(self):
        b = builder()
        observation, _ = build(
            b,
            [
                {"role": "textbox", "label": "Password", "input_type": "password"},
                {"role": "textbox", "label": "Upload", "input_type": "file"},
                {"role": "textbox", "label": "hidden", "input_type": "hidden"},
                {"role": "button", "label": "Sign in"},
            ],
        )
        self.assertEqual([t.label for t in observation.targets], ["Sign in"])

    def test_sensitive_labels_are_dropped(self):
        b = builder()
        observation, _ = build(
            b,
            [
                {"role": "textbox", "label": "信用卡号"},
                {"role": "textbox", "label": "one-time code"},
                {"role": "button", "label": "验证码"},
                {"role": "button", "label": "Continue"},
            ],
        )
        self.assertEqual([t.label for t in observation.targets], ["Continue"])

    def test_elements_flagged_sensitive_are_dropped(self):
        b = builder()
        observation, _ = build(
            b,
            [
                {"role": "button", "label": "Pay", "sensitive": True},
                {"role": "button", "label": "Help"},
            ],
        )
        self.assertEqual([t.label for t in observation.targets], ["Help"])

    def test_non_interactive_and_invisible_elements_are_dropped(self):
        b = builder()
        observation, _ = build(
            b,
            [
                {"role": "heading", "label": "Title"},
                {"role": "button", "label": "Hidden", "visible": False},
                {"role": "button", "label": "Shown"},
            ],
        )
        self.assertEqual([t.label for t in observation.targets], ["Shown"])


class UnsupportedSurfaceTests(unittest.TestCase):
    def test_iframe_shadow_and_canvas_are_reported_not_degraded(self):
        b = builder()
        observation, unsupported = build(
            b,
            [
                {"unsupported_surface": "iframe", "detail": "cross-origin frame"},
                {"unsupported_surface": "shadow-dom"},
                {"unsupported_surface": "canvas"},
                {"role": "button", "label": "OK"},
            ],
        )
        self.assertEqual(
            sorted(item.kind for item in unsupported),
            ["canvas", "iframe", "shadow-dom"],
        )
        self.assertTrue(all(isinstance(item, UnsupportedSurface) for item in unsupported))
        # Reported surfaces must not silently become clickable coordinates.
        self.assertEqual([t.label for t in observation.targets], ["OK"])

    def test_detail_is_bounded(self):
        b = builder()
        _, unsupported = build(b, [{"unsupported_surface": "iframe", "detail": "x" * 500}])
        self.assertLessEqual(len(unsupported[0].detail), 120)


class RoleOperationTests(unittest.TestCase):
    def test_role_determines_supported_operations(self):
        b = builder()
        observation, _ = build(
            b,
            [
                {"role": "button", "label": "Go"},
                {"role": "textbox", "label": "Comment"},
                {"role": "combobox", "label": "Sort"},
                {"role": "select", "label": "Country"},
            ],
        )
        by_label = {t.label: t for t in observation.targets}
        self.assertEqual(by_label["Go"].operations, (WebOperation.CLICK,))
        self.assertIn(WebOperation.TYPE_TEXT, by_label["Comment"].operations)
        self.assertTrue(by_label["Comment"].editable)
        self.assertIn(WebOperation.SELECT, by_label["Sort"].operations)
        self.assertEqual(by_label["Country"].operations, (WebOperation.SELECT,))

    def test_checked_and_selected_are_tristate(self):
        b = builder()
        observation, _ = build(
            b,
            [
                {"role": "checkbox", "label": "Agree", "checked": True},
                {"role": "tab", "label": "Second", "selected": False},
                {"role": "button", "label": "Plain"},
            ],
        )
        by_label = {t.label: t for t in observation.targets}
        self.assertIs(by_label["Agree"].checked, True)
        self.assertIs(by_label["Second"].selected, False)
        self.assertIsNone(by_label["Plain"].checked)

    def test_metadata_carries_code_owned_node_handle(self):
        b = builder()
        observation, _ = build(b, [{"role": "button", "label": "A", "backend_id": "backend-9"}])
        target = observation.targets[0]
        self.assertEqual(target.metadata["backend_id"], "backend-9")
        self.assertIsInstance(target.metadata["node"], int)


class ObservationContractTests(unittest.TestCase):
    def test_built_observation_carries_required_identity_fields(self):
        b = builder()
        observation, _ = build(
            b,
            [{"role": "button", "label": "A"}],
            origin="https://shop.example",
            title="Cart",
            text="visible text",
            loading_state="complete",
        )
        self.assertEqual(observation.session_id, "sess-1")
        self.assertEqual(observation.tab_id, "tab-1")
        self.assertEqual(observation.origin, "https://shop.example")
        self.assertEqual(observation.title, "Cart")
        self.assertEqual(observation.loading_state, "complete")
        self.assertTrue(observation.page_revision)
        self.assertTrue(observation.target("e001"))

    def test_target_lookup_returns_none_for_stale_id(self):
        b = builder()
        observation, _ = build(b, [{"role": "button", "label": "A"}])
        self.assertIsNone(observation.target("e999"))


if __name__ == "__main__":
    unittest.main()
