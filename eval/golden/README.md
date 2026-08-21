# 黄金集标注目录（eval/golden）

本书黄金集标注的**入库**内容。版权语料不入库：

- 盲测章节原文（ordinal 1/12/16）在本地忽略目录
  `data/annotations/盲测集原文-1-12-16.md`（git 不跟踪，仅标注时对照）；
- 仓库只提交：`book_content_hash`（防错配）、章节 ordinal、人工标注
  （qas/entity_merges/facts/causals/evidence_spans）与必要的短证据 span
  （逐字短句，合理引用，非全文）。

## 文件

- `百年孤独-golden-v1.json`：冻结黄金集骨架（schema `golden-v1`，
  `book_content_hash=dcea09d9…`）。标注完成后此文件即冻结，不得因
  评测结果修改。

## 校验命令

```bash
# 加载 + 全量校验（book_id / 章节 ordinal / 内容 hash / span 逐字）
.venv/bin/python -m novelcanon.cli pilot book_cc --golden eval/golden/百年孤独-golden-v1.json
```
