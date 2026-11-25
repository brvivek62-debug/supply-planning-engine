"""Data profile (MASTER_PROMPT 14): ``reports/data_profile.md`` + ``reports/figures/*.png`` from the written tables.

Profiles demand share by ABC class, CV distribution, promo spike rate, a seasonality example, lead time by mode,
supplier capacity utilisation, delay rate and PO status mix.
"""
from __future__ import annotations

from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from planner import calendar as cal  # noqa: E402
from planner import tableio  # noqa: E402
from planner.config import Config  # noqa: E402

NAVY, ACCENT, GREY = "#1f3a5f", "#d08c2e", "#8a94a6"


def _save(fig, folder: Path, name: str) -> str:
    folder.mkdir(parents=True, exist_ok=True)
    fig.tight_layout()
    fig.savefig(folder / name, dpi=110, metadata={"Software": None})
    plt.close(fig)
    return name


def _ax(title: str, size=(6.4, 3.6)):
    fig, ax = plt.subplots(figsize=size)
    ax.set_title(title, fontsize=11, loc="left")
    ax.spines[["top", "right"]].set_visible(False)
    return fig, ax


def profile(t: dict[str, pd.DataFrame], cfg: Config) -> dict:
    fd, sku, po, lt = t["forecast_demand"], t["sku"].set_index("sku_id"), t["purchase_order"], t["lead_time"]
    fd = fd.assign(abc=fd["sku_id"].map(sku["abc_class"]), category=fd["sku_id"].map(sku["category"]))
    vol = fd.groupby("abc")["forecast_qty_ea"].sum()
    n_sku = sku[sku["sku_type"] == "FINISHED"]["abc_class"].value_counts()
    abc = pd.DataFrame({"skus": n_sku, "demand_ea": vol}).fillna(0).sort_index()
    abc["sku_share"] = abc["skus"] / abc["skus"].sum()
    abc["demand_share"] = abc["demand_ea"] / abc["demand_ea"].sum()
    g = fd.groupby(["sku_id", "location_id"])["forecast_qty_ea"]
    cv = (g.std(ddof=0) / g.mean()).replace([np.inf], np.nan).dropna()
    driver = fd["demand_driver"].value_counts(normalize=True).sort_index()
    promo = fd[fd["demand_driver"] == "PROMO"]
    promo_lift = (promo["forecast_qty_ea"] / promo["baseline_qty_ea"].where(promo["baseline_qty_ea"] > 0)).dropna()
    season = fd.groupby(["category", "week"])["baseline_qty_ea"].sum().unstack(0).sort_index()
    std = lt[~lt["is_expedite_lane"]] if lt["is_expedite_lane"].dtype == bool else lt
    lead = lt.groupby("transport_mode")["total_lead_weeks"].describe()[["count", "min", "50%", "mean", "max"]]
    # utilisation: firm PO + planned order receipts per supplier-week / capacity
    cap = t["supplier_capacity"].set_index(["supplier_id", "week"])["capacity_qty_ea"]
    open_po = po[~po["status"].isin(["RECEIVED", "CANCELLED"])]
    firm = open_po.groupby(["supplier_id", "revised_receipt_week"])["qty_base_ea"].sum()
    firm.index.names = ["supplier_id", "week"]
    load = firm.reindex(cap.index, fill_value=0).astype(float)
    if "planned_order" in t:
        pl = t["planned_order"].groupby(["supplier_id", "receipt_week"])["qty_base_ea"].sum()
        pl.index.names = ["supplier_id", "week"]
        load = load + pl.reindex(cap.index, fill_value=0)
    util = (load / cap.where(cap > 0)).dropna()
    status = po["status"].value_counts().sort_index()
    shipped_or_open = po[~po["status"].isin(["CANCELLED"])]
    delay_rate = float((shipped_or_open["delay_weeks"] > 0).mean())
    return {"abc": abc, "cv": cv, "driver": driver, "promo_lift": promo_lift, "season": season, "lead": lead,
            "util": util, "status": status, "delay_rate": delay_rate, "delays": po["delay_weeks"].value_counts().sort_index(),
            "rows": len(fd), "std_lanes": len(std)}


