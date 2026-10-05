#!/usr/bin/env python3
"""Run the HerbAudit CLI for several models, then plot accuracy vs. cost and
accuracy vs. time.

Nothing is scored or priced here. For every model this script:
  1. runs `herbaudit --input ... --model ...` (accuracy is HerbAudit's own scoring
     against GBIF / the manual annotations, cost is HerbAudit's own cost_usd),
  2. times the run (per-image extraction time as printed by HerbAudit, plus wall clock),
  3. reads ./herbaudit_output_<model>/*_audit.json (herbaudit_accuracy, cost_usd,
     tokens) and only averages those numbers for the plot.

Already-processed images are re-used from HerbAudit's per-model output folder;
add --force to re-run everything (needed for fresh timings).

Example:
  python run_multi_model.py --input IMAGES/ --ollama-model gemma4:26b-a4b \
    --ollama-host http://host:5007 \
    --models ollama gemini-3.5-flash gemini-3.5-flash-lite gpt-4o
"""
import argparse, json, os, re, subprocess, shlex, sys, time
from pathlib import Path

import pandas as pd

IMAGE_EXT = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".webp"}
TIME_RE = re.compile(r"([\w.\-]+\.\w+) done (\d+(?:\.\d+)?)s")  # tqdm postfix / plain "  12.3s"


def fix_path(raw):
    """Accept Windows-style paths when running under WSL (C:\\Users\\x -> /mnt/c/Users/x)."""
    s = raw.strip().strip('"').strip("'")
    m = re.match(r"^([A-Za-z]):[\\/](.*)$", s)
    if m and os.name != "nt":
        s = f"/mnt/{m.group(1).lower()}/" + m.group(2).replace("\\", "/")
    elif os.name != "nt" and "\\" in s:
        s = s.replace("\\", "/")
    return s


def provider_of(model):
    m = model.lower()
    if m.startswith("gemini"):
        return "gemini"
    if m.startswith(("gpt", "o1", "o3", "o4", "chatgpt", "text-")):
        return "openai"
    return "ollama"  # same rule as herbaudit.cli._infer_provider


def output_dir_for(workdir, model):
    # same naming as herbaudit.audit.run.run_audit
    return workdir / f"herbaudit_output_{re.sub(r'[^A-Za-z0-9_.-]', '_', model)}"


# ------------------------------------------------------------ run herbaudit ---
def run_herbaudit(model, prov, a, workdir):
    cmd = [sys.executable, "-m", "herbaudit.cli", "--input", a.input, "--model", model]
    if prov == "gemini" and a.gemini_key:
        cmd += ["--gemini-key", a.gemini_key]
    if prov == "openai" and a.openai_key:
        cmd += ["--openai-key", a.openai_key]
    if prov == "ollama" and a.ollama_host:
        cmd += ["--ollama-host", a.ollama_host]
    if a.force:
        cmd.append("--force")
    cmd += shlex.split(a.herbaudit_args or "")
    print(f"\n$ {' '.join(c if c != a.gemini_key and c != a.openai_key else '***' for c in cmd)}")

    per_image, t0 = {}, time.perf_counter()
    env = dict(os.environ, PYTHONUNBUFFERED="1")
    # herbaudit writes ./herbaudit_output_<model> relative to cwd -> run it in workdir
    proc = subprocess.Popen(cmd, cwd=workdir, env=env, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, errors="replace")
    for line in proc.stdout:  # \r from tqdm is treated as a line break
        sys.stdout.write(line.rstrip("\n") + "\n") if line.strip() else None
        for name, sec in TIME_RE.findall(line):
            per_image[name] = float(sec)  # dict -> duplicate tqdm refreshes collapse
    proc.wait()
    wall = time.perf_counter() - t0
    if proc.returncode != 0:
        print(f"  !! herbaudit exited with code {proc.returncode} for {model}")
    return wall, per_image


# ------------------------------------------------------------------ collect ---
def collect(model, out_dir, n_images):
    """Read HerbAudit's own numbers from the per-image audit JSONs."""
    acc, cost, tin, tout, tthink = [], [], [], [], []
    n_ok = 0
    for f in out_dir.glob("*_audit.json"):
        rec = json.loads(f.read_text(encoding="utf-8"))
        meta = rec.get("herbaudit_meta", rec)
        n_ok += 1
        if meta.get("herbaudit_accuracy") is not None:
            acc.append(meta["herbaudit_accuracy"])
        cost.append(meta.get("cost_usd") or 0.0)
        tin.append(meta.get("tokens_in") or 0)
        tout.append(meta.get("tokens_out") or 0)
        if meta.get("tokens_thinking") is not None:  # absent in results cached before this field existed
            tthink.append(meta["tokens_thinking"])
    if not n_ok:
        return None
    mean = lambda xs: sum(xs) / len(xs) if xs else float("nan")
    return dict(n_extracted=n_ok, n_failed=max(0, n_images - n_ok), n_scored=len(acc),
                accuracy=mean(acc) * 100, cost_per_1000=mean(cost) * 1000,
                tokens_in_avg=mean(tin), tokens_out_avg=mean(tout),
                tokens_thinking_avg=mean(tthink))


