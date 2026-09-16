"""Render the registered R04 result without selecting a policy from test data."""
import argparse
import html
import json
from pathlib import Path

from evorec.research.reporting import plt, save_figure, write_atomic
from evorec.research.runner import file_sha


def cells(row):
    cohorts = row["metrics"]["cohorts"]
    a, c = cohorts["all_positive_events"], cohorts["model_cold_available"]
    return a["ndcg@10"], a["recall@20"], c["recall@20"]


def report(run):
    series = json.loads((run/"series.json").read_text(encoding="utf-8"))
    if series["status"] != "completed":
        raise ValueError("only completed runs can be published as results")
    directory = Path(series["configuration"]["report_directory"])
    figures = directory/"figures"
    figures.mkdir(parents=True, exist_ok=True)
    config = series["configuration"]
    policies = [r for r in series["validation_results"] if "policy" in r]
    selected = series["selected_policy"]["name"]
    labels = [r["policy"]["name"] for r in policies]
    plt.rcParams.update({"font.size": 10, "axes.spines.top": False, "axes.spines.right": False})
    fig, axes = plt.subplots(1, 2, figsize=(12, 4.6))
    for axis, index, title in zip(axes, (0, 2), ("Overall NDCG@10", "Model-cold available Recall@20")):
        values = [cells(r)[index] for r in policies]
        bars = axis.bar(labels, values, color=["#226e9b" if label == selected else "#b7cdd9" for label in labels])
        axis.bar_label(bars, fmt="%.6f", fontsize=8, padding=3)
        axis.set_ylim(0, max(values)*1.24)
        axis.set_title(title)
        axis.grid(axis="y", alpha=.15)
    axes[0].axhline(series["validation_ndcg_floor"], color="#ba5142", linestyle="--", label="97% quality floor")
    axes[0].legend(fontsize=8, loc="lower right")
    fig.suptitle(f"R04 validation | seed 17 | selected: {selected}")
    fig.tight_layout()
    save_figure(fig, figures/"validation.png")

    test = [r for r in series["test_results"] if r.get("seed") == config["seeds"][0]]
    fig, axes = plt.subplots(1, 2, figsize=(12, 4.6))
    for axis, index, title in zip(axes, (0, 2), ("Overall NDCG@10", "Model-cold available Recall@20")):
        values = [cells(r)[index] for r in test]
        bars = axis.bar(labels, values, color=["#226e9b" if label == selected else "#a7c5b1" for label in labels])
        axis.bar_label(bars, fmt="%.6f", fontsize=8, padding=3)
        axis.set_ylim(0, max(values)*1.25 if max(values) else 1)
        axis.set_title(title)
        axis.grid(axis="y", alpha=.15)
    fig.suptitle("R04 test | Predeclared seed-17 ablations; no test-based selection")
    fig.tight_layout()
    save_figure(fig, figures/"test-ablation.png")

    fig, axes = plt.subplots(1, 2, figsize=(12, 4.6))
    changed = [100*r["gate_statistics"]["ranking_changed"]/r["gate_statistics"]["requests"] for r in test]
    promoted = [r["gate_statistics"]["new_cold_promoted"]/r["gate_statistics"]["requests"] for r in test]
    for axis, values, title in zip(axes, (changed, promoted), ("Requests with changed ranking (%)", "New cold items promoted per request")):
        bars = axis.bar(labels, values, color="#ce9750")
        axis.bar_label(bars, fmt="%.2f", fontsize=8, padding=3)
        axis.set_ylim(0, max(values)*1.2 if max(values) else 1)
        axis.set_title(title)
        axis.grid(axis="y", alpha=.15)
    fig.suptitle("R04 test | Candidate exposure is not target recall")
    fig.tight_layout()
    save_figure(fig, figures/"gate-activity.png")
    summary = series["seed_summaries"][selected]
    cold_same = len({cells(r)[2] for r in policies}) == 1
    insights = [
        f"验证集选中 {selected}。整体 NDCG@10 下限预先固定为原融合的 97%，本轮下限为 {series['validation_ndcg_floor']:.6f}。",
        ("五种策略的验证冷商品 Recall@20 相同，增加保留位置没有提供选型收益；同分时按整体 NDCG、保留数量与声明顺序选择，原融合保留。"
         if cold_same else "选型优先提高验证冷商品 Recall@20，同时满足整体质量下限；所有阈值在查看本轮结果前固定。"),
        f"选中策略的三个冻结模型测试 NDCG@10 为 {summary['ndcg@10']['mean']:.6f} ± {summary['ndcg@10']['sample_std']:.6f}；冷商品 Recall@20 为 {summary['cold_recall@20']['mean']:.6f} ± {summary['cold_recall@20']['sample_std']:.6f}。",
        "± 表示模型种子间样本标准差，不是置信区间或集成结果。本轮没有显著性检验。",
        "本轮没有新增训练：TF-IDF、SVD、三个双塔检查点及协同统计均来自冻结的 R03 训练数据。新用户早期行为只用于请求历史。",
        "门控只读取请求时可见历史的可表示数量与一致性，不读取目标商品或命中标签。两个候选路径仍全部计算，因此不能声称节省了推理成本。",
        "冷商品指从未出现在冻结 R03 训练交互中的商品（含负反馈记录）；可用性按完整类别首次交互时间代理。静态标题与类别缺少历史版本时间戳。",
        "R04 与之前用户不重合，但商品和时间重合。只能在本轮相同协议内比较，不能把跨轮分数差写成模型提升。",
    ]
    def table(rows):
        lines = ["| 方法 | 整体 NDCG@10 | 整体 Recall@20 | 冷商品 Recall@20 | 冷命中 / 冷目标 |",
                 "| --- | ---: | ---: | ---: | ---: |"]
        for row in rows:
            c = row["metrics"]["cohorts"]["model_cold_available"]
            lines.append(f"| {row['name']} | "+" | ".join(f"{v:.6f}" for v in cells(row))+
                         f" | {round(c['recall@20']*c['n'])} / {c['n']} |")
        return lines
    lines = ["# EvoRec R04：冷商品保留与规则门控", "",
             f"状态：completed｜协议：{series['protocol_id']}｜完成时间：{series['finished_at']}", ""]
    for p in insights:
        lines += [p, ""]
    lines += ["## 验证集选型", "", *table(policies), "", "![验证策略比较](figures/validation.png)", "",
              "## 冻结选型后的测试", "", *table(series["test_results"]), "",
              "下图为预先登记的 seed 17 消融。其测试结果不会用于回改选中策略。", "",
              "![测试消融](figures/test-ablation.png)", "", "## 曝光与门控活动", "",
              "![门控活动](figures/gate-activity.png)", "",
              "reserve 表示无门槛保留；gated 额外要求至少 2 个可表示历史商品，且加权平均单位向量的模长不低于 0.6。历史权重按 0.8 衰减。", "",
              "已有 Top20 冷商品会受保护。quota=1 尝试保留到第 20 位；quota=2 尝试放在第 10、20 位。原候选已满足数量、内容候选缺少冷商品或门槛未通过时不改列表。", "",
              "## 多种子汇总", "", "| 策略 / 指标 | 均值 | 样本标准差 |", "| --- | ---: | ---: |"]
    for name, stats in series["seed_summaries"].items():
        lines += [f"| {name} / {key} | {value['mean']:.6f} | {value['sample_std']:.6f} |" for key,value in stats.items()]
    lines += ["", "## 样本与证据", "",
              f"查询样本 {series['data_provenance']['rows']:,} 条交互、{series['data_provenance']['users']:,} 名用户；"
              f"验证 {series['validation_diagnostics']['all_positive_events']:,} 个正反馈事件，测试 {series['test_queries']:,} 个。", "",
              "- [配置、数据指纹、各分组与逐请求轨迹索引](results.json)",
              "- [实验前登记的协议](../r04-gating-protocol.md)",
              "- [独立审计](../../validation/gating-checks.json)",
              "- [测试后的失败定位（不参与选型）](error-analysis.md)",
              "- [运行说明](../../../research/R04-gating-guide.md)", "",
              "原始数据、模型和压缩轨迹位于本地忽略目录；机器可读摘要进入文档归档。", ""]
    write_atomic(directory/"report.md", "\n".join(lines))
    published = {**series, "report_renderer_sha256": file_sha(Path(__file__))}
    write_atomic(directory/"results.json", json.dumps(published, ensure_ascii=False, indent=2)+"\n")
    archive = Path("docs/experiments/archive")/(run.name+".json")
    archive.parent.mkdir(parents=True, exist_ok=True)
    if archive.exists() and archive.read_bytes() != (run/"series.json").read_bytes():
        raise ValueError("refusing to replace a different completed archive")
    archive.write_bytes((run/"series.json").read_bytes())
    body = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>EvoRec R04 冷商品门控报告</title><style>body{font:16px/1.8 system-ui,'Microsoft YaHei',sans-serif;max-width:1100px;margin:32px auto;padding:0 20px;background:#f7f8fa;color:#19394b}img{width:100%;height:auto;background:white;border-radius:12px}section{background:#edf3f6;padding:18px 24px;border-radius:12px}a{color:#17658c}</style></head><body><h1>EvoRec R04：冷商品保留与规则门控</h1>"""
    body += f"<p>完成 · 协议 {series['protocol_id']} · 固定模型，验证集选策略</p><section>"
    body += "".join(f"<p>{html.escape(p)}</p>" for p in insights)+"</section>"
    for title, name in (("验证选型", "validation"), ("测试消融", "test-ablation"), ("门控活动", "gate-activity")):
        body += f'<h2>{title}</h2><img src="figures/{name}.png" alt="{title}">'
    body += '<p><a href="report.md">完整数据与解释</a> · <a href="results.json">结果来源</a> · <a href="error-analysis.md">失败定位</a> · <a href="../../blog/evorec-project-log.md">项目博客</a></p></body></html>'
    write_atomic(directory/"report.html", body)
    return series


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", type=Path, required=True)
    report(parser.parse_args().run)
