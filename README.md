# NovelCanon

Turn long Chinese novels into a traceable, chapter-queryable structured knowledge base.

**English** | [中文](https://github.com/doebkblcya/NovelCanon/blob/main/README.zh-CN.md)

## Product

NovelCanon builds a structured knowledge base from full-length Chinese novels on SQLite as the single source of truth: unified entity identities, relation/state evolution, event causality, chapter/volume/book summaries, and QA over hybrid full-text, semantic, and structured retrieval.

## Highlights

- **Entity resolution**: merge mentions across chapters into stable canonical IDs; aliases evolve with disclosure order
- **Claims & evidence**: every claim is anchored to verbatim source spans with an assert/update/retract version chain
- **Event causality**: a first-class event link table drives causal-chain queries with explainable path confidence
- **Temporal semantics**: dual independent query axes — `knowledge_cutoff_chapter` (disclosure cutoff) and `world_at_chapter` (world-state replay)
- **Hybrid retrieval**: FTS + vector + structured routes fused per query type; answers carry evidence and chapter references
- **Hierarchical summaries**: chapter → volume → book, invalidated and rebuilt by `max_observed_ordinal`

## Tech Stack

- Storage: SQLite (WAL, transactions, FTS/vector indexes)
- Pipeline: per-chapter extraction → global entity resolution → event linking → evidence verification → hierarchical reduce
- Models: referenced via versioned generation/embedding profiles, swappable

See spec: [docs/定版方案.md](docs/定版方案.md)
