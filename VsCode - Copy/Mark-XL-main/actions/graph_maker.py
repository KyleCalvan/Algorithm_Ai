"""
Graph & chart generation for MARK XL.

Three families of visualisation:
  • data charts    — bar / line / area / pie / scatter / histogram / doughnut
  • function plots — any f(n) expression, plus a built-in Big-O growth comparison
  • networks       — graph-theory graphs (nodes + edges) with several layouts

Rendering: matplotlib → PNG when it is installed; otherwise a self-contained
HTML/SVG page produced with the standard library alone, so charts still work in
a bare virtualenv. The finished file is opened with the OS default viewer and its
path is returned to the caller (see GraphResult).
"""
import ast
import functools
import math
import os
import platform
import random
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

# The desktop console is cp1252 by default on Windows; never let a symbol in a
# log line crash the assistant.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
except Exception:
    pass


def get_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


BASE_DIR   = get_base_dir()
OUTPUT_DIR = BASE_DIR / "output" / "graphs"
_SYSTEM    = platform.system()

_PALETTE = [
    "#4f8ef7", "#f78f4f", "#3fbf8e", "#b07fe0", "#f2c94c",
    "#ef6f6f", "#3fc4d4", "#8d99ae", "#e8849b", "#6a9a3f",
]
_ACCENT   = "#e8614a"
_TEXT     = "#232b36"
_GRID     = "#dfe4ea"
_AXIS     = "#8a94a3"

_FILE_COUNTER = 0


class _Margins:
    """Plot insets, read as ``margin.l`` rather than ``margin.l``.

    An f-string that nests the same quote character around a subscript —
    ``f'...{margin.l}...'`` — is only valid syntax on Python ≥ 3.12 (PEP 701).
    On 3.11 and older it is a SyntaxError at import time, which would stop the
    whole assistant from launching. Attribute access keeps every f-string
    quote-free on any version.
    """

    __slots__ = ("l", "r", "t", "b")

    def __init__(self, l, r, t, b):
        self.l, self.r, self.t, self.b = l, r, t, b

    def __getitem__(self, key):
        return getattr(self, key)


class GraphResult(str):
    """A spoken result that also carries the path of the file it describes.

    It IS a plain str, so the model just sees the sentence — but main.py can
    pull `.path` off it to push the image to the phone dashboard.
    """

    def __new__(cls, text: str, path: str = "", fmt: str = ""):
        self = super().__new__(cls, text)
        self.path = path
        self.format = fmt
        return self


# ── shared helpers ────────────────────────────────────────────────────────────

def _to_float(v, default=None):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _slugify(text: str, fallback: str = "graph") -> str:
    s = re.sub(r"[^\w\-]+", "_", (text or "").strip().lower()).strip("_")
    return (s or fallback)[:60]


def _unique_path(title: str, ext: str) -> Path:
    global _FILE_COUNTER
    _FILE_COUNTER += 1
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    name = f"{_slugify(title)}_{stamp}_{_FILE_COUNTER:02d}.{ext.lstrip('.')}"
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    return OUTPUT_DIR / name


def _open_file(path: Path) -> bool:
    try:
        if _SYSTEM == "Windows":
            os.startfile(str(path))                       # type: ignore[attr-defined]
        elif _SYSTEM == "Darwin":
            subprocess.Popen(["open", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path)])
        return True
    except Exception as e:
        print(f"[graph_maker] could not open {path.name}: {e}")
        return False


def _has_matplotlib() -> bool:
    try:
        import matplotlib  # noqa: F401
        return True
    except Exception:
        return False


def _matplotlib():
    import matplotlib
    matplotlib.use("Agg")          # headless / off-main-thread safe
    import matplotlib.pyplot as plt
    return plt


def _collect_series(args: dict, chart_type: str = "") -> tuple[list[tuple[str, list]], int]:
    """Normalise labels/values/series into [(name, [numbers]), …].

    Accepts a map {name: [values]} or a list of {"name": …, "values": […]} records,
    so either function-schema shape reaches the same place. For scatter charts
    [x, y] pairs are kept as tuples — the renderers handle both shapes.

    Returns the series plus a count of values that could not be turned into a
    number, so the caller can warn the user instead of quietly drawing a chart
    with fewer bars than categories were asked for.
    """
    out: list[tuple[str, list]] = []
    dropped = 0

    def norm(vals) -> list:
        nonlocal dropped
        nums = []
        for v in (vals or []):
            if chart_type == "scatter" and isinstance(v, (list, tuple)) and len(v) >= 2:
                x, y = _to_float(v[0]), _to_float(v[1])
                if x is not None and y is not None:
                    nums.append((x, y))
                else:
                    dropped += 1
            else:
                n = _to_float(v)
                if n is not None:
                    nums.append(n)
                else:
                    dropped += 1
        return nums

    series = args.get("series")
    if isinstance(series, dict):
        for name, vals in series.items():
            nums = norm(vals)
            if nums:
                out.append((str(name), nums))
    elif isinstance(series, list):
        for item in series:
            if isinstance(item, dict):
                name = item.get("name") or item.get("label") or "Series"
                vals = item.get("values") or item.get("data") or []
            else:
                name, vals = "Series", item if isinstance(item, list) else []
            nums = norm(vals)
            if nums:
                out.append((str(name), nums))
    if not out:
        raw = args.get("values")
        if raw is None:
            raw = args.get("data")
        if not isinstance(raw, list):
            raw = [raw] if raw is not None else []
        nums = norm(raw)
        if nums:
            out.append((str(args.get("series_name") or "Series"), nums))
    return out, dropped


def _labels_for(args: dict, n: int) -> list[str]:
    labels = args.get("labels")
    if not labels:
        labels = args.get("categories")
    if not isinstance(labels, list):
        labels = [str(x) for x in (labels or [])]
    labels = [str(x) for x in labels]
    if len(labels) < n:
        labels = labels + [str(i + 1) for i in range(len(labels), n)]
    return labels[:n] if labels else [str(i + 1) for i in range(n)]


# ── safe maths for function plots ─────────────────────────────────────────────

def _factorial(x):
    """math.factorial, but tolerant of the float n every curve is sampled at.

    math.factorial raises TypeError on a float, so the raw function made
    "factorial(n)" unplotable. Round to the nearest integer (the only domain
    factorial is defined on) and cap the argument — the plot range is clamped
    to 1000 anyway, so the cap costs nothing real and keeps a hostile literal
    like factorial(1e300) from hanging on a gigantic integer.
    """
    k = int(round(x))
    if k < 0 or k > 1000:
        raise ValueError("factorial argument out of range")
    return math.factorial(k)


# ── keeping eval() honest ─────────────────────────────────────────────────────
#
# _eval_expr runs eval() on text the caller typed. Float overflow is already
# harmless: int ** float raises OverflowError instead of computing, and every
# renderer catches that per point. The one shape that can hang the whole tool is
# *integer* exponentiation — `10**10**10**10` spends forever building an int with
# 10**10 digits inside a C loop that holds the GIL, so no thread, signal or
# asyncio timeout can interrupt it (the plot runs on loop.run_in_executor in
# main.py, so the turn simply stalls with no error). Bound the size of the result
# *before* any arithmetic happens, by routing every `**` through _safe_pow.
_MAX_RESULT_DIGITS = 10_000     # 10**10000 is 40 KiB and a couple of ms — plenty for a plot.


