"""
전체 카테고리 자동 순회 수집 스크립트
- IP 피로 방지를 위한 카테고리 간 긴 쿨다운
- 실패 시 자동 스킵 및 다음 카테고리 진행
- 진행 상태 저장/복구
"""

import json
import time
import random
from pathlib import Path
from datetime import datetime

import sys
sys.path.insert(0, str(Path(__file__).parent))
from crawler import crawl, OUTPUT_DIR

STATE_FILE = OUTPUT_DIR / "run_state.json"

REMAINING_SUPERDEAL = [
    {"idx": 5, "name": "식품"},
    {"idx": 6, "name": "생필품"},
    {"idx": 7, "name": "가구/침구"},
    {"idx": 8, "name": "생활/건강"},
    {"idx": 9, "name": "스포츠/레저"},
    {"idx": 10, "name": "가전/컴퓨터"},
    {"idx": 11, "name": "디지털"},
]

INTER_CATEGORY_COOLDOWN = (600.0, 900.0)  # 10~15분 카테고리 간 대기
ITEMS_PER_CATEGORY = 40


def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {"completed": [], "current_run": datetime.now().isoformat()}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def run_all():
    state = load_state()
    print(f"[AUTO-CRAWL] Started: {state['current_run']}")
    print(f"[AUTO-CRAWL] Already completed: {state['completed']}")
    print(f"[AUTO-CRAWL] Items per category: {ITEMS_PER_CATEGORY}")
    print(f"[AUTO-CRAWL] Inter-category cooldown: {INTER_CATEGORY_COOLDOWN[0]/60:.0f}~{INTER_CATEGORY_COOLDOWN[1]/60:.0f} min")
    print("=" * 60)

    # IP recovery wait (recent 429 errors)
    initial_wait = 900  # 15 min
    print(f"\n[IP RECOVERY] Waiting {initial_wait//60} min for IP cooldown...")
    time.sleep(initial_wait)
    print("[IP RECOVERY] Done. Starting collection.")

    total_success = 0

    for cat in REMAINING_SUPERDEAL:
        if cat["name"] in state["completed"]:
            print(f"\n[SKIP] {cat['name']} (already done)")
            continue

        print(f"\n{'=' * 60}")
        print(f"[CATEGORY] {cat['name']} (idx={cat['idx']})")
        print(f"[TIME] {datetime.now().strftime('%H:%M:%S')}")
        print(f"{'=' * 60}")

        try:
            results, stats = crawl(
                max_items=ITEMS_PER_CATEGORY,
                delay_range=(10.0, 18.0),
                source="superdeal",
                category_idx=cat["idx"],
            )
            total_success += stats["success"]
            print(f"\n[DONE] {cat['name']}: +{stats['success']} (bot={stats['bot_detected']})")

            state["completed"].append(cat["name"])
            save_state(state)

        except Exception as e:
            print(f"\n[ERROR] {cat['name']}: {e}")

        # Inter-category cooldown
        remaining = [c for c in REMAINING_SUPERDEAL if c["name"] not in state["completed"]]
        if remaining:
            cooldown = random.uniform(*INTER_CATEGORY_COOLDOWN)
            print(f"\n[COOLDOWN] Waiting {cooldown/60:.1f} min before next category...")
            time.sleep(cooldown)

    print(f"\n{'=' * 60}")
    print(f"[ALL DONE] Total new: {total_success}")
    print(f"[ALL DONE] Completed categories: {state['completed']}")
    print(f"{'=' * 60}")


if __name__ == "__main__":
    run_all()
