# Acceptance report: Night Operator

**Verdict: `PARTIAL / NOT ACCEPTED`.**

Локальный plugin, unit/integration contracts, dry-run demo, manifest validation, path-scoped doctor, native compatibility и fail-closed logical attachment-record boundary проходят на зафиксированном ниже source snapshot. Полная приёмка невозможна до human-only live profile acceptance и реализации/проверки production-контуров scheduler, физической artifact verification, bounded verification commands, notification delivery, production wiring и dangerous-action enforcement.

## 1. Предмет проверки

- Worktree: `C:\Users\shogu\AppData\Local\hermes\scratch\night-operator-20260925`
- Branch: `night-operator-20260925`
- Base HEAD: `d35b3610bc528f6157ee3c82cccef8590ca46137`
- Subject SHA-256: `3d7aa98569fc6bf1a302758f32b1b53cf04fa8e3865722c5921aefaa80d6bc1e`
- Subject definition: SHA-256 от canonical ordered `relative-path:sha256` rows с завершающим `\n` для core, manifest, config example, dry-run demo и двух test-файлов. README/architecture/runbook/acceptance не входят в subject hash, чтобы отчёт не ссылался на собственный hash.

| Файл | SHA-256 | Размер |
|---|---|---:|
| `plugins/night_operator/__init__.py` | `26060da54fe0ffc147153fc2a77a729011bd3568f4b2a3c17b65b8ad6cf6e9cb` | 63477 bytes |
| `plugins/night_operator/demo_dry_run.py` | `8745e8e6655a2d63c5c757d7572a1c93f8153723ddc519f9db37c8d4dd449d90` | 6199 bytes |
| `plugins/night_operator/plugin.yaml` | `8806500420c1ddbd7793d2b1386459d9b89e256200d7a302044e9c7d6c88a8e2` | 1402 bytes |
| `plugins/night_operator/config.example.yaml` | `b0933bf57ddf9eb488efbdbce47d8d01285609ea3253281c9ed17a91ea47670a` | 1359 bytes |
| `tests/plugins/test_night_operator.py` | `3275a1b6b10105902c1eddf240a4d859982211db7455dc242d253c043306cad5` | 19331 bytes |
| `tests/plugins/test_night_operator_integration.py` | `fbba60bb9297c4bf74c41b822d34be0fbf0bb30d91def023de223f0434342491` | 84889 bytes |

Исходники и тесты остаются untracked; commit не создавался. До immutable ref/package плагин нельзя считать готовым к live installation.

## 2. Обработка независимых аудитов

Space Bunny audit `deleg_73116e5e` закончил работу на старом снимке `f346b9c0…` и выдал `FAIL`. Он правильно обнаружил:

- whitespace/prefix/truncation aliases вместо exact raw artifact identity;
- отсутствие native-parent gate для части `done` non-pass решений;
- caller-controlled `implementation_profile` как spoofable authorization source;
- неподтверждённое durable поле `verified_parent_ids`;
- post-materialization лимит attachment rows вместо настоящего read bound.

Повторный Space Bunny audit `deleg_f73398f9` проверил subject `b6c8241d…`, подтвердил 6/7 контрактов и дал `FAIL` из-за двух end-to-end residual-классов:

1. matcher был exact, но `Outcome.metadata["artifacts"]` и `decide()` сохраняли `_safe_ids → _ids` output, то есть strip/dedup/truncate;
2. `verified_parent_ids` сравнивались после `_ids()`, поэтому padded native parent ID принимался как exact.

Оба класса локально воспроизведены содержательными RED: durable long/duplicate identity сокращалась, secret-like identity завершала карточку, `decide()` схлопывал duplicates, padded parent давал `complete`. Дополнительный RED показал тот же parent-нормализующий выход в чистой функции `decide()`. Затем исправлены отдельными GREEN-контрактами. Независимый Space Bunny delta-audit `deleg_58aa6270` на логическом subject `9d842b53…` дал `PASS`: exact artifacts и exact parents подтверждены на `decide()` и обоих terminal-путях `apply_outcome()`; полный suite `81/81`, tree не изменён. Текущий subject `3d7aa985…` отличается от проверенного только удалением лишней пустой строки в конце `__init__.py`; после этой нормализации полный suite повторно зелёный, а финальный immutable commit требует нового независимого review.

## 3. Фактически проверено на текущем source

