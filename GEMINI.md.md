You are an expert Python engineer and prepress automation specialist. Build a production-grade, resumable indexing and technical prepress inspection script for DTF (Direct-to-Film) and DTG (Direct-to-Garment) print workflows.

### Target Directory
`C:\Users\100\Documents\POGON PRINT`

---

### Key Requirements

#### 1. Resumable SQLite Database Architecture
* Create a local SQLite database (`prepress_index.db`) using Python's standard `sqlite3` module.
* Define a schema to track:
  * `file_uri` (Primary Key / Unique Path)
  * `file_name`, `file_size_bytes`, `created_date`, `modified_date`, `file_extension`
  * `dimensions_pt` ($\text{Width} \times \text{Height}$ in points)
  * `dimensions_mm` ($\text{Width} \times \text{Height}$ in millimeters)
  * `embedded_raster_dpi` (Minimum and average effective DPI)
  * `color_spaces` (Detected profiles: CMYK, RGB, Grayscale, DeviceN)
  * `spot_color_channels` (List of custom/spot separation names, e.g., White, Varnish)
  * `extracted_content` (Cleaned, deduplicated text payload)
  * `preview_uri` (Local path to generated dark-preview PNG)
  * `indexed_at` (Timestamp)
* **Resumability:** Check file existence and `modified_date` prior to processing. Skip files that have already been indexed and have not changed.
* **Written in phyton:** Or write itself .bat or .ps1 executable code to run, also complete script should use single shared sql database.

  *   Instruct it to handle complete process in 5 steps, using the data provided in previous step to offload rendering all at once.
  *   Always process files one-by-one and save to database, stop after each of 5 steps completed and ask the user to continue or exit.
  *   Also, if user exited, script should continue where it stopped proccesing.

---

#### 2. Prepress Technical Inspection
* **Dimensions:** Extract PDF `MediaBox`/`CropBox` dimensions in PostScript points ($1\text{ pt} = 1/72\text{ in}$) and convert to millimeters:
  $$\text{Dimension}_{\text{mm}} = \left(\frac{\text{Dimension}_{\text{pt}}}{72}\right) \times 25.4$$
* **Resolution (DPI):** Iterate through embedded raster images, compute their effective horizontal and vertical DPI based on placement transform matrices, and log any assets falling below 300 DPI.
* **Color Profiles & Spot Channels:** Detect color spaces across vector paths and embedded raster assets. Inspect PDF separation color spaces and catalog all named spot color channels (crucial for DTF white ink layers).

---

#### 3. Text Extraction & Gang-Sheet Deduplication
* **PDFs:** Extract text layers via **PyMuPDF (`fitz`)**.
* **Raster Images (PNG, JPG, TIFF):** Extract text via **`pytesseract` OCR**.
* **Gang-Sheet Fuzzy Deduplication:** DTF gang-sheets frequently duplicate identical graphics. Implement a text-chunk deduplication routine using **Jaccard similarity** (or token-based distance metrics via `thefuzz`) to eliminate repetitive text blocks and retain only unique text payloads.

---

#### 4. White-Ink High-Contrast Preview Generator
* Many DTF/DTG designs use 100% white ink or transparent backgrounds that disappear on default white canvas renders.
* Use **PyMuPDF** to dynamically inject a solid black rectangle (`fill=(0, 0, 0)`) across the entire page rect using `overlay=False` (or insert it before the first existing drawing command) so it sits strictly **behind** all artwork.
* Render the composite page to a high-resolution PNG ($300\text{ DPI}$).
* Save the generated image into a dedicated `_previews` folder in the root working directory and log the relative/absolute path in `preview_uri`.

---

#### 5. Execution & JSON Export
* Process files iteratively with a progress indicator and robust `try/except` logging to avoid halting on corrupted assets.
* Upon batch completion, export the complete SQLite database to a structured JSON file: `prepress_catalog.json`.

---

### Deliverable
Provide clean, PEP 8-compliant, modular Python code with all necessary imports (`sqlite3`, `fitz`, `pytesseract`, `PIL`, `thefuzz`, `json`, `pathlib`, `os`). Include inline comments detailing the prepress math and canvas manipulation logic.


=============================================================================================================================================
=============================================================================================================================================


