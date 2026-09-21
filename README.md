# 实验室样品分装谱系 API

使用 **Python + FastAPI + SQLite** 实现的样品分装 / 谱系追踪服务。

登记带体积的**初始样品**后，可以**批量**提交一批容器间转移；系统按**源容器当时的混合比例**
传播其中**全部祖先来源**，所有体积运算使用标准库 [`decimal.Decimal`](https://docs.python.org/3/library/decimal.html)
完成，全程不经过 `float`，杜绝二进制浮点误差。

- 一批转移**严格依次发生**：后面的转移能看到同批中前面转移的结果。
- 整批是一个 SQLite 事务：**源液不足、未知容器、自转移**（或任何参数错误）都会导致
  **整批拒绝、状态完全不变**。
- 可用 `POST /transfers/preview` **预演**同一批转移：与真实提交完全相同的校验与分摊规则，
  返回每一步的扣取明细与受影响容器的最终状态，但**绝不写入数据库**。
- 可选的**乐观并发保护**：每个容器持久化一个整数 `version`（创建时为 0），
  `GET /containers/{id}` 返回当前版本；批量提交/预演可携带 `expected_versions`
  做版本校验，成功提交后每个被触碰容器的版本恰好加 1（同批多次出现也只加 1）。
- 可选的**幂等键** `idempotency_key`（仅 `/transfers/batch` 生效）：成功请求的
  请求体指纹与完整响应与转移**写入同一 SQLite 事务**；同键同请求体重放首次结果
  （不重复扣减体积、不重复递增 `version`），同键但请求体不同返回
  `409 idempotency_conflict`。预演接口不写幂等记录；不携带键时行为与旧版本完全一致。
- **只追加审计哈希链**：每个**成功**批次在同一事务追加一条审计记录，内容包含
  **批次摘要**（逐步转移及按混合比例实际扣取的祖先来源精确份额）、**受影响容器**
  与提交后的**结果版本**；每条记录的 SHA-256 哈希纳入上一条记录的哈希（首条引用
  全 0 的创世哈希），形成可整体校验的链。存储层触发器拒绝任何 UPDATE/DELETE；
  可按容器与序号范围查询记录（`GET /audit/records`）、取单条（`GET /audit/records/{seq}`）
  并校验整条链（`GET /audit/verify`）。**失败批次、幂等重放与预演均不产生记录。**
- 可查询任一容器的**当前体积**以及各祖先初始样品的**来源占比**。

---

## 一条命令启动

需要已安装 Docker 与 Docker Compose v2：

```bash
docker compose up --build
```

启动后服务监听：**http://localhost:8000**

- Swagger 交互文档：http://localhost:8000/docs
- OpenAPI 描述：http://localhost:8000/openapi.json
- 健康检查：http://localhost:8000/health

容器启动时（`docker-entrypoint.sh`）会：

1. 安装 Python 依赖（`requirements.txt`）；
2. 在 `DATABASE_PATH` 处创建 SQLite 数据库与表（已存在则保持不变，幂等）；
3. 启动 uvicorn。

SQLite 数据保存在 Docker 命名卷 `lineage-data`（容器内 `/data`）中，重启不丢失。

### 旧版本数据库的自动迁移

直接用新镜像启动挂载了**旧数据卷**的容器即可，无需手工迁移——启动时的
`init_db()` 全部是幂等 DDL：

- 旧库缺少 `containers.version` 列时自动 `ALTER TABLE ... ADD COLUMN` 补齐，
  已有容器的版本一律视为 `0`；
- 旧库缺少幂等表时自动创建 `idempotency_keys`
  （`idempotency_key` 主键、`request_sha256` 请求体指纹、`status_code`、
  `response_body` 完整响应、`created`），历史数据不受影响，旧客户端不带键
  请求的行为也完全不变；
- 旧库缺少审计表时自动创建 **`audit_records`**（哈希链）与 **`audit_containers`**
  （批次→受影响容器索引）并安装四个**只追加触发器**（禁止两表的 UPDATE/DELETE）。
  升级前发生的历史批次没有审计记录，链从升级后**第一个成功批次**开始计数
  （该条即 `seq=1`、引用全 0 创世哈希），无需补录。

升级步骤（保留数据）：

```bash
git pull                                   # 或重新构建镜像
docker compose up --build -d               # 复用 lineage-data 卷，启动即自动迁移
docker compose logs -f api                 # 看到 “database ready” 即迁移完成
```

如需确认迁移结果，可在容器内查询：

```bash
docker compose exec api python -c "
from app.db import connect
c = connect()
print([r[0] for r in c.execute(\"SELECT name FROM sqlite_master WHERE type='table'\")])
print([r[1] for r in c.execute('PRAGMA table_info(containers)')])
"
# 应包含 idempotency_keys 表，containers 列中应包含 version
```

停止与清理：

```bash
docker compose down          # 停止并删除容器（保留数据卷）
docker compose down -v       # 同时删除数据卷
```

---

## 配置项

全部配置通过环境变量注入（见 `docker-compose.yml` 的 `environment`）：

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `DATABASE_PATH` | `/data/lineage.db` | SQLite 数据库文件路径，目录不存在会自动创建 |
| `PORT` | `8000` | 容器内监听端口（compose 已映射 `8000:8000`；如改端口需同步修改端口映射） |
| `DECIMAL_SCALE` | `9` | 体积与各来源份额保留的小数位数；请求体积超出该精度会被拒绝（422） |
| `PROPORTION_SCALE` | `6` | 查询结果中来源占比（proportion）的小数位数 |
| `DECIMAL_PRECISION` | `50` | Decimal 中间运算（除法等）的有效数字精度，最小 28 |

---

## 数据模型

- **初始样品**：登记一个带初始体积的容器，它是自身唯一的祖先来源。
- **空容器**：预先登记的空容器，可作为转移目标。
- 每个容器的状态记录为若干条 `{祖先初始样品 ID -> 绝对体积份额}`，份额之和即当前体积。
  Decimal 在数据库中以**字符串**存储。
- 每个容器额外持久化一个整数 **`version`**：创建（样品/空容器）时为 `0`，每次成功的
  批量转移后被触碰的容器加 1（同批多次触碰只加 1），用于 `expected_versions` 乐观并发控制。
  服务启动时会自动为旧版本数据库补齐该列，已有容器的版本视为 `0`，无需手工迁移。
- 表 **`idempotency_keys`** 仅服务于 `/transfers/batch` 的可选幂等：只在批次成功提交时
  写入一行，记录幂等键、原始请求体的 SHA-256 指纹、首次响应的 HTTP 状态码与完整响应体；
  该行与容器扣减、`version` 递增在**同一个 SQLite 事务**内落库。预演接口不写此表，
  失败的批次（4xx，包括业务错误、`stale_container`、`idempotency_conflict`）也不写。
- 表 **`audit_records`** 是只追加的批次审计哈希链，**`audit_containers`** 是其按容器
  查询的索引表，两表均由触发器禁止 UPDATE/DELETE。审计记录的字段、哈希规则与
  校验方式见下文“批次审计哈希链”一节。

---

## API 一览

> 请求 / 响应中的体积字段接受 JSON 数字或字符串，**推荐使用字符串**（如 `"40.50"`），
> 以避免 JSON 数字经 float 解析；响应统一输出为字符串。

### 1. 登记初始样品

```bash
curl -X POST http://localhost:8000/samples \
  -H 'Content-Type: application/json' \
  -d '{"sample_id":"S1","volume":"100"}'
# 201 -> {"sample_id":"S1","volume":"100.000000000"}
```

### 2. 登记空容器

```bash
curl -X POST http://localhost:8000/containers \
  -H 'Content-Type: application/json' \
  -d '{"container_id":"V1"}'
# 201 -> {"container_id":"V1"}
```

重复登记返回 `409 container_exists`。

### 3. 批量转移（整批原子，按数组顺序依次执行）

```bash
curl -X POST http://localhost:8000/transfers/batch \
  -H 'Content-Type: application/json' \
  -d '{"transfers":[
    {"source":"S1","destination":"V1","volume":"40"},
    {"source":"S2","destination":"V1","volume":"60"}
  ]}'
# 200 -> {"applied":2,"message":"已依次提交 2 条转移",
#        "versions":{"S1":1,"S2":1,"V1":1}}
```

转移时从源容器按其**当时**各祖先来源的混合比例取出，累加到目标容器的同名来源上，
源容器对应份额同步扣减。

响应中的 `versions` 是本批**每个被触碰容器提交后的版本**：容器创建时 `version=0`，
每次成功的批量转移后被触碰的容器 `version` 恰好加 1，即使同一容器在本批中多次出现
（如先作为目标、后作为源的链式转移）也只加 1；未参与本批的容器版本不变。

#### 可选：乐观并发保护（expected_versions）

请求体可选携带 `expected_versions`（容器 ID → 客户端预期版本，版本取自上次
`GET /containers/{id}` 或上一批响应中的 `versions`）：

- **覆盖校验**：一旦提供，键集合必须**恰好**覆盖本批全部源和目标容器（去重后不多不少），
  否则返回 `422 invalid_expected_versions`，消息中列明缺少 / 多余的容器；
- **版本校验**：覆盖正确时，服务在**同一个写事务内**读取并比对每个被触碰容器的当前版本，
  任一不符即返回 `409 stale_container`，**整批回滚、绝不落库**（任何容器的体积与版本都不变）；
- 校验通过并成功提交后，返回各容器递增后的新版本（`versions`）；
- **不提供** `expected_versions` 时完全保持原有行为（无版本校验），新旧客户端可混用。

典型用法：先 `GET` 取得各容器版本 → 带版本提交 → 收到 `409 stale_container` 说明期间
被其他客户端改过，重新 `GET` / `preview` 后用新版本重试。

```bash
curl -X POST http://localhost:8000/transfers/batch \
  -H 'Content-Type: application/json' \
  -d '{"transfers":[{"source":"S1","destination":"V1","volume":"40"}],
       "expected_versions":{"S1":0,"V1":0}}'
# 200 -> {"applied":1,"message":"已依次提交 1 条转移","versions":{"S1":1,"V1":1}}
```

#### 可选：幂等键（idempotency_key）

请求体可选携带 `idempotency_key`（非空白字符串，最长 255；键两侧空白会被去掉后使用）。
键仅对 `/transfers/batch` 生效，建议由客户端为每个“意图”生成一个稳定的唯一值
（如 UUID），在网络超时、收到 5xx 等不确定结果时用**同一个键、同一个请求体**安全重试：

- **首次成功**：请求体指纹（未经解析的**原始请求体字节**的 SHA-256）、HTTP 状态码与
  **完整响应**与本批转移写入**同一个 SQLite 事务**——幂等记录与扣减要么同时可见，
  要么一起回滚；
- **同键同请求体重放**：直接返回首次响应（响应体逐字节一致），并带响应头
  `Idempotency-Replayed: true`；**不会再次扣减体积、不会再次递增 `version`**，
  也不会重新执行业务校验（例如首次之后 `expected_versions` 已过期，重放仍返回首次结果）；
- **同键但请求体不同**：返回 `409 idempotency_conflict`，整批拒绝且不落库
  （注意指纹以**原始字节**为准：即使语义相同，JSON 空白、键顺序、数字 `40` 与
  字符串 `"40"` 的差异也算不同请求体；请直接重发首次的原始报文）；
- **失败请求不留痕**：任何 4xx（参数错误、源液不足、未知容器、`stale_container` 等）
  都不会写入幂等记录，同一个键可以修正请求后继续重试；
- 预演接口 `/transfers/preview` **不查、不写**幂等记录（请求体里带了键也会被忽略），
  不会“消费”键；
- **不携带** `idempotency_key` 时与旧版本行为完全一致（每批正常执行一次）。

```bash
# 首次提交
curl -X POST http://localhost:8000/transfers/batch \
  -H 'Content-Type: application/json' \
  -d '{"transfers":[{"source":"S1","destination":"V1","volume":"40"}],
       "idempotency_key":"b7f2e1a9-4f1c-4d6a-9c23-7a8e0d6b3f11"}'
# 200 -> {"applied":1,"message":"已依次提交 1 条转移","versions":{"S1":1,"V1":1}}

# 同样的键 + 完全相同的请求体重试（如超时后）：重放，不重复扣减、不重复 +1 版本
curl -X POST http://localhost:8000/transfers/batch \
  -H 'Content-Type: application/json' \
  -d '{"transfers":[{"source":"S1","destination":"V1","volume":"40"}],
       "idempotency_key":"b7f2e1a9-4f1c-4d6a-9c23-7a8e0d6b3f11"}' \
  -D -
# HTTP/1.1 200
# idempotency-replayed: true
# {"applied":1,"message":"已依次提交 1 条转移","versions":{"S1":1,"V1":1}}

# 同一个键但请求体不同（体积改成 41）：
curl -X POST http://localhost:8000/transfers/batch \
  -H 'Content-Type: application/json' \
  -d '{"transfers":[{"source":"S1","destination":"V1","volume":"41"}],
       "idempotency_key":"b7f2e1a9-4f1c-4d6a-9c23-7a8e0d6b3f11"}'
# 409 -> {"error":{"code":"idempotency_conflict",
#         "message":"幂等键 'b7f2e1a9-...' 已用于另一个请求体，整批拒绝且不落库"}}
```

幂等键与 `expected_versions` 可以同时使用：`expected_versions` 只是请求体的一部分，
首次提交时正常校验；重放时直接返回首次响应、不再校验版本。

**整批拒绝**的情形（HTTP 状态码与错误码）：

| 情形 | 状态码 | `error.code` |
| --- | --- | --- |
| 源 / 目标容器不存在 | 404 | `unknown_container` |
| `source` 与 `destination` 相同 | 422 | `self_transfer` |
| 转移体积大于源容器当前体积 | 422 | `insufficient_volume` |
| 体积为 0、为负、非有限数，或小数位超过 `DECIMAL_SCALE` | 422 | `invalid_volume` |
| `expected_versions` 未恰好覆盖本批全部源/目标（含空对象） | 422 | `invalid_expected_versions` |
| `expected_versions` 中某容器版本与当前版本不符 | 409 | `stale_container` |
| 同一 `idempotency_key` 已用于另一个请求体（指纹不同） | 409 | `idempotency_conflict` |

错误响应形如：

```json
{"error":{"code":"insufficient_volume","message":"第 2 条转移源液不足：'V1' 当前 75.000000000，请求 9999.000000000，整批拒绝"}}
```

### 4. 预演批量转移（不落库）

`POST /transfers/preview` 使用与批量提交**完全相同的请求结构**（同样接受可选的
`expected_versions`；`idempotency_key` 字段虽可出现但会被**忽略**——预演不查重、
不重放、不写幂等记录），在数据库当前状态上按数组顺序整批模拟（链式转移使用本批前序
模拟结果），返回：

- `steps`：每一步的**条目序号**与从各祖先来源**实际扣取的精确份额**（`taken` 之和
  精确等于该步请求体积，同样采用最大余额法）；
- `containers`：全部受影响容器（本批所有源 / 目标容器）在预演结束时的**最终体积、
  来源构成与 `version`**，结构与 `GET /containers/{id}` 一致。其中 `version` 是
  **若按本批真实提交后将变成的版本**（当前版本 + 1，每个被触碰容器只加 1 次）。

预演与真实提交**共用同一套校验、版本校验与分摊代码**：提供 `expected_versions` 时，
覆盖错误同样返回 `422 invalid_expected_versions`、版本不符同样返回
`409 stale_container`，HTTP 状态码、`error.code` 以及消息中的条目序号（“第 N 条转移”）
均与 `/transfers/batch` 完全一致。预演**只读不写、不递增版本**，成功或失败都不会改变
数据库，可以安全地重复调用；因此可用于“先核对预期版本、确认提交后版本，再正式提交”。

```bash
curl -X POST http://localhost:8000/transfers/preview \
  -H 'Content-Type: application/json' \
  -d '{"transfers":[
    {"source":"S1","destination":"V1","volume":"40"},
    {"source":"S2","destination":"V1","volume":"60"},
    {"source":"V1","destination":"V2","volume":"25"}
  ]}'
```

```json
{
  "steps": [
    {"index": 1, "source": "S1", "destination": "V1", "volume": "40.000000000",
     "taken": [{"source_id": "S1", "amount": "40.000000000"}]},
    {"index": 2, "source": "S2", "destination": "V1", "volume": "60.000000000",
     "taken": [{"source_id": "S2", "amount": "60.000000000"}]},
    {"index": 3, "source": "V1", "destination": "V2", "volume": "25.000000000",
     "taken": [
       {"source_id": "S1", "amount": "10.000000000"},
       {"source_id": "S2", "amount": "15.000000000"}
     ]}
  ],
  "containers": [
    {"container_id": "S1", "version": 1, "volume": "60.000000000",
     "sources": [{"source_id": "S1", "amount": "60.000000000", "proportion": "1.000000"}],
     "proportion_scale": 6},
    {"container_id": "S2", "version": 1, "volume": "40.000000000",
     "sources": [{"source_id": "S2", "amount": "40.000000000", "proportion": "1.000000"}],
     "proportion_scale": 6},
    {"container_id": "V1", "version": 1, "volume": "75.000000000",
     "sources": [
       {"source_id": "S1", "amount": "30.000000000", "proportion": "0.400000"},
       {"source_id": "S2", "amount": "45.000000000", "proportion": "0.600000"}
     ],
     "proportion_scale": 6},
    {"container_id": "V2", "version": 1, "volume": "25.000000000",
     "sources": [
       {"source_id": "S1", "amount": "10.000000000", "proportion": "0.400000"},
       {"source_id": "S2", "amount": "15.000000000", "proportion": "0.600000"}
     ],
     "proportion_scale": 6}
  ]
}
```

上例中第 3 步从混合容器 `V1` 取 25：预演按前两步形成的 40/60 混合比分摊为
S1 取 10、S2 取 15；调用后数据库中 S1/S2 仍为 100、V1/V2 仍为空，版本也都保持 0；
各容器返回的 `"version": 1` 是**预测**真实提交后的版本（当前 0 + 1）。

### 5. 查询容器当前体积与来源占比

```bash
curl http://localhost:8000/containers/V1
```

```json
{
  "container_id": "V1",
  "version": 2,
  "volume": "75.000000000",
  "sources": [
    {"source_id": "S1", "amount": "30.000000000", "proportion": "0.400000"},
    {"source_id": "S2", "amount": "45.000000000", "proportion": "0.600000"}
  ],
  "proportion_scale": 6
}
```

- `version`：容器的乐观并发版本，创建时为 `0`，每次被成功批次触碰后加 1
  （同批多次出现只加 1）；可作为 `/transfers/batch`、`/transfers/preview` 的
  `expected_versions` 入参。
- `amount`：该祖先来源在容器中的**绝对体积**（满足体积守恒，可跨容器累加核对）。
- `proportion`：占容器当前体积的比例，按 `PROPORTION_SCALE` 四舍五入，仅用于展示。
- 空容器返回 `"version":0`、`"volume":"0"`、`"sources":[]`；未知容器返回 `404 unknown_container`。

### 6. 批次审计哈希链（只追加、可校验）

每个**成功**提交的批量转移，都会与容器扣减、版本递增在**同一个 SQLite 事务**内
追加一条审计记录；该事务回滚（失败批次）时记录随之消失，因此：

- 只有真实成功的批次产生记录；**失败（任何 4xx）不产生记录**；
- `/transfers/preview` 预演**只读**，成功或失败都**不产生记录**（携带幂等键也忽略）；
- 幂等提交的**重放**请求不重新执行转移，**不追加**第二条记录（每个成功批次恰一条）；
- 不携带幂等键的普通提交同样写审计记录。

一条审计记录包含：

| 字段 | 含义 |
| --- | --- |
| `seq` | 链上序号，**从 1 开始连续递增**（`BEGIN IMMEDIATE` 写锁保证不重号） |
| `prev_hash` | 上一条记录的 `record_hash`；`seq=1` 为 64 个 `0`（创世哈希） |
| `record_hash` | 本条记录内容的 SHA-256 小写 hex（算法见下） |
| `summary` | **批次摘要**：`transfer_count` 与逐步明细 `steps`（序号、源、目标、规范化体积，以及按源容器当时混合比例从各祖先来源**实际扣取的精确份额** `taken`） |
| `affected_containers` | **受影响容器**：本批全部源/目标去重后按 ID 排序 |
| `versions` | **结果版本**：提交后每个受影响容器的乐观版本（与批量响应中的 `versions` 一致） |
| `created` | 落库时间（UTC ISO-8601，由数据库生成，**不参与哈希**） |

哈希算法：`record_hash = SHA-256( 规范化 JSON( {seq, prev_hash, summary,
affected_containers, versions} ) )`，规范化指 `sort_keys=True`、紧凑分隔符
`(",", ":")`、不转义非 ASCII 的 UTF-8 JSON（与库中 `payload` 列逐字节一致）。
因此任何对历史记录内容的篡改都会让重算哈希与存储值不符；删除/重排记录则会破坏
序号连续性或后继记录的 `prev_hash`。存储层另有四个 SQLite 触发器
（`trg_audit_records_no_update/no_delete`、`trg_audit_containers_no_update/no_delete`）
拒绝一切通过 SQL 的 UPDATE/DELETE——只追加是存储层强制的，而非仅靠应用约定。

#### 6.1 按序号取单条记录

```bash
curl http://localhost:8000/audit/records/1
```

```json
{
  "seq": 1,
  "prev_hash": "0000000000000000000000000000000000000000000000000000000000000000",
  "record_hash": "23729d1c…e8078",
  "transfer_count": 2,
  "summary": {
    "transfer_count": 2,
    "steps": [
      {"index": 1, "source": "S1", "destination": "V1", "volume": "40.000000000",
       "taken": [{"source_id": "S1", "amount": "40.000000000"}]},
      {"index": 2, "source": "S2", "destination": "V1", "volume": "60.000000000",
       "taken": [{"source_id": "S2", "amount": "60.000000000"}]}
    ]
  },
  "affected_containers": ["S1", "S2", "V1"],
  "versions": {"S1": 1, "S2": 1, "V1": 1},
  "created": "2026-09-21T08:30:12.123Z"
}
```

序号不存在返回 `404 audit_record_not_found`。

#### 6.2 按容器和/或序号范围查询

`GET /audit/records` 的查询参数（均可选，可任意组合）：

| 参数 | 说明 |
| --- | --- |
| `container_id` | 仅返回该容器出现在受影响容器集合中的批次；容器不存在返回 `404 unknown_container` |
| `seq_from` | 序号闭区间下界（含），必须 ≥ 1 |
| `seq_to` | 序号闭区间上界（含），必须 ≥ 1；`seq_from > seq_to` 返回 `422 invalid_seq_range` |

结果一律按 `seq` 升序返回；无命中时 `records` 为空数组（不是错误）。

```bash
# V1 参与过的全部批次
curl 'http://localhost:8000/audit/records?container_id=V1'

# 第 2~10 号记录
curl 'http://localhost:8000/audit/records?seq_from=2&seq_to=10'

# 组合：V1 在第 5 号之后的批次
curl 'http://localhost:8000/audit/records?container_id=V1&seq_from=5'
```

```json
{
  "container_id": "V1",
  "seq_from": 5,
  "seq_to": null,
  "count": 1,
  "records": [ { "seq": 7, "...": "..." } ]
}
```

#### 6.3 校验整条链

```bash
curl http://localhost:8000/audit/verify
```

服务在一致性只读快照内从 `seq=1` 顺序重放全链，逐条检查：

1. 序号从 1 开始**连续无缺号**（检出删除中间记录）；
2. 按 `payload` **重算 SHA-256** 与存储的 `record_hash` 一致（检出内容篡改）；
3. `prev_hash` 等于上一条记录的 `record_hash`，首条为全 0 创世哈希
   （检出断裂/重排，篡改者无法仅凭单条内容伪造链节）；
4. 记录内 `affected_containers` 与 `versions` 的容器集合一致（摘要自洽）；
5. `audit_containers` 索引行与记录中的受影响容器完全一致（索引未被篡改）。

链完好时：

```json
{"valid": true, "count": 3, "head_seq": 1, "tail_seq": 3,
 "tail_hash": "a1b2c3…", "break_at": null, "reason": null}
```

发现任何删改：`valid=false`，`break_at` 给出**首个**断裂记录的序号，`reason`
为可读原因，例如：

```json
{"valid": false, "count": 3, "head_seq": 1, "tail_seq": 3, "tail_hash": null,
 "break_at": 1, "reason": "记录内容与 record_hash 不符（记录被篡改或损坏）"}
```

空链（尚无成功批次）返回
`{"valid": true, "count": 0, "head_seq": null, "tail_seq": null, "tail_hash": null, ...}`。
`tail_hash` 可作为链头锚点对外公布，供外部比对链是否被续写。

---

## 精确性说明

转移量在各祖先来源之间的分摊采用**最大余额法**（Hare quota + Largest remainder）：
每份先向下取整到 `10^-DECIMAL_SCALE`，再把剩余的整数个刻度按被舍掉的余额从大到小补回。
由此保证：

- 各来源取出额之和**精确等于**请求转移体积；
- 每一份额满足 `0 ≤ 取出额 ≤ 源容器中该来源的份额`，因此不会出现“取超了”或负值；
- 与真实比例的偏差不超过 1 个体积刻度；
- 整个系统体积守恒：所有容器的 `amount` 总和始终等于初始样品体积总和。

注意 `proportion` 是四舍五入后的展示值，各比例之和可能不严格等于 1；需要精确核对时请使用
`amount` 字段。

---

## 本地开发（不使用 Docker）

要求 Python 3.11+：

```bash
pip install -r requirements.txt

# 启动（默认在 ./lineage.db 建库；可用环境变量覆盖）
DATABASE_PATH=./lineage.db uvicorn app.main:app --reload --port 8000

# 运行测试
pip install -r requirements-dev.txt
pytest -q
```

## 项目结构

```
app/
  config.py     # 环境变量配置
  db.py         # SQLite 连接、建库与只追加触发器（启动时执行）
  schemas.py    # Pydantic 请求/响应模型（Decimal <-> 字符串）
  service.py    # 核心逻辑：登记、批量转移事务、比例分摊、查询
  audit.py      # 只追加批次审计哈希链：写入、查询、整链校验
  main.py       # FastAPI 路由与错误处理
tests/          # pytest 端到端、分摊算法与审计链测试（含篡改检出）
Dockerfile
docker-compose.yml
docker-entrypoint.sh
```
