# GDC CLI 에이전트 플레이북

이 저장소는 **GDC(Genomic Data Commons) 데이터를 CLI로 검색·다운로드·조립**하는 도구다.
너(LLM 에이전트)의 역할: 사용자의 자연어 요청을 아래 CLI 명령으로 변환해 실행하고,
ML 학습 가능한 테이블을 만들어 주는 것. 모든 실행은 결정론적 CLI가 담당한다 — 너는
필드/값/필터 같은 **의미 슬롯만** 채운다. 아래는 CLI 전용 안내다(별도의 `gdc-chat`/
`gdc-wiki` LLM 레이어는 이 문서 범위 밖이며 사용하지 않는다).

## 0. 핵심 원칙 (먼저 읽기)

- **추측하지 말고 검증하라.** 필터/필드를 만들기 전에 `gdc fields`, `gdc values`,
  `gdc <명령> --help`로 실제 필드명과 값 철자를 확인하라. 스키마는 live이고 CLI는 자기
  기술적(self-describing)이다.
- **먼저 미리보기.** 대량 수집(`build-dataset`)은 항상 `--dry-run`으로 코호트 크기와
  다운로드 용량을 먼저 확인한 뒤, 사용자 확인을 받고 실제 다운로드하라.
- **넓게 잡아라.** 질병/부위 요청("폐암")은 특정 프로젝트로 좁히지 말고 `--cohort-filter
  "cases.primary_site in bronchus and lung"`처럼 전체 GDC를 스캔하라. 스터디명(TCGA,
  CPTAC)을 명시했을 때만 `--project`로 좁힌다. (TCGA로 좁히면 CPTAC-3 등 비-TCGA 데이터를
  놓친다.)
- **암 ML이면 정상조직을 빼라.** `build-dataset`은 기본적으로 정상조직 샘플도 포함한다.
  종양 학습용이면 `--tumor-only`(정상 제외) 또는 `--sample-type "Primary Tumor"`를 붙여라.
  안 붙이면 건강한 조직 행이 조용히 학습 데이터에 섞인다.
- **나이는 years로.** 나이를 피처로 쓰면 `--age-unit years`를 붙여 days(예 18250)를 연 단위로
  변환하라. 안 붙이면 raw days가 그대로 들어간다.
- **wide 데이터는 parquet.** 발현/메틸레이션처럼 컬럼이 수만~수십만이면 `--out x.parquet`.
  메틸레이션은 `--methyl-top-variance N`으로 상위 분산 probe만 남겨 크기를 줄일 수 있다.
- **재현성은 자동.** `build-dataset`은 실행마다 `<out>_provenance.json`(사용 file_id+md5,
  실행 명령, 필터, 별칭, case 수, API base, 타임스탬프)을 남긴다 — 결과 보고 시 이 경로도 알려라.

## 1. 설치 · 실행 · 환경

- 실행: `gdc <명령>` (설치된 스크립트) 또는 `python -m gdc_cli.cli <명령>`. 둘은 동일하다.
- 파이썬 3.10+, 의존성: pandas, pyarrow, requests, rich, tenacity, typer, PyYAML.
- **gdc-client(선택)**: 공식 GDC Data Transfer Tool이 PATH에 있으면 다운로드 시 자동으로
  병렬·재개 다운로더로 사용된다(없으면 내장 다운로더로 폴백). 설치는
  https://gdc.cancer.gov/access-data/gdc-data-transfer-tool 참고.
- 환경변수(`.env` 또는 셸):

  | 변수 | 기본값 | 설명 |
  |---|---|---|
  | `GDC_TOKEN` | (없음) | 통제접근(controlled) 파일 다운로드용 토큰. open 데이터는 불필요 |
  | `GDC_API_BASE_URL` | `https://api.gdc.cancer.gov` | API 베이스 URL |
  | `GDC_CACHE_DIR` | `.gdc_cache` | `_mapping` 스키마 캐시 위치 |
  | `GDC_CACHE_TTL_SECONDS` | `86400` | 스키마 캐시 수명(초) |
  | `GDC_REQUEST_DELAY_SECONDS` | `0.1` | 요청 간 지연 |

## 2. 전체 구조 & 데이터 흐름

```
탐색            검색                 다운로드              조립                  통합
gdc fields  →  gdc search files  →  gdc download      →  gdc assemble-*   →   gdc build-ml
gdc values     (메타 TSV 생성)       (--mode data)         (파일 → 행렬)         (조인 → wide 테이블)
gdc aliases
                     └───────────────── 위 전체를 한 명령으로: gdc build-dataset ─────────────────┘
```

