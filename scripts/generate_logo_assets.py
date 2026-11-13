"""Generates QueryPilot professional logo, favicon, and banner assets.

Concept A: Sleek Aerospace Compass Q
- Centered, faceted multi-axis navigation compass needle
- Perfectly balanced negative space with precision radar coordinate reticle
- Bold circular gradient Q ring with aerodynamic 45° jet-fin tail
"""

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
    <!-- Background Radial Gradient -->
    <radialGradient id="bgGrad" cx="50%" cy="46%" r="65%">
      <stop offset="0%" stop-color="#111827" />
      <stop offset="55%" stop-color="#0b1120" />
      <stop offset="100%" stop-color="#060911" />
    </radialGradient>

    <!-- Q Ring Circular Gradient -->
    <linearGradient id="qRingGrad" x1="110" y1="90" x2="400" y2="400" gradientUnits="userSpaceOnUse">
      <stop offset="0%" stop-color="#00f5ff" />
      <stop offset="25%" stop-color="#38bdf8" />
      <stop offset="65%" stop-color="#6366f1" />
      <stop offset="100%" stop-color="#8b5cf6" />
    </linearGradient>

    <!-- Primary Vertical Needle - North Left Facet (Cyan/White) -->
    <linearGradient id="needleVertL" x1="234" y1="256" x2="256" y2="156" gradientUnits="userSpaceOnUse">
      <stop offset="0%" stop-color="#0284c7" />
      <stop offset="60%" stop-color="#38bdf8" />
      <stop offset="100%" stop-color="#ffffff" />
    </linearGradient>

    <!-- Primary Vertical Needle - North Right Facet (Indigo/Cyan) -->
    <linearGradient id="needleVertR" x1="278" y1="256" x2="256" y2="156" gradientUnits="userSpaceOnUse">
      <stop offset="0%" stop-color="#4338ca" />
      <stop offset="60%" stop-color="#6366f1" />
      <stop offset="100%" stop-color="#e0e7ff" />
    </linearGradient>

    <!-- Vertical Needle - South Tip Facets -->
    <linearGradient id="needleSouthL" x1="256" y1="256" x2="256" y2="356" gradientUnits="userSpaceOnUse">
      <stop offset="0%" stop-color="#0284c7" />
      <stop offset="100%" stop-color="#38bdf8" />
    </linearGradient>
    <linearGradient id="needleSouthR" x1="256" y1="256" x2="256" y2="356" gradientUnits="userSpaceOnUse">
      <stop offset="0%" stop-color="#312e81" />
      <stop offset="100%" stop-color="#4f46e5" />
    </linearGradient>

    <!-- Secondary Angled Blade - Top-Right Facets -->
    <linearGradient id="bladeTopR_White" x1="256" y1="256" x2="330" y2="182" gradientUnits="userSpaceOnUse">
      <stop offset="0%" stop-color="#f8fafc" />
      <stop offset="100%" stop-color="#ffffff" />
    </linearGradient>
    <linearGradient id="bladeTopR_Cyan" x1="256" y1="256" x2="330" y2="182" gradientUnits="userSpaceOnUse">
      <stop offset="0%" stop-color="#0284c7" />
      <stop offset="70%" stop-color="#38bdf8" />
      <stop offset="100%" stop-color="#bae6fd" />
    </linearGradient>

    <!-- Secondary Angled Blade - Bottom-Left Facets -->
    <linearGradient id="bladeBotL_Cyan" x1="256" y1="256" x2="182" y2="330" gradientUnits="userSpaceOnUse">
      <stop offset="0%" stop-color="#0284c7" />
      <stop offset="70%" stop-color="#38bdf8" />
      <stop offset="100%" stop-color="#ffffff" />
    </linearGradient>
    <linearGradient id="bladeBotL_Dark" x1="256" y1="256" x2="182" y2="330" gradientUnits="userSpaceOnUse">
      <stop offset="0%" stop-color="#0f172a" />
      <stop offset="70%" stop-color="#0369a1" />
      <stop offset="100%" stop-color="#0284c7" />
    </linearGradient>

    <!-- Q Tail Gradient -->
    <linearGradient id="qTailGrad" x1="280" y1="310" x2="425" y2="425" gradientUnits="userSpaceOnUse">
      <stop offset="0%" stop-color="#4f46e5" />
      <stop offset="45%" stop-color="#6366f1" />
      <stop offset="80%" stop-color="#38bdf8" />
      <stop offset="100%" stop-color="#00f5ff" />
    </linearGradient>

    <!-- Glow Filter -->
    <filter id="ringGlow" x="-20%" y="-20%" width="140%" height="140%">
      <feGaussianBlur stdDeviation="8" result="blur" />
      <feComposite in="SourceGraphic" in2="blur" operator="over" />
    </filter>

    <!-- Needle Drop Shadow -->
    <filter id="needleShadow" x="-25%" y="-25%" width="150%" height="150%">
      <feDropShadow dx="2" dy="5" stdDeviation="6" flood-color="#000000" flood-opacity="0.65" />
    </filter>

    <!-- Base Drop Shadow -->
    <filter id="baseShadow" x="-10%" y="-10%" width="120%" height="120%">
      <feDropShadow dx="0" dy="12" stdDeviation="16" flood-color="#000000" flood-opacity="0.6" />
    </filter>
  </defs>

  <!-- Squircle Base Canvas -->
  <rect x="24" y="24" width="464" height="464" rx="108" fill="url(#bgGrad)" stroke="#1e293b" stroke-width="4" filter="url(#baseShadow)" />

  <!-- Ambient Light Reflection Arc -->
  <path d="M 110 160 A 170 170 0 0 1 360 84" stroke="#38bdf8" stroke-width="2" stroke-opacity="0.25" stroke-linecap="round" fill="none" />

  <!-- Radar Coordinate Geometry (Balanced Negative Space) -->
  <g opacity="0.38">
    <!-- Concentric Target Orbit Rings -->
    <circle cx="256" cy="256" r="148" stroke="#334155" stroke-width="1.5" stroke-dasharray="4 8" fill="none" />
    <circle cx="256" cy="256" r="108" stroke="#38bdf8" stroke-width="1.2" stroke-dasharray="3 6" fill="none" stroke-opacity="0.6" />
    <circle cx="256" cy="256" r="70" stroke="#334155" stroke-width="1" stroke-dasharray="2 4" fill="none" />

    <!-- Crosshair Reticle Axes -->
    <line x1="256" y1="96" x2="256" y2="416" stroke="#38bdf8" stroke-width="1.2" stroke-opacity="0.5" />
    <line x1="96" y1="256" x2="416" y2="256" stroke="#38bdf8" stroke-width="1.2" stroke-opacity="0.5" />

    <!-- 45-degree Guide Ticks -->
    <line x1="172" y1="172" x2="182" y2="182" stroke="#6366f1" stroke-width="1.5" stroke-linecap="round" stroke-opacity="0.7" />
    <line x1="340" y1="340" x2="330" y2="330" stroke="#6366f1" stroke-width="1.5" stroke-linecap="round" stroke-opacity="0.7" />
    <line x1="340" y1="172" x2="330" y2="182" stroke="#6366f1" stroke-width="1.5" stroke-linecap="round" stroke-opacity="0.7" />
    <line x1="172" y1="340" x2="182" y2="330" stroke="#6366f1" stroke-width="1.5" stroke-linecap="round" stroke-opacity="0.7" />
  </g>

  <!-- Cardinal Precision Pips -->
  <line x1="256" y1="64" x2="256" y2="76" stroke="#00f5ff" stroke-width="3" stroke-linecap="round" />
  <line x1="64" y1="256" x2="76" y2="256" stroke="#38bdf8" stroke-width="3" stroke-linecap="round" />
  <line x1="256" y1="436" x2="256" y2="448" stroke="#6366f1" stroke-width="3" stroke-linecap="round" />
  <line x1="436" y1="256" x2="448" y2="256" stroke="#818cf8" stroke-width="3" stroke-linecap="round" />

  <!-- Bold Circular Q Ring -->
  <circle cx="256" cy="256" r="148" stroke="url(#qRingGrad)" stroke-width="36" fill="none" filter="url(#ringGlow)" />

  <!-- The Q Tail: Bold Aerodynamic Jet Fin at 45° -->
  <polygon points="274,310 338,300 424,420 376,432 308,356" fill="url(#qTailGrad)" filter="url(#ringGlow)" />
  <!-- Tail Upper Gleam -->
  <line x1="338" y1="300" x2="424" y2="420" stroke="#ffffff" stroke-width="2.5" stroke-linecap="round" stroke-opacity="0.9" />

  <!-- Navigation Compass Instrument (Centered & Elevated) -->
  <g filter="url(#needleShadow)">
    <!-- 1. Secondary Angled Needle (Cross Blade) -->
    <!-- Top-Right Blade -->
    <polygon points="256,256 330,182 284,236" fill="url(#bladeTopR_White)" />
    <polygon points="256,256 330,182 242,216" fill="url(#bladeTopR_Cyan)" />
    <line x1="256" y1="256" x2="330" y2="182" stroke="#ffffff" stroke-width="1.8" stroke-linecap="round" />

    <!-- Bottom-Left Blade -->
    <polygon points="256,256 182,330 228,276" fill="url(#bladeBotL_Cyan)" />
    <polygon points="256,256 182,330 270,296" fill="url(#bladeBotL_Dark)" />
    <line x1="256" y1="256" x2="182" y2="330" stroke="#38bdf8" stroke-width="1.5" stroke-linecap="round" />

    <!-- 2. Primary Vertical Needle -->
    <!-- North Point (Fills Upper Half) -->
    <polygon points="256,156 234,256 256,256" fill="url(#needleVertL)" />
    <polygon points="256,156 256,256 278,256" fill="url(#needleVertR)" />
    <line x1="256" y1="156" x2="256" y2="256" stroke="#ffffff" stroke-width="2.5" stroke-linecap="round" stroke-opacity="0.95" />

    <!-- South Point (Balances Lower Half) -->
    <polygon points="256,356 234,256 256,256" fill="url(#needleSouthL)" />
    <polygon points="256,356 256,256 278,256" fill="url(#needleSouthR)" />
    <line x1="256" y1="356" x2="256" y2="256" stroke="#38bdf8" stroke-width="2" stroke-linecap="round" stroke-opacity="0.85" />

    <!-- 3. Central Pivot Hub Ring -->
    <circle cx="256" cy="256" r="17" fill="#0b1120" stroke="#00f5ff" stroke-width="3.5" />
    <circle cx="256" cy="256" r="9" fill="#1e293b" />
    <circle cx="256" cy="256" r="4.5" fill="#ffffff" />
  </g>

  <!-- North Star Beacon (Apex Gleam) -->
  <circle cx="256" cy="156" r="5" fill="#ffffff" filter="url(#ringGlow)" />
