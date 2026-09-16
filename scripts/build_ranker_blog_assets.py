"""Copy R05 measured charts into a separately versioned blog asset directory."""
import html
import json
import shutil
from pathlib import Path
from PIL import Image

from evorec.research.runner import file_sha


def build():
    source=Path("docs/experiments/r05-ranker")
    out=Path("docs/blog/assets/evorec/r05"); out.mkdir(parents=True,exist_ok=True)
    results=json.loads((source/"results.json").read_text(encoding="utf-8"))
    if results["status"]!="completed": raise ValueError("training is not completed")
    specs=[
        ("learning-curves","R05 新排序模型训练曲线","4 次训练、35 轮。普通损失与冷样本加权损失的绝对值不直接比较；最佳轮次只看验证集。"),
        ("test-comparison","R05 同候选池排序比较","ColdListMLP 只运行 seed 17；普通 ListMLP 有三个种子。R03 参考使用不同编码器与召回链路。"),
    ]
    assets=[]
    for name,title,caption in specs:
        for suffix in (".png",".svg"):
            src=source/"figures"/(name+suffix); dst=out/src.name
            shutil.copyfile(src,dst)
            row={"file":dst.name,"title":title,"caption":caption,"sha256":file_sha(dst),
                 "source_image":src.as_posix(),"source_results":(source/"results.json").as_posix(),
                 "source_results_sha256":file_sha(source/"results.json"),"protocol_id":results["protocol_id"]}
            if suffix==".png":
                with Image.open(dst) as im: row.update({"width":im.width,"height":im.height})
            assets.append(row)
    manifest={"generator":"scripts/build_ranker_blog_assets.py","generator_sha256":file_sha(Path(__file__)),
              "protocol_id":results["protocol_id"],"assets":assets}
    (out/"manifest.json").write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    md=["# R05 新排序模型配图","","图片直接来自已完成报告，PNG 用于嵌入文章，SVG 用于放大。","",
        "[来源清单](manifest.json) · [实验报告](../../../../experiments/r05-ranker/report.md) · [项目实录](../../../evorec-project-log.md)",""]
    for name,title,caption in specs:
        md += [f"## {title}","",caption,"",f"![{title}]({name}.png)","",f"[SVG]({name}.svg)",""]
    (out/"README.md").write_text("\n".join(md),encoding="utf-8")
    body='<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>EvoRec R05 配图</title><style>body{max-width:1150px;margin:32px auto;padding:0 20px;font:16px/1.8 system-ui,"Microsoft YaHei",sans-serif;color:#203d50}img{width:100%;height:auto}figure{margin:24px 0}</style></head><body><h1>R05 新排序模型：实际训练与测试</h1>'
    for name,title,caption in specs:
        body+=f'<figure><h2>{html.escape(title)}</h2><img src="{name}.png" alt="{html.escape(title)}"><figcaption>{html.escape(caption)}</figcaption></figure>'
    body+='<p><a href="manifest.json">图片来源</a> · <a href="../../../../experiments/r05-ranker/report.html">完整报告</a></p></body></html>'
    (out/"index.html").write_text(body,encoding="utf-8")
    print(json.dumps({"status":"completed","image_files":len(assets),"directory":out.as_posix()}))


if __name__=="__main__":
    build()
