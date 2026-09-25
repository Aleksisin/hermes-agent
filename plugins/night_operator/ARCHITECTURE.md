# Архитектура Night Operator

## 1. Цель

Night Operator — native Hermes Kanban plugin, который наблюдает состояния и события Kanban, образует однозначные review batches и проводит проверку через native lifecycle. Окно Hermes является только отображением состояния; пиксели, screenshots и OCR не являются источником истины.

## 2. Не-цели

- GUI automation и OCR.
- Изменение core Hermes или Kanban DB schema.
- Автоматическое изменение acceptance criteria.
- Self-approval, merge в main, force-push, удаление данных, deployment или secret access.
- Произвольный terminal/browser/credential surface.
- Собственный бесконечный Kanban dispatcher.
- Внешняя доставка уведомлений до подключения native notification API.

## 3. Компоненты

| Компонент | Ответственность |
|---|---|
| `register(ctx)` | Регистрация четырёх hooks и plugin-owned CLI |
| `_on_completed` | Observer completed hook; никакого LLM-анализа |
| `_on_blocked` | Ставит bounded post-commit reconciliation request |
| `_on_dispatch_tick` | Post-lock reconciliation с durable cadence gate |
| `_batch_from_metadata` | Извлекает batch и expected IDs только из run metadata |
| `_batch_state` | Проверяет membership и `done` status всех parents |
| `create_or_get_verification` | Atomic create/get verification card с versioned idempotency key |
| `decide` / `apply_outcome` | Контракт outcome и native transitions |
| `reconcile` | Один ограниченный sweep: сначала `done`, затем `blocked`/`triage` |
| `night_operator_command` | Dry-run CLI; write только через двойной fail-closed gate |
| `render_notice` | Формирует минимальный redaction-safe payload; delivery не реализована |

## 4. Trust boundaries

```text
worker-controlled task text / run metadata
                 │ untrusted
                 ▼
       validation + redaction
                 │
                 ▼
       native Kanban DB / task_runs
                 │
                 ▼
      plugin lifecycle transitions
                 │
                 ▼
      human-only decision card
```

- Task title, body, summary, metadata, comments и events считаются untrusted input.
- Инструкция в карточке не может изменить policy, batch identity или terminal outcome.
- Secrets редактируются до body, summary, durable metadata, comment, notice и любого caller-facing `Outcome`.
- `Outcome.__post_init__` является общей границей redaction для `reason`, `question`, `options` и рекурсивного `metadata`; отдельные call sites не должны обходить её.
- `tool_policy()` не является sandbox; plugin safety обеспечивается отсутствием model tools и отказом от live activation.

## 5. Поток данных

### Completed path

1. Native Kanban завершает implementation task.
2. `kanban_task_completed` передаёт `task_id`, `board` и native payload.
3. Plugin читает последний `task_runs.metadata`.
4. Если `operator_batch` и `operator_expected_ids` отсутствуют или неоднозначны:
   - dry-run: решение без write;
   - write-mode: отдельная human-only decision card; source task не меняется.
5. Если task не входит в `expected_ids`, создаётся human-only decision card; membership не угадывается.
6. Если все expected parents имеют `status=done`, создаётся ровно одна verification card.
7. Повтор события, sweep или restart находят ту же idempotency key и не создают дубль.

### Review path

1. Verification card назначается operator profile и проходит native claim/review lifecycle.
2. `pass` требует evidence, artifacts и активный `current_run_id`.
3. Implementer для `source_status=review` берётся из последнего закрытого native run с outcome `review_requested`. Переданный `implementation_profile` только сверяется с этим native source of truth.
4. Каждый заявленный artifact должен exact-совпадать с исходным `filename` или `stored_path` attachment record на verification card либо её native parents. Нормализация whitespace, deduplication и silent truncation не создают aliases. Exact identities длиной до 4000 символов сохраняются в durable output, включая duplicates и interior spaces, только если redaction boundary является no-op; secret-like или redacted identities блокируются до side effects. Проверка ограничена `task_attachments`; `stat()`, чтение содержимого и hash не выполняются.
5. `verified_parent_ids` должен быть списком unique exact redaction-invariant IDs и совпадать с native `task_links`; padded, duplicate, redacted или иным образом нормализованный claim блокируется, а durable metadata получает только native IDs.
6. Self-approval блокируется сравнением native implementer profile с профилем активного reviewer/operator run.
7. Для `source_status=done` дополнительно требуется structural internal-card marker и совпадение durable `created_by` с профилем активного run; изменяемый `assignee` не является доказательством ownership. Указанный source должен быть native parent verification card. Этот gate выполняется до side effects для `pass`, `changes`, `follow_up`, `needs_input` и `blocked`.
8. `changes` использует `request_changes`; после `max_review_rounds` создаётся escalation card.
9. `needs_input`/`blocked` переводят карточку в blocked без silent success.
10. Все terminal transitions связывают action с `expected_run_id`, когда native API это поддерживает.

### Done-source path

