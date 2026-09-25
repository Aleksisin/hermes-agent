# Night Operator

Fail-closed native Hermes Kanban plugin для пакетной проверки завершённых карточек.

**Текущий статус: `PARTIAL / NOT ACCEPTED`.** Native plugin, deterministic state transitions, dry-run CLI и локальные unit/integration contracts реализованы. Live-профиль, scheduler cadence, внешние уведомления, физическая проверка файловых артефактов и production acceptance ещё не приняты.

## Что делает

- Наблюдает native Kanban lifecycle через `kanban_task_claimed`, `kanban_task_completed`, `kanban_task_blocked` и `on_kanban_dispatch_tick`.
- Читает batch membership только из последнего `task_runs.metadata`: `operator_batch` и `operator_expected_ids` (совместимые fallback-ключи: `review_group`, `batch_id`, `expected_ids`).
- Не изменяет историю `done`.
- Создаёт отдельную verification card для полностью завершённой batch-группы.
- Поддерживает native same-card review transitions и отдельные remediation/follow-up/escalation cards.
- Выполняет bounded reconciliation sweep без создания второго Kanban dispatcher.
- Регистрирует один plugin-owned CLI и **ноль model tools**.

GUI, OCR, произвольный terminal, browser, credentials, deployment и произвольные внешние сообщения не используются.

## Native surface

| Surface | Использование |
|---|---|
| `kanban_task_claimed` | Только наблюдение; запуск review не выполняется |
| `kanban_task_completed` | Проверка batch и создание verification card в write-mode |
| `kanban_task_blocked` | Постановка post-commit reconciliation request |
| `on_kanban_dispatch_tick` | Post-lock bounded sweep; периодичность scheduler отдельно не принята |
| `hermes night-operator sweep` | Dry-run по умолчанию; `--write` требует `enabled=true` и `dry_run=false` |
| `provides_tools: []` | Plugin не добавляет terminal/browser/credential tools |

Подробности: [ARCHITECTURE.md](ARCHITECTURE.md), эксплуатация: [RUNBOOK.md](RUNBOOK.md), пример: [config.example.yaml](config.example.yaml), доказательства: [ACCEPTANCE.md](ACCEPTANCE.md).

## Безопасные defaults

```yaml
enabled: false
dry_run: true
board: default
review_group_key: operator_batch
max_review_rounds: 2
reconciliation_interval_seconds: 300
escalation_channel: null
max_items: 100
operator_profile: night-operator
```

- `enabled=false` — master kill switch.
- `dry_run=true` — не создавать task/comment и не менять lifecycle.
- CLI без `--write` принудительно переводит sweep в dry-run.
- Malformed/unreadable YAML возвращает безопасный default; `--write` на таком config отклоняется.
- Board slug нормализуется и проверяется до filesystem path construction.
- Обычные worker-controlled поля проходят redaction до записи. Exact artifact identity принимается только когда redaction не изменяет строку: максимум 100 raw строк по 4000 символов, duplicates и interior spaces сохраняются, а secret-like/padded/prefix/truncated aliases блокируются до side effects.
- Каждый создаваемый `Outcome` рекурсивно редактирует `reason`, `question`, `options` и `metadata` до возврата caller'у или сериализации в CLI.
- Для `review` implementer определяется закрытым native run `review_requested`; для `done` — закрытым native `completed` run источника. Значение caller-controlled `implementation_profile` только сверяется и не является источником истины; mismatch блокирует решение.
- Для любого решения над `done` source сначала выполняется bounded native-parent gate. Не-native source блокируется до remediation/follow-up/escalation side effects.
- `pass` требует exact исходных `filename`/`stored_path` в `task_attachments` verification card или её native parents. Проверка ограничена record metadata и не вызывает `stat()`, read или hash; whitespace, prefix и truncated aliases не принимаются.
- `verified_parent_ids` сравнивается с native `task_links` без `strip`, deduplication или truncation: только unique exact redaction-invariant IDs; durable metadata записывает native set.
- Parent graph, provenance run и attachment rows читаются прямыми SQL `SELECT` с hard `LIMIT`; максимум 100 source cards и 1000 attachment rows включают overflow witness.
- `artifact_record_count` равен числу уникальных заявленных artifact identities, для которых найдена хотя бы одна native row; duplicate rows не раздувают счётчик.
- Внутренние plugin-owned cards исключаются до SQL `LIMIT`, поэтому verification history не расходует bounded slot внешней работы.

## Проверка без live-профиля

Из корня checkout:

```bash
python -m unittest \
  tests.plugins.test_night_operator \
  tests.plugins.test_night_operator_integration -v

python plugins/night_operator/demo_dry_run.py
hermes plugins validate plugins/night_operator
hermes plugins doctor plugins/night_operator --ci
```

`pytest` для этих contracts не требуется и не устанавливается.

## Важные границы

1. `tool_policy()` — декларация, не enforcement: native core не читает этот dict как security boundary.
2. `render_notice()` формирует payload, но не доставляет уведомление.
3. Plugin не исполняет bounded verification commands. Для `pass` он fail-closed проверяет только logical attachment records; физическое существование, содержимое и hash blob не проверяются.
4. Структурный internal-card guard не является криптографической аутентификацией карточки; forged key + machine marker остаётся допустимым на уровне DB access control.
5. `done + pass` дополнительно сравнивает durable `created_by` с профилем активного run. Implementer identity для `review` и `done` дополнительно сверяется с закрытым native `task_runs`; structural ownership не является криптографическим авторством.
6. `HERMES_HOME` внутри Hermes-root не изолирует Kanban сам по себе; изолированный прогон обязан задать `HERMES_KANBAN_HOME` и `HERMES_KANBAN_DB`, а fixtures дополнительно снимают inherited delegation/worker-fence переменные.
7. Локальные tests и demo создают временные boards только под `get_scratch_dir()` и восстанавливают исходное окружение.
8. `claim_review_task()` не эмитит `kanban_task_claimed`; это особенность native lifecycle.
9. До отдельного human approval нельзя создавать live profile, включать plugin, запускать `--write` или менять gateway/cron.
