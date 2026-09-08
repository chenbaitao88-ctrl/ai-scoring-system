# Phase11B-2a：EvidencePackage 导入 API 契约（冻结）

> 状态：已冻结（2026-08-06）
> 本阶段只定义 API 外观（路径、请求/响应模型、HTTP 映射），不创建 router、不修改 `main.py`。
> 权威基础：Phase11A-2a（EvidencePackage 契约）、Phase11A-fix-2（EvidenceSidecar 契约）、Phase11A-fix-4（RegistrationIndex 容器契约）。

## 1. 统一前缀与路径清单

统一前缀：`/api/evidence-packages`

| # | Method | Path | 说明 |
|---|--------|------|------|
| 1 | POST | `/api/evidence-packages/dry-run` | 单包 dry-run |
| 2 | POST | `/api/evidence-packages/dry-run-batch` | 批次 dry-run（1—1000 项） |
| 3 | POST | `/api/evidence-packages/register` | 正式登记（单包） |
| 4 | GET | `/api/evidence-packages/registrations` | 登记列表（可过滤） |
| 5 | GET | `/api/evidence-packages/registrations/{package_id}/revisions/{package_revision}` | 登记详情 |
| 6 | GET | `/api/evidence-packages/registrations/{package_id}/revisions/{package_revision}/validations/{validation_id}` | 指定验证结果（含历史） |
| 7 | GET | `/api/evidence-packages/registrations/{package_id}/revisions/{package_revision}/validations/{validation_id}/issues` | 问题明细 |

本阶段不创建 `APIRouter`，不执行实际 API 请求。

## 2. package_ref 路径安全规则

- 必须是受控输入根下的 POSIX 相对目录引用。
- 不接受服务器绝对路径（`/...`）、盘符（`C:`）。
- 拒绝 `..` 路径穿越、空段（`a//b`）、`.` 段。
- 拒绝反斜杠、空字节、空值。
- 仅允许字符：`A-Za-z0-9 _ - . /`。
- API 不接受真实本地任意路径。

## 3. 请求模型

### 3.1 EvidenceDryRunRequest

```json
{ "package_ref": "pkg-demo-001" }
```

| 字段 | 类型 | 规则 |
|------|------|------|
| package_ref | string | 安全相对目录引用（见第 2 节） |

### 3.2 EvidenceBatchDryRunRequest

```json
{
  "package_refs": ["pkg-demo-001", "pkg-demo-002"],
  "fail_fast": false
}
```

| 字段 | 类型 | 规则 |
|------|------|------|
| package_refs | string[] | 1—1000 项；不允许重复引用；保留调用方顺序；每项遵守单包路径安全规则 |
| fail_fast | boolean | 默认 `false` |

#### fail_fast 冻结语义（2026-08-06 总控冻结，Phase 11B-2c）

- `fail_fast=false`：按 `package_refs` 原顺序处理全部项目。
- `fail_fast=true`：严格串行处理；遇到首个分类为 `failed` 或 `internal_error` 的项目后立即停止；触发停止的项目必须保留在 `items` 中；`passed_with_warnings` 不触发停止。
- 未执行项目：不进入 `items`；不生成虚假 `result`；不生成新的错误码；不得标记成 `failed`、`skipped` 或 `internal_error`。
- 响应计数：`total` 表示本次实际执行的项目数（不是请求 `package_refs` 总数）；`items` 必须是原请求顺序的连续前缀；`len(items) == total`；`total == passed + passed_with_warnings + failed + internal_error`。
- HTTP：批次正常完成或 fail_fast 提前结束均返回 HTTP 200；请求模型不合法仍返回 HTTP 422。
- 单项异常（无法形成 ValidationResult 的内部错误）隔离为该项 `error`，计入 `internal_error`；响应不得泄露异常正文、绝对路径、traceback 或 `actual_summary`。

### 3.3 EvidenceRegisterRequest

```json
{
  "package_ref": "pkg-demo-001",
  "expected_index_revision": null
}
```

| 字段 | 类型 | 规则 |
|------|------|------|
| package_ref | string | 安全相对目录引用 |
| expected_index_revision | int? | 允许 `None`、`0` 或正整数；禁止负数；语义见 fix-4 |

不接受任何评分、Provider、Prompt 或数据库参数。

## 4. 安全响应模型

不得直接把内部 Pydantic 对象完整透传给 API。不得返回原始正文、绝对路径、学生个人信息（姓名/手机号/网盘链接）、traceback 或内部异常字符串。

### 4.1 EvidenceIssueResponse

```json
{
  "issue_id": "issue-synthetic-warning-0001",
  "code": "SYNTHETIC_WARNING",
  "severity": "warning",
  "message_key": "SYNTHETIC_WARNING"
}
```

仅允许：`issue_id`、`code`、`severity`、`message_key`。

### 4.2 EvidenceValidationResponse

