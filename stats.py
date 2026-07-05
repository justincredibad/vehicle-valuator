"""
Stats engine — turns raw scraped listings into a price estimate.

No LLM involved anywhere in this file. Everything here is deterministic
and auditable: same input listings always produce the same output number.
"""

from dataclasses import dataclass, field
import statistics

import config


@dataclass
class PriceEstimate:
    point_estimate: int
    low: int                       # point_estimate - 10%
    high: int                      # point_estimate + 10%
    sample_size: int
    outliers_removed: int
    confidence: str                # "high" / "medium" / "low"
    notes: list[str] = field(default_factory=list)


def _iqr_filter(prices: list[int]) -> tuple[list[int], int]:
    """Remove outliers using the Tukey IQR method. Returns (kept, num_removed)."""
    if len(prices) < 4:
        return prices, 0  # too few points for IQR to be meaningful

    sorted_prices = sorted(prices)
    n = len(sorted_prices)
    q1 = sorted_prices[n // 4]
    q3 = sorted_prices[(3 * n) // 4]
    iqr = q3 - q1
    lower_fence = q1 - config.IQR_OUTLIER_MULTIPLIER * iqr
    upper_fence = q3 + config.IQR_OUTLIER_MULTIPLIER * iqr

    kept = [p for p in prices if lower_fence <= p <= upper_fence]
    removed = len(prices) - len(kept)
    return kept, removed


def estimate_price(listings: list[dict], target_year: int | None = None) -> PriceEstimate | None:
    """
    Compute a price estimate with a 10% buffer from a list of scraped listings.

    listings: list of dicts with at least a 'price' key (from scraper.py).
    target_year: if provided and listings span multiple years, listings closer
                 to this year are weighted more heavily via a simple year-distance
                 adjustment on the median.

    Returns None if there's no usable data at all.
    """
    notes = []
    prices = [l["price"] for l in listings if l.get("price")]

    if not prices:
        return None

    raw_count = len(prices)
    filtered_prices, removed = _iqr_filter(prices)

    if not filtered_prices:
        # IQR filter removed everything (shouldn't normally happen) — fall back to raw
        filtered_prices = prices
        removed = 0
        notes.append("Outlier filter was too aggressive; used raw data instead.")

    median_price = int(statistics.median(filtered_prices))

    # Optional: nudge estimate based on year distance if listings span a range
    # and we know the buyer's target year specifically.
    if target_year:
        same_year_prices = [
            l["price"] for l in listings
            if l.get("reg_year") == target_year and l.get("price")
        ]
        if len(same_year_prices) >= 3:
            # Enough same-year data to trust it directly over the blended median
            same_year_filtered, _ = _iqr_filter(same_year_prices)
            if same_year_filtered:
                median_price = int(statistics.median(same_year_filtered))
                notes.append(
                    f"Used {len(same_year_filtered)} listings specifically from "
                    f"{target_year} rather than the full year-range blend."
                )

    low = int(median_price * (1 - config.PRICE_BUFFER_PCT))
    high = int(median_price * (1 + config.PRICE_BUFFER_PCT))

    # Confidence heuristic based purely on sample size — simple and explainable
    if raw_count >= 15:
        confidence = "high"
    elif raw_count >= config.MIN_SAMPLE_SIZE:
        confidence = "medium"
    else:
        confidence = "low"
        notes.append(
            f"Only {raw_count} listings found — estimate may be unreliable. "
            "Consider widening the year range or model variant."
        )

    if removed:
        notes.append(f"Removed {removed} outlier listing(s) before computing the median.")

    return PriceEstimate(
        point_estimate=median_price,
        low=low,
        high=high,
        sample_size=raw_count,
        outliers_removed=removed,
        confidence=confidence,
        notes=notes,
    )


if __name__ == "__main__":
    # Quick sanity check with fake data
    fake_listings = [
        {"price": 45000, "reg_year": 2019},
        {"price": 47000, "reg_year": 2019},
        {"price": 46500, "reg_year": 2019},
        {"price": 120000, "reg_year": 2019},  # outlier
        {"price": 44000, "reg_year": 2019},
        {"price": 48000, "reg_year": 2019},
    ]
    result = estimate_price(fake_listings, target_year=2019)
    print(result)