- **엔드포인트**: `cases`(환자 1행), `files`(파일 1행), `projects`, `annotations`.
- **메타(search 결과 TSV)** → **download**로 실제 파일 획득 → **assemble**로 파일들을
  `case × 유전자` 행렬로 변환 → **build-ml**로 임상+오믹스를 case_id 기준 조인.
- `build-dataset`는 이 5단계를 코호트 정의만 받아 자동 수행한다(LLM 진입점).

**조인 규칙(중요)**: 조인 키는 `case_id`. 발현(expr)이 있으면 발현 샘플이 행 기준이 되고,
나머지 오믹스(mut/cna/mirna/methyl/prot)는 case별 1행(종양 샘플 우선)으로 축약돼 조인되며
**종양 행에만 값이 채워지고 정상조직 행은 NaN**이다. 발현이 없으면 임상이 case 단위 행 기준.

**행 단위(`--level`)**: 기본은 발현이 있으면 `sample`(발현 샘플당 1행), 없으면 `case`(case당
1행). `--level case`를 주면 발현이 있어도 case당 1행(종양 우선)으로 축약한다.

**⚠️ 합집합 drop 함정**: 발현이 행 기준이므로, `--paired` 없이(=합집합) 발현은 없고 다른
오믹스만 있는 case는 **최종 테이블에 들어갈 수 없어 자동으로 drop된다**. 그래서 `--dry-run`
미리보기는 "Cohort cases"(코호트 case 수)와 "Final table rows"(실제 최종 행 수)를 **둘 다**
보여주고, drop되는 case 수를 경고한다. 사용자에게는 항상 **Final table rows**를 기준으로 보고하라.

## 3. 명령어 레퍼런스 (모든 옵션)

### 3.1 탐색

**`gdc fields <endpoint>`** — 필드 목록/검색
| 옵션 | 설명 |
|---|---|
| `<endpoint>` | cases \| files \| projects \| annotations (필수 위치인자) |
| `--search`, `-s TEXT` | 이름에 키워드 포함하는 필드만 |
| `--refresh` | 스키마 캐시 무시하고 다시 가져오기 |

**`gdc values <endpoint> <field>`** — 특정 필드의 값 분포(facet)와 건수. 필터 값 철자를
정할 때 필수. 예: `gdc values cases primary_site`.

**`gdc aliases`** — 사용 가능한 **필드 별칭**과 **필터 별칭(플래그)** 전체 목록.

### 3.2 검색 — `gdc search <endpoint>`

| 옵션 | 기본 | 설명 |
|---|---|---|
| `<endpoint>` | — | cases/files/projects/annotations |
| `--filter`, `-f "field op value"` | — | 필터 표현식(반복 가능). op: `=`,`!=`,`<`,`<=`,`>`,`>=`,`in`,`not in`,`is`,`exclude`,`excludeifany`. 값 여러 개는 콤마 |
| `--filter-json '{...}'` | — | GDC 원본 필터 JSON을 그대로 전달. **nested AND/OR**가 필요할 때(표현식 문법으로 안 되는 경우) 사용 |
| `--alias NAME` | — | 필터 별칭 이름(반복). 보통은 아래 플래그형으로 씀 |
| `--<flag>` | — | 필터 별칭 **플래그** (예: `--rnaseq --gene_counts --open`). `gdc aliases`로 확인 |
| `--fields "a,b,c"` | — | 출력 컬럼. **필드 별칭은 여기서만 확장됨** |
| `--multi-value join\|first\|last` | join | 다중값 필드(진단/치료 등) 처리. `join`=`;`로 이어붙임, `first`=첫 값(≈주진단), `last`=마지막 값(≈최근) |
| `--format tsv\|json` | tsv | 출력 포맷 |
| `--size N\|all` | 100 | 최대 레코드 수. 전체는 `all` |
| `--sort "field:asc\|desc"` | — | 정렬 |
| `--out PATH` | out/ 자동 | 출력 파일 |
| `--or` | (AND) | 여러 필터를 OR로 결합 |
| `--verbose` | — | 실제 전송 payload 출력 |

**필터 표현식 문법**:
- **값 내부 콤마는 따옴표로** 감싼다: `-f 'diagnoses.primary_diagnosis in "Adenocarcinoma, NOS","Squamous cell carcinoma, NOS"'`
  → 값 2개로 파싱(따옴표 없으면 콤마에서 잘못 쪼개짐).
