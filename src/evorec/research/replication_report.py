"""Live training report and final replication figures, separate from historical R05."""
import html
import json
from pathlib import Path

from evorec.research.reporting import plt, save_figure, write_atomic
from evorec.research.runner import file_sha


def render(series, analysis=None):
    out = Path(series["configuration"]["report_directory"])
    (out / "figures").mkdir(parents=True, exist_ok=True)
    original = json.loads((Path(series["configuration"]["source_run"]) / "series.json").read_text(encoding="utf-8"))
    published = {"series": series, "analysis": analysis, "renderer_sha256": file_sha(Path(__file__))}
    write_atomic(out / "results.json", json.dumps(published, ensure_ascii=False, indent=2) + "\n")
    fig, axes = plt.subplots(1, 3, figsize=(14, 4))
    keys = ("train_loss", "validation_ndcg@10", "validation_cold_recall@20")
    titles = ("Weighted training loss", "Validation NDCG@10", "Validation cold Recall@20")
    for trial in series["trials"]:
        for axis, key in zip(axes, keys):
            axis.plot([r["epoch"] for r in trial["history"]],
                      [r[key] for r in trial["history"]], marker=".", label=f"seed {trial['seed']}")
    for axis, title in zip(axes, titles):
        axis.set_title(title)
        axis.set_xlabel("Epoch")
        axis.grid(alpha=0.2)
        if axis.lines:
            axis.legend()
    fig.tight_layout()
    save_figure(fig, out / "figures/learning-curves.png")
    notes = [
        "本轮是原 R05 的固定配置复验及事后统计补充，旧测试已经查看，不是新的封闭测试。",
        "训练样例、候选池、编码器、模型结构、损失权重和优化设置均固定；只补种子并重放 seed 17。",
        "逐种子最佳轮次仅由全体验证 NDCG@10 选定。没有按测试结果重新选择方法或种子。",
    ]
    rows = [r for r in original["test_results"] if r["name"] in {"RRF", "CF-blend"}] + series["test_results"]
    headers = ["方法", "整体 NDCG@10", "有历史 NDCG@10", "冷 Recall@20", "冷命中"]
    table_rows = []
    for row in rows:
        cohorts = row["metrics"]["cohorts"]
        cold = cohorts["model_cold_available"]
        table_rows.append([
            row["name"], f"{cohorts['all_positive_events']['ndcg@10']:.6f}",
            f"{cohorts['history_present']['ndcg@10']:.6f}", f"{cold['recall@20']:.6f}",
            f"{round(cold['recall@20'] * cold['n'])} / {cold['n']}",
        ])
    lines = ["# R05 冷加权模型复验与用户聚类区间", "",
             f"状态：{series['status']}；复验编号：{series['replication_id']}", ""]
    for note in notes:
        lines.extend([note, ""])
    lines.extend(["## 训练", "", "![三种子训练曲线](figures/learning-curves.png)", "",
                  "| 种子 | 最佳轮次 | 实际轮数 | 重载验证 |", "| --- | ---: | ---: | --- |"])
    for t in series["trials"]:
        lines.append(f"| {t['seed']} | {t.get('best_epoch', '—')} | {len(t['history'])} | {t.get('checkpoint_reload_verified', False)} |")
    lines.extend(["", "## 测试结果", "", "| " + " | ".join(headers) + " |",
                  "| --- | ---: | ---: | ---: | ---: |"])
    lines.extend("| " + " | ".join(row) + " |" for row in table_rows)
    images = ["learning-curves.png"]
    extra_html = ""
    if analysis:
        summary = analysis["seed_summary"]
        all_ndcg = summary["all_positive_events"]["ndcg@10"]
        cold_recall = summary["model_cold_available"]["recall@20"]
        summary_text = (
            f"冷加权三种子整体 NDCG@10={all_ndcg['mean']:.6f} ± {all_ndcg['sample_std']:.6f}；"
            f"冷目标 Recall@20={cold_recall['mean']:.6f} ± {cold_recall['sample_std']:.6f}。"
            "± 是种子间样本标准差，不是置信区间。"
        )
        lines.extend(["", summary_text, "", "## 配对用户区间", "",
                      "以下展示三个固定种子的逐请求指标平均相对基线的差值。它不是集成排序器；"
                      "区间条件于固定检查点，只反映用户抽样不确定性。完整 48 项比较见 uncertainty.json。", "",
                      "| 分组 / 指标 | 对照 | 差值 | 95% 边际区间 |",
                      "| --- | --- | ---: | --- |"])
        selected = [
            r for r in analysis["intervals"]
            if r["method"] == "Fixed-seed metric mean"
            and ((r["cohort"] != "model_cold_available" and r["metric"] == "ndcg@10")
                 or (r["cohort"] == "model_cold_available" and r["metric"] == "recall@20"))
        ]
        for row in selected:
            lines.append(f"| {row['cohort']} / {row['metric']} | {row['baseline']} | "
                         f"{row['estimate']:+.6f} | [{row['low']:+.6f}, {row['high']:+.6f}] |")
        fig, axes = plt.subplots(1, 3, figsize=(14, 4))
        for axis, cohort, title in zip(
                axes, ("all_positive_events", "history_present", "model_cold_available"),
                ("All: NDCG@10 difference", "History: NDCG@10 difference", "Cold: Recall@20 difference")):
            shown = [r for r in selected if r["cohort"] == cohort]
            for index, row in enumerate(shown):
                axis.plot([row["low"], row["high"]], [index, index], color="#226a9d", linewidth=3)
                axis.scatter([row["estimate"]], [index], color="#d27a25", zorder=3)
            axis.axvline(0, color="#555", linestyle="--", linewidth=1)
            axis.set_yticks(range(len(shown)), [f"vs {r['baseline']}" for r in shown])
            axis.set_title(title)
            axis.set_xlabel("Absolute metric difference (95% marginal CI)")
            axis.set_ylim(-0.6, len(shown) - 0.4)
            axis.grid(axis="x", alpha=0.2)
        fig.suptitle("Fixed-seed metric mean | Paired user bootstrap, 10,000 resamples", fontsize=12)
        fig.tight_layout()
        save_figure(fig, out / "figures/paired-intervals.png")
        images.append("paired-intervals.png")
        limitations = (
            "区间未进行多重比较校正；不能据 48 项中的个别区间声称总体显著。"
            "用户聚类未处理跨用户的共同商品和时间冲击，也不包含训练随机性。"
            "静态元数据、低候选覆盖和离线业务边界保持不变。"
        )
        lines.extend(["", "![用户聚类区间](figures/paired-intervals.png)", "", limitations, "",
                      f"审计：{analysis['audited_queries']:,} 条记录，"
                      f"{analysis['audited_candidate_checks']:,} 次候选检查；seed 17 原始测试排名逐请求一致。",
                      "", "[完整区间](uncertainty.json) · [机器可读结果](results.json)"])
        extra_html = f"<p>{html.escape(summary_text)}</p><p>{html.escape(limitations)}</p>"
    write_atomic(out / "report.md", "\n".join(lines) + "\n")
    table = "<table><thead><tr>" + "".join(f"<th>{html.escape(h)}</th>" for h in headers)
    table += "</tr></thead><tbody>"
    table += "".join("<tr>" + "".join(f"<td>{html.escape(c)}</td>" for c in row) + "</tr>" for row in table_rows)
    table += "</tbody></table>"
    body = "<!doctype html><html lang='zh-CN'><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
    body += "<title>EvoRec 冷加权模型复验</title><style>body{max-width:1080px;margin:40px auto;padding:0 22px;font:16px/1.8 system-ui;color:#213547;background:#fafbfc}h1{line-height:1.3}table{border-collapse:collapse;width:100%;background:white}td,th{padding:10px;border-bottom:1px solid #ddd;text-align:left}img{width:100%;background:white;border-radius:10px;margin:20px 0}a{color:#23618b}.meta{color:#65758b}</style><body>"
    body += f"<h1>冷加权模型复验与用户聚类区间</h1><p class='meta'>状态：{html.escape(series['status'])} · {series['replication_id']}</p>"
    body += "".join(f"<p>{html.escape(n)}</p>" for n in notes)
    body += table + extra_html
    body += "".join(f"<img src='figures/{name}' alt='{name.removesuffix('.png')}'>" for name in images)
    body += "<p><a href='report.md'>完整说明与区间表</a> · <a href='results.json'>结果数据</a></p></body></html>"
    write_atomic(out / "report.html", body)
