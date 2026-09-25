# Runbook: Night Operator

> Текущий статус: `PARTIAL / NOT ACCEPTED`. Команды изменения live profile/config и включения write-mode выполняются только после отдельного human approval.

## 1. Безопасная локальная проверка

Из корня Hermes checkout:

```bash
python -m unittest \
  tests.plugins.test_night_operator \
  tests.plugins.test_night_operator_integration -v

python plugins/night_operator/demo_dry_run.py
hermes plugins validate plugins/night_operator
hermes plugins doctor plugins/night_operator --ci
```

Ожидаемые свойства:

- unittest: `OK`, exit code `0`;
- demo: `writes=0`, `dry_run=true`;
- validate/doctor: exit code `0`.

Если доступен `pytest`, его отсутствие не является дефектом plugin: authoritative contracts используют stdlib `unittest`.

## 2. Изолированный discovery

`HERMES_HOME` внутри Hermes-root не изолирует Kanban сам по себе. Перед любым acceptance-запуском снимите inherited delegation/worker fence и задайте все переменные:

```bash
unset HERMES_DELEGATED_CHILD_CONTEXT HERMES_KANBAN_TASK \
      HERMES_KANBAN_RUN_ID HERMES_KANBAN_CLAIM_LOCK

export HERMES_HOME="$PWD/.hermes-acceptance/home"
export HERMES_KANBAN_HOME="$PWD/.hermes-acceptance/board"
export HERMES_KANBAN_DB="$PWD/.hermes-acceptance/board/kanban.db"
export HERMES_KANBAN_BOARD="default"
export HERMES_KANBAN_WORKSPACES_ROOT="$PWD/.hermes-acceptance/workspaces"
export HERMES_BUNDLED_PLUGINS="$PWD/plugins"
```

Перед запуском выведите и проверьте resolved paths:

```bash
python -c "from hermes_constants import get_hermes_home,get_default_hermes_root; from hermes_cli.kanban_db import kanban_home,kanban_db_path; print(get_hermes_home()); print(get_default_hermes_root()); print(kanban_home()); print(kanban_db_path())"
```

Условие приёмки: `kanban_home()` и `kanban_db_path()` находятся внутри acceptance directory, а не в live root.

## 3. Dry-run demo

```bash
python plugins/night_operator/demo_dry_run.py
```

Demo создаёт только временную SQLite board под Hermes `cache/scratch`, изолирует delegation/Kanban variables, заполняет synthetic `done` tasks, вызывает plugin CLI без `--write` и удаляет temporary directory при выходе. Live board, gateway, profile и credentials не затрагиваются. Demo не зависит от текущего cwd как entry point и проходит внутри delegation child context.

## 4. Создание live profile — human-only

Команда создаёт persistent profile и может создать wrapper:

```bash
hermes profile create night-operator --no-alias
```

Не используйте `--clone`, `--clone-all` или `--clone-channels`: они копируют config/state и потенциально credentials/channels. Если профиль уже существует, сначала подтвердите его effective home и plugin list.

## 5. Установка plugin — human-only

Из опубликованного Git repository с immutable 40-hex commit:

```bash
hermes -p night-operator plugins install <git-url-or-owner-repo> \
  --ref <40-char-commit-sha> --no-enable --no-deps
```

Bare local worktree не является installable Git repository. Для source checkout используйте validation/discovery отдельно; не копируйте plugin package в live profile без проверки effective key.

Активация без выдачи tool override:

```bash
hermes -p night-operator plugins enable night-operator --no-allow-tool-override
```

После enable обязательно проверьте effective key. Flat install обычно даёт `night-operator`; category install может дать path-derived key. Plugin использует `ctx.plugin_id`, а не hardcoded display name.

## 6. Конфигурация

Безопасный пример находится в [config.example.yaml](config.example.yaml). Plugin settings читаются из:

```text
plugins.entries.<effective-plugin-id>.settings.*
```

Минимальные команды для отдельных значений:

```bash
hermes -p night-operator config set plugins.entries.night-operator.settings.enabled false
hermes -p night-operator config set plugins.entries.night-operator.settings.dry_run true
hermes -p night-operator config set plugins.entries.night-operator.settings.board default
```

Не помещайте keys/tokens в config. Plugin не читает `.env`, OAuth files или secret stores.

## 7. Inspect

```bash
hermes -p night-operator plugins show night-operator
hermes -p night-operator plugins doctor night-operator --ci
hermes -p night-operator kanban list --status review --json
hermes -p night-operator kanban show <task-id> --json
hermes -p night-operator night-operator sweep --board default --json
```