class _PowerRewriter(ast.NodeTransformer):
    """Rewrite `a ** b` as `_pow(a, b)` so every exponentiation is bounded."""

class _PowerRewriter(ast.NodeTransformer):
    """Rewrite `a ** b` as `_pow(a, b)` so every exponentiation is bounded."""

    def visit_BinOp(self, node):
        self.generic_visit(node)
        if isinstance(node.op, ast.Pow):
            return ast.Call(func=ast.Name(id="_pow", ctx=ast.Load()),
                            args=[node.left, node.right], keywords=[])
        return node


# Every node type a plot expression may legitimately use.  Anything else — an
# attribute lookup, a subscript, a lambda, a comprehension, a walrus — is
# rejected before eval() is ever called, so the string coming from the caller
# cannot reach anything outside _SAFE_NAMES.  ``{"__builtins__": {}}`` alone is
# not enough: ``factorial`` is a real function object, and
# ``factorial.__globals__`` would hand back this module's builtins.
_ALLOWED_EXPR_NODES = (
    ast.Expression, ast.BinOp, ast.UnaryOp, ast.Constant, ast.Name, ast.Call,
    ast.keyword,
    ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod, ast.Pow,
    ast.USub, ast.UAdd, ast.Load,
)


@functools.lru_cache(maxsize=512)
def _compile_expr(expr: str):
    """Parse an expression once, with `**` routed through _safe_pow.

    Returns a code object; raises SyntaxError on garbage, which the callers
    already treat as "not a valid expression".
    """
    tree = ast.parse(expr, mode="eval")
    for node in ast.walk(tree):
        # Reject any name the environment does not define, so an expression that
        # merely *looks* parseable (``__import__('os')``) is turned away here
        # instead of relying on eval's own NameError.
        if isinstance(node, ast.Name) and node.id not in _SAFE_NAMES:
            raise SyntaxError(f"unknown name in a plot expression: {node.id}")
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitXor):
            # `^` binds *looser* than `+` in Python, so `n^2 + 1` would mean
            # n ** (2+1).  The callers normalise the caret as text before it
            # reaches here, where the substitution keeps the intended math;
            # a raw caret therefore has no meaning and is refused.
            raise SyntaxError("use ** (or ^ written as text) for powers, not ^")
        if not isinstance(node, _ALLOWED_EXPR_NODES):
            raise SyntaxError(f"not allowed in a plot expression: {type(node).__name__}")
    _PowerRewriter().visit(tree)
    ast.fix_missing_locations(tree)
    return compile(tree, "<graph_maker expr>", "eval")


def _safe_pow(a, b, mod=None):
    """`**` that cannot build a gigantic integer.

    With a float operand, `a ** b` raises OverflowError and the callers handle
    it. Two ints have no such ceiling, and the exponent alone sets the digit
    count (``b * log10(a)``), so bound that figure before the arithmetic rather
    than after it.
    """
    if mod is not None:
        return pow(a, b, mod)                    # modular pow cannot blow up
    if isinstance(b, int) and b > 0 and isinstance(a, int) and abs(a) > 1:
        try:
            size = b * math.log10(abs(a))        # ≈ digits the result would have
        except (OverflowError, ValueError):
            size = math.inf
        if size > _MAX_RESULT_DIGITS:
            raise OverflowError(
                "that exponent is far too large to plot — try a smaller one")
    return a ** b


_SAFE_NAMES = {
    "log": math.log2, "log2": math.log2, "log10": math.log10, "ln": math.log,
    "sqrt": math.sqrt, "abs": abs, "pow": _safe_pow, "exp": math.exp,
    "min": min, "max": max, "floor": math.floor, "ceil": math.ceil,
    "factorial": _factorial, "n": 0.0, "pi": math.pi, "e": math.e,
    "_pow": _safe_pow,
}


def _eval_expr(expr: str, n: float) -> float:
    # A fresh env per call: _SAFE_NAMES is module-level and shared, so writing
    # "n" into it would race between two concurrent plot calls and one of them
    # would be evaluated at the other one's x.
    env = dict(_SAFE_NAMES)
    env["n"] = n
    return float(eval(_compile_expr(expr), {"__builtins__": {}}, env))  # noqa: S307


def _probe_expr(expr: str, lo: float, hi: float) -> bool:
    """Return True if ``expr`` yields a finite value anywhere on [lo, hi].

    Probing only n=1 wrongly rejected perfectly good curves that happen to be
    undefined exactly there — log2(n-1), 1/(n-1) — with a "you wrote it wrong"
    reply. The renderers already skip points that raise or go infinite, so an
    expression only has to be *somewhere* valid on the requested range.
    """
    for p in {1.0, lo, hi, (lo + hi) / 2}:
        try:
            if math.isfinite(_eval_expr(expr, p)):
                return True
        except Exception:
            pass
    return False


_BIG_O = {
    "O(1)":       "1",
    "O(log n)":   "log2(n)",
    "O(n)":       "n",
    "O(n log n)": "n*log2(n)",
    "O(n^2)":     "n**2",
    "O(n^3)":     "n**3",
    "O(2^n)":     "2**n",
    "O(n!)":      "factorial(n)",
}


def _normalise_expression(expr: str) -> str:
    expr = str(expr).strip()
    # `^` means exponentiation, not XOR. Rewrite it even when `**` is already
    # present elsewhere in the expression — `2^n + n**2` must not keep its caret,
    # or the float `n` makes Python attempt XOR and the whole curve is dropped.
    return expr.replace("^", "**")             # n^2 → n**2


# ── network (graph-theory) helpers ────────────────────────────────────────────

def _collect_graph(args: dict) -> tuple[list[str], list[tuple[str, str, object]]]:
    raw_nodes = args.get("nodes")
    if isinstance(raw_nodes, dict):
        nodes = [str(k) for k in raw_nodes.keys()]
    elif isinstance(raw_nodes, list):
        nodes = [str(n) for n in raw_nodes]
    else:
        nodes = []

    edges: list[tuple[str, str, object]] = []
    for e in (args.get("edges") or []):
        if not isinstance(e, (list, tuple)) or len(e) < 2:
            continue
        a, b = str(e[0]), str(e[1])
        w = e[2] if len(e) > 2 else None
        edges.append((a, b, w))
        for x in (a, b):
            if x not in nodes:
                nodes.append(x)              # auto-register nodes named only in edges
    return nodes, edges


def _adjacency(nodes: list[str], edges, directed: bool):
    adj = {n: set() for n in nodes}
    for a, b, _w in edges:
        if a in adj and b in adj:
            adj[a].add(b)
            if not directed:
                adj[b].add(a)
    return adj


def _layout_circular(nodes: list[str]) -> dict:
    n = len(nodes)
    if n == 1:
        return {nodes[0]: (0.5, 0.5)}
    return {
        node: (0.5 + 0.42 * math.cos(2 * math.pi * i / n),
               0.5 + 0.42 * math.sin(2 * math.pi * i / n))
        for i, node in enumerate(nodes)
    }


