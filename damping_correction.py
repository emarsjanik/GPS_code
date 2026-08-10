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

    # Confirmed necessary against real data: at some trial Lambda
    # values during the search, alpha can underflow to exactly zero
    # across an entire arc (particularly for arcs with many points at
    # the higher end of the elevation mask), making the design matrix
    # entirely zero and crashing the SVD inside lstsq with a raw
    # LAPACK error rather than a catchable exception. Detecting this
    # degenerate case and returning a large penalty RSS instead keeps
    # the Lambda search well-behaved -- it will naturally steer away
    # from these regions rather than crash.
    if not np.all(np.isfinite(design)) or np.allclose(design, 0.0):
        return 0.0, 0.0, float(np.sum(dsnr ** 2))

    coeffs, residuals, _rank, _sv = np.linalg.lstsq(design, dsnr, rcond=None)
    amp_s, amp_c = coeffs

    predicted = design @ coeffs
    rss = float(np.sum((dsnr - predicted) ** 2))

    return float(amp_s), float(amp_c), rss


def normalized_irls_weights(residuals: np.ndarray, c: float = 4.685) -> np.ndarray:
    """
    Equations 10-13: normalized bisquare IRLS weights. Standardizes
    residuals using a robust MAD-based scale estimate (Eq. 10-11),
    computes bisquare weights (Eq. 12), then normalizes so the
    weights sum to N (Eq. 13) -- this last step is the paper's own
    specific refinement over a plain bisquare IRLS: it "preserves the
    numerical scale of the system across iterations, stabilizes
    convergence under diverse noise levels."

    c=4.685 is the paper's own stated tuning constant (Woolrich,
    2008), defining the inlier threshold under an assumed normal
    residual distribution.
    """
    median_r = np.median(residuals)
    mad = 1.4826 * np.median(np.abs(residuals - median_r))

    if mad == 0:
        # All residuals identical (e.g. a perfect fit) -- no
        # meaningful scale to standardize against; uniform weights.
        return np.ones_like(residuals)

    u = residuals / (c * mad)
    b = np.where(np.abs(u) <= 1, (1 - u ** 2) ** 2, 0.0)

    b_sum = np.sum(b)
    if b_sum == 0:
        # Degenerate case: every residual flagged as an outlier --
        # falling back to uniform weights is safer than returning
        # all-zero weights, which would make the next solve singular.
        return np.ones_like(residuals)

    n = len(residuals)
    return b / b_sum * n


def solve_window_irls(arc_times_hours: np.ndarray, arc_rh: np.ndarray,
                       arc_edotf: np.ndarray, window_center_hours: float,
                       max_iterations: int = 30, convergence_tol: float = 1e-6) -> dict:
    """
    Wraps solve_window() in the paper's normalized IRLS loop: solves
    once with uniform weights, computes normalized bisquare weights
    from the residuals, re-solves, and repeats until the parameter
    estimates change by less than convergence_tol (matching the
    paper's own stated 30-iteration cap and 1e-6 threshold) or the
    iteration limit is reached.

    Returns the same dict as solve_window(), plus n_iterations and
    final_weights.
    """
    weights = None
    prev_params = None

    for iteration in range(1, max_iterations + 1):
        result = solve_window(arc_times_hours, arc_rh, arc_edotf, window_center_hours, weights)
        current_params = np.array([result["h_w"], result["hdot_w"]])

        weights = normalized_irls_weights(result["residuals"])

        if prev_params is not None:
            change = np.max(np.abs(current_params - prev_params))
            if change < convergence_tol:
                result["n_iterations"] = iteration
                result["final_weights"] = weights
                return result

        prev_params = current_params

    result["n_iterations"] = max_iterations
    result["final_weights"] = weights
    return result


