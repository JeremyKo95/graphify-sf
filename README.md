# graphify-sf

> ⚡ A **Salesforce-native** reimagining of [graphify](https://github.com/safishamsi/graphify) —
> rebuilt from the ground up for LWC, Apex, and the realities of Governor Limits.
> Same elegant graph idea, now first-class on the Salesforce Platform.

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](./LICENSE)
[![Salesforce API](https://img.shields.io/badge/Salesforce-API%20v60.0-00A1E0.svg)](#)

---

## Why This Version? (Key Enhancements)

`graphify` is a brilliant, general-purpose graph library. But the Salesforce Platform
plays by its own rules — Governor Limits, Lightning Web Security (LWS), and CPQ data
shapes simply don't exist in a vanilla JS/Node world. `graphify-sf` keeps the original's
clean API while solving the platform-specific problems below.

| Concern on Salesforce | Vanilla `graphify` | ✅ `graphify-sf` |
|---|---|---|
| **Governor Limits** | Unbounded loops can blow SOQL/CPU limits | Bulkified traversal with built-in `Limits`-aware batching (e.g. auto-chunks at 90% CPU budget) |
| **LWS / Locker Service** | Relies on globals/`eval` blocked in LWS | 100% LWS-compliant, zero sandbox-violating APIs |
| **CPQ data model** | No concept of Quote/QuoteLine relationships | Native adapters for `SBQQ__Quote__c` → `SBQQ__QuoteLine__c` graphs out of the box |
| **Server ↔ Client** | Browser-only | First-class **Apex** counterpart (`GraphifyService.cls`) sharing the same edge model |
| **Reactivity** | Manual DOM wiring | Ships as a reactive **LWC** with `@wire`-friendly data binding |
| **Eventing** | None | Optional **Platform Event** hooks to stream graph mutations across sessions |

> 📊 *Example benchmark (replace with your real numbers):* traversing a 5k-node CPQ quote
> graph stays under **38% of the synchronous CPU limit**, vs. timeouts when porting the
> original library naively.

### Quick Start

```bash
# LWC component
sf project deploy start -d force-app/main/default/lwc/graphify

# Apex service
sf project deploy start -d force-app/main/default/classes/GraphifyService.cls
```

---

## Inspiration & Credits

This project stands on the shoulders of [**graphify**](https://github.com/safishamsi/graphify)
by **[Safi Shamsi](https://github.com/safishamsi)**. The core idea, architecture,
and API ergonomics are theirs — `graphify-sf` is an independent, Salesforce-tailored adaptation
built with deep respect for the original work. If you need a platform-agnostic solution, please
use and support the upstream project.

> 본 프로젝트는 [graphify](https://github.com/safishamsi/graphify)의 아이디어와 아키텍처에서
> 출발했습니다. 핵심 개념에 대한 모든 공로는 원작자에게 있으며, `graphify-sf`는 이를 Salesforce
> 플랫폼(LWC/Apex/CPQ) 환경에 맞게 재구현한 독립적인 파생 프로젝트입니다. 원작자의 노고에 깊이
> 감사드립니다. 범용 환경이 필요하다면 원본 프로젝트를 사용하시길 권장합니다.

> The upstream project's original README is preserved in this repo as [`README.upstream.md`](./README.upstream.md).

## License

MIT © Safi Shamsi (graphify) · MIT © JeremyKo95 (graphify-sf). See [LICENSE](./LICENSE).
