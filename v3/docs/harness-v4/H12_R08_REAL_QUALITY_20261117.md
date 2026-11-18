# H12 R08 真实模型首样本验收（2026-11-17）

结论：FAIL，开发集 7/12 PASS；编码 MVP 未放行。12道开发题均已调用真实模型，
未挑选重采样。独立新持出集12题记 NOT_RUN_NOT_ADMITTED，遵守开发≥10/12后才评分、
持出≥9/12的冻结门槛。原始 UNKNOWN 与核账后的失败视图分别保留。

评分源码 `04ad15c6a5ba493a7f8fb110388131229af9865d`，指纹 `9d9bcc37e4cd68ebc49c760e476ef3a213b35af1c539b7d54327794696881fd7`。
候选清单 SHA256 `9df592bf7993865e1a7bf37148cb0a64397e36f5371f29324048f9eb7ec22b64`；实验 `hv4-h12-r08-20261117-first-sample-v1`。
模型 deepseek-flash；每次物理请求均核对 thinking=enabled、reasoning_effort=max，单次物理尝试。

工程按用户要求只做失败定向复测：847项旧完整运行通过记录保留，3项原失败、5项新增和1项
变更检查共9项在冻结源码通过；有1个身份与旧通过项重叠，当前共855个收集身份。没有声称
当前源码完成855项全量执行。准备门两组各12/12，复用未变任务的实际回执，仅重跑原失败。

范围限制：开发01/02/12沿用明确冻结的旧Click源码快照与公开测试；该快照不含上游完整
tests目录，故其回归不能称上游Click全套。readiness=12/12指冻结公开计划可执行。原
PASS保留按冻结范围的含义；下一候选若声明全上游回归，应补齐对应版本资产并重新冻结。

| 开发题 | 原评分→核账视图 | 模型/工具调用 | 保守费用US$ | 原因 |
|---|---|---|---|---|
| r08-dev-01-click-help-name | FAIL→FAIL | 7/22 | 0.040535 | 限定符号未在文件中定义，工具缺少具体续查反馈；固定纠错额度耗尽，未形成补丁。 |
| r08-dev-02-click-short-help | PASS→PASS | 10/22 | 0.075806 | 独立 Judge 和本题冻结公开回归通过。 |
| r08-dev-03-requests-quoted-content-type | FAIL→FAIL | 18/33 | 0.175179 | 已知未执行命令的纠错回执被 restore_known 当成执行日志；ExecutionReceipt(None) 触发 ValidationError。 |
| r08-dev-04-pflag-unknown-flag | PASS→PASS | 12/32 | 0.134308 | 独立 Judge 和本题冻结公开回归通过。 |
| r08-dev-05-requests-http-url-helper | UNKNOWN→FAIL | 9/17 | 0.085813 | 固定 2 MiB SSE 上限在 finish/usage 前截断响应。小时账单已核账，但模型回复仍不可用，不重发。 |
| r08-dev-06-express-healthz | PASS→PASS | 5/12 | 0.028341 | 独立 Judge 和本题冻结公开回归通过。 |
| r08-dev-07-got-header-value | PASS→PASS | 11/19 | 0.074776 | 独立 Judge 和本题冻结公开回归通过。 |
| r08-dev-08-requests-encoding-refactor | FAIL→FAIL | 13/25 | 0.135485 | 与第03题同根因：被拒绝的诊断命令回执没有执行 receipt，随后 artifact_page 恢复时误校验 None。 |
| r08-dev-09-click-style-refactor | PASS→PASS | 9/21 | 0.083415 | 独立 Judge 和本题冻结公开回归通过。 |
| r08-dev-10-express-server-lifecycle | PASS→PASS | 7/18 | 0.044819 | 独立 Judge 和本题冻结公开回归通过。 |
| r08-dev-11-got-local-roundtrip | FAIL→FAIL | 7/23 | 0.068829 | 补丁 retry:0 不符合当前 Got 对象参数要求；公开探针吞掉子进程 stderr，模型搜索不存在的依赖目录并耗尽纠错额度。独立Judge实际报参数类型错误。 |
| r08-dev-12-click-multifile-placeholder | PASS→PASS | 9/27 | 0.066125 | 独立 Judge 和本题冻结公开回归通过。 |

