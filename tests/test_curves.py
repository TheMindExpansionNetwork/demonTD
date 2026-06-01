"""Unit tests for the curve parser + evaluator used by demon_ext's
client-side scheduled curves.

These live as standalone module-level functions on demon_ext so they're
testable without TD globals. The DemonExt class wraps them with the
caching + manual-override logic.
"""

from __future__ import annotations

import pytest


# Defer import until inside test fns so an import failure here gives a
# clear pytest collection error rather than a top-level crash.
def _import_curves():
    import demon_ext  # noqa: F401
    return demon_ext


def test_curve_parses_valid_linear_points():
    """{"points": [...]} with a list of [x, y] floats parses to a
    sorted, endpoint-clamped list of tuples."""
    dx = _import_curves()
    pts = dx.parse_curve_spec('{"points": [[0, 0.0], [0.5, 1.0], [1, 0.3]]}')
    assert pts is not None
    assert len(pts) == 3
    assert pts[0] == (0.0, 0.0)
    assert pts[1] == (0.5, 1.0)
    assert pts[2] == (1.0, 0.3)


def test_curve_clamps_endpoints_to_x_0_and_1():
    """First point's x is forced to 0, last point's x is forced to 1
    so the [0, 1] domain is always covered."""
    dx = _import_curves()
    pts = dx.parse_curve_spec('{"points": [[0.1, 0.2], [0.5, 0.9], [0.95, 0.4]]}')
    assert pts is not None
    assert pts[0][0] == 0.0
    assert pts[-1][0] == 1.0
    # y values should be preserved.
    assert pts[0][1] == pytest.approx(0.2)
    assert pts[-1][1] == pytest.approx(0.4)


def test_curve_sorts_points_by_x():
    """Points provided out of x-order are sorted on parse so the
    evaluator can rely on monotonic x."""
    dx = _import_curves()
    pts = dx.parse_curve_spec(
        '{"points": [[1, 1.0], [0.5, 0.5], [0, 0.0], [0.25, 0.25]]}')
    assert pts is not None
    xs = [p[0] for p in pts]
    assert xs == sorted(xs)


def test_curve_invalid_spec_returns_none():
    """Anything that isn't a dict with a valid `points` list -> None,
    so the sampler can skip without raising on the audio path."""
    dx = _import_curves()
    assert dx.parse_curve_spec("") is None
    assert dx.parse_curve_spec("not json") is None
    assert dx.parse_curve_spec("null") is None
    assert dx.parse_curve_spec('{"points": null}') is None
    assert dx.parse_curve_spec('{"points": []}') is None
    assert dx.parse_curve_spec('{"points": [[0, 0.5]]}') is None  # <2 points
    assert dx.parse_curve_spec('{"points": [[0, 0.5], "not a pair"]}') is None


def test_curve_eval_at_control_points_exact():
    """eval_curve_linear at exactly a control point's x returns that
    point's y verbatim."""
    dx = _import_curves()
    pts = [(0.0, 0.0), (0.5, 1.0), (1.0, 0.3)]
    assert dx.eval_curve_linear(pts, 0.0) == pytest.approx(0.0)
    assert dx.eval_curve_linear(pts, 0.5) == pytest.approx(1.0)
    assert dx.eval_curve_linear(pts, 1.0) == pytest.approx(0.3)


def test_curve_eval_linear_interp_between_points():
    """Linear interpolation: midway between (0, 0) and (0.5, 1.0) is
    (0.25, 0.5)."""
    dx = _import_curves()
    pts = [(0.0, 0.0), (0.5, 1.0), (1.0, 0.0)]
    assert dx.eval_curve_linear(pts, 0.25) == pytest.approx(0.5)
    assert dx.eval_curve_linear(pts, 0.75) == pytest.approx(0.5)
    # Quarter-points within each segment.
    assert dx.eval_curve_linear(pts, 0.125) == pytest.approx(0.25)
    assert dx.eval_curve_linear(pts, 0.875) == pytest.approx(0.25)


def test_curve_eval_clamps_t_outside_0_1():
    """t < 0 -> first point's y; t > 1 -> last point's y. Avoids
    extrapolation surprises if the playhead wraps in a weird way."""
    dx = _import_curves()
    pts = [(0.0, 0.2), (1.0, 0.8)]
    assert dx.eval_curve_linear(pts, -0.5) == pytest.approx(0.2)
    assert dx.eval_curve_linear(pts, 2.0) == pytest.approx(0.8)