</svg>
"""

# -----------------------------------------------------------------------------
# 2. Transparent Clean Favicon / Mark SVG (512x512)
# -----------------------------------------------------------------------------
FAVICON_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512" width="512" height="512" fill="none">
  <defs>
    <linearGradient id="favRing" x1="110" y1="90" x2="400" y2="400" gradientUnits="userSpaceOnUse">
      <stop offset="0%" stop-color="#00f5ff" />
      <stop offset="25%" stop-color="#38bdf8" />
      <stop offset="65%" stop-color="#6366f1" />
      <stop offset="100%" stop-color="#8b5cf6" />
    </linearGradient>
    <linearGradient id="favVertL" x1="234" y1="256" x2="256" y2="156" gradientUnits="userSpaceOnUse">
      <stop offset="0%" stop-color="#0284c7" />
      <stop offset="60%" stop-color="#38bdf8" />
      <stop offset="100%" stop-color="#ffffff" />
    </linearGradient>
    <linearGradient id="favVertR" x1="278" y1="256" x2="256" y2="156" gradientUnits="userSpaceOnUse">
      <stop offset="0%" stop-color="#4338ca" />
      <stop offset="60%" stop-color="#6366f1" />
      <stop offset="100%" stop-color="#e0e7ff" />
    </linearGradient>
    <linearGradient id="favTail" x1="280" y1="310" x2="425" y2="425" gradientUnits="userSpaceOnUse">
      <stop offset="0%" stop-color="#4f46e5" />
      <stop offset="45%" stop-color="#6366f1" />
      <stop offset="80%" stop-color="#38bdf8" />
      <stop offset="100%" stop-color="#00f5ff" />
    </linearGradient>
    <filter id="favGlow" x="-20%" y="-20%" width="140%" height="140%">
      <feGaussianBlur stdDeviation="8" result="blur" />
      <feComposite in="SourceGraphic" in2="blur" operator="over" />
    </filter>
    <filter id="favNeedleShadow" x="-20%" y="-20%" width="140%" height="140%">
      <feDropShadow dx="2" dy="5" stdDeviation="6" flood-color="#000000" flood-opacity="0.6" />
    </filter>
  </defs>

  <!-- Base Disc -->
  <circle cx="256" cy="256" r="236" fill="#0b1120" stroke="#1e293b" stroke-width="8" />

  <!-- Radar coordinate tracks -->
  <circle cx="256" cy="256" r="108" stroke="#38bdf8" stroke-width="1.5" stroke-dasharray="3 6" fill="none" opacity="0.4" />

  <!-- Q Ring -->
  <circle cx="256" cy="256" r="148" stroke="url(#favRing)" stroke-width="36" fill="none" filter="url(#favGlow)" />

  <!-- Q Tail -->
  <polygon points="274,310 338,300 424,420 376,432 308,356" fill="url(#favTail)" filter="url(#favGlow)" />
  <line x1="338" y1="300" x2="424" y2="420" stroke="#ffffff" stroke-width="2.5" stroke-linecap="round" stroke-opacity="0.9" />

  <!-- Centered Navigation Compass -->
  <g filter="url(#favNeedleShadow)">
    <!-- Cross Blade -->
    <polygon points="256,256 330,182 284,236" fill="#ffffff" />
    <polygon points="256,256 182,330 228,276" fill="#38bdf8" />

    <!-- Vertical Needle -->
    <polygon points="256,156 234,256 256,256" fill="url(#favVertL)" />
    <polygon points="256,156 256,256 278,256" fill="url(#favVertR)" />
    <line x1="256" y1="156" x2="256" y2="256" stroke="#ffffff" stroke-width="2.5" stroke-linecap="round" />

    <polygon points="256,356 234,256 256,256" fill="#0284c7" />
    <polygon points="256,356 256,256 278,256" fill="#4338ca" />
    <line x1="256" y1="356" x2="256" y2="256" stroke="#38bdf8" stroke-width="2" stroke-linecap="round" />

    <!-- Hub -->
    <circle cx="256" cy="256" r="17" fill="#0b1120" stroke="#00f5ff" stroke-width="3.5" />
    <circle cx="256" cy="256" r="6" fill="#ffffff" />
  </g>

  <!-- Beacon Star -->
  <circle cx="256" cy="156" r="5" fill="#ffffff" filter="url(#favGlow)" />
</svg>
"""

