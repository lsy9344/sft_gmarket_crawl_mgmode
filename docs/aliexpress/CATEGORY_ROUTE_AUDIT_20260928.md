# Ali category route live audit — 2026-09-28

Live browser inspection around 14:09–14:14 KST. No paid proxy was used and no collector was started.

## Observed

- App saved route: `https://ko.aliexpress.com/w/wholesale-%EC%95%BC%EC%B1%84.html?categoryTab=food_%26_grocery&isFromCategory=y`.
- That route displayed a last-page control of **60** in this browser session.
- App route with `page=9` returned different product links: 67 distinct link IDs versus 63 on page 1; 63 page-9 IDs differed from page 1. These are whole-document link counts, not validated category-product counts; recommendations/cart links may be included.
- 47 IDs from that live page-9 response matched the existing stored product list (1,321 IDs).
- Actually hovered Home → 모든 카테고리 → 식품과 식료품 and clicked 야채. Its current link included `postCatIds=201375603%2C201376502%2C201381201`, `q=fresh%2520vegetables`, and category-navigation parameters missing from the app route.
- With that current menu route at `page=8`, the visible pagination was `1 … 6 7 8 9 10 … 29`; response pageInfo said totalResults=1728, page=8, pageSize=60.
- At `page=9`, visible pagination was `1 … 7 8 9 10 11 … 29`, active page=9, next aria-disabled=false. Response pageInfo said totalResults=1728, page=9, pageSize=60.
- Browser screenshots: `ali-page1-pagination.png` (app route, 60); `ali-real-category-page9-pagination.png` (current menu route, 29).

## Limits and corrections

- User reports a last page of 8. That exact view has not been reproduced; requested its address for comparison. Do not assert the user is wrong or infer the cause from region/account alone.
- `wholesale` in the URL does NOT prove it is unrelated to site categories: the actual current category menu also uses that route form.
- Existing 96.4% means 1,273 / 1,321 stored targets processed. It is not verified coverage of the user's category.
- App bundles a static category tree and does not verify the visible last page. Its whole-document item-link selector can include non-listing links.
- These findings establish different request routes, not which individual saved records belong to the user's 8-page view. Do not delete records or rebuild the collection based on this audit alone.