def _layout_layers(nodes: list[str], edges, root=None) -> dict:
    """Place nodes in BFS levels — matches how the lessons teach BFS trees."""
    adj = _adjacency(nodes, edges, directed=False)
    src = root if root in adj else (nodes[0] if nodes else None)
    if src is None:
        return {}
    level = {src: 0}
    queue = [src]
    while queue:
        cur = queue.pop(0)
        for nb in sorted(adj[cur]):
            if nb not in level:
                level[nb] = level[cur] + 1
                queue.append(nb)
    if len(level) < len(nodes):
        orphan = max(level.values()) + 1
        for node in nodes:
            if node not in level:
                level[node] = orphan
    by_level: dict[int, list[str]] = {}
    for node in nodes:
        by_level.setdefault(level[node], []).append(node)
    max_level = max(by_level)
    pos = {}
    for lvl, group in sorted(by_level.items()):
        for i, node in enumerate(group):
            x = (i + 1) / (len(group) + 1)
            y = 0.92 - (lvl / max(1, max_level)) * 0.78
            pos[node] = (x, y)
    return pos


def _layout_spring(nodes: list[str], edges, seed: int = 7) -> dict:
    n = len(nodes)
    if n == 1:
        return {nodes[0]: (0.5, 0.5)}
    rng = random.Random(seed)
    pos = {node: (rng.random(), rng.random()) for node in nodes}
    adj = _adjacency(nodes, edges, directed=False)
    iterations = 240 if n <= 50 else 70
    k = 0.32 / math.sqrt(n)
    temp = 0.12

    for _ in range(iterations):
        disp = {node: [0.0, 0.0] for node in nodes}
        for i in range(n):
            a = nodes[i]
            for j in range(n):
                if i == j:
                    continue
                b = nodes[j]
                dx = pos[a][0] - pos[b][0]
                dy = pos[a][1] - pos[b][1]
                d = math.sqrt(dx * dx + dy * dy + 1e-9)
                f = (k * k) / d
                disp[a][0] += (dx / d) * f
                disp[a][1] += (dy / d) * f
        for a in adj:
            for b in adj[a]:
                if a >= b:
                    continue
                dx = pos[a][0] - pos[b][0]
                dy = pos[a][1] - pos[b][1]
                d = math.sqrt(dx * dx + dy * dy + 1e-9)
                f = (d * d) / k
                disp[a][0] -= (dx / d) * f
                disp[a][1] -= (dy / d) * f
                disp[b][0] += (dx / d) * f
                disp[b][1] += (dy / d) * f
        for node in nodes:
            dx, dy = disp[node]
            d = math.sqrt(dx * dx + dy * dy + 1e-9)
            if d > 0:
                step = min(d, temp)
                pos[node] = (pos[node][0] + (dx / d) * step,
                             pos[node][1] + (dy / d) * step)
        temp *= 0.97

    xs = [p[0] for p in pos.values()]
    ys = [p[1] for p in pos.values()]
    x0, x1 = min(xs), max(xs)
    y0, y1 = min(ys), max(ys)
    return {
        node: (0.07 + (pos[node][0] - x0) / (x1 - x0 + 1e-9) * 0.86,
               0.08 + (pos[node][1] - y0) / (y1 - y0 + 1e-9) * 0.84)
        for node in nodes
    }


# ══════════════════════════════════════════════════════════════════════════════
# matplotlib renderers
# ══════════════════════════════════════════════════════════════════════════════

def _mpl_chart(args: dict, chart_type: str, series, labels, title,
               x_label, y_label, save_path, width, height, dpi) -> Path:
    plt = _matplotlib()
    fig, ax = plt.subplots(figsize=(width, height), dpi=dpi)
    colours = args.get("colors") or _PALETTE

    if chart_type == "pie" or chart_type == "doughnut":
        name, nums = series[0]
        explode = [0.04] * len(nums)
        wedges, _texts, autotexts = ax.pie(
            nums, labels=labels, colors=colours[: len(nums)],
            explode=explode, autopct="%1.1f%%", startangle=90,
            wedgeprops={"linewidth": 1.4, "edgecolor": "white",
                        "width": 0.55 if chart_type == "doughnut" else 1.0},
            textprops={"fontsize": 9},
        )
        for t in autotexts:
            t.set_color("white")
            t.set_fontweight("bold")
        ax.axis("equal")
        if title:
            ax.set_title(title, fontsize=13, pad=14)
        return _finish(fig, ax, save_path, title, x_label, y_label)

    if chart_type == "scatter":
        for i, (name, pts) in enumerate(series):
            xs = [_to_float(p[0], float(j + 1)) if isinstance(p, (list, tuple)) else float(j + 1)
                  for j, p in enumerate(pts)]
            ys = [_to_float(p[1], 0.0) if isinstance(p, (list, tuple)) else _to_float(p, 0.0)
                  for j, p in enumerate(pts)]
            ax.scatter(xs, ys, s=46, color=colours[i % len(colours)], label=name, zorder=3)
    elif chart_type == "histogram":
        nums = series[0][1]
        bins = int(args.get("bins") or 10)
        ax.hist(nums, bins=bins, color=colours[0], edgecolor="white", zorder=3)
    elif chart_type == "area":
        for i, (name, nums) in enumerate(series):
            ax.fill_between(range(len(nums)), nums, alpha=0.35,
                            color=colours[i % len(colours)], label=name)
            ax.plot(range(len(nums)), nums, color=colours[i % len(colours)], lw=2)
    elif chart_type == "horizontal_bar":
        for i, (name, nums) in enumerate(series):
            # matplotlib needs exactly one y per value; a series shorter than the
            # category list raises "shape mismatch" instead of drawing a partial
            # chart, so only as many labels as this series has values are used.
            ax.barh(list(labels[: len(nums)]), nums,
                    color=colours[i % len(colours)], label=name, zorder=3)
    elif chart_type == "line":
        for i, (name, nums) in enumerate(series):
            marker = "o" if len(nums) <= 24 else None
            ax.plot(list(labels[: len(nums)]), nums, marker=marker, lw=2.2,
                    color=colours[i % len(colours)], label=name, zorder=3)
    else:                                                    # bar
        width_bar = 0.7 / max(1, len(series))
        n_cats = max(len(nums) for _n, nums in series) if series else 0
        for i, (name, nums) in enumerate(series):
            xs = [idx + i * width_bar for idx in range(len(nums))]
            ax.bar(xs, nums, width=width_bar * 0.9,
                   color=colours[i % len(colours)], label=name, zorder=3)
        # Centre the group's tick under its bars and label every category, even
        # when the first series is not the longest one.
        ax.set_xticks([idx + width_bar * (len(series) - 1) / 2 for idx in range(n_cats)])
        ax.set_xticklabels(labels[:n_cats], rotation=20 if n_cats > 6 else 0, ha="right")

    ax.grid(axis="y", linestyle="--", alpha=0.45, color=_GRID, zorder=0)
    for spine in ("top", "right"):
        ax.spines[spine].set_visible(False)
    if chart_type not in ("horizontal_bar",):
        ax.tick_params(axis="x", labelsize=8)
    if len(series) > 1:
        ax.legend(fontsize=8, frameon=False)
    return _finish(fig, ax, save_path, title, x_label, y_label)


def _finish(fig, ax, save_path, title, x_label, y_label) -> Path:
    if title and not ax.get_title():
        ax.set_title(title, fontsize=13, pad=10)
    if x_label:
        ax.set_xlabel(x_label, fontsize=10)
    if y_label:
        ax.set_ylabel(y_label, fontsize=10)
    out = Path(save_path) if save_path else _unique_path(title or "chart", "png")
    fig.tight_layout()
    fig.savefig(str(out), facecolor="white")
    plt_close(fig)
    return out