Исходная `done` карточка остаётся `done`. До любого remediation/follow-up/escalation/completion plugin подтверждает, что source имеет status `done`, входит в bounded native `task_links` verification card, а implementer profile совпадает с её закрытым native `completed` run. Результат проверки оформляется новой карточкой:

- `changes` → remediation card, terminal state `remediation`;
- `follow_up` → отдельная follow-up card, terminal state `follow_up`;
- `needs_input`/`blocked` → escalation card;
- `pass` → завершается только verification card с durable redacted metadata.

## 6. State machine

```text
implementation: running ──complete──► done
                                      │
                         batch incomplete / ambiguous
                                      ▼
                              decision_required
                              (human-only card)
                                      │
                         all expected parents done
                                      ▼
                            verification_created
                                      │
                             reviewer claims run
                                      ▼
                                  running
                    ┌─────────────────┼──────────────────┐
                    │                 │                  │
                   pass             changes          needs_input
                    │                 │                  │
                 complete          request_changes       block
                                      │                  │
                         rounds remain│         escalation / human
                                      ▼
                          ready → worker → review
                                      │
                              rounds exhausted
                                      ▼
                                escalation

source_status=done:
  pass       → complete verification card only
  changes    → remediation card; source remains done
  follow_up  → follow-up card; source remains done
  blocked    → escalation card; source remains done
```

## 7. Idempotency и конкуренция

- `review_key` имеет версию `v2` и digest канонической структуры `{board, batch, sorted(expected_ids)}`.
- Digest исключает delimiter ambiguity и сохраняет идемпотентность при перестановке members одного batch.
- Native card creation выполняет idempotency lookup и insert внутри `kanban_db.write_txn` (`BEGIN IMMEDIATE`).
- Thread- и multiprocess-контракты проверяют одну task ID и одну idempotency row.
- Старый textual `v1` формат не смешивается с `v2`.
- Перед live rollout исторические `v1` cards требуют отдельной миграции; сейчас plugin не активирован live.

## 8. Bounded native reads

- Parent graph: максимум 99 native parents плюс один overflow witness; вместе с verification card лимит равен 100 source cards.
- Implementer provenance: один прямой `SELECT profile FROM task_runs ... ORDER BY ... LIMIT 1` для последнего закрытого run нужного outcome; полная история `task_runs` не материализуется.
- Attachment metadata: максимум 1000 принятых attachment rows суммарно; каждый source-card SELECT использует `LIMIT remaining+1`, чтобы overflow обнаруживался до чтения хвоста.
- Exact artifact identity: максимум 100 исходных строк, каждая не длиннее 4000 символов; значения не нормализуются и не усекаются. Parser допускает строку, только если `_redact_structure(value) == value`; redaction-invariant длинные строки и duplicates сохраняются, secret-like значения блокируются.
- `artifact_record_count` считает уникальные заявленные identities, подтверждённые хотя бы одной native attachment row. Это не число DB rows и не доказательство физического blob.

## 9. Reconciliation и restart

- Sweep bounded параметром `max_items`; `done` рассматриваются первыми, чтобы blocked/triage не могли исчерпать весь budget.
- Plugin-owned candidates исключаются SQL-фильтром до `LIMIT`: внутренняя verification history не может исчерпать slot внешней работы.
- Blocked lifecycle hook не пишет из чужой транзакции; он только планирует post-commit work.
- На dispatch tick выполняется board scan, поэтому потеря process-local planned request не является единственным носителем reconciliation.
- Durable cadence хранится в `reconcile:last_unix`; `time.monotonic()` используется только process-local и не переносится через restart.
- Legacy `reconcile:last` с monotonic-значением игнорируется.

## 10. Internal-card identity

Card считается plugin-owned только при согласованной структуре:

1. `idempotency_key` начинается с reserved `operator-*` prefix;
2. body содержит machine marker `[night-operator:<kind>]`.

Title сам по себе не является доказательством. Это защита от worker-controlled title spoof, но не cryptographic authentication: writer, способный одновременно подделать key и comment/body marker, остаётся доверенным только на уровне access control самой board DB.

## 11. Dry-run semantics

Dry-run запрещает:

- создание verification/decision/remediation/follow-up/escalation cards;
- comments;
- native lifecycle transitions;
- write-mode plugin settings.

Он возвращает только bounded observation outcomes. Повреждённый YAML не повышает привилегии: используется `enabled=false`, `dry_run=true`, а `--write` отклоняется.

## 12. Что не реализовано или не принято

- Live profile creation и live plugin enablement.
- Доказательство фактической scheduler cadence `on_kanban_dispatch_tick` на gateway restart.
- Native notification delivery; `escalation_channel` пока только setting.
- Физическая проверка существования/содержимого/hash произвольных файловых артефактов. Реализована только bounded logical presence attachment records на verification card и её native parents.
- Bounded verification command runner.
- Production wiring `decide()`/`apply_outcome()` к model/tool call sites.
- Cryptographic authentication plugin-owned cards.
- Полная изоляция plugin Python execution от других native capabilities.
