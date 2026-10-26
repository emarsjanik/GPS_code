#!/usr/bin/env python3
"""
amplitude_thresholds.py

One LSP amplitude threshold per GNSS frequency, read off each
frequency's own amplitude histogram in the gnssir results -- for
station.json's opt-in "gnssrefl_amplitude_min_by_frequency".

WHY

gnssir keeps an arc only if the peak of its Lomb-Scargle periodogram
(Amp, column 7 of results/<doy>.txt) is ABOVE reqAmp. make_gnssir_input
writes one value for every frequency ([ampl] * len(freqs); 5 here),
but the amplitudes of different signals are not comparable: transmit
power, the antenna's gain at low elevation and the receiver's tracking
differ by constellation and band, so one threshold is loose for a
strong signal and strict for a weak one. gnssrefl's own documentation
calls the threshold "receiver and elevation angle region dependent",
and its subdaily takes one value per frequency (-ampl).

It matters in storms. A rough sea lowers the coherent reflected
power -- roughly by exp(-4 k^2 s^2 sin^2 e) for a surface height
spread s, the damping term of Strandberg et al.'s inverse SNR model
(2016, 2019) -- so arcs drop below the threshold, and of a strong
signal the damped, doubtful arcs still pass. Purnell et al. (2020)
find the moving sea surface dominates the error budget of the
inverse method (noise dominates that of the spectral one gnssir
uses), and Sepulveda et al. (2023) see errors rise abruptly at high
waves at an exposed pier. In the 25-27 Sep 2026 storm the spline read
up to ~3 m above what the Chatham gauge implied: the arcs that survive
a storm are the biased ones.

THE RULE

For each frequency f, over a CALM window (the owner picks it; storms
lower every amplitude):

    threshold_f = fraction x median(Amp_f)

so an arc is kept if its amplitude is at least that fraction of what
its own signal gives in calm weather -- the same relative damping for
every signal. `fraction` defaults to the station's present overall
strictness, (current threshold) / median(Amp of every frequency
together): the thresholds are redistributed between signals, not
tightened or loosened as a whole. --fraction sets it. A frequency with
fewer than MIN_ARCS arcs in the window keeps its current value.
Thresholds are rounded to 0.1.

Only arcs in the azimuth window the water level uses (subdaily
-azim1/-azim2, read from last night's settings) count: subdaily drops
the others anyway.

ARCS BELOW TODAY'S THRESHOLD

results/<doy>.txt holds only arcs above the current threshold, so its
histogram is cut off there. gnssrefl 4.1.5 also writes every rejected
arc to results/<sta>/failQC/<doy>.txt. An arc there with a computed
amplitude and peak-to-noise ratio, whose peak is not within 0.10 m of
the reflector-height limits ("tooclose"), whose peak-to-noise is above
the json's PkNoise and whose amplitude is at or below its frequency's
reqAmp failed on amplitude alone (gnssir's checks run ediff/delT,
tooclose, noise, amplitude, peak2noise in that order, and an arc failing
ediff or delT has no amplitude). Those arcs complete the histogram, and
select_day() puts them back for a lower threshold, so a variant scored
by evaluate_qc_variants.py is the set of arcs gnssir would have kept, in
both directions (to the two decimals the files print amplitudes with). A
day without a failQC file (made before gnssrefl wrote one) gets nothing
back for a lower threshold. --no-failqc leaves them out.

Usage (on the station, gnssrefl_venv not needed):
    python3 analysis_tools/amplitude_thresholds.py --station usgs \\
        --from 2026-09-08 --to 2026-09-14
    ... --json thresholds.json     also write the table and thresholds

Read-only: nothing under $REFL_CODE is changed.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import gnss_record  # noqa: E402

RH_COL, AZ_COL, AMP_COL, FREQ_COL, PKNOISE_COL, MJD_COL = 2, 5, 6, 10, 13, 15
TOOCLOSE_M = 0.10        # gnssir: a peak this close to minH/maxH is rejected
MIN_ARCS = 30            # arcs a frequency needs in the window for its own threshold
DEFAULT_DAYS = 30        # window when none is given: the newest results days
FAILQC_DIR = "failQC"    # gnssrefl 4.1.5: results/<sta>/failQC/<doy>.txt

# gnssrefl 4.1.5 gnss_frequencies.FREQUENCIES (constellation, signal label)
SIGNALS = {1: "GPS L1", 2: "GPS L2", 20: "GPS L2C", 5: "GPS L5",
           101: "GLONASS L1", 102: "GLONASS L2",
           201: "Galileo L1", 205: "Galileo L5", 206: "Galileo L6", 207: "Galileo L7",
           208: "Galileo L8",
           301: "BeiDou L1", 302: "BeiDou L2", 305: "BeiDou L5", 306: "BeiDou L6",
           307: "BeiDou L7", 308: "BeiDou L8"}


# ----------------------------------------------------------------
# The gnssir json and the results files
# ----------------------------------------------------------------

def gnssir_json(refl_code: Path | str, sta: str, extension: str = "") -> dict | None:
    """The station's gnssir json, as gnssrefl 4.1.5 finds it
    (input/<sta>/[<ext>/]<sta>.json, else the older input/<sta>[.<ext>].json)."""
    base = Path(refl_code) / "input"
    paths = ([base / sta / extension / f"{sta}.json", base / f"{sta}.{extension}.json"]
             if extension else [base / sta / f"{sta}.json", base / f"{sta}.json"])
    for path in paths:
        try:
            cfg = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        return cfg if isinstance(cfg, dict) else None
    return None


def current_thresholds(cfg: dict) -> dict:
    """{frequency: reqAmp} as gnssir applies it (reqAmp[i] for freqs[i])."""
    freqs = [int(f) for f in cfg.get("freqs", [])]
    req = list(cfg.get("reqAmp", []))
    default = float(req[0]) if req else 5.0
    return {f: float(req[i]) if i < len(req) else default for i, f in enumerate(freqs)}


def parse(line: str) -> list | None:
    """The numbers of one results row, or None for a header or a short row."""
    if line.startswith("%") or not line.strip():
        return None
    try:
        c = [float(x) for x in line.split()]
    except ValueError:
        return None
    return c if len(c) > MJD_COL else None


def _rows(path: Path):
    try:
        with open(path, errors="replace") as f:
            for line in f:
                c = parse(line)
                if c is not None:
                    yield (line if line.endswith("\n") else line + "\n"), c
    except OSError:
        return


def failed_only_on_amplitude(c: list, cfg: dict, req: dict) -> bool:
    """True for a failQC row that gnssir rejected on its amplitude alone
    (see ARCS BELOW TODAY'S THRESHOLD in the module docstring)."""
    amp, pkn, rh = c[AMP_COL], c[PKNOISE_COL], c[RH_COL]
    if not (math.isfinite(amp) and math.isfinite(pkn) and math.isfinite(rh)):
        return False
    if rh == 0 and amp == 0:
        return False
    if abs(rh - float(cfg.get("minH", -1e9))) < TOOCLOSE_M or \
            abs(rh - float(cfg.get("maxH", 1e9))) < TOOCLOSE_M:
        return False
    f = int(c[FREQ_COL])
    if f not in req or amp > req[f]:
        return False
    return pkn > float(cfg.get("PkNoise", 0.0))


def day_paths(refl_code: Path | str, sta: str, d: date) -> tuple[Path, Path]:
    folder = Path(refl_code) / str(d.year) / "results" / sta
    name = f"{d.timetuple().tm_yday:03d}.txt"
    return folder / name, folder / FAILQC_DIR / name


def day_arcs(refl_code, sta: str, d: date, cfg: dict, failqc: bool = True) -> list:
    """[(line, numbers, from_failqc)] for one UTC day: every arc gnssir
    kept, and with `failqc` every arc it rejected on amplitude alone."""
    req = current_thresholds(cfg)
    kept, failed = day_paths(refl_code, sta, d)
    out = [(line, c, False) for line, c in _rows(kept)]
    if failqc:
        out += [(line, c, True) for line, c in _rows(failed)
                if failed_only_on_amplitude(c, cfg, req)]
    return out


def select_day(arcs: list, thresholds: dict, current: dict) -> list:
    """The results lines gnssir would have written with per-frequency
    thresholds `thresholds` ({freq: amp}; other frequencies keep
    `current`), in time order: an arc is kept if its amplitude is above
    its threshold (gnssir rejects amp <= reqAmp). An arc gnssir kept is
    kept as it is when its threshold is not raised, so the file
    amplitudes' two decimals never drop one; a failQC arc comes back
    only when its threshold is lowered."""
    out = []
    for line, c, from_failqc in arcs:
        f = int(c[FREQ_COL])
        cur = current.get(f)
        new = thresholds.get(f, cur)
        if new is None:
            keep = not from_failqc
        elif from_failqc:
            keep = cur is not None and new < cur and c[AMP_COL] > new
        else:
            keep = cur is None or new <= cur or c[AMP_COL] > new
        if keep:
            out.append((c[MJD_COL], line))
    out.sort(key=lambda t: t[0])
    return [line for _, line in out]


# ----------------------------------------------------------------
# Histogram and thresholds
# ----------------------------------------------------------------

def _percentile(values, p: float) -> float:
    v = sorted(values)
    if not v:
        return float("nan")
    k = (len(v) - 1) * p / 100.0
    lo, hi = math.floor(k), math.ceil(k)
    return v[lo] + (v[hi] - v[lo]) * (k - lo)


def window_days(refl_code, sta: str, first: date | None, last: date | None) -> list:
    """Results days in first..last; without a window, the newest DEFAULT_DAYS."""
    days = list(gnss_record.results_days(refl_code, sta))
    if first is None and last is None:
        return days[-DEFAULT_DAYS:]
    return [d for d in days if (first is None or d >= first) and (last is None or d <= last)]


def amplitudes(refl_code, sta: str, days, cfg: dict, window=None,
               failqc: bool = True) -> dict:
    """{freq: {"kept": [...], "failqc": [...]}}: the amplitudes of the arcs
    gnssir kept, and of those it rejected on amplitude alone, in `days`,
    azimuth in `window` ((lo, hi) degrees; None for every azimuth)."""
    out = {f: {"kept": [], "failqc": []} for f in current_thresholds(cfg)}
    for d in days:
        for _, c, from_failqc in day_arcs(refl_code, sta, d, cfg, failqc):
            if window is not None and not (window[0] <= c[AZ_COL] <= window[1]):
                continue
            s = out.setdefault(int(c[FREQ_COL]), {"kept": [], "failqc": []})
            s["failqc" if from_failqc else "kept"].append(c[AMP_COL])
    return out


def derive(amps: dict, current: dict, fraction: float | None = None,
           min_arcs: int = MIN_ARCS) -> dict:
    """threshold_f = fraction x median(Amp_f) (THE RULE above). Returns
    {"fraction", "pooled_median", "current_overall", "thresholds": {f: amp},
    "table": [per-frequency rows]}; "thresholds" holds only the
    frequencies with enough arcs for their own value (the others keep the
    json's)."""
    pooled = [a for s in amps.values() for a in s["kept"] + s["failqc"]]
    pooled_median = _percentile(pooled, 50) if pooled else float("nan")
    current_overall = _percentile(list(current.values()), 50) if current else float("nan")
    neutral = current_overall / pooled_median if pooled_median > 0 else float("nan")
    given = fraction is not None
    if not given:
        fraction = neutral
    thresholds, table = {}, []
    for f in sorted(set(current) | set(amps)):
        a = amps.get(f, {"kept": [], "failqc": []})
        every = a["kept"] + a["failqc"]
        n = len(every)
        med = _percentile(every, 50) if n else float("nan")
        cur = current.get(f)
        if n >= min_arcs and math.isfinite(fraction) and med > 0:
            new = round(fraction * med, 1)
            note = ""
        else:
            new = cur
            note = f"fewer than {min_arcs} arcs: current value kept"
        if not note:
            thresholds[f] = new
        # as select_day() keeps them
        lowered = cur is not None and new is not None and new < cur
        kept_new = (sum(1 for x in a["kept"] if new is None or cur is None or new <= cur
                        or x > new)
                    + sum(1 for x in a["failqc"] if lowered and x > new))
        table.append({"freq": f, "signal": SIGNALS.get(f, f"freq {f}"), "arcs": n,
                      "from_failqc": len(a["failqc"]), "median": med,
                      "p10": _percentile(every, 10), "p25": _percentile(every, 25),
                      "current": cur, "proposed": new, "kept_now": len(a["kept"]),
                      "kept_proposed": kept_new, "note": note})
    return {"fraction": fraction, "fraction_given": given, "neutral_fraction": neutral,
            "pooled_median": pooled_median, "current_overall": current_overall,
            "thresholds": thresholds, "table": table}


def station_json_entry(thresholds: dict) -> dict:
    """The station.json key, frequencies as strings (JSON object keys)."""
    return {"gnssrefl_amplitude_min_by_frequency":
            {str(f): thresholds[f] for f in sorted(thresholds)}}


def print_table(result: dict, first, last, failqc: bool, window) -> None:
    def num(x, w=7, p=1):
        return f"{x:{w}.{p}f}" if isinstance(x, (int, float)) and math.isfinite(x) else " " * (w - 1) + "-"
    az = f"azimuth {window[0]:g}-{window[1]:g} deg" if window else "every azimuth"
    print(f"  LSP amplitude per frequency, {first} to {last} UTC, {az}"
          + (", results + failQC arcs rejected on amplitude alone" if failqc else ", results only"))
    print(f"  {'freq':>4}  {'signal':<11} {'arcs':>6} {'failQC':>6} {'median':>7} {'p10':>7} "
          f"{'p25':>7} {'current':>7} {'propose':>7} {'kept now':>8} {'kept':>6}")
    for r in result["table"]:
        print(f"  {r['freq']:>4}  {r['signal']:<11} {r['arcs']:>6} {r['from_failqc']:>6} "
              f"{num(r['median'])} {num(r['p10'])} {num(r['p25'])} {num(r['current'], 7, 2)} "
              f"{num(r['proposed'], 7, 1)} {r['kept_now']:>8} {r['kept_proposed']:>6}"
              + (f"  ({r['note']})" if r["note"] else ""))
    if not math.isfinite(result["pooled_median"]):
        print("  no arcs in the window")
        return
    neutral = (f"{result['neutral_fraction']:.3f} = current {result['current_overall']:.2f} / "
               f"median of every frequency {result['pooled_median']:.2f}")
    print(f"  fraction {result['fraction']:.3f} (given; today's overall strictness is {neutral})"
          if result["fraction_given"] else f"  fraction {neutral} (today's overall strictness)")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[1],
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--refl-code", default=os.environ.get("REFL_CODE") or None,
                   help="gnssrefl REFL_CODE (default: $REFL_CODE, else products/refl_code)")
    p.add_argument("--station", required=True, help="gnssrefl 4-character station code")
    p.add_argument("--from", dest="first", type=date.fromisoformat, default=None,
                   help="first day of the CALM window (YYYY-MM-DD)")
    p.add_argument("--to", dest="last", type=date.fromisoformat, default=None,
                   help=f"last day of the window (default: the newest {DEFAULT_DAYS} results days)")
    p.add_argument("--fraction", type=float, default=None,
                   help="threshold = fraction x the frequency's median amplitude "
                        "(default: today's overall strictness)")
    p.add_argument("--azim", type=float, nargs=2, default=None, metavar=("FROM", "TO"),
                   help="azimuth window (default: last night's subdaily -azim1/-azim2)")
    p.add_argument("--no-failqc", action="store_true",
                   help="use only the arcs gnssir kept (the histogram is then cut at today's threshold)")
    p.add_argument("--min-arcs", type=int, default=MIN_ARCS)
    p.add_argument("--json", default=None, help="also write the table and thresholds here")
    a = p.parse_args()
    refl_code = os.path.abspath(a.refl_code or gnss_record.PROJECT_DIR / "products" / "refl_code")
    cfg = gnssir_json(refl_code, a.station)
    if cfg is None:
        print(f"  No gnssir json for {a.station} under {refl_code}/input")
        return 1
    days = window_days(refl_code, a.station, a.first, a.last)
    if not days:
        print("  No results days in that window.")
        return 1
    window = tuple(a.azim) if a.azim else gnss_record.azimuth_window(
        gnss_record.status_settings(refl_code, a.station) or "")
    failqc = not a.no_failqc
    amps = amplitudes(refl_code, a.station, days, cfg, window, failqc)
    result = derive(amps, current_thresholds(cfg), a.fraction, a.min_arcs)
    print_table(result, days[0], days[-1], failqc, window)
    entry = station_json_entry(result["thresholds"])
    print("  station.json (opt-in; score it first with evaluate_qc_variants.py):")
    print("    " + json.dumps(entry)[1:-1])
    if a.json:
        Path(a.json).write_text(json.dumps(dict(result, window=[str(days[0]), str(days[-1])],
                                                azimuth=window, failqc=failqc,
                                                station_json=entry), indent=1, default=str) + "\n")
        print(f"  wrote {a.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