def _mpl_function(expressions: list[tuple[str, str]], x_min, x_max, title,
                  x_label, y_label, save_path, width, height, dpi) -> Path:
    plt = _matplotlib()
    fig, ax = plt.subplots(figsize=(width, height), dpi=dpi)

    n_points = min(400, max(20, int(x_max - x_min) * 4 + 20))
    step = (x_max - x_min) / max(1, n_points - 1)
    xs = [x_min + i * step for i in range(n_points)]

    for i, (label, expr) in enumerate(expressions):
        ys = []
        for x in xs:
            try:
                y = _eval_expr(expr, x)
                if math.isfinite(y):
                    ys.append(y)
                else:
                    ys.append(None)
            except Exception:
                ys.append(None)
        good = [(x, y) for x, y in zip(xs, ys) if y is not None]
        if not good:
            continue
        gx = [p[0] for p in good]
        gy = [p[1] for p in good]
        ax.plot(gx, gy, lw=2.2, color=_PALETTE[i % len(_PALETTE)], label=label)

    ax.grid(linestyle="--", alpha=0.45, color=_GRID, zorder=0)
    for spine in ("top", "right"):
        ax.spines[spine].set_visible(False)
    if expressions:
        ax.legend(fontsize=9, frameon=False)
    return _finish(fig, ax, save_path, title or "Function plot", x_label or "n", y_label)


def _mpl_network(nodes, edges, pos, directed, highlight_nodes, highlight_edges,
                 title, save_path, width, height, dpi) -> Path:
    plt = _matplotlib()
    fig, ax = plt.subplots(figsize=(width, height), dpi=dpi)

    hl_n = {str(x) for x in (highlight_nodes or [])}
    hl_e = {(str(a), str(b)) for a, b in (highlight_edges or [])}

    for a, b, w in edges:
        if a not in pos or b not in pos:
            continue
        x1, y1 = pos[a]
        x2, y2 = pos[b]
        hot = (a, b) in hl_e or (b, a) in hl_e
        colour = _ACCENT if hot else "#9aa4b2"
        lw = 2.8 if hot else 1.6
        if directed:
            ang = math.atan2(y2 - y1, x2 - x1)
            sx, sy = x1 + 0.055 * math.cos(ang), y1 + 0.055 * math.sin(ang)
            tx, ty = x2 - 0.06 * math.cos(ang), y2 - 0.06 * math.sin(ang)
            ax.annotate("", xy=(tx, ty), xytext=(sx, sy),
                        arrowprops=dict(arrowstyle="-|>", color=colour, lw=lw,
                                        mutation_scale=16))
        else:
            ax.plot([x1, x2], [y1, y2], color=colour, lw=lw, zorder=1)
        if w is not None:
            ax.text((x1 + x2) / 2, (y1 + y2) / 2, str(w),
                    fontsize=8, color=_AXIS, ha="center", va="center", zorder=2,
                    bbox=dict(facecolor="white", edgecolor="none", pad=0.8))

    for node in nodes:
        x, y = pos[node]
        hot = node in hl_n
        ax.add_patch(plt.Circle((x, y), 0.05,
                                color=(_ACCENT if hot else _PALETTE[0]),
                                zorder=3))
        ax.text(x, y, node, fontsize=9, ha="center", va="center",
                color="white", fontweight="bold", zorder=4)

    ax.set_xlim(-0.05, 1.05)
    ax.set_ylim(-0.05, 1.05)
    ax.set_aspect("equal")
    ax.axis("off")
    return _finish(fig, ax, save_path, title or "Network", "", "")


def plt_close(fig):
    try:
        import matplotlib.pyplot as plt
        plt.close(fig)
    except Exception:
        pass


# ══════════════════════════════════════════════════════════════════════════════
# HTML/SVG fallback renderers (stdlib only)
# ══════════════════════════════════════════════════════════════════════════════

