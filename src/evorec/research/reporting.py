"""Export protocol-separated results and per-epoch charts with explicit evidence limits."""
import argparse
import hashlib
import html
import json
import os
from datetime import datetime, timezone
from pathlib import Path

os.environ.setdefault("MPLCONFIGDIR", str(Path("tmp/matplotlib").resolve()))
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

COLORS = ["#1464a0", "#dc7f25", "#2f8c66", "#9a5aae", "#d35062", "#596676"]
METRICS = ("ndcg@10", "recall@20", "candidate_recall@200")


def write_atomic(path, text):
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)


def save_figure(fig, path):
    for suffix in (".png", ".svg"):
        target = path.with_suffix(suffix)
        temporary = target.with_name(target.name + ".tmp")
        fig.savefig(temporary, format=suffix[1:], dpi=160, bbox_inches="tight", facecolor="white")
        temporary.replace(target)
    plt.close(fig)


def number(value):
    return "—" if value is None else f"{value:.6f}"


def md_table(rows, validation=False):
    prefix = "验证 " if validation else ""
    lines = [f"| 方法 | N | {prefix}NDCG@10 | {prefix}Recall@20 | 候选 Recall@200 |",
             "| --- | ---: | ---: | ---: | ---: |"]
    lines.extend(f"| {name} | {m['n']} | " + " | ".join(number(m[k]) for k in METRICS) + " |" for name, m in rows)
    return lines


def html_table(rows):
    body = '<div class="table-wrap"><table><thead><tr><th>方法</th><th>N</th><th>NDCG@10</th><th>Recall@20</th><th>候选 Recall@200</th></tr></thead><tbody>'
    for name, metrics in rows:
        body += f"<tr><td>{html.escape(name)}</td><td>{metrics['n']}</td>" + "".join(f"<td>{number(metrics[key])}</td>" for key in METRICS) + "</tr>"
    return body + "</tbody></table></div>"


