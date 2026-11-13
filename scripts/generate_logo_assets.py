"""Generates QueryPilot professional logo, favicon, and banner assets."""

from pathlib import Path
import subprocess
from PIL import Image

ASSETS_DIR = Path("docs/assets")
ASSETS_DIR.mkdir(parents=True, exist_ok=True)
FRONTEND_PUBLIC = Path("frontend/public")
FRONTEND_PUBLIC.mkdir(parents=True, exist_ok=True)
TAURI_ICONS = Path("src-tauri/icons")

# -----------------------------------------------------------------------------
# 1. Standalone Mark / Icon SVG (512x512)
# -----------------------------------------------------------------------------
LOGO_ICON_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512" width="512" height="512" fill="none">
  <defs>
    <!-- Background Gradient -->
    <radialGradient id="bgGlow" cx="50%" cy="40%" r="65%">
      <stop offset="0%" stop-color="#1e293b" />
      <stop offset="60%" stop-color="#0f172a" />
      <stop offset="100%" stop-color="#090d16" />
    </radialGradient>

    <!-- Outer Ring Gradient (The Q Body) -->
    <linearGradient id="ringGrad" x1="120" y1="100" x2="400" y2="420" gradientUnits="userSpaceOnUse">
      <stop offset="0%" stop-color="#38bdf8" />
      <stop offset="45%" stop-color="#6366f1" />
      <stop offset="100%" stop-color="#a855f7" />
    </linearGradient>

    <!-- Inner Core Needle Left Facet -->
    <linearGradient id="needleLeft" x1="160" y1="310" x2="350" y2="160" gradientUnits="userSpaceOnUse">
      <stop offset="0%" stop-color="#0284c7" />
      <stop offset="50%" stop-color="#38bdf8" />
      <stop offset="100%" stop-color="#e0f2fe" />
    </linearGradient>

    <!-- Inner Core Needle Right Facet -->
    <linearGradient id="needleRight" x1="240" y1="360" x2="350" y2="160" gradientUnits="userSpaceOnUse">
      <stop offset="0%" stop-color="#4f46e5" />
      <stop offset="60%" stop-color="#6366f1" />
      <stop offset="100%" stop-color="#c7d2fe" />
    </linearGradient>

    <!-- Data Track Arc 1 -->
    <linearGradient id="trackGrad" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0%" stop-color="#38bdf8" stop-opacity="0.8" />
      <stop offset="100%" stop-color="#818cf8" stop-opacity="0.2" />
    </linearGradient>

    <!-- Outer Glow Filter -->
    <filter id="coreGlow" x="-20%" y="-20%" width="140%" height="140%">
      <feGaussianBlur stdDeviation="16" result="blur" />
      <feComposite in="SourceGraphic" in2="blur" operator="over" />
    </filter>

    <!-- Subtle Drop Shadow -->
    <filter id="dropShadow" x="-10%" y="-10%" width="120%" height="120%">
      <feDropShadow dx="0" dy="12" stdDeviation="16" flood-color="#000000" flood-opacity="0.6" />
    </filter>
  </defs>

  <!-- Squircle Base Canvas -->
  <rect x="24" y="24" width="464" height="464" rx="108" fill="url(#bgGlow)" stroke="#1e293b" stroke-width="4" filter="url(#dropShadow)" />

  <!-- Ambient Light Reflection Arc -->
  <path d="M 100 160 A 180 180 0 0 1 360 80" stroke="#38bdf8" stroke-width="2" stroke-opacity="0.2" stroke-linecap="round" fill="none" />
  
  <!-- Subtle Data Geometry (Radar / Database Grid) -->
  <circle cx="248" cy="248" r="150" stroke="#334155" stroke-width="1.5" stroke-dasharray="4 8" stroke-opacity="0.4" fill="none" />
  <circle cx="248" cy="248" r="100" stroke="#334155" stroke-width="1.5" stroke-dasharray="3 6" stroke-opacity="0.3" fill="none" />

  <!-- The "Q" Outer Precision Ring -->
  <path d="M 248 88
           A 160 160 0 1 0 388 328
           L 412 368
           A 22 22 0 0 1 378 392
           L 348 352
           A 160 160 0 0 0 248 88 Z"
        fill="none"
        stroke="url(#ringGrad)"
        stroke-width="26"
        stroke-linecap="round"
        stroke-linejoin="round"
        filter="url(#coreGlow)" />

  <!-- Inner Orbit Accent Rings (Database Layers) -->
  <path d="M 148 248 A 100 100 0 0 1 332 180" stroke="url(#trackGrad)" stroke-width="6" stroke-linecap="round" fill="none" />
  <path d="M 168 288 A 100 100 0 0 0 318 318" stroke="url(#trackGrad)" stroke-width="6" stroke-linecap="round" fill="none" />

  <!-- Precision Cardinal Ticks -->
  <line x1="248" y1="64" x2="248" y2="76" stroke="#38bdf8" stroke-width="3" stroke-linecap="round" />
  <line x1="64" y1="248" x2="76" y2="248" stroke="#38bdf8" stroke-width="3" stroke-linecap="round" />
  <line x1="248" y1="420" x2="248" y2="432" stroke="#6366f1" stroke-width="3" stroke-linecap="round" />
  <line x1="420" y1="248" x2="432" y2="248" stroke="#818cf8" stroke-width="3" stroke-linecap="round" />

  <!-- Pilot Delta Navigation Arrow (Primary Emblem) -->
  <!-- Left Wing -->
  <polygon points="358,138 184,286 244,272" fill="url(#needleLeft)" filter="url(#dropShadow)" />
  <!-- Right Wing -->
  <polygon points="358,138 244,272 284,348" fill="url(#needleRight)" filter="url(#dropShadow)" />
  <!-- Spine Highlight (Gleam) -->
  <line x1="358" y1="138" x2="244" y2="272" stroke="#ffffff" stroke-width="2.5" stroke-linecap="round" stroke-opacity="0.9" />

  <!-- Q Tail Connector Jet (Speed trail slicing outward) -->
  <polygon points="252,286 392,416 364,424 234,302" fill="url(#ringGrad)" opacity="0.9" />
  <circle cx="392" cy="416" r="6" fill="#38bdf8" filter="url(#coreGlow)" />

  <!-- North-East Guidance Beacon (Apex Star) -->
  <circle cx="358" cy="138" r="4.5" fill="#ffffff" filter="url(#coreGlow)" />