def run_damping_aware_correction(
    arc_times_hours: np.ndarray,
    arc_rh: np.ndarray,
    arc_elevation: list[np.ndarray],
    arc_dsnr: list[np.ndarray],
    arc_edot: list[np.ndarray],
    arc_wavelength_m: np.ndarray,
    window_length_hours: float = 1.0,
    window_shift_hours: float = 1 / 6,
) -> list[dict]:
    """
    Full pipeline (Components 1+2, pre-IRLS): for each sliding window
    across the real data's time span --
        1. Jointly fit one shared damping parameter Lambda across
           every arc in the window (fit_shared_damping).
        2. Compute each arc's damping-aware EdotF-style coefficient
           using that Lambda (damping_aware_edotf).
        3. Solve for the window's water level and its rate of change
           (solve_window).

    All array-of-arrays arguments (arc_elevation, arc_dsnr, arc_edot)
    are parallel lists, one entry per arc, matching the order of
    arc_times_hours/arc_rh. arc_wavelength_m is each arc's own real
    wavelength (a parallel array, not a single shared value) --
    necessary once mixing multiple GNSS frequencies, since each has a
    genuinely different wavelength. The recommended, most accurate
    source for this: 2 * meta['cf'] from extract_arcs's own metadata
    for each arc (confirmed correct against gnssrefl's own value to
    8+ decimal places for GPS L1, and correctly handles GLONASS's
    per-satellite FDMA wavelength variation automatically, since
    gnssrefl already accounts for it internally when computing cf).

    Windows with fewer than 2 arcs are skipped (solve_window's own
    minimum requirement) -- their gap is left as a real, honest gap
    in the output rather than filled with an unreliable estimate.

    Returns a list of dicts, one per successfully-solved window, each
    with window_center_hours, h_w, hdot_w, damping_lambda, and n_arcs
    -- ready for further use (e.g. normalized IRLS in Component 3, or
    direct plotting/comparison against the tide models).
    """
    windows = build_sliding_windows(arc_times_hours, window_length_hours, window_shift_hours)

    results = []
    for window_center in windows:
        mask = arcs_in_window(arc_times_hours, window_center, window_length_hours)
        n_in_window = int(np.sum(mask))

        if n_in_window < 2:
            continue

        window_elevation = [arc_elevation[i] for i in np.where(mask)[0]]
        window_dsnr = [arc_dsnr[i] for i in np.where(mask)[0]]
        window_edot = [arc_edot[i] for i in np.where(mask)[0]]
        window_wavelength = [arc_wavelength_m[i] for i in np.where(mask)[0]]
        window_rh = arc_rh[mask]
        window_times = arc_times_hours[mask]

        damping_fit = fit_shared_damping(window_elevation, window_dsnr, window_rh, window_wavelength)
        shared_lambda = damping_fit["damping_lambda"]

        window_edotf = np.array([
            damping_aware_edotf(elevation, edot, wavelength_m, shared_lambda)
            for elevation, edot, wavelength_m in zip(window_elevation, window_edot, window_wavelength)
        ])

        try:
            solved = solve_window_irls(window_times, window_rh, window_edotf, window_center)
        except np.linalg.LinAlgError:
            # Confirmed real, necessary safeguard: an ill-conditioned
            # window (see solve_window()'s own docstring) previously
            # produced physically impossible results rather than
            # failing loudly. Skipping it leaves a real, honest gap
            # in the output instead.
            continue

        results.append({
            "window_center_hours": window_center,
            "h_w": solved["h_w"],
            "hdot_w": solved["hdot_w"],
            "damping_lambda": shared_lambda,
            "n_arcs": n_in_window,
            "n_irls_iterations": solved["n_iterations"],
        })

    return results


# Empirically-calibrated scale factor between extract_arcs's own
# "edot" array and the units gnssrefl's internal EdotF calculation
# actually uses. Confirmed via direct, real cross-validation against
# gnssrefl's own already-computed EdotF values (both meta['edot_factor']
# and gnssir_processing_results['EdotF']) across 28 real arcs from our
# own station: the ratio between an uncalibrated classical_edotf() and
# gnssrefl's own value was remarkably consistent (mean 62.179,
# std 0.134 -- about 0.2% relative variation), confirming a real,
# fixed scale difference rather than noise. We don't have a clean
# theoretical derivation for this exact value (extract_arcs's own
# edot computation isn't fully documented at this level of detail),
# so this is an honest empirical calibration against gnssrefl's own
# trusted output, not a claimed first-principles unit conversion.
_EDOT_CALIBRATION_FACTOR = 62.179


def classical_edotf(elevation_deg: np.ndarray, edot: np.ndarray) -> float:
    """
    The classical (unweighted) elevation-rate factor: mean(tan(E)) /
    mean(edot), matching gnssrefl's own EdotF (confirmed against
    gnssrefl's own documentation: "the average of the tangent of the
    elevation angle during an arc, [divided by] edot" -- units
    confirmed to be hours, since radians are dimensionless, so
    rad/(rad/hour) simplifies to hours directly).

    Includes _EDOT_CALIBRATION_FACTOR to correct for a real, confirmed
    scale mismatch between extract_arcs's own "edot" array and the
    units gnssrefl's internal EdotF actually uses -- see that
    constant's own docstring for how this was empirically validated.

    Cross-validate this function's output against gnssrefl's own
    meta['edot_factor'] or gnssir_processing_results['EdotF'] for a
    real arc before trusting results built on top of it -- this
    exact check is what caught the original scale mismatch.
    """
    tan_e = np.tan(np.radians(elevation_deg))
    return float(np.mean(tan_e) / np.mean(edot) / _EDOT_CALIBRATION_FACTOR)


