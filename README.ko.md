[English](README.md) | 한국어

# ParqBridge

ParqBridge는 Parquet 파일 하나를 로컬 웹 화면에서 살펴보고 Oracle(12.2+, 19c 기준 설계), MySQL 8+, PostgreSQL 16+용 DDL을 만드는 도구입니다. DB 설정이 있으면 연결 테스트, 테이블 생성, 배치 INSERT와 적재 후 행 수 대조까지 할 수 있습니다. FastAPI와 빌드 체인 없는 바닐라 JavaScript로 동작합니다.

현재 아키텍처와 설계 제약은 [설계 문서](docs/DESIGN.md)를 참고하세요.

기본 서버 주소는 `http://127.0.0.1:8765`입니다. DB 프로필이 없거나 설정이 완전하지 않으면 조회와 DDL만 가능한 오프라인 모드로 실행됩니다.

## 요구사항

- Windows 10/11에서 검증
- Python 3.14
- 프로젝트 전용 `.venv`
- `requirements.txt`에 고정된 의존성
- DB 적재를 쓸 때는 Oracle 12.2+, MySQL 8+, PostgreSQL 16+ 중 하나의 접속 정보

macOS/Linux 명령도 함께 제공하지만, 앱 실행과 테스트는 해당 환경에서 검증하지 않았습니다.

## 설치

저장소를 받은 뒤 가상환경과 의존성을 준비합니다.

PowerShell:

```powershell
git clone "https://github.com/Seohd99/ParqBridge.git"
Set-Location ParqBridge
py -3.14 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

macOS/Linux (bash):

```bash
git clone "https://github.com/Seohd99/ParqBridge.git"
cd ParqBridge
python3.14 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt
```

설치 확인과 단위 테스트는 저장소 루트에서 실행합니다.

PowerShell:

```powershell
$python = ".\.venv\Scripts\python.exe"
& $python -c "import fastapi, pyarrow, duckdb, oracledb, pymysql, psycopg; print('dependencies ready')"
& $python -m pytest -q -rs
```

macOS/Linux (bash):

```bash
.venv/bin/python -c 'import fastapi, pyarrow, duckdb, oracledb, pymysql, psycopg; print("dependencies ready")'
.venv/bin/python -m pytest -q -rs
```

## 설정

예시 파일을 복사해 로컬 설정을 만듭니다. 실제 설정 파일은 gitignore 대상입니다.

```powershell
Copy-Item config\parqbridge.example.toml config\parqbridge.toml
```

macOS/Linux (bash):

```bash
cp config/parqbridge.example.toml config/parqbridge.toml
```

Parquet 로컬 경로를 선택하려면 `[server]`에 파일이 들어 있는 폴더를 허용 루트로 지정합니다. 앱은 이 루트 안의 경로만 보여 주며 선택 파일은 복사하거나 수정하지 않습니다.

```toml
[server]
host = "127.0.0.1"
port = 8765
upload_max_mb = 2048
allowed_roots = ["D:\\data"]
# macOS/Linux 예: allowed_roots = ["/home/me/data"]

[load]
batch_size = 10000
child_max_depth = 3
```

DB를 사용할 때는 실제 값으로 해당 섹션을 채웁니다. 비밀번호는 `password_env`가 가리키는 환경변수에 두는 방식을 권장합니다.

```toml
[db.oracle]
host = "db.example.local"
port = 1521
service_name = "ORCLPDB1"
user = "parqbridge_user"
password_env = "PARQBRIDGE_ORACLE_PASSWORD"

[db.mysql]
host = "db.example.local"
port = 3306
database = "parqbridge"
user = "parqbridge_user"
password_env = "PARQBRIDGE_MYSQL_PASSWORD"