- **결측 검사**: `-f "field is missing"`(값 없음) / `-f "field not missing"`(값 있음).
- **nested OR**: `--filter-json '{"op":"or","content":[{"op":"=","content":{"field":"a","value":["1"]}},{"op":"=","content":{"field":"b","value":["2"]}}]}'`

예:
```bash
# 폐암 케이스 (필드 별칭 사용)
gdc search cases -f "project.project_id = TCGA-LUAD" --fields "case,sex,age,stage" --size 20 --out out/luad.tsv
# 발현 파일 메타 (필터 플래그 = 오믹스)
gdc search files -f "cases.project.project_id in TCGA-LUAD" --gene_counts --star_counts --open \
  --fields "file,filename,filesize,file_case,file_sample_type,cases.samples.submitter_id" --size all --out out/expr_meta.tsv
# 병기(stage)가 기록된 케이스만 (결측 제외)
gdc search cases --luad -f "diagnoses.ajcc_pathologic_stage not missing" --fields "case,stage" --size all
```

### 3.3 다운로드 — `gdc download`

| 옵션 | 기본 | 설명 |
|---|---|---|
| `--from-search PATH` | — | search 결과 TSV(파일 메타)에서 file_id를 읽어 다운로드 |
| `--filter`, `-f` / `--alias` / `--<flag>` | — | (from-search 대신) 필터로 직접 파일 찾기 |
| `--mode manifest\|data` | manifest | `manifest`=매니페스트 파일만 생성, `data`=실제 파일 다운로드 |
| `--out-dir PATH` | out/ | 저장 폴더. 파일은 `<out-dir>/<file_id>/<파일명>`에 저장됨 |
| `--yes` | — | 다운로드 확인 프롬프트 자동 승인 |
| `--verbose` | — | payload 출력 |

- gdc-client가 PATH에 있으면 자동 사용(병렬·재개). 없으면 내장 다운로더(순차, HTTP Range
  재개 지원). 내장 다운로더는 메타에 `md5sum` 컬럼이 있으면 무결성 검증(불일치 시 삭제).
- 통제접근 파일은 `GDC_TOKEN` 없으면 자동 건너뜀.

### 3.4 조립 (파일 → 행렬)

모든 `assemble-*`는 `--meta`(search TSV) + `--files-dir`(download 폴더) → `--out`. 출력은
확장자가 `.parquet`/`.pq`면 parquet, 아니면 TSV.

| 명령 | 고유 옵션(기본) | 출력 행렬 |
|---|---|---|
| `assemble-expr` | `--value tpm_unstranded`, `--gene gene_name` | 샘플 × `expr_<gene>` (case_id, sample_barcode, sample_type 포함) |
| `assemble-mut` | `--mode count\|binary`, `--variant-class "A,B"` | case × `mut_<gene>` |
| `assemble-cnv` | `--value copy_number`, `--gene gene_name` | case × `cna_<gene>` |
| `assemble-mirna` | `--value reads_per_million_miRNA_mapped` | case × `mirna_<id>` |
| `assemble-methyl` | (없음; 헤더 없는 2컬럼 beta 파일) | case × `methyl_<probe>` (~485k 컬럼 → parquet) |
| `assemble-protein` | `--value protein_expression` | case × `prot_<antibody>` |

### 3.5 통합 — `gdc build-ml`

임상 + 오믹스 행렬들을 case_id로 조인해 wide 테이블 1개 생성.
| 옵션 | 설명 |
|---|---|
| `--clinical PATH` | 임상 TSV(case_id 포함). 발현 없으면 이게 행 기준 |
| `--expr / --mut / --cna / --mirna / --methyl / --protein PATH` | 각 오믹스 행렬(assemble 출력, TSV/parquet) |
| `--out PATH` | 출력(확장자로 포맷 결정) |

### 3.6 원샷 — `gdc build-dataset` (권장 진입점)