# ------------------------------------------------------------------- plots ---
def pareto(points):
    best, out = -1, []
    for c, acc in sorted(points):
        if acc > best:
            out.append((c, acc)); best = acc
    return out


def _monotone_curve(xs, ys, n=300):
    """Smooth monotone-cubic (PCHIP-style) curve through increasing points."""
    import numpy as np
    x, y = np.asarray(xs, float), np.asarray(ys, float)
    if len(x) < 3:
        t = np.linspace(x[0], x[-1], n)
        return t, np.interp(t, x, y)
    h, d = np.diff(x), np.diff(y) / np.diff(x)
    m = np.zeros_like(y)
    for k in range(1, len(x) - 1):
        if d[k - 1] * d[k] > 0:
            w1, w2 = 2 * h[k] + h[k - 1], h[k] + 2 * h[k - 1]
            m[k] = (w1 + w2) / (w1 / d[k - 1] + w2 / d[k])

    def end(h0, h1, d0, d1):
        s = ((2 * h0 + h1) * d0 - h0 * d1) / (h0 + h1)
        if s * d0 <= 0:
            return 0.0
        if d0 * d1 <= 0 and abs(s) > 3 * abs(d0):
            return 3 * d0
        return s
    m[0], m[-1] = end(h[0], h[1], d[0], d[1]), end(h[-1], h[-2], d[-1], d[-2])
    t = np.linspace(x[0], x[-1], n)
    i = np.clip(np.searchsorted(x, t, side="right") - 1, 0, len(x) - 2)
    s = (t - x[i]) / h[i]
    h00, h10 = 2 * s**3 - 3 * s**2 + 1, s**3 - 2 * s**2 + s
    h01, h11 = -2 * s**3 + 3 * s**2, s**3 - s**2
    return t, h00 * y[i] + h10 * h[i] * m[i] + h01 * y[i + 1] + h11 * h[i] * m[i + 1]