def _esc(text) -> str:
    return (str(text).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def _svg_wrap(title: str, body: str, width: int, height: int) -> str:
    return (
        f"<!DOCTYPE html>\n<html lang=\"en\"><head><meta charset=\"utf-8\">\n"
        f"<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n"
        f"<title>{_esc(title)}</title>\n"
        f"<style>\n"
        f"  body {{ margin:0; background:#f4f6f8; color:{_TEXT};\n"
        f"         font-family: -apple-system, 'Segoe UI', Roboto, sans-serif; }}\n"
        f"  .wrap {{ max-width:{width + 32}px; margin:20px auto; background:#fff;\n"
        f"           border-radius:14px; box-shadow:0 2px 14px rgba(20,30,50,.10);\n"
        f"           padding:18px 20px 24px; }}\n"
        f"  h1 {{ font-size:18px; margin:2px 0 14px; }}\n"
        f"  svg {{ width:100%; height:auto; display:block; }}\n"
        f"  .note {{ font-size:12px; color:{_AXIS}; margin-top:10px; }}\n"
        f"</style></head><body>\n"
        f"<div class=\"wrap\">\n  <h1>{_esc(title)}</h1>\n"
        f"  <svg viewBox=\"0 0 {width} {height}\" xmlns=\"http://www.w3.org/2000/svg\">\n"
        f"{body}\n  </svg>\n"
        f"  <div class=\"note\">Generated by MARK XL · stdlib SVG fallback</div>\n"
        f"</div>\n</body></html>\n"
    )


def _svg_axes(margin, width, height, x0, x1, y0, y1, x_label, y_label, labels,
              horizontal=False):
    """Emit gridlines + axis lines for a cartesian plot; returns (svg, map_x, map_y).

    ``horizontal`` swaps the axes for horizontal-bar charts: the x axis becomes
    the numeric value axis and the category labels run down the left edge.
    """
    plot_w = width - margin.l - margin.r
    plot_h = height - margin.t - margin.b

    def map_x(v):
        return margin.l + (v - x0) / (x1 - x0 + 1e-12) * plot_w

    def map_y(v):
        return margin.t + (1 - (v - y0) / (y1 - y0 + 1e-12)) * plot_h

    parts = []
    if horizontal:
        # Vertical gridlines with the numeric scale along the bottom, categories
        # down the left edge.
        for i in range(6):
            gx = margin.l + plot_w * i / 5
            val = x0 + (x1 - x0) * i / 5        # ascending left-to-right
            parts.append(f'<line x1="{gx:.1f}" y1="{margin.t:.1f}" x2="{gx:.1f}" '
                         f'y2="{height - margin.b:.1f}" stroke="{_GRID}" stroke-width="1"/>')
            parts.append(f'<text x="{gx:.1f}" y="{height - margin.b + 16:.1f}" '
                         f'font-size="10" fill="{_AXIS}" text-anchor="middle">'
                         f'{_fmt(val)}</text>')
        # Category 0 at the top, matching where the bars are drawn.
        for i, lab in enumerate(labels):
            ty = map_y(y1 - i - 0.5)
            parts.append(f'<text x="{margin.l - 8:.1f}" y="{ty + 3.5:.1f}" '
                         f'font-size="10" fill="{_AXIS}" text-anchor="end">'
                         f'{_esc(lab)[:14]}</text>')
    else:
        # horizontal gridlines (5 steps) with the numeric scale on the left
        for i in range(6):
            gy = margin.t + plot_h * i / 5
            val = y1 - (y1 - y0) * i / 5
            parts.append(f'<line x1="{margin.l}" y1="{gy:.1f}" x2="{width - margin.r:.1f}" '
                         f'y2="{gy:.1f}" stroke="{_GRID}" stroke-width="1"/>')
            parts.append(f'<text x="{margin.l - 8:.1f}" y="{gy + 3.5:.1f}" font-size="10" '
                         f'fill="{_AXIS}" text-anchor="end">{_fmt(val)}</text>')
        # x ticks: category labels or numeric
        if labels:
            for i, lab in enumerate(labels):
                tx = map_x(i + 0.5) if x1 == len(labels) else map_x(i + 1)
                parts.append(f'<text x="{tx:.1f}" y="{height - margin.b + 16:.1f}" '
                             f'font-size="10" fill="{_AXIS}" text-anchor="middle">'
                             f'{_esc(lab)[:14]}</text>')
    parts.append(f'<line x1="{margin.l}" y1="{height - margin.b:.1f}" '
                 f'x2="{width - margin.r:.1f}" y2="{height - margin.b:.1f}" '
                 f'stroke="{_AXIS}" stroke-width="1.4"/>')
    parts.append(f'<line x1="{margin.l}" y1="{margin.t:.1f}" '
                 f'x2="{margin.l:.1f}" y2="{height - margin.b:.1f}" '
                 f'stroke="{_AXIS}" stroke-width="1.4"/>')
    if x_label:
        parts.append(f'<text x="{width / 2:.1f}" y="{height - 10:.1f}" font-size="11" '
                     f'fill="{_TEXT}" text-anchor="middle">{_esc(x_label)}</text>')
    if y_label:
        parts.append(f'<text x="14" y="{height / 2:.1f}" font-size="11" fill="{_TEXT}" '
                     f'text-anchor="middle" transform="rotate(-90 14 {height / 2:.1f})">'
                     f'{_esc(y_label)}</text>')
    return "\n".join(parts), map_x, map_y


def _fmt(v) -> str:
    v = float(v)
    if abs(v) >= 1e6 or (0 < abs(v) < 1e-3):
        return f"{v:.1e}"
    if v == int(v):
        return str(int(v))
    return f"{v:.2f}".rstrip("0").rstrip(".")


def _svg_legend(series, width, margin):
    if len(series) <= 1:
        return ""
    parts, x = [], margin.l
    for i, (name, _) in enumerate(series):
        colour = _PALETTE[i % len(_PALETTE)]
        parts.append(f'<rect x="{x:.1f}" y="6" width="11" height="11" rx="2" fill="{colour}"/>')
        parts.append(f'<text x="{x + 15:.1f}" y="15.5" font-size="11" fill="{_TEXT}">'
                     f'{_esc(name)}</text>')
        x += 21 + 7 * len(str(name))
    return "\n".join(parts)


def _scatter_pairs(series) -> list[tuple[float, float]]:
    """Accept either [[x, y], …] pairs or a plain [v, v, …] list (x = 1, 2, 3…)."""
    out = []
    for _name, pts in series:
        for j, p in enumerate(pts):
            if isinstance(p, (list, tuple)) and len(p) >= 2:
                out.append((_to_float(p[0], 0.0), _to_float(p[1], 0.0)))
            else:
                out.append((float(j + 1), _to_float(p, 0.0)))
    return out


def _svg_cartesian(chart_type, series, labels, title, x_label, y_label,
                   width=900, height=560) -> str:
    margin = _Margins(64, 24, 34, 58)
    plot_n = max(len(s[1]) for s in series) if series else 0
    all_vals = [v[1] if isinstance(v, (list, tuple)) else v
                for _n, nums in series for v in nums]
    if chart_type == "scatter":
        pairs = _scatter_pairs(series)
        xs = [p[0] for p in pairs]
        x0, x1 = (min(xs), max(xs)) if xs else (0, 1)
        lo, hi = (min(all_vals), max(all_vals)) if all_vals else (0, 1)
    elif chart_type == "horizontal_bar":
        # A horizontal bar's *length* encodes the value, so the x axis has to
        # span the data (with 0 kept as the baseline) and the y axis is just the
        # category index.  Sharing the vertical charts' category x axis instead
        # means map_x(v) lands far outside the canvas for almost any value, and
        # every bar renders as a spike off the right edge.
        lo, hi = (min(all_vals), max(all_vals)) if all_vals else (0, 1)
        x0, x1 = min(0, lo), max(0, hi)
        if x1 == x0:
            x1 = x0 + 1
        xp = (x1 - x0) * 0.08
        if hi > 0:
            x1 += xp
        if lo < 0:
            x0 -= xp
        lo, hi = 0, plot_n                       # category rows on the y axis
    elif chart_type == "histogram":
        x0, x1 = 0, plot_n
    else:
        x0, x1 = 0, max(1, plot_n)

    # The y range: histogram bars encode bin COUNTS, whose maximum can far
    # exceed max(values) — values=[1,1,1,1,1] gives counts=[5,0,0,0] against
    # max(values)=1, so the range must come from the counts or the tallest bar
    # is mapped off the canvas.  A horizontal bar chart instead uses the y axis
    # for category rows.  Everything else scales to the data values.
    counts = bins = None
    bin_lo = bin_hi = 0.0
    if chart_type == "histogram":
        nums = series[0][1]
        bins = max(4, min(40, int(len(nums) ** 0.5) or 10))
        bin_lo, bin_hi = min(nums), max(nums)
        if bin_hi == bin_lo:
            bin_hi = bin_lo + 1
        counts = [0] * bins
        for v in nums:
            counts[min(bins - 1, int((v - bin_lo) / (bin_hi - bin_lo) * bins))] += 1
        lo, hi = min(counts), max(counts)
    elif chart_type == "horizontal_bar":
        lo, hi = 0, plot_n                      # category rows, set above
    else:
        lo, hi = (min(all_vals), max(all_vals)) if all_vals else (0, 1)

    # 0 is kept in range as the bar baseline; headroom goes on the side away from
    # it. Multiplying by 1.08 instead would shrink the span when hi < 0 and can
    # push y1 below y0, inverting the axis and drawing zero-height bars.
    y0, y1 = min(0, lo), max(0, hi)
    if y1 == y0:
        y1 = y0 + 1
    if chart_type != "horizontal_bar":
        # A horizontal bar chart's y axis is the exact category grid, so it must
        # not gain headroom — the category labels and the bars both place row i
        # with map_y, and padding would shift one relative to the other.
        pad = (y1 - y0) * 0.08
        if hi > 0:
            y1 += pad
        if lo < 0:
            y0 -= pad

    axes, map_x, map_y = _svg_axes(margin, width, height, x0, x1, y0, y1, x_label, y_label,
                                   None if chart_type in ("scatter", "histogram") else labels,
                                   horizontal=(chart_type == "horizontal_bar"))
    parts = [axes, _svg_legend(series, width, margin)]
    palette = _PALETTE

    if chart_type == "scatter":
        for i, (name, pts) in enumerate(series):
            for j, p in enumerate(pts):
                if isinstance(p, (list, tuple)) and len(p) >= 2:
                    cx, cy = map_x(_to_float(p[0], 0.0)), map_y(_to_float(p[1], 0.0))
                else:
                    cx, cy = map_x(j + 1), map_y(_to_float(p, 0.0))
                parts.append(f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="4.5" '
                             f'fill="{palette[i % len(palette)]}" opacity="0.85"/>')
        return _svg_wrap(title, "\n".join(parts), width, height)

    if chart_type == "histogram":
        bw = (map_x(plot_n) - margin.l) / bins
        for i, c in enumerate(counts):
            bx = margin.l + i * bw
            by = map_y(c)
            parts.append(f'<rect x="{bx + 1:.1f}" y="{by:.1f}" width="{bw - 2:.1f}" '
                         f'height="{height - margin.b - by:.1f}" fill="{palette[0]}" '
                         f'opacity="0.9" rx="2"/>')
            parts.append(f'<text x="{bx + bw / 2:.1f}" y="{height - margin.b + 15:.1f}" '
                         f'font-size="9" fill="{_AXIS}" text-anchor="middle">'
                         f'{_fmt(bin_lo + (i + 0.5) * (bin_hi - bin_lo) / bins)}</text>')
        return _svg_wrap(title, "\n".join(parts), width, height)

    if chart_type == "line":
        for i, (name, nums) in enumerate(series):
            pts = " ".join(f"{map_x(j + 0.5):.1f},{map_y(v):.1f}" for j, v in enumerate(nums))
            parts.append(f'<polyline points="{pts}" fill="none" '
                         f'stroke="{palette[i % len(palette)]}" stroke-width="2.4"/>')
            for j, v in enumerate(nums):
                parts.append(f'<circle cx="{map_x(j + 0.5):.1f}" cy="{map_y(v):.1f}" r="3.4" '
                             f'fill="{palette[i % len(palette)]}"/>')
        return _svg_wrap(title, "\n".join(parts), width, height)

    if chart_type == "area":
        for i, (name, nums) in enumerate(series):
            base = map_y(0)
            pts = " ".join(f"{map_x(j + 0.5):.1f},{map_y(v):.1f}" for j, v in enumerate(nums))
            close = f"{map_x(len(nums) - 0.5):.1f},{base:.1f} {map_x(0.5):.1f},{base:.1f}"
            parts.append(f'<polygon points="{pts} {close}" fill="{palette[i % len(palette)]}" '
                         f'opacity="0.32"/>')
            parts.append(f'<polyline points="{pts}" fill="none" '
                         f'stroke="{palette[i % len(palette)]}" stroke-width="2.2"/>')
        return _svg_wrap(title, "\n".join(parts), width, height)

    # bar / horizontal_bar
    n_series = max(1, len(series))
    bar_w = (map_x(plot_n) - margin.l) / max(1, plot_n)
    group_w = bar_w * 0.62
    slot = group_w / n_series
    bw = slot if n_series == 1 else slot * 0.9       # a hair of air between series
    zero_x = map_x(0)
    for i, (name, nums) in enumerate(series):
        colour = palette[i % len(palette)]
        for j, v in enumerate(nums):
            if chart_type == "horizontal_bar":
                row_h = (height - margin.t - margin.b) / max(1, plot_n)
                group_h = row_h * 0.62
                bh = min(20, group_h / n_series)
                by = margin.t + (j + 0.5) * row_h + (i - (n_series - 1) / 2) * (group_h / n_series)
                # A negative value has no width to the right of the axis; draw it
                # left of the origin instead of emitting a negative-width rect.
                left, right = (zero_x, map_x(v)) if v >= 0 else (map_x(v), zero_x)
                parts.append(f'<rect x="{min(left, right):.1f}" y="{by - bh / 2:.1f}" '
                             f'width="{max(1.0, abs(right - left)):.1f}" '
                             f'height="{bh:.1f}" '
                             f'fill="{colour}" rx="3"/>')
            else:
                # Centre the series' bar on its own slice of the category group;
                # without the per-series offset every series is drawn at the same
                # x and later ones simply cover the earlier ones.
                bx = map_x(j + 0.5) - group_w / 2 + i * slot
                # Bars hang from the zero line, not from the plot floor — with
                # negative values the floor is the minimum, which would draw them
                # standing *up* from below the axis.
                top = map_y(max(v, 0.0))
                h = map_y(min(v, 0.0)) - top
                parts.append(f'<rect x="{bx:.1f}" y="{top:.1f}" '
                             f'width="{bw:.1f}" '
                             f'height="{abs(h):.1f}" '
                             f'fill="{colour}" rx="3"/>')
    return _svg_wrap(title, "\n".join(parts), width, height)


def _svg_pie(series, labels, title, width=760, height=560, doughnut=False) -> str:
    name, nums = series[0]
    total = sum(nums)
    cx, cy, r = width / 2, height / 2 + 6, min(width, height) / 2 - 70
    inner = r * 0.45 if doughnut else 0.0
    parts = []
    angle = -math.pi / 2
    for i, v in enumerate(nums):
        frac = v / total if total else 0
        sweep = frac * 2 * math.pi
        x1 = cx + r * math.cos(angle)
        y1 = cy + r * math.sin(angle)
        x2 = cx + r * math.cos(angle + sweep)
        y2 = cy + r * math.sin(angle + sweep)
        large = 1 if sweep > math.pi else 0
        colour = _PALETTE[i % len(_PALETTE)]
        if frac >= 1:
            # A 2*pi sweep ends exactly where it began, and SVG omits an arc whose
            # endpoints coincide — the wedge would collapse to a zero-width line
            # and the whole pie would render blank. Trace the full circle as two
            # half arcs instead.
            x3, y3 = 2 * cx - x1, 2 * cy - y1
            path = (f"M{x1:.1f},{y1:.1f} A{r:.1f},{r:.1f} 0 0 1 {x3:.1f},{y3:.1f} "
                    f"A{r:.1f},{r:.1f} 0 0 1 {x1:.1f},{y1:.1f} Z")
        else:
            path = (f"M{cx:.1f},{cy:.1f} L{x1:.1f},{y1:.1f} "
                    f"A{r:.1f},{r:.1f} 0 {large} 1 {x2:.1f},{y2:.1f} Z")
        if doughnut:
            # Same wedge, minus the centre disc (evenodd punches the hole per wedge).
            path += (f" M{cx + inner:.1f},{cy:.1f} "
                     f"A{inner:.1f},{inner:.1f} 0 1 1 {cx - inner:.1f},{cy:.1f} "
                     f"A{inner:.1f},{inner:.1f} 0 1 1 {cx + inner:.1f},{cy:.1f} Z")
            parts.append(f'<path d="{path}" fill="{colour}" stroke="white" '
                         f'stroke-width="2" fill-rule="evenodd"/>')
        else:
            parts.append(f'<path d="{path}" fill="{colour}" stroke="white" stroke-width="2"/>')
        mid = angle + sweep / 2
        lx = cx + (r * 0.62) * math.cos(mid)
        ly = cy + (r * 0.62) * math.sin(mid)
        pct = f"{frac * 100:.1f}%"
        parts.append(f'<text x="{lx:.1f}" y="{ly:.1f}" font-size="11" font-weight="bold" '
                     f'fill="white" text-anchor="middle">{pct}</text>')
        lx2 = cx + (r + 22) * math.cos(mid)
        ly2 = cy + (r + 22) * math.sin(mid)
        lab = labels[i] if i < len(labels) else str(i + 1)
        parts.append(f'<text x="{lx2:.1f}" y="{ly2:.1f}" font-size="11" fill="{_TEXT}" '
                     f'text-anchor="middle">{_esc(lab)[:16]}</text>')
        angle += sweep
    return _svg_wrap(title, "\n".join(parts), width, height)


def _svg_function(expressions, x_min, x_max, title, x_label, y_label,
                  width=900, height=560) -> str:
    margin = _Margins(64, 24, 34, 58)
    n_points = min(400, max(20, int(x_max - x_min) * 4 + 20))
    step = (x_max - x_min) / max(1, n_points - 1)
    xs = [x_min + i * step for i in range(n_points)]
    curves = []
    ymin = ymax = None
    for label, expr in expressions:
        pts = []
        for x in xs:
            try:
                y = _eval_expr(expr, x)
                if math.isfinite(y):
                    pts.append((x, y))
                    ymin = y if ymin is None else min(ymin, y)
                    ymax = y if ymax is None else max(ymax, y)
            except Exception:
                pass
        curves.append((label, pts))
    # Same span logic as _svg_cartesian: keep zero visible as the baseline and
    # put the headroom on the side away from it. Multiplying a negative maximum
    # by 1.08 would push y1 below y0 and invert the axis, sending the curve
    # far off the canvas.
    y0 = 0.0 if ymin is None or ymin > 0 else ymin
    y1 = 0.0 if ymax is None or ymax < 0 else ymax
    if y1 == y0:
        y1 = y0 + 1
    pad = (y1 - y0) * 0.08
    if ymax is not None and ymax > 0:
        y1 += pad
    if ymin is not None and ymin < 0:
        y0 -= pad
    axes, map_x, map_y = _svg_axes(margin, width, height, x_min, x_max, y0, y1,
                                   x_label or "n", y_label, None)
    parts = [axes]
    for i, (label, pts) in enumerate(curves):
        colour = _PALETTE[i % len(_PALETTE)]
        svg_pts = " ".join(f"{map_x(x):.1f},{map_y(y):.1f}" for x, y in pts)
        parts.append(f'<polyline points="{svg_pts}" fill="none" stroke="{colour}" '
                     f'stroke-width="2.4"/>')
        parts.append(f'<rect x="{margin.l + 10 + i * 150:.1f}" y="8" width="11" '
                     f'height="11" rx="2" fill="{colour}"/>')
        parts.append(f'<text x="{margin.l + 25 + i * 150:.1f}" y="17.5" font-size="11" '
                     f'fill="{_TEXT}">{_esc(label)}</text>')
    return _svg_wrap(title, "\n".join(parts), width, height)


def _svg_network(nodes, edges, pos, directed, highlight_nodes, highlight_edges,
                 title, width=820, height=580) -> str:
    sx = lambda x: 40 + x * (width - 80)
    sy = lambda y: 40 + (1 - y) * (height - 80)
    hl_n = {str(x) for x in (highlight_nodes or [])}
    hl_e = {(str(a), str(b)) for a, b in (highlight_edges or [])}
    parts = []

    for a, b, w in edges:
        if a not in pos or b not in pos:
            continue
        x1, y1 = sx(pos[a][0]), sy(pos[a][1])
        x2, y2 = sx(pos[b][0]), sy(pos[b][1])
        hot = (a, b) in hl_e or (b, a) in hl_e
        colour = _ACCENT if hot else "#9aa4b2"
        lw = 3 if hot else 1.8
        parts.append(f'<line x1="{x1:.1f}" y1="{y1:.1f}" x2="{x2:.1f}" y2="{y2:.1f}" '
                     f'stroke="{colour}" stroke-width="{lw}"/>')
        if directed:
            ang = math.atan2(y2 - y1, x2 - x1)
            ax_, ay_ = x2 - 20 * math.cos(ang), y2 - 20 * math.sin(ang)
            t1 = (ax_ + 8 * math.cos(ang + 2.6), ay_ + 8 * math.sin(ang + 2.6))
            t2 = (ax_ + 8 * math.cos(ang - 2.6), ay_ + 8 * math.sin(ang - 2.6))
            parts.append(f'<polygon points="{ax_:.1f},{ay_:.1f} {t1[0]:.1f},{t1[1]:.1f} '
                         f'{t2[0]:.1f},{t2[1]:.1f}" fill="{colour}"/>')
        if w is not None:
            parts.append(f'<text x="{(x1 + x2) / 2:.1f}" y="{(y1 + y2) / 2 - 4:.1f}" '
                         f'font-size="10" fill="{_AXIS}" text-anchor="middle" '
                         f'font-weight="bold">{_esc(w)}</text>')

    for node in nodes:
        x, y = sx(pos[node][0]), sy(pos[node][1])
        hot = node in hl_n
        colour = _ACCENT if hot else _PALETTE[0]
        parts.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="19" fill="{colour}" '
                     f'stroke="white" stroke-width="2.5"/>')
        parts.append(f'<text x="{x:.1f}" y="{y + 4:.1f}" font-size="11" font-weight="bold" '
                     f'fill="white" text-anchor="middle">{_esc(node)}</text>')
    return _svg_wrap(title, "\n".join(parts), width, height)


