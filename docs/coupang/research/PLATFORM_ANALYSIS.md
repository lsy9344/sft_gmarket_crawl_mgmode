> **⚠ 아카이브됨 — 구현 전 연구 (rev.1~5).** 이 문서는 구현 *이전*의 연구/설계
> 문서로, 실측 결과 여러 전제가 폐기됐다. 현재 기준은 **`../CRAWL_RESULTS.md`** 가
> 우선한다. 역사적 참고용(안티봇 우회 연구 등)으로 보관한다.

# Coupang OMP Crawler - Platform Analysis

## Target

- URL: `https://www.coupang.com/np/omp`
- OMP = Open Market Place (3자 판매자 입점 상품 전용 섹션)
- 로켓배송(직매입) 제외, 오픈마켓 판매자 상품만 노출

## Anti-Bot System: Akamai Bot Manager (5-Layer)

| Layer | Detection Method | Detail |
|-------|-----------------|--------|
| 1 | HTTP/TLS Fingerprint | TLS ClientHello, HTTP/2 프레임 순서, cipher suite 분석 |
| 2 | JS Challenge (`_abck` cookie) | ~70KB 센서 스크립트 실행 필수, 서버측 검증 |
| 3 | Browser Fingerprint | Canvas, WebGL, AudioContext, 폰트, 플러그인 |
| 4 | Behavioral Analysis | 마우스 궤적, 키스트로크 다이나믹스, 스크롤 패턴 |
| 5 | IP Reputation | 데이터센터/VPN IP 즉시 차단, 해외 IP 차단 |

### Gmarket(Cloudflare) vs Coupang(Akamai) 비교

| 항목 | Gmarket | Coupang |
|------|---------|---------|
| 보호 범위 | 목록 페이지만 Cloudflare | **전 사이트** Akamai |
| 무보호 엔드포인트 | `mg.gmarket.co.kr` 존재 | **없음** |
| JS 실행 | 목록만 필요 | **모든 페이지 필수** |
| 행동 분석 | 없음 | **마우스/키보드/스크롤** |
| TLS 검증 | 기본 | **엄격 (HTTP/2 포함)** |
| IP 제한 | 한국 IP면 충분 | **한국 주거용 IP 필수** |
| 센서 스크립트 | 없음 | **70KB _abck 생성** |

## Access Test Results (2026-07-25)

| Method | Result |
|--------|--------|
| `requests` (no headers) | 403 Forbidden |
| `requests` + Chrome UA + Accept-Language | 403 or empty |
| Server-side fetch (WebFetch) | 403 Forbidden |
| Selenium/Playwright (default) | Blocked within 5 min |
| HTTP/2 automated traffic | Intermittent blocking |

## Coupang-Specific Blocking Behaviors

1. **HTTP/2 차단**: 자동화 HTTP/2 트래픽 비주기적 차단 (Coupang 고유)
2. **세션 리셋**: 연속 요청 시 세션 무효화
3. **자동화 탐지 경고**: "Chrome이 자동화된 테스트 소프트웨어에 의해 제어되고 있습니다"
4. **외국 IP 즉시 차단**: challenge page 또는 empty result 반환
5. **비주기적 차단**: 패턴 기반이 아닌 불규칙 차단 (회피 어려움)

## Detection Indicators (for monitoring)

```python
BOT_KEYWORDS_COUPANG = [
    "자동화된 테스트 소프트웨어",
    "접근이 제한",
    "보안 절차",
    "확인 절차",
    "잠시 후 다시 시도",
]
```

HTTP Status Codes:
- `403`: Akamai block
- `418`: Teapot (bot detection)
- `429`: Rate limit
- Empty response with 200: Soft block
