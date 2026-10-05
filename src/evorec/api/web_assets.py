"""Only public repository display assets; shared by routing and link checks."""
WEB_PAGES = {"/app": "web/index.html", "/app/results": "web/results.html"}
R06_ASSETS = {name: "docs/experiments/r06-multi-interest/" + name for name in (
    "results.json", "uncertainty.json", "report.md", "report.html", "figures/manifest.json",
    "figures/coverage-and-ranking.png", "figures/learning-curves.png", "figures/paired-intervals.png",
)}
R06_ASSETS["protocol.md"] = "docs/experiments/r06-multi-interest-protocol.md"
PUBLIC_WEB_FILES = {**WEB_PAGES, **{f"/app/evidence/r06/{name}": path for name, path in R06_ASSETS.items()}}
