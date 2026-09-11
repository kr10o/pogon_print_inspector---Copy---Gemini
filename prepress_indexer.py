"""
DTF / DTG Resumable Prepress Indexer & Preview Engine
Production-Grade Pipeline - High-Stability & Low-Memory Configuration.
Refined Crawling, Indexing, Pure White Logo Extraction, and Vector Group Decomposition.
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
from typing import Any, Dict, List, Optional, Set, Tuple

# Ensure Windows stdout handles UTF-8 (Croatian characters like č, ć, ž, š, đ)
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# 2. Third-Party Imports
try:
    import pymupdf as fitz
    fitz.TOOLS.mupdf_display_errors(False)  # Silences PDF syntax error spam
    
    import pytesseract
    from PIL import Image, ImageOps
    Image.MAX_IMAGE_PIXELS = None  # Disables DecompressionBomb warning
    
    try:
        from thefuzz import fuzz
    except ImportError:
        fuzz = None
except ImportError as e:
    print(f"[!] Missing critical dependency: {e.name}")
    print("[*] Please run run_prepress.bat to install required packages inside .venv.")
    sys.exit(1)

# 3. Application Configuration
pytesseract.pytesseract.tesseract_cmd = r'C:\Program Files\Tesseract-OCR\tesseract.exe'


# ==============================================================================
# CONFIGURATION & STABILITY TUNING
# ==============================================================================
TARGET_DIRECTORY = Path(r"C:\Users\100\Documents\POGON PRINT")
DB_PATH = TARGET_DIRECTORY / "prepress_index.db"
PREVIEWS_DIR = TARGET_DIRECTORY / "_previews"
EXTRACTED_GROUPS_DIR = TARGET_DIRECTORY / "_extracted_groups"
EXPORT_JSON_PATH = TARGET_DIRECTORY / "prepress_catalog.json"

SUPPORTED_EXTENSIONS = {".pdf", ".ai", ".svg", ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".webp"}
IGNORE_DIRS = {"_previews", "_extracted_groups", ".venv", "__pycache__", ".git", ".idea", ".vscode"}
IGNORE_EXACT_NAMES = {"thumbs.db", "desktop.ini"}

TARGET_PREVIEW_DPI = 150
MAX_PREVIEW_PIXELS = 2560  # Maximum dimension in pixels for preview rendering (prevents OOM)
MIN_PRINT_DPI_THRESHOLD = 300.0

# Oversized thresholds triggering vector group decomposition
OVERSIZED_DIMENSION_PT = 2500.0   # ~880 mm / 35 inches
OVERSIZED_FILE_SIZE_BYTES = 80 * 1024 * 1024  # 80 MB
OVERSIZED_DRAWINGS_COUNT = 4000

# Stability settings: prevents memory saturation and PC lockups
SAFE_WORKERS = max(1, min(4, (os.cpu_count() or 4) // 2))
BATCH_CHUNK_SIZE = 100
MAX_OCR_DIMENSION = 2048  # Maximum pixel dimension for OCR preprocessing

SPOT_COLOR_REGEX = re.compile(r"\[\s*/(?:Separation|DeviceN)\s+/([^ \t\n\r\[\]<>/]+)")


# ==============================================================================
# PATH & URI NORMALIZATION
# ==============================================================================
def is_junk_path(path: Path) -> bool:
    """Returns True if path is a macOS AppleDouble (._*), hidden, or system junk file."""
    for part in path.parts:
        if part.startswith(".") and part not in (".", ".."):
            return True
        if part in IGNORE_DIRS:
            return True
    name = path.name
    if name.startswith("._") or name.startswith("."):
        return True
    if name.startswith("~$"):
        return True
    if name.lower() in IGNORE_EXACT_NAMES:
        return True
    return False


def to_catalog_uri(p: Path | str) -> str:
    """Formats file path to standard catalog format: \\POGON PRINT\\..."""
    path = Path(p)
    try:
        rel = path.relative_to(TARGET_DIRECTORY)
        return f"\\POGON PRINT\\{str(rel)}"
    except ValueError:
        s = str(path)
        idx = s.find("POGON PRINT")
        if idx != -1:
            return "\\" + s[idx:]
        return str(path)


def resolve_catalog_uri(uri: str) -> Path:
    """Resolves catalog URI back to an absolute local filesystem path."""
    clean = uri.replace("/", "\\")
    if clean.startswith("\\POGON PRINT\\"):
        rel = clean[len("\\POGON PRINT\\"):]
        return TARGET_DIRECTORY / rel
    if clean.startswith("\\POGON PRINT"):
        rel = clean[len("\\POGON PRINT"):].lstrip("\\")
        return TARGET_DIRECTORY / rel
    return Path(uri)


# ==============================================================================
# COLOR & WHITE LOGO RECOGNITION HELPERS
# ==============================================================================
def is_white_or_near_white_color(color_tuple: Optional[Tuple[float, ...]]) -> bool:
    """
    Checks if color is:
    - 0%C 0%M 0%Y 0%K (pure white)
    - 0%C 0%M 1%Y 0%K (prepress white ink tint / 1% yellow)
    - RGB pure white (1.0, 1.0, 1.0) or near-white (>=0.97, >=0.97, >=0.96)
    - Grayscale white (>=0.98)
    """
    if not color_tuple:
        return False
    if len(color_tuple) == 1:
        return color_tuple[0] >= 0.98
    elif len(color_tuple) == 3:
        r, g, b = color_tuple
        return r >= 0.97 and g >= 0.97 and b >= 0.96
    elif len(color_tuple) == 4:
        c, m, y, k = color_tuple
        # 0%C 0%M 0%Y 0%K or 0%C 0%M 1%Y 0%K
        return c <= 0.005 and m <= 0.005 and y <= 0.015 and k <= 0.005
    return False


def get_artwork_info(page: fitz.Page) -> Tuple[fitz.Rect, bool, bool]:
    """
    Inspects page vector drawings and images.
    Returns: (artwork_bbox, has_white_elements, has_explicit_white_bg)
    """
    page_rect = page.rect
    drawings = page.get_drawings()
    has_white = False
    has_explicit_white_bg = False
    bbox = fitz.Rect()

    for i, d in enumerate(drawings):
        r = fitz.Rect(d["rect"])
        fill = d.get("fill")
        stroke = d.get("color")

        # Check if first drawing is a full-page explicit white background rectangle
        if i == 0 and r.width >= page_rect.width * 0.92 and r.height >= page_rect.height * 0.92:
            if is_white_or_near_white_color(fill):
                has_explicit_white_bg = True
                continue

        # Check if drawing has white or 1% Y elements
        if is_white_or_near_white_color(fill) or is_white_or_near_white_color(stroke):
            has_white = True

        if r.is_empty or r.is_infinite or r.width < 1 or r.height < 1:
            continue
        # Skip full-page background rects from artwork bbox calculation
        if r.width >= page_rect.width * 0.92 and r.height >= page_rect.height * 0.92:
            continue
        bbox |= r

    for img_info in page.get_images():
        xref = img_info[0]
        for r in page.get_image_rects(xref):
            if r.width >= page_rect.width * 0.92 and r.height >= page_rect.height * 0.92:
                continue
            bbox |= r

    if bbox.is_empty:
        bbox = fitz.Rect(page_rect)

    return bbox, has_white, has_explicit_white_bg


def insert_dynamic_black_background(page: fitz.Page, bbox: fitz.Rect, has_explicit_white_bg: bool, margin: float = 15.0) -> fitz.Rect:
    """
    Dynamically sizes a black rectangle approximately to the white space / artwork area
    and inserts it behind all layers in the PDF (in the bg).
    """
    page_rect = page.rect
    # If artwork covers most of page or is empty, use full page rect
    if bbox.width >= page_rect.width * 0.85 and bbox.height >= page_rect.height * 0.85:
        dyn_rect = fitz.Rect(page_rect)
    else:
        dyn_rect = fitz.Rect(
            max(page_rect.x0, bbox.x0 - margin),
            max(page_rect.y0, bbox.y0 - margin),
            min(page_rect.x1, bbox.x1 + margin),
            min(page_rect.y1, bbox.y1 + margin)
        )

    # Insert black rectangle behind all layers (in the bg)
    shape = page.new_shape()
    shape.draw_rect(dyn_rect)
    shape.finish(fill=(0, 0, 0), color=None)
    shape.commit(overlay=False)

    # If the PDF has an explicit white background layer, overlay=False sits behind it!
    # To ensure white logo contrast, draw black rectangle over the background layer
    if has_explicit_white_bg:
        shape2 = page.new_shape()
        shape2.draw_rect(dyn_rect)
        shape2.finish(fill=(0, 0, 0), color=None)
        shape2.commit(overlay=False)

    return dyn_rect


def calculate_safe_preview_dpi(rect: fitz.Rect, target_dpi: int = TARGET_PREVIEW_DPI) -> int:
    """Calculates safe rendering DPI so large format graphics never exceed memory limits."""
    max_dim_pt = max(rect.width, rect.height)
    if max_dim_pt <= 0:
        return int(target_dpi)
    max_allowed_dpi = (MAX_PREVIEW_PIXELS / max_dim_pt) * 72.0
    return int(max(36, min(int(target_dpi), int(max_allowed_dpi))))


# ==============================================================================
# OVERSIZED PDF DECOMPOSITION: VECTOR GROUP EXTRACTION & RETRY
# ==============================================================================
def is_oversized_pdf(file_path: Path, doc: Optional[fitz.Document] = None) -> bool:
    """Checks if PDF is too big to parse in a single pass without splitting."""
    try:
        if file_path.stat().st_size > OVERSIZED_FILE_SIZE_BYTES:
            return True
        if doc and len(doc) > 0:
            p = doc[0]
            if p.rect.width > OVERSIZED_DIMENSION_PT or p.rect.height > OVERSIZED_DIMENSION_PT:
                return True
            if len(p.get_drawings()) > OVERSIZED_DRAWINGS_COUNT:
                return True
    except Exception:
        pass
    return False


def cluster_vector_groups_from_page(doc: fitz.Document, page_num: int = 0, margin: float = 15.0, max_groups: int = 100) -> List[fitz.Rect]:
    """Clusters vector drawings and images into discrete spatial groups separated by whitespace."""
    page = doc[page_num]
    page_rect = page.rect
    drawings = page.get_drawings()

    artwork_rects = []
    for d in drawings:
        r = fitz.Rect(d["rect"])
        if r.width >= page_rect.width * 0.90 and r.height >= page_rect.height * 0.90:
            continue
        if r.is_empty or r.is_infinite or r.width < 2 or r.height < 2:
            continue
        artwork_rects.append(r)

    for img_info in page.get_images():
        xref = img_info[0]
        for r in page.get_image_rects(xref):
            if r.width >= page_rect.width * 0.90 and r.height >= page_rect.height * 0.90:
                continue
            artwork_rects.append(r)

    if not artwork_rects:
        return []

    clusters: List[fitz.Rect] = []
    for r in artwork_rects:
        r_exp = fitz.Rect(r.x0 - margin, r.y0 - margin, r.x1 + margin, r.y1 + margin)
        merged_indices = [i for i, c in enumerate(clusters) if c.intersects(r_exp)]
        if not merged_indices:
            clusters.append(fitz.Rect(r))
        else:
            target = merged_indices[0]
            clusters[target] |= r
            for i in reversed(merged_indices[1:]):
                clusters[target] |= clusters[i]
                del clusters[i]

    clusters.sort(key=lambda c: (c.y0, c.x0))
    return clusters[:max_groups]


def extract_vector_groups_to_individual_pdfs(file_path: Path, doc: fitz.Document) -> List[Path]:
    """Extracts all vector groups into individual .pdf files and saves them to _extracted_groups."""
    out_dir = EXTRACTED_GROUPS_DIR / file_path.stem
    out_dir.mkdir(parents=True, exist_ok=True)
    extracted_paths: List[Path] = []

    for page_idx in range(len(doc)):
        clusters = cluster_vector_groups_from_page(doc, page_idx)
        for grp_idx, cluster in enumerate(clusters):
            pad = 8.0
            page = doc[page_idx]
            clip_rect = fitz.Rect(
                max(0, cluster.x0 - pad),
                max(0, cluster.y0 - pad),
                min(page.rect.x1, cluster.x1 + pad),
                min(page.rect.y1, cluster.y1 + pad)
            )
            new_doc = fitz.open()
            new_page = new_doc.new_page(width=clip_rect.width, height=clip_rect.height)
            new_page.show_pdf_page(new_page.rect, doc, page_idx, clip=clip_rect)
            grp_filename = f"{file_path.stem}_p{page_idx+1}_grp{grp_idx+1}.pdf"
            grp_path = out_dir / grp_filename
            new_doc.save(str(grp_path))
            new_doc.close()
            extracted_paths.append(grp_path)

    return extracted_paths


# ==============================================================================
# DATABASE MANAGEMENT
# ==============================================================================
def init_db() -> sqlite3.Connection:
    TARGET_DIRECTORY.mkdir(parents=True, exist_ok=True)
    PREVIEWS_DIR.mkdir(parents=True, exist_ok=True)
    EXTRACTED_GROUPS_DIR.mkdir(parents=True, exist_ok=True)

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
        # Clean up any legacy macOS junk files (._*) or corrupt system files from previous runs
        conn.execute("DELETE FROM prepress_files WHERE file_name LIKE '._%' OR file_name LIKE '.%' OR file_name IN ('Thumbs.db', 'desktop.ini')")
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
# PIPELINE STEP 1: DISCOVERY & BASELINE REGISTRATION (REFINED CRAWLING)
# ==============================================================================
def run_step_1(conn: sqlite3.Connection) -> None:
    print("\n" + "="*70 + "\nSTEP 1/5: File Discovery & Database Registration (Refined Crawling)\n" + "="*70)

    if not TARGET_DIRECTORY.exists():
        print(f"[-] Target directory not found: {TARGET_DIRECTORY}")
        sys.exit(1)

    # Clean legacy junk records
    with conn:
        conn.execute("DELETE FROM prepress_files WHERE file_name LIKE '._%' OR file_name LIKE '.%' OR file_name IN ('Thumbs.db', 'desktop.ini')")

    found_files = []
    for root, dirs, files in os.walk(TARGET_DIRECTORY):
        # Prune ignored directory trees
        dirs[:] = [d for d in dirs if d not in IGNORE_DIRS and not d.startswith(".")]
        for f in files:
            p = Path(root) / f
            if is_junk_path(p):
                continue
            if p.suffix.lower() in SUPPORTED_EXTENSIONS:
                found_files.append(p)

    print(f"[*] Found {len(found_files)} valid assets on disk. Synchronizing database...")
    new_count, updated_count = 0, 0
    now_iso = datetime.datetime.now().isoformat()

    with conn:
        for file_path in found_files:
            uri = to_catalog_uri(file_path)
            try:
                stat = file_path.stat()
            except OSError:
                continue

            mtime = datetime.datetime.fromtimestamp(stat.st_mtime).isoformat()
            ctime = datetime.datetime.fromtimestamp(stat.st_ctime).isoformat()

            row = conn.execute("SELECT modified_date FROM prepress_files WHERE file_uri = ?", (uri,)).fetchone()

            if row is None:
                conn.execute("""
                    INSERT INTO prepress_files (
                        file_uri, file_name, file_size_bytes, created_date, modified_date, 
                        file_extension, status_step1, last_updated
                    ) VALUES (?, ?, ?, ?, ?, ?, 1, ?)
                """, (uri, file_path.name, stat.st_size, ctime, mtime, file_path.suffix.lower(), now_iso))
                new_count += 1
            elif row[0] != mtime:
                conn.execute("""
                    UPDATE prepress_files SET
                        file_size_bytes = ?, modified_date = ?, status_step1 = 1,
                        status_step2 = 0, status_step3 = 0, status_step4 = 0, status_step5 = 0,
                        last_updated = ?
                    WHERE file_uri = ?
                """, (stat.st_size, mtime, now_iso, uri))
                updated_count += 1

    print(f"[✓] Step 1 finished. New indexed: {new_count} | Modified re-queued: {updated_count}")


# ==============================================================================
# PIPELINE STEP 2: PREPRESS TECHNICAL INSPECTION
# ==============================================================================
def _worker_inspect(payload: Tuple[str, str]) -> Tuple[str, Dict[str, Any], bool]:
    uri, ext = payload
    file_path = resolve_catalog_uri(uri)
    result = {"dim_pt": "N/A", "dim_mm": "N/A", "min_dpi": 0.0, "color_spaces": set(), "spot_colors": set()}

    if not file_path.exists():
        return uri, result, False

    try:
        if ext in (".pdf", ".ai", ".svg"):
            with fitz.open(file_path) as doc:
                if len(doc) > 0:
                    page = doc[0]
                    rect = page.rect
                    w_mm, h_mm = (rect.width / 72.0) * 25.4, (rect.height / 72.0) * 25.4
                    result["dim_pt"] = f"{rect.width:.2f} x {rect.height:.2f} pt"
                    result["dim_mm"] = f"{w_mm:.2f} x {h_mm:.2f} mm"

                    dpi_list = []
                    for img_info in page.get_images(full=True):
                        xref = img_info[0]
                        base_img = doc.extract_image(xref)
                        if not base_img: continue
                        result["color_spaces"].add(f"Image:{base_img.get('colorspace', 'Unknown')}")
                        pix_w, pix_h = base_img.get("width", 0), base_img.get("height", 0)
                        for img_rect in page.get_image_rects(xref):
                            if img_rect.width > 0 and img_rect.height > 0:
                                dpi_list.append(min(pix_w / (img_rect.width / 72.0), pix_h / (img_rect.height / 72.0)))
                    if dpi_list:
                        result["min_dpi"] = round(min(dpi_list), 1)

                    # Inspect vector drawings for pure white (0/0/0/0 or 0/0/1/0) and color spaces
                    _, has_white, has_explicit_bg = get_artwork_info(page)
                    if has_white:
                        result["color_spaces"].add("Vector:White/1%Y_Tint")
                    if has_explicit_bg:
                        result["color_spaces"].add("Vector:Explicit_White_BG")

                    for xref in range(1, doc.xref_length()):
                        try:
                            obj_str = doc.xref_object(xref)
                            matches = SPOT_COLOR_REGEX.findall(obj_str)
                            for match in matches:
                                if match not in ("All", "None", "Cyan", "Magenta", "Yellow", "Black"):
                                    result["spot_colors"].add(match)
                        except Exception:
                            pass
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
                        conn.execute("""
                            UPDATE prepress_files SET
                                dimensions_pt = ?, dimensions_mm = ?, embedded_raster_dpi = ?, 
                                color_spaces = ?, spot_color_channels = ?, status_step2 = 1, last_updated = ?
                            WHERE file_uri = ?
                        """, (data["dim_pt"], data["dim_mm"], data["min_dpi"], c_space, spots, datetime.datetime.now().isoformat(), uri))
            gc.collect()
    print()


# ==============================================================================
# PIPELINE STEP 3: TEXT & LOGO EXTRACTION (WHITE-INK & OCR ENHANCED)
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
    file_path = resolve_catalog_uri(uri)
    if not file_path.exists(): return uri, "", False

    raw_chunks: List[str] = []

    if ext in (".pdf", ".ai", ".svg"):
        try:
            with fitz.open(file_path) as doc:
                if len(doc) > 0:
                    # Check if file is too big to parse: extract vector groups and retry
                    if is_oversized_pdf(file_path, doc):
                        extracted_groups = extract_vector_groups_to_individual_pdfs(file_path, doc)
                        grp_texts = []
                        for grp_p in extracted_groups[:25]:
                            try:
                                with fitz.open(grp_p) as grp_doc:
                                    p_g = grp_doc[0]
                                    txt_g = p_g.get_text().strip()
                                    if not txt_g:
                                        pix_g = p_g.get_pixmap(dpi=150)
                                        img_g = Image.frombytes("RGB", [pix_g.width, pix_g.height], pix_g.samples)
                                        txt_g = pytesseract.image_to_string(img_g.convert("L")).strip()
                                    if txt_g:
                                        grp_texts.append(txt_g)
                            except Exception:
                                pass
                        if grp_texts:
                            return uri, deduplicate_gangsheet_text(grp_texts), True

                    for page in doc:
                        # 1. Live font text
                        live_blocks = [b[4] for b in page.get_text("blocks") if b[6] == 0]
                        raw_chunks.extend(live_blocks)

                        # 2. Vector curves & white logo reading
                        bbox, has_white, has_explicit_bg = get_artwork_info(page)
                        safe_dpi = calculate_safe_preview_dpi(page.rect, target_dpi=150.0)

                        # A: High-contrast dark background for pure white (0%C 0%M 0%Y 0%K or 0%C 0%M 1%Y 0%K)
                        if has_white or has_explicit_bg:
                            try:
                                doc_temp = fitz.open(file_path)
                                p_temp = doc_temp[page.number]
                                _ = insert_dynamic_black_background(p_temp, bbox, has_explicit_bg)
                                pix_white = p_temp.get_pixmap(dpi=safe_dpi, alpha=False)
                                img_white = Image.frombytes("RGB", [pix_white.width, pix_white.height], pix_white.samples)
                                
                                # Invert: white logo on black background becomes crisp dark text on white canvas
                                inv = ImageOps.invert(img_white.convert("L"))
                                if max(inv.size) > MAX_OCR_DIMENSION:
                                    inv.thumbnail((MAX_OCR_DIMENSION, MAX_OCR_DIMENSION), Image.Resampling.LANCZOS)
                                ocr_w = pytesseract.image_to_string(inv)
                                raw_chunks.extend(ocr_w.splitlines())
                                doc_temp.close()
                            except Exception:
                                pass

                        # B: Standard OCR if live text is absent/minimal (for colored outlined logos)
                        if len(" ".join(live_blocks).strip()) < 10:
                            try:
                                pix_std = page.get_pixmap(dpi=safe_dpi, alpha=False)
                                img_std = Image.frombytes("RGB", [pix_std.width, pix_std.height], pix_std.samples)
                                gray = img_std.convert("L")
                                if max(gray.size) > MAX_OCR_DIMENSION:
                                    gray.thumbnail((MAX_OCR_DIMENSION, MAX_OCR_DIMENSION), Image.Resampling.LANCZOS)
                                ocr_s = pytesseract.image_to_string(gray)
                                raw_chunks.extend(ocr_s.splitlines())
                            except Exception:
                                pass
        except Exception:
            pass
    else:
        try:
            with Image.open(file_path) as img:
                w, h = img.size
                if max(w, h) > MAX_OCR_DIMENSION:
                    scale = MAX_OCR_DIMENSION / float(max(w, h))
                    new_size = (int(w * scale), int(h * scale))
                    proc_img = img.resize(new_size, resample=Image.Resampling.BILINEAR)
                else:
                    proc_img = img.copy()

                # In case of transparent PNG with white logo, composite over black
                if proc_img.mode in ("RGBA", "LA") or (proc_img.mode == "P" and "transparency" in proc_img.info):
                    rgba = proc_img.convert("RGBA")
                    bg = Image.new("RGBA", rgba.size, (0, 0, 0, 255))
                    comp = Image.alpha_composite(bg, rgba)
                    inv = ImageOps.invert(comp.convert("L"))
                    ocr_text = pytesseract.image_to_string(inv)
                    raw_chunks.extend(ocr_text.splitlines())
                else:
                    ocr_text = pytesseract.image_to_string(proc_img.convert("L"))
                    raw_chunks.extend(ocr_text.splitlines())
                proc_img.close()
        except Exception:
            pass

    return uri, deduplicate_gangsheet_text(raw_chunks), True


def run_step_3(conn: sqlite3.Connection) -> None:
    print("\n" + "="*70 + "\nSTEP 3/5: Text Extraction & Gang-Sheet Deduplication (OCR Enhanced)\n" + "="*70)

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
                        conn.execute("UPDATE prepress_files SET extracted_content = ?, status_step3 = 1, last_updated = ? WHERE file_uri = ?",
                                     (clean_text, datetime.datetime.now().isoformat(), uri))
            gc.collect()
    print()


# ==============================================================================
# PIPELINE STEP 4: DYNAMIC BLACK RECTANGLE HIGH-CONTRAST PREVIEWS
# ==============================================================================
def _worker_render(payload: Tuple[str, str, str]) -> Tuple[str, str, bool]:
    uri, ext, prev_dir = payload
    file_path = resolve_catalog_uri(uri)
    if not file_path.exists(): return uri, "", False

    dest_filename = f"prev_{file_path.stem}_{abs(hash(uri)) % 1000000}.png"
    dest_path = Path(prev_dir) / dest_filename
    catalog_preview_uri = to_catalog_uri(dest_path)
    success = False

    try:
        if ext in (".pdf", ".ai", ".svg"):
            with fitz.open(file_path) as doc:
                if len(doc) > 0:
                    page = doc[0]
                    bbox, has_white, has_explicit_bg = get_artwork_info(page)
                    safe_dpi = calculate_safe_preview_dpi(page.rect, target_dpi=TARGET_PREVIEW_DPI)

                    # Dynamic black rectangle inserted behind layers
                    _ = insert_dynamic_black_background(page, bbox, has_explicit_bg)
                    pix = page.get_pixmap(dpi=safe_dpi, alpha=False)
                    pix.save(str(dest_path))
                    pix = None
                    success = True
        else:
            with Image.open(file_path) as img:
                rgba = img.convert("RGBA")
                bg = Image.new("RGBA", rgba.size, (0, 0, 0, 255))
                composite = Image.alpha_composite(bg, rgba)

                # Thumbnail if unusually large
                if max(composite.size) > MAX_PREVIEW_PIXELS:
                    composite.thumbnail((MAX_PREVIEW_PIXELS, MAX_PREVIEW_PIXELS), Image.Resampling.LANCZOS)

                composite.convert("RGB").save(dest_path, "PNG")
                rgba.close()
                bg.close()
                composite.close()
                success = True
    except Exception:
        pass

    return uri, catalog_preview_uri if success else "GENERATION_FAILED", True


def run_step_4(conn: sqlite3.Connection) -> None:
    print("\n" + "="*70 + "\nSTEP 4/5: High-Contrast Dark Previews (Dynamic Black Background)\n" + "="*70)

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
                        conn.execute("UPDATE prepress_files SET preview_uri = ?, status_step4 = 1, last_updated = ? WHERE file_uri = ?",
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
        f.write('  "target_directory": "\\\\POGON PRINT",\n')
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
                    "extracted_unique_text": r[11] or "",
                    "preview_uri": r[12] or "GENERATION_FAILED"
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
        conn.execute("UPDATE prepress_files SET status_step5 = 1")

    print(f"\n[✓] Successfully exported {total_records} records to:\n    {EXPORT_JSON_PATH}")


# ==============================================================================
# ENTRY POINT
# ==============================================================================
def main() -> None:
    multiprocessing.freeze_support()

    print("""
======================================================================
     DTF / DTG RESUMABLE PREPRESS INDEXER & PREVIEW ENGINE
     (Refined Crawling, White Logo Extraction & Oversized Decomposition)
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