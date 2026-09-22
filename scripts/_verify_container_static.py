from pathlib import Path
root = Path("/app/app/static/assets")
print("container js", len(list(root.glob("*.js"))))
legacy = [p.name for p in root.glob("*.js") if any(x in p.read_text(errors="ignore") for x in ("nl_goal", "browser_use", "legacy_hybrid"))]
ota = [p.name for p in root.glob("*.js") if "max_steps_per_nl" in p.read_text(errors="ignore") or "智能 OTA" in p.read_text(errors="ignore")]
print("legacy", len(legacy), legacy[:3])
print("ota", len(ota), ota[:3])