## 每题问题、通过条件和证据

### 01 r08-dev-01-click-help-name

Repair click so a command that declares an argument named "help" keeps its value; the automatic help option must still render and parse --help.

冻结必需条件：Repair click so a command that declares an argument named "help" keeps its value; the automatic help option must still render and parse --help. Required: argument named help receives its value; --help still renders

结果：FAIL。限定符号未在文件中定义，工具缺少具体续查反馈；固定纠错额度耗尽，未形成补丁。
Run `47eef4cc-ef69-49ae-95f9-03fa0d0ed937`；最终树 `93dccdf5c2c42051e54016a728503612f3c3299994576c132bdf5b5d37f8a70a`。
独立Judge `FAIL`；新增回归失败 `0`（null 表示未证实，不能写0）。
实际冻结公开命令：`["python3", "-B", "-c", "import sys,runpy;sys.path[:0]=['/workspace/src','/workspace'];runpy.run_module('pytest',run_name='__main__')", "tests/", "--verbosity=2", "--maxfail=0"]`。
原评分：[evidence/harness-v4/HV4-H12-R08/live-first-sample-v1/candidate/dev/r08-dev-01-click-help-name/score.json](/home/developer/magr-harness-h12-repair/v3/evidence/harness-v4/HV4-H12-R08/live-first-sample-v1/candidate/dev/r08-dev-01-click-help-name/score.json)；SHA256 `21f3b8d1ce005bc4da80e66610885eba46e1407875b6ce1e1839335c485a2f43`。

### 02 r08-dev-02-click-short-help

Fix default short-help truncation so sentence boundaries followed by abbreviations (for example "vs.") do not drop later words or produce a misleading cut.

冻结必需条件：Fix default short-help truncation so sentence boundaries followed by abbreviations (for example "vs.") do not drop later words or produce a misleading cut. Required: abbreviation mid-sentence keeps later words

结果：PASS。独立 Judge 和本题冻结公开回归通过。
Run `752538eb-c97b-4f61-b420-55c9b7d9b5a1`；最终树 `5d124008bbca6f8d5eae0b719eb269029eed8bf8eb44052f2c65c66e138d8601`。
独立Judge `PASS`；新增回归失败 `0`（null 表示未证实，不能写0）。
实际冻结公开命令：`["python3", "-B", "-c", "import sys,runpy;sys.path[:0]=['/workspace/src','/workspace'];runpy.run_module('pytest',run_name='__main__')", "tests/", "--verbosity=2", "--maxfail=0"]`。
原评分：[evidence/harness-v4/HV4-H12-R08/live-first-sample-v1/candidate/dev/r08-dev-02-click-short-help/score.json](/home/developer/magr-harness-h12-repair/v3/evidence/harness-v4/HV4-H12-R08/live-first-sample-v1/candidate/dev/r08-dev-02-click-short-help/score.json)；SHA256 `0f4e4908941e35d9b04659713ebae33d544ffcc92aadcb6dca95f1c50446f7b7`。

### 03 r08-dev-03-requests-quoted-content-type

Fix requests.utils._parse_content_type_header so semicolons and equals signs inside double-quoted parameter values are preserved as data, while unquoted semicolons still separate parameters. Keep the media type, lower-cased parameter names, quote removal, and existing bare-token=True behavior unchanged.

冻结必需条件：Fix requests.utils._parse_content_type_header so semicolons and equals signs inside double-quoted parameter values are preserved as data, while unquoted semicolons still separate parameters. Keep the media type, lower-cased parameter names, quote removal, and existing bare-token=True behavior unchanged. Required: Quoted values containing semicolons and equals signs are preserved.; Multiple quoted parameters are parsed independently.; Existing charset, media type and bare-token behavior is preserved.

