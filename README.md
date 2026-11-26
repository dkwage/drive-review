# Drive Review

외장 드라이브의 사진, 영상, 문서와 ZIP 항목을 썸네일로 검토하는 Windows 10/11 앱입니다. 초록 플래그는 업로드, 빨간 플래그는 격리를 뜻합니다. 파일 내용을 비교한 결과는 컴퓨터에 저장되어 다른 드라이브를 검토할 때도 사용할 수 있습니다.

## 시작하기

Windows 빌드의 `DriveReview-windows` 압축 파일을 풀고 `DriveReview.exe`를 실행합니다. 실행 파일과 함께 제공되는 폴더를 같은 위치에 둡니다.

1. 위쪽 목록에서 외장 드라이브를 선택하거나 **폴더 열기**를 누릅니다. 방금 연결한 드라이브는 **새로 고침**으로 찾을 수 있습니다.
2. 검토자 이름을 입력합니다. 폴더 트리에서 ZIP을 펼치면 압축을 풀지 않고 내부 파일을 살펴볼 수 있습니다.
3. 그리드에서 파일을 선택하고 키를 누릅니다. 여러 파일은 Shift 또는 Ctrl로 선택할 수 있습니다. macOS에서는 Command로 개별 선택을 추가할 수 있습니다.

| 키 | 동작 |
| --- | --- |
| P | 초록 플래그 지정 |
| X | 빨간 플래그 지정 |
| U | 플래그 해제 |
| 방향키 | 그리드에서 이동 |
| Space | 큰 썸네일로 미리 보기 |
| Enter 또는 더블 클릭 | 기본 앱에서 임시 사본 열기 |

분류, 플래그, 파일명이나 경로로 결과를 좁힐 수 있습니다. 정렬 기준은 이름, 크기, 수정 시간, 폴더입니다. 휴지통과 시스템 파일도 검토해야 할 경우 **시스템 항목 표시**를 켭니다. 상태 표시줄에서 플래그별 개수와 합계 용량을 확인할 수 있습니다.

**중복 찾기**를 누르면 같은 크기의 파일을 부분 해시 또는 ZIP CRC32로 좁히고, 전체 BLAKE2b 해시로 일치 여부를 판정합니다. **바이트 비교**를 켜면 열 수 있는 일치 파일을 직접 비교합니다. 첫 검색은 다른 드라이브와 비교할 해시를 만들므로 드라이브 전체를 읽습니다. 취소한 작업을 다시 시작하면 저장된 해시를 재사용합니다.

**중복 그룹**에서 보관 제안과 드라이브 이름을 확인합니다. 자동으로 붙은 빨간 플래그의 사유는 `duplicate`입니다. P, X, U로 변경할 수 있습니다. 검토가 끝나면 플래그 저장이 완료되도록 창을 닫고 드라이브를 안전하게 분리한 뒤 담당자에게 전달합니다.

## 드라이브 담당자

- **업로드 세트 준비**를 눌러 원본 드라이브 밖의 로컬 폴더를 지정합니다. 일반 파일의 상대 경로가 유지됩니다. ZIP 항목은 기본적으로 `zip_entries/<ZIP 상대 경로>/<ZIP 내부 경로>`에 저장됩니다. `manifest.csv`에는 `dest_path,source_rel_path,zip_entry,hash`가 들어갑니다. 이 폴더를 별도 업로드 도구에 전달할 수 있습니다.
- **빨간 파일 격리**를 누르고 개수와 용량을 확인합니다. 원본 파일은 `_drive_review/quarantine/` 아래의 같은 상대 경로로 이동합니다. ZIP 내부 빨간 항목은 검토 기록에 남습니다.
- **격리 실행 취소**를 누르면 격리한 파일을 원래 위치로 돌려놓습니다. 동일한 경로에 파일이 생겼다면 충돌을 표시하고 격리 사본을 보존합니다. 이동과 복구 이력은 `_drive_review/quarantine_log.csv`에 저장합니다.
- **Excel 목록 내보내기**는 분류, 파일명, 확장자, 경로, 크기, 수정 시간, Flag, Hash를 기록합니다. 선택한 원본의 목록을 새 시트로 추가합니다.

## flags.csv

`<선택한 드라이브 또는 폴더>/_drive_review/flags.csv`는 UTF-8 BOM CSV입니다. 플래그 변경 후 자동 저장하며, 같은 원본을 다시 열면 이어서 검토할 수 있습니다.