# ══════════════════════════════════════════════════════════════════════════════
# action entry points
# ══════════════════════════════════════════════════════════════════════════════

_CHART_TYPES = ("bar", "line", "area", "pie", "scatter", "histogram", "horizontal_bar", "doughnut")
_LAYOUTS = ("spring", "circular", "layers")


def _help_text() -> str:
    return (
        "I can build these data charts: " + ", ".join(_CHART_TYPES) +
        ". I can also plot functions — for example 'compare n squared against n log n' "
        "or 'show Big-O growth rates' — and I can draw graph-theory networks from a node "
        "and edge list. Tell me the chart type, the labels and the values."
    )


def _do_chart(args: dict, player) -> GraphResult:
    chart_type = (args.get("chart_type") or args.get("type") or "bar").strip().lower()
    if chart_type == "column":
        chart_type = "bar"
    if chart_type not in _CHART_TYPES:
        chart_type = "bar"

    title   = (args.get("title") or "").strip()
    x_label = (args.get("x_label") or args.get("xlabel") or "").strip()
    y_label = (args.get("y_label") or args.get("ylabel") or "").strip()
    save_path = args.get("save_path")
    show    = args.get("show")
    if show is None:
        show = True
    show = bool(show)
    width  = _to_float(args.get("width"), 9.0)
    height = _to_float(args.get("height"), 5.5)
    dpi    = int(_to_float(args.get("dpi"), 130) or 130)

    series, dropped = _collect_series(args, chart_type)
    if chart_type in ("pie", "doughnut", "histogram"):
        series = series[:1]

    if not series:
        if dropped:
            return GraphResult(
                "None of the values you gave me were numbers, sir, so there is nothing to "
                "plot. Repeat the values and I'll draw the chart."
            )
        return GraphResult(
            "I need some numbers before I can draw that chart — pass the values "
            "(and labels for the categories), sir."
        )

    labels = _labels_for(args, max(len(s[1]) for s in series))

    if player:
        player.write_log(f"[graph_maker] {chart_type}: {title or 'untitled'}")

    if _has_matplotlib():
        # matplotlib handles every chart type, doughnut included (wedgeprops width).
        out = _mpl_chart(args, chart_type, series, labels, title, x_label, y_label,
                         save_path, width, height, dpi)
    else:
        if chart_type in ("pie", "doughnut"):
            default_title = "Doughnut chart" if chart_type == "doughnut" else "Pie chart"
            html = _svg_pie(series, labels, title or default_title,
                            doughnut=chart_type == "doughnut")
        else:
            html = _svg_cartesian(chart_type, series, labels, title or f"{chart_type} chart",
                                  x_label, y_label)
        out = _unique_path(title or "chart", "html")
        out.write_text(html, encoding="utf-8")

    if show:
        _open_file(out)
    label = f" '{title}'" if title else ""
    warning = ""
    if dropped:
        warning = (
            f" One caution, sir: {dropped} of the values you gave me was not a number, "
            "so that category is missing from the chart. Give me the value again and "
            "I'll redraw it."
        )
    return GraphResult(
        f"Done, sir — I've built a {chart_type} chart{label} with "
        f"{len(series[0][1])} value(s). It's on your screen now, saved to {out.name}."
        f"{warning}",
        path=str(out), fmt=out.suffix.lstrip("."),
    )


