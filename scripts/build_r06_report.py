"""Publish audited R06 summaries and measured figures; no private traces are copied."""
import argparse
import hashlib
import html
import json
import shutil
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

FAMILIES = ("A-frozen", "B-frozen", "C-adapted", "D-adapted")
COLORS = ("#41657d", "#da9f35", "#257f79", "#9665a0")


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def table(headers, rows):
    return "<table><thead><tr>"+"".join("<th>"+html.escape(str(x))+"</th>" for x in headers)+"</tr></thead><tbody>"+"".join(
        "<tr>"+"".join("<td>"+html.escape(str(x))+"</td>" for x in row)+"</tr>" for row in rows)+"</tbody></table>"


def save_figure(fig, name, directory):
    fig.savefig(directory / (name+".png"), dpi=160, bbox_inches="tight", facecolor="white")
    fig.savefig(directory / (name+".svg"), bbox_inches="tight", facecolor="white", metadata={"Date": None})
    plt.close(fig)


def render(run):
    series, analysis = read(run / "series.json"), read(run / "analysis.json")
    if series["status"] != "completed" or analysis["status"] != "passed":
        raise ValueError("completed training and passed audit are required")
    if analysis["series_sha256"] != sha(run / "series.json"):
        raise ValueError("audit refers to a different run")
    directory = Path(series["configuration"]["report_directory"])
    figures = directory / "figures"
    figures.mkdir(parents=True, exist_ok=True)
    plt.rcParams.update({"font.family": "DejaVu Sans", "font.size": 10, "axes.spines.top": False,
                         "axes.spines.right": False, "svg.hashsalt": "evorec-r06"})
    diagnostics = series["test_candidates"]["diagnostics"]
    fig, axes = plt.subplots(1, 2, figsize=(11.5, 4.3))
    groups = ("model_cold_available", "history_cold_available", "no_history_cold_available")
    positions = np.arange(3)
    for offset, arm, color in ((-.18, "A", COLORS[0]), (.18, "B", COLORS[1])):
        values = [100*diagnostics[arm][name]["pool_recall"] for name in groups]
        bars = axes[0].bar(positions+offset, values, .34, color=color, label=arm+" candidate pool")
        for bar, name in zip(bars, groups):
            value = diagnostics[arm][name]
            axes[0].text(bar.get_x()+bar.get_width()/2, bar.get_height()+.12,
                         str(value["target_in_pool"]), ha="center", fontsize=9)
    axes[0].set_xticks(positions, ["All cold", "History + cold", "No history + cold"])
    axes[0].set_ylabel("Cold target in pool (%)")
    axes[0].set_title("Same budget: CF 200 + content 200")
    axes[0].legend()
    axes[0].margins(y=.22)
    for index, name in enumerate(FAMILIES):
        result = analysis["seed_summary"]["all_positive_events"][name]["ndcg@10"]
        axes[1].bar(index, result["mean"], color=COLORS[index], yerr=result["sample_std"], capsize=4)
        axes[1].text(index, result["mean"]+result["sample_std"]+.00015, f'{result["mean"]:.6f}',
                     ha="center", fontsize=9)
    axes[1].set_xticks(range(4), ["A frozen", "B frozen", "C adapted", "D adapted"])
    axes[1].set_ylabel("Overall NDCG@10")
    axes[1].set_title("Three fixed seeds: mean and sample SD")
    axes[1].margins(y=.25)
    fig.tight_layout()
    save_figure(fig, "coverage-and-ranking", figures)

    fig, axes = plt.subplots(2, 2, figsize=(11.5, 7.5))
    for column, arm in enumerate(("D", "C")):
        for color, seed in zip(COLORS[:3], (17, 29, 43)):
            trial = next(t for t in series["trials"] if t["arm"] == arm and t["seed"] == seed)
            epochs = [row["epoch"] for row in trial["history"]]
            axes[0, column].plot(epochs, [r["train_loss"] for r in trial["history"]], marker="o", color=color, label=f"seed {seed}")
            axes[1, column].plot(epochs, [r["validation_ndcg@10"] for r in trial["history"]], marker="o", color=color)
            best = trial["best_epoch"]
            row = next(row for row in trial["history"] if row["epoch"] == best)
            axes[1, column].scatter([best], [row["validation_ndcg@10"]], marker="*", s=160, color=color, edgecolors="black", zorder=4)
        axes[0, column].set_title(arm+": "+("centroid retraining control" if arm == "D" else "multi-interest adaptation"))
        axes[0, column].set_ylabel("Cold-weighted listwise loss")
        axes[0, column].legend()
        axes[1, column].set_ylabel("Validation NDCG@10")
        axes[1, column].set_xlabel("Epoch (stars = selected checkpoint)")
    for ax in axes.flat:
        ax.grid(alpha=.18)
    fig.tight_layout()
    save_figure(fig, "learning-curves", figures)

    fig, axes = plt.subplots(1, 2, figsize=(12, 4.8))
    for ax, cohort, metric, title in zip(axes,
        ("all_positive_events", "model_cold_available"), ("ndcg@10", "recall@20"),
        ("Overall NDCG@10 difference", "Cold Recall@20 difference")):
        rows = [r for r in analysis["intervals"] if r["cohort"] == cohort and r["metric"] == metric and r["status"] == "estimated"]
        for index, row in enumerate(rows):
            ax.plot([row["low"], row["high"]], [index, index], color="#257f79", linewidth=2)
            ax.scatter([row["estimate"]], [index], color="#257f79", s=34)
        ax.set_yticks(range(len(rows)), [r["method"]+" - "+r["baseline"] for r in rows])
        ax.axvline(0, color="#89939d", linestyle="--", linewidth=1)
        ax.set_title(title)
        ax.set_xlabel("Paired difference; 95% marginal interval")
        ax.grid(axis="x", alpha=.18)
        ax.invert_yaxis()
    fig.suptitle("User-cluster bootstrap; fixed checkpoints; no multiplicity adjustment", fontsize=11)
    fig.tight_layout()
    save_figure(fig, "paired-intervals", figures)

    result = {"series": series, "analysis": analysis, "renderer": "scripts/build_r06_report.py",
              "renderer_sha256": sha(__file__)}
    (directory / "results.json").write_text(json.dumps(result, indent=2)+"\n", encoding="utf-8")
    shutil.copyfile(run / "analysis.json", directory / "uncertainty.json")
    manifest = {"source_series_sha256": sha(run / "series.json"), "source_analysis_sha256": sha(run / "analysis.json"),
                "source_results_sha256": sha(directory / "results.json"), "generator_sha256": sha(__file__),
                "assets": [{"file": p.name, "sha256": sha(p)} for p in sorted(figures.iterdir()) if p.suffix in {".png", ".svg"}]}
    (figures / "manifest.json").write_text(json.dumps(manifest, indent=2)+"\n", encoding="utf-8")
    rows = []
    for result in series["test_results"]:
        cohorts = result["metrics"]["cohorts"]
        cold = cohorts["model_cold_available"]
        rows.append([result["name"], f'{cohorts["all_positive_events"]["ndcg@10"]:.6f}',
                     f'{cohorts["history_present"]["ndcg@10"]:.6f}', f'{cold["recall@20"]:.6f}',
                     f'{round(cold["recall@20"]*cold["n"])} / {cold["n"]}'])
    headers = ["方法", "整体 NDCG@10", "有历史 NDCG@10", "冷 Recall@20", "冷 Top20 命中"]
    pool_rows = [[arm, group, str(value["n"]), str(value["target_in_pool"]), f'{value["pool_recall"]:.6f}',
                  f'{value["mean_pool_candidates"]:.2f}']
                 for arm, groups_ in diagnostics.items() for group, value in groups_.items()]
    trials = [[t["name"], str(len(t["history"])), str(t["best_epoch"]), str(t["checkpoint_reload_verified"])] for t in series["trials"]]
    costs = []
    for split, entry in [("train "+f["name"], f) for f in series["training_folds"]] + [
        ("validation", series["validation_candidates"]), ("test", series["test_candidates"])]:
        cost = entry["cost"]
        costs.append([split, f'{cost["cf_fit_and_rank_seconds"]:.3f}', f'{cost["centroid"]["wall_seconds"]:.3f}',
                      f'{cost["multi_interest"]["including_setup_wall_seconds"]:.3f}',
                      str(cost["centroid"]["dense_vector_dot_products"]), str(cost["multi_interest"]["dense_vector_dot_products"])])
    cold_a, cold_b = [diagnostics[arm]["model_cold_available"] for arm in ("A", "B")]
    no_history = diagnostics["B"]["no_history_cold_available"]
    conclusions = [
        "结论：本轮不支持用多兴趣路径替换原路径；验证规则保留 "+series["selected_method"]+"，不按测试结果重新选型。",
        f'同样的候选数量预算下，冷目标入池由 {cold_a["target_in_pool"]} / {cold_a["n"]} 提高到 {cold_b["target_in_pool"]} / {cold_b["n"]}（{100*cold_a["pool_recall"]:.3f}% → {100*cold_b["pool_recall"]:.3f}%）。这是描述性覆盖变化，不等于排序收益。',
        f'无历史冷目标仍为 {no_history["target_in_pool"]} / {no_history["n"]}；该人群没有从近期多兴趣表示受益。',
        "固定排序器的 B-A 与匹配重训练的 C-D，整体 NDCG 和冷 Recall 的预登记区间均跨过零。C-D 的整体 NDCG 上界很接近零但仍为正，不能据四舍五入宣称确定下降。",
        "B-RRF 相对 A-RRF 的整体 NDCG 边际区间为正，说明收益依赖排序方法；C 相对 CF 的优势也不能解释成多兴趣相对均值召回的独立优势。",
    ]
    family_headers = ["方法族", "整体 NDCG@10 均值 ± 样本标准差", "冷 Recall@20 均值 ± 样本标准差"]
    family_rows = [[name] + [
        f'{analysis["seed_summary"][cohort][name][metric]["mean"]:.6f} ± {analysis["seed_summary"][cohort][name][metric]["sample_std"]:.6f}'
        for cohort, metric in (("all_positive_events", "ndcg@10"), ("model_cold_available", "recall@20"))
    ] for name in FAMILIES]
    interval_headers = ["比较", "分组与指标", "配对差值", "95% 边际区间"]
    interval_rows = [[r["method"]+" − "+r["baseline"],
                      ("整体 NDCG@10" if r["cohort"] == "all_positive_events" else "冷 Recall@20"),
                      f'{r["estimate"]:+.9f}', f'[{r["low"]:+.9f}, {r["high"]:+.9f}]']
                     for r in analysis["intervals"]
                     if (r["cohort"], r["metric"]) in {("all_positive_events","ndcg@10"), ("model_cold_available","recall@20")}]
    lines = ["# R06 同预算多兴趣召回与排序适配", "",
             "状态：completed，审计 passed。协议："+series["protocol_id"]+"。实际训练代码："+series["code"]["git_base_commit"]+"。", "",
             "新用户桶与 R01–R05 用户交集为零；全部检查点及验证选型落盘后才开放测试。日历、商品和静态元数据假设仍与旧轮重合。", "",
             "A/B 使用原 R05 冻结检查点；C/D 在 R06 验证集选轮。A/D 使用均值召回，B/C 使用均值＋近期多兴趣召回。CF 200、内容 200、并集最多 400。", "",
             "**验证规则选中："+series["selected_method"]+"**。质量下限："+f'{series["validation_ndcg_floor"]:.6f}'+"。测试结果不参与重新选型。", "",
             "## 候选覆盖与排名", "", "![同预算候选覆盖与整体排序](figures/coverage-and-ranking.png)", ""]
    def md_table(head, data):
        return ["| "+" | ".join(head)+" |", "| "+" | ".join(["---"]*len(head))+" |"]+["| "+" | ".join(row)+" |" for row in data]
    lines += ["## 结果解读", ""] + [text+"\n" for text in conclusions]
    lines += md_table(family_headers, family_rows)+["", "下面保留全部方法的单次结果。", ""]
    lines += md_table(headers, rows)+["", "## 分组候选覆盖", ""]
    pool_headers = ["候选路径", "分组", "请求数", "目标入池数", "入池率", "平均并集大小"]
    lines += md_table(pool_headers, pool_rows)+["", "## 训练与选轮", "", "![实际训练曲线](figures/learning-curves.png)", ""]
    lines += md_table(["模型", "实际轮数", "最佳轮次", "重载一致"], trials)
    lines += ["", "## 配对用户区间", "", "![预登记配对区间](figures/paired-intervals.png)", "",
              "共 50 项预登记比较。三个固定种子的逐请求指标平均不是集成排序器；图中的区间条件于固定检查点，不包含训练随机性，未做多重比较校正。全部区间见 [uncertainty.json](uncertainty.json)。", "",
              "## 计算成本", ""]
    lines[-2:-2] = md_table(interval_headers, interval_rows)+[""]
    cost_headers = ["阶段", "CF 拟合及排名秒", "均值内容秒", "新增多兴趣秒", "均值向量点积数", "多兴趣向量点积数"]
    lines += md_table(cost_headers, costs)+["", "A/B 共享 CF 与均值内容计算；新增多兴趣耗时含对象初始化，详细融合及特征构造耗时见结果 JSON。单次离线批处理观测受缓存与设备状态影响，不是线上延迟或稳定性能基准。", "",
              "## 验证和边界", "",
              f'完整重放 {analysis["audited_source_queries"]:,} 条候选来源请求与 {analysis["audited_ranking_queries"]:,} 条排名记录，共 {analysis["audited_candidate_checks"]:,} 次候选检查。均值路径训练缓存与 R05 完全一致，所有新检查点重载验证通过。',
              "", "低召回分母、缺失历史、静态元数据和候选遗漏均保留。新排序器的训练正例取决于实际候选覆盖；C-D 是匹配重训练对照，C-B 同时包含重新选轮，不能作单一因素归因。",
              "", "[协议](../r06-multi-interest-protocol.md) · [运行指南](../../../research/R06-multi-interest-guide.md) · [机器可读结果](results.json) · [图表来源](figures/manifest.json)", ""]
    (directory / "report.md").write_text("\n".join(lines), encoding="utf-8")
    style = "body{font:16px/1.75 system-ui,'Microsoft YaHei',sans-serif;max-width:1180px;margin:32px auto;padding:0 20px;color:#243d4b;background:#f8fafb}table{border-collapse:collapse;width:100%;background:white;margin:18px 0}td,th{padding:8px 12px;border-bottom:1px solid #dce5ea;text-align:left}th{background:#eaf0f4}img{max-width:100%;background:white}section{padding:18px;background:#edf5f2}a{color:#286775}"
    page = '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>EvoRec R06 实验报告</title><style>'+style+'</style></head><body><h1>R06：同预算多兴趣召回与排序适配</h1>'
    page += "<section><p>训练完成，候选与排名审计通过。验证规则选中 "+html.escape(series["selected_method"])+"。</p><p>A/B 冻结 R05 排序器；C/D 重新训练。A/D 使用均值召回，B/C 使用均值＋多兴趣召回。</p><p>三个种子指标平均不是集成模型；用户区间条件于固定检查点、未做多重比较校正。无线上收益结论。</p></section>"
    page += "<h2>结果解读</h2>"+"".join("<p>"+html.escape(text)+"</p>" for text in conclusions)+table(family_headers, family_rows)
    page += '<h2>同预算比较</h2><img src="figures/coverage-and-ranking.png" alt="候选覆盖和三种子整体排序结果">'+table(headers, rows)
    page += "<h2>分组候选覆盖</h2>"+table(pool_headers, pool_rows)
    page += '<h2>实际训练</h2><img src="figures/learning-curves.png" alt="C 和 D 的三个种子训练曲线">'+table(["模型", "实际轮数", "最佳轮次", "重载一致"], trials)
    page += '<h2>预登记区间</h2><img src="figures/paired-intervals.png" alt="配对用户抽样的差值与边际区间">'
    page += table(interval_headers, interval_rows)
    page += "<h2>计算成本</h2>"+table(cost_headers, costs)+"<p>共享计算与新增计算分别记录；单次批处理时间不是线上 p95。</p>"
    page += '<p><a href="report.md">完整解释</a> · <a href="results.json">结果数据</a> · <a href="uncertainty.json">全部区间</a> · <a href="figures/manifest.json">图表来源</a></p></body></html>'
    (directory / "report.html").write_text(page+"\n", encoding="utf-8")
    archive = Path("docs/experiments/archive") / (run.name+".json")
    if archive.exists() and archive.read_bytes() != (run / "series.json").read_bytes():
        raise ValueError("refusing to overwrite a different run archive")
    archive.write_bytes((run / "series.json").read_bytes())
    print(json.dumps({"status": "completed", "report": directory.as_posix(), "figure_files": 6}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", type=Path, required=True)
    args = parser.parse_args()
    render(args.run)
