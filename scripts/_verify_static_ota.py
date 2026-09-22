from pathlib import Path

def check(root: Path, label: str) -> None:
    if not root.is_dir():
        print(f"{label}: MISSING {root}")
        return
    legacy = []
    ota = []
    for p in root.glob("*.js"):
        t = p.read_text(encoding="utf-8", errors="ignore")
        if any(x in t for x in ("nl_goal", "browser_use", "legacy_hybrid")):
            legacy.append(p.name)
        if "智能 OTA" in t or "max_steps_per_nl" in t:
            ota.append(p.name)
    print(f"{label}: js={len(list(root.glob('*.js')))} ota_hits={len(ota)} legacy_hits={len(legacy)}")
    if legacy:
        print("  legacy files:", legacy[:5])
    if ota:
        print("  ota files:", ota[:5])

check(Path(r"D:/uitest/VoyanTest/app/static/assets"), "local")
check(Path(r"D:/uitest/VoyanTest/frontend/dist/assets"), "dist")
