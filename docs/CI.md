# CI 의존성

CI는 Linux 자체 러너에서 Python 3.12 가상환경을 새로 만들고
`requirements-ci.lock`을 해시 검증과 함께 설치한다. `requirements.txt`는 실행 환경의
직접 의존성 목록으로 남기고, CI 전이 의존성은 별도 잠금 파일에 둔다.

잠금 파일을 다시 만들 때는 Linux x86_64 러너와 같은 조건으로 다음을 실행한다.

```bash
uv pip compile requirements-ci.in \
  --python-version 3.12 \
  --python-platform x86_64-manylinux_2_34 \
  --generate-hashes \
  --output-file requirements-ci.lock
sha256sum requirements.txt requirements-ci.in > requirements-ci.inputs.sha256
```

`requirements-ci.in`이나 실행 의존성을 바꾼 뒤에만 잠금 파일과 입력 해시를 함께 갱신한다.
CI는 설치 전에 입력 해시를 검사한다. 패키지 저장소는 작업 폴더 밖의
`runner.tool_cache/package-stores/.../pip`에 남기며, `node_modules`처럼 작업 결과를
재사용하지 않는다.

이 잠금은 glibc 2.34 이상인 Linux x86_64 러너를 기준으로 한다. 더 오래된 Linux를
사용하면 PyQt6 wheel 조건을 확인한 뒤 플랫폼 옵션과 잠금을 다시 맞춘다.
