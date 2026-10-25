# Changelog

## 0.1.3

The manifest lists the pre_tool_call hook that schedule and unschedule already register. Hermes shows that one approve line. Search, health, status, and check still return nothing from the hook. The README states version 0.1.3.

## 0.1.2

The README opens with the four steps of the first request: install, `MEILI_HOST`, a server that is already running, and `index_health` with no arguments. That call does not need the API key and does not ask for approval. The skill turns a Japanese request into an English tool_search query. A search that is missing a field names `index probes`, `q hermes`, and `id_field sku`. A missing host names `index_health`. A bad watch action names `action status`, which does not ask for approval. The README states version 0.1.2.

The first call is index_health with no arguments. 日本語: 最初は引数なしの index_health。失敗した返事に、次の1手を英語の後に日本語で書く。

## 0.1.1

Adds the `meilisearch-index` skill. It tells the model to find the Meilisearch tools with tool_search before web_search. On Hermes 0.21.6 and later, while the plugin is enabled, it adds one line to the skills index of every session. It does not set requires_toolsets, so it stays listed when Tool Search is left at its default, which hides this plugin's tools, and also on a surface where the tools were turned off with hermes tools. On Hermes 0.21.4 and 0.21.5 it is not listed and loads only by its full name.

index_search reads fullwidth digits in its arguments, such as an index uid or the query, as ASCII digits, and trims surrounding spaces. index_watch does not; it still refuses a fullwidth index uid. Uppercase is sent unchanged because it changes the meaning. Extra arguments, a missing id_field, 10.0, and a search string that is too long are refused with a fix and an example.

Each tool description now opens with Meilisearch and what the call does. Under Tool Search's default, the first sentence is the part of the description the model sees before it searches. The index_health description is written in full where the tool is registered.

The README describes the skill, where it is listed, and how to remove it, and states version 0.1.1. plugin.yaml carries the same description as the catalog entry.
