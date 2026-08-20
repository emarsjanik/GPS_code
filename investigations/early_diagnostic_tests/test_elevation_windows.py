
import csv
import math


PATH = "gnssir_tide_arc_analysis.csv"


# ------------------------------------------------------------
# Pearson correlation
# ------------------------------------------------------------

def pearson(pairs):

    if len(pairs) < 3:
        return float("nan")

    xs = [p[0] for p in pairs]
    ys = [p[1] for p in pairs]

    mx = sum(xs) / len(xs)
    my = sum(ys) / len(ys)

    cov = sum(
        (x - mx) * (y - my)
        for x, y in zip(xs, ys)
    )

    sx = math.sqrt(
        sum((x - mx) ** 2 for x in xs)
    )

    sy = math.sqrt(
        sum((y - my) ** 2 for y in ys)
    )

    if sx == 0 or sy == 0:
        return float("nan")

    return cov / (sx * sy)


# ------------------------------------------------------------
# Load dataset
# ------------------------------------------------------------

rows = []

with open(PATH, newline="") as f:

    for r in csv.DictReader(f):

        try:

            az = float(r["Azim"])
            gnss = float(r["gnss_water_level_m"])
            tide = float(r["tide_solution_m"])
            emin = float(r["eminO"])
            emax = float(r["emaxO"])

        except (ValueError, KeyError):
            continue

        values = (az, gnss, tide, emin, emax)

        if not all(math.isfinite(x) for x in values):
            continue

        rows.append({
            "az": az,
            "gnss": gnss,
            "tide": tide,
            "emin": emin,
            "emax": emax,
        })


print()
print("=" * 100)
print("GNSS-IR / TIDE — ELEVATION WINDOW TEST")
print("=" * 100)

print()
print(f"Dataset: {PATH}")
print(f"Usable records: {len(rows)}")

print()
print("Azimuth sectors:")
print("  Sector A = 100–130 degrees")
print("  Sector B = 150–215 degrees")

print()
print("Reference elevation range:")
print("  Production = 5–15 degrees")


# ------------------------------------------------------------
# Elevation windows
#
# Each window means:
#
#   observed arc must be entirely contained
#   within the requested elevation range.
#
# Example:
#
#   5–10 means eminO >= 5 and emaxO <= 10
# ------------------------------------------------------------

windows = [
    (5, 15),
    (5, 13),
    (5, 12),
    (5, 10),
    (7, 15),
    (8, 15),
    (10, 15),
]


# ------------------------------------------------------------
# Calculate statistics
# ------------------------------------------------------------

def analyze(records):

    if len(records) == 0:
        return None

    pairs = [
        (r["tide"], r["gnss"])
        for r in records
    ]

    n = len(pairs)

    r = pearson(pairs)

    bias = sum(
        gnss - tide
        for tide, gnss in pairs
    ) / n

    residuals = [
        gnss - (tide + bias)
        for tide, gnss in pairs
    ]

    rms = math.sqrt(
        sum(x * x for x in residuals) / n
    )

    return n, r, bias, rms


# ------------------------------------------------------------
# Run analysis
# ------------------------------------------------------------

for emin_limit, emax_limit in windows:

    print()
    print("-" * 100)

    print(
        f"Elevation window: "
        f"{emin_limit}–{emax_limit} degrees"
    )

    print("-" * 100)

    for label, azmin, azmax in [
        ("100–130", 100, 130),
        ("150–215", 150, 215),
    ]:

        selected = [

            r for r in rows

            if (
                azmin <= r["az"] <= azmax
                and r["emin"] >= emin_limit
                and r["emax"] <= emax_limit
            )
        ]

        result = analyze(selected)

        if result is None:

            print(
                f"{label:>9s} : "
                "n=0"
            )

            continue

        n, r, bias, rms = result

        print(
            f"{label:>9s} : "
            f"n={n:3d}  "
            f"r={r:+.4f}  "
            f"bias={bias:+.3f} m  "
            f"bias-removed RMS={rms * 100:6.2f} cm"
        )


# ------------------------------------------------------------
# Additional test:
# progressively remove the LOWEST elevations
# while keeping the production upper limit at 15 degrees.
# ------------------------------------------------------------

print()
print()
print("=" * 100)
print("PROGRESSIVE LOW-ELEVATION CUT")
print("=" * 100)

print()
print(
    "This asks whether removing the lowest-elevation "
    "reflections improves the tide relationship."
)

for lower in [5, 6, 7, 8, 9, 10, 11, 12]:

    print()
    print(
        f"Minimum elevation = {lower}° "
        f"(maximum remains 15°)"
    )

    for label, azmin, azmax in [
        ("100–130", 100, 130),
        ("150–215", 150, 215),
    ]:

        selected = [

            r for r in rows

            if (
                azmin <= r["az"] <= azmax
                and r["emin"] >= lower
                and r["emax"] <= 15
            )
        ]

        result = analyze(selected)

        if result is None:

            print(
                f"  {label:>9s} : n=0"
            )

            continue

        n, r, bias, rms = result

        print(
            f"  {label:>9s} : "
            f"n={n:3d}  "
            f"r={r:+.4f}  "
            f"bias={bias:+.3f} m  "
            f"RMS={rms * 100:6.2f} cm"
        )


print()
print("=" * 100)
print("DONE")
print("=" * 100)
