결론: **데이터 파일은 사용 가능하지만, 현재 EXE 배포본은 승인 문서·인증서 패키지가 맞지 않아 최종 배포는 HOLD입니다.**

## Review
- **Correct — 실행 파일:** `dist/SellerCollector.exe`는 141,752,080바이트의 Windows x64 PE32+ 파일이다. PyInstaller one-file 표식과 번들 목차에서 GUI, 설치 모듈, Playwright/Patchright 드라이버를 확인했다.
- **Correct — 무결성:** 실제 SHA-256은 `75851aea347f18be7dfa6e0539c67ab3a29b3c9b2a3750ec293b05c31653cfe7`이며 `dist/SHA256SUMS.txt`와 일치한다.
- **Correct — 서명:** Windows `Get-AuthenticodeSignature` 결과는 `Valid`였다. 서명자는 `CN=SellerCollector Internal Release`, thumbprint는 `E9A454D2B5FE0BE5A8D10D7ACD965F7E80EB322A`이며 DigiCert 타임스탬프도 존재한다. PE 내 서명 대상 해시와 실제 Authenticode SHA-256도 일치했다.
- **Correct — 실제 런타임 확인:** 해당 EXE의 `--verify-runtime`이 현재 Windows 호스트에서 exit 0을 반환했다. Camoufox 0.5.4, 브라우저 `152.0.4-beta.28-386fc2f4`, GeoIP, Gmarket Chromium을 모두 확인했다.
- **Correct — 데이터:** 두 결과 파일은 각각 9,852행, 동일한 14필드 순서이며 CSV/JSON 셀 불일치가 0건이다. CSV는 Excel 호환 UTF-8 BOM, JSON은 정상 UTF-8이다. `vendor_id`와 URL은 모두 고유하고 핵심 사업자 필드는 전 행에서 채워져 있다.
- **Blocker (HIGH) — 승인 증적 불일치:** 현재 EXE의 크기·해시는 `docs/RELEASE_READINESS.md:41-46` 및 `docs/DEPLOYMENT_APPROVAL.md:78-85`에 승인된 이전 후보(`6F5A...`, 141,696,136바이트)와 다르다. 따라서 현재 파일은 문서상 승인된 후보가 아니다.
- **Blocker (HIGH) — 인증서 누락:** `dist/`와 Windows 빌드 staging의 배포 폴더에는 자체서명 공개 인증서가 없다. 하지만 내부 설치 절차는 이를 신뢰 저장소에 설치하도록 요구한다(`docs/DEPLOYMENT_APPROVAL.md:89-95`). 새 PC나 제한된 회사 PC에서는 실행이 차단될 수 있다.
- **Note (MEDIUM) — 최신 clean-PC 검증 부재:** 문서의 clean Windows 설치·GUI 검증은 2026-07-28 이전 해시 후보에 대한 것이다(`docs/RELEASE_READINESS.md:16-34`). 현재 EXE는 기존 런타임이 설치된 호스트에서만 확인했으므로 최초 설치 경로는 아직 재검증되지 않았다.
- **Note (MEDIUM) — 단일 EXE 범위:** Python 코드와 드라이버는 단일 EXE에 포함되지만 브라우저 런타임은 포함되지 않는다. 최초 사용 시 약 1.4GB 다운로드와 동일 Windows 사용자 실행이 필요하다(`README.md:331-355`). 배포 폴더에 README/사용 설명서가 없어 일반 사용자가 이 절차를 놓칠 수 있다.
- **Note (LOW) — 패키지 정리:** 로컬 `dist/`에는 Windows EXE 외에 Linux ELF 파일 3개도 있다. 정식 staging 폴더는 EXE와 해시 파일만 있어, 로컬 `dist/` 전체를 그대로 전달하면 혼동을 준다.
- **Note (LOW) — 데이터 타입:** `store_name` 2,187건이 JSON `null`이고 CSV에서는 빈 셀이다. 공란 자체는 허용된다(`docs/coupang/WORK_ORDER.md:821-828`)지만 모델은 문자열로 선언되어 있어(`app/models/coupang_records.py:70-85`) 엄격한 JSON 소비자는 보정이 필요하다. 유효 커버리지는 7,665/9,852(77.8%)이다.
- **Note (INFO):** 요청된 `plan.md`와 `progress.md`는 지정 경로에 존재하지 않았다.

**사용자 영향:** 데이터는 바로 Excel·JSON 처리에 사용할 수 있지만, EXE를 신규 사용자에게 전달하면 신뢰 경고나 런타임 설치 안내 부족으로 첫 실행이 막힐 수 있다.

**다음 행동:** 현재 EXE 해시로 승인 문서를 갱신하고 공개 인증서·간단 설치 안내를 함께 묶은 뒤, 깨끗한 Windows 사용자 환경에서 `setup → verify → GUI 실행`을 다시 확인해야 한다.

**최종 판정: 데이터 PASS / 현재 EXE 내부·외부 배포 HOLD.** 라이브 수집은 수행하지 않았다.