| Контур | Результат | Доказательство |
|---|---|---|
| Unit + integration suite | **PASS**, 81/81, `RC=0` | `night-operator-exact-final-suite.log`: `Ran 81 tests`, `OK`; запуск при `HERMES_DELEGATED_CHILD_CONTEXT=1` и очищенных Kanban variables |
| Exact raw artifact identity | **PASS**, `RC=0` | `night-operator-exact-identity-green.log`: whitespace, filename prefix и stored-path truncation не принимаются |
| Durable exact artifact round-trip | **PASS**, `RC=0` | `night-operator-auditdelta-targeted-explicit2.log`: длинная redaction-invariant identity и duplicate declarations сохраняются дословно; secret-like identity блокируется |
| Exact parent claims и native set | **PASS**, `RC=0` | `night-operator-exact-parent-green.log`: padded ID блокируется в `apply_outcome()` и `decide()`; durable set формируется из native `task_links` |
| Независимый Space Bunny delta-audit логической правки | **PASS**, `RC=0` | `deleg_58aa6270`, `stealth/space-bunny-alpha`: subject `9d842b53…`, exact artifacts/parents на всех проверяемых путях, полный suite `81/81`; files не изменены. После удаления лишней EOF-строки текущий subject — `3d7aa985…`; финальный commit ещё требует независимого review |
| Native-parent gate для всех `done` решений | **PASS**, `RC=0` | `night-operator-nonpass-parent-green.log`: `changes`, `follow_up`, `needs_input`, `blocked` блокируются до side effects при unrelated source |
| Native implementer provenance | **PASS**, `RC=0` | `night-operator-spoof-implementer-green.log`: caller spoof блокируется для `review` и `done`; identity берётся из закрытого native run |
| Native parent metadata: review | **PASS**, `RC=0` | `night-operator-native-parent-metadata-green.log`: claimed IDs сравниваются с `task_links`, durable metadata получает native set |
| Native parent metadata: done | **PASS**, `RC=0` | `night-operator-done-native-parent-metadata-green.log`: тот же контракт на `done + pass` |
| Hard native parent read bound | **PASS**, `RC=0` | `night-operator-parent-read-bound-done-green.log`: review и done используют LIMITed SQL; 101 parent блокируется до side effects |
| Hard attachment read bound | **PASS**, `RC=0` | `night-operator-attachment-read-bound-green.log`: 1001 rows обнаруживаются через `LIMIT remaining+1`; unbounded `list_attachments()` не вызывается |
| Bounded implementer provenance read | **PASS**, `RC=0` | `night-operator-provenance-read-bound-green2.log`: ровно два `SELECT profile ... LIMIT 1`; `list_runs()` не вызывается |
| Unique declared artifact count | **PASS**, `RC=0` | `night-operator-artifact-count-green.log`: два уникальных identity и три DB rows дают `artifact_record_count=2` |
| Compile gate | **PASS**, `RC=0` | `python -B -m py_compile` для plugin, demo и двух test-файлов |
| Tracked dry-run demo | **PASS**, `RC=0` | `night-operator-exact-final-demo.log`: `dry_run=true`, `writes=0`, `task_count=3`, `comment_count=0`, `outcome_count=2` |
| Plugin manifest validation | **PASS**, `RC=0` | `night-operator-exact-final-validate.log`: manifest/config/loadability/declarations/security scan passed |
| Path-scoped plugin doctor | **PASS**, `RC=0` | `night-operator-exact-final-doctor.log`: discovery/import/registration passed; 0 tools, 4 hooks |
| Plugin compatibility | **PASS**, `RC=0` | `night-operator-exact-final-compat.log`: нет enabled-plugin imports, ожидающих удаления |
| Temporary-board isolation | **PASS** | Tests и demo используют `get_scratch_dir()`, изолируют delegation/Kanban variables и восстанавливают окружение |
| Source EOL | **PASS** | Все шесть subject files и четыре markdown-документа: LF, без mixed EOL, с завершающим newline |

Сообщения о formatting error в начале canonical suite относятся к намеренному негативному `malformed YAML` test. Итог прогона — `81 tests / OK`, поэтому это не suite failure.

Команда suite:

```bash
env -u PYTHONPATH \
    -u HERMES_KANBAN_DB \
    -u HERMES_KANBAN_HOME \
    -u HERMES_KANBAN_BOARD \
    -u HERMES_KANBAN_WORKSPACES_ROOT \
    HERMES_DELEGATED_CHILD_CONTEXT=1 \
    HERMES_HOME='C:/Users/shogu/AppData/Local/hermes/cache/scratch/night-operator-release-suite-exact-final' \
    PYTHONUTF8=1 PYTHONDONTWRITEBYTECODE=1 \
    python -B -m unittest -q \
      tests.plugins.test_night_operator \
      tests.plugins.test_night_operator_integration
```

Команда demo:

```bash
env -u PYTHONPATH \
    -u HERMES_KANBAN_DB \
    -u HERMES_KANBAN_HOME \
    -u HERMES_KANBAN_BOARD \
    -u HERMES_KANBAN_WORKSPACES_ROOT \
    HERMES_DELEGATED_CHILD_CONTEXT=1 \
    HERMES_HOME='C:/Users/shogu/AppData/Local/hermes/cache/scratch/night-operator-demo-exact-final' \
    PYTHONUTF8=1 PYTHONDONTWRITEBYTECODE=1 \
    python -B plugins/night_operator/demo_dry_run.py
```