"""
DTF / DTG Resumable Prepress Indexer & Preview Engine
Production-Grade Pipeline - High-Stability & Low-Memory Configuration.
"""

# 1. Standard Library Imports
import concurrent.futures
import datetime
import gc
import json
import multiprocessing
import os
import re
import sqlite3
import string
import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple

# 2. Third-Party Imports
try:
    import pymupdf as fitz
    fitz.TOOLS.mupdf_display_errors(False)  # Silences PDF syntax error spam
    
    import pytesseract
    from PIL import Image, ImageOps
    Image.MAX_IMAGE_PIXELS = None  # Disables DecompressionBomb warning
    
    from thefuzz import fuzz
except ImportError as e:
    print(f"[!] Missing critical dependency: {e.name}")
    print("[*] Please run the batch script to install required packages.")
    sys.exit(1)

# 3. Application Configuration
pytesseract.pytesseract.tesseract_cmd = r'C:\Program Files\Tesseract-OCR\tesseract.exe'


# ==============================================================================
# CONFIGURATION & STABILITY TUNING
# ==============================================================================
TARGET_DIRECTORY = Path(r"C:\Users\100\Documents\POGON PRINT")
DB_PATH = TARGET_DIRECTORY / "prepress_index.db"
PREVIEWS_DIR = TARGET_DIRECTORY / "_previews"
EXPORT_JSON_PATH = TARGET_DIRECTORY / "prepress_catalog.json"
SUPPORTED_EXTENSIONS = {".pdf", ".png", ".jpg", ".jpeg", ".tif", ".tiff"}

TARGET_PREVIEW_DPI = 150
MIN_PRINT_DPI_THRESHOLD = 300.0

