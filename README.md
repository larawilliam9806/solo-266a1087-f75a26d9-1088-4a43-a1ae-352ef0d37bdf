# 迁库题录 · 分盒对账

> 状态：已交付首版（Flask + SQLite + Docker Compose）。

## 业务场景

馆藏从旧系统迁往新系统后，需要核对迁库题录。旧库明细不开放批量导出，只能在
档案阅览室按盒调阅；旧库端可以导出**每盒条数和 SHA-256 摘要**。因此对账按盒
进行：

1. 新库端导入固定的旧库清单（每盒条数 + 摘要）和新库题录。
2. 清单盒在新库缺题录的，按空盒（0 条）参与比较。
3. 只有新库存在的盒，直接列为**多录盒**，不必回旧库。
4. 其余盒列入**须回旧库调明细**清单，按盒调回明细后上传。
5. 回传明细的条数和摘要必须都与清单相符，才按档号列出漏录、多录和字段新旧值。
6. 核对期间可随时按盒换交新题录；某盒内容一变，撤下该盒原差异并按新内容更新
   结论，其他盒照常可查。
7. 若提交结果时该盒内容已变（页面过期/并发），拒绝保存本次结果。
8. 换交或核对失败时，原有数据和仍有效的结论保持不变。

## 摘要口径

摘要必须对行序和等价 CSV 写法不敏感。实现见
`archive_reconcile/canonical.py::canonical_payload`：

```
payload = {
  "format": "archive-box-v1",
  "box_no": "<盒号>",
  "items": [
    {"archive_id": "...", "retention": "...", "title": "..."}, ...
  ]
}
digest = sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True,
                           separators=(",", ":")).encode("utf-8")).hexdigest()
```

- `items` 按 `archive_id` 排序（盒内档号唯一，故为全序）。
- 文本经 NFC 规范化并去除首尾空白，字段内 CRLF/CR 归一为 LF。
- 不受影响：行序、列序、UTF-8 BOM、CRLF/LF、字段引号、逗号后空格、空行、
  可选的 10 行以内说明前导行、UTF-8 / GB18030 编码。

旧库端导出清单：

```bash
python -m archive_reconcile.export_manifest 旧库题录.csv -o manifest.csv
```

## 运行

### Docker Compose

```bash
docker compose up -d --build
# 浏览器打开 http://localhost:8000
```

数据保存在命名卷 `reconcile-data` 的 `/data/reconcile.sqlite3`。可用环境变量
`APP_PORT`、`SECRET_KEY`、`GUNICORN_WORKERS` 覆盖默认值。

### 本地

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
flask --app archive_reconcile.app run --debug
```

### 测试

```bash
pip install pytest
python -m pytest -q
```

## CSV 格式

题录（新库题录、换交、回传明细通用）：

```csv
盒号,盒内唯一档号,题名,保管期限
B1,A-001,关于甲的报告,永久
```

清单（旧库端导出）：

```csv
盒号,条数,SHA-256摘要
B1,3,<64 位十六进制>
```

列名也识别英文别名（如 `box_no`、`archive_id`、`title`、`retention`、
`item_count`、`digest`），表头列序不限。

## 数据模型与一致性的实现

- `manifest`：批次建立时写入，之后只读。
- `box_versions` / `box_items`：新库题录的只追加版本。重复导入相同内容不产生
  新版本；内容变化产生新版本，并在**同一事务**里删除该盒旧结论、按新内容和
  已有旧库明细立即重算。
- `old_details`：通过清单校验的旧库明细。清单不变则明细长期有效，因此换交后能
  立刻用同一份明细重新比较。
- `reconciliations` / `differences`：每盒一条结论 + 按档号的差异行。
- 保存结论时以 `expected_version_id`（页面渲染时看到的版本）做乐观并发校验；
  不匹配抛 `StaleBoxError`，事务回滚，409 返回并保留原有数据。
- 明细先校验条数与摘要，再落库；不符则抛 `ManifestMismatchError`，不写任何行。

## 目录

```
archive_reconcile/
  app.py              Flask 路由（批次、分盒看板、单盒、换交、明细、确认、导出）
  canonical.py        CSV 解析、文本规范化、盒摘要
  export_manifest.py  旧库端清单导出 CLI
  reconcile.py        清单校验与逐档号比较规则
  store.py            SQLite 模式与事务化存储
  views.py            看板投影
  templates/          Jinja 模板
  static/style.css    样式
tests/                规范化、清单校验、并发/换交/失败保留等测试
```
