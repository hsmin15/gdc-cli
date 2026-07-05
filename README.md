# GDC CLI

**GDC(Genomic Data Commons) 데이터를 CLI로 검색·다운로드·조립해 ML 학습용 테이블로 만드는 도구.**

암 오믹스 데이터(발현/변이/CNV/miRNA/메틸레이션/단백질)와 임상 정보를 코호트 정의 한 줄로
검색·다운로드하고, `case × 유전자` 행렬로 조립한 뒤 하나의 wide 테이블로 조인한다. 모든 실행은
결정론적 CLI가 담당하며, LLM 에이전트가 자연어 요청을 CLI 명령으로 변환해 실행하는 것을 목표로
설계되었다(에이전트 플레이북은 [`CLAUDE.md`](CLAUDE.md) / [`AGENTS.md`](AGENTS.md) 참고).

> A CLI to search, download, and assemble NCI Genomic Data Commons data into
> machine-learning-ready tables. Define a cohort once and the tool searches file
> metadata, downloads (with resume + md5 verification), assembles per-case/per-sample
> omics matrices, and joins them with clinical labels into a single wide table.

## 설치

```bash
git clone <this-repo-url>
cd gdc-cli
pip install -e .
```

- Python 3.10+
- 의존성: pandas, pyarrow, PyYAML, requests, rich, tenacity, typer (자동 설치)
- 설치 후 `gdc` 명령 또는 `python -m gdc_cli.cli` 로 실행(동일).

**gdc-client(선택):** 공식 [GDC Data Transfer Tool](https://gdc.cancer.gov/access-data/gdc-data-transfer-tool)이
PATH에 있으면 병렬·재개 다운로더로 자동 사용된다. 없으면 내장 다운로더(순차, HTTP Range 재개,
md5 검증)로 폴백한다.

## 빠른 시작

```bash
# 1) 필드/값 탐색 — 필터를 만들기 전에 실제 필드명·값 철자를 확인
gdc fields cases -s stage
gdc values cases primary_site

# 2) 검색 (메타 TSV 생성)
gdc search cases -f "project.project_id = TCGA-LUAD" \
  --fields "case,sex,age,stage" --size 20 --out out/luad.tsv

# 3) 원샷 파이프라인: 코호트 정의 → 검색 → 다운로드 → 조립 → 조인
#    먼저 --dry-run 으로 코호트 크기와 다운로드 용량을 확인
gdc build-dataset --cohort-filter "cases.primary_site in bronchus and lung" \
  --omics expr,mut,cna --paired --tumor-only --age-unit years \
  --clinical "sex,age,stage" --out out/lung.parquet --dry-run

#    확인 후 --dry-run 을 빼면 실제 수집
gdc build-dataset --cohort-filter "cases.primary_site in bronchus and lung" \
  --omics expr,mut,cna --paired --tumor-only --age-unit years \
  --clinical "sex,age,stage" --out out/lung.parquet

# 4) 결과 QC
gdc describe out/lung.parquet
```

## 명령어 개요

| 명령 | 설명 |
|---|---|
| `gdc fields <endpoint>` | 필드 목록/검색 (cases·files·projects·annotations) |
| `gdc values <endpoint> <field>` | 특정 필드의 값 분포(facet)와 건수 |
| `gdc aliases` | 필드 별칭 / 필터 별칭(플래그) 목록 |
| `gdc search <endpoint>` | 필터로 검색해 메타 TSV/JSON 출력 |
| `gdc download` | 검색 결과(또는 필터)로 파일 다운로드(매니페스트/데이터) |
| `gdc assemble-{expr,mut,cnv,mirna,methyl,protein}` | 다운로드한 파일 → `case × 유전자` 행렬 |
| `gdc build-ml` | 임상 + 오믹스 행렬들을 case_id로 조인 |
| `gdc build-dataset` | 검색→다운로드→조립→조인을 한 번에(권장 진입점) |
| `gdc describe <table>` | 만들어진 테이블의 경량 QC |

각 명령의 전체 옵션은 `gdc <명령> --help` 또는 [`CLAUDE.md`](CLAUDE.md)의 명령어 레퍼런스를 참고.

## 환경변수

`.env` 파일 또는 셸 환경변수로 설정한다(`.env`는 `.gitignore`에 포함되어 커밋되지 않는다).

| 변수 | 기본값 | 설명 |
|---|---|---|
| `GDC_TOKEN` | (없음) | 통제접근(controlled) 파일 다운로드용 토큰. open 데이터는 불필요 |
| `GDC_API_BASE_URL` | `https://api.gdc.cancer.gov` | API 베이스 URL |
| `GDC_CACHE_DIR` | `.gdc_cache` | `_mapping` 스키마 캐시 위치 |
| `GDC_CACHE_TTL_SECONDS` | `86400` | 스키마 캐시 수명(초) |
| `GDC_REQUEST_DELAY_SECONDS` | `0.1` | 요청 간 지연 |

## 핵심 원칙

- **추측하지 말고 검증하라.** 필터/필드를 만들기 전에 `gdc fields` / `gdc values` 로 실제
  필드명·값 철자를 확인한다(스키마는 live).
- **먼저 미리보기.** 대량 수집은 항상 `--dry-run`으로 코호트 크기·용량을 확인한 뒤 실제 다운로드.
- **암 ML이면 정상조직을 빼라.** `--tumor-only` 또는 `--sample-type "Primary Tumor"`.
- **나이는 years로.** 나이를 피처로 쓰면 `--age-unit years`(raw days → 연 단위 변환).
- **wide 데이터는 parquet.** 발현/메틸레이션처럼 컬럼이 수만~수십만이면 `--out x.parquet`.
- **재현성은 자동.** `build-dataset`은 실행마다 `<out>_provenance.json`(사용 file_id+md5,
  실행 명령, 필터, case 수, API base, 타임스탬프)을 남긴다.

## ⚠️ 데이터 취급 주의

이 도구가 다운로드/생성하는 파일(`out/`, `*_work/`, `*.parquet`, `_provenance.json` 등)에는
환자 유래 데이터가 포함될 수 있다. `.gitignore`가 이들을 기본 제외하지만, 통제접근(controlled)
데이터와 `GDC_TOKEN`은 GDC 데이터 사용 정책에 따라 취급하고 절대 저장소에 커밋하지 말 것.

## 라이선스

[MIT](LICENSE)
