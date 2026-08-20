"""
damping_correction.py

Implements the damping-aware height-rate correction from Altuntas,
Williams & Tunalioglu (2026), GPS Solutions -- see REFERENCES.md.

Built incrementally, component by component, each validated against
synthetic data with known ground truth before being combined into the
full pipeline. This module holds the foundational pieces:

    1. The forward model for a damped SNR oscillation (their Eq. 1,
       extended with the damping weight from their Eq. 8).
    2. Fitting the damping parameter Lambda and oscillation amplitude
       to one real arc's actual SNR data, given a known reflector
       height (typically gnssir's own LSP-derived estimate for that
       arc) -- this is what later feeds into the damping-aware h-rate
       correction (their Eq. 9) once extended to a full window.

Component 2 (the windowed multi-arc h/hdot estimation, their Eq. 5-7)
and Component 3 (normalized IRLS, their Eq. 10-13) build on top of
this and are implemented separately, once this foundational piece is
confirmed correct.
"""

from __future__ import annotations

import numpy as np
from scipy.optimize import minimize_scalar


def damping_weight(elevation_deg: np.ndarray, wavelength_m: float, damping_lambda: float) -> np.ndarray:
    """
    Equation 8: alpha(E) = exp(-Lambda * k^2 * sin^2(E))
    where k = 2*pi/wavelength. Represents the reduction of multipath
    oscillation amplitude with elevation angle due to surface
    roughness/damping -- larger elevation angles see stronger damping.
    """
    k = 2 * np.pi / wavelength_m
    e_rad = np.radians(elevation_deg)
    return np.exp(-damping_lambda * (k ** 2) * np.sin(e_rad) ** 2)


def forward_model_dsnr(elevation_deg: np.ndarray, rh_m: float, wavelength_m: float,
                        amp_s: float, amp_c: float, damping_lambda: float) -> np.ndarray:
    """
    The damped dSNR forward model: a sine/cosine oscillation at the
    frequency implied by rh_m, with amplitude decaying according to
    the damping weight (Eq. 8) as elevation increases -- rather than
    Eq. 1's single amplitude/phase form, this uses the equivalent
    amp_s*sin + amp_c*cos parameterization, which is what makes the
    amplitude parameters linear (needed for the variable projection
    fit in Component 2) while damping_lambda remains the one
    nonlinear parameter.
    """
    e_rad = np.radians(elevation_deg)
    phase = (4 * np.pi * rh_m / wavelength_m) * np.sin(e_rad)
    alpha = damping_weight(elevation_deg, wavelength_m, damping_lambda)
    return alpha * (amp_s * np.sin(phase) + amp_c * np.cos(phase))


def fit_amplitude_given_lambda(elevation_deg: np.ndarray, dsnr: np.ndarray,
                                rh_m: float, wavelength_m: float,
                                damping_lambda: float) -> tuple[float, float, float]:
    """
    For a fixed damping_lambda, amp_s and amp_c enter the forward
    model linearly -- so they can be solved directly via ordinary
    least squares (this is the "linear part" that variable
    projection eliminates analytically for each trial Lambda).

    Returns (amp_s, amp_c, residual_sum_of_squares).
    """
    e_rad = np.radians(elevation_deg)
    phase = (4 * np.pi * rh_m / wavelength_m) * np.sin(e_rad)
    alpha = damping_weight(elevation_deg, wavelength_m, damping_lambda)

    basis_sin = alpha * np.sin(phase)
    basis_cos = alpha * np.cos(phase)
    design = np.column_stack([basis_sin, basis_cos])

    coeffs, residuals, _rank, _sv = np.linalg.lstsq(design, dsnr, rcond=None)
    amp_s, amp_c = coeffs

    predicted = design @ coeffs
    rss = float(np.sum((dsnr - predicted) ** 2))

    return float(amp_s), float(amp_c), rss


def fit_damping_and_amplitude(elevation_deg: np.ndarray, dsnr: np.ndarray,
                               rh_m: float, wavelength_m: float,
                               lambda_bounds: tuple[float, float] = (0.0, 0.6)) -> dict:
    """
    Variable projection fit for a single arc: searches over
    damping_lambda (the one genuinely nonlinear parameter) to
    minimize the residual sum of squares, with amp_s/amp_c solved
    exactly (via ordinary least squares) at each trial Lambda rather
    than searched -- this is what makes variable projection much
    better-behaved than a naive 3-parameter nonlinear search.

    Default lambda_bounds confirmed against the paper's own
    simulation setup: Lambda = 0.5 * (surface_std)^2 with surface_std
    ranging 0-0.75m gives Lambda in [0, 0.28] -- values much larger
    than this drive the damping weight (Eq. 8) to numerically
    collapse toward zero given GPS L1's short wavelength, making the
    fit degenerate rather than just less accurate.

    Returns a dict with the fitted lambda, amp_s, amp_c, and the
    final residual sum of squares.
    """
    def objective(damping_lambda: float) -> float:
        _amp_s, _amp_c, rss = fit_amplitude_given_lambda(
            elevation_deg, dsnr, rh_m, wavelength_m, damping_lambda
        )
        return rss

    result = minimize_scalar(
        objective, bounds=lambda_bounds, method="bounded",
        options={"xatol": 1e-4},
    )

    best_lambda = float(result.x)
    amp_s, amp_c, rss = fit_amplitude_given_lambda(
        elevation_deg, dsnr, rh_m, wavelength_m, best_lambda
    )

    return {
        "damping_lambda": best_lambda,
        "amp_s": amp_s,
        "amp_c": amp_c,
        "rss": rss,
        "amplitude": float(np.hypot(amp_s, amp_c)),
    }