</svg>
"""

# -----------------------------------------------------------------------------
# 2. Transparent Clean Favicon / Inline Mark SVG (No heavy outer box)
# -----------------------------------------------------------------------------
FAVICON_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512" width="512" height="512" fill="none">
  <defs>
    <linearGradient id="favRing" x1="120" y1="100" x2="400" y2="420" gradientUnits="userSpaceOnUse">
      <stop offset="0%" stop-color="#38bdf8" />
      <stop offset="50%" stop-color="#6366f1" />
      <stop offset="100%" stop-color="#a855f7" />
    </linearGradient>
    <linearGradient id="favNeedleLeft" x1="160" y1="310" x2="350" y2="160" gradientUnits="userSpaceOnUse">
      <stop offset="0%" stop-color="#0284c7" />
      <stop offset="60%" stop-color="#38bdf8" />
      <stop offset="100%" stop-color="#ffffff" />
    </linearGradient>
    <linearGradient id="favNeedleRight" x1="240" y1="360" x2="350" y2="160" gradientUnits="userSpaceOnUse">
      <stop offset="0%" stop-color="#4338ca" />
      <stop offset="60%" stop-color="#6366f1" />
      <stop offset="100%" stop-color="#e0e7ff" />
    </linearGradient>
    <filter id="favGlow" x="-20%" y="-20%" width="140%" height="140%">
      <feGaussianBlur stdDeviation="12" result="blur" />
      <feComposite in="SourceGraphic" in2="blur" operator="over" />
    </filter>
  </defs>

  <!-- Background Disc -->
  <circle cx="256" cy="256" r="236" fill="#0f172a" stroke="#1e293b" stroke-width="8" />

  <!-- The "Q" Outer Precision Ring -->
  <path d="M 256 96
           A 160 160 0 1 0 396 336
           L 426 382
           A 22 22 0 0 1 388 406
           L 354 362
           A 160 160 0 0 0 256 96 Z"
        fill="none"
        stroke="url(#favRing)"
        stroke-width="30"
        stroke-linecap="round"
        stroke-linejoin="round"
        filter="url(#favGlow)" />

  <!-- Internal Orbit Arc -->
  <path d="M 160 256 A 96 96 0 0 1 336 190" stroke="#38bdf8" stroke-width="8" stroke-linecap="round" stroke-opacity="0.6" fill="none" />

  <!-- Pilot Delta Arrow -->
  <polygon points="364,148 190,296 250,282" fill="url(#favNeedleLeft)" />
  <polygon points="364,148 250,282 290,358" fill="url(#favNeedleRight)" />
  <line x1="364" y1="148" x2="250" y2="282" stroke="#ffffff" stroke-width="3" stroke-linecap="round" />

  <!-- Q Tail Connector -->
  <polygon points="258,296 398,426 370,434 240,312" fill="url(#favRing)" opacity="0.95" />
  <circle cx="398" cy="426" r="8" fill="#38bdf8" />
  <circle cx="364" cy="148" r="6" fill="#ffffff" />
</svg>
"""