코호트 정의만 받아 검색→다운로드→조립→조인을 자동 수행.
| 옵션 | 기본 | 설명 |
|---|---|---|
| `--out PATH` | (필수) | 최종 테이블. `.parquet` 권장 |
| `--project "ID,ID"` | — | 프로젝트 id로 코호트 지정. **생략 시 전체 GDC** |
| `--cohort-filter "field op value"` | — | 질병/부위 등 case 기반 코호트 필터(반복). 예: `"cases.primary_site in bronchus and lung"` |
| `--clinical "a,b,c"` | — | 임상 필드/별칭(콤마). 발현 없으면 필수 |
| `--omics LIST` | expr,mut | `expr,mut,cna,mirna,methyl,prot` 중 콤마 조합 |
| `--paired` | (합집합) | 요청 오믹스를 **모두 가진 case만**(교집합). 코호트가 크게 줄어듦 |
| `--level case\|sample` | 자동 | 행 단위. 기본 = 발현 있으면 sample, 없으면 case. `case`면 발현도 case당 1행으로 축약 |
| `--tumor-only` | — | 정상조직 샘플 제외(**암 ML 권장**) |
| `--sample-type "T1,T2"` | — | 지정 sample_type만 유지(예: `"Primary Tumor"`). `--tumor-only`보다 정밀 |
| `--prefer-sample-type "T"` | — | case를 1행으로 축약할 때 우선할 sample_type |
| `--age-unit days\|years` | days | `years`면 age 계열 컬럼(days)을 연 단위로 변환(`_years` 접미사) |
| `--multi-value join\|first\|last` | join | 다중값 임상필드 처리(위 search와 동일) |
| `--skip-existing` | — | work-dir에 이미 만들어진 오믹스 행렬(`<omics>_matrix.parquet`) 재사용(재다운로드 방지) |
| `--methyl-top-variance N` | — | 메틸레이션에서 분산 상위 N개 probe만 유지(feature selection) |
| `--dry-run` | — | 다운로드 없이 **코호트 미리보기**(코호트 case 수, 최종 행 수, drop 수, 오믹스별 파일 수·용량) + provenance만 저장 |
| `--size N\|all` | all | 검색당 상한. 실데이터는 `all`(상한 걸면 오믹스 간 케이스 어긋남) |
| `--expr-value` | tpm_unstranded | 발현 값 컬럼 |
| `--mut-mode count\|binary` | count | 변이 인코딩 |
| `--cnv-workflow` | ASCAT3 | CNV 콜러(대소문자 구분) |
| `--mirna-value` | reads_per_million_miRNA_mapped | miRNA 값 컬럼 |
| `--work-dir PATH` | `<out>_work` | 중간 산출물 폴더(오믹스별 메타·파일·행렬) |
| `--json-errors` | — | 실패 시 트레이스백 대신 `{"ok":false,"error":...,"message":...}` JSON 출력(exit 1) |

**자동 산출물**: 성공 시 최종 테이블 외에 (1) 완료 요약(최종 shape·오믹스별 컬럼 수·임상 완성도)을
콘솔에 출력하고, (2) `<out>_provenance.json` 재현성 리포트를 저장한다. 임상은 코호트 전체 재검색이
아니라 **대상 case_id를 청크로 직접 질의**한다.

권장 흐름:
```bash
# 1) 미리보기 — 질병은 넓게(primary_site), 정상조직 제외, 나이는 연 단위, 다운로드 안 함
gdc build-dataset --cohort-filter "cases.primary_site in bronchus and lung" \
  --omics expr,mut,cna --paired --tumor-only --age-unit years \
  --clinical "sex,age,stage" --out out/lung.parquet --dry-run
# 2) 사용자 확인 후 실제 수집 (dry-run 빼면 다운로드)
gdc build-dataset --cohort-filter "cases.primary_site in bronchus and lung" \
  --omics expr,mut,cna --paired --tumor-only --age-unit years \
  --clinical "sex,age,stage" --out out/lung.parquet
# 3) 결과 QC
gdc describe out/lung.parquet
```

### 3.7 QC — `gdc describe <table>`

만들어진 테이블(parquet/TSV)의 경량 품질 점검. 전체 shape, 오믹스 그룹별 컬럼 수·평균 완성도,
임상/라벨 컬럼별 dtype·비결측률·고유값 수·분포를 출력한다.
| 옵션 | 기본 | 설명 |
|---|---|---|
| `<path>` | (필수) | 점검할 테이블 경로 |
| `--max-columns N` | 40 | 상세 표시할 비오믹스 컬럼 최대 수 |
| `--top N` | 5 | 범주형 컬럼에서 표시할 상위 값 개수 |

## 4. 별칭 시스템 (`gdc aliases`로 전체 확인)

