"""Generates QueryPilot professional logo, favicon, and banner assets.

Uses the confirmed master brand mark chosen by the user:
- 512x512 master logo
- All Windows desktop icons (PNG variants + multi-res ICO)
- Horizontal README banner with typography & pillars
- Web SVGs and favicons
"""

import base64
from pathlib import Path
import subprocess
from PIL import Image

ASSETS_DIR = Path("docs/assets")
ASSETS_DIR.mkdir(parents=True, exist_ok=True)
FRONTEND_PUBLIC = Path("frontend/public")
FRONTEND_PUBLIC.mkdir(parents=True, exist_ok=True)
TAURI_ICONS = Path("src-tauri/icons")
TAURI_ICONS.mkdir(parents=True, exist_ok=True)

MASTER_PATH = ASSETS_DIR / "querypilot-logo-master.png"


def generate_logos_and_icons():
    # 1. Open Master 1024x1024 image
    im = Image.open(MASTER_PATH).convert("RGBA")

    # 2. Save 512x512 logo PNG
    logo_512 = im.resize((512, 512), Image.Resampling.LANCZOS)
    out_logo_png = ASSETS_DIR / "querypilot-logo.png"
    logo_512.save(out_logo_png, "PNG")
    print(f"Saved: {out_logo_png}")

    # 3. Save all Tauri PNG icon variants
    sizes_to_save = {
        "32x32.png": (32, 32),
        "64x64.png": (64, 64),
        "128x128.png": (128, 128),
        "128x128@2x.png": (256, 256),
        "256x256.png": (256, 256),
        "icon.png": (512, 512),
        "StoreLogo.png": (50, 50),
        "Square30x30Logo.png": (30, 30),
        "Square44x44Logo.png": (44, 44),
        "Square71x71Logo.png": (71, 71),
        "Square89x89Logo.png": (89, 89),
        "Square107x107Logo.png": (107, 107),
        "Square142x142Logo.png": (142, 142),
        "Square150x150Logo.png": (150, 150),
        "Square284x284Logo.png": (284, 284),
        "Square310x310Logo.png": (310, 310),
    }
    for name, sz in sizes_to_save.items():
        resized = im.resize(sz, Image.Resampling.LANCZOS)
        resized.save(TAURI_ICONS / name)

    # 4. Save proper Windows ICO with standard multi-resolution bitmap layers
    ico_sizes = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
    im.save(TAURI_ICONS / "icon.ico", format="ICO", sizes=ico_sizes)
    print("Updated ALL Tauri desktop icon variants and multi-resolution icon.ico.")

    # 5. Build base64 data for SVGs
    logo_b64 = base64.b64encode(out_logo_png.read_bytes()).decode("ascii")

    # 6. Generate Logo SVG
    logo_svg = f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512" width="512" height="512">
  <image href="data:image/png;base64,{logo_b64}" width="512" height="512" />
</svg>
"""
    (ASSETS_DIR / "querypilot-logo.svg").write_text(logo_svg, encoding="utf-8")
    (FRONTEND_PUBLIC / "logo.svg").write_text(logo_svg, encoding="utf-8")
    (FRONTEND_PUBLIC / "favicon.svg").write_text(logo_svg, encoding="utf-8")
    print("Saved logo.svg and favicon.svg.")

    # 7. Generate Banner SVG & PNG
    emblem_220 = im.resize((220, 220), Image.Resampling.LANCZOS)
    temp_p = ASSETS_DIR / "temp_e220.png"
    emblem_220.save(temp_p, "PNG")
    emblem_b64 = base64.b64encode(temp_p.read_bytes()).decode("ascii")
    temp_p.unlink()

    banner_svg = f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 960 260" width="960" height="260" fill="none">
  <defs>
    <radialGradient id="bannerGlow" cx="20%" cy="50%" r="60%">
      <stop offset="0%" stop-color="#141c30" />
      <stop offset="70%" stop-color="#0a0f1d" />
      <stop offset="100%" stop-color="#050811" />
    </radialGradient>
    <linearGradient id="brandGrad" x1="0" y1="0" x2="1" y2="0">
      <stop offset="0%" stop-color="#00f5ff" />
      <stop offset="50%" stop-color="#818cf8" />
      <stop offset="100%" stop-color="#c084fc" />
    </linearGradient>
    <linearGradient id="bannerBorderGrad" x1="0" y1="0" x2="960" y2="260" gradientUnits="userSpaceOnUse">
      <stop offset="0%" stop-color="#00f5ff" stop-opacity="0.5" />
      <stop offset="50%" stop-color="#334155" stop-opacity="0.3" />
      <stop offset="100%" stop-color="#c084fc" stop-opacity="0.4" />
    </linearGradient>
    <filter id="bannerDrop" x="-5%" y="-5%" width="110%" height="110%">
      <feDropShadow dx="0" dy="10" stdDeviation="16" flood-color="#000000" flood-opacity="0.6" />
    </filter>
  </defs>

  <!-- Banner Background -->
  <rect width="960" height="260" rx="24" fill="url(#bannerGlow)" stroke="url(#bannerBorderGrad)" stroke-width="2" filter="url(#bannerDrop)" />

  <!-- Left Icon Emblem -->
  <image href="data:image/png;base64,{emblem_b64}" x="22" y="20" width="220" height="220" />

  <!-- Typography: QueryPilot -->
  <text x="272" y="118" font-family="-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif" font-size="64" font-weight="800" letter-spacing="-0.03em" fill="#f8fafc">
    Query<tspan fill="url(#brandGrad)">Pilot</tspan>
  </text>

  <!-- Tagline -->
  <text x="274" y="160" font-family="-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif" font-size="20" font-weight="500" letter-spacing="0.01em" fill="#94a3b8">
    Autonomous Natural-Language to SQL Analyst
  </text>

  <!-- Guarded Badges / Pillars -->
  <g transform="translate(274, 186)">
    <rect x="0" y="0" width="165" height="28" rx="6" fill="#111827" stroke="#1f293d" stroke-width="1.2" />
    <text x="12" y="19" font-family="-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif" font-size="12" font-weight="600" fill="#38bdf8">⚡ Dual-Engine Routing</text>

    <rect x="175" y="0" width="160" height="28" rx="6" fill="#111827" stroke="#1f293d" stroke-width="1.2" />
    <text x="187" y="19" font-family="-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif" font-size="12" font-weight="600" fill="#818cf8">🛡️ Guarded Execution</text>

    <rect x="345" y="0" width="155" height="28" rx="6" fill="#111827" stroke="#1f293d" stroke-width="1.2" />
    <text x="357" y="19" font-family="-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif" font-size="12" font-weight="600" fill="#34d399">🔒 Zero Data Leakage</text>
  </g>
</svg>
"""
    banner_svg_path = ASSETS_DIR / "querypilot-banner.svg"
    banner_svg_path.write_text(banner_svg, encoding="utf-8")

    # Render banner PNG with Edge
    edge_path = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
    banner_png_path = (ASSETS_DIR / "querypilot-banner.png").resolve()
    cmd = [
        edge_path,
        "--headless",
        "--disable-gpu",
        "--hide-scrollbars",
        "--default-background-color=00000000",
        "--window-size=960,260",
        f"--screenshot={banner_png_path}",
        banner_svg_path.resolve().as_uri(),
    ]
    subprocess.run(cmd, check=True)
    print("Rendered: querypilot-banner.png")


if __name__ == "__main__":
    generate_logos_and_icons()
