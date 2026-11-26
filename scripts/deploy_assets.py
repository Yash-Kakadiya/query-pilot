import os
import shutil
import pathlib
import sys

def main():
    repo_root = pathlib.Path(__file__).parent.parent.resolve()
    src = (repo_root / "frontend" / "dist").resolve()
    if not (src / "index.html").is_file():
        sys.exit("Error: frontend/dist/index.html not found.")

    localappdata = pathlib.Path(os.environ["LOCALAPPDATA"])

    targets = [
        (repo_root / "src" / "query_pilot" / "agent" / "static").resolve(),
        (repo_root / "dist" / "querypilot-agent" / "_internal" / "query_pilot" / "agent" / "static").resolve(),
        (repo_root / "release" / "querypilot-agent" / "_internal" / "query_pilot" / "agent" / "static").resolve(),
        (localappdata / "QueryPilot" / "querypilot-agent" / "_internal" / "query_pilot" / "agent" / "static").resolve(),
        (localappdata / "QueryPilot" / "_up_" / "dist" / "querypilot-agent" / "_internal" / "query_pilot" / "agent" / "static").resolve(),
    ]

    # Dynamically discover all release distribution static directories (e.g. QueryPilot-v0.2.0-windows-x64, v0.3.0)
    release_dir = repo_root / "release"
    if release_dir.is_dir():
        for sub in release_dir.iterdir():
            if sub.is_dir():
                cand = (sub / "querypilot-agent" / "_internal" / "query_pilot" / "agent" / "static").resolve()
                if cand.exists() and cand not in targets:
                    targets.append(cand)

    for t in targets:
        if t.exists():
            print(f"Syncing to {t}...")
            assets_dir = t / "assets"
            if assets_dir.exists():
                shutil.rmtree(assets_dir)
            for item in src.iterdir():
                dest = t / item.name
                if item.is_dir():
                    shutil.copytree(item, dest)
                else:
                    shutil.copy2(item, dest)
            print(f"  Synced -> index.html size: {(t / 'index.html').stat().st_size} bytes")

    # Clean EBWebView cache
    cache_dirs = [
        localappdata / "com.querypilot.desktop" / "EBWebView",
        localappdata / "QueryPilot" / "EBWebView",
    ]
    for ebwebview in cache_dirs:
        if ebwebview.exists():
            try:
                shutil.rmtree(ebwebview)
                print(f"Cleared EBWebView cache successfully at {ebwebview}.")
            except Exception as e:
                print(f"Note: Could not clear EBWebView cache at {ebwebview}: {e}")

if __name__ == "__main__":
    main()