- **필드 별칭**: `--fields`에서만 확장. 예 `case`→case_id, `file`→file_id, `filename`→file_name,
  `filesize`→file_size, `sex`→demographic.sex_at_birth, `age`→diagnoses.age_at_diagnosis,
  `stage`→diagnoses.ajcc_pathologic_stage, `file_case`→cases.case_id,
  `file_sample_type`→cases.samples.sample_type.
- **필터 별칭(플래그)**: `--<이름>` 형태로 필터에 추가. 예 오믹스: `--gene_counts`,
  `--star_counts`, `--somatic_mutation`, `--gene_cnv`, `--mirna_counts`, `--methylation_beta`,
  `--protein_expression`; 접근/전략: `--open`, `--controlled`, `--rnaseq`, `--wxs`.
- **조직형/코호트 별칭**(cases 엔드포인트, TCGA project 기반): `--luad`, `--lusc`, `--luad_lusc`,
  `--brca`, `--coad`, `--read`, `--prad`. primary_site만으로 구분 안 되는 세부 조직형을 짧게 지정.
  (예: `gdc search cases --luad --fields "case,stage"`.) 세부 조직형이 필요 없으면 넓은
  `--cohort-filter cases.primary_site ...`를 우선하라.
- **주의**: 필터 별칭은 **플래그**로만 쓴다. `--filter`에는 진짜 필드 경로를 써야 한다
  (예 `-f "cases.primary_site in ..."`), 별칭 이름을 `--filter`에 넣지 말 것.

## 5. omics 종류 요약

| omics 키 | data_type (검색 시, 대소문자 구분) | 조립 결과 접두사 | 비고 |
|---|---|---|---|
| expr | Gene Expression Quantification (workflow "STAR - Counts") | `expr_` | 샘플 단위 base |
| mut | Masked Somatic Mutation | `mut_` | case별 변이 count/binary |
| cna | Gene Level Copy Number (workflow ASCAT3 등) | `cna_` | case별 종양 CNV |
| mirna | miRNA Expression Quantification (data_format txt) | `mirna_` | RPM |
| methyl | Methylation Beta Value | `methyl_` | ~485k probe → parquet |
| prot | Protein Expression Quantification (RPPA) | `prot_` | antibody |

## 6. 함정 & 규칙 (반드시 지킬 것)

- **대소문자 구분**: `data_type`, `experimental_strategy`, `analysis.workflow_type` 값은
  Title Case 원문 그대로(소문자로 쓰면 0건). `data_category`, `data_format`, `sample_type`,
  `sex_at_birth`, `vital_status`는 구분 안 함.
- **폐기된 필드**: `demographic.gender` → `demographic.sex_at_birth`,
  `diagnoses.tumor_stage` → `diagnoses.ajcc_pathologic_stage`.
- **age는 days 단위** (50세 ≈ 18250일). 피처로 쓸 땐 `--age-unit years`로 변환하라.
- **정상조직 기본 포함**: `build-dataset`은 기본적으로 정상 샘플도 넣는다. 종양 학습이면
  `--tumor-only`(또는 `--sample-type "Primary Tumor"`)를 반드시 붙여라.
- **OS(전체생존)** = `demographic.vital_status` + `demographic.days_to_death` +
  `diagnoses.days_to_last_follow_up`에서 유도.
- 필터에 "homo sapiens" 같은 종 조건은 불필요(GDC는 사람).
- **배치 효과**: 여러 스터디를 합치면 스터디 간 편차가 있으니 ML 전 정규화는 사용자 몫.
- `--paired` + 넓은 코호트를 함께 쓰면 교집합이 작아질 수 있음 — dry-run으로 크기 확인.

## 7. 에이전트 체크리스트

1. 요청을 엔드포인트/필드/값/오믹스로 분해한다.
2. 확실치 않은 필드/값은 `gdc fields <ep> -s <kw>` / `gdc values <ep> <field>`로 검증한다.
3. 질병/부위 요청이면 `--cohort-filter cases.primary_site ...`(넓게), 스터디명이면 `--project`.
4. 암 ML이면 `--tumor-only`, 나이 피처면 `--age-unit years`를 붙인다. 행 단위가 애매하면 `--level` 확인.
5. `gdc build-dataset ... --dry-run`으로 **Final table rows**·drop 수·다운로드 용량을 사용자에게 제시한다.
6. 확인받으면 `--dry-run`을 빼고 실제 수집한다(출력은 `.parquet` 권장).
7. `gdc describe <out>`로 QC하고, 결과 파일 경로·shape(행 수, 오믹스별 컬럼 수)·`<out>_provenance.json`을 보고한다.
