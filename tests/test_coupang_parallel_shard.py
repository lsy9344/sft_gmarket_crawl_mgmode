"""쿠팡 병렵 분할 수집(단일 카테고리 → 샤드) — 분할·병합·매니저/워커 연동.

네트워크 없음: 샤드 분할 규칙, 샤드 출력 병합(parallel_merge), 매니저
shard_mode(폴더명·상태 영속·finalize 조건), 워커 루프 후 finalize 호출을
검증한다. 가족 준비물은 test_coupang_parallel_manager 의
_prepare_complete_family 패턴을 shard_NN 폴더 이름으로 재사용한다.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from app.core.base import Control  # noqa: E402
from app.core.coupang import parallel_manager as pm  # noqa: E402
from app.core.coupang.parallel_manager import (  # noqa: E402
    STATE_FILENAME,
    ParallelCoupangManager,
    ParallelRunConfig,
    split_family_into_shards,
)
from app.core.coupang.parallel_merge import (  # noqa: E402
    MERGE_STATE_FILENAME,
    merge_shard_outputs,
)
from app.core.coupang.patchright_full_fruit import (  # noqa: E402
    PRODUCT_SELLER_FIELDS,
    SELLER_FIELDS,
    _read_csv,
    _write_csv,
    _write_json,
)
from app.core.coupang.patchright_top_sellers import TopSellerStore  # noqa: E402
from app.core.coupang.patchright_top_thousand import (  # noqa: E402
    PRODUCT_FIELDS,
    TopThousandStore,
)
from app.workers.coupang_parallel_worker import CoupangParallelWorker  # noqa: E402

INTERVAL_MINUTES = 90
INTERVAL_SECONDS = INTERVAL_MINUTES * 60
T0 = time.time()

_SHARD_POOL = [
    ("185671", "주방용품/냄비/프라이팬"),
    ("185735", "주방용품/그릇/홈세트"),
    ("185872", "주방용품/밀폐저장/도시락"),
    ("186176", "주방용품/주방소형/조리도구"),
]


def _product_row(category: tuple[str, str], vendor_item_id: str) -> dict:
    category_id, category_name = category
    return {
        "category_id": category_id,
        "category_name": category_name,
        "page_number": "1",
        "product_id": "10",
        "item_id": "20",
        "vendor_item_id": vendor_item_id,
        "title": f"상품 {vendor_item_id}",
        "price": "1000",
        "review_count": "100",
        "delivery_markers": "",
        "url": (
            "https://www.coupang.com/vp/products/10"
            f"?itemId=20&vendorItemId={vendor_item_id}"
        ),
        "collected_at": "2026-10-05 00:00:00",
    }


def _seller_row(vendor_id: str, status: str = "saved") -> dict:
    row = {field: "" for field in SELLER_FIELDS}
    row.update(
        status=status,
        vendor_id=vendor_id,
        url=f"https://www.coupang.com/vendors/{vendor_id}",
        store_name=f"스토어{vendor_id}",
        company_name=f"주식회사{vendor_id}",
        rating_count="1000",
        updated_at="2026-10-05 00:00:00",
    )
    return row


def _mapping_row(vendor_item_id: str, vendor_id: str) -> dict:
    return {
        "product_id": "10",
        "item_id": "20",
        "vendor_item_id": vendor_item_id,
        "vendor_id": vendor_id,
        "mapped_at": "2026-10-05 00:00:00",
    }


def _prepare_complete_shard(
    shard_dir: Path,
    family: list[tuple[str, str]],
    *,
    products: list[dict] | None = None,
    sellers: list[dict] | None = None,
    mappings: list[dict] | None = None,
    failed_mappings: list[dict] | None = None,
    failed_sellers: list[dict] | None = None,
) -> Path:
    """완주한 샤드 출력 폴더를 만든다(기본값: 상품 1·판매자 saved 완료)."""
    shard_dir.mkdir(parents=True, exist_ok=True)
    TopThousandStore(shard_dir, family).ensure_files()
    TopSellerStore(shard_dir).ensure_files()
    _write_csv(
        shard_dir / "top_products.csv",
        PRODUCT_FIELDS,
        products if products is not None else [_product_row(family[0], "viid-1")],
    )
    _write_csv(
        shard_dir / "top_sellers.csv",
        SELLER_FIELDS,
        sellers if sellers is not None else [_seller_row("A00001")],
    )
    _write_csv(
        shard_dir / "top_product_seller.csv",
        PRODUCT_SELLER_FIELDS,
        mappings if mappings is not None else [_mapping_row("viid-1", "A00001")],
    )
    _write_json(
        shard_dir / "top_failed_mappings.json",
        failed_mappings if failed_mappings is not None else [],
    )
    _write_json(
        shard_dir / "top_failed_sellers.json",
        failed_sellers if failed_sellers is not None else [],
    )
    state = {
        "version": 1,
        "status": "completed",
        "category_index": len(family),
        "page_number": 1,
        "consecutive_empty_pages": 0,
        "categories": {
            category_id: {
                "category_name": name,
                "status": "exhausted",
                "raw_seen": 60,
                "rocket_seen": 40,
                "jet_seen": 0,
                "tp_collected": 20,
                "pages_scanned": 2,
            }
            for category_id, name in family
        },
    }
    (shard_dir / "top_state.json").write_text(
        json.dumps(state, ensure_ascii=False), encoding="utf-8"
    )
    return shard_dir


def _make_manager(
    root: Path,
    families: list[list[tuple[str, str]]],
    events: list,
    *,
    shard_mode: bool = True,
    instance_count: int = 1,
):
    config = ParallelRunConfig(
        families=families,
        output_dir=root / "out",
        instance_count=instance_count,
        interval_minutes=INTERVAL_MINUTES,
        listing_pages=1,
        shard_mode=shard_mode,
    )
    with mock.patch.object(
        pm, "_state_root_for", lambda instance_id: root / f"state_{instance_id}"
    ):
        return ParallelCoupangManager(
            config, Control(), events.append, browser_scope_factory=_NoBrowser()
        )


class _NoBrowser:
    """세션이 돌면 안 되는 테스트용 팩토리 — 호출되면 실패한다."""

    def __call__(self, *_args, **_kwargs):
        raise AssertionError("브라우저 세션이 열렸습니다 — 완주 경로만 테스트한다")


def _read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def _events_of(events: list[dict], type_name: str) -> list[dict]:
    return [event for event in events if event.get("type") == type_name]


# ── 1. 샤드 분할 ──────────────────────────────────────────────────────


class SplitFamilyIntoShardsTest(unittest.TestCase):
    def test_round_robin_split_is_complete_and_disjoint(self):
        family = [(str(index), f"cat{index}") for index in range(19)]
        shards = split_family_into_shards(family, 14)
        self.assertEqual(len(shards), 14)
        seen: set[str] = set()
        for shard in shards:
            ids = {category_id for category_id, _ in shard}
            self.assertFalse(ids & seen)  # 샤드끼리 겹치지 않는다
            seen |= ids
        self.assertEqual(seen, {str(index) for index in range(19)})  # 전부 배분

    def test_empty_shards_are_dropped(self):
        family = [(str(index), f"cat{index}") for index in range(3)]
        shards = split_family_into_shards(family, 8)
        self.assertEqual([len(shard) for shard in shards], [1, 1, 1])

    def test_single_category_yields_single_shard(self):
        family = [("1", "리프")]
        self.assertEqual(split_family_into_shards(family, 7), [[("1", "리프")]])

    def test_invalid_shard_count_is_rejected(self):
        with self.assertRaises(ValueError):
            split_family_into_shards([("1", "a")], 0)


# ── 2. 샤드 출력 병합 ─────────────────────────────────────────────────


class MergeShardOutputsTest(unittest.TestCase):
    def test_union_and_finals_at_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            shard_a = _prepare_complete_shard(
                root / "shard_01",
                [_SHARD_POOL[0]],
                products=[
                    _product_row(_SHARD_POOL[0], "viid-1"),
                    _product_row(_SHARD_POOL[0], "viid-2"),
                ],
                sellers=[_seller_row("A00001"), _seller_row("A00002", "failed")],
                mappings=[_mapping_row("viid-1", "A00001")],
                failed_sellers=[{"vendor_id": "A00009", "error": "x"}],
                failed_mappings=[
                    {"vendor_item_id": "viid-2", "error": "판매자 연결 결과 없음"}
                ],
            )
            shard_b = _prepare_complete_shard(
                root / "shard_02",
                [_SHARD_POOL[1]],
                products=[
                    # 같은 (카테고리, viid) 중복 — 한 번만 남는다.
                    _product_row(_SHARD_POOL[1], "viid-3"),
                ],
                sellers=[
                    # 같은 판매자가 다른 샤드에도 — saved 가 failed 를 이긴다.
                    _seller_row("A00002"),
                    _seller_row("A00003"),
                ],
                mappings=[_mapping_row("viid-3", "A00002")],
                failed_sellers=[{"vendor_id": "A00009", "error": "중복"}],
            )

            summary = merge_shard_outputs(root, [shard_a, shard_b])

            self.assertEqual(summary["shard_count"], 2)
            self.assertEqual(summary["products"], 3)  # viid-1/2/3
            self.assertEqual(summary["sellers"], 3)  # A00001~03 saved
            self.assertEqual(summary["mappings"], 2)
            self.assertEqual(summary["failed_mappings"], 1)
            self.assertEqual(summary["failed_sellers"], 1)  # vendor_id 중복 제거
            # 루트 완성형이 만들어진다.
            finals = {key: Path(path) for key, path in summary["finals"].items()}
            self.assertEqual(set(finals), {"sellers", "all"})
            for path in finals.values():
                self.assertTrue(path.exists(), path)
            # 판매자 완성형은 saved 만 들어가고 failed 는 이겨진 상태로 반영.
            seller_rows = _read_csv(root / "top_sellers.csv", SELLER_FIELDS)
            by_vendor = {row["vendor_id"]: row for row in seller_rows}
            self.assertEqual(by_vendor["A00002"]["status"], "saved")
            # 병합 표시 파일이 루트에 남는다.
            marker = _read_json(root / MERGE_STATE_FILENAME)
            self.assertEqual(marker["version"], 1)
            self.assertEqual(marker["products"], 3)

    def test_missing_shard_files_are_treated_as_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            empty = root / "shard_01"
            empty.mkdir()
            summary = merge_shard_outputs(root, [empty])
            self.assertEqual(summary["products"], 0)
            self.assertEqual(summary["sellers"], 0)

    def test_empty_shard_list_is_rejected(self):
        with (
            tempfile.TemporaryDirectory() as tmp,
            self.assertRaises(ValueError),
        ):
            merge_shard_outputs(Path(tmp) / "out", [])


# ── 3. 매니저 shard_mode ─────────────────────────────────────────────


class ManagerShardModeTest(unittest.TestCase):
    def _two_complete_shards(self, root: Path, prefix: str = "shard") -> list[list[tuple[str, str]]]:
        shards = [
            [_SHARD_POOL[0], _SHARD_POOL[1]],
            [_SHARD_POOL[2], _SHARD_POOL[3]],
        ]
        for index, family in enumerate(shards):
            _prepare_complete_shard(
                root / "out" / f"{prefix}_{index + 1:02d}", family
            )
        return shards

    def test_sessions_write_into_shard_folders_and_finalize_merges(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            events: list = []
            shards = self._two_complete_shards(root)
            manager = _make_manager(root, shards, events, instance_count=1)

            # 세션 1 — 샤드 1 완주, 샤드 2 승계. 세션 2 — 샤드 2 완주 → done.
            self.assertTrue(manager.run_due(T0))
            self.assertTrue((root / "out" / "shard_01").exists())
            self.assertTrue(manager.run_due(T0 + INTERVAL_SECONDS))
            self.assertEqual(manager.instances["1"].status, "done")
            # 샤드 모드의 안내 문구는 작업 단위(샤드)로 말한다.
            logs = " ".join(
                str(event.get("message") or "")
                for event in _events_of(events, "log")
            )
            self.assertIn("샤드 2개를 인스턴스 1개에 배분했습니다", logs)
            self.assertIn("샤드 2을(를) 승계해", logs)

            # finalize — 루트 병합 1회.
            summary = manager.finalize()
            self.assertIsNotNone(summary)
            self.assertEqual(summary["shard_count"], 2)
            self.assertTrue((root / "out" / MERGE_STATE_FILENAME).exists())
            self.assertTrue((root / "out" / "final_dataset_all.csv").exists())
            merged = _events_of(events, "merge_complete")
            self.assertEqual(len(merged), 1)
            self.assertEqual(merged[0]["products"], 2)

            # 두 번째 호출은 이미 병합됐다는 표시로 아무 것도 하지 않는다.
            self.assertIsNone(manager.finalize())

    def test_error_instance_recovered_on_restart_then_finalize_merges(self):
        """error 로 끊긴 샤드 실행도 재시작 재심사로 완주·병합된다.

        2026-10-06 검토 반영 — error 인스턴스가 재시작으로 회복되지 않으면
        맡은 샤드가 고아가 되어 루트 병합이 영원히 일어나지 않았다.
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            events: list = []
            shards = self._two_complete_shards(root)
            manager = _make_manager(root, shards, events, instance_count=1)
            # 첫 세션 직전에 오류로 끊긴 상황을 저장한다(샤드 1 배분·샤드 2 대기).
            manager.instances["1"].status = "error"
            manager.persist_state()

            events2: list = []
            restarted = _make_manager(root, shards, events2, instance_count=1)
            self.assertEqual(restarted.instances["1"].status, "waiting")
            self.assertTrue(restarted.run_due(T0))  # 샤드 1 완주 → 샤드 2 승계
            self.assertTrue(restarted.run_due(T0 + INTERVAL_SECONDS))  # 샤드 2 완주
            self.assertEqual(restarted.instances["1"].status, "done")
            summary = restarted.finalize()
            self.assertIsNotNone(summary)
            self.assertEqual(summary["shard_count"], 2)
            self.assertTrue((root / "out" / MERGE_STATE_FILENAME).exists())

    def test_finalize_skipped_for_family_mode_or_incomplete_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            events: list = []
            # 가족 모드 — 병합 대상이 아니다(가족 모드 폴더로 완주 준비).
            shards = self._two_complete_shards(root, prefix="family")
            family_manager = _make_manager(
                root, shards, events, shard_mode=False, instance_count=1
            )
            family_manager.run_due(T0)
            family_manager.run_due(T0 + INTERVAL_SECONDS)
            self.assertEqual(family_manager.instances["1"].status, "done")
            self.assertIsNone(family_manager.finalize())
            self.assertFalse((root / "out" / MERGE_STATE_FILENAME).exists())

            # 분할 모드라도 아직 진행 중이면 병합하지 않는다.
            root2 = Path(tempfile.mkdtemp(dir=tmp))
            events2: list = []
            shards2 = self._two_complete_shards(root2)
            running = _make_manager(root2, shards2, events2, instance_count=1)
            self.assertIsNone(running.finalize())
            # 차단/오류 인스턴스가 남은 실행도 병합하지 않는다(반쪽 데이터).
            running.instances["1"].status = "blocked"
            self.assertIsNone(running.finalize())

    def test_state_persists_shard_mode_and_mode_change_replans(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            events: list = []
            shards = self._two_complete_shards(root)
            _make_manager(root, shards, events, instance_count=1)
            saved = _read_json(root / "out" / STATE_FILENAME)
            self.assertIs(saved["shard_mode"], True)

            # 같은 모드로 재시작하면 배분이 복원되고…
            _make_manager(root, shards, events, instance_count=1)
            self.assertIn("복원", restored_events_text(events))

            # 모드를 바꿔 재시작하면 저장 상태를 버리고 새로 배분한다.
            replanned = _make_manager(
                root, shards, events, shard_mode=False, instance_count=1
            )
            self.assertEqual(replanned.instances["1"].family_index, 0)
            self.assertEqual(replanned.pending_families, [1])
            # family 모드 폴더 기준으로 완주를 다시 확인한다(빈 폴더 → 처음부터).

    def test_work_dir_name_switches_prefix_by_mode(self):
        config = ParallelRunConfig(
            families=[[_SHARD_POOL[0]]],
            output_dir=Path("/tmp/x"),
            instance_count=1,
            shard_mode=True,
        )
        self.assertEqual(config.work_dir_name(0), "shard_01")
        family_config = ParallelRunConfig(
            families=[[_SHARD_POOL[0]]],
            output_dir=Path("/tmp/x"),
            instance_count=1,
        )
        self.assertEqual(family_config.work_dir_name(2), "family_03")


def restored_events_text(events: list[dict]) -> str:
    return str(events[-1].get("message") or "")


# ── 4. 워커 finalize 연동 ─────────────────────────────────────────────


class _FakeManager:
    def __init__(self, *, done_after_runs=0, finalize_result=None, finalize_error=None):
        self.done_after_runs = done_after_runs
        self.run_due_calls = 0
        self.finalize_calls = 0
        self.finalize_result = finalize_result
        self.finalize_error = finalize_error
        self.instances = {
            "1": SimpleNamespace(
                config=SimpleNamespace(
                    instance_id="1", name="인스턴스 1", line="direct"
                ),
                status="waiting",
                family_index=0,
                next_run_at=0.0,
                total_products=0,
                total_sellers=0,
            )
        }

    def all_done(self) -> bool:
        return self.run_due_calls >= self.done_after_runs

    def run_due(self, now: float) -> bool:
        self.run_due_calls += 1
        return True

    def finalize(self):
        self.finalize_calls += 1
        if self.finalize_error is not None:
            raise self.finalize_error
        return self.finalize_result


class WorkerFinalizeTest(unittest.TestCase):
    def _config(self, output_dir: Path) -> ParallelRunConfig:
        return ParallelRunConfig(
            families=[[("100", "샤드A")]],
            output_dir=output_dir,
            instance_count=1,
            shard_mode=True,
        )

    def _record(self, worker):
        class _Rec:
            def __init__(self):
                self.logs: list[str] = []
                self.warnings: list[str] = []
                self.finished: list[dict] = []

        rec = _Rec()
        worker.log_message.connect(rec.logs.append)
        worker.warning_message.connect(rec.warnings.append)
        worker.finished_parallel.connect(rec.finished.append)
        return rec

    def test_finalize_result_reaches_final_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            worker = CoupangParallelWorker(self._config(Path(tmp)), Control())
            merge = {"shard_count": 3, "products": 10, "sellers": 4,
                     "finals": {"all": "/tmp/out/final_dataset_all.csv"}}
            fake = _FakeManager(finalize_result=merge)
            worker.manager = fake
            rec = self._record(worker)
            worker.run()
            self.assertEqual(fake.finalize_calls, 1)
            summary = rec.finished[0]
            self.assertEqual(summary["merge"], merge)
            self.assertIsNone(summary["merge_error"])
            self.assertTrue(summary["shard_mode"])

    def test_finalize_failure_becomes_merge_error_not_run_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            worker = CoupangParallelWorker(self._config(Path(tmp)), Control())
            worker.manager = _FakeManager(
                finalize_error=RuntimeError("병합 폭발")
            )
            rec = self._record(worker)
            worker.run()
            summary = rec.finished[0]
            self.assertIsNone(summary["error"])  # 수집 자체는 정상 완료
            self.assertIn("병합 폭발", summary["merge_error"])
            self.assertTrue(rec.warnings)

    def test_cancelled_run_skips_finalize(self):
        with tempfile.TemporaryDirectory() as tmp:
            control = Control()
            control.request_cancel()
            worker = CoupangParallelWorker(self._config(Path(tmp)), control)
            fake = _FakeManager(finalize_result={"shard_count": 1})
            worker.manager = fake
            rec = self._record(worker)
            worker.run()
            self.assertEqual(fake.finalize_calls, 0)
            self.assertIsNone(rec.finished[0]["merge"])


# ── 5. 패널 분할 모드 ─────────────────────────────────────────────────


class PanelShardModeTest(unittest.TestCase):
    def setUp(self) -> None:
        from PyQt6.QtWidgets import QApplication

        from app.core.coupang.categories import CategoryNode
        from app.ui.coupang_parallel_panel import CoupangParallelPanel

        app = QApplication.instance() or QApplication([])
        self.QApplication = app
        self.CategoryNode = CategoryNode
        self.CoupangParallelPanel = CoupangParallelPanel
        self.panel = CoupangParallelPanel()
        self._set_groups()

    def tearDown(self) -> None:
        self.panel.deleteLater()

    def _set_groups(self) -> None:
        node = self.CategoryNode(
            id="194276",
            name="식품",
            uri="/np/categories/194276",
            children=[
                self.CategoryNode("194373", "견과/건과", "/np/categories/194373", []),
                self.CategoryNode(
                    "194688",
                    "축산",
                    "/np/categories/194688",
                    [self.CategoryNode("194690", "소고기", "/np/categories/194690", [])],
                ),
            ],
        )
        self.panel.set_category_groups([("쇼핑", [node])])

    def _select_root(self) -> None:
        group = self.panel.category_tree.topLevelItem(0)
        group.child(0).setSelected(True)

    def _shard_mode(self) -> None:
        self.panel.mode_combo.setCurrentIndex(1)

    def test_shard_mode_builds_shard_families(self):
        self._select_root()
        self._shard_mode()
        config = self.panel.build_run_config(
            output_dir="/tmp/shard_out", instance_count=2
        )
        self.assertIsNotNone(config)
        self.assertTrue(config.shard_mode)
        # 카테고리 4개 × 인스턴스 2 → 샤드 4개(인스턴스당 2배 규칙).
        self.assertEqual(len(config.families), 4)
        ids = [category_id for shard in config.families for category_id, _ in shard]
        self.assertEqual(
            sorted(ids), ["194276", "194373", "194688", "194690"]
        )

    def test_shard_mode_without_selection_returns_none_with_log(self):
        self._shard_mode()
        self.assertIsNone(self.panel.build_run_config(output_dir="/tmp/shard_out"))
        self.assertTrue(
            any("분할 모드" in log for log in self.panel.log_view.toPlainText().splitlines())
        )

    def test_leaf_category_collapses_to_single_shard_and_trims_instances(self):
        # 손자 노드(소고기, 하위 없음)를 선택 — 분할 불가 리프 카테고리.
        group = self.panel.category_tree.topLevelItem(0)
        food = group.child(0)
        food.child(1).child(0).setSelected(True)
        self._shard_mode()
        config = self.panel.build_run_config(
            output_dir="/tmp/shard_out", instance_count=3
        )
        self.assertIsNotNone(config)
        self.assertEqual(len(config.families), 1)
        self.assertEqual(config.instance_count, 1)  # 샤드 수로 축소
        self.assertTrue(
            any("축소" in log for log in self.panel.log_view.toPlainText().splitlines())
        )

    def test_family_mode_unchanged_by_mode_combo_default(self):
        self._select_root()
        config = self.panel.build_run_config(
            output_dir="/tmp/family_out", instance_count=2
        )
        self.assertFalse(config.shard_mode)
        self.assertEqual(len(config.families), 1)
        self.assertEqual(config.work_dir_name(0), "family_01")

    def test_shard_preview_label_and_card_names(self):
        self._select_root()
        self._shard_mode()
        self.assertIn("4개 샤드", self.panel.selected_label.text())
        self.assertEqual(len(self.panel._family_names), 4)
        self.assertTrue(
            all(name.startswith("샤드 ") for name in self.panel._family_names)
        )


class PanelVolumeAwareSplitTest(unittest.TestCase):
    """볼륨 인지 분할 미리보기·실행 설정(2026-10-06 설계 §5-§6)."""

    def setUp(self) -> None:
        from PyQt6.QtWidgets import QApplication

        from app.core.coupang.categories import CategoryNode
        from app.ui.coupang_parallel_panel import CoupangParallelPanel

        app = QApplication.instance() or QApplication([])
        self.QApplication = app
        self.CategoryNode = CategoryNode
        self.CoupangParallelPanel = CoupangParallelPanel
        self.panel = CoupangParallelPanel()
        self._set_groups()

    def tearDown(self) -> None:
        self.panel.deleteLater()

    def _set_groups(self) -> None:
        # productCount 가 붙은 트리 — 채소 1개(큰) + 잎채소·뿌리채소(작은).
        node = self.CategoryNode(
            id="194432",
            name="채소",
            uri="/np/categories/194432",
            product_count=377_000,
            children=[
                self.CategoryNode(
                    "194433",
                    "잎채소",
                    "/np/categories/194433",
                    [],
                    product_count=30_000,
                ),
                self.CategoryNode(
                    "194434",
                    "뿌리채소",
                    "/np/categories/194434",
                    [],
                    product_count=20_000,
                ),
            ],
        )
        self.panel.set_category_groups([("쇼핑", [node])])

    def _select_root(self) -> None:
        group = self.panel.category_tree.topLevelItem(0)
        group.child(0).setSelected(True)

    def _shard_mode(self) -> None:
        self.panel.mode_combo.setCurrentIndex(1)

    def test_preview_reports_volume_and_split(self):
        self._select_root()
        self._shard_mode()
        label = self.panel.selected_label.text()
        self.assertIn("총 물량 427,000개", label)
        self.assertIn("조각 분할", label)
        self.assertIn("작업 단위", label)
        # 큰 카테고리(채소)는 목록 조각으로, 작은 두 개는 whole 로 나뉜다.
        self.assertTrue(
            any("목록" in name for name in self.panel._family_names)
        )

    def test_build_run_config_carries_unit_specs_and_root_family(self):
        self._select_root()
        self._shard_mode()
        config = self.panel.build_run_config(
            output_dir="/tmp/volume_out", instance_count=4
        )
        self.assertIsNotNone(config)
        self.assertIsNotNone(config.unit_specs)
        self.assertIsNotNone(config.root_family)
        self.assertEqual(len(config.root_family), 3)
        kinds = {spec["kind"] for spec in config.unit_specs}
        self.assertIn("pages", kinds)
        self.assertIn("whole", kinds)
        # families 와 단위 사양이 1:1 로 정렬돼 있다(설정 검증 통과).
        self.assertEqual(len(config.families), len(config.unit_specs))

    def test_units_beyond_instance_count_do_not_trim(self):
        """작업 단위가 인스턴스 수보다 많으면 축소하지 않는다(카테고리 수
        상한 극복 — §1)."""
        self._select_root()
        self._shard_mode()
        config = self.panel.build_run_config(
            output_dir="/tmp/volume_out", instance_count=2
        )
        self.assertGreater(len(config.families), config.instance_count)

    def test_shrink_message_uses_work_unit_wording(self):
        """물량 없이 단위 수가 인스턴스 수보다 적을 때 — '작업 단위' 축소."""
        node = self.CategoryNode(
            id="194688",
            name="축산",
            uri="/np/categories/194688",
            children=[],
        )
        self.panel.set_category_groups([("쇼핑", [node])])
        group = self.panel.category_tree.topLevelItem(0)
        group.child(0).setSelected(True)
        self._shard_mode()
        config = self.panel.build_run_config(
            output_dir="/tmp/leaf_out", instance_count=3
        )
        self.assertIsNotNone(config)
        self.assertEqual(config.instance_count, 1)
        logs = self.panel.log_view.toPlainText().splitlines()
        self.assertTrue(
            any("작업 단위" in line and "축소" in line for line in logs),
            logs,
        )


if __name__ == "__main__":
    unittest.main()