def damping_aware_edotf(elevation_deg: np.ndarray, edot: np.ndarray,
                         wavelength_m: float, damping_lambda: float) -> float:
    """
    Equation 9: the damping-aware elevation-rate factor, replacing
    the classical unweighted averages in classical_edotf() with
    alpha^2-weighted averages -- giving more influence to epochs
    where the multipath oscillation amplitude is less damped (i.e.
    less affected by sea surface roughness), rather than treating
    every epoch in the arc as equally informative.

        sum(alpha_i^2 * tan(E_i)) / sum(alpha_i^2 * edot_i) / calibration

    Reduces to classical_edotf() exactly when damping_lambda = 0
    (alpha = 1 everywhere) -- a direct, checkable consistency
    property between the two functions. Uses the same
    _EDOT_CALIBRATION_FACTOR as classical_edotf(), for the same
    confirmed reason.
    """
    alpha = damping_weight(elevation_deg, wavelength_m, damping_lambda)
    weights = alpha ** 2
    tan_e = np.tan(np.radians(elevation_deg))
    numerator = np.sum(weights * tan_e)
    denominator = np.sum(weights * edot)
    return float(numerator / denominator / _EDOT_CALIBRATION_FACTOR)


def fit_shared_damping(arcs_elevation: list[np.ndarray], arcs_dsnr: list[np.ndarray],
                        arcs_rh: list[float], arcs_wavelength_m: list[float],
                        lambda_bounds: tuple[float, float] = (0.0, 0.6)) -> dict:
    """
    Extends fit_damping_and_amplitude() (Component 1, single-arc) to
    match the paper's actual approach: "it is assumed that all arcs
    within the same analysis window share a common damping parameter
    Lambda... arc-specific sine and cosine amplitudes are solved
    simultaneously, while a single Lambda is estimated for the entire
    window" -- i.e. one shared, nonlinear Lambda across every arc in
    the window, with each arc keeping its own linear (amp_s, amp_c)
    pair.

    arcs_wavelength_m: each arc's own real wavelength (a parallel
    list, not a single shared value) -- necessary once mixing
    multiple GNSS frequencies within the same window, since each
    frequency has a genuinely different wavelength. Lambda itself is
    still shared across all arcs; only the wavelength used inside
    each arc's own damping_weight() calculation is per-arc.

    Still uses variable projection: for any trial Lambda, every arc's
    own amp_s/amp_c can be solved independently and exactly (they
    don't interact across arcs), so the 1D search over Lambda only
    needs to sum each arc's own residual sum of squares.

    arcs_elevation, arcs_dsnr, arcs_rh, arcs_wavelength_m: parallel
    lists, one entry per arc in this window (arcs_rh is each arc's
    own LSP-derived RH, used as the known reflector height for that
    arc's forward model).

    Returns dict with the shared damping_lambda and a per-arc list of
    (amp_s, amp_c) results.
    """
    n_arcs = len(arcs_elevation)
    if n_arcs == 0:
        raise ValueError("Need at least 1 arc to fit a shared damping parameter")

    def total_objective(damping_lambda: float) -> float:
        total_rss = 0.0
        for elevation, dsnr, rh, wavelength_m in zip(arcs_elevation, arcs_dsnr, arcs_rh, arcs_wavelength_m):
            _amp_s, _amp_c, rss = fit_amplitude_given_lambda(
                elevation, dsnr, rh, wavelength_m, damping_lambda
            )
            total_rss += rss
        return total_rss

    result = minimize_scalar(
        total_objective, bounds=lambda_bounds, method="bounded",
        options={"xatol": 1e-4},
    )
    best_lambda = float(result.x)

    per_arc = []
    for elevation, dsnr, rh, wavelength_m in zip(arcs_elevation, arcs_dsnr, arcs_rh, arcs_wavelength_m):
        amp_s, amp_c, rss = fit_amplitude_given_lambda(
            elevation, dsnr, rh, wavelength_m, best_lambda
        )
        per_arc.append({"amp_s": amp_s, "amp_c": amp_c, "rss": rss})

    return {
        "damping_lambda": best_lambda,
        "per_arc": per_arc,
        "total_rss": float(total_objective(best_lambda)),
    }


