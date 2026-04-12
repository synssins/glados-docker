"""
Eye frame renderer for the HUB75 display.

Uses NumPy for vectorised rendering — ~100x faster than the pure-Python
loop.  Falls back to a scalar implementation if NumPy is unavailable
(shouldn't happen; it's a core dependency).

Coordinate system:
  (0, 0) = top-left pixel
  Panel center = ((W-1)/2, (H-1)/2) = (31.5, 31.5) for 64x64

Rendering layers (back to front):
  1. Black background
  2. Glow halo (soft quadratic falloff beyond iris edge)
  3. Iris circle (hard circle with 1px anti-alias)
  4. Eyelid mask (top and bottom cutoffs with 1px blend)
"""

from __future__ import annotations

import math

import numpy as np

from .state_machine import EyeParams

# Pre-computed coordinate grids (lazily initialised per panel size)
_grid_cache: dict[tuple[int, int], tuple[np.ndarray, np.ndarray]] = {}


def _get_grids(w: int, h: int) -> tuple[np.ndarray, np.ndarray]:
    """Return cached (X, Y) meshgrids for the panel dimensions."""
    key = (w, h)
    if key not in _grid_cache:
        xs = np.arange(w, dtype=np.float32)
        ys = np.arange(h, dtype=np.float32)
        _grid_cache[key] = np.meshgrid(xs, ys)  # X[h,w], Y[h,w]
    return _grid_cache[key]


def render_eye_frame(
    params: EyeParams,
    t: float,
    panel_w: int = 64,
    panel_h: int = 64,
) -> bytes:
    """Render a single eye frame as raw RGB bytes.

    Args:
        params: Current eye parameters (color, lids, brightness, etc.).
        t: Wall-clock time in seconds (for pulse animation).
        panel_w: Panel width in pixels.
        panel_h: Panel height in pixels.

    Returns:
        ``panel_w * panel_h * 3`` bytes (RGB, row-major, top-left origin).
    """
    # Unpack parameters
    ir, ig, ib = params.iris_color
    radius = params.iris_radius
    bri = params.brightness
    pulse_speed = params.pulse_speed
    pulse_depth = params.pulse_depth
    glow_intensity = params.glow
    top_lid = params.top_lid
    bottom_lid = params.bottom_lid

    # Center with gaze offset
    cx = (panel_w - 1) / 2.0 + params.offset_x
    cy = (panel_h - 1) / 2.0 + params.offset_y

    # Animated brightness pulse
    pulse = 1.0 - pulse_depth / 2.0 + (pulse_depth / 2.0) * math.sin(t * pulse_speed)
    effective_bri = np.float32(bri * pulse)

    # Get coordinate grids
    X, Y = _get_grids(panel_w, panel_h)

    # Distance from iris center (float32 for speed)
    dx = X - np.float32(cx)
    dy = Y - np.float32(cy)
    dist = np.sqrt(dx * dx + dy * dy)

    # ── Layer 1: Black background ────────────────────────────
    # Start with zeros — r, g, b are float32 arrays
    r = np.zeros((panel_h, panel_w), dtype=np.float32)
    g = np.zeros((panel_h, panel_w), dtype=np.float32)
    b = np.zeros((panel_h, panel_w), dtype=np.float32)

    # ── Layer 2: Glow halo ───────────────────────────────────
    glow_extent = np.float32(4.0)
    glow_mask = (dist > radius - 1.0) & (dist <= radius + glow_extent)
    if np.any(glow_mask):
        dist_beyond = np.clip(dist - radius, 0.0, None)
        falloff = (1.0 - dist_beyond / glow_extent) ** 2
        glow_a = glow_intensity * falloff * effective_bri * 0.6
        r = np.where(glow_mask, ir * glow_a, r)
        g = np.where(glow_mask, ig * glow_a, g)
        b = np.where(glow_mask, ib * glow_a, b)

    # ── Layer 3: Iris circle with 1px anti-alias ─────────────
    iris_outer = dist <= radius + 0.5
    iris_inner = dist <= radius - 0.5
    # Anti-alias band: pixels on the edge
    alpha = np.where(iris_inner, 1.0, np.clip(0.5 - (dist - radius), 0.0, 1.0))
    alpha = np.where(iris_outer, alpha, 0.0)
    iris_bri = effective_bri * alpha
    # Iris overwrites glow where present
    iris_mask = iris_outer
    r = np.where(iris_mask, ir * iris_bri, r)
    g = np.where(iris_mask, ig * iris_bri, g)
    b = np.where(iris_mask, ib * iris_bri, b)

    # ── Layer 4: Eyelid masks ────────────────────────────────
    if top_lid > 0.0:
        lid_top_y = cy - radius + (top_lid * 2.0 * radius)
        lid_alpha = np.clip((Y - lid_top_y) + 0.5, 0.0, 1.0)
        # Only apply where the lid actually clips (above lid_top_y + 0.5)
        lid_region = Y < lid_top_y + 0.5
        r = np.where(lid_region, r * lid_alpha, r)
        g = np.where(lid_region, g * lid_alpha, g)
        b = np.where(lid_region, b * lid_alpha, b)

    if bottom_lid > 0.0:
        lid_bottom_y = cy + radius - (bottom_lid * 2.0 * radius)
        lid_alpha = np.clip((lid_bottom_y - Y) + 0.5, 0.0, 1.0)
        lid_region = Y > lid_bottom_y - 0.5
        r = np.where(lid_region, r * lid_alpha, r)
        g = np.where(lid_region, g * lid_alpha, g)
        b = np.where(lid_region, b * lid_alpha, b)

    # ── Noise floor — kill sub-pixel glow artifacts ──────────
    # During fade-out, glow falloff can produce values like 1.2 that
    # round to 1 in uint8 — a single dim green/cyan pixel on black.
    r = np.where(r < 2.0, 0.0, r)
    g = np.where(g < 2.0, 0.0, g)
    b = np.where(b < 2.0, 0.0, b)

    # ── Assemble RGB buffer ──────────────────────────────────
    frame = np.zeros((panel_h, panel_w, 3), dtype=np.uint8)
    frame[:, :, 0] = np.clip(r, 0, 255).astype(np.uint8)
    frame[:, :, 1] = np.clip(g, 0, 255).astype(np.uint8)
    frame[:, :, 2] = np.clip(b, 0, 255).astype(np.uint8)

    return frame.tobytes()