# -----------------------------------------------------------------------------
# 3. Horizontal Banner with Wordmark SVG (900x240)
# -----------------------------------------------------------------------------
BANNER_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 960 260" width="960" height="260" fill="none">
  <defs>
    <radialGradient id="bannerGlow" cx="20%" cy="50%" r="60%">
      <stop offset="0%" stop-color="#1e293b" />
      <stop offset="70%" stop-color="#0f172a" />
      <stop offset="100%" stop-color="#070a12" />
    </radialGradient>
    <linearGradient id="brandGrad" x1="0" y1="0" x2="1" y2="0">
      <stop offset="0%" stop-color="#38bdf8" />
      <stop offset="50%" stop-color="#818cf8" />
      <stop offset="100%" stop-color="#c084fc" />
    </linearGradient>
    <linearGradient id="ringGradB" x1="40" y1="30" x2="200" y2="210" gradientUnits="userSpaceOnUse">
      <stop offset="0%" stop-color="#38bdf8" />
      <stop offset="50%" stop-color="#6366f1" />
      <stop offset="100%" stop-color="#a855f7" />
    </linearGradient>
    <linearGradient id="needleLB" x1="80" y1="160" x2="180" y2="70" gradientUnits="userSpaceOnUse">
      <stop offset="0%" stop-color="#0284c7" />
      <stop offset="50%" stop-color="#38bdf8" />
      <stop offset="100%" stop-color="#ffffff" />
    </linearGradient>
    <linearGradient id="needleRB" x1="120" y1="190" x2="180" y2="70" gradientUnits="userSpaceOnUse">
      <stop offset="0%" stop-color="#4f46e5" />
      <stop offset="60%" stop-color="#6366f1" />
      <stop offset="100%" stop-color="#c7d2fe" />
    </linearGradient>
    <filter id="glowB" x="-20%" y="-20%" width="140%" height="140%">
      <feGaussianBlur stdDeviation="8" result="blur" />
      <feComposite in="SourceGraphic" in2="blur" operator="over" />
    </filter>
  </defs>

  <!-- Banner Background -->
  <rect width="960" height="260" rx="24" fill="url(#bannerGlow)" stroke="#1e293b" stroke-width="2" />

  <!-- Left Icon Emblem (Centered at x=135, y=130) -->
  <g transform="translate(15, 10)">
    <!-- Base Disc -->
    <circle cx="120" cy="120" r="95" fill="#0b1120" stroke="#1e293b" stroke-width="3" />
    <!-- Ticks -->
    <circle cx="120" cy="120" r="70" stroke="#334155" stroke-width="1.2" stroke-dasharray="3 5" stroke-opacity="0.5" fill="none" />
    <!-- Q Ring -->
    <path d="M 120 45 A 75 75 0 1 0 185 160 L 200 182 A 10 10 0 0 1 184 194 L 166 172 A 75 75 0 0 0 120 45 Z"
          fill="none" stroke="url(#ringGradB)" stroke-width="14" stroke-linecap="round" stroke-linejoin="round" filter="url(#glowB)" />
    <!-- Orbit Arc -->
    <path d="M 75 120 A 45 45 0 0 1 155 90" stroke="#38bdf8" stroke-width="3.5" stroke-linecap="round" stroke-opacity="0.6" fill="none" />
    <!-- Needle -->
    <polygon points="172,70 90,140 120,132" fill="url(#needleLB)" />
    <polygon points="172,70 120,132 140,170" fill="url(#needleRB)" />
    <line x1="172" y1="70" x2="120" y2="132" stroke="#ffffff" stroke-width="1.5" stroke-linecap="round" />
    <!-- Q Tail -->
    <polygon points="124,138 190,200 176,204 114,146" fill="url(#ringGradB)" />
    <circle cx="190" cy="200" r="4" fill="#38bdf8" />
    <circle cx="172" cy="70" r="3" fill="#ffffff" />
  </g>

  <!-- Typography: QueryPilot -->
  <text x="280" y="118" font-family="-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif" font-size="64" font-weight="800" letter-spacing="-0.03em" fill="#f8fafc">
    Query<tspan fill="url(#brandGrad)">Pilot</tspan>
  </text>

  <!-- Tagline -->
  <text x="282" y="160" font-family="-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif" font-size="20" font-weight="500" letter-spacing="0.01em" fill="#94a3b8">
    Autonomous Natural-Language to SQL Analyst
  </text>

  <!-- Guarded Badges / Pillars -->
  <g transform="translate(282, 186)">
    <rect x="0" y="0" width="165" height="28" rx="6" fill="#1e293b" stroke="#334155" stroke-width="1" />
    <text x="12" y="19" font-family="-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif" font-size="12" font-weight="600" fill="#38bdf8">⚡ Dual-Engine Routing</text>

    <rect x="175" y="0" width="160" height="28" rx="6" fill="#1e293b" stroke="#334155" stroke-width="1" />
    <text x="187" y="19" font-family="-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif" font-size="12" font-weight="600" fill="#818cf8">🛡️ Guarded Execution</text>

    <rect x="345" y="0" width="155" height="28" rx="6" fill="#1e293b" stroke="#334155" stroke-width="1" />
    <text x="357" y="19" font-family="-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif" font-size="12" font-weight="600" fill="#34d399">🔒 Zero Data Leakage</text>
  </g>
