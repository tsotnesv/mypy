from __future__ import annotations

import unittest

from mypy.server.explain import (
    CausalLink,
    ExplanationRecorder,
    InvalidationReason,
    RecheckedTarget,
    SourceLocation,
    UpdateExplanation,
    filter_explanation_for_target,
    render_reverse,
    render_tree,
)


class ExplainSuite(unittest.TestCase):
    def _build_chain(self) -> UpdateExplanation:
        mod = RecheckedTarget(
            target="app.models",
            kind="module",
            location=SourceLocation("app/models.py", 1, 0),
            causes=[CausalLink(InvalidationReason.FILE_MODIFIED, "Source file modified on disk")],
            signature_changed=True,
            emitted_triggers=["<app.models.User.rate>"],
        )
        attr = RecheckedTarget(
            target="app.models.User.rate",
            kind="attribute",
            location=SourceLocation("app/models.py", 9, 4),
            causes=[
                CausalLink(
                    InvalidationReason.SYMBOL_CHANGED,
                    "type changed (builtins.float -> decimal.Decimal)",
                    trigger="<app.models.User.rate>",
                    upstream_target="app.models",
                )
            ],
            signature_changed=True,
            emitted_triggers=["<app.models.User.rate>"],
        )
        caller = RecheckedTarget(
            target="app.billing.total",
            kind="function",
            location=SourceLocation("app/billing.py", 5, 0),
            causes=[
                CausalLink(
                    InvalidationReason.TRIGGER_FIRED,
                    "depends on <app.models.User.rate>",
                    trigger="<app.models.User.rate>",
                    upstream_target="app.models.User.rate",
                )
            ],
            signature_changed=False,
        )
        unrelated = RecheckedTarget(
            target="app.other",
            kind="module",
            location=SourceLocation("app/other.py", 1, 0),
            causes=[CausalLink(InvalidationReason.FILE_MODIFIED, "Source file modified on disk")],
        )
        return UpdateExplanation(
            update_id=2,
            changed_files=["app/models.py", "app/other.py"],
            removed_files=[],
            targets={
                "app.models": mod,
                "app.models.User.rate": attr,
                "app.billing.total": caller,
                "app.other": unrelated,
            },
            duration_ms=12.0,
        )

    def test_roundtrip_dict(self) -> None:
        exp = self._build_chain()
        restored = UpdateExplanation.from_dict(exp.to_dict()) # type: ignore[arg-type]
        assert restored == exp

    def test_reverse_causal_query_filters_unrelated(self) -> None:
        exp = self._build_chain()
        filtered = filter_explanation_for_target(exp, "app.billing.total")
        assert filtered.rechecked is True
        assert set(filtered.targets.keys()) == {
            "app.models",
            "app.models.User.rate",
            "app.billing.total",
        }
        assert filtered.changed_files == ["app/models.py"]
        rev = render_reverse(filtered)
        assert "app.billing.total (app/billing.py:5)" in rev
        assert "app.models.User.rate (app/models.py:9)" in rev
        assert "app.other" not in rev

    def test_reverse_causal_query_untouched_target(self) -> None:
        exp = self._build_chain()
        filtered = filter_explanation_for_target(exp, "app.shipping.estimate")
        assert filtered.rechecked is False
        assert "was not rechecked in Update #2" in render_reverse(filtered)

    def test_cycle_and_diamond_rendering(self) -> None:
        a = RecheckedTarget(
            target="pkg.a",
            kind="function",
            location=SourceLocation("pkg.py", 1, 0),
            causes=[
                CausalLink(InvalidationReason.FILE_MODIFIED, "modified"),
                CausalLink(
                    InvalidationReason.TRIGGER_FIRED,
                    "depends on <pkg.b>",
                    trigger="<pkg.b>",
                    upstream_target="pkg.b",
                ),
            ],
            signature_changed=True,
        )
        b = RecheckedTarget(
            target="pkg.b",
            kind="function",
            location=SourceLocation("pkg.py", 5, 0),
            causes=[
                CausalLink(
                    InvalidationReason.TRIGGER_FIRED,
                    "depends on <pkg.a>",
                    trigger="<pkg.a>",
                    upstream_target="pkg.a",
                )
            ],
            signature_changed=True,
        )
        exp = UpdateExplanation(
            update_id=3,
            changed_files=["pkg.py"],
            removed_files=[],
            targets={"pkg.a": a, "pkg.b": b},
        )
        tree = render_tree(exp)
        assert "(cycle -> pkg.a)" in tree
        
    def test_render_tree_no_duplicate_trigger_on_symbol_diff(self) -> None:
        exp = self._build_chain()
        rendered = render_tree(exp)
        # Trigger <app.models.User.rate> should appear once (on the trigger_fired edge
        # to app.billing.total), not twice before and after [symbol_changed].
        assert rendered.count("Trigger <app.models.User.rate>") == 1, rendered
        assert "[symbol_changed] app.models.User.rate (app/models.py:9)" in rendered
        assert "[trigger_fired] app.billing.total (app/billing.py:5)" in rendered

    def test_render_tree_max_depth_truncation(self) -> None:
        exp = self._build_chain()
        rendered = render_tree(exp, max_depth=1)
        assert "[symbol_changed] app.models.User.rate (app/models.py:9)" in rendered
        assert "downstream target(s) hidden by --depth 1" in rendered
        assert "app.billing.total" not in rendered

    def test_render_reverse_chain(self) -> None:
        exp = self._build_chain()
        filtered = filter_explanation_for_target(exp, "app.billing.total")
        rendered = render_reverse(filtered)
        # Verify bottom-up ordering: app.billing.total -> app.models.User.rate -> app.models
        idx_target = rendered.index("app.billing.total (app/billing.py:5)")
        idx_symbol = rendered.index("app.models.User.rate (app/models.py:9)")
        idx_file = rendered.index("app.models (app/models.py:1)")
        assert idx_target < idx_symbol < idx_file

    def test_recorder_skips_self_trigger_and_duplicate_edges(self) -> None:
        rec = ExplanationRecorder(update_id=5, is_initial=False)
        root_target = rec._get_or_create_target("a.X")
        root_target.add_cause(
            CausalLink(
                reason=InvalidationReason.SYMBOL_CHANGED,
                detail="type changed (int -> str)",
                trigger="<a.X>",
            )
        )
        rec.trigger_origins["<a.X>"] = "a.X"

        # Simulate propagation firing <a.X> back onto a.X and twice onto b.f
        rec.record_trigger_propagation("<a.X>", "a.X")
        rec.record_trigger_propagation("<a.X>", "b.f")
        rec.record_trigger_propagation("<a.X>", "b.f")
        exp = rec.finalize({})

        # a.X must not gain a redundant trigger_fired self-cause
        assert all(
            c.reason != InvalidationReason.TRIGGER_FIRED
            for c in exp.targets["a.X"].causes
        )
        # b.f must only record <a.X> once
        b_causes = exp.targets["b.f"].causes
        assert len(b_causes) == 1
        assert b_causes[0].trigger == "<a.X>"