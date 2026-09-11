import sqlite3
from pathlib import Path

DB_PATH = Path(r"C:\Users\100\Documents\POGON PRINT\prepress_index.db")

def skip_bad_files():
    with sqlite3.connect(DB_PATH) as conn:
        # Find any files stuck before Step 2
        stuck_files = conn.execute("SELECT file_uri FROM prepress_files WHERE status_step2 = 0").fetchall()
        
        if not stuck_files:
            print("[*] No stuck files found. You are good to go!")
            return

        for (uri,) in stuck_files:
            print(f"[!] Found corrupted file: {Path(uri).name}")
            print(f"    Path: {uri}")
            
            # Force it to 'complete' with a CORRUPTED flag so the script moves on
            conn.execute("""
                UPDATE prepress_files 
                SET status_step2 = 1, 
                    status_step3 = 1, 
                    status_step4 = 1, 
                    dimensions_pt = 'CORRUPTED', 
                    dimensions_mm = 'CORRUPTED' 
                WHERE file_uri = ?
            """, (uri,))
            
        print("\n[✓] Database updated. The pipeline will now skip this file.")

if __name__ == "__main__":
    skip_bad_files()