结果：FAIL。已知未执行命令的纠错回执被 restore_known 当成执行日志；ExecutionReceipt(None) 触发 ValidationError。
Run `e463ba58-fb42-4b73-a061-adefdeec1c4c`；最终树 `a653547ed677eb9b3fd631190cce2f77c486e03a8ef3838911b4b0ace7a14179`。
独立Judge `None`；新增回归失败 `None`（null 表示未证实，不能写0）。
实际冻结公开命令：`["python3", "-B", "-c", "import sys,runpy;sys.path[:0]=['/workspace/src','/workspace'];runpy.run_module('pytest',run_name='__main__')", "tests/", "--verbosity=2", "--maxfail=0"]`。
原评分：[evidence/harness-v4/HV4-H12-R08/live-first-sample-v1/candidate/dev/r08-dev-03-requests-quoted-content-type/score.json](/home/developer/magr-harness-h12-repair/v3/evidence/harness-v4/HV4-H12-R08/live-first-sample-v1/candidate/dev/r08-dev-03-requests-quoted-content-type/score.json)；SHA256 `5fb1654fe9c48ba8850a31412796e620ebcbf5577fdbb827321a3cf236f1a0eb`。

### 04 r08-dev-04-pflag-unknown-flag

With interspersed parsing disabled, stop parsing at the first unknown short flag and leave the remaining arguments unconsumed so the caller can distinguish them.

冻结必需条件：With interspersed parsing disabled, stop parsing at the first unknown short flag and leave the remaining arguments unconsumed so the caller can distinguish them. Required: parse stops at the unknown flag; Args() keeps the remainder

结果：PASS。独立 Judge 和本题冻结公开回归通过。
Run `c8865507-c55a-4389-8b00-9f5113e0da39`；最终树 `2a7f1349bb2321d45a4a5aaa60dfe8bf049ae7b1291e245a8d548b55af4bd03f`。
独立Judge `PASS`；新增回归失败 `0`（null 表示未证实，不能写0）。
实际冻结公开命令：`["go", "test", "-p", "1", "-v", "./...", "-json", "-count=1"]`。
原评分：[evidence/harness-v4/HV4-H12-R08/live-first-sample-v1/candidate/dev/r08-dev-04-pflag-unknown-flag/score.json](/home/developer/magr-harness-h12-repair/v3/evidence/harness-v4/HV4-H12-R08/live-first-sample-v1/candidate/dev/r08-dev-04-pflag-unknown-flag/score.json)；SHA256 `10071ca80819579963dd0e0421ec04a3a515205729fd5e42cae74a5c8f9765f3`。

### 05 r08-dev-05-requests-http-url-helper

Add requests.utils.is_http_url(value) -> bool. It returns True only for absolute http/https URLs with a non-empty host and no whitespace; it returns False for non-strings, relative references, other schemes and malformed authorities. It never raises.

冻结必需条件：Add requests.utils.is_http_url(value) -> bool. It returns True only for absolute http/https URLs with a non-empty host and no whitespace; it returns False for non-strings, relative references, other schemes and malformed authorities. It never raises. Required: http://example.com and https://example.com:8443/path are True; //example.com, example.com, ftp://example.com, mailto:a@b and " http://x" are False; non-string inputs and invalid ports return False without raising

结果：FAIL。固定 2 MiB SSE 上限在 finish/usage 前截断响应。小时账单已核账，但模型回复仍不可用，不重发。
Run `8ea6c739-2940-4780-9af9-ce4d889edac1`；最终树 `1472d18e551d504c2f2188c4bfbc46cec51fa43f34f565acfcb0bba93f9e7115`。
独立Judge `None`；新增回归失败 `None`（null 表示未证实，不能写0）。
实际冻结公开命令：`["python3", "-B", "-c", "import sys,runpy;sys.path[:0]=['/workspace/src','/workspace'];runpy.run_module('pytest',run_name='__main__')", "tests/", "--verbosity=2", "--maxfail=0"]`。
原评分：[evidence/harness-v4/HV4-H12-R08/live-first-sample-v1/candidate/dev/r08-dev-05-requests-http-url-helper/score.json](/home/developer/magr-harness-h12-repair/v3/evidence/harness-v4/HV4-H12-R08/live-first-sample-v1/candidate/dev/r08-dev-05-requests-http-url-helper/score.json)；SHA256 `fecb7ea9fc3a08b77d1813b0b6ddefc4d37eeeec70f16550048b4fd5de23f961`。

