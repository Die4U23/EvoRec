"""Post-hoc R04 error localization; never used to reselect a policy."""
import gzip
import json
from collections import Counter
from contextlib import ExitStack
from pathlib import Path

from evorec.research.reporting import plt, save_figure
from evorec.research.runner import file_sha

RUN = Path("artifacts/runs/r04-gating-20260915")
OUT = Path("docs/experiments/r04-gating")


def analyze():
    series = json.loads((RUN/"series.json").read_text(encoding="utf-8"))
    assert series["status"] == "completed"
    lookup = {r["name"]:r for r in series["test_results"]}
    names = ("Content-SVD","fixed-s17","reserve-2-s17","gated-2-s17")
    counts, losses, gate_groups = Counter(), Counter(), {"eligible":Counter(), "ineligible":Counter()}
    with ExitStack() as stack:
        streams = [stack.enter_context(gzip.open(lookup[name]["trace"]["path_from_project_root"],"rt",encoding="utf-8")) for name in names]
        gates = stack.enter_context(gzip.open(lookup["gated-2-s17"]["gate_trace"]["path_from_project_root"],"rt",encoding="utf-8"))
        for lines in zip(*streams, gates, strict=True):
            rows = [json.loads(line) for line in lines]
            raw, fixed, reserved, gated, signal = rows
            assert len({r["query_id"] for r in rows}) == 1
            if not raw["target_available"] or not raw["target_model_cold"]:
                continue
            target = raw["target"]
            raw_rank = raw["recommendations"].index(target)+1 if target in raw["recommendations"] else 201
            group = gate_groups["eligible" if signal["gate_passed"] else "ineligible"]
            counts["cold_targets"] += 1
            group["cold_targets"] += 1
            counts["has_history"] += bool(raw["history"])
            counts["has_represented_history"] += signal["represented_history"] > 0
            counts["gate_eligible"] += signal["gate_passed"]
            raw_hit = raw_rank <= 20
            fixed_hit = target in fixed["recommendations"][:20]
            counts["raw_top200"] += raw_rank <= 200
            counts["raw_top20"] += raw_hit
            for name, row in (("fixed",fixed),("reserve2",reserved),("gated2",gated)):
                hit = target in row["recommendations"][:20]
                counts[name+"_top20"] += hit
                group[name+"_top20"] += hit
                if name != "fixed":
                    counts[name+"_gained_vs_fixed"] += hit and not fixed_hit
                    counts[name+"_lost_vs_fixed"] += fixed_hit and not hit
            group["raw_top20"] += raw_hit
            if raw_rank > 200:
                losses["outside_raw_top200"] += 1
            elif raw_rank > 20:
                losses["raw_rank_21_to_200"] += 1
            elif not fixed_hit:
                losses["raw_top20_but_fixed_miss"] += 1
            else:
                losses["raw_and_fixed_top20"] += 1
    assert sum(losses.values()) == counts["cold_targets"]
    for name,result_name in (("raw","Content-SVD"),("fixed","fixed-s17"),("reserve2","reserve-2-s17"),("gated2","gated-2-s17")):
        cohort = lookup[result_name]["metrics"]["cohorts"]["model_cold_available"]
        assert counts[name+"_top20"] == round(cohort["recall@20"]*cohort["n"])
    result = {
        "status":"completed_posthoc_diagnosis", "protocol_id":series["protocol_id"],
        "series_sha256":file_sha(RUN/"series.json"), "script_sha256":file_sha(Path(__file__)),
        "scope":"R04 test; frozen seed 17; descriptive analysis after registered selection",
        "counts":dict(counts),"mutually_exclusive_raw_rank_groups":dict(losses),
        "gate_groups":{name:dict(values) for name,values in gate_groups.items()},
        "source_traces":{name:lookup[name]["trace"] for name in names},
        "limits":["labels used for diagnosis only; cannot feed test labels to a gate",
                  "test-aware diagnosis is not a new held-out experiment or causal proof",
                  "rank groups follow raw Content-SVD, not a strict multi-stage funnel"],
    }
    (OUT/"error-analysis.json").write_text(json.dumps(result,indent=2)+"\n",encoding="utf-8")
    labels=["Raw Top200","Raw Top20","Fixed RRF Top20","Reserve-2 Top20","Gated-2 Top20"]
    values=[counts[k] for k in ("raw_top200","raw_top20","fixed_top20","reserve2_top20","gated2_top20")]
    fig,ax=plt.subplots(figsize=(11,4.8))
    bars=ax.barh(labels,values,color=["#a1bace","#6598bc","#226e9b","#ce9750","#81aa8c"])
    ax.bar_label(bars,padding=4)
    ax.invert_yaxis()
    ax.set_xlim(0,max(values)*1.2)
    ax.set_xlabel(f"Cold available target hits (same {counts['cold_targets']:,} requests)")
    ax.set_title("R04 post-hoc diagnosis | seed 17 | ranking sets can overlap")
    ax.grid(axis="x",alpha=.15)
    fig.tight_layout()
    save_figure(fig,OUT/"figures/error-localization.png")
    paragraphs = [
        "# R04：冷商品落选位置分析", "",
        "这是正式选型完成后的描述性分析，允许读取目标标签来解释失败；不据此回改 R04 门槛，也不把它当作新的未见测试。", "",
        f"本轮 seed 17 有 {counts['cold_targets']:,} 个模型冷且可用目标；"
        f"{counts['has_history']:,} 个请求有历史，{counts['has_represented_history']:,} 个至少有一个可表示历史，"
        f"{counts['gate_eligible']:,} 个通过门控条件。", "",
        "| 同一批冷目标中的命中位置 | 命中数 |", "| --- | ---: |",
    ]
    paragraphs += [f"| {label} | {value} |" for label,value in zip(labels,values,strict=True)]
    paragraphs += ["", "![冷商品落选位置](figures/error-localization.png)", "",
        f"固定内容 Top20 中的 {counts['raw_top20']} 个目标命中，有 {losses['raw_top20_but_fixed_miss']} 个未保留在固定融合 Top20。"
        f"与此同时，{losses['outside_raw_top200']:,} 个目标连固定内容 Top200 也没有进入。候选表示与最终排序都存在瓶颈。", "",
        f"无门槛 reserve-2 相对固定融合新增 {counts['reserve2_gained_vs_fixed']} 个命中、丢失 {counts['reserve2_lost_vs_fixed']} 个；"
        f"gated-2 新增 {counts['gated2_gained_vs_fixed']} 个、丢失 {counts['gated2_lost_vs_fixed']} 个。"
        "增加冷商品位置只保证曝光数量，不保证被放入的就是相关商品。", "",
        f"其中 {counts['cold_targets']-counts['has_history']:,} 个冷目标请求没有历史，当前内容路径会回退到仅含训练商品的近期热门。"
        "因此不能把所有候选缺失都归因为文本表示差；后续需分别报告有历史和冷用户分组，避免将信息不足混入模型能力判断。", "",
        "下一项研究应分别评价内容候选质量与候选内排序。优先在训练内部构造按时间滚动的模拟冷商品任务，"
        "让排序模型学习用户—候选的相对相关性；验证阶段同时约束整体质量和冷商品效果。"
        "固定候选、无学习融合、无冷商品训练目标应作为消融。所有模型选择都需要新的未使用用户分组。", "",
        "这是一项由失败分析形成的待检验假设，尚未实现新排序模型，也没有新的提升结论。", "",
        "[原始统计及轨迹指纹](error-analysis.json) · [正式 R04 报告](report.md)", "",
    ]
    (OUT/"error-analysis.md").write_text("\n".join(paragraphs),encoding="utf-8")
    print(json.dumps(result["counts"]))


if __name__ == "__main__":
    analyze()
