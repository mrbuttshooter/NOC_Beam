"""Motion helpers -- Visual 2.0.

Tiny QPropertyAnimation / QVariantAnimation wrappers giving the app the
150-250ms eased micro-motion Qt apps never bother with:

- ``fade_in``        stacked-view crossfade (opacity 0 -> 1)
- ``slide_fade_in``  banner / hero entrance (fade + small upward nudge)
- ``pulse`` / ``stop_pulse``  soft breathing loop (status dot, Ringing chip)
- ``slide_geometry`` animated geometry (bottom-tab active indicator)

Every helper respects the module-level ``REDUCE_MOTION`` kill switch (also
settable via the ``NOC_BEAM_REDUCE_MOTION=1`` env var): when on, helpers
jump straight to the end state so the UI stays fully functional.

Implementation notes:
- Animations are parented to the affected widget and started with
  ``DeleteWhenStopped``; Qt owns the lifetime, no manual bookkeeping.
- Opacity rides on a QGraphicsOpacityEffect that is REMOVED when the
  animation finishes -- leaving effects installed permanently costs
  render passes and can blur text on fractional DPI.
- Never raises: motion is decoration; a failed animation must never take
  a call-flow feature down with it.
"""
from __future__ import annotations

import logging
import os

from PySide6.QtCore import (
    QAbstractAnimation,
    QEasingCurve,
    QPoint,
    QPropertyAnimation,
    QRect,
    QVariantAnimation,
)
from PySide6.QtWidgets import QGraphicsOpacityEffect, QWidget

log = logging.getLogger(__name__)

# Global kill switch. Flip to True (or set NOC_BEAM_REDUCE_MOTION=1) to
# disable all micro-motion app-wide; helpers snap to their end state.
REDUCE_MOTION: bool = os.environ.get("NOC_BEAM_REDUCE_MOTION", "") == "1"

# Shared durations (ms) -- the brief's 150-250ms band.
DUR_FAST = 160
DUR_MED = 200
DUR_SLOW = 220

_DELETE = QAbstractAnimation.DeletionPolicy.DeleteWhenStopped


def _clear_effect(widget: QWidget) -> None:
    try:
        widget.setGraphicsEffect(None)  # deletes the installed effect
    except Exception:
        pass


def fade_in(
    widget: QWidget,
    duration: int = DUR_FAST,
    *,
    easing: QEasingCurve.Type = QEasingCurve.Type.OutCubic,
) -> None:
    """Opacity 0 -> 1. Used as the stacked-widget view-switch crossfade."""
    if widget is None:
        return
    if REDUCE_MOTION:
        _clear_effect(widget)
        return
    try:
        effect = QGraphicsOpacityEffect(widget)
        effect.setOpacity(0.0)
        widget.setGraphicsEffect(effect)
        anim = QPropertyAnimation(effect, b"opacity", widget)
        anim.setStartValue(0.0)
        anim.setEndValue(1.0)
        anim.setDuration(int(duration))
        anim.setEasingCurve(easing)
        anim.finished.connect(lambda w=widget: _clear_effect(w))
        anim.start(_DELETE)
    except Exception:
        log.debug("fade_in failed", exc_info=True)
        _clear_effect(widget)


def slide_fade_in(
    widget: QWidget,
    dy: int = 10,
    duration: int = DUR_MED,
    *,
    easing: QEasingCurve.Type = QEasingCurve.Type.OutCubic,
) -> None:
    """Entrance: fade 0 -> 1 while nudging up/down |dy|px into place.

    ``dy`` > 0 slides UP into place (hero cards); ``dy`` < 0 slides DOWN
    (banners entering from the top). Works on layout-managed widgets: the
    position is only displaced for the animation's lifetime and lands
    exactly back on the layout position.
    """
    if widget is None:
        return
    if REDUCE_MOTION:
        _clear_effect(widget)
        return
    try:
        end_pos = widget.pos()
        start_pos = end_pos + QPoint(0, int(dy))

        effect = QGraphicsOpacityEffect(widget)
        effect.setOpacity(0.0)
        widget.setGraphicsEffect(effect)
        op = QPropertyAnimation(effect, b"opacity", widget)
        op.setStartValue(0.0)
        op.setEndValue(1.0)
        op.setDuration(int(duration))
        op.setEasingCurve(easing)
        op.finished.connect(lambda w=widget: _clear_effect(w))

        mv = QPropertyAnimation(widget, b"pos", widget)
        mv.setStartValue(start_pos)
        mv.setEndValue(end_pos)
        mv.setDuration(int(duration))
        mv.setEasingCurve(easing)

        op.start(_DELETE)
        mv.start(_DELETE)
    except Exception:
        log.debug("slide_fade_in failed", exc_info=True)
        _clear_effect(widget)


