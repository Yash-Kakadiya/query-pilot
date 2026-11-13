"""Generates QueryPilot professional logo, favicon, and banner assets.

Clean, continuous geometry where the Q's diagonal tail stroke directly continues
from the trailing edge of the navigation compass with zero overlap.
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
    <radialGradient id="bgGlow" cx="50%" cy="42%" r="65%">
      <stop offset="0%" stop-color="#1e293b" />
      <stop offset="55%" stop-color="#0f172a" />
      <stop offset="100%" stop-color="#090d16" />
    </radialGradient>

    <!-- Outer Ring Gradient (The Q Body) -->
    <linearGradient id="ringGrad" x1="100" y1="90" x2="410" y2="410" gradientUnits="userSpaceOnUse">
      <stop offset="0%" stop-color="#38bdf8" />
      <stop offset="45%" stop-color="#6366f1" />
      <stop offset="100%" stop-color="#a855f7" />
    </linearGradient>

    <!-- Pilot Needle Left Facet -->
    <linearGradient id="needleLeft" x1="160" y1="310" x2="355" y2="145" gradientUnits="userSpaceOnUse">
      <stop offset="0%" stop-color="#0284c7" />
      <stop offset="50%" stop-color="#38bdf8" />
      <stop offset="100%" stop-color="#ffffff" />
    </linearGradient>

    <!-- Pilot Needle Right Facet -->
    <linearGradient id="needleRight" x1="240" y1="270" x2="355" y2="136" gradientUnits="userSpaceOnUse">
      <stop offset="0%" stop-color="#4338ca" />
      <stop offset="60%" stop-color="#6366f1" />
      <stop offset="100%" stop-color="#e0e7ff" />
    </linearGradient>

    <!-- Continuous Tail Stroke (starts at compass edge 242,270 to 290,290) -->
    <linearGradient id="tailGrad" x1="265" y1="280" x2="416" y2="410" gradientUnits="userSpaceOnUse">
      <stop offset="0%" stop-color="#4f46e5" />
      <stop offset="45%" stop-color="#6366f1" />
      <stop offset="100%" stop-color="#38bdf8" />
    </linearGradient>

    <!-- Inner Orbit Track -->
    <linearGradient id="trackGrad" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0%" stop-color="#38bdf8" stop-opacity="0.8" />
      <stop offset="100%" stop-color="#818cf8" stop-opacity="0.2" />
    </linearGradient>

    <!-- Glow Filter -->
    <filter id="coreGlow" x="-20%" y="-20%" width="140%" height="140%">
      <feGaussianBlur stdDeviation="12" result="blur" />
      <feComposite in="SourceGraphic" in2="blur" operator="over" />
    </filter>

    <!-- Drop Shadow Filter -->
    <filter id="dropShadow" x="-10%" y="-10%" width="120%" height="120%">
      <feDropShadow dx="0" dy="10" stdDeviation="14" flood-color="#000000" flood-opacity="0.6" />
    </filter>
  </defs>

  <!-- Squircle Base Canvas -->
  <rect x="24" y="24" width="464" height="464" rx="108" fill="url(#bgGlow)" stroke="#1e293b" stroke-width="4" filter="url(#dropShadow)" />

  <!-- Ambient Light Reflection Arc -->
  <path d="M 100 160 A 180 180 0 0 1 360 80" stroke="#38bdf8" stroke-width="2" stroke-opacity="0.2" stroke-linecap="round" fill="none" />
  
  <!-- Subtle Data Geometry (Radar / Grid Rings) -->
  <circle cx="248" cy="244" r="148" stroke="#334155" stroke-width="1.5" stroke-dasharray="4 8" stroke-opacity="0.35" fill="none" />
  <circle cx="248" cy="244" r="98" stroke="#334155" stroke-width="1.5" stroke-dasharray="3 6" stroke-opacity="0.25" fill="none" />

  <!-- Precision Cardinal Ticks -->
  <line x1="248" y1="62" x2="248" y2="74" stroke="#38bdf8" stroke-width="3" stroke-linecap="round" />
  <line x1="62" y1="244" x2="74" y2="244" stroke="#38bdf8" stroke-width="3" stroke-linecap="round" />
  <line x1="248" y1="414" x2="248" y2="426" stroke="#6366f1" stroke-width="3" stroke-linecap="round" />
  <line x1="414" y1="244" x2="426" y2="244" stroke="#818cf8" stroke-width="3" stroke-linecap="round" />

  <!-- The "Q" Outer Precision Ring -->
  <circle cx="248" cy="244" r="150" stroke="url(#ringGrad)" stroke-width="26" fill="none" filter="url(#coreGlow)" />

  <!-- Inner Orbit Accent Rings (Database Layers) -->
  <path d="M 148 244 A 98 98 0 0 1 332 176" stroke="url(#trackGrad)" stroke-width="6" stroke-linecap="round" fill="none" />
  <path d="M 168 284 A 98 98 0 0 0 318 314" stroke="url(#trackGrad)" stroke-width="6" stroke-linecap="round" fill="none" />

  <!-- Pilot Navigation Compass Needle -->
  <!-- Left Facet (Cyan Wing) -->
  <polygon points="356,136 182,284 242,270" fill="url(#needleLeft)" filter="url(#dropShadow)" />
  <!-- Right Facet (Indigo Wing) -->
  <polygon points="356,136 242,270 290,290" fill="url(#needleRight)" filter="url(#dropShadow)" />
  <!-- Center Spine Highlight -->
  <line x1="356" y1="136" x2="242" y2="270" stroke="#ffffff" stroke-width="2.5" stroke-linecap="round" stroke-opacity="0.95" />

  <!-- The "Q" Tail: Continues cleanly from the trailing edge of the compass (242,270) - (290,290) -->
  <!-- Zero overlap, perfectly seamless transition through the Q ring -->
  <polygon points="242,270 376,424 416,400 290,290" fill="url(#tailGrad)" filter="url(#coreGlow)" />
  <!-- Tail Top Aerodynamic Highlight -->
  <line x1="290" y1="290" x2="416" y2="400" stroke="#ffffff" stroke-width="2" stroke-linecap="round" stroke-opacity="0.85" />

  <!-- North-East Guidance Beacon (Apex Star) -->
  <circle cx="356" cy="136" r="5" fill="#ffffff" filter="url(#coreGlow)" />
</svg>
"""