[db.postgres]
host = "db.example.local"
port = 5432                    # 기본값 5432
database = "parqbridge"
schema = "public"              # 선택 사항
user = "parqbridge_user"
password_env = "PARQBRIDGE_POSTGRES_PASSWORD"
```

현재 셸 세션에 환경변수를 설정하는 예입니다. 비밀번호를 명령줄 기록에 직접 입력하지 않도록 프롬프트에서 읽습니다.

PowerShell:

```powershell
$secure = Read-Host "Oracle password" -AsSecureString
$env:PARQBRIDGE_ORACLE_PASSWORD = [System.Net.NetworkCredential]::new("", $secure).Password
```

macOS/Linux (bash):

```bash
read -r -s -p "Oracle password: " PARQBRIDGE_ORACLE_PASSWORD
printf '\n'
export PARQBRIDGE_ORACLE_PASSWORD
```

MySQL은 `PARQBRIDGE_MYSQL_PASSWORD`, PostgreSQL은 `PARQBRIDGE_POSTGRES_PASSWORD`를 같은 방식으로 설정합니다. 환경변수 대신 비밀번호를 파일에 둘 경우 `config/parqbridge.local.toml`의 해당 `[db.<kind>]` 섹션에 `password = "..."`를 둡니다. 이 파일도 gitignore 대상입니다. 주 설정 `parqbridge.toml`의 `password`는 무시되고 경고가 표시됩니다.

다른 설정 파일을 사용하려면 서버를 실행하기 전에 환경변수를 지정합니다.

```powershell
$env:PARQBRIDGE_CONFIG = "C:\path\to\parqbridge.toml"
```

macOS/Linux (bash):

```bash
export PARQBRIDGE_CONFIG="/path/to/parqbridge.toml"
```

`config/parqbridge.local.toml`은 기본 설정 파일 위에 병합됩니다. DB 섹션이 없거나 필수 값 또는 비밀번호가 빠져 있으면 해당 DB만 오프라인 상태가 되고, 화면에 사유와 설정 안내가 나타납니다.

## 실행

설정의 host/port로 실행합니다. 기본값은 `127.0.0.1:8765`입니다.

PowerShell:

```powershell
.\.venv\Scripts\python.exe -m app
.\.venv\Scripts\python.exe -m uvicorn app.main:app
```

macOS/Linux (bash):

```bash
.venv/bin/python -m app
.venv/bin/python -m uvicorn app.main:app
```

Uvicorn 직접 실행 명령은 Uvicorn 기본 주소인 `127.0.0.1:8000`을 사용합니다.

브라우저에서 서버 주소를 열고 언어 선택기에서 한국어 또는 영어를 고릅니다. 선택한 언어는 브라우저 `localStorage`에 저장됩니다.

## CLI

`python -m app`은 기존처럼 웹 서버를 실행합니다. 명령줄 작업은 `.venv`의 Python으로 실행하며, 입력 Parquet 경로는 설정의 `[server].allowed_roots` 안에 있어야 합니다. 파일은 원래 위치에서 읽기 전용으로 열립니다.

PowerShell:

```powershell
$python = ".\.venv\Scripts\python.exe"
& $python -m app --help
& $python -m app inspect "D:\data\part.parquet"
& $python -m app inspect "D:\data\part.parquet" --json
& $python -m app preview "D:\data\part.parquet" --offset 100 --limit 25
& $python -m app preview "D:\data\part.parquet" --json
& $python -m app ddl "D:\data\part.parquet" --dialect oracle --mode none --table PB_TEST_PART -o part.sql
& $python -m app load "D:\data\part.parquet" --dialect mysql --table PB_TEST_PART --batch-size 1000
& $python -m app config check
```

macOS/Linux (bash):

```bash
python=.venv/bin/python
"$python" -m app --help
"$python" -m app inspect "/home/me/data/part.parquet"
"$python" -m app inspect "/home/me/data/part.parquet" --json
"$python" -m app preview "/home/me/data/part.parquet" --offset 100 --limit 25
"$python" -m app preview "/home/me/data/part.parquet" --json
"$python" -m app ddl "/home/me/data/part.parquet" --dialect oracle --mode none --table PB_TEST_PART -o part.sql
"$python" -m app load "/home/me/data/part.parquet" --dialect mysql --table PB_TEST_PART --batch-size 1000
"$python" -m app config check
```

`ddl`과 `load`의 dialect 선택지는 현재 지원 방언에서 가져옵니다. `--lang ko|en`은 각 명령 뒤에 붙여 출력 언어를 고릅니다. `load`의 `truncate` 또는 `drop_create`는 대상 부모 테이블명과 같은 `--confirm-table` 값이 필요하며, Ctrl+C는 배치 경계에서 적재를 취소하고 이미 커밋된 행을 결과에 표시합니다. 종료 코드는 성공 `0`, 실패 `1`, 확인 거부 `2`, 취소 `130`입니다. DB 비밀번호는 CLI 인자로 받지 않으며 기존 `password_env` 또는 `config/parqbridge.local.toml` 설정을 사용합니다.

`ddl -o`는 입력 Parquet 또는 Parquet 파일을 출력 대상으로 거부합니다. 기존 출력 파일을 바꾸려면 `--force`를 지정하세요. `ddl`과 `load`는 `--length-scan full|sample`로 길이 측정 범위를 선택할 수 있고 기본값은 `full`입니다.

선택 사항으로 editable console script를 설치할 수 있습니다. 기존 의존성을 설치한 뒤 아래 명령을 직접 실행하면 `parqbridge` 명령을 사용할 수 있습니다.

```powershell
.\.venv\Scripts\python.exe -m pip install -e .
```

## 사용 흐름

1. 업로드 탭에서 `.parquet` 파일을 선택하거나 로컬 경로 탭에서 허용된 폴더를 탐색합니다.
2. 메타데이터, 컬럼 통계, 스키마, flat/nested 분류와 행 페이지 미리보기를 확인합니다.
3. 펼치기 모드, 대상 DB, 테이블명과 컬럼 옵션을 선택해 DDL과 변환 결과 미리보기를 만듭니다.
4. DDL을 복사하거나 `.sql`로 내려받습니다.
5. DB가 온라인이면 연결 테스트 후 기존 테이블 정책을 고르고 `CREATE + INSERT`를 실행합니다.
6. 진행률을 확인합니다. 완료 후 원본 또는 변환 예상 행 수와 DB `COUNT(*)` 결과를 비교합니다.

주요 API는 `GET /api/status`, `GET /api/files/browse`, `POST /api/files/local`, `POST /api/files/upload`, `GET /api/files/{file_id}/inspect`, `GET /api/files/{file_id}/preview`, `GET /api/files/{file_id}/length-scan`, `POST /api/files/{file_id}/ddl`, `POST /api/files/{file_id}/load`, `POST /api/files/{file_id}/release`, `GET /api/jobs/{job_id}`입니다. 파일 핸들과 작업 상태는 프로세스 메모리에 있으므로 서버를 재시작하면 파일을 다시 선택해야 합니다.

## 펼치기 모드

체크하지 않은 기본 상태는 `none`입니다. DDL 미리보기와 적재 계획은 같은 변환 계획을 사용합니다.

| 모드 | 동작 | 예 |
|---|---|---|
| `none` | list/struct/map을 JSON 값 하나로 보존합니다. Oracle은 `CLOB`, MySQL은 `JSON`, PostgreSQL은 `JSONB`를 사용합니다. | `authors: [{name: "Kim"}]` → `AUTHORS` JSON 컬럼 |
| `child` | struct 필드는 부모 컬럼으로 평탄화하고, list/map은 부모 키와 순번·값을 가진 자식 테이블로 만듭니다. 깊이 상한을 넘는 중첩은 JSON으로 둡니다. | `author.name` → `AUTHOR_NAME`; `tags: ["a", "b"]` → 부모 테이블과 `TABLE_TAGS` 자식 테이블의 `ORD`, 값 행 |
| `explode` | 선택한 최상위 list 하나를 행으로 전개합니다. 다른 중첩 컬럼은 JSON으로 둡니다. 기본값은 null/빈 배열의 부모 행을 유지하고 list 값은 `NULL`로 둡니다. | `tags: ["a", "b"]` → 태그마다 한 행 |

`child`에서는 사용자가 고른 최상위 scalar 컬럼을 부모 키로 쓸 수 있습니다. 선택하지 않으면 파일 행 번호 기반 `_PB_ROW_ID`를 생성합니다. `explode`는 list 컬럼 하나를 선택해야 하며, 미리보기에서 예상 행 수를 확인할 수 있습니다.

## 주요 타입 매핑

| Arrow 타입 | Oracle 12.2+ (19c 기준) | MySQL 8+ | PostgreSQL 16+ |
|---|---|---|---|
| int8 | `NUMBER(3)` | `TINYINT` | `SMALLINT` |
| int16 | `NUMBER(5)` | `SMALLINT` | `SMALLINT` |
| int32 | `NUMBER(10)` | `INT` | `INTEGER` |
| int64 | `NUMBER(19)` | `BIGINT` | `BIGINT` |
| uint8/16/32/64 | `NUMBER(3/5/10/20)` | `TINYINT` / `SMALLINT` / `INT` / `BIGINT` `UNSIGNED` | `SMALLINT` / `INTEGER` / `BIGINT` / `NUMERIC(20)` |
| float32 / float64 | `BINARY_FLOAT` / `BINARY_DOUBLE` | `FLOAT` / `DOUBLE` | `REAL` / `DOUBLE PRECISION` |
| decimal(p,s) | `NUMBER(p,s)`; precision 38 초과는 경고 후 문자 타입 | `DECIMAL(p,s)`; precision 65 초과는 경고 후 문자 타입 | `NUMERIC(p,s)` |
| bool | `NUMBER(1)` | `TINYINT(1)` | `BOOLEAN` |
| string | `VARCHAR2(n CHAR)` 또는 `CLOB` | `VARCHAR(n)`, 길이에 따라 `TEXT` / `MEDIUMTEXT` / `LONGTEXT` | 표본 길이는 `VARCHAR(n)`, `max`/`lob`는 `TEXT` |
| date | `DATE` | `DATE` | `DATE` |
| time | `CLOB` (문자열 fallback) | `LONGTEXT` (문자열 fallback) | `TIME(p)`; 6자리 초과는 경고 |
| timestamp / timestamp with zone | `TIMESTAMP(p)` / `TIMESTAMP(p) WITH TIME ZONE` | `DATETIME(p)`; 시간대 값은 UTC 변환 경고 | `TIMESTAMP(p)` / `TIMESTAMPTZ(p)`; 6자리 초과는 경고 |
| binary | `RAW(n)` 또는 `BLOB` | `VARBINARY(n)` 또는 `LONGBLOB` | `BYTEA` |
| list/struct/map, `none` | `CLOB` (선택한 경우 `IS JSON` 검사 추가) | `JSON` | `JSONB` |

문자열·binary 타입 폭은 `sample`, `max`, `lob` 전략으로 정하고, 별도 `length_scan` 옵션은 길이를 재는 범위를 고릅니다. 기본 `full`은 최상위 문자열·binary 집계를 DuckDB(최대 4 스레드, 연결당 메모리 한도 2GB)로 계산하고, 중첩 list·struct·map 리프는 row group 단위 Arrow 배치(1,024행)를 최대 4개 worker로 읽습니다. 문자 수는 Unicode 코드포인트, 바이트 수는 UTF-8 길이로 계산하고 컬럼별 최댓값만 보관하므로 파일 전체를 메모리에 올리지 않습니다. `sample`은 파일 전체에서 고르게 고른 최대 4개 row group의 앞부분, 최대 512행만 측정하고 20% 여유를 둡니다. row group 내부 후반의 긴 값은 표본에서 빠질 수 있습니다.

웹에서 로컬 파일이나 업로드 파일을 선택하면 전체 측정이 백그라운드에서 시작됩니다. 측정 상태는 `GET /api/files/{file_id}/length-scan`으로 확인할 수 있습니다. 완료 전 `full` DDL 요청은 표본 추정 DDL과 진행 상태를 즉시 돌려주고, 측정이 끝나면 화면에서 확정 DDL을 자동으로 다시 불러옵니다. 적재는 작업 안에서 확정 측정이 끝날 때까지 기다리며, 그 측정값으로 화면의 확정 DDL과 같은 테이블 모델을 만듭니다. CLI에서는 동기식으로 측정합니다.

`full` 측정은 현재 파일의 최대값에 여유를 두지 않으므로, 다른 파일을 같은 테이블에 `append`할 때 더 긴 값이 들어오면 폭이 부족할 수 있습니다. 여러 파일을 이어 넣을 계획이면 `max` 또는 `lob`를 고려하세요. Oracle `max`는 `VARCHAR2(1000 CHAR)`를 기본 폭으로 쓰고 측정값이 1,000자를 넘거나 4,000바이트 한도를 넘으면 `CLOB`으로 올립니다. MySQL `max`는 측정값에 맞춰 `VARCHAR(16383)`, `TEXT`, `MEDIUMTEXT`, `LONGTEXT` 중 필요한 폭을 선택하며 테이블 행 크기 예산에 따라 추가 강등될 수 있습니다. PostgreSQL `max`와 `lob`는 길이 제한 없는 `TEXT`를 씁니다. Oracle `lob`는 `CLOB`, MySQL `lob`는 `LONGTEXT`입니다.

## 기존 테이블 정책

`CREATE + INSERT` 전에 정책을 고릅니다.

- `fail`: 같은 이름의 테이블이 있으면 작업을 거부합니다. 기본값입니다.
- `append`: 기존 테이블 컬럼 집합이 계획과 맞을 때 행을 추가합니다.
- `truncate`: 확인된 대상 테이블을 비우고 적재합니다.
- `drop_create`: 확인된 테이블을 삭제한 뒤 다시 만듭니다.

`truncate`와 `drop_create`는 대화상자에서 부모 테이블명을 정확히 다시 입력해야 합니다. child 모드에서는 영향을 받는 부모·자식 테이블 목록도 표시됩니다.

## 테스트와 골든 파일

전체 테스트를 실행합니다.

PowerShell:

```powershell
.\.venv\Scripts\python.exe -m pytest -q -rs
```

macOS/Linux (bash):

```bash
.venv/bin/python -m pytest -q -rs
```

통합 테스트는 해당 DB 프로필이 온라인일 때만 실행됩니다. 설정이 없으면 `oracle`/`mysql`/`postgres` 마커 테스트가 사유와 함께 skip됩니다. DB를 사용하는 경우 테스트는 `PB_TEST_` 테이블만 만들고 정리합니다.

마커별 실행:

```powershell
.\.venv\Scripts\python.exe -m pytest -q -m oracle -rs
.\.venv\Scripts\python.exe -m pytest -q -m mysql -rs
.\.venv\Scripts\python.exe -m pytest -q -m postgres -rs
```

macOS/Linux (bash):

```bash
.venv/bin/python -m pytest -q -m oracle -rs
.venv/bin/python -m pytest -q -m mysql -rs
.venv/bin/python -m pytest -q -m postgres -rs
```

DDL 출력을 의도적으로 바꿨을 때만 골든 파일을 갱신합니다.

```powershell
@'
import os
import pytest
os.environ["UPDATE_GOLDEN"] = "1"
raise SystemExit(pytest.main(["-q", "tests/test_ddl.py"]))
'@ | .\.venv\Scripts\python.exe -
```

macOS/Linux (bash):

```bash
UPDATE_GOLDEN=1 .venv/bin/python -m pytest -q tests/test_ddl.py
```

갱신 후 `tests/golden/*.sql` 변경 내용을 살피고 전체 테스트를 다시 실행합니다.

## 대용량 측정

기존 1 GiB 이상 파일을 선택해 미리보기·inspect 응답 시간과 fake-driver 적재 RSS를 측정할 수 있습니다. 원본은 읽기 전용으로 열며 복사하거나 수정하지 않습니다. RSS는 Windows `GetProcessMemoryInfo`의 peak working set을 사용합니다.

아래 스크립트는 Win32 `GetProcessMemoryInfo` API를 사용하므로 Windows 전용입니다.

```powershell
.\.venv\Scripts\python.exe tools\measure_large_parquet.py "D:\data\large.parquet" --batch-sizes 1000,5000,10000
```

스크립트는 선택 파일의 크기, 행 수와 row group 수, API 첫 페이지와 90% 위치 OFFSET 페이지 시간, 배치 크기별 `execute_load` 시간과 RSS 증가량을 JSON으로 출력합니다. DB 드라이버 대신 삽입 행을 보관하지 않는 fake MySQL 연결을 사용하므로 실DB 적재 성능을 측정하지 않습니다.

## 플랫폼별 코드·검증 참고

- 자동 테스트와 앱 사용은 Windows 10/11에서 확인했습니다. macOS/Linux용 명령은 제공하지만 해당 플랫폼에서 실행하거나 검증하지 않았습니다.
- `tools/measure_large_parquet.py`의 RSS 측정은 Windows `psapi.GetProcessMemoryInfo`를 사용합니다.
- 경로 검증은 플랫폼 경로 API를 사용하고, Windows junction API가 있는 경우 junction도 검사합니다. `tests/test_files.py`의 심볼릭 링크 탈출 테스트는 테스트 환경에서 링크 생성을 허용하지 않으면 skip합니다.
- 업로드 파일명 정리 과정은 Windows 백슬래시도 경로 구분자로 정규화합니다.

## 보안과 운영 주의

- 서버는 기본적으로 `127.0.0.1`에만 바인드합니다. 공유 네트워크에 공개하지 마세요.
- 원본 로컬 Parquet은 허용 루트 안에서 읽기 전용으로만 선택됩니다. 업로드 파일만 업로드 목록에서 삭제할 수 있습니다.
- `allowed_roots`는 필요한 폴더로 좁히세요. 경로 탈출과 허용 루트 밖을 가리키는 링크는 차단됩니다.
- 비밀번호는 환경변수 또는 gitignore된 `parqbridge.local.toml`에 둡니다. 비밀번호와 DSN을 로그, 화면, 응답, 테스트 fixture에 기록하지 마세요.
- 브라우저를 닫거나 서버를 재시작하면 메모리의 선택 파일 핸들과 작업 상태가 사라집니다.

## 알려진 제한

- map 키를 wide 컬럼으로 펼치는 기능은 후속 과제입니다. 현재는 `none`의 JSON 또는 `child`의 map 자식 테이블을 사용합니다.
- `length_scan=sample`은 추정치이며 파일 전체 최댓값을 보장하지 않습니다. 표본 밖 긴 값이 의심되거나 적재 오류가 나면 `length_scan=full`로 다시 계획하세요. 전체 스캔의 정확한 최대 폭도 다른 파일의 `append` 값까지 보장하지는 않습니다.
- `LOAD DATA LOCAL INFILE` 경로는 비활성화되어 있습니다. 현재 MySQL 적재는 배치 `executemany`를 사용합니다.
- DB 실적은 드라이버, 네트워크, 스키마와 서버 설정에 따라 달라집니다. fake-driver 측정값은 실DB 적재 속도나 메모리를 뜻하지 않습니다.
- 화면의 파일 핸들 및 작업 이력은 프로세스 메모리에만 보관됩니다.

## 라이선스

[MIT](LICENSE)
