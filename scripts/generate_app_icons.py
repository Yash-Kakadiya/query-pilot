"""Script to generate all desktop app icons and frontend logo assets from the master logo image.
"""
from pathlib import Path
import base64
from PIL import Image

def generate_icons():
    repo_root = Path(__file__).resolve().parent.parent
    master_logo_path = Path(r"C:\Users\YASH\.gemini\antigravity\brain\ebdd2fa6-6fa4-4ea2-bbcc-9e27a2f2a4b8\.user_uploaded\media_1794572259528_350c78ed.jpg")
    
    if not master_logo_path.is_file():
        raise FileNotFoundError(f"Master logo not found at {master_logo_path}")

    img = Image.open(master_logo_path).convert("RGBA")
    print(f"Loaded master logo: {img.size}")

    # 1. Update src-tauri/icons
    icons_dir = repo_root / "src-tauri" / "icons"
    icons_dir.mkdir(parents=True, exist_ok=True)

    sizes = {
        "32x32.png": (32, 32),
        "64x64.png": (64, 64),
        "128x128.png": (128, 128),
        "128x128@2x.png": (256, 256),
        "256x256.png": (256, 256),
        "icon.png": (512, 512),
        "Square30x30Logo.png": (30, 30),
        "Square44x44Logo.png": (44, 44),
        "Square71x71Logo.png": (71, 71),
        "Square89x89Logo.png": (89, 89),
        "Square107x107Logo.png": (107, 107),
        "Square142x142Logo.png": (142, 142),
        "Square150x150Logo.png": (150, 150),
        "Square284x284Logo.png": (284, 284),
        "Square310x310Logo.png": (310, 310),
        "StoreLogo.png": (50, 50),
    }

    for filename, size in sizes.items():
        resized = img.resize(size, Image.Resampling.LANCZOS)
        out_path = icons_dir / filename
        resized.save(out_path, "PNG", optimize=True)
        print(f"Saved {out_path} ({size})")

    # Generate multi-size icon.ico
    ico_sizes = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
    ico_path = icons_dir / "icon.ico"
    img.save(ico_path, format="ICO", sizes=ico_sizes)
    print(f"Saved Windows ICO: {ico_path}")

    # 2. Update frontend public assets
    public_dir = repo_root / "frontend" / "public"
    public_dir.mkdir(parents=True, exist_ok=True)
    img.resize((192, 192), Image.Resampling.LANCZOS).save(public_dir / "logo192.png", "PNG", optimize=True)
    img.resize((512, 512), Image.Resampling.LANCZOS).save(public_dir / "logo512.png", "PNG", optimize=True)
    img.resize((64, 64), Image.Resampling.LANCZOS).save(public_dir / "favicon.png", "PNG", optimize=True)
    img.save(public_dir / "favicon.ico", format="ICO", sizes=[(16, 16), (32, 32), (48, 48)])

    # 3. Base64 for QueryPilotLogo.tsx
    logo_for_header = img.resize((128, 128), Image.Resampling.LANCZOS)
    header_png_path = icons_dir / "header_logo_temp.png"
    logo_for_header.save(header_png_path, "PNG", optimize=True)
    b64_data = base64.b64encode(header_png_path.read_bytes()).decode("ascii")
    header_png_path.unlink()

    logo_tsx_path = repo_root / "frontend" / "src" / "components" / "QueryPilotLogo.tsx"
    tsx_content = f'''import React from 'react'

interface QueryPilotLogoProps {{
  size?: number
  className?: string
  alt?: string
}}

const EMBEDDED_LOGO_SRC =
  'data:image/png;base64,{b64_data}'

export const QueryPilotLogo: React.FC<QueryPilotLogoProps> = ({{
  size = 36,
  className = '',
  alt = 'QueryPilot Logo',
}}) => {{
  return (
    <img
      src={{EMBEDDED_LOGO_SRC}}
      width={{size}}
      height={{size}}
      alt={{alt}}
      className={{`querypilot-brand-logo ${{className}}`.trim()}}
      style={{{{
        display: 'inline-block',
        verticalAlign: 'middle',
        borderRadius: `${{Math.round(size * 0.22)}}px`,
        objectFit: 'contain',
      }}}}
    />
  )
}}
'''
    logo_tsx_path.write_text(tsx_content, encoding="utf-8")
    print(f"Updated {logo_tsx_path} with fresh embedded logo data URI.")

    # 4. Create high-resolution Banner (1200 x 400) for GitHub / Docs
    banner_width = 1200
    banner_height = 420
    banner = Image.new("RGBA", (banner_width, banner_height), (10, 14, 26, 255))
    
    # Place glowing logo on the left
    logo_banner_size = 280
    logo_banner = img.resize((logo_banner_size, logo_banner_size), Image.Resampling.LANCZOS)
    logo_y = (banner_height - logo_banner_size) // 2
    banner.paste(logo_banner, (80, logo_y), logo_banner)

    # Let's save the master logo and banner in assets/
    assets_dir = repo_root / "assets"
    assets_dir.mkdir(parents=True, exist_ok=True)
    img.save(assets_dir / "logo.png", "PNG", optimize=True)
    
    # Let's also draw banner typography using PIL ImageDraw
    from PIL import ImageDraw, ImageFont
    draw = ImageDraw.Draw(banner)
    
    # Gradient line or accent glow
    draw.rectangle([(0, 0), (banner_width, 4)], fill=(0, 216, 255, 255))
    draw.rectangle([(0, banner_height - 4), (banner_width, banner_height)], fill=(147, 51, 234, 255))
    
    # Text
    try:
        font_title = ImageFont.truetype("arialbd.ttf", 64)
        font_sub = ImageFont.truetype("arial.ttf", 26)
        font_tags = ImageFont.truetype("arialbd.ttf", 18)
    except Exception:
        font_title = ImageFont.load_default()
        font_sub = font_title
        font_tags = font_title

    text_x = 400
    draw.text((text_x, 110), "QueryPilot", fill=(240, 246, 255, 255), font=font_title)
    draw.text((text_x, 195), "AI-Powered SQL Copilot & Database Companion", fill=(148, 163, 184, 255), font=font_sub)
    draw.text((text_x, 245), "Cloud MSSQL (MonsterASP.net)  *  Local MSSQL  *  SQLite", fill=(56, 189, 248, 255), font=font_tags)
    draw.text((text_x, 275), "Natural Language to SQL  *  Read-Only Safety  *  Offline Ready", fill=(192, 132, 252, 255), font=font_tags)

    banner_path = assets_dir / "querypilot-banner.png"
    banner.save(banner_path, "PNG", optimize=True)
    print(f"Saved banner to {banner_path}")

if __name__ == "__main__":
    generate_icons()
