"""Publish byte-identical charts from the completed R05 replication report."""
import json
import shutil
from pathlib import Path

from PIL import Image
from evorec.research.runner import file_sha


def build():
    source = Path("docs/experiments/r05-cold-replication")
    result = json.loads((source / "results.json").read_text(encoding="utf-8"))
    if result["series"]["status"] != "completed" or result["analysis"]["status"] != "passed":
        raise ValueError("completed training and analysis are required")
    output = Path("docs/blog/assets/evorec/r05-replication")
    output.mkdir(parents=True, exist_ok=True)
    entries = []
    for name in ("learning-curves", "paired-intervals"):
        for suffix in (".png", ".svg"):
            original = source / "figures" / (name + suffix)
            destination = output / original.name
            shutil.copyfile(original, destination)
            entry = {"file": destination.name, "sha256": file_sha(destination),
                     "source_image": original.as_posix(),
                     "source_results": (source / "results.json").as_posix(),
                     "source_results_sha256": file_sha(source / "results.json")}
            if suffix == ".png":
                with Image.open(destination) as picture:
                    entry.update({"width": picture.width, "height": picture.height})
            entries.append(entry)
    manifest = {
        "generator": "scripts/build_replication_blog_assets.py",
        "generator_sha256": file_sha(Path(__file__)),
        "replication_id": result["series"]["replication_id"],
        "assets": entries,
    }
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    (output / "README.md").write_text(
        "# 冷加权复验配图\n\n"
        "图片来自实际完成的训练与用户聚类分析；[来源清单](manifest.json)。\n\n"
        "![三种子训练曲线](learning-curves.png)\n\n"
        "[训练曲线 SVG](learning-curves.svg)\n\n"
        "![三个固定种子指标平均的配对区间](paired-intervals.png)\n\n"
        "[区间 SVG](paired-intervals.svg)\n\n"
        "区间是固定检查点条件下的 95% 边际区间，未做多重比较校正；"
        "三种子指标平均不是集成排序结果。\n\n"
        "[完整报告](../../../../experiments/r05-cold-replication/report.md)\n",
        encoding="utf-8")
    print(json.dumps({"status": "completed", "image_files": len(entries),
                      "directory": output.as_posix()}))


if __name__ == "__main__":
    build()