```json
{
  "validation_id": "validation-demo-0001",
  "package_id": "pkg-demo-001",
  "package_revision": 1,
  "mode": "registration",
  "status": "passed",
  "evidence_level": "sufficient",
  "registration_allowed": true,
  "model_input_allowed": true,
  "issue_counts": {"info": 0, "warning": 1, "error": 0, "fatal": 0},
  "issues": [{"issue_id": "issue-synthetic-warning-0001", "code": "SYNTHETIC_WARNING", "severity": "warning", "message_key": "SYNTHETIC_WARNING"}],
  "started_at": "2026-08-06T07:00:00Z",
  "completed_at": "2026-08-06T07:00:01Z"
}
```

仅返回必要字段。不得暴露 `actual_summary`、输入文件正文、服务器路径。

`issues` 为安全 Issue 模型（`EvidenceIssueResponse`，含 `message_key`），不是内部 `ValidationIssueRef`；
router 负责把 `ValidationResult.issues` 与实际 `ValidationIssue` 合并映射为安全 Issue。

`evidence_level` 语义（总控决策）：
- dry-run 响应应从已验证的 EvidencePackage 映射实际等级。
- 登记/查询响应：现有 Sidecar `ValidationResult` 未持久化 `evidence_level`，返回 `null`。
- Phase 11B 不因此修改 Sidecar 契约或数据库；这是零 Schema MVP 的已知限制，不得由 router 猜测等级。

### 4.3 EvidenceBatchItemResponse

```json
{ "package_ref": "pkg-demo-001", "result": null, "error": null }
```

`result` 与 `error` 必须且只能出现一个。`package_ref` 只能回显调用方提交的安全相对引用。

### 4.4 EvidenceBatchDryRunResponse

```json
{
  "total": 2,
  "passed": 1,
  "passed_with_warnings": 0,
  "failed": 0,
  "internal_error": 1,
  "items": [
    { "package_ref": "pkg-demo-001", "result": {"validation_id": "validation-demo-0001", "package_id": "pkg-demo-001", "package_revision": 1, "mode": "registration", "status": "passed", "registration_allowed": true, "model_input_allowed": true, "issue_counts": {"info": 0, "warning": 0, "error": 0, "fatal": 0}, "issues": [], "started_at": "2026-08-06T07:00:00Z", "completed_at": "2026-08-06T07:00:01Z"}, "error": null },
    { "package_ref": "pkg-demo-002", "result": null, "error": {"code": "VALIDATION_INTERNAL_ERROR", "message_key": "VALIDATION_INTERNAL_ERROR", "retryable": true} }
  ]
}
```

计数不变量：`total == passed + passed_with_warnings + failed + internal_error`，且 `items` 数量等于 `total`。

### 4.5 EvidenceRegistrationResponse

```json
{
  "idempotent_hit": false,
  "rejected": false,
  "record_id": "record-abc123",
  "entry_id": "entry-def456",
  "package_id": "pkg-demo-001",
  "package_revision": 1,
  "validation": {"validation_id": "validation-demo-0001", "package_id": "pkg-demo-001", "package_revision": 1, "mode": "registration", "status": "passed", "registration_allowed": true, "model_input_allowed": true, "issue_counts": {"info": 0, "warning": 0, "error": 0, "fatal": 0}, "issues": [], "started_at": "2026-08-06T07:00:00Z", "completed_at": "2026-08-06T07:00:01Z"},
  "index_revision": 1
}
```

不变量（冻结，Phase 11B-2a-fix-1）：

- `rejected=true`：`validation` 必须存在（结构化验证依据）；`record_id`/`entry_id`/`index_revision` 必须为 `null`；`idempotent_hit` 必须为 `false`。
- `rejected=false`：`validation`/`record_id`/`entry_id` 必须存在；`index_revision` 必须存在且 `>= 1`。
- `idempotent_hit=true` 要求 `rejected=false`；不允许"成功登记响应但所有 ID 均为空"。

未登记或拒绝时，不伪造 `record_id`、`entry_id`、`index_revision`。

#### 拒绝登记 HTTP 响应（冻结）

- 正式登记被验证门拒绝时，HTTP 状态为 `422`。
- 响应体仍使用 `EvidenceRegistrationResponse`，其中：`rejected=true`、携带安全的 `validation`、不携带 record/entry/index revision。
- 请求模型错误或无法形成 ValidationResult 的 422 使用 `EvidenceApiError`。
- 不得用 traceback 或原始异常解释拒绝原因。

### 4.6 EvidenceRegistrationListResponse

```json
{
  "total": 1,
  "items": [
    {"package_id": "pkg-demo-001", "package_revision": 1, "batch_id": "batch-demo-001", "submission_id": "sub-demo-001", "registration_status": "registered", "latest_validation_status": "passed", "model_input_allowed": true, "manifest_sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}
  ]
}
```

列表元素只包含 Index 中用于展示的安全元数据。

### 4.7 EvidenceRegistrationDetailResponse

