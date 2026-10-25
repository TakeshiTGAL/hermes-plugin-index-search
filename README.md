# index-search

Search one index you run, and watch one saved query for new ids on the first page.

## First request

The first call does not need an API key and does not save a watch.

1. `hermes plugins install TakeshiTGAL/hermes-plugin-index-search --enable`
2. Set `MEILI_HOST` to the origin, such as `http://127.0.0.1:7700`. A search-only `MEILI_API_KEY` is not required for this first call.
3. The Meilisearch server is already running.
4. Ask "is the search server up". The model calls `index_health` with no arguments. That call does not ask for approval. A search needs `index`, `q`, and `id_field` on `index_search`. Saving a watch query is a later step. This plugin does not ask.

The first call is index_health with no arguments. 日本語: 最初は引数なしの index_health。検索は index と q と id_field。見張りは後で、このプラグインは聞かない。

The product name is `index-search`. The toolset is `index_search`. The tools are `index_search`, `index_health`, and `index_watch`. There is no slash command and no CLI. The plugin does not ship a search engine and does not call `/mcp`. It does not register a hook. `register` does not import `tests`.

Tools are on for every Hermes surface, including the gateway, until you turn a surface off with `hermes tools`. There is no separate user list. Anyone you allow on the gateway can search with your key. `hermes tools` does not disable a slash command, because this plugin has none.

`index_search`, `index_health`, and `index_watch` do not ask for approval. This plugin does not read yolo, approvals off, cron, `-q`, or unattended. Those are Hermes's own gate, and this plugin never returns an approve line. Requires Hermes `>=0.21.6`. An index uid such as `movies_en_us` and an id field such as `created_at_id` are stored unchanged. The printed cron line is not truncated.

## Skill

The plugin registers one skill, `meilisearch-index`. It tells the model to look up `index_search`, `index_health`, and `index_watch` with tool_search, in English, before it tries web_search. The skill runs nothing and sends nothing by itself.

This plugin requires Hermes `>=0.21.6`. While the plugin is enabled, the skill adds one line to the skills index of every session. That includes a surface where the tools were turned off with `hermes tools`: the line stays there, and the calls are simply unavailable. There is no separate user list for the skill; every session that loads the plugin can read it. `hermes plugins disable index-search` removes the skill and the tools together. A cron job you already created stays until you run `hermes cron remove <id>`.

## What you configure

`MEILI_HOST` is required. It is an origin: `http` or `https`, a host, and an optional port. There is no default host. A userinfo, path, query, or fragment is refused before HTTP. Plain `http` is allowed. Over plain `http`, `MEILI_API_KEY` is sent unencrypted.

`MEILI_API_KEY` is sent only as `Authorization: Bearer` on `POST /indexes/{uid}/search`. Use a search-only key. A master or admin key in that variable is still sent with the search. `GET /health` sends no key, even when the variable is set. No other route is called. Redirects (301, 302, 303, 307, 308) are not followed. A proxy set in the environment is not used, so the key is not sent to it. A response body over 2,000,000 bytes is not accepted. An IPv6 host keeps its brackets.

`index_search` takes `index`, `q`, `id_field`, and an optional `title_field`. An empty or whitespace-only query is refused before HTTP, because the server treats an empty `q` as a match of every document. `q` is at most 400 characters. The index uid is 1 to 64 letters, digits, hyphens, or underscores. The reply is the id and, when you name `title_field`, a title of at most 200 characters. The rest of the hit is not returned. `1` and `"1"` stay distinct ids. You name the id field, because displayed attributes can hide the primary key. A hit without a usable id is not a successful page.

The search body sends `limit` 20. A successful reply includes `estimatedTotalHits`. Only the first 20 hits are watched. When that total is over 20, matches past the page are never reported. `save` says so, and a quiet watch reports it with `notify` true, starting with the baseline, so the warning reaches the delivery target instead of being dropped as a quiet round. While the total stays over 20 that quiet warning repeats once a day. A check that reports new ids includes the same warning every time. It starts again when the total falls to 20 or fewer and rises past 20 later. If the page shares no saved id and the total is over 20, the result says matches between them may have been missed. `save` itself does not search. A page whose `hits` array has more than 20 objects does not advance seen ids. Each read stops if no data arrives for 30 seconds. There is no total deadline for one run. The index uid must be a string. A number or a boolean is refused before HTTP and is not saved.

## Watch

`index_watch` actions are `save`, `status`, and `check`. `schedule` and `unschedule` do not create or remove a cron job. One watch. `save` does not search and does not create a cron job. It stores the index, query, id field, and optional title field in the state file, and it prints the `hermes cron create` line and does not delete the job. A cron child reads that file. It does not read `config_schema`.

You create the job. The printed line runs every 15 minutes and uses `--deliver local`. There is no daily cap in this plugin. If you do not stop it, that is 96 runs a day. Each run is a model turn. A turn that calls a tool is another model request. After `hermes plugins remove`, the job stays until you run `hermes cron remove <id>`. When the profile is not `default`, put `-p <name>` before the subcommand: `hermes -p <name> cron remove <id>`.

Seen ids advance only when `check` is a proven cron run and Hermes is not isolating this plugin. The cron session marker is `cron.execution_identity.current_cron_execution` (Hermes 0.21.6). This plugin does not read `tools.approval_context._is_cron_approval_context`. A manual `index_search`, including one made from cron, does not write seen ids. A manual `check` does not write them either, and when it lists ids it says this call did not write seen ids. The plugin cannot force the cron model to call `check`, and it cannot force the model's wording. If the model calls `index_search`, the cursor does not move.