def update_report(series, directory=Path("docs/experiments")):
    directory.mkdir(parents=True, exist_ok=True)
    figures = directory / "figures"
    figures.mkdir(exist_ok=True)
    published = dict(series)
    published["report_updated_at"] = datetime.now(timezone.utc).isoformat()
    published["report_renderer_sha256"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    write_atomic(directory / "training-results.json", json.dumps(published, ensure_ascii=False, indent=2) + "\n")
    baseline_rows = [(r["name"], r["validation"]["cohorts"]["all_positive_events"]) for r in series.get("baselines", [])]
    trial_rows = [(t["name"], t["best_validation"]["cohorts"]["all_positive_events"]) for t in series.get("trials", []) if t.get("best_validation")]
    rows = baseline_rows + trial_rows
    plt.rcParams.update({"font.size": 10, "axes.spines.top": False, "axes.spines.right": False})
    fig, axes = plt.subplots(1, 2, figsize=(12, 4.6))
    for axis, metric, title in zip(axes, METRICS[:2], ("Validation NDCG@10", "Validation Recall@20")):
        axis.barh(range(len(rows)), [m[metric] or 0 for _, m in rows], color=[COLORS[i % len(COLORS)] for i in range(len(rows))])
        axis.set_yticks(range(len(rows)), [name for name, _ in rows], fontsize=8)
        axis.set_xlim(left=0)
        axis.set_title(title)
        axis.grid(axis="x", alpha=.15)
    fig.suptitle("EvoRec | Same protocol, all positive validation events", fontsize=13)
    fig.tight_layout()
    save_figure(fig, figures / "validation-comparison.png")
    fig, axes = plt.subplots(1, 2, figsize=(12, 4.3))
    for trial in series.get("trials", []):
        history = trial.get("history", [])
        if history:
            axes[0].plot([r["epoch"] for r in history], [r["train_loss"] for r in history], marker=".", label=trial["name"])
            axes[1].plot([r["epoch"] for r in history], [r["validation_ndcg@10"] for r in history], marker=".", label=trial["name"])
    if baseline_rows:
        name, measured = max(baseline_rows, key=lambda r: r[1]["ndcg@10"])
        axes[1].axhline(measured["ndcg@10"], color="#2f8c66", linestyle="--", label=f"{name} baseline")
    axes[0].set_title("Training cross-entropy")
    axes[1].set_title("Validation NDCG@10 per epoch")
    for axis in axes:
        axis.set_xlabel("Epoch")
        axis.grid(alpha=.15)
        if axis.lines:
            axis.legend(fontsize=7)
    fig.tight_layout()
    save_figure(fig, figures / "learning-curves.png")

    insights = []
    summary_rows = []
    test_rows = [(r["name"], r["metrics"]["cohorts"]["all_positive_events"]) for r in series.get("test_results") or []]
    seed_summary = series.get("selected_neural_seed_summary")
    if series["status"] == "completed" and seed_summary:
        first_seed = series["configuration"]["seeds"][0]
        selection_rows = baseline_rows + [(t["name"], t["best_validation"]["cohorts"]["all_positive_events"])
                                         for t in series["trials"] if t["seed"] == first_seed]
        winner, _ = max(selection_rows, key=lambda r: r[1]["ndcg@10"])
        test_map = dict(test_rows)
        reference = test_map.get("Popular")
        winner_metrics = test_map[winner]
        insights.append(f"按预先指定的验证 NDCG@10 比较，当前首选方法为 {winner}；其测试 NDCG@10 为 {winner_metrics['ndcg@10']:.6f}。")
        if reference and reference["ndcg@10"]:
            delta = (winner_metrics["ndcg@10"] / reference["ndcg@10"] - 1) * 100
            neural_delta = (seed_summary["ndcg@10"]["mean"] / reference["ndcg@10"] - 1) * 100
            insights.append(f"相对同协议 Popular，首选方法的测试 NDCG@10 高 {delta:.1f}%；序列模型三种子均值高 {neural_delta:.1f}%。这是该样本的相对差值，尚未做统计显著性检验。")
        if winner_metrics["ndcg@10"] > seed_summary["ndcg@10"]["mean"]:
            insights.append("序列模型仍未超过首选基线。训练损失持续下降时，验证效果已出现停滞或退化，因此保留验证最佳检查点。")
        cohort = series["test_results"][0]["metrics"]["cohorts"]
        cold = cohort["model_cold_available"]["n"]
        insights.append(f"测试集 {series['test_queries']:,} 个正反馈事件中，{cold:,} 个目标属于训练交互未覆盖但当时可用的商品，所有当前方法在该组的 Recall@20 都为 0。下一轮需验证内容候选路径。"
                        if all(r["metrics"]["cohorts"]["model_cold_available"]["recall@20"] == 0 for r in series["test_results"])
                        else f"模型冷且可用分组含 {cold:,} 个事件，详见分组指标。")
        summary_rows = [(key, value["mean"], value["sample_std"]) for key, value in seed_summary.items()]

    test_chart = ""
    if test_rows:
        plot_rows = [(name, m, 0.0) for name, m in test_rows if not any(t["name"] == name for t in series.get("trials", []))]
        if seed_summary:
            plot_rows.append(("SASRec-CE (3-seed mean)", {k: seed_summary[k]["mean"] for k in METRICS}, seed_summary["ndcg@10"]["sample_std"]))
        else:
            plot_rows = [(name, m, 0.0) for name, m in test_rows]
        fig, axis = plt.subplots(figsize=(10, 4.3))
        axis.barh([name for name, _, _ in plot_rows], [m["ndcg@10"] for _, m, _ in plot_rows],
                  xerr=[error for _, _, error in plot_rows], color=[COLORS[i % len(COLORS)] for i in range(len(plot_rows))], capsize=4)
        axis.set_xlabel("Test NDCG@10")
        axis.set_xlim(left=0)
        axis.set_title("Frozen models | Neural error bar = sample SD across seeds")
        axis.grid(axis="x", alpha=.15)
        for index, (_, metrics, _) in enumerate(plot_rows):
            axis.annotate(f"{metrics['ndcg@10']:.6f}", (metrics["ndcg@10"], index), xytext=(8, 8), textcoords="offset points", fontsize=8)
        axis.margins(x=.16)
        fig.tight_layout()
        save_figure(fig, figures / "test-comparison.png")
        test_chart = "figures/test-comparison.png"

    phase_names = {"completed": "本轮训练已完成", "training": "训练中", "preparing": "准备数据与基线", "final_test": "冻结模型后评估", "failed": "本轮失败"}
    status = phase_names.get(series["status"], series["status"])
    intro = [
        f"更新：{published['report_updated_at']}｜状态：{status}｜协议：{series['protocol_id']}",
        "本页在每轮训练结束后自动更新；打开 HTML 后每 30 秒刷新。训练结束后保留最后结果，不会自动启动下一轮。",
        "仅比较相同样本、时间边界和评价口径；R01 前缀样本结果单独归档。",
        f"训练设备：{series.get('device', 'CPU')}；训练样本 {series.get('training_examples', 0):,}，验证事件 {series.get('validation_queries', 0):,}。模型选型和早停只读取验证集。",
    ]
    limits = [
        "完整类别首次交互严格早于请求，作为商品可用性代理；它不是实际商品上架时间。",
        "评论评分作为隐式兴趣代理；本轮不证明商业 CTR、线上延迟或生成式推荐效果。",
        "SASRec-CE 使用全词表交叉熵、训练频率偏置和本项目 Transformer 配置，不宣称严格复现原论文。",
        "三种子均值来自三个独立模型的指标，未做预测集成；样本标准差不是置信区间。",
        "GPU 分批评估未提供单请求延迟分位数；显存指标是训练进程截至该点的累计峰值。",
        "研究环境复用了系统包；依赖快照与安装边界见协议文档。本轮测试已查看，后续调参需预注册新的时间窗口或留出协议。",
    ]
    markdown = ["# EvoRec 持续实验报告", ""]
    for paragraph in intro:
        markdown += [paragraph, ""]
    if insights:
        markdown += ["## 本轮发现", ""]
        for paragraph in insights:
            markdown += [paragraph, ""]
    markdown += ["## 验证集比较", "", *md_table(rows, validation=True), "", "![验证集比较](figures/validation-comparison.png)", "",
                 "## 训练过程", "", "![训练损失与验证曲线](figures/learning-curves.png)", "",
                 "| 训练 | 已完成轮数 | 最佳轮次 | 训练、评估和报告总耗时（秒） |", "| --- | ---: | ---: | ---: |"]
    for trial in series.get("trials", []):
        markdown.append(f"| {trial['name']} | {len(trial.get('history', []))} | {trial.get('best_epoch', '—')} | {trial.get('training_wall_seconds', 0):.2f} |")
    markdown += ["", "训练损失下降不等于推荐效果提高。按验证 NDCG@10 保存最佳检查点，保留退化和未超过基线的实验。", ""]
    if test_rows:
        markdown += ["## 冻结模型后的测试结果", "", *md_table(test_rows), "", f"![测试集比较]({test_chart})", ""]
    if summary_rows:
        markdown += ["### 序列模型三种子汇总", "", "| 指标 | 均值 | 样本标准差 |", "| --- | ---: | ---: |"]
        markdown += [f"| {key} | {mean:.6f} | {std:.6f} |" for key, mean, std in summary_rows]
        markdown += ["", "种子：17、29、43。先在种子 17 比较两组学习率，再重复所选配置；三个种子分别用验证集选择最佳轮次。", ""]
        cohort = series["test_results"][0]["metrics"]["cohorts"]
        markdown += ["### 分组有效样本", "", "| 分组 | 测试事件数 |", "| --- | ---: |"]
        labels = {"all_positive_events": "全部正反馈", "available_target": "目标当时可用", "history_present": "有正反馈历史", "cold_user": "无正反馈历史", "model_cold_available": "模型冷且可用"}
        markdown += [f"| {label} | {cohort[key]['n']} |" for key, label in labels.items()]
        markdown += ["", "上述分组存在交叉，不能相加；无历史与模型冷商品描述不同维度。各方法分组指标保存在结果 JSON。", ""]
    markdown += ["## 证据与边界", "", "- [机器可读结果](training-results.json)。",
                 "- [本轮协议与环境说明](r02-protocol.md)。",
                 "- [独立轨迹审计](../validation/training-checks.json)。"]
    markdown += [f"- {item}" for item in limits]
    markdown += [""]
    write_atomic(directory / "training-report.md", "\n".join(markdown))
    body = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="30"><title>EvoRec 持续实验报告</title>
<style>body{font-family:system-ui,'Microsoft YaHei',sans-serif;max-width:1100px;margin:40px auto;padding:0 24px;color:#183247;background:#f7f9fb;line-height:1.75}h1{line-height:1.3}h2{margin-top:36px}img{display:block;width:100%;height:auto;background:white;border-radius:12px;margin:16px 0}.table-wrap{overflow:auto}table{border-collapse:collapse;width:100%;background:white;font-variant-numeric:tabular-nums}td,th{padding:10px;border-bottom:1px solid #ddd;text-align:left;white-space:nowrap}.findings{background:#eaf4ee;padding:18px 24px;border-radius:12px}small{color:#526777}a{color:#1464a0}.tag{display:inline-block;background:#183247;color:white;border-radius:6px;padding:3px 12px}li{margin:10px 0}@media(max-width:600px){body{padding:0 14px;margin:24px auto}td,th{padding:8px;font-size:13px}}</style></head><body>
<h1>EvoRec 持续实验报告</h1>"""
    body += f'<span class="tag">{html.escape(status)}</span>'
    body += "".join(f"<p>{html.escape(paragraph)}</p>" for paragraph in intro)
    if insights:
        body += '<section class="findings"><h2>本轮发现</h2>' + "".join(f"<p>{html.escape(p)}</p>" for p in insights) + "</section>"
    body += '<h2>验证集比较</h2>' + html_table(rows) + '<img src="figures/validation-comparison.png" alt="相同协议下各方法的验证指标比较">'
    body += '<h2>训练过程</h2><p>实线为各训练的逐轮指标；虚线为最佳验证基线。训练损失下降不等于效果提高。</p><img src="figures/learning-curves.png" alt="训练交叉熵与验证 NDCG 曲线">'
    if test_rows:
        body += '<h2>冻结模型后的测试结果</h2>' + html_table(test_rows) + f'<img src="{test_chart}" alt="测试指标对比，序列模型误差线为三种子样本标准差">'
    if summary_rows:
        body += '<h2>序列模型三种子汇总</h2><div class="table-wrap"><table><tr><th>指标</th><th>均值</th><th>样本标准差</th></tr>'
        body += "".join(f"<tr><td>{html.escape(key)}</td><td>{mean:.6f}</td><td>{std:.6f}</td></tr>" for key, mean, std in summary_rows)
        body += '</table></div><p>种子 17、29、43；均值不是集成模型效果，标准差不是置信区间。</p>'
    body += "<h2>证据与边界</h2><ul>" + "".join(f"<li>{html.escape(p)}</li>" for p in limits) + "</ul>"
    body += '<p><a href="training-report.md">完整文字报告</a> · <a href="training-results.json">结果数据</a> · <a href="r02-protocol.md">实验协议</a> · <a href="../validation/training-checks.json">独立审计</a></p></body></html>'
    write_atomic(directory / "training-report.html", body)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--series", type=Path, required=True)
    parser.add_argument("--directory", type=Path, default=Path("docs/experiments"))
    args = parser.parse_args()
    update_report(json.loads(args.series.read_text()), args.directory)


if __name__ == "__main__":
    main()