def solve_window(arc_times_hours: np.ndarray, arc_rh: np.ndarray,
                  arc_edotf: np.ndarray, window_center_hours: float,
                  weights: np.ndarray | None = None) -> dict:
    """
    Equations 5-7: within one time window, jointly solves for the
    window's water level (h_w) and its rate of change (hdot_w) from
    every arc observed in that window, using each arc's own EdotF-
    style coefficient (classical or damping-aware -- this function
    doesn't care which, it just needs the final per-arc number).

    Design decision, explicitly noted: the paper's tropospheric
    correction term (delta_T) is treated as already applied here --
    gnssrefl's own refraction correction (confirmed via our own logs:
    "Standard Bennett refraction correction") is already baked into
    every RH value it reports, so arc_rh is used as-is rather than
    re-deriving a separate tropospheric model from scratch.

    Observation equation per arc j:
        y_j = h_w + hdot_w * [EdotF_j + (t_j - t_w)]

    Stacked into y = A @ [h_w, hdot_w], solved via (weighted) least
    squares. weights=None gives ordinary least squares (used here);
    Component 3 (normalized IRLS) will call this repeatedly with
    updated robust weights.

    Requires at least 2 arcs (2 unknowns: h_w, hdot_w). Also requires
    the resulting system to be reasonably well-conditioned -- raises
    LinAlgError if not.

    Confirmed necessary against real data: an exactly-determined
    2-arc window (2 equations, 2 unknowns) has zero degrees of
    freedom, and if those two arcs' own EdotF+time_offset
    coefficients happen to be close to each other (near-colinear),
    the system becomes numerically ill-conditioned. This produced
    genuinely impossible real results (h_w up to 1300m, hdot_w up to
    ~7000 m/hour) before this check was added -- every single such
    case traced back to exactly n_arcs=2 with a near-singular design
    matrix. Rather than silently returning garbage, this raises a
    clear, catchable error so the caller can skip the window (leaving
    a real, honest gap) instead.

    Returns dict with h_w, hdot_w, and the arcs' residuals (needed by
    the IRLS loop in Component 3).
    """
    n = len(arc_times_hours)
    if n < 2:
        raise ValueError(f"Need at least 2 arcs to solve a window, got {n}")

    time_offset = arc_times_hours - window_center_hours
    coefficient = arc_edotf + time_offset

    design = np.column_stack([np.ones(n), coefficient])

    if weights is None:
        solution, _residuals, _rank, sv = np.linalg.lstsq(design, arc_rh, rcond=None)
    else:
        w_sqrt = np.sqrt(weights)
        solution, _residuals, _rank, sv = np.linalg.lstsq(
            design * w_sqrt[:, None], arc_rh * w_sqrt, rcond=None
        )

    # Condition number = ratio of largest to smallest singular value.
    # Calibrated directly against both real and synthetic data: normal,
    # reasonably-separated windows land in the ~4-20 range, while
    # windows with near-identical arc coefficients (the real failure
    # mode found in production data) reached 2000+ in direct testing
    # and were the exact source of the physically impossible results
    # described above. 200 is set as a threshold with real margin
    # above normal windows but well below confirmed-bad ones.
    condition_number = sv[0] / sv[-1] if sv[-1] > 0 else np.inf
    if condition_number > 200:
        raise np.linalg.LinAlgError(
            f"Window is too ill-conditioned to solve reliably "
            f"(condition number {condition_number:.2e}, n_arcs={n})"
        )

    h_w, hdot_w = solution
    predicted = design @ solution
    residuals = arc_rh - predicted

    return {
        "h_w": float(h_w),
        "hdot_w": float(hdot_w),
        "residuals": residuals,
        "n_arcs": n,
    }


def build_sliding_windows(arc_times_hours: np.ndarray, window_length_hours: float,
                           window_shift_hours: float) -> list[float]:
    """
    Generates window center times spanning the real data's actual
    time range, matching the paper's own sliding-window setup (they
    used a 60-min window with a 10-min shift). Returns a list of
    window center times (hours); each window spans
    [center - window_length/2, center + window_length/2].

    Confirmed necessary to derive this from the actual data range
    rather than assuming a fixed 24-hour day: our own real arcs can
    span multiple days or partial days (e.g. the isolated single-day
    fragments we found earlier tonight), and windows should only be
    generated where we actually have data.
    """
    if len(arc_times_hours) == 0:
        return []

    t_min = float(np.min(arc_times_hours))
    t_max = float(np.max(arc_times_hours))

    half_window = window_length_hours / 2
    first_center = t_min + half_window
    last_center = t_max - half_window

    if last_center < first_center:
        # Not enough time span for even one full window -- fall back
        # to a single window centered on the data's own midpoint.
        return [(t_min + t_max) / 2]

    n_windows = int(np.floor((last_center - first_center) / window_shift_hours)) + 1
    return [first_center + i * window_shift_hours for i in range(n_windows)]


def arcs_in_window(arc_times_hours: np.ndarray, window_center_hours: float,
                    window_length_hours: float) -> np.ndarray:
    """Returns a boolean mask selecting arcs whose time falls within
    this window's span."""
    half_window = window_length_hours / 2
    return np.abs(arc_times_hours - window_center_hours) <= half_window


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