Path-scoped doctor для source checkout:

```bash
hermes plugins doctor plugins/night_operator --ci
```

Live installation намеренно отсутствует, поэтому команда с installed ID `hermes plugins doctor night-operator --ci` не является локальным source gate.

## 4. Матрица acceptance criteria

| # | Требование ТЗ | Статус | Граница |
|---:|---|---|---|
| 1 | Native Hermes Kanban | **implemented; verified locally** | Четыре native lifecycle hooks и plugin-owned CLI зарегистрированы; `provides_tools: []` |
| 2 | Verification card создаётся native lifecycle/reconciliation | **implemented; verified locally** | Membership берётся из latest `task_runs.metadata`; text задачи не анализируется |
| 3 | Повторы/restart не создают дубликаты | **implemented; verified at function/process level; live restart unverified** | v2 idempotency key, `BEGIN IMMEDIATE`, thread и Windows `spawn` tests; реальный gateway restart не выполнялся |
| 4 | Успешная проверка завершает verification card | **implemented; verified locally** | `pass` требует evidence, active run, native implementer provenance, exact logical attachment record и native parent ownership; physical blob не проверяется |
| 5 | Неуспех содержит конкретные следующие шаги | **implemented; verified on covered outcomes** | `changes`, `follow_up`, `needs_input`, escalation/remediation проверены; полный adversarial audit всех failure combinations не выполнен |
| 6 | `done` remediation/follow-up без reopen | **implemented; verified locally** | Все `done` решения сначала проходят native-parent/implementer gate; parent остаётся `done` |
| 7 | Неоднозначный blocked card эскалируется человеку | **implemented; verified locally** | Fail-closed decision/escalation path без silent pass |
| 8 | Произвольная карточка не закрывается без evidence | **implemented; verified locally** | Missing evidence, spoofed implementer, unrelated parent, alias identity или missing logical record блокируют pass |
| 9 | Опасные действия блокируются | **implemented only as declarations; enforcement unverified; human-only** | `tool_policy()` не является native security boundary; `pre_tool_call` enforcement для plugin не реализован |
| 10 | Durable audit trail | **partially implemented; verified on main paths** | Evidence и native parent IDs проверены; полнота audit trail для provider timeout и contradictory handoff не доказана |
| 11 | Dry-run проходит integration suite | **implemented; verified locally** | 81/81 и tracked demo с `writes=0` |
| 12 | Install/config/run/pause/rollback runbook | **implemented; verified as documentation** | `RUNBOOK.md`; live команды отдельно помечены human-only |
| 13 | Live mode не включается до приёмки | **verified by non-action** | Live profile, board, gateway, cron и write-mode не создавались/не включались |

## 5. Implemented

- Fail-closed config: `enabled=false`, `dry_run=true`; strict booleans и malformed YAML.
- Native hooks: `kanban_task_claimed`, `kanban_task_completed`, `kanban_task_blocked`, `on_kanban_dispatch_tick`.
- Metadata-only batch membership, v2 collision-resistant review key и atomic idempotent card creation.
- Bounded reconciliation с приоритетом `done`; SQL-фильтр исключает plugin-owned candidates до `LIMIT`.
- Durable Unix-epoch throttle `reconcile:last_unix`; legacy monotonic state не блокирует restart.
- Same-card review lifecycle; remediation/follow-up/escalation создаются отдельными cards; `done` history не переписывается.
- Implementer authorization через закрытые native runs: `review_requested` для review и `completed` для done source; caller argument только сверяется.
- Native-parent gate до всех `done` side effects; claimed `verified_parent_ids` обязан совпасть с native `task_links`, durable set формируется из native IDs.
- Fail-closed logical artifact boundary: exact исходных `filename`/`stored_path` в native `task_attachments` verification card или её native parents.
- Настоящие bounded reads: максимум 100 source cards, 1000 attachment rows и один latest closed provenance run; overflow обнаруживается SQL `LIMIT`-witness без materialize-all.
- `artifact_record_count` означает число уникальных заявленных identities, подтверждённых хотя бы одной native row; duplicate rows не раздувают счётчик.
- Общая redaction boundary в `Outcome.__post_init__` для `reason`, `question`, `options` и рекурсивного `metadata`, плюс durable metadata safeguards.
- Dry-run CLI с double gate: plugin enabled, `dry_run=false`, explicit `--write` и отдельное human approval.
- Standalone manifest, 0 model tools, safe config example, architecture/state machine, runbook, dry-run demo и tests.

## 6. Unverified