def plot(summary, out_dir, font="UGent Panno Text"):
    import numpy as np
    import matplotlib.pyplot as plt
    from matplotlib import font_manager as fm
    import logging
    logging.getLogger("matplotlib.font_manager").setLevel(logging.ERROR)
    from matplotlib.transforms import Bbox
    for ttf in (Path.home() / ".fonts").glob("UGentPannoText*.ttf"):
        fm.fontManager.addfont(str(ttf))
    installed = {f.name for f in fm.fontManager.ttflist}
    use = [f for f in [font, "UGent Panno Text", "Panno Text", "Roboto Condensed", "Ubuntu Condensed",
                       "Arial Narrow", "DejaVu Sans"] if f in installed]
    if font not in installed:
        print(f"NOTE: font '{font}' not installed, using '{use[0]}'. Install it or pass --font.")
    plt.rcParams.update({"font.family": use, "font.size": 14, "text.color": "#0f2f3f",
                         "axes.labelcolor": "#0f2f3f", "xtick.color": "#555", "ytick.color": "#555",
                         "axes.edgecolor": "#bbb"})
    # (marker, marker colour, size, label colour)
    style = {"gemini": ("*", "#2b78d6", 260, "#123a6b"),
             "openai": ("o", "#e8749f", 160, "#7a1b45"),
             "ollama": ("D", "#1fb27a", 170, "#0f6b4a")}
    names = {"gemini": "Gemini", "openai": "GPT", "ollama": "Ollama (local)"}

    def draw(xcol, xlabel, title, fname, dollar):
        d_all = summary.dropna(subset=[xcol, "accuracy"])
        if d_all.empty:
            print(f"Skipping {fname}: no data for '{xcol}' (run herbaudit with --force to get timings)")
            return
        fig, ax = plt.subplots(figsize=(11, 6))
        front = pareto(list(zip(d_all[xcol], d_all["accuracy"])))
        xmax = d_all[xcol].max() * 1.18 or 1
        ymin, ymax = max(0, d_all["accuracy"].min() - 2.2), 100
        ax.set_xlim(left=-xmax * .01, right=xmax)
        ax.set_ylim(ymin, ymax)

        # Efficiency frontier: vertical from the axis up to the cheapest frontier point,
        # a smooth monotone curve through the frontier, then flat to the right edge.
        cx, cy = _monotone_curve([p[0] for p in front], [p[1] for p in front])
        cx, cy = np.r_[cx, xmax], np.r_[cy, front[-1][1]]
        ax.fill_between(cx, cy, ymax, color="#eef1f6", alpha=1, zorder=0)
        line, = ax.plot(np.r_[front[0][0], cx], np.r_[ymin, cy], ls="--", color="#5a6b85", lw=1.8,
                        label="Efficiency frontier", zorder=1)

        handles = []
        for prov, (mk, col, sz, _) in style.items():
            d = d_all[d_all["provider"] == prov]
            if len(d):
                handles.append(ax.scatter(d[xcol], d["accuracy"], marker=mk, c=col, s=sz,
                                          edgecolor="white", label=names[prov], zorder=3))
        handles.append(line)

        ax.set_xlabel(xlabel, fontsize=17, weight="semibold")
        ax.set_ylabel("HerbAudit Accuracy (%)", fontsize=17, weight="semibold")
        ax.tick_params(labelsize=14)
        if dollar:
            ax.xaxis.set_major_formatter(lambda v, _: f"${v:g}")
        ax.grid(alpha=.25)
        for s in ("top", "right"):
            ax.spines[s].set_visible(False)
        ax.set_title(title, loc="left", color="#888", fontsize=16, y=1.17, pad=0)
        ax.legend(handles=handles, loc="center", bbox_to_anchor=(0.5, 1.045), ncol=len(handles),
                  frameon=True, fancybox=True, framealpha=1, edgecolor="#d9dee6", fontsize=16,
                  borderpad=.7, columnspacing=2.2, handletextpad=.6)
        fig.tight_layout()

        # Model labels: try several spots around each point and keep the one that
        # overlaps the fewest other labels, markers or the plot edge.
        fig.canvas.draw()
        r = fig.canvas.get_renderer()
        px = ax.transData.transform(np.c_[d_all[xcol], d_all["accuracy"]])
        mk_boxes = [Bbox.from_extents(x - 16, y - 16, x + 16, y + 16) for x, y in px]
        axbox, placed = ax.get_window_extent(r), []
        spots = [(10, 8, "left", "bottom"), (10, -8, "left", "top"),
                 (-10, 8, "right", "bottom"), (-10, -8, "right", "top"),
                 (12, 0, "left", "center"), (-12, 0, "right", "center"),
                 (0, 16, "center", "bottom"), (0, -16, "center", "top"),
                 (14, 26, "left", "bottom"), (14, -26, "left", "top"),
                 (-14, 26, "right", "bottom"), (-14, -26, "right", "top")]

        def _area(a_, b_):
            w = min(a_.x1, b_.x1) - max(a_.x0, b_.x0)
            h = min(a_.y1, b_.y1) - max(a_.y0, b_.y0)
            return w * h if w > 0 and h > 0 else 0.0

        for _, row in d_all.sort_values("accuracy", ascending=False).iterrows():
            i = list(d_all.index).index(row.name)
            best_cost, best_spot = None, None
            for spot in spots:
                dx, dy, ha, va = spot
                t = ax.annotate(row["model"], (row[xcol], row["accuracy"]), xytext=(dx, dy),
                                textcoords="offset points", ha=ha, va=va, fontsize=14,
                                color=style[row["provider"]][3], weight="semibold", zorder=4)
                bb = t.get_window_extent(r).expanded(1.04, 1.12)
                t.remove()
                cost = sum(_area(bb, p_) for p_ in placed)
                cost += sum(_area(bb, m_) for j, m_ in enumerate(mk_boxes) if j != i)
                if not (axbox.x0 <= bb.x0 and bb.x1 <= axbox.x1 and axbox.y0 <= bb.y0 and bb.y1 <= axbox.y1):
                    cost += 1e6
                if best_cost is None or cost < best_cost:
                    best_cost, best_spot, best_bb = cost, spot, bb
                if cost == 0:
                    break
            dx, dy, ha, va = best_spot
            far = max(abs(dx), abs(dy)) > 16   # label pushed away from its point: add a leader line
            ax.annotate(row["model"], (row[xcol], row["accuracy"]), xytext=(dx, dy),
                        textcoords="offset points", ha=ha, va=va, fontsize=14,
                        color=style[row["provider"]][3], weight="semibold", zorder=4,
                        arrowprops=dict(arrowstyle="-", color="#9aa5b5", lw=.9, shrinkA=2, shrinkB=7) if far else None)
            placed.append(best_bb)
        fig.savefig(out_dir / fname, dpi=200)
        plt.close(fig)

    n = len(summary)
    draw("cost_per_1000", "Cost per 1000 specimens ($)",
         f"Mean accuracy against mean cost per 1000 specimens · {n} models", "accuracy_vs_cost.png", True)
    draw("sec_per_specimen", "Time per specimen (s)",
         f"Mean accuracy against mean time per specimen · {n} models", "accuracy_vs_time.png", False)


