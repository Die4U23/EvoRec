"""Live, evidence-bounded reports for R05 listwise ranking."""
import html
import json
from pathlib import Path

from evorec.research.reporting import plt,save_figure,write_atomic
from evorec.research.runner import file_sha


def report(series):
    out=Path(series["configuration"]["report_directory"])
    (out/"figures").mkdir(parents=True,exist_ok=True)
    published={**series,"report_renderer_sha256":file_sha(Path(__file__))}
    write_atomic(out/"results.json",json.dumps(published,ensure_ascii=False,indent=2)+"\n")
    trials=series.get("trials",[])
    fig,axes=plt.subplots(1,3,figsize=(15,4.3))
    for trial in trials:
        history=trial["history"]
        for ax,key in zip(axes,("train_loss","validation_ndcg@10","validation_cold_recall@20")):
            if history:
                ax.plot([r["epoch"] for r in history],[r[key] for r in history],marker=".",label=trial["name"])
    for ax,title in zip(axes,("Weighted listwise training loss","Validation NDCG@10","Validation cold Recall@20")):
        ax.set_title(title); ax.set_xlabel("Epoch"); ax.grid(alpha=.15)
        if ax.lines: ax.legend(fontsize=7)
    fig.tight_layout()
    save_figure(fig,out/"figures/learning-curves.png")
    notes=[
        "R05 使用新的留出用户；训练只使用 R03 训练期内的时间滚动样本。标题与类别仍采用静态快照可用假设。",
        "TF-IDF / SVD 仅拟合 2017 年以前训练商品；滚动窗口的协同与热门统计仅使用各窗口开始前的交互。",
        "学习方法与 RRF 基线共享最多 400 个合法候选，输出 Top200。候选未命中的训练目标不会被插入池中；所有评价正反馈保留在分母。",
        "普通训练与冷样本加权的损失权重不同，两条训练损失曲线的绝对值不宜直接比较。",
        "每个模型按整体验证 NDCG 选最佳轮次；最终策略另按预登记的整体质量下限与冷商品 Recall 选择。神经模型三种子研究设置不等于默认策略。",
        "无可表示历史时保持原融合；不使用测试标签参与特征构造。未观测候选作为对比负例，不代表明确负反馈。",
    ]
    if series.get("selected_method"):
        notes.append(f"验证集最终选择：{series['selected_method']}。三种子训练的神经设置：{series['selected_setting']['name']}。")
    if series["status"]=="completed":
        lookup={r["name"]:r for r in series["test_results"]}
        chosen=lookup[series["selected_method"]]
        reference=lookup["RRF"]
        a=chosen["metrics"]["cohorts"]["all_positive_events"]
        b=reference["metrics"]["cohorts"]["all_positive_events"]
        cold=chosen["metrics"]["cohorts"]["model_cold_available"]
        base_cold=reference["metrics"]["cohorts"]["model_cold_available"]
        notes.append(f"在同一候选池和同一测试用户上，验证选中方法的 NDCG@10={a['ndcg@10']:.6f}，"
                     f"相对 RRF 的观测差值为 {(a['ndcg@10']/b['ndcg@10']-1)*100:+.1f}%；"
                     f"冷目标 Top20 命中从 {round(base_cold['recall@20']*base_cold['n'])} 增至 "
                     f"{round(cold['recall@20']*cold['n'])} / {cold['n']}。")
        family=chosen.get("family")
        members=[r for r in series["test_results"] if r.get("family")==family and "seed" in r]
        if len(members)==1:
            notes.append(f"最终选中的 {chosen['name']} 只有一个种子，不能套用另一训练设置的三种子标准差。"
                         "现有结果还不能证明该方法的跨种子稳定收益或统计显著性。")
    def table(rows):
        lines=["| 方法 | NDCG@10 | Recall@20 | 冷 Recall@20 | 冷命中 / 冷目标 |","| --- | ---: | ---: | ---: | ---: |"]
        for r in rows:
            a=r["metrics"]["cohorts"]["all_positive_events"]; c=r["metrics"]["cohorts"]["model_cold_available"]
            lines.append(f"| {r['name']} | {a['ndcg@10']:.6f} | {a['recall@20']:.6f} | {c['recall@20']:.6f} | {round(c['recall@20']*c['n'])} / {c['n']} |")
        return lines
    validation=list(series.get("validation_results",[]))
    for t in trials:
        if t.get("best_validation"):
            validation.append({"name":t["name"],"metrics":t["best_validation"]})
    lines=["# EvoRec R05：模拟冷商品训练与神经候选排序","",
           f"状态：{series['status']}｜协议：{series['protocol_id']}",""]
    for p in notes: lines += [p,""]
    if series.get("training_folds"):
        lines += ["## 实际训练样本","","| 时间窗口 | 采样请求 | 有效排序样例 | 模拟冷样例 | 候选未命中 |",
                  "| --- | ---: | ---: | ---: | ---: |"]
        for r in series["training_folds"]:
            lines += [f"| {r['name']} | {r['sampled_queries']} | {r['training_examples']} | {r['cold_training_examples']} | {r['skipped_candidate_miss']} |"]
        lines += ["","候选未命中只影响排序损失的可构造性，不从验证 / 测试的评价分母中删去。",""]
    lines += ["## 学习曲线","","![R05 学习曲线](figures/learning-curves.png)","",
              "## 验证结果","",*table(validation),""]
    test=series.get("test_results",[])
    if test:
        lines += ["## 冻结后的测试","",*table(test),""]
        fig,axes=plt.subplots(1,2,figsize=(13,max(4.2,len(test)*.44)))
        for ax,group,key,title,color in zip(axes,("all_positive_events","model_cold_available"),
                                            ("ndcg@10","recall@20"),("NDCG@10","Cold available Recall@20"),
                                            ("#286e99","#438b68")):
            values=[r["metrics"]["cohorts"][group][key] for r in test]
            bars=ax.barh([r["name"] for r in test],values,color=color)
            ax.bar_label(bars,fmt="%.6f",padding=3,fontsize=8)
            ax.set_xlim(0,max(values)*1.28 if max(values) else 1)
            ax.set_title(title); ax.grid(axis="x",alpha=.15)
        fig.suptitle("R05 test | Same users; R03 reference has different retrieval")
        fig.tight_layout(); save_figure(fig,out/"figures/test-comparison.png")
        lines += ["![R05 测试比较](figures/test-comparison.png)","",
                  "R03 冻结参考使用不同编码器与候选链路，不能当作只改变排序器的消融。",""]
    if series.get("seed_summary"):
        lines += ["## 所选神经设置的三种子统计","","| 指标 | 均值 | 样本标准差 |","| --- | ---: | ---: |"]
        for key,value in series["seed_summary"].items():
            lines += [f"| {key} | {value['mean']:.6f} | {value['sample_std']:.6f} |"]
        lines += ["","标准差来自种子 17 / 29 / 43，不是置信区间或集成结果；本轮没有显著性检验。",""]
    if series.get("test_pool_diagnostics"):
        d=series["test_pool_diagnostics"]
        lines += ["## 候选可达性","",
                  f"测试 {d['queries']:,} 个请求中，{d['target_in_pool']:,} 个目标进入共享候选池；"
                  f"{d['cold_available_targets']:,} 个模型冷且可用目标中，{d['cold_targets_in_pool']:,} 个进入池。"
                  "这是候选可达性上界，不是模型已经达到的 Top20 效果。",""]
    lines += ["## 证据","","- [配置、训练、检查点和轨迹索引](results.json)",
              "- [预先登记的协议](../r05-ranker-protocol.md)",
              "- [独立审计](../../validation/ranker-checks.json)",
              "- [模型原理与检查点用法](../../../research/R05-ranker-guide.md)","",
              "完成的模型、编码器、训练缓存、源代码快照与请求轨迹位于本地运行目录。在线服务和跨机器复现仍需另行验证。",""]
    write_atomic(out/"report.md","\n".join(lines))
    refresh='<meta http-equiv="refresh" content="30">' if series["status"] not in ("completed","failed") else ""
    body=f'<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">{refresh}<title>EvoRec R05 排序训练</title><style>body{{font:16px/1.8 system-ui,"Microsoft YaHei",sans-serif;max-width:1150px;margin:32px auto;padding:0 20px;color:#203d50;background:#f5f7fa}}img{{width:100%;height:auto}}section{{background:#e9f0f5;padding:16px 22px;border-radius:12px}}</style></head><body><h1>EvoRec R05：神经候选排序</h1>'
    body+=f"<p>状态：{html.escape(series['status'])} · 协议：{series['protocol_id']}</p><section>"
    body+="".join(f"<p>{html.escape(p)}</p>" for p in notes)+"</section>"
    body+='<h2>训练与验证曲线</h2><img src="figures/learning-curves.png" alt="R05 训练与验证曲线">'
    if test: body+='<h2>冻结后的测试结果</h2><img src="figures/test-comparison.png" alt="R05 测试比较">'
    body+='<p><a href="report.md">完整结果表</a> · <a href="results.json">原始结果</a> · <a href="../r05-ranker-protocol.md">登记协议</a></p></body></html>'
    write_atomic(out/"report.html",body)

if __name__=="__main__":
    import argparse
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run",type=Path,required=True)
    args=parser.parse_args()
    report(json.loads((args.run/"series.json").read_text(encoding="utf-8")))
