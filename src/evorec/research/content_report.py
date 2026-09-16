"""Per-epoch reports for the explicitly bounded static-content experiment."""
import html
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from evorec.research.reporting import plt, save_figure, write_atomic

KEYS = ("ndcg@10", "recall@20", "candidate_recall@200")


def cells(metrics):
    overall = metrics["cohorts"]["all_positive_events"]
    cold = metrics["cohorts"]["model_cold_available"]
    return [overall["ndcg@10"], overall["recall@20"], overall["candidate_recall@200"], cold["recall@20"], cold["candidate_recall@200"]]


def report(series, directory):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "figures").mkdir(exist_ok=True)
    published = dict(series)
    published["report_updated_at"] = datetime.now(timezone.utc).isoformat()
    published["report_renderer_sha256"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    write_atomic(directory / "results.json", json.dumps(published, ensure_ascii=False, indent=2) + "\n")
    validation = [(r["name"], r["metrics"]) for r in series.get("baselines", [])]
    validation += [(t["name"], t["best_validation"]) for t in series.get("trials", []) if t.get("best_validation")]
    table_header = ["| 方法 | NDCG@10 | Recall@20 | 候选 Recall@200 | 冷商品 Recall@20 | 冷商品候选 Recall@200 |",
                    "| --- | ---: | ---: | ---: | ---: | ---: |"]
    def markdown_table(rows):
        return table_header + [f"| {name} | " + " | ".join("—" if v is None else f"{v:.6f}" for v in cells(m)) + " |" for name, m in rows]
    def chart(rows, name, title, errors=None):
        fig, axes = plt.subplots(1, 2, figsize=(14, max(4, len(rows)*.45)))
        for column, (axis, pos, label) in enumerate(zip(axes, (0, 3), ("NDCG@10", "Model-cold available Recall@20"))):
            values = [cells(m)[pos] or 0 for _, m in rows]
            axis.barh(range(len(rows)), values, color="#226e9b" if column == 0 else "#2f8c66")
            if errors:
                for i, error in enumerate(errors):
                    if error and error[column] > 0:
                        axis.errorbar(values[i], i, xerr=error[column], fmt="none", color="black", capsize=3)
            axis.set_yticks(range(len(rows)), [n for n, _ in rows], fontsize=8)
            maximum = max((v + (errors[i][column] if errors and errors[i] else 0) for i, v in enumerate(values)), default=0)
            axis.set_xlim(0, max(maximum * 1.22, .001))
            axis.set_xlabel(label)
            axis.grid(axis="x", alpha=.15)
        fig.suptitle(title, fontsize=12)
        fig.tight_layout()
        save_figure(fig, directory / "figures" / name)
    chart(validation, "validation.png", "R03 | Static metadata assumption | Validation")
    fig, axes = plt.subplots(1, 3, figsize=(15, 4.1))
    for trial in series.get("trials", []):
        history = trial.get("history", [])
        for axis, key in zip(axes, ("train_loss", "validation_ndcg@10", "validation_cold_recall@20")):
            if history:
                axis.plot([r["epoch"] for r in history], [r[key] for r in history], marker=".", label=trial["name"])
    for axis, title in zip(axes, ("Training cross-entropy", "Validation NDCG@10", "Validation cold Recall@20")):
        axis.set_title(title)
        axis.set_xlabel("Epoch")
        axis.grid(alpha=.15)
        if axis.lines:
            axis.legend(fontsize=6)
    fig.tight_layout()
    save_figure(fig, directory / "figures/learning-curves.png")
    paragraphs = [
        "本轮使用新的用户哈希分组，用户与 R01 / R02 不重合；商品与时间区间仍重合。",
        "商品标题与类别来自静态快照，缺少历史版本时间戳。以下结果成立于静态内容可用假设，不证明严格历史内容回放或线上收益。",
        "文本词表、IDF、SVD 和双塔参数只使用本轮训练商品 / 交互；商品目录中的其他内容仅经过冻结编码器转换。",
        "每轮训练自动更新本页；HTML 每 30 秒刷新。完成后保留结果，不自动重复调参。",
    ]
    insights = []
    if series.get("selected_setting"):
        insights.append(f"验证集选中的双塔学习率为 {series['selected_setting']['learning_rate']}。")
    if series.get("test_results"):
        lookup = {r["name"]: r for r in series["test_results"]}
        if series.get("selected_validation_method") in lookup:
            selected = lookup[series["selected_validation_method"]]
            values = cells(selected["metrics"])
            insights.append(f"按验证 NDCG@10 选中的单次方法为 {selected['name']}；测试 NDCG@10={values[0]:.6f}，模型冷且可用分组 Recall@20={values[3]:.6f}。")
        content = lookup.get("Content-SVD")
        if content:
            value = cells(content["metrics"])[3]
            insights.append(f"纯内容路径的测试冷商品 Recall@20={value:.6f}。内容可进入候选与整体排序质量需要分别评价。")
        summary = series.get("seed_summaries", {}).get("Tower-RRF")
        reference = lookup.get("CF-blend-a0.25")
        if summary and reference:
            mean = summary["ndcg@10"]["mean"]
            deviation = summary["ndcg@10"]["sample_std"]
            baseline = reference["metrics"]["cohorts"]["all_positive_events"]["ndcg@10"]
            relative = (mean / baseline - 1) * 100
            insights.append(f"双塔融合的三种子测试 NDCG@10 为 {mean:.6f} ± {deviation:.6f}，相对本轮 CF-blend-a0.25 的观测差值为 {relative:+.1f}%。尚未进行显著性检验。")
            insights.append("纯内容路径保留了更多冷商品命中，融合方法的整体排序更好但冷商品命中更少。当前已打通候选入口，绝对召回率仍需继续改善。")
    markdown = ["# EvoRec R03：内容召回与神经双塔", "",
                f"状态：{series['status']}｜协议：{series['protocol_id']}｜更新：{published['report_updated_at']}", ""]
    for p in paragraphs + insights:
        markdown += [p, ""]
    markdown += ["## 验证集", "", *markdown_table(validation), "", "![验证比较](figures/validation.png)", "",
                 "## 训练曲线", "", "![训练曲线](figures/learning-curves.png)", ""]
    test_rows = [(r["name"], r["metrics"]) for r in series.get("test_results", [])]
    test_chart_rows, errors = [], []
    if test_rows:
        markdown += ["## 冻结模型后的测试结果", "", *markdown_table(test_rows), ""]
        for result in series["test_results"]:
            if "seed" not in result:
                test_chart_rows.append((result["name"], result["metrics"]))
                errors.append(None)
        for family, summary in series.get("seed_summaries", {}).items():
            synthetic = {"cohorts": {
                "all_positive_events": {k: summary[k]["mean"] for k in KEYS},
                "model_cold_available": {"recall@20": summary["cold_recall@20"]["mean"],
                                         "candidate_recall@200": summary["cold_candidate_recall@200"]["mean"]},
            }}
            test_chart_rows.append((family + " (3-seed mean)", synthetic))
            errors.append([summary["ndcg@10"]["sample_std"], summary["cold_recall@20"]["sample_std"]])
        chart(test_chart_rows, "test.png", "R03 test | Neural error bars = sample SD, not confidence intervals", errors)
        markdown += ["![测试比较](figures/test.png)", "",
                     "### 三种子均值与波动", "",
                     "| 系列 / 指标 | 均值 | 样本标准差 |", "| --- | ---: | ---: |"]
        for family, summary in series.get("seed_summaries", {}).items():
            markdown += [f"| {family} / {key} | {m['mean']:.6f} | {m['sample_std']:.6f} |" for key, m in summary.items()]
        markdown += ["", "均值来自三个独立模型指标，不是预测集成；标准差不是置信区间，本轮没有进行显著性检验。", ""]
    if series.get("test_diagnostics"):
        markdown += ["## 可达性与样本数", "", "| 项目 | 数量 |", "| --- | ---: |"]
        markdown += [f"| {key} | {value} |" for key, value in series["test_diagnostics"].items()]
        markdown += ["", "分组有交叉，不能相加。缺失内容或全历史无法表示的请求使用近期热门回退。", ""]
    markdown += ["## 证据", "", "- [结果、种子、配置和指标](results.json)",
                 "- [预先固定的协议](../r03-content-protocol.md)",
                 "- 原始内容、编码器、模型、代码快照及请求记录保存在本地运行目录，文件哈希随结果记录。",
                 "- 精确检索在每个请求的合法商品集合中排序；批量 GPU 耗时不等于在线 p95。",
                 "- TF-IDF/SVD 是统计文本表示；双塔是可训练 MLP，不使用预训练语言模型。", ""]
    write_atomic(directory / "report.md", "\n".join(markdown))
    body = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta http-equiv="refresh" content="30"><title>EvoRec R03 内容召回报告</title><style>body{font:16px/1.8 system-ui,'Microsoft YaHei',sans-serif;max-width:1200px;margin:32px auto;padding:0 20px;background:#f6f8fa;color:#19394b}img{width:100%;height:auto;background:white;border-radius:12px}section{background:#eef4e9;padding:16px 24px;border-radius:12px}a{color:#1464a0}</style></head><body><h1>EvoRec R03：内容召回与神经双塔</h1>"""
    body += f"<p>状态：{html.escape(series['status'])} · 协议：{html.escape(series['protocol_id'])} · {html.escape(published['report_updated_at'])}</p>"
    body += "<section>" + "".join(f"<p>{html.escape(p)}</p>" for p in paragraphs + insights) + "</section>"
    for title, name in [("验证比较", "validation.png"), ("训练曲线", "learning-curves.png")] + ([("测试比较", "test.png")] if test_rows else []):
        body += f'<h2>{title}</h2><img src="figures/{name}" alt="{title}">'
    body += '<p><a href="report.md">完整数据表与结论</a> · <a href="results.json">机器可读结果</a> · <a href="../r03-content-protocol.md">协议与假设</a></p></body></html>'
    write_atomic(directory / "report.html", body)
