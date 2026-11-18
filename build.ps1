# Builds dist\Pressroom\Pressroom.exe (folder build: starts in seconds; no console window).
$ErrorActionPreference = "Continue"  # PyInstaller logs to stderr; rely on exit codes instead
Set-Location $PSScriptRoot
$py = ".\.venv\Scripts\python.exe"

# App icon from the logo mark.
& $py -c @"
from PIL import Image, ImageDraw
S = 256; im = Image.new('RGBA', (S, S), (0, 0, 0, 0)); d = ImageDraw.Draw(im)
d.rounded_rectangle([8, 8, S - 8, S - 8], 48, fill=(5, 8, 15, 255))
c = S // 2
d.arc([c - 88, c - 88, c + 88, c + 88], 20, 330, fill=(59, 227, 255), width=12)
d.ellipse([c - 46, c - 46, c + 46, c + 46], outline=(142, 107, 255), width=10)
for a, b in [((c, 22), (c, 78)), ((c, S - 78), (c, S - 22)), ((22, c), (78, c)), ((S - 78, c), (S - 22, c))]:
    d.line([a, b], fill=(59, 227, 255), width=10)
d.ellipse([c - 16, c - 16, c + 16, c + 16], fill=(255, 61, 154))
im.save('pressroom.ico', sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
"@

& $py -m PyInstaller --noconfirm --clean --onedir --windowed --name Pressroom --icon pressroom.ico `
  --add-data "ui;ui" `
  --exclude-module onnxruntime --exclude-module pymupdf4llm --exclude-module pymupdf_layout `
  --exclude-module networkx --exclude-module tabulate --exclude-module numpy --exclude-module tkinter `
  run_pressroom.py 2>&1 | ForEach-Object { "$_" } | Select-String -Pattern "ERROR|WARNING: lib not found|completed successfully"
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed with exit code $LASTEXITCODE" }

Write-Host "Built: $(Resolve-Path dist\Pressroom\Pressroom.exe)"
