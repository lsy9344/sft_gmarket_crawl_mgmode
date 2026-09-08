"""
전체 카테고리 재순회 - 남은 아이템 최대 수집
베스트 10개 (각 ~150개 잔여) + 슈퍼딜 12개 (잔여)
"""

import json
import time
import random
from pathlib import Path
from datetime import datetime

import sys
sys.stdout.reconfigure(line_buffering=True)
sys.path.insert(0, str(Path(__file__).parent))
from crawler import crawl, OUTPUT_DIR, BEST_CATEGORIES, SUPERDEAL_CATEGORIES

STATE_FILE = OUTPUT_DIR / "fullsweep_state.json"

ITEMS_PER_CATEGORY = 150
INTER_CATEGORY_COOLDOWN = (600.0, 900.0)


def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {"completed": [], "run_start": datetime.now().isoformat()}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def run_full_sweep():
    state = load_state()

    all_categories = []
    for i, cat in enumerate(BEST_CATEGORIES):
        all_categories.append({"source": "best", "idx": i, "name": f"best/{cat['name']}"})
    for i, cat in enumerate(SUPERDEAL_CATEGORIES):
        all_categories.append({"source": "superdeal", "idx": i, "name": f"superdeal/{cat['name']}"})

    remaining = [c for c in all_categories if c["name"] not in state["completed"]]

    print(f"[FULL SWEEP] Start: {state['run_start']}")
    print(f"[FULL SWEEP] Total categories: {len(all_categories)}")
    print(f"[FULL SWEEP] Already done: {len(state['completed'])}")
    print(f"[FULL SWEEP] Remaining: {len(remaining)}")
    print(f"[FULL SWEEP] Items per category: {ITEMS_PER_CATEGORY}")
    print("=" * 60)

    if not remaining:
        print("[DONE] All categories already swept.")
        return

    print(f"\n[WARMUP] 30s before start...")
    time.sleep(30)

    total_success = 0

    for cat in remaining:
        print(f"\n{'=' * 60}")
        print(f"[CATEGORY] {cat['name']}")
        print(f"[TIME] {datetime.now().strftime('%H:%M:%S')}")
        print(f"[PROGRESS] {len(state['completed'])}/{len(all_categories)} done")
        print(f"{'=' * 60}")

        bot_heavy = False
        try:
            results, stats = crawl(
                max_items=ITEMS_PER_CATEGORY,
                delay_range=(10.0, 18.0),
                source=cat["source"],
                category_idx=cat["idx"],
            )
            total_success += stats["success"]
            bot_heavy = stats["bot_detected"] >= 3
            print(f"\n[DONE] {cat['name']}: +{stats['success']} (bot={stats['bot_detected']})")

        except Exception as e:
            print(f"\n[ERROR] {cat['name']}: {e}")
            bot_heavy = True

        state["completed"].append(cat["name"])
        state["last_success_total"] = total_success
        save_state(state)

        still_remaining = [c for c in remaining if c["name"] not in state["completed"]]
        if still_remaining:
            if bot_heavy:
                cooldown = random.uniform(900.0, 1200.0)
                print(f"\n[BOT COOLDOWN] {cooldown/60:.1f} min (extended)...")
            else:
                cooldown = random.uniform(*INTER_CATEGORY_COOLDOWN)
                print(f"\n[COOLDOWN] {cooldown/60:.1f} min...")
            time.sleep(cooldown)

    print(f"\n{'=' * 60}")
    print(f"[FULL SWEEP COMPLETE] Total new: {total_success}")
    print(f"{'=' * 60}")


if __name__ == "__main__":
    run_full_sweep()