### 06 r08-dev-06-express-healthz

Add examples/healthz.js exporting an express app with GET /healthz returning 200 application/json {"status":"ok"} and GET /ready returning 503 while app.locals.ready is false and 204 when true. The file must listen on PORT (default 3000) only when run directly.

冻结必需条件：Add examples/healthz.js exporting an express app with GET /healthz returning 200 application/json {"status":"ok"} and GET /ready returning 503 while app.locals.ready is false and 204 when true. The file must listen on PORT (default 3000) only when run directly. Required: GET /healthz returns 200 and JSON status ok; GET /ready returns 503 before readiness and 204 after; module export is testable without binding a port

结果：PASS。独立 Judge 和本题冻结公开回归通过。
Run `c63ef6ac-67dd-4d20-b9b4-54de9dc2d6c1`；最终树 `08472703504df06e4a396055f11b500fd53fbb74ae63c6b5796c78e5d3559634`。
独立Judge `PASS`；新增回归失败 `0`（null 表示未证实，不能写0）。
实际冻结公开命令：`["node", "/node_modules/mocha/bin/mocha.js", "--require", "test/support/env", "test/", "test/acceptance/", "--reporter", "json", "--no-bail"]`。
原评分：[evidence/harness-v4/HV4-H12-R08/live-first-sample-v1/candidate/dev/r08-dev-06-express-healthz/score.json](/home/developer/magr-harness-h12-repair/v3/evidence/harness-v4/HV4-H12-R08/live-first-sample-v1/candidate/dev/r08-dev-06-express-healthz/score.json)；SHA256 `7ab24197eb77718aa75d50d9707cddbcb9644b435548bf922c4afa48dc2006ae`。

### 07 r08-dev-07-got-header-value

Add source/core/header-value.ts exporting isValidHeaderValue(value: string): boolean and re-export it from source/index.ts. It rejects CR, LF, NUL and other C0 controls except horizontal tab; it accepts printable ASCII and obs-text (0x80-0xFF); empty strings are valid.

冻结必需条件：Add source/core/header-value.ts exporting isValidHeaderValue(value: string): boolean and re-export it from source/index.ts. It rejects CR, LF, NUL and other C0 controls except horizontal tab; it accepts printable ASCII and obs-text (0x80-0xFF); empty strings are valid. Required: CR/LF/NUL/0x01 are rejected; tab, visible ASCII and 0x80-0xFF are accepted; exported symbol is usable from source/index.ts

结果：PASS。独立 Judge 和本题冻结公开回归通过。
Run `7b19df48-3f21-444c-a8fb-472b6f11060a`；最终树 `165064e76f7331790f1594d760ce64d1c1505b42b7a3fc9f71fa62d2fbbf4bfd`。
独立Judge `PASS`；新增回归失败 `0`（null 表示未证实，不能写0）。
实际冻结公开命令：`["npm", "test", "--", "--config=/opt/h12/ava-config.mjs", "--tap", "--no-fail-fast", "--no-cache"]`。
原评分：[evidence/harness-v4/HV4-H12-R08/live-first-sample-v1/candidate/dev/r08-dev-07-got-header-value/score.json](/home/developer/magr-harness-h12-repair/v3/evidence/harness-v4/HV4-H12-R08/live-first-sample-v1/candidate/dev/r08-dev-07-got-header-value/score.json)；SHA256 `1fa23769a146edd976a1f654c6275d248d1873da92d95d44c99d5747f3072c3b`。