- Реальная scheduler cadence и сохранение pending work через production gateway restart.
- Native notification delivery: `render_notice()` создаёт payload, но sender/channel не подключён.
- Физическое существование, содержимое или hash blob, а также произвольные file/workspace artifacts. Проверены только native attachment records.
- Bounded verification command runner и проверка acceptance criteria из handoff.
- Production wiring `decide()`/`apply_outcome()` к model/tool call sites.
- Поведение внешних consumers, которые могут игнорировать `artifact_physical_presence="unverified"` и трактовать `terminal_state="complete"` как физическую проверку.
- Реальный profile-scoped dangerous-action enforcement через `pre_tool_call`/capabilities.
- Cryptographic authentication plugin-owned cards. Reserved key + machine marker — только structural guard; writer с DB access может подделать оба поля.
- Полный native audit trail для provider timeout, contradictory handoff и всех error combinations.
- Live rollback/pause/resume на persistent profile.
- Полный global `plugins doctor --ci` на всём host plugin set; path-scoped doctor прошёл, а непринятый global red не подменяет plugin-local gate.

## 7. Human-only

- `hermes profile create/use/remove` для `night-operator`.
- `plugins install/enable/disable/remove` в live profile.
- Capability/tool override grants и любое расширение model tools.
- `enabled=true`, `dry_run=false`, CLI `--write`.
- Gateway start/restart и production Kanban dispatch.
- Cron/routine creation для reconciliation.
- Notification channel creation и external delivery.
- Merge, force-push, deletion, deployment, secret access.
- Решение о bounded command runner или trusted artifact store.
- Восстановление `ibf-operator.approvals.deny`: live config сейчас `[]`, pre-update snapshot содержит 10 rules; точное восстановление и writer audit — отдельная задача. В этом окне восстановление не выполнялось.

## 8. Известные ограничения

1. Structural internal-card guard не равен authentication/authorization и опирается на DB access control.
2. Native run/profile binding доказывает lifecycle provenance по текущей SQLite board, но не криптографическое авторство.
3. `artifact_record_presence="logical_only"` и `artifact_physical_presence="unverified"` — обязательные поля; внешний consumer обязан учитывать их, но это пока не доказано.
4. Native redactor покрывает известные secret-like patterns; это не универсальный DLP и не обнаружение произвольной секретной схемы.
5. `tool_policy()` декларативен; отсутствие plugin tools уменьшает surface, но не создаёт sandbox для всего profile.
6. `escalation_channel: null`; durable human-only card path реализован, внешняя доставка отсутствует.
7. `claim_review_task()` не эмитит `kanban_task_claimed`; plugin не полагается на этот отсутствующий signal.
8. Profiles не наследуют root plugins автоматически; plugin должен находиться в effective profile scope.
9. Profile сам по себе не задаёт board isolation; board path остаётся отдельной конфигурационной границей.
10. Source files untracked; SHA-based acceptance не заменяет immutable commit/package.
11. Полный release не принят, пока scheduler, delivery, physical artifact verification, bounded verification commands, dangerous-action enforcement, production wiring и live profile acceptance не имеют end-to-end evidence.

## 9. Изменения, которые не выполнялись

- Не изменялись core Hermes, Kanban DB schema и live Kanban board.
- Не создавался live `night-operator` profile.
- Не устанавливался и не включался plugin в live profile.
- Не запускались gateway, production dispatcher, cron workers или write-mode.
- Не создавались scheduler jobs и notification channels.
- Не устанавливались новые зависимости; authoritative runner — stdlib `unittest`.
- Не читались `.env`, OAuth files, tokens, credentials или secret stores.
- Не восстанавливался `ibf-operator.approvals.deny`.
- Не создавался commit для untracked plugin/tests.

## 10. Минимальные следующие шаги

1. Human approval: создать отдельный persistent `night-operator` profile из immutable commit/package и не переносить credentials/channels.
2. На isolated board подключить plugin и доказать dispatch cadence, restart durability, отсутствие дублей и bounded throughput.
3. Определить и реализовать отдельный native boundary для физического blob/content/hash и bounded verification commands; не добавлять arbitrary terminal без отдельного решения.
4. Реализовать profile-scoped `pre_tool_call`/capability enforcement и negative E2E tests для denied tools/actions.
5. Подключить native notification adapter, проверить redaction, delivery failure и отсутствие уведомлений для успешного review.
6. Связать `decide()`/`apply_outcome()` с production operator call site; отдельно проверить, что все consumers учитывают `artifact_physical_presence="unverified"`.
7. После закрытия unverified matrix выполнить live E2E, rollback rehearsal и повторный независимый review immutable merge SHA.

До выполнения пунктов 1–7 вердикт остаётся **`PARTIAL / NOT ACCEPTED`**.
