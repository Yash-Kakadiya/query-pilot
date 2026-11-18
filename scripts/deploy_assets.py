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
    ebwebview = localappdata / "com.querypilot.desktop" / "EBWebView"
    if ebwebview.exists():
        try:
            shutil.rmtree(ebwebview)
            print("Cleared EBWebView cache successfully.")
        except Exception as e:
            print(f"Note: Could not clear EBWebView cache: {e}")

if __name__ == "__main__":
    main()