```json
{
  "package_id": "pkg-demo-001",
  "package_revision": 1,
  "record_id": "record-abc123",
  "entry_id": "entry-def456",
  "registration_status": "registered",
  "record_revision": 1,
  "latest_validation": {"validation_id": "validation-demo-0001", "package_id": "pkg-demo-001", "package_revision": 1, "mode": "registration", "status": "passed", "registration_allowed": true, "model_input_allowed": true, "issue_counts": {"info": 0, "warning": 0, "error": 0, "fatal": 0}, "issues": [], "started_at": "2026-08-06T07:00:00Z", "completed_at": "2026-08-06T07:00:01Z"},
  "issues": []
}
```

必须使用安全响应子模型，不直接返回内部对象。

### 4.8 EvidenceApiError

```json
{ "code": "SIDECAR_INDEX_CORRUPTED", "message_key": "SIDECAR_INDEX_CORRUPTED", "retryable": false }
```

不得包含：`exception`、`traceback`、`absolute_path`、`raw_response`。

## 5. None -> HTTP 404 映射

- `get_registration(package_id, revision)` 未登记时返回 `None`。
- `None` 是正式、稳定的内部 not-found 信号，不是成功空对象。
- API 层必须将 `None` 转换为 HTTP 404。
- 不得把未登记误报为 Index 损坏。

## 6. HTTP 映射（冻结）

| 状态码 | 场景 |
|--------|------|
| 200 | dry-run 完成（即使验证结论为 failed） |
| 200 | 批次 dry-run 完成 |
| 200 | 查询成功 |
| 200 | 幂等登记命中 |
| 201 | 首次正式登记成功 |
| 201 | 创建新 ValidationResult 并更新登记成功 |
| 404 | get_registration 返回 None |
| 404 | 指定 ValidationResult 不存在 |
| 409 | revision conflict |
| 409 | idempotency conflict |
| 409 | content conflict |
| 409 | lock conflict |
| 422 | 请求模型不合法 |
| 422 | package_ref 越界或不安全 |
| 422 | 包未 ready |
| 422 | 隐私门失败 |
| 422 | registration not allowed |
| 422 | 不支持的 EvidencePackage 版本 |
| 500 | Sidecar 损坏 |
| 500 | Index/Record 哈希不一致 |
| 500 | Audit/Index 写入失败 |
| 500 | validator internal error |
| 500 | 未知内部错误 |

要求：dry-run 的业务验证失败应返回结构化 ValidationResponse，不能仅靠 HTTP 422 表达；HTTP 422 只用于请求或无法进入验证流程的输入问题。

## 7. 稳定错误码表（复用 fix-2 9.2）

| code | 语义 |
|------|------|
| SIDECAR_UNSUPPORTED_VERSION | schema 版本不支持 |
| SIDECAR_RECORD_CONFLICT | 相同身份不同哈希或重复创建冲突 |
| SIDECAR_REVISION_CONFLICT | expected revision 不一致 |
| SIDECAR_RECORD_CORRUPTED | record 损坏 |
| SIDECAR_VALIDATION_NOT_FOUND | 引用的 validation 不存在 |
| SIDECAR_INDEX_CORRUPTED | Index 损坏或与记录不一致 |
| SIDECAR_INDEX_WRITE_FAILED | Index 原子替换失败 |
| SIDECAR_LOCK_CONFLICT | 锁冲突 |
| SIDECAR_HASH_MISMATCH | 哈希不符 |
| SIDECAR_RELATIVE_REF_INVALID | 相对引用非法 |
| REGISTRATION_NOT_ALLOWED | 登记不允许 |
| REGISTRATION_IDEMPOTENCY_CONFLICT | 幂等冲突 |
| VALIDATION_INTERNAL_ERROR | 验证器内部错误 |

### REQUEST_VALIDATION_ERROR（冻结，Phase 11B-2d）

- `code = REQUEST_VALIDATION_ERROR`、`message_key = REQUEST_VALIDATION_ERROR`、`retryable = false`、HTTP 422。
- 作用范围：`/api/evidence-packages/*` 下 EvidencePackage 请求模型（Pydantic 请求校验）失败时，返回顶层 `EvidenceApiError`（不是 Pydantic detail 结构）。
- 不返回 Pydantic `detail`、非法原值、绝对路径、traceback 或异常正文。
- 覆盖场景：空请求体、额外字段、非法 package_ref、错误 expected_index_revision、重复 package_refs、超过 1000 项等请求模型校验失败。
- 不影响其他路由：非 EvidencePackage 路径的请求校验错误保持 FastAPI 默认响应结构。

## 8. 模型纪律

- Pydantic 使用项目现有版本与写法。
- 所有模型 `extra="forbid"`。
- 所有时间字段带时区（UTC aware）。
- 数字必须有限，拒绝 NaN/Infinity。
- 枚举优先复用现有 Evidence 模型枚举。
- 不新增依赖；不引入数据库 ORM 模型；不新增数据库字段。