# -------------------------------------------------------------------- main ---
def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--input", required=True, help="Folder of specimen images (passed to herbaudit)")
    p.add_argument("--models", nargs="+", required=True,
                   help="Model names as for `herbaudit --model`; 'ollama' = the model from --ollama-model")
    p.add_argument("--ollama-host", default=None)
    p.add_argument("--ollama-model", default=None, help="Real Ollama tag used for the 'ollama' entry")
    p.add_argument("--ollama-label", default="gemma-4-26B-A4B", help="Name shown in the plot for it")
    p.add_argument("--gemini-key", default=os.getenv("GEMINI_API_KEY"))
    p.add_argument("--openai-key", default=os.getenv("OPENAI_API_KEY"))
    p.add_argument("--herbaudit-args", help='Extra flags passed to herbaudit, e.g. "--detector opencv --no-collage"')
    p.add_argument("--workdir", default=".", help="Where herbaudit_output_<model> folders live (herbaudit's cwd)")
    p.add_argument("--out", default="results", help="Where summary.csv and plots go")
    p.add_argument("--force", action="store_true", help="Re-run every image (fresh timings)")
    p.add_argument("--skip-run", action="store_true", help="Don't call herbaudit; only re-collect and re-plot")
    p.add_argument("--font", default="UGent Panno Text", help="Plot font (UGent house font; must be installed)")
    a = p.parse_args()

    a.input = fix_path(a.input)
    if not Path(a.input).is_dir():
        sys.exit(f"Input folder not found: {a.input}\n"
                 "Under WSL use /mnt/c/Users/<you>/... (quote it because of spaces), e.g.\n"
                 '  --input "/mnt/c/Users/daacik/OneDrive - UGent/Bureaublad/ground_truth"')
    workdir, out = Path(a.workdir).resolve(), Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    n_images = sum(1 for f in Path(a.input).iterdir() if f.suffix.lower() in IMAGE_EXT)
    timings_path = out / "timings.json"
    timings = json.loads(timings_path.read_text()) if timings_path.exists() else {}

    rows = []
    for m in a.models:
        label, model = m, m
        if m == "ollama":
            if not a.ollama_model:
                sys.exit("--ollama-model is required when 'ollama' is in --models")
            label, model = a.ollama_label, a.ollama_model
        prov = provider_of(model)

        if not a.skip_run:
            wall, per_image = run_herbaudit(model, prov, a, workdir)
            t = timings.setdefault(label, {"per_image": {}, "wall_s": 0.0})
            if a.force:
                t["per_image"] = {}
            t["per_image"].update(per_image)   # cached images keep their earlier timing
            t["wall_s"] = wall
            timings_path.write_text(json.dumps(timings, indent=1))

        res = collect(model, output_dir_for(workdir, model), n_images)
        if res is None:
            print(f"[{label}] no results found, skipping"); continue
        t = timings.get(label, {})
        pi = list(t.get("per_image", {}).values())
        if pi:
            sec, src = sum(pi) / len(pi), "per-image"
        elif t.get("wall_s"):
            sec, src = t["wall_s"] / res["n_extracted"], "wall/n (incl. scoring)"
        else:
            sec, src = float("nan"), "none"
        rows.append(dict(model=label, provider=prov, **res, sec_per_specimen=sec,
                         n_timed=len(pi), time_source=src, wall_s=t.get("wall_s")))

    if not rows:
        sys.exit("Nothing to plot.")
    summary = pd.DataFrame(rows).sort_values("accuracy", ascending=False)
    summary.to_csv(out / "summary.csv", index=False)
    print("\n" + summary.round(3).to_string(index=False))
    if summary["sec_per_specimen"].isna().any():
        print("\nNOTE: some models have no timing (cached results, never timed) - re-run them with --force.")
    plot(summary, out, a.font)
    print(f"\nSaved to {out.resolve()}: summary.csv, timings.json, accuracy_vs_cost.png, accuracy_vs_time.png")


if __name__ == "__main__":
    main()