Последняя команда принудительно dry-run, если нет `--write`.

## 8. Pause

Plugin-specific pause:

```bash
hermes -p night-operator plugins disable night-operator
```

Глобальный emergency pause (останавливает cron/Kanban dispatch и новые gateway turns шире plugin):

```bash
hermes pause
```

Не используйте global pause как замену plugin kill switch: его scope больше.

## 9. Resume

```bash
hermes -p night-operator plugins enable night-operator --no-allow-tool-override
hermes resume
```

После resume проверьте plugin show и dry-run sweep до любых writes.

## 10. Write-mode activation — human-only

Write-mode не включается одной командой. Нужны одновременно:

1. plugin enabled в effective profile;
2. `plugins.entries.<id>.settings.enabled=true`;
3. `dry_run=false`;
4. явный CLI `--write` для sweep;
5. отдельное human approval.

После этого сначала выполните read-only sweep, сравните ожидаемые cards и только затем запускайте bounded write command. Не включённый `enabled=true` или всё ещё включённый `dry_run=true` блокирует write с exit code `2`.

## 11. Rollback

Остановить автоматические действия:

```bash
hermes -p night-operator plugins disable night-operator
```

Отключить plugin settings:

```bash
hermes -p night-operator config set plugins.entries.night-operator.settings.enabled false
hermes -p night-operator config set plugins.entries.night-operator.settings.dry_run true
```

Удаление plugin package — только после подтверждения effective install source:

```bash
hermes -p night-operator plugins remove <effective-plugin-name-or-key>
```

Не удаляйте profile или board data в аварийной ситуации до backup и human approval. `done` history и audit trail не переписываются.

## 12. Incident: duplicate cards

1. Оставить plugin disabled или write-disabled.
2. Найти card rows по reserved idempotency key.
3. Сверить `task_runs`, comments и events.
4. Не удалять и не переоткрывать `done` parent.
5. Создать human decision/remediation card только после согласования.
6. Если key формата `v1`, миграция в `v2` требует отдельного acceptance; молчаливое смешивание запрещено.

## 13. Incident: suspicious verification

1. Self-approval, unclaimed review, forged structural marker, spoofed `implementation_profile`, unrelated `done` source, mismatched `verified_parent_ids` или alias artifact identity → `blocked`, human-only.
2. Не переходить сразу к remediation/completion: сначала подтвердить, что решение было отклонено **до** соответствующего side effect.
3. Не считать structural marker криптографической аутентификацией.
4. Проверить DB access control и источник reserved key/body marker.
5. Сверить lifecycle provenance с native SQLite: для review implementer — последний закрытый run `review_requested` verification card; для done source — последний закрытый run `completed` источника.
6. Сверить `verified_parent_ids` с native `task_links` без нормализации: только unique exact IDs; surrounding whitespace и duplicate claim означают `blocked`, в durable metadata допустим только native set.
7. Сверить каждый artifact с исходным `filename`/`stored_path`; surrounding whitespace, prefix и truncation являются другими identity. Длинная redaction-invariant строка и duplicate declaration сохраняются дословно; secret-like значение, которое redactor изменяет, должно блокироваться до `complete_task()`.
8. Проверить bounded refusal: >100 source cards, >1000 attachment rows или отсутствие bounded provenance row должны дать `blocked`, а не чтение unbounded history.
9. `artifact_record_presence="logical_only"` и `artifact_physical_presence="unverified"` не доказывают существование blob; не выдавать `complete` за physical verification.
10. При необходимости остановить профиль и провести board-level review.

## 14. Incident: scheduler/restart

1. Проверить, действительно ли runtime вызывает `on_kanban_dispatch_tick`.
2. Проверить `reconcile:last_unix`, а не legacy monotonic state.
3. Запустить один bounded dry-run sweep.
4. Сравнить task count до/после.
5. Restart acceptance выполняется только на isolated board и не на live profile без approval.

## 15. Human-only operations

- `hermes profile create/use/remove`;
- `plugins install/enable/disable/remove`;
- capability/tool override grants;
- `enabled=true`, `dry_run=false`, CLI `--write`;
- gateway start/restart и production Kanban dispatch;
- cron creation для reconciliation;
- notification channel creation и external delivery;
- merge, force-push, deletion, deployment, secret access;
- product/architecture decisions и изменение acceptance criteria.
