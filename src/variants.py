"""Run controlled variants of the backtest to separate signal quality from
risk-parameter choices. Signals are computed once and reused."""
import config
import backtest


def main():
    prepared = backtest.prepare()

    variants = [
        ("BASELINE  (SL 25% margin, th 0.8/0.2)", {}, False),
        ("TIGHT SL  (10% of margin)", {"SL_PCT_OF_MARGIN": 0.10}, False),
        ("LOOSE SL  (50% of margin)", {"SL_PCT_OF_MARGIN": 0.50}, False),
        ("NO SL     (spread max loss only)", {"SL_PCT_OF_MARGIN": 10.0}, False),
        ("STRICT TH (0.9/0.1)", {"LONG_TH": 0.9, "SHORT_TH": 0.1}, False),
        ("INVERTED  (signal flipped, diagnostics)", {}, True),
    ]

    defaults = {k: getattr(config, k) for k in
                ["SL_PCT_OF_MARGIN", "LONG_TH", "SHORT_TH"]}
    for name, overrides, invert in variants:
        for k, v in {**defaults, **overrides}.items():
            setattr(config, k, v)
        print(f"\n########  {name}  ########", flush=True)
        tag = name.split("(")[0].strip().lower().replace(" ", "_")
        backtest.run(prepared=prepared, invert=invert, tag=tag)


if __name__ == "__main__":
    main()