# -----------------------------------------------------------------------------
# 2. Transparent Clean Favicon / Mark SVG (512x512)
# -----------------------------------------------------------------------------
FAVICON_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512" width="512" height="512" fill="none">
  <defs>
    <linearGradient id="favRing" x1="100" y1="90" x2="410" y2="410" gradientUnits="userSpaceOnUse">
      <stop offset="0%" stop-color="#38bdf8" />
      <stop offset="50%" stop-color="#6366f1" />
      <stop offset="100%" stop-color="#a855f7" />
    </linearGradient>
    <linearGradient id="favNeedleLeft" x1="160" y1="310" x2="355" y2="145" gradientUnits="userSpaceOnUse">
      <stop offset="0%" stop-color="#0284c7" />
      <stop offset="60%" stop-color="#38bdf8" />
      <stop offset="100%" stop-color="#ffffff" />
    </linearGradient>
    <linearGradient id="favNeedleRight" x1="240" y1="270" x2="355" y2="136" gradientUnits="userSpaceOnUse">
      <stop offset="0%" stop-color="#4338ca" />
      <stop offset="60%" stop-color="#6366f1" />
      <stop offset="100%" stop-color="#e0e7ff" />
    </linearGradient>
    <linearGradient id="favTail" x1="265" y1="280" x2="416" y2="410" gradientUnits="userSpaceOnUse">
      <stop offset="0%" stop-color="#4f46e5" />
      <stop offset="45%" stop-color="#6366f1" />
      <stop offset="100%" stop-color="#38bdf8" />
    </linearGradient>
    <filter id="favGlow" x="-20%" y="-20%" width="140%" height="140%">
      <feGaussianBlur stdDeviation="12" result="blur" />
      <feComposite in="SourceGraphic" in2="blur" operator="over" />
    </filter>
  </defs>

  <!-- Background Base Disc -->
  <circle cx="256" cy="256" r="236" fill="#0f172a" stroke="#1e293b" stroke-width="8" />

  <!-- The "Q" Outer Precision Ring -->
  <circle cx="256" cy="250" r="150" stroke="url(#favRing)" stroke-width="28" fill="none" filter="url(#favGlow)" />

  <!-- Internal Orbit Arc -->
  <path d="M 158 250 A 98 98 0 0 1 336 182" stroke="#38bdf8" stroke-width="8" stroke-linecap="round" stroke-opacity="0.6" fill="none" />

  <!-- Pilot Compass Needle -->
  <polygon points="364,142 190,290 250,276" fill="url(#favNeedleLeft)" />
  <polygon points="364,142 250,276 298,296" fill="url(#favNeedleRight)" />
  <line x1="364" y1="142" x2="250" y2="276" stroke="#ffffff" stroke-width="3" stroke-linecap="round" />

  <!-- Continuous Tail: starts from compass edge (250,276) - (298,296) -->
  <polygon points="250,276 384,430 424,406 298,296" fill="url(#favTail)" />
  <line x1="298" y1="296" x2="424" y2="406" stroke="#ffffff" stroke-width="2.5" stroke-linecap="round" stroke-opacity="0.85" />

  <!-- Beacon Star -->
  <circle cx="364" cy="142" r="6" fill="#ffffff" />
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
    <linearGradient id="needleRB" x1="120" y1="130" x2="180" y2="70" gradientUnits="userSpaceOnUse">
      <stop offset="0%" stop-color="#4338ca" />
      <stop offset="60%" stop-color="#6366f1" />
      <stop offset="100%" stop-color="#e0e7ff" />
    </linearGradient>
    <linearGradient id="tailGradB" x1="130" y1="140" x2="202" y2="200" gradientUnits="userSpaceOnUse">
      <stop offset="0%" stop-color="#4f46e5" />
      <stop offset="45%" stop-color="#6366f1" />
      <stop offset="100%" stop-color="#38bdf8" />
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
    <circle cx="120" cy="120" r="72" stroke="#334155" stroke-width="1.2" stroke-dasharray="3 5" stroke-opacity="0.4" fill="none" />
    <!-- Q Ring -->
    <circle cx="120" cy="120" r="74" fill="none" stroke="url(#ringGradB)" stroke-width="13" filter="url(#glowB)" />
    <!-- Orbit Arc -->
    <path d="M 75 120 A 45 45 0 0 1 155 90" stroke="#38bdf8" stroke-width="3.5" stroke-linecap="round" stroke-opacity="0.6" fill="none" />
    <!-- Needle Left Wing -->
    <polygon points="172,70 90,140 120,132" fill="url(#needleLB)" />
    <!-- Needle Right Wing -->
    <polygon points="172,70 120,132 144,142" fill="url(#needleRB)" />
    <line x1="172" y1="70" x2="120" y2="132" stroke="#ffffff" stroke-width="1.5" stroke-linecap="round" />
    <!-- Q Tail (Continues from compass edge 120,132 to 144,142) -->
    <polygon points="120,132 184,206 202,194 144,142" fill="url(#tailGradB)" filter="url(#glowB)" />
    <line x1="144" y1="142" x2="202" y2="194" stroke="#ffffff" stroke-width="1.2" stroke-opacity="0.85" />
    <!-- Beacon -->
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