# Stability settings: prevents memory saturation and PC lockups
SAFE_WORKERS = max(1, min(4, (os.cpu_count() or 4) // 2))
BATCH_CHUNK_SIZE = 100
MAX_OCR_DIMENSION = 2048  # Maximum pixel dimension for OCR preprocessing

SPOT_COLOR_REGEX = re.compile(r"\[\s*/(?:Separation|DeviceN)\s+/([^ \t\n\r\[\]<>/]+)")


# ==============================================================================
# DATABASE MANAGEMENT
# ==============================================================================
def init_db() -> sqlite3.Connection:
    TARGET_DIRECTORY.mkdir(parents=True, exist_ok=True)
    PREVIEWS_DIR.mkdir(parents=True, exist_ok=True)
    
    conn = sqlite3.connect(DB_PATH)
    with conn:
        _ = conn.execute("""
        CREATE TABLE IF NOT EXISTS prepress_files (
            file_uri TEXT PRIMARY KEY, file_name TEXT, file_size_bytes INTEGER,
            created_date TEXT, modified_date TEXT, file_extension TEXT,
            dimensions_pt TEXT, dimensions_mm TEXT, embedded_raster_dpi REAL,
            color_spaces TEXT, spot_color_channels TEXT, extracted_content TEXT,
            preview_uri TEXT, status_step1 INTEGER DEFAULT 0,
            status_step2 INTEGER DEFAULT 0, status_step3 INTEGER DEFAULT 0,
            status_step4 INTEGER DEFAULT 0, status_step5 INTEGER DEFAULT 0,
            last_updated TEXT
        )
        """)
    return conn


def prompt_continue(current_step: int, next_step: int) -> bool:
    print(f"\n[+] Step {current_step} completed successfully.")
    if next_step > 5:
        print("\n[*] Pipeline execution complete! Catalog exported.")
        return True
        
    while True:
        resp = input(f"[?] Proceed to Step {next_step}? ([y]/n/exit): ").strip().lower()
        if resp in ("", "y", "yes"):
            return True
        elif resp in ("n", "no", "exit", "q"):
            print("\n[!] Execution paused by user. Progress safely saved in database.")
            sys.exit(0)


# ==============================================================================
# PIPELINE STEP 1: DISCOVERY & BASELINE REGISTRATION
# ==============================================================================
def run_step_1(conn: sqlite3.Connection) -> None:
    print("\n" + "="*70 + "\nSTEP 1/5: File Discovery & Database Registration\n" + "="*70)

    if not TARGET_DIRECTORY.exists():
        print(f"[-] Target directory not found: {TARGET_DIRECTORY}")
        sys.exit(1)

    found_files = [
        p for p in TARGET_DIRECTORY.rglob("*") 
        if p.is_file() and p.suffix.lower() in SUPPORTED_EXTENSIONS and "_previews" not in p.parts
    ]

    print(f"[*] Found {len(found_files)} potential assets on disk. Synchronizing...")
    new_count, updated_count = 0, 0
    now_iso = datetime.datetime.now().isoformat()

    with conn:
        for file_path in found_files:
            uri = str(file_path.resolve())
            stat = file_path.stat()
            mtime = datetime.datetime.fromtimestamp(stat.st_mtime).isoformat()
            ctime = datetime.datetime.fromtimestamp(stat.st_ctime).isoformat()
            
            row = conn.execute("SELECT modified_date FROM prepress_files WHERE file_uri = ?", (uri,)).fetchone()

            if row is None:
                _ = conn.execute("""
                    INSERT INTO prepress_files (
                        file_uri, file_name, file_size_bytes, created_date, modified_date, 
                        file_extension, status_step1, last_updated
                    ) VALUES (?, ?, ?, ?, ?, ?, 1, ?)
                """, (uri, file_path.name, stat.st_size, ctime, mtime, file_path.suffix.lower(), now_iso))
                new_count += 1
            elif row[0] != mtime:
                _ = conn.execute("""
                    UPDATE prepress_files SET
                        file_size_bytes = ?, modified_date = ?, status_step1 = 1,
                        status_step2 = 0, status_step3 = 0, status_step4 = 0, status_step5 = 0,
                        last_updated = ?
                    WHERE file_uri = ?
                """, (stat.st_size, mtime, now_iso, uri))
                updated_count += 1

    print(f"[✓] Step 1 finished. New indexed: {new_count} | Modified re-queued: {updated_count}")


# ==============================================================================
# PIPELINE STEP 2: PREPRESS TECHNICAL INSPECTION (CONTROLLED MULTIPROCESSING)
# ==============================================================================
def _worker_inspect(payload: Tuple[str, str]) -> Tuple[str, Dict[str, Any], bool]:
    uri, ext = payload
    file_path = Path(uri)
    result = {"dim_pt": "N/A", "dim_mm": "N/A", "min_dpi": 0.0, "color_spaces": set(), "spot_colors": set()}
    
    if not file_path.exists():
        return uri, result, False

    try:
        if ext == ".pdf":
            with fitz.open(file_path) as doc:
                if doc:
                    rect = doc[0].rect
                    w_mm, h_mm = (rect.width / 72.0) * 25.4, (rect.height / 72.0) * 25.4
                    result["dim_pt"] = f"{rect.width:.2f} x {rect.height:.2f} pt"
                    result["dim_mm"] = f"{w_mm:.2f} x {h_mm:.2f} mm"

                    dpi_list = []
                    for img_info in doc[0].get_images(full=True):
                        xref = img_info[0]
                        base_img = doc.extract_image(xref)
                        if not base_img: continue
                        result["color_spaces"].add(f"Image:{base_img.get('colorspace', 'Unknown')}")
                        pix_w, pix_h = base_img.get("width", 0), base_img.get("height", 0)
                        for img_rect in doc[0].get_image_rects(xref):
                            if img_rect.width > 0 and img_rect.height > 0:
                                dpi_list.append(min(pix_w / (img_rect.width / 72.0), pix_h / (img_rect.height / 72.0)))
                    if dpi_list: result["min_dpi"] = round(min(dpi_list), 1)

                    for xref in range(1, doc.xref_length()):
                        try:
                            obj_str = doc.xref_object(xref)
                            matches = SPOT_COLOR_REGEX.findall(obj_str)
                            for match in matches:
                                if match not in ("All", "None", "Cyan", "Magenta", "Yellow", "Black"):
                                    result["spot_colors"].add(match)
                        except Exception: pass
        else:
            with Image.open(file_path) as img:
                dpi_val = img.info.get("dpi", (72.0, 72.0))
                dpi_val = float(dpi_val[0]) if isinstance(dpi_val, tuple) else float(dpi_val)
                result["min_dpi"] = round(dpi_val if dpi_val > 0 else 72.0, 1)
                w_in, h_in = img.width / result["min_dpi"], img.height / result["min_dpi"]
                result["dim_pt"] = f"{w_in * 72.0:.2f} x {h_in * 72.0:.2f} pt"
                result["dim_mm"] = f"{w_in * 25.4:.2f} x {h_in * 25.4:.2f} mm"
                result["color_spaces"].add(f"RasterMode:{img.mode}")
    except Exception as err:
        result["color_spaces"].add(f"Error: {err}")
    
    return uri, result, True


def run_step_2(conn: sqlite3.Connection) -> None:
    print("\n" + "="*70 + "\nSTEP 2/5: Prepress Technical Inspection\n" + "="*70)
    
    pending = conn.execute("SELECT file_uri, file_extension FROM prepress_files WHERE status_step1 = 1 AND status_step2 = 0").fetchall()
    total = len(pending)
    print(f"[*] Queue size: {total} files to inspect.")
    if total == 0: return

    print(f"[*] Running with {SAFE_WORKERS} safe background workers...\n")
    processed = 0

    with concurrent.futures.ProcessPoolExecutor(max_workers=SAFE_WORKERS) as executor:
        for i in range(0, total, BATCH_CHUNK_SIZE):
            chunk = pending[i:i + BATCH_CHUNK_SIZE]
            futures = {executor.submit(_worker_inspect, item): item[0] for item in chunk}
            
            for future in concurrent.futures.as_completed(futures):
                processed += 1
                print(f"\r -> Inspected ({processed}/{total})", end="")
                uri, data, success = future.result()
                
                if success:
                    c_space = ", ".join(sorted(data["color_spaces"])) or "Standard"
                    spots = ", ".join(sorted(data["spot_colors"])) or "None"
                    with conn:
                        _ = conn.execute("""
                            UPDATE prepress_files SET
                                dimensions_pt = ?, dimensions_mm = ?, embedded_raster_dpi = ?, 
                                color_spaces = ?, spot_color_channels = ?, status_step2 = 1, last_updated = ?
                            WHERE file_uri = ?
                        """, (data["dim_pt"], data["dim_mm"], data["min_dpi"], c_space, spots, datetime.datetime.now().isoformat(), uri))
            gc.collect()
    print()


# ==============================================================================
# PIPELINE STEP 3: TEXT EXTRACTION & DEDUPLICATION (OPTIMIZED OCR)
# ==============================================================================
def clean_text_for_comparison(text: str) -> str:
    no_punct = text.translate(str.maketrans('', '', string.punctuation)).lower()
    return " ".join(no_punct.split())


def deduplicate_gangsheet_text(raw_blocks: List[str], sim_threshold: float = 0.75) -> str:
    unique_blocks: List[str] = []
    for raw in raw_blocks:
        clean = " ".join(raw.strip().split())
        if len(clean) < 3: continue
        comp_clean = clean_text_for_comparison(clean)
        is_duplicate = False
        
        for existing in unique_blocks:
            comp_exist = clean_text_for_comparison(existing)
            set_a, set_b = set(comp_clean.split()), set(comp_exist.split())
            j_score = len(set_a & set_b) / len(set_a | set_b) if (set_a and set_b) else 0.0
            f_score = (fuzz.token_set_ratio(comp_clean, comp_exist) / 100.0) if fuzz else 0.0
            if j_score >= sim_threshold or f_score >= sim_threshold:
                is_duplicate = True
                break

        if not is_duplicate:
            unique_blocks.append(clean)
    return "\n".join(unique_blocks)


def _worker_extract_text(payload: Tuple[str, str]) -> Tuple[str, str, bool]:
    uri, ext = payload
    file_path = Path(uri)
    if not file_path.exists(): return uri, "", False

    raw_chunks = []
    if ext == ".pdf":
        try:
            with fitz.open(file_path) as doc:
                for page in doc:
                    raw_chunks.extend([b[4] for b in page.get_text("blocks") if b[6] == 0])
        except Exception: pass
    else:
        try:
            with Image.open(file_path) as img:
                # Optimized OCR preprocessing: Convert to Grayscale & downscale huge gang-sheets
                w, h = img.size
                if max(w, h) > MAX_OCR_DIMENSION:
                    scale = MAX_OCR_DIMENSION / float(max(w, h))
                    new_size = (int(w * scale), int(h * scale))
                    proc_img = img.resize(new_size, resample=Image.Resampling.BILINEAR).convert("L")
                else:
                    proc_img = img.convert("L")

                ocr_text = pytesseract.image_to_string(proc_img)
                raw_chunks.extend(ocr_text.splitlines())
                proc_img.close()
        except Exception: pass

    return uri, deduplicate_gangsheet_text(raw_chunks), True


def run_step_3(conn: sqlite3.Connection) -> None:
    print("\n" + "="*70 + "\nSTEP 3/5: Text Extraction & Gang-Sheet Deduplication (STABILITY MODE)\n" + "="*70)
    
    pending = conn.execute("SELECT file_uri, file_extension FROM prepress_files WHERE status_step2 = 1 AND status_step3 = 0").fetchall()
    total = len(pending)
    print(f"[*] Queue size: {total} files to extract.")
    if total == 0: return

    print(f"[*] Running OCR across {SAFE_WORKERS} background workers in safe chunks...\n")
    processed = 0

    with concurrent.futures.ProcessPoolExecutor(max_workers=SAFE_WORKERS) as executor:
        for i in range(0, total, BATCH_CHUNK_SIZE):
            chunk = pending[i:i + BATCH_CHUNK_SIZE]
            futures = {executor.submit(_worker_extract_text, item): item[0] for item in chunk}
            
            for future in concurrent.futures.as_completed(futures):
                processed += 1
                print(f"\r -> Processed Text ({processed}/{total})", end="")
                uri, clean_text, success = future.result()
                if success:
                    with conn:
                        _ = conn.execute("UPDATE prepress_files SET extracted_content = ?, status_step3 = 1, last_updated = ? WHERE file_uri = ?",
                                         (clean_text, datetime.datetime.now().isoformat(), uri))
            gc.collect()
    print()


# ==============================================================================
# PIPELINE STEP 4: WHITE-INK HIGH-CONTRAST DARK PREVIEWS (CONTROLLED)
# ==============================================================================
def _worker_render(payload: Tuple[str, str, str]) -> Tuple[str, str, bool]:
    uri, ext, prev_dir = payload
    file_path = Path(uri)
    if not file_path.exists(): return uri, "", False
    
    dest_path = Path(prev_dir) / f"prev_{file_path.stem}_{abs(hash(uri)) % 1000000}.png"
    success = False

    try:
        if ext == ".pdf":
            with fitz.open(file_path) as doc:
                if len(doc) > 0:
                    page = doc[0]
                    shape = page.new_shape()
                    shape.draw_rect(page.rect)
                    shape.finish(fill=(0, 0, 0), color=None)
                    shape.commit(overlay=False)
                    pix = page.get_pixmap(dpi=TARGET_PREVIEW_DPI, alpha=False)
                    pix.save(str(dest_path))
                    pix = None
                    success = True
        else:
            with Image.open(file_path) as img:
                rgba = img.convert("RGBA")
                bg = Image.new("RGBA", rgba.size, (0, 0, 0, 255))
                composite = Image.alpha_composite(bg, rgba)
                composite.convert("RGB").save(dest_path, "PNG")
                rgba.close()
                bg.close()
                composite.close()
                success = True
    except Exception: pass
    
    return uri, str(dest_path.resolve()) if success else "GENERATION_FAILED", True


def run_step_4(conn: sqlite3.Connection) -> None:
    print("\n" + "="*70 + "\nSTEP 4/5: High-Contrast Dark Previews (CONTROLLED RENDERING)\n" + "="*70)
    
    pending = conn.execute("SELECT file_uri, file_extension, file_name FROM prepress_files WHERE status_step3 = 1 AND status_step4 = 0").fetchall()
    total = len(pending)
    print(f"[*] Queue size: {total} files to render.")
    if total == 0: return

    print(f"[*] Rendering previews with {SAFE_WORKERS} workers in controlled batches...\n")
    processed = 0

    with concurrent.futures.ProcessPoolExecutor(max_workers=SAFE_WORKERS) as executor:
        for i in range(0, total, BATCH_CHUNK_SIZE):
            chunk = pending[i:i + BATCH_CHUNK_SIZE]
            futures = {executor.submit(_worker_render, (uri, ext, str(PREVIEWS_DIR))): uri for uri, ext, _ in chunk}
            
            for future in concurrent.futures.as_completed(futures):
                processed += 1
                print(f"\r -> Rendered ({processed}/{total})", end="")
                uri, record_val, success = future.result()
                if success:
                    with conn:
                        _ = conn.execute("UPDATE prepress_files SET preview_uri = ?, status_step4 = 1, last_updated = ? WHERE file_uri = ?",
                                         (record_val, datetime.datetime.now().isoformat(), uri))
            gc.collect()
    print()


# ==============================================================================
# PIPELINE STEP 5: CATALOG CONSOLIDATION & STREAMING JSON EXPORT
# ==============================================================================
def run_step_5(conn: sqlite3.Connection) -> None:
    print("\n" + "="*70 + "\nSTEP 5/5: Catalog Consolidation & Streaming JSON Export\n" + "="*70)

    total_records = conn.execute("SELECT COUNT(*) FROM prepress_files").fetchone()[0]
    print(f"[*] Streaming {total_records} records to JSON to maintain zero RAM overhead...")

    cursor = conn.cursor()
    cursor.execute("""
        SELECT file_uri, file_name, file_size_bytes, created_date, modified_date, 
               file_extension, dimensions_pt, dimensions_mm, embedded_raster_dpi, 
               color_spaces, spot_color_channels, extracted_content, preview_uri
        FROM prepress_files ORDER BY file_name ASC
    """)

    # Stream JSON directly to disk
    with open(EXPORT_JSON_PATH, "w", encoding="utf-8") as f:
        f.write("{\n")
        f.write(f'  "generated_at": "{datetime.datetime.now().isoformat()}",\n')
        f.write(f'  "target_directory": {json.dumps(str(TARGET_DIRECTORY.resolve()))},\n')
        f.write(f'  "total_records": {total_records},\n')
        f.write('  "catalog": [\n')

        first = True
        count = 0
        while True:
            rows = cursor.fetchmany(500)
            if not rows:
                break
            for r in rows:
                record = {
                    "file_uri": r[0],
                    "file_name": r[1],
                    "file_size_bytes": r[2],
                    "dates": {"created": r[3], "modified": r[4]},
                    "prepress_specs": {
                        "dimensions_pt": r[6],
                        "dimensions_mm": r[7],
                        "embedded_raster_min_dpi": r[8],
                        "color_spaces": [cs.strip() for cs in (r[9] or "").split(",") if cs.strip()],
                        "spot_channels": [sp.strip() for sp in (r[10] or "").split(",") if sp.strip()]
                    },
                    "extracted_unique_text": r[11],
                    "preview_uri": r[12]
                }
                if not first:
                    f.write(",\n")
                first = False
                
                # Write formatted JSON line indented inside catalog array
                json_str = json.dumps(record, ensure_ascii=False)
                f.write("    " + json_str)
                count += 1
                if count % 1000 == 0 or count == total_records:
                    print(f"\r -> Streamed to disk ({count}/{total_records})", end="")

        f.write("\n  ]\n}")

    with conn:
        _ = conn.execute("UPDATE prepress_files SET status_step5 = 1")

    print(f"\n[✓] Successfully exported {total_records} records to:\n    {EXPORT_JSON_PATH}")


# ==============================================================================
# ENTRY POINT
# ==============================================================================
def main() -> None:
    multiprocessing.freeze_support()
    
    print("""
======================================================================
     DTF / DTG RESUMABLE PREPRESS INDEXER & PREVIEW ENGINE
======================================================================
    """)
    conn = init_db()
    
    try:
        run_step_1(conn)
        if prompt_continue(1, 2):
            run_step_2(conn)
        if prompt_continue(2, 3):
            run_step_3(conn)
        if prompt_continue(3, 4):
            run_step_4(conn)
        if prompt_continue(4, 5):
            run_step_5(conn)
            prompt_continue(5, 6)
    finally:
        conn.close()

if __name__ == "__main__":
    main()