### 08 r08-dev-08-requests-encoding-refactor

Extract the Content-Type encoding decision from requests.utils.get_encoding_from_headers into a private helper without changing any public function signature or returned encoding for existing and malformed headers.

冻结必需条件：Extract the Content-Type encoding decision from requests.utils.get_encoding_from_headers into a private helper without changing any public function signature or returned encoding for existing and malformed headers.；public signature of get_encoding_from_headers is unchanged; encoding decisions match the baseline corpus for 40 header variants

结果：FAIL。与第03题同根因：被拒绝的诊断命令回执没有执行 receipt，随后 artifact_page 恢复时误校验 None。
Run `10d88559-e9e0-4552-98ab-ef11e0a36eca`；最终树 `cb66256b8600a6f353fd77e45706529db8bf56c45f71288945c7a4ba9ea10de1`。
独立Judge `None`；新增回归失败 `None`（null 表示未证实，不能写0）。
实际冻结公开命令：`["python3", "-B", "-c", "import sys,runpy;sys.path[:0]=['/workspace/src','/workspace'];runpy.run_module('pytest',run_name='__main__')", "tests/", "--verbosity=2", "--maxfail=0"]`。
原评分：[evidence/harness-v4/HV4-H12-R08/live-first-sample-v1/candidate/dev/r08-dev-08-requests-encoding-refactor/score.json](/home/developer/magr-harness-h12-repair/v3/evidence/harness-v4/HV4-H12-R08/live-first-sample-v1/candidate/dev/r08-dev-08-requests-encoding-refactor/score.json)；SHA256 `b884f8578fd3d2b03f10c30abb05662bcc83f553719aa0e7f29d233aa21ada22`。

### 09 r08-dev-09-click-style-refactor

Extract the ANSI sequence assembly inside click.style into a private helper while keeping click.style output byte-identical for the frozen parameter matrix (fg/bg/bold/dim/underline/overline/italic/blink/reverse/strikethrough/reset combinations).

冻结必需条件：Extract the ANSI sequence assembly inside click.style into a private helper while keeping click.style output byte-identical for the frozen parameter matrix (fg/bg/bold/dim/underline/overline/italic/blink/reverse/strikethrough/reset combinations).；outputs are byte-identical to the baseline matrix; public click.style signature is unchanged

结果：PASS。独立 Judge 和本题冻结公开回归通过。
Run `0bebb57d-a997-4610-a791-39a734f7379a`；最终树 `580f2dc22ffa8b6d3ed51ee5082a72e7628fb6654b6ff65d973572443f45f344`。
独立Judge `PASS`；新增回归失败 `0`（null 表示未证实，不能写0）。
实际冻结公开命令：`["python3", "-B", "-c", "import sys,runpy;sys.path[:0]=['/workspace/src','/workspace'];runpy.run_module('pytest',run_name='__main__')", "tests/", "-m", "not stress", "--verbosity=2", "--maxfail=0"]`。
原评分：[evidence/harness-v4/HV4-H12-R08/live-first-sample-v1/candidate/dev/r08-dev-09-click-style-refactor/score.json](/home/developer/magr-harness-h12-repair/v3/evidence/harness-v4/HV4-H12-R08/live-first-sample-v1/candidate/dev/r08-dev-09-click-style-refactor/score.json)；SHA256 `e92c24965d19a03c6bb02e1d79585b1f705d03a672bb51724a72b1c9929771d8`。

### 10 r08-dev-10-express-server-lifecycle

Add examples/server.js that starts an express app on PORT (default 3000) with GET / returning "ok" and GET /healthz returning JSON {"status":"ok"}. It must log a single ready line with the bound port and exit cleanly on SIGTERM.