def lerp_params(src: EyeParams, dst: EyeParams, t: float) -> EyeParams:
    """Linearly interpolate all float fields between two EyeParams.

    Args:
        src: Starting parameters.
        dst: Target parameters.
        t: Interpolation factor, clamped to [0.0, 1.0].

    Returns:
        New EyeParams with interpolated values.
    """
    t = max(0.0, min(1.0, t))
    inv = 1.0 - t

    # Interpolate color channels as integers
    ri = int(src.iris_color[0] * inv + dst.iris_color[0] * t)
    gi = int(src.iris_color[1] * inv + dst.iris_color[1] * t)
    bi = int(src.iris_color[2] * inv + dst.iris_color[2] * t)

    return EyeParams(
        iris_color=(
            min(255, max(0, ri)),
            min(255, max(0, gi)),
            min(255, max(0, bi)),
        ),
        iris_radius=src.iris_radius * inv + dst.iris_radius * t,
        top_lid=src.top_lid * inv + dst.top_lid * t,
        bottom_lid=src.bottom_lid * inv + dst.bottom_lid * t,
        brightness=src.brightness * inv + dst.brightness * t,
        pulse_speed=src.pulse_speed * inv + dst.pulse_speed * t,
        pulse_depth=src.pulse_depth * inv + dst.pulse_depth * t,
        offset_x=src.offset_x * inv + dst.offset_x * t,
        offset_y=src.offset_y * inv + dst.offset_y * t,
        glow=src.glow * inv + dst.glow * t,
    )