</svg>
"""

def generate_svgs():
    (ASSETS_DIR / "querypilot-logo.svg").write_text(LOGO_ICON_SVG, encoding="utf-8")
    (ASSETS_DIR / "querypilot-banner.svg").write_text(BANNER_SVG, encoding="utf-8")
    (FRONTEND_PUBLIC / "favicon.svg").write_text(FAVICON_SVG, encoding="utf-8")
    (FRONTEND_PUBLIC / "logo.svg").write_text(LOGO_ICON_SVG, encoding="utf-8")
    print("SVGs generated successfully.")

def render_pngs_with_edge():
    edge_path = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
    if not Path(edge_path).exists():
        print("Edge binary not found, skipping rasterization.")
        return

    # Create temporary HTML pages to screenshot
    tmp_html = ASSETS_DIR / "temp_render.html"
    
    # 1. Render Logo PNG (512x512)
    logo_svg_path = (ASSETS_DIR / "querypilot-logo.svg").resolve().as_uri()
    tmp_html.write_text(f"""<!DOCTYPE html>
<html>
<head>
<style>
  body, html {{ margin: 0; padding: 0; background: transparent; overflow: hidden; }}
  img {{ width: 512px; height: 512px; display: block; }}
</style>
</head>
<body>
  <img src="{logo_svg_path}" />
</body>
</html>""", encoding="utf-8")

    out_logo_png = (ASSETS_DIR / "querypilot-logo.png").resolve()
    cmd1 = [
        edge_path,
        "--headless",
        "--disable-gpu",
        "--hide-scrollbars",
        "--default-background-color=00000000",
        "--window-size=512,512",
        f"--screenshot={out_logo_png}",
        tmp_html.resolve().as_uri(),
    ]
    subprocess.run(cmd1, check=True)
    print(f"Rendered: {out_logo_png}")

    # 2. Render Banner PNG (960x260)
    banner_svg_path = (ASSETS_DIR / "querypilot-banner.svg").resolve().as_uri()
    tmp_html.write_text(f"""<!DOCTYPE html>
<html>
<head>
<style>
  body, html {{ margin: 0; padding: 0; background: transparent; overflow: hidden; }}
  img {{ width: 960px; height: 260px; display: block; }}
</style>
</head>
<body>
  <img src="{banner_svg_path}" />
</body>
</html>""", encoding="utf-8")

    out_banner_png = (ASSETS_DIR / "querypilot-banner.png").resolve()
    cmd2 = [
        edge_path,
        "--headless",
        "--disable-gpu",
        "--hide-scrollbars",
        "--default-background-color=00000000",
        "--window-size=960,260",
        f"--screenshot={out_banner_png}",
        tmp_html.resolve().as_uri(),
    ]
    subprocess.run(cmd2, check=True)
    print(f"Rendered: {out_banner_png}")

    # Clean up temp html
    if tmp_html.exists():
        tmp_html.unlink()

    # Generate app icon variants in src-tauri/icons if needed
    if out_logo_png.exists():
        im = Image.open(out_logo_png)
        for size in [32, 64, 128, 256]:
            resized = im.resize((size, size), Image.Resampling.LANCZOS)
            target = TAURI_ICONS / f"{size}x{size}.png"
            if target.parent.exists():
                resized.save(target)
        # Save main icon.png
        im.resize((512, 512), Image.Resampling.LANCZOS).save(TAURI_ICONS / "icon.png")
        # Save icon.ico
        im.save(TAURI_ICONS / "icon.ico", sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
        print("Updated Tauri desktop icons.")

if __name__ == "__main__":
    generate_svgs()
    render_pngs_with_edge()