冻结必需条件：Add examples/server.js that starts an express app on PORT (default 3000) with GET / returning "ok" and GET /healthz returning JSON {"status":"ok"}. It must log a single ready line with the bound port and exit cleanly on SIGTERM. Required: process becomes ready within 10 seconds on the assigned port; both routes answer while the process runs; SIGTERM stops the process and releases the port within 5 seconds

结果：PASS。独立 Judge 和本题冻结公开回归通过。
Run `aa6575e3-24c8-4d0c-9481-7f7db39222a4`；最终树 `4049f7cc452dc9f2a5a89d023b863d89adbc1ca34132880fcd85eff64e0c5c93`。
独立Judge `PASS`；新增回归失败 `0`（null 表示未证实，不能写0）。
实际冻结公开命令：`["node", "/node_modules/mocha/bin/mocha.js", "--require", "test/support/env", "test/", "test/acceptance/", "--reporter", "json", "--no-bail"]`。
原评分：[evidence/harness-v4/HV4-H12-R08/live-first-sample-v1/candidate/dev/r08-dev-10-express-server-lifecycle/score.json](/home/developer/magr-harness-h12-repair/v3/evidence/harness-v4/HV4-H12-R08/live-first-sample-v1/candidate/dev/r08-dev-10-express-server-lifecycle/score.json)；SHA256 `5bb26c7447440e93b0ca99db98d786073a57e4a5a41705d1eb1e88de34bd55f8`。

### 11 r08-dev-11-got-local-roundtrip

Add scripts/selfcheck.mjs and an npm script selfcheck. The script starts a local HTTP server on an ephemeral port, performs a got GET, asserts the status and body, closes the server and exits 0; any failure exits non-zero and still closes the server.

冻结必需条件：Add scripts/selfcheck.mjs and an npm script selfcheck. The script starts a local HTTP server on an ephemeral port, performs a got GET, asserts the status and body, closes the server and exits 0; any failure exits non-zero and still closes the server. Required: selfcheck completes with exit 0 and prints the observed status; the local server is closed and no handle keeps the process alive; failure path exits non-zero without leaking the server

结果：FAIL。补丁 retry:0 不符合当前 Got 对象参数要求；公开探针吞掉子进程 stderr，模型搜索不存在的依赖目录并耗尽纠错额度。独立Judge实际报参数类型错误。
Run `de7ba5ec-8a7a-420a-b868-bf3cf36ab204`；最终树 `ee6c6d97f1de29cc1c6eefaa2400feabb45844ee98695967ef9399c4dc4a9dfc`。
独立Judge `FAIL`；新增回归失败 `0`（null 表示未证实，不能写0）。
实际冻结公开命令：`["npm", "test", "--", "--config=/opt/h12/ava-config.mjs", "--tap", "--no-fail-fast", "--no-cache"]`。
原评分：[evidence/harness-v4/HV4-H12-R08/live-first-sample-v1/candidate/dev/r08-dev-11-got-local-roundtrip/score.json](/home/developer/magr-harness-h12-repair/v3/evidence/harness-v4/HV4-H12-R08/live-first-sample-v1/candidate/dev/r08-dev-11-got-local-roundtrip/score.json)；SHA256 `1a0290b02fcb4ae838946965571c5ffc85ffd66c5c5ad65e3f626bd7d85810fd`。

### 12 r08-dev-12-click-multifile-placeholder

Add a configurable short-help placeholder used by Command.get_short_help_str across utils and formatting so a custom placeholder is applied consistently without changing the default "..." behavior.

冻结必需条件：Add a configurable short-help placeholder used by Command.get_short_help_str across utils and formatting so a custom placeholder is applied consistently without changing the default "..." behavior. Required: custom placeholder is honored; default behavior is unchanged