def _do_plot_function(args: dict, player) -> GraphResult:
    title   = (args.get("title") or "").strip()
    x_label = (args.get("x_label") or args.get("xlabel") or "n").strip()
    y_label = (args.get("y_label") or args.get("ylabel") or "").strip()
    save_path = args.get("save_path")
    show = args.get("show")
    show = True if show is None else bool(show)
    width  = _to_float(args.get("width"), 9.0)
    height = _to_float(args.get("height"), 5.5)
    dpi    = int(_to_float(args.get("dpi"), 130) or 130)

    expressions: list[tuple[str, str]] = []
    if str(args.get("preset") or "").strip().lower() in ("big_o", "bigo", "complexity", "growth"):
        x_min = _to_float(args.get("x_min"), 1.0)
        x_max = _to_float(args.get("x_max"), 20.0)
        expressions = list(_BIG_O.items())
        title = title or "Big-O Growth Rates Compared"
        y_label = y_label or "operations"
    else:
        raw = args.get("expressions")
        if not isinstance(raw, list):
            raw = [raw] if raw else []
        raw = [e for e in raw if e]
        if len(raw) == 1 and isinstance(raw[0], str) and "," in raw[0]:
            raw = [e.strip() for e in raw[0].split(",")]
        probe_lo = max(0.0, _to_float(args.get("x_min"), 1.0))
        probe_hi = max(probe_lo + 1, min(_to_float(args.get("x_max"), 20.0), 1000.0))
        for e in raw:
            if len(str(e)) > 80:
                continue
            expr = _normalise_expression(e)
            if not _probe_expr(expr, probe_lo, probe_hi):
                continue
            expressions.append((expr, expr))
        x_min = _to_float(args.get("x_min"), 1.0)
        x_max = _to_float(args.get("x_max"), 20.0)
        if not expressions:
            return GraphResult(
                "I couldn't parse those expressions, sir. Write them as functions of n, "
                "for example 'n**2' or 'n*log2(n)' — or ask me to 'plot Big-O growth rates'."
            )
        title = title or "f(n) = " + " vs ".join(name for name, _ in expressions)

    x_min = max(0.0, x_min)
    x_max = max(x_min + 1, min(x_max, 1000.0))

    if player:
        player.write_log(f"[graph_maker] plot_function: {len(expressions)} curve(s)")

    if _has_matplotlib():
        out = _mpl_function(expressions, x_min, x_max, title, x_label, y_label,
                            save_path, width, height, dpi)
    else:
        html = _svg_function(expressions, x_min, x_max, title, x_label, y_label)
        out = _unique_path(title or "function", "html")
        out.write_text(html, encoding="utf-8")

    if show:
        _open_file(out)
    return GraphResult(
        f"Plotted {len(expressions)} curve(s) from n={x_min:g} to n={x_max:g}, sir — "
        f"showing it now. Saved to {out.name}.",
        path=str(out), fmt=out.suffix.lstrip("."),
    )


