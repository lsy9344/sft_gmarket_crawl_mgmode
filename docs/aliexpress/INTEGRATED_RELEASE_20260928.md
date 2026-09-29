# Full-tab release with latest Ali changes

Deployment: `C:\Users\dltnd\Desktop\ShipTest\SellerCollector.exe`.

Uses the verified executable built in `AliCollectorPhoneBuild_20260928`. Its normal entry point shows all seven tabs; `--ali-only` remains an optional isolated mode. Includes Ali resource blocking, recovery rotation budget, pending-item resume, and phone extraction/cache improvements. No rebuild was needed to enable the existing full-tab mode.

- SHA256: `523dba1066a68aa0a336e9fbb533da85235a11792ca81afc74f00d11055feb70`
- Previous deployment preserved as `SellerCollector_before_ali_integration_20260928.exe`.
- `SHA256SUMS.txt` updated; existing runtime setup executable retained.
- `Start-All.cmd` / `run_integrated_in_sandbox.ps1` start the same executable without `--ali-only`, using a separate runtime filename so the currently running Gmarket executable is untouched.
- Existing dataset and resume stores were not changed by deployment.

Validation: 170 Windows tests already passed for this executable's source. Real Qt normal-mode check confirmed Gmarket, Coupang, Foodspring, Coupang category, Gmarket category, Ali category, and Settings are visible, with no active collection. Frozen normal-mode startup and normal log creation passed. Launcher syntax and deployed checksum verified.

The category-route and last-page issues documented in `CATEGORY_ROUTE_AUDIT_20260928.md` are not fixed by this integration. This release must not be described as resolving category coverage.
