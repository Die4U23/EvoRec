"""Build local, versioned blog illustrations and copy verified experiment figures."""
import hashlib
import html
import json
import shutil
from pathlib import Path

from matplotlib.font_manager import FontProperties
from matplotlib.patches import FancyBboxPatch, FancyArrowPatch
from PIL import Image

from evorec.research.reporting import plt, save_figure

DEST = Path("docs/blog/assets/evorec")
FONT = FontProperties(fname="C:/Windows/Fonts/msyh.ttc")
BLUE, GREEN, INK, GRAY = "#e6f0f7", "#e8f2e9", "#203d50", "#657684"


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def label(ax, x, y, text, size=12, **kwargs):
    ax.text(x, y, text, fontproperties=FONT, fontsize=size, color=INK,
            ha="center", va="center", linespacing=1.7, **kwargs)


def box(ax, x, y, w, h, text, color=BLUE, size=12):
    ax.add_patch(FancyBboxPatch((x,y), w,h, boxstyle="round,pad=0.03,rounding_size=.14",
                               linewidth=1.1, edgecolor="#b5c7d2", facecolor=color))
    label(ax, x+w/2,y+h/2,text,size)


def arrow(ax, start, end):
    ax.add_patch(FancyArrowPatch(start,end,arrowstyle="-|>",mutation_scale=15,color=GRAY,linewidth=1.4))


def canvas(title, subtitle, height=8.6):
    fig, ax = plt.subplots(figsize=(14,height))
    ax.set_xlim(0,14); ax.set_ylim(0,height); ax.axis("off")
    label(ax,7,height-.5,title,21,fontweight="bold")
    label(ax,7,height-1.06,subtitle,11)
    return fig,ax


def build():
    DEST.mkdir(parents=True, exist_ok=True)
    plt.rcParams["svg.fonttype"] = "path"
    assets = []
    def add(name, title, caption, source, kind):
        for suffix in (".png",".svg"):
            target = DEST/(name+suffix)
            item = {"file": target.name, "title": title, "alt": title, "caption": caption,
                    "kind": kind, "source": source, "sha256": sha(target)}
            if suffix == ".png":
                with Image.open(target) as im:
                    im.verify()
                with Image.open(target) as im:
                    item["width"],item["height"] = im.size
            assets.append(item)
    fig,ax = canvas("EvoRec 内容双塔：表示、学习与检索",
                    "已实现的离线链路｜文本编码只在 R03 训练商品拟合；R04 冻结编码器与双塔")
    box(ax,.5,5.25,3.6,1.2,"请求前的正反馈历史\n最多 50 件商品")
    box(ax,5.0,5.25,3.6,1.2,"冻结内容向量\n按 0.8 衰减加权、归一化")
    box(ax,9.5,5.25,3.9,1.2,"用户塔：残差 MLP\n128 维 → 128 维")
    box(ax,.5,3.3,3.6,1.2,"候选标题与类别\n137,249 件商品的静态快照")
    box(ax,5.0,3.3,3.6,1.2,"TF-IDF → SVD → 归一化\n20,000 词特征 → 128 维")
    box(ax,9.5,3.3,3.9,1.2,"商品塔：残差 MLP\n所有候选共用参数")
    for y in (5.85,3.9):
        arrow(ax,(4.15,y),(4.92,y)); arrow(ax,(8.65,y),(9.42,y))
    arrow(ax,(6.8,4.55),(6.8,5.17))
    box(ax,6.2,1.12,7.2,1.22,"归一化向量点积 → 合法性过滤 → Top200\n历史不可表示时回退近期热门；无向量候选跳过",GREEN,12)
    arrow(ax,(13.7,5.85),(13.7,1.72)); arrow(ax,(13.7,1.72),(13.44,1.72))
    arrow(ax,(11.4,3.24),(11.4,2.4))
    box(ax,.5,1.12,4.95,1.22,"训练：仅 R03 训练正反馈商品集合的 Softmax\n在线推荐接口与发布链路仍待接入","#f7eddc",11)
    label(ax,7,.36,"边界：静态内容缺少历史版本；这张图不代表线上系统已完成。",11)
    save_figure(fig,DEST/"content-tower-architecture.png")
    add("content-tower-architecture","内容双塔结构与实现边界",
        "图中为已运行的离线结构；R03 训练，R04 冻结复用。在线业务接入仍是计划。",
        "src/evorec/research/content.py","architecture")

    fig,ax = canvas("R04 冷商品保留：规则如何做决定",
                    "同一组请求、固定候选与模型｜选择只看验证集，测试用于最终评价",8.8)
    box(ax,.5,5.6,3.5,1.28,"请求前的用户历史\n可表示数量 + 内容一致性",BLUE,12)
    box(ax,5.1,5.6,3.8,1.28,"规则门槛（仅 gated 策略）\n数量 ≥ 2 且一致性 ≥ 0.6",GREEN,12)
    box(ax,10.0,5.6,3.5,1.28,"未通过 → 保持原列表\n不读取目标商品与命中标签","#f7eddc",11)
    arrow(ax,(4.06,6.24),(5.03,6.24)); arrow(ax,(8.96,6.24),(9.93,6.24))
    box(ax,.5,3.1,3.5,1.45,"固定双塔 + 协同 RRF\n内容权重 0.5\n作为原始 Top200")
    box(ax,5.1,3.1,3.8,1.45,"固定 Content-SVD Top200\n按原顺序取模型冷商品\n冷：不在 R03 训练交互中")
    box(ax,10.0,3.1,3.5,1.45,"通过后检查 Top20\n已达保留数 → 不修改\n不足 → 保护旧冷商品再补入",GREEN,11)
    arrow(ax,(7,5.54),(7,5.0))
    arrow(ax,(7,5.0),(11.75,5.0)); arrow(ax,(11.75,5.0),(11.75,4.62))
    arrow(ax,(8.96,3.82),(9.93,3.82))
    box(ax,5.1,1.05,8.4,1.12,"保留 1 个：第 20 位；保留 2 个：第 10、20 位\n最终最多 200 个，无重复；仍遵守时间可用与已见过滤",GREEN,12)
    arrow(ax,(11.75,3.04),(11.75,2.24))
    arrow(ax,(2.25,3.04),(2.25,1.61)); arrow(ax,(2.25,1.61),(5.03,1.61))
    label(ax,7,.35,"两个检索路径仍全部计算。本轮检验排序策略，不证明计算预算或在线延迟改善。",11)
    save_figure(fig,DEST/"cold-reservation-flow.png")
    add("cold-reservation-flow","冷商品保留与门控流程",
        "五种策略在实验前固定。门控只读取历史信号；未跳过任一检索路径。",
        "src/evorec/research/gating.py","mechanism")
    sources = [
        ("r04-error-localization", "R04 冷商品落选位置分析", "测试后诊断：比较同一批冷目标的命中位置；排序集合可能交叉，不是严格漏斗。",
         "docs/experiments/r04-gating/figures/error-localization", "docs/experiments/r04-gating/error-analysis.json"),
        ("r02-learning-curves", "R02 序列模型训练曲线", "R02：39 轮、4 次训练。损失下降不保证验证指标持续改善。",
         "docs/experiments/figures/learning-curves", "docs/experiments/training-results.json"),
        ("r02-test-comparison", "R02 序列模型与统计基线", "R02 独立样本。三种子标准差不是置信区间；序列模型未超过强协同基线。",
         "docs/experiments/figures/test-comparison", "docs/experiments/training-results.json"),
        ("r03-learning-curves", "R03 内容双塔训练曲线", "R03：48 轮、4 次训练。按验证指标保留检查点；静态内容可用假设。",
         "docs/experiments/r03-content/figures/learning-curves", "docs/experiments/r03-content/results.json"),
        ("r03-test-comparison", "R03 整体排序与冷商品召回", "R03 独立样本。内容融合改善整体排序，但冷商品命中少于纯内容路径。",
         "docs/experiments/r03-content/figures/test", "docs/experiments/r03-content/results.json"),
        ("r04-validation", "R04 验证集策略比较", "R04 冻结 R03 模型，使用新用户验证选择策略；红线为整体 NDCG 下限。",
         "docs/experiments/r04-gating/figures/validation", "docs/experiments/r04-gating/results.json"),
        ("r04-test-ablation", "R04 测试集消融", "固定 seed 17 的五种预登记策略；测试中的差异不用于回改选型。",
         "docs/experiments/r04-gating/figures/test-ablation", "docs/experiments/r04-gating/results.json"),
        ("r04-gate-activity", "R04 门控活动与冷商品曝光", "改变列表的请求比例和新增冷商品位置数量，与目标命中率分别评价。",
         "docs/experiments/r04-gating/figures/gate-activity", "docs/experiments/r04-gating/results.json"),
    ]
    for name,title,caption,source,result in sources:
        for suffix in (".png",".svg"):
            shutil.copyfile(Path(source+suffix),DEST/(name+suffix))
        add(name,title,caption,result,"measured")
        for asset in assets[-2:]:
            asset["source_sha256"] = sha(Path(result))
            asset["source_image_sha256"] = sha(Path(source+Path(asset["file"]).suffix))
            asset["protocol_id"] = json.loads(Path(result).read_text(encoding="utf-8"))["protocol_id"]
    manifest = {"schema_version": 1, "generator": "scripts/build_blog_assets.py", "generator_sha256": sha(Path(__file__)),
                "limits": ["R02/R03/R04 have different query users; compare methods only within a stage",
                           "sample standard deviation is not a confidence interval",
                           "architecture diagrams describe offline implementation, not deployed functionality"],
                "assets": assets}
    (DEST/"manifest.json").write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    lines = ["# EvoRec 博客图片资源", "", "10 张图，每张同时提供 PNG 与可缩放 SVG。PNG 用于文章嵌图，SVG 用于放大与排版。", "",
             "结果图直接复制各阶段程序生成的图表，来源 JSON、图片指纹和协议编号记录在 [清单](manifest.json)。两张机制图由项目代码生成，不包含模拟结果。", "",
             "[离线图库](index.html) · [项目实录](../../evorec-project-log.md)", "",
             "| 图片 | 用途与说明 | 格式 |", "| --- | --- | --- |"]
    for item in assets[::2]:
        stem = Path(item["file"]).stem
        lines += [f"| {item['title']} | {item['caption']} | [PNG]({stem}.png) / [SVG]({stem}.svg) |"]
    lines += ["", "复建：在项目根目录、已完成三轮结果报告后，使用研究环境运行 scripts/build_blog_assets.py。", "",
              "R02/R03/R04 查询用户不同，不将跨轮绝对分数变化标为算法提升。R03/R04 依赖静态内容可用假设。", ""]
    (DEST/"README.md").write_text("\n".join(lines),encoding="utf-8")
    body = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>EvoRec 项目图册</title><style>body{max-width:1120px;margin:40px auto;padding:0 20px;font:16px/1.8 system-ui,'Microsoft YaHei',sans-serif;color:#203d50;background:#f5f7fa}figure{background:white;padding:20px;margin:24px 0;border-radius:14px}img{width:100%;height:auto}figcaption{margin-top:12px}a{color:#17658c}</style></head><body><h1>EvoRec 项目图册</h1><p>10 张可追溯配图：模型结构、实验过程与策略取舍。数据来自本地已完成的实验；不同阶段的用户样本不同。</p>"""
    for item in assets[::2]:
        stem = Path(item["file"]).stem
        body += f'<figure><h2>{html.escape(item["title"])}</h2><img src="{item["file"]}" alt="{html.escape(item["alt"])}" loading="lazy"><figcaption>{html.escape(item["caption"])} <a href="{stem}.svg">SVG 矢量图</a></figcaption></figure>'
    body += '<p><a href="manifest.json">来源清单</a> · <a href="../../evorec-project-log.md">项目实录</a></p></body></html>'
    (DEST/"index.html").write_text(body,encoding="utf-8")
    print(json.dumps({"illustrations":len(assets)//2,"files":len(assets),"gallery":str(DEST/"index.html")}))


if __name__ == "__main__":
    build()