def _do_network(args: dict, player) -> GraphResult:
    nodes, edges = _collect_graph(args)
    if not nodes:
        return GraphResult(
            "I need at least a few nodes to draw a network, sir — pass a node list "
            "and an edge list like A-B, B-C."
        )
    directed     = bool(args.get("directed"))
    layout       = (args.get("layout") or "spring").strip().lower()
    if layout not in _LAYOUTS:
        layout = "spring"
    title        = (args.get("title") or "").strip()
    save_path     = args.get("save_path")
    show = args.get("show")
    show = True if show is None else bool(show)
    width  = _to_float(args.get("width"), 8.0)
    height = _to_float(args.get("height"), 6.0)
    dpi    = int(_to_float(args.get("dpi"), 130) or 130)

    highlight_nodes = [str(x) for x in (args.get("highlight_nodes") or [])]
    # An entry here may be a bare [a, b] pair or a weighted [a, b, w] triple, and
    # either may arrive with fewer than two elements; unpacking it as `a, b`
    # would raise and kill the whole action.
    highlight_edges = []
    for e in (args.get("highlight_edges") or []):
        if isinstance(e, (list, tuple)) and len(e) >= 2:
            highlight_edges.append((str(e[0]), str(e[1])))

    if layout == "circular":
        pos = _layout_circular(nodes)
    elif layout in ("layers", "hierarchical", "bfs"):
        pos = _layout_layers(nodes, edges, args.get("root"))
    else:
        pos = _layout_spring(nodes, edges)

    if player:
        player.write_log(f"[graph_maker] network: {len(nodes)} nodes, {len(edges)} edges")

    if _has_matplotlib():
        out = _mpl_network(nodes, edges, pos, directed, highlight_nodes, highlight_edges,
                           title or f"Network · {len(nodes)} nodes", save_path,
                           width, height, dpi)
    else:
        html = _svg_network(nodes, edges, pos, directed, highlight_nodes, highlight_edges,
                            title or f"Network · {len(nodes)} nodes")
        out = _unique_path(title or "network", "html")
        out.write_text(html, encoding="utf-8")

    if show:
        _open_file(out)
    kind = "directed" if directed else "undirected"
    return GraphResult(
        f"Drew a {kind} network with {len(nodes)} nodes and {len(edges)} edges, sir — "
        f"it's on your screen. Saved to {out.name}.",
        path=str(out), fmt=out.suffix.lstrip("."),
    )


def graph_maker(
    parameters=None,
    response=None,
    player=None,
    session_memory=None,
    speak=None,
) -> str:
    """Build, render and open a graph. Returns a GraphResult (a str subclass)."""
    args = parameters or {}
    action = (args.get("action") or "create").strip().lower()

    try:
        if action in ("types", "help", "list"):
            return _help_text()
        if action == "plot_function":
            return _do_plot_function(args, player)
        if action == "network":
            return _do_network(args, player)
        return _do_chart(args, player)
    except Exception as e:
        import traceback
        traceback.print_exc()
        if player:
            player.write_log(f"ERR graph_maker: {e}")
        return f"I couldn't build that graph, sir — {e}."