# -----------------------------------------------------------------------------
# 3. Horizontal Banner with Wordmark SVG (960x260)
# -----------------------------------------------------------------------------
BANNER_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 960 260" width="960" height="260" fill="none">
  <defs>
    <radialGradient id="bannerGlow" cx="20%" cy="50%" r="60%">
      <stop offset="0%" stop-color="#1e293b" />
      <stop offset="70%" stop-color="#0f172a" />
      <stop offset="100%" stop-color="#070a12" />
    </radialGradient>
    <linearGradient id="brandGrad" x1="0" y1="0" x2="1" y2="0">
      <stop offset="0%" stop-color="#00f5ff" />
      <stop offset="50%" stop-color="#818cf8" />
      <stop offset="100%" stop-color="#c084fc" />
    </linearGradient>
    <linearGradient id="ringGradB" x1="50" y1="40" x2="200" y2="200" gradientUnits="userSpaceOnUse">
      <stop offset="0%" stop-color="#00f5ff" />
      <stop offset="25%" stop-color="#38bdf8" />
      <stop offset="65%" stop-color="#6366f1" />
      <stop offset="100%" stop-color="#8b5cf6" />
    </linearGradient>
    <linearGradient id="vertLB" x1="110" y1="120" x2="120" y2="72" gradientUnits="userSpaceOnUse">
      <stop offset="0%" stop-color="#0284c7" />
      <stop offset="60%" stop-color="#38bdf8" />
      <stop offset="100%" stop-color="#ffffff" />
    </linearGradient>
    <linearGradient id="vertRB" x1="130" y1="120" x2="120" y2="72" gradientUnits="userSpaceOnUse">
      <stop offset="0%" stop-color="#4338ca" />
      <stop offset="60%" stop-color="#6366f1" />
      <stop offset="100%" stop-color="#e0e7ff" />
    </linearGradient>
    <linearGradient id="tailGradB" x1="130" y1="145" x2="202" y2="202" gradientUnits="userSpaceOnUse">
      <stop offset="0%" stop-color="#4f46e5" />
      <stop offset="45%" stop-color="#6366f1" />
      <stop offset="85%" stop-color="#38bdf8" />
      <stop offset="100%" stop-color="#00f5ff" />
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
    <!-- Radar Ticks -->
    <circle cx="120" cy="120" r="70" stroke="#334155" stroke-width="1.2" stroke-dasharray="3 5" stroke-opacity="0.4" fill="none" />
    <circle cx="120" cy="120" r="50" stroke="#38bdf8" stroke-width="1" stroke-dasharray="2 4" stroke-opacity="0.4" fill="none" />
    <!-- Reticle axes -->
    <line x1="120" y1="46" x2="120" y2="194" stroke="#38bdf8" stroke-width="1" stroke-opacity="0.3" />
    <line x1="46" y1="120" x2="194" y2="120" stroke="#38bdf8" stroke-width="1" stroke-opacity="0.3" />

    <!-- Q Ring -->
    <circle cx="120" cy="120" r="70" fill="none" stroke="url(#ringGradB)" stroke-width="17" filter="url(#glowB)" />
    
    <!-- Q Tail -->
    <polygon points="128,145 158,141 202,198 178,204 146,168" fill="url(#tailGradB)" filter="url(#glowB)" />
    <line x1="158" y1="141" x2="202" y2="198" stroke="#ffffff" stroke-width="1.4" stroke-linecap="round" stroke-opacity="0.9" />

    <!-- Centered Needle -->
    <g>
      <!-- Cross blade -->
      <polygon points="120,120 155,85 133,111" fill="#ffffff" />
      <polygon points="120,120 85,155 107,129" fill="#38bdf8" />

      <!-- Vertical Needle -->
      <polygon points="120,72 109,120 120,120" fill="url(#vertLB)" />
      <polygon points="120,72 120,120 131,120" fill="url(#vertRB)" />
      <line x1="120" y1="72" x2="120" y2="120" stroke="#ffffff" stroke-width="1.5" stroke-linecap="round" />

      <polygon points="120,168 109,120 120,120" fill="#0284c7" />
      <polygon points="120,168 120,120 131,120" fill="#4338ca" />
      <line x1="120" y1="168" x2="120" y2="120" stroke="#38bdf8" stroke-width="1.2" stroke-linecap="round" />

      <!-- Hub -->
      <circle cx="120" cy="120" r="8" fill="#0b1120" stroke="#00f5ff" stroke-width="2" />
      <circle cx="120" cy="120" r="3" fill="#ffffff" />
    </g>

    <!-- Beacon -->
    <circle cx="120" cy="72" r="3" fill="#ffffff" />
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

    tmp_html = ASSETS_DIR / "temp_render.html"

    # 1. Render Logo PNG (512x512)
    logo_svg_path = (ASSETS_DIR / "querypilot-logo.svg").resolve().as_uri()
    tmp_html.write_text(
        f"""<!DOCTYPE html>
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
</html>""",
        encoding="utf-8",
    )

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
    tmp_html.write_text(
        f"""<!DOCTYPE html>
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
</html>""",
        encoding="utf-8",
    )

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

    if tmp_html.exists():
        tmp_html.unlink()

    # Update ALL icon variants in src-tauri/icons
    if out_logo_png.exists():
        im = Image.open(out_logo_png)

        # Standard PNG icon sizes
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
            target = TAURI_ICONS / name
            resized.save(target)

        # Save proper Windows ICO with standard multi-resolution bitmap layers
        ico_sizes = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
        im.save(TAURI_ICONS / "icon.ico", format="ICO", sizes=ico_sizes)
        print("Updated ALL Tauri desktop icon variants.")


if __name__ == "__main__":
    generate_svgs()
    render_pngs_with_edge()