def figures(p: dict, folder: Path) -> list[str]:
    out = []
    fig, ax = _ax("Demand share vs SKU share by ABC class")
    x = np.arange(len(p["abc"]))
    ax.bar(x - 0.2, p["abc"]["sku_share"], 0.4, color=GREY, label="share of SKUs")
    ax.bar(x + 0.2, p["abc"]["demand_share"], 0.4, color=NAVY, label="share of demand")
    ax.set_xticks(x, p["abc"].index)
    ax.yaxis.set_major_formatter(matplotlib.ticker.PercentFormatter(1.0))
    ax.legend(frameon=False)
    out.append(_save(fig, folder, "demand_share_by_abc.png"))

    fig, ax = _ax("Coefficient of variation of weekly demand per SKU-location")
    ax.hist(p["cv"].clip(upper=2), bins=40, color=NAVY)
    ax.set_xlabel("CV (std / mean, clipped at 2)")
    ax.set_ylabel("SKU-locations")
    out.append(_save(fig, folder, "cv_distribution.png"))

    fig, ax = _ax("PROMO lift (forecast / baseline) on promo weeks")
    ax.hist(p["promo_lift"].clip(upper=5), bins=30, color=ACCENT)
    ax.set_xlabel("lift x")
    ax.set_ylabel("SKU-location-weeks")
    out.append(_save(fig, folder, "promo_lift.png"))

    fig, ax = _ax("Seasonality: baseline demand by category (index, mean = 1)", size=(7.2, 3.8))
    s = p["season"] / p["season"].mean()
    wk = [cal.week_num(w) for w in s.index]
    for c in s.columns:
        ax.plot(wk, s[c], lw=1.4, label=c.replace("_", " ").title())
    ax.set_xlabel("ISO week 2026")
    ax.legend(frameon=False, fontsize=7, ncol=2)
    out.append(_save(fig, folder, "seasonality_by_category.png"))

    fig, ax = _ax("Total lead time by transport mode (weeks)")
    lead = p["lead"]
    ax.bar(lead.index, lead["mean"], color=NAVY)
    ax.errorbar(lead.index, lead["mean"], yerr=[lead["mean"] - lead["min"], lead["max"] - lead["mean"]],
                fmt="none", ecolor=GREY, capsize=4)
    ax.set_ylabel("weeks (bar = mean, whisker = min-max)")
    out.append(_save(fig, folder, "lead_time_by_mode.png"))

    fig, ax = _ax("Supplier capacity utilisation per supplier-week (firm + planned receipts)")
    ax.hist(p["util"].clip(upper=1.5), bins=40, color=NAVY)
    ax.axvline(1.0, color=ACCENT, lw=1.2)
    ax.set_xlabel("load / capacity (clipped at 1.5)")
    ax.set_ylabel("supplier-weeks")
    out.append(_save(fig, folder, "supplier_utilisation.png"))

    fig, ax = _ax("Purchase order status mix")
    ax.barh(p["status"].index, p["status"].values, color=NAVY)
    ax.invert_yaxis()
    ax.set_xlabel("POs")
    out.append(_save(fig, folder, "po_status_mix.png"))
    return out


def _md(df: pd.DataFrame, fmt: dict | None = None) -> str:
    fmt = fmt or {}
    cols = [df.index.name or ""] + list(df.columns)
    lines = ["| " + " | ".join(str(c) for c in cols) + " |", "|" + "|".join("---" for _ in cols) + "|"]
    for idx, r in df.iterrows():
        vals = [fmt.get(c, "{}").format(v) for c, v in r.items()]
        lines.append(f"| {idx} | " + " | ".join(vals) + " |")
    return "\n".join(lines)


def render(cfg: Config, p: dict, figs: list[str], fig_rel: str) -> str:
    u = p["util"]
    cvq = p["cv"].quantile([0.1, 0.5, 0.9])
    abc = p["abc"].rename_axis("abc_class")
    return "\n".join([
        f"# Data profile — scale `{cfg.scale}`, seed {cfg.seed}", "",
        "Generated by `python -m planner export` from the written input tables (after injection).", "",
        "## Demand", "",
        f"Forecast rows: {p['rows']:,}.", "",
        _md(abc, {"skus": "{:,.0f}", "demand_ea": "{:,.0f}", "sku_share": "{:.1%}", "demand_share": "{:.1%}"}), "",
        f"- CV of weekly demand per SKU-location: p10 {cvq[0.1]:.2f}, median {cvq[0.5]:.2f}, p90 {cvq[0.9]:.2f}",
        "- Demand driver mix: " + ", ".join(f"{k} {v:.2%}" for k, v in p["driver"].items()),
        f"- PROMO spike rate {p['driver'].get('PROMO', 0):.2%} of SKU-location-weeks; lift median "
        f"{p['promo_lift'].median():.2f}x (range {p['promo_lift'].min():.2f}-{p['promo_lift'].max():.2f}x)", "",
        "## Supply", "",
        _md(p["lead"].rename_axis("transport_mode"), {"count": "{:,.0f}", "min": "{:.0f}", "50%": "{:.0f}", "mean": "{:.2f}", "max": "{:.0f}"}), "",
        f"- Supplier capacity utilisation (firm + planned receipts / capacity) per supplier-week: median {u.median():.1%}, "
        f"p90 {u.quantile(0.9):.1%}, weeks at or above capacity {(u >= 1).mean():.1%} (injected overloads + MRP filling capacity)",
        f"- Transport delay rate (non-cancelled POs with delay_weeks > 0): {p['delay_rate']:.2%}; delay weeks: "
        + ", ".join(f"{k} wk {v:,}" for k, v in p["delays"].items()),
        "- PO status mix: " + ", ".join(f"{k} {v:,} ({v / p['status'].sum():.1%})" for k, v in p["status"].items()), "",
        "## Figures", "",
        *[f"![{f[:-4]}]({fig_rel}/{f})" for f in figs], "",
    ])


def run(cfg: Config, base: Path | None = None, out_dir: Path | None = None) -> Path:
    base = Path(base) if base else tableio.data_dir(cfg.scale)
    t = tableio.read_all(base, tableio.fmt_of(cfg), ["forecast_demand", "sku", "purchase_order", "lead_time",
                                                       "supplier_capacity", "planned_order"])
    folder = Path(out_dir) if out_dir else tableio.reports_dir(cfg.scale, base)
    p = profile(t, cfg)
    figs = figures(p, folder / "figures")
    path = folder / "data_profile.md"
    path.write_text(render(cfg, p, figs, "figures"), encoding="utf-8")
    return path
