"""실증 A+B: 판매자 단계까지 Decodo 세션만 사용하는 전 과정 실실행.

- 실증 B(먼저): 실행 중 취소 → 부분 저장 확인
- 실증 A(이어서): 같은 출력 폴더 재실행 → 재개 + 완주 + 필드/게이트/분류 검증
집 회선 미사용 — 모든 트래픽은 config.proxy(Decodo 스티키) 경유.
"""
import json, sys, threading, time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, "/home/noah/Desktop/project/dev_busi/sft_gmarket_crawl_mgmode")
from app.core.base import Control
from app.core import decodo
from app.core.coupang.decodo_run import run_category_attempts
from app.core.coupang.search_crawler import SearchCrawler, SearchRunConfig

OUT = Path("/tmp/coupang-vendor-proxy-verify")
CAT_ID, CAT_NAME = "432814", "이유식/어린이식품"
CANCEL_AFTER_S = float(sys.argv[1]) if len(sys.argv) > 1 else 0  # 0이면 취소 없이 완주

log_lines = []
def log(m):
    line = f"[{datetime.now().strftime('%H:%M:%S')}] {m}"
    print(line, flush=True)
    log_lines.append(line)

def make_config():
    return SearchRunConfig(
        output_dir=OUT / "output",
        category_id=CAT_ID, category_name=CAT_NAME,
        vendor_phase_line="proxy",       # 핵심: 판매자 단계도 프록시 세션
        browser_engine="chromium",
        exclude_rocket=True,
        max_pages=200,
    )

control = Control()
if CANCEL_AFTER_S > 0:
    threading.Timer(CANCEL_AFTER_S, control.request_cancel).start()

t0 = time.time()
summary = run_category_attempts(
    make_config(), decodo.load_settings(), control,
    crawler_factory=lambda cfg: SearchCrawler(cfg, control, on_log=log),
    on_log=log,
)
elapsed = round(time.time() - t0, 1)

result = {
    "cancelled": summary.cancelled,
    "termination_reason": summary.termination_reason,
    "error": summary.error,
    "products_seen": summary.products_seen,
    "unique_vendors": summary.unique_vendors,
    "business_info_success": summary.business_info_success,
    "json_path": str(summary.json_path) if summary.json_path else None,
    "elapsed_s": elapsed,
}
# 검증 체크: 출력 폴더에 12h 게이트 파일이 없어야 한다
run_dirs = list((OUT / "output").rglob("coupang_block_state.json"))
result["block_state_files"] = [str(p) for p in run_dirs]
# 로그 지표
result["log_has_proxy_vendor"] = any("같은 프록시 세션" in m for m in log_lines)
result["log_has_direct_switch"] = any("2차 전환" in m for m in log_lines)
print("RESULT=" + json.dumps(result, ensure_ascii=False))
(OUT / ("result-%s.json" % ("cancel" if CANCEL_AFTER_S > 0 else "resume"))).write_text(
    json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
(OUT / ("driver-%s.log" % ("cancel" if CANCEL_AFTER_S > 0 else "resume"))).write_text(
    "\n".join(log_lines), encoding="utf-8")