```csv
rel_path,zip_entry,flag,reason,size,mtime_iso,hash,category,reviewer,flagged_at_iso
```

| 열 | 값 |
| --- | --- |
| `rel_path` | 선택한 원본을 기준으로 하는 상대 경로. 구분자는 `/`입니다. |
| `zip_entry` | ZIP 내부 항목의 상대 경로. 일반 파일은 빈 값입니다. |
| `flag` | `green` 또는 `red`. 미지정 항목은 저장하지 않습니다. |
| `reason` | 검토자가 지정하면 `manual`, 중복 검사에서 자동 지정하면 `duplicate`. |
| `size`, `mtime_iso` | 바이트 단위 크기와 ISO 8601 형식의 UTC 수정 시각. |
| `hash` | 확인된 BLAKE2b-256 해시의 16진수 값. 해시 계산 전에는 비어 있을 수 있습니다. |
| `category` | `Video`, `Image`, `Docs`, `Music`, `Compressed`, `Design`, `Etc` 중 하나. |
| `reviewer`, `flagged_at_iso` | 검토자 이름과 플래그 지정 시각(UTC). |

읽기 전용 드라이브에서 저장하면 창에 로컬 CSV 위치가 표시됩니다. 해당 CSV는 같은 컴퓨터에서 다시 열 수 있습니다. 다른 컴퓨터에 인계할 때는 드라이브의 CSV가 최신인지 확인하세요. 파일의 크기나 수정 시간이 달라지면 그 파일의 플래그를 다시 확인해야 합니다.

## Decisions

- Windows의 인덱스와 썸네일은 `%LOCALAPPDATA%\DriveReview`에, macOS에서는 `~/Library/Application Support/DriveReview`에 저장합니다. 볼륨 일련번호나 UUID가 있으면 이를 사용하며, 사용할 수 없으면 선택한 원본의 경로를 사용합니다. 썸네일은 Windows Shell, Pillow 또는 macOS Quick Look에서 가져오며, 썸네일을 만들 수 없는 유형은 분류 아이콘으로 표시합니다. [Microsoft Windows Shell 썸네일 API](https://learn.microsoft.com/windows/win32/api/shobjidl_core/nf-shobjidl_core-ishellitemimagefactory-getimage)
- 보관 제안은 시스템 폴더 밖의 파일, 일반 파일, 더 얕은 경로, 더 오래된 수정 시간, 더 짧은 이름 순으로 우선합니다. 수동으로 지정한 플래그를 유지합니다. 다른 드라이브에만 보관본이 있는 경우 인덱스에 **초록**으로 기록된 일치 파일이 있어야 자동 격리합니다. 바이트 비교를 켠 경우 해당 보관본도 실제로 열어 비교해야 합니다.
- ZIP 중앙 디렉터리에서 목록을 읽습니다. ZIP 영상과 64 MiB를 초과하는 ZIP 항목의 썸네일은 미리 보기를 요청할 때만 만듭니다. 손상되었거나 암호화된 ZIP 및 안전하게 경로를 해석할 수 없는 ZIP은 경고가 붙은 일반 파일로 표시합니다. 파일을 기본 앱에서 열 때는 원본을 보호하기 위해 임시 사본을 사용합니다. 대용량 파일은 복사할 로컬 공간이 필요합니다.
- 업로드 경로와 일반 파일명이 충돌하면 ZIP 항목 폴더에 숫자를 붙입니다. 원본의 최상위 이름이 `manifest.csv`이면 그 경로를 `_drive_review_sources/` 아래에 둡니다. 매니페스트의 `dest_path`가 실제 저장 경로를 가리킵니다.
- 격리 로그를 이동 전에 기록합니다. 이동이 중단되면 실제 파일 위치를 검사하여 취소를 다시 실행할 수 있습니다. 이 앱은 파일을 영구 삭제하는 기능을 제공하지 않습니다.

## 개발과 빌드

Python 3.12가 필요합니다. macOS에서 저장소 루트에서 실행합니다.

```sh
/opt/homebrew/bin/uv venv --python 3.12 .venv
/opt/homebrew/bin/uv pip install --python .venv/bin/python -r requirements.txt
.venv/bin/python main.py
QT_QPA_PLATFORM=offscreen .venv/bin/python -m unittest discover -s tests
```

Windows에서는 `build.bat`를 실행하면 의존성을 설치하고 테스트를 실행한 뒤 `dist\DriveReview\DriveReview.exe`를 만듭니다. GitHub Actions의 Windows 작업도 같은 방식으로 빌드합니다.