结果：PASS。独立 Judge 和本题冻结公开回归通过。
Run `5db8d718-bdd5-4c71-a5d9-40345042e0df`；最终树 `1befd676fd7352621ed16b77783040f2bab4af67c8407b246b6de7f96a26650f`。
独立Judge `PASS`；新增回归失败 `0`（null 表示未证实，不能写0）。
实际冻结公开命令：`["python3", "-B", "-c", "import sys,runpy;sys.path[:0]=['/workspace/src','/workspace'];runpy.run_module('pytest',run_name='__main__')", "tests/test_short_help_public.py", "--verbosity=2", "--maxfail=0"]`。
原评分：[evidence/harness-v4/HV4-H12-R08/live-first-sample-v1/candidate/dev/r08-dev-12-click-multifile-placeholder/score.json](/home/developer/magr-harness-h12-repair/v3/evidence/harness-v4/HV4-H12-R08/live-first-sample-v1/candidate/dev/r08-dev-12-click-multifile-placeholder/score.json)；SHA256 `210ef00fa8eb2820d6cbba4f354ea92d311b82ea027f2eefdd04979f404f2a21`。

## 独立持出汇总

本次开发门未通过，独立持出 12 道题均为 NOT_RUN。

## 核账、原记录与后续修复

用户提供19:00—20:00账单56次，对应55笔已知供应商回执和1笔缺失回执；差额为输入35521、
输出6691 Token（其中缓存命中19584、未命中15937）。按原冻结USD费率保守结算18686微美元。
原摘录未给币种，本次不做汇率推断，不把预算上界称为供应商实际USD账单。原请求未重发，
缺失模型响应未合成，原UNKNOWN score.json未修改，核账视图把它列为未完成FAIL。

首列经旧供应商CSV核对为账户 user_id，原规范化记录误标 bill_id，已追加元数据说明；
小时、模型、凭据及全部请求的归属核对不变，没有二次结算或覆盖原CAS证据。

本轮新增 117 次物理调用、保守 US$1.013431；
共享累计 930 次、保守 US$5.224921/US$12；unknown=0、reserved=0。

第03/05/08题的执行异常没有合格原PG终态；严格终态审计仍拒绝，不伪造结果或放松门。即使
按最宽的未通过视图也已低于10/12，持出评分没有启动。128份原历史评分的SHA256全部未变。

独立修复分支包括 SSE有限上限、未执行回执恢复、符号续查反馈和新公开探针诊断；分批累计24项相关检查通过，
源码/交付 `06e30e2f1913cada74533b7365b70c216eaa1bb4`。未把修复换入本轮评分，也未对历史模型题重采样。

修复与相关检查的可审查说明：

- [SSE捕获边界](/home/developer/magr-harness-h12-r08-wire-repair/v3/docs/harness-v4/H12_R08_WIRE_REPAIR_20261117.md)，源码bb3c9dc，6项相关检查。
- [非执行回执恢复](/home/developer/magr-harness-h12-r08-wire-repair/v3/docs/harness-v4/H12_R08_ARTIFACT_RESTORE_REPAIR_20261117.md)，源码0a718db，5项（含真实PG/Broker）。
- [符号续查反馈](/home/developer/magr-harness-h12-r08-wire-repair/v3/docs/harness-v4/H12_R08_SYMBOL_FEEDBACK_REPAIR_20261117.md)，源码b95a51f，7项。
- [公开探针子进程诊断](/home/developer/magr-harness-h12-r08-wire-repair/v3/docs/harness-v4/H12_R08_PUBLIC_DIAGNOSTICS_REPAIR_20261117.md)，源码06e30e2，6项；新资产须新冻结后使用。

已通过项未进行新的全量工程复测。后续候选的真实模型改进尚未验证，不能将这24项工程检查
解释成原5道失败题已经模型通过；本轮开发仍7/12，持出评分仍未获准入。

[机器记录](/home/developer/magr-harness-h12-repair/v3/docs/harness-v4/accept/HV4-H12-R08/accept.json)保存每题完整公开目标、通过条件、计划/评分/供应商证据哈希、原PG绑定与账本。
[核账证据](/home/developer/magr-harness-h12-repair/v3/evidence/harness-v4/HV4-H12-R08/billing-20261117-1900/applied.json)。仅本地交付，未推送、合并、部署或启动H13。
