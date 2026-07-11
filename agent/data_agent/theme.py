"""Visual theme for DataAgent charts & reports.

One palette, one type scale, one set of ECharts base options — so every chart
and every report looks like it came from the same designer. This is the
"beauty governance" from batch 13 of the demo plan: consistency is most of what
makes a dashboard look professional.

The teal accent (#2DD4BF) matches the DAClaw brand badge in the README.
"""

from __future__ import annotations

from typing import Any, Dict, List

# -- brand + categorical palette -------------------------------------------

BRAND = "#2DD4BF"  # DAClaw teal
INK = "#0f172a"  # slate-900, primary text
MUTED = "#64748b"  # slate-500, secondary text
GRID = "#e2e8f0"  # slate-200, gridlines / borders
PANEL_BG = "#ffffff"
PAGE_BG = "#f8fafc"  # slate-50

# A calm, high-contrast categorical palette (works on white).
PALETTE: List[str] = [
    "#2DD4BF",  # teal
    "#6366F1",  # indigo
    "#F59E0B",  # amber
    "#EF4444",  # red
    "#10B981",  # emerald
    "#3B82F6",  # blue
    "#EC4899",  # pink
    "#8B5CF6",  # violet
    "#14B8A6",  # teal-600
    "#F97316",  # orange
]

# Sequential ramp for heatmaps (light -> brand).
HEAT_RAMP: List[str] = ["#f0fdfa", "#99f6e4", "#2DD4BF", "#0d9488", "#134e4a"]

FONT_STACK = (
    '-apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", '
    '"Microsoft YaHei", Roboto, Helvetica, Arial, sans-serif'
)


def echarts_base_options() -> Dict[str, Any]:
    """Shared ECharts options merged into every chart for a consistent look."""
    return {
        "color": PALETTE,
        "textStyle": {"fontFamily": FONT_STACK, "color": INK},
        "grid": {"left": 48, "right": 24, "top": 48, "bottom": 48, "containLabel": True},
        "tooltip": {
            "trigger": "item",
            "backgroundColor": "rgba(15,23,42,0.92)",
            "borderColor": "rgba(15,23,42,0.92)",
            "textStyle": {"color": "#fff"},
        },
        "legend": {"textStyle": {"color": MUTED}, "top": 8},
    }


def axis_style() -> Dict[str, Any]:
    """Common axis styling (applied to both x and y)."""
    return {
        "axisLine": {"lineStyle": {"color": GRID}},
        "axisTick": {"show": False},
        "axisLabel": {"color": MUTED},
        "splitLine": {"lineStyle": {"color": GRID, "type": "dashed"}},
    }
