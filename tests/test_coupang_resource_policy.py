"""판매자 상세 세션 리소스 차단 — 네트워크 없음."""

from __future__ import annotations

import unittest

from app.core.coupang.patchright_canary import (
    BLOCKED_RESOURCE_TYPES,
    block_heavy_resources,
)


class _Request:
    def __init__(self, resource_type: str) -> None:
        self.resource_type = resource_type


class _Route:
    def __init__(self, resource_type: str) -> None:
        self.request = _Request(resource_type)
        self.aborted = False
        self.continued = False

    def abort(self) -> None:
        self.aborted = True

    def continue_(self) -> None:
        self.continued = True


class _Context:
    def __init__(self) -> None:
        self.pattern = ""
        self.handler = None

    def route(self, pattern, handler) -> None:
        self.pattern = pattern
        self.handler = handler


class _FailingRoute(_Route):
    def abort(self) -> None:
        raise RuntimeError("already handled")


class BlockHeavyResourcesTest(unittest.TestCase):
    def test_blocks_image_font_media_only(self):
        context = _Context()
        self.assertTrue(block_heavy_resources(context))
        self.assertEqual(context.pattern, "**/*")
        blocked = set(BLOCKED_RESOURCE_TYPES)
        for resource_type in ("image", "media", "font"):
            route = _Route(resource_type)
            context.handler(route)
            self.assertTrue(route.aborted, resource_type)
            self.assertFalse(route.continued, resource_type)
        for resource_type in ("document", "script", "xhr", "fetch"):
            route = _Route(resource_type)
            context.handler(route)
            self.assertFalse(route.aborted, resource_type)
            self.assertTrue(route.continued, resource_type)
        self.assertEqual(blocked, {"image", "media", "font"})

    def test_context_without_route_is_ignored(self):
        self.assertFalse(block_heavy_resources(object()))

    def test_abort_failure_falls_back_to_continue(self):
        context = _Context()
        self.assertTrue(block_heavy_resources(context))
        route = _FailingRoute("image")
        context.handler(route)  # 예외가 밖으로 나가면 안 된다.
        self.assertTrue(route.continued)


if __name__ == "__main__":
    unittest.main()