def pulse(
    widget: QWidget,
    low: float = 0.7,
    high: float = 1.0,
    period_ms: int = 900,
) -> None:
    """Soft opacity breathing loop (Ringing chip, registering status dot).

    Idempotent: calling again while a pulse is live is a no-op. Stop with
    :func:`stop_pulse`, which also restores full opacity.
    """
    if widget is None or REDUCE_MOTION:
        return
    if getattr(widget, "_motion_pulse", None) is not None:
        return  # already breathing
    try:
        effect = QGraphicsOpacityEffect(widget)
        effect.setOpacity(high)
        widget.setGraphicsEffect(effect)
        anim = QVariantAnimation(widget)
        anim.setStartValue(float(high))
        anim.setKeyValueAt(0.5, float(low))
        anim.setEndValue(float(high))
        anim.setDuration(int(period_ms))
        anim.setEasingCurve(QEasingCurve.Type.InOutSine)
        anim.setLoopCount(-1)
        anim.valueChanged.connect(
            lambda v, e=effect: e.setOpacity(float(v))
        )
        anim.start()
        widget._motion_pulse = anim  # type: ignore[attr-defined]
    except Exception:
        log.debug("pulse failed", exc_info=True)
        _clear_effect(widget)


def stop_pulse(widget: QWidget) -> None:
    """End a :func:`pulse` loop and restore the widget to full opacity."""
    if widget is None:
        return
    anim = getattr(widget, "_motion_pulse", None)
    if anim is None:
        return
    try:
        anim.stop()
        anim.deleteLater()
    except Exception:
        pass
    widget._motion_pulse = None  # type: ignore[attr-defined]
    _clear_effect(widget)


def slide_geometry(
    widget: QWidget,
    target: QRect,
    duration: int = 180,
    *,
    easing: QEasingCurve.Type = QEasingCurve.Type.OutCubic,
) -> None:
    """Animate a free (non-layout) widget's geometry -- the bottom-tab
    active indicator sliding between tabs."""
    if widget is None:
        return
    if REDUCE_MOTION:
        try:
            widget.setGeometry(target)
        except Exception:
            pass
        return
    try:
        anim = QPropertyAnimation(widget, b"geometry", widget)
        anim.setStartValue(widget.geometry())
        anim.setEndValue(target)
        anim.setDuration(int(duration))
        anim.setEasingCurve(easing)
        anim.start(_DELETE)
    except Exception:
        log.debug("slide_geometry failed", exc_info=True)
        try:
            widget.setGeometry(target)
        except Exception:
            pass


def press_flash(
    widget: QWidget,
    low: float = 0.82,
    duration: int = 150,
) -> None:
    """Quick opacity dip-and-recover on press (dialpad keys).

    One-shot: 1.0 -> low -> 1.0 over ``duration``; the effect is removed
    when done so the widget renders effect-free at rest.
    """
    if widget is None or REDUCE_MOTION:
        return
    try:
        effect = QGraphicsOpacityEffect(widget)
        effect.setOpacity(1.0)
        widget.setGraphicsEffect(effect)
        anim = QVariantAnimation(widget)
        anim.setStartValue(1.0)
        anim.setKeyValueAt(0.4, float(low))
        anim.setEndValue(1.0)
        anim.setDuration(int(duration))
        anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        anim.valueChanged.connect(lambda v, e=effect: e.setOpacity(float(v)))
        anim.finished.connect(lambda w=widget: _clear_effect(w))
        anim.start()
    except Exception:
        log.debug("press_flash failed", exc_info=True)
        _clear_effect(widget)