The first successful cron check saves a baseline and does not report that page as new. A manual check while that baseline is still pending does not save it, and it does not say that it did. Later checks report ids that are not in the seen set. 500 ids are kept. Older ids drop off and can look new again. Real 0 hits, an empty `hits` array with `estimatedTotalHits` 0, does not clear the seen set. An empty `hits` array with a larger total is an unusable page, and seen ids are not advanced. An empty body is an incomplete search, not zero hits. The first time a non-empty page becomes 0 hits, the result says `This page has 0 hits. Seen ids were kept.` Repeats stay quiet until the page is non-empty again. The result does not say there is nothing new.

A failed search is not a change to the index. The same failure is reported on the first check, when the cause changes, again after 24 hours, and once when the search works again. Argument errors are not counted as that failure. When the cron session marker cannot be read, or Hermes isolates this plugin (`HERMES_PLUGIN_HOST_PROCESS` set to a value other than empty or `0`), `check` does not call the server, does not advance seen ids, and writes nothing. In that form a cron run and a hand-typed check look the same, so the failure clock is not moved: a check you run by hand cannot silence the next cron report. The result is reported on every check, which is every 15 minutes while the watch is scheduled, until the form is fixed. `save` still writes the local query in that form. It does not search. An unset value is normal on Hermes v0.21.4, and search and health still run.

Changing the index, query, or id field asks for a new baseline on the next successful check. A failed save does not clear seen ids. Changing only the title field keeps seen ids. The save result names the title field, or `(none)` when it is empty. An omitted title field keeps the previous one. `schedule` and `unschedule` leave the file unchanged. A stored job id from an older version is kept and named. This plugin does not call `get_job` or `remove_job`, so it does not say the job is gone and it does not say this call removed it.

Delivery is not a tool argument. The printed command puts `--deliver local` on `hermes cron create`. You can change that flag on the command you run. `cli`, `cron`, `api_server`, and `bot-chat` are not accepted as arguments of this plugin.

When the active Hermes profile is not `default`, the printed create and remove lines put `-p <name>` before the subcommand. The profile is the name `register` read from `ctx.profile_name`. If the profile name cannot be read, the result says `profile unknown` and does not print a default-profile command. This plugin does not take `-p` or `--profile` as its own argument.

## Where state is stored

`<HERMES_HOME>/plugin-data/index-search` until you delete that directory. There is no fallback to `~/.hermes`. Writes go through Hermes's write check with the destination path only, then an atomic replace. If that check cannot be loaded, nothing is written. If a cron check reads a page and the watch file cannot be written, seen ids are not advanced, and the result says the page was read but the file was not written. If `save` cannot write the file, no cron job is created, because this plugin does not create one, and the result says the file was not written. It does not say the search failed. A corrupt state file is not overwritten. The API key is not written to the state file, the URL, or the error text. The tool result also drops the key, including the form JSON uses when the key contains a quote or a backslash, so a hit id or title that contains the key is not returned.

The job prompt tells the model that a quiet check, `notify` false, must reply with exactly `[SILENT]`.

This plugin reads these Hermes names. If one cannot be loaded, raises, or no longer has that name, the watch fails closed. The profile name is not in this list. `register` reads `ctx.profile_name`.

- `plugins.plugin_storage.plugin_data_dir`
- `cron.execution_identity.current_cron_execution`
- `agent.file_safety.get_write_denied_error`

Imports are flat only when `__package__` is empty. Tests ship beside the plugin and are not loaded as the plugin.

## Server behavior that was checked

Checked on 2026-10-08 against a local Community Edition image `getmeili/meilisearch:v1.12` (package 1.12.8). That build's license is MIT. Enterprise sharding is not used. This plugin does not ship the engine. Your server is not described as MIT.

On that build, a search did not enqueue a task. An empty `q` matched every document. Displayed attributes can omit the primary key from hits. A missing key was HTTP 401. A bad key was HTTP 403. A missing index was HTTP 404. The default keys observed were two (search, and admin), not four.

The server can send anonymous telemetry until the operator turns it off. On the tested 1.12.8 image, an empty `MEILI_NO_ANALYTICS` value failed to start the process, because `--no-analytics` required a value. Setting `MEILI_NO_ANALYTICS=true` disabled anonymous telemetry. This plugin does not set that variable. If the host bills for search, those calls spend that account. A self-hosted Community Edition has no fee from this plugin.

## What was not tested

- index_watch was called through real Hermes cron runs with a stand-in model on 0.21.4 and current main. index_search and index_health were called through Hermes's tool dispatch only. No live model chose any of them in a chat.
- Meilisearch Cloud was not used. If that host bills for search, the account is the host's.
- Builds other than the image above, Enterprise binaries, a `mustRestart` health body, and the four default keys described in the current docs were not observed here.
- Attribute names are refused only when they contain `.`, `/`, or a control character. The server's full attribute grammar was not copied.
- The plugin cannot stop a cron model from calling `index_search`.
- The plugin does not turn engine telemetry off.

An empty `HERMES_HOME` scan with Hermes v0.22.2 and with origin-main is `Verdict: SAFE`, 16 findings, `Decision: ALLOWED — Allowed (community source, safe verdict)`. The findings are a test key constant, git and recall subprocess calls in the tests, and example loopback addresses in this file, the next-step text, and the tests. They stay, because the setup example and the tests need them.

`manifest_version` is 2. `requires_hermes` is `>=0.21.6`. The plugin version is 0.1.4. The license of this plugin is MIT. Dependencies are the Python standard library only.
