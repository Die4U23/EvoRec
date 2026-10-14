"""Render bilingual README charts from the published R06 summary; no training."""
import hashlib
import json
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib import font_manager
from matplotlib.ticker import FormatStrFormatter, MultipleLocator

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "docs/experiments/r06-multi-interest/results.json"
OUTPUT = ROOT / "assets/readme"
FAMILIES = ("A-frozen", "B-frozen", "C-adapted", "D-adapted")


def main():
    result = json.loads(SOURCE.read_text(encoding="utf-8"))
    summary = result["analysis"]["seed_summary"]
    available = {font.name for font in font_manager.fontManager.ttflist}
    chinese_font = next((name for name in ("Microsoft YaHei", "Noto Sans CJK SC", "SimHei") if name in available), None)
    if chinese_font is None:
        raise RuntimeError("Install Microsoft YaHei, Noto Sans CJK SC or SimHei to render Chinese labels")
    OUTPUT.mkdir(parents=True, exist_ok=True)
    plt.rcParams.update({"font.size": 14, "axes.spines.top": False, "axes.spines.right": False,
                         "axes.spines.left": False, "axes.edgecolor": "#cbd5e1", "text.color": "#182d37",
                         "axes.labelcolor": "#182d37", "xtick.color": "#425563", "ytick.color": "#425563",
                         "svg.hashsalt": "evorec-readme-r06-v1", "axes.unicode_minus": False})
    rows = []
    for language in ("en", "zh"):
        plt.rcParams["font.family"] = chinese_font if language == "zh" else "DejaVu Sans"
        fig, axes = plt.subplots(2, 1, figsize=(10, 9.2), facecolor="white")
        fig.subplots_adjust(left=.13, right=.97, top=.78, bottom=.13, hspace=.72)
        title = "R06 · Offline ranking results" if language == "en" else "R06 · 离线排序结果"
        subtitle = "Same test cohort · seeds 17 / 29 / 43 · mean ± sample SD" if language == "en" else "同一测试样本 · 种子 17 / 29 / 43 · 均值 ± 样本标准差"
        fig.text(.08, .965, title, fontsize=23, weight="bold", va="top")
        fig.text(.08, .914, subtitle, fontsize=13, color="#526675", va="top")
        for ax, group, metric, label in (
            (axes[0], "all_positive_events", "ndcg@10", "Overall NDCG@10 · 12,524 events" if language == "en" else "整体 NDCG@10 · 12,524 个事件"),
            (axes[1], "model_cold_available", "recall@20", "Cold Recall@20 · 6,978 events" if language == "en" else "冷商品 Recall@20 · 6,978 个事件"),
        ):
            means = [summary[group][family][metric]["mean"] for family in FAMILIES]
            stds = [summary[group][family][metric]["sample_std"] for family in FAMILIES]
            bars = ax.bar(FAMILIES, means, yerr=stds, capsize=5, width=.58,
                          color=["#246e64", "#719c90", "#487c9e", "#8eacbf"],
                          error_kw={"ecolor": "#223c49", "elinewidth": 1.4})
            ax.set_title(label, loc="left", fontsize=16, pad=18)
            ax.set_ylim(0, max(m+s for m,s in zip(means,stds))*1.25)
            ax.yaxis.set_major_formatter(FormatStrFormatter("%.3f"))
            ax.yaxis.set_major_locator(MultipleLocator(.003 if metric == "ndcg@10" else .001))
            ax.grid(axis="y", color="#e4e9ec", linewidth=.8)
            ax.set_axisbelow(True)
            ax.tick_params(axis="both", length=0)
            for bar, mean, sd in zip(bars, means, stds):
                ax.text(bar.get_x()+bar.get_width()/2, mean+sd+ax.get_ylim()[1]*.025,
                        f"{mean:.6f}", ha="center", va="bottom", fontsize=12)
            if language == "en":
                rows.append({"group": group, "metric": metric, "families": {
                    family: summary[group][family][metric] for family in FAMILIES}})
        note = ("Error bars show seed variation, not confidence intervals.\nPaired B−A and C−D intervals cross zero; A-frozen-s17 was retained."
                if language == "en" else "误差线表示种子间波动，不是置信区间。\n配对 B−A、C−D 区间均跨零；验证规则保留 A-frozen-s17。")
        fig.text(.08, .055, note, fontsize=11, color="#526675", linespacing=1.6)
        for extension in ("png", "svg"):
            metadata = {"Date": None} if extension == "svg" else {"Software": "EvoRec README figure generator"}
            output_path = OUTPUT / f"r06-ranking-{language}.{extension}"
            fig.savefig(output_path, dpi=160, metadata=metadata)
            if extension == "svg":
                output_path.write_text(
                    "\n".join(line.rstrip() for line in output_path.read_text(encoding="utf-8").splitlines()) + "\n",
                    encoding="utf-8", newline="\n",
                )
        plt.close(fig)
    manifest = {"source": "docs/experiments/r06-multi-interest/results.json",
                "source_sha256": hashlib.sha256(SOURCE.read_bytes()).hexdigest(),
                "generator": "scripts/build_readme_figures.py",
                "generator_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                "data": rows, "error_bars": "sample standard deviation across seeds; not confidence intervals",
                "new_experiment_run": False}
    (OUTPUT/"manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")


if __name__ == "__main__":
    main()
