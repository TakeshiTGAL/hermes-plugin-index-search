---
name: meilisearch-index
description: "Meilisearch index search and health. Before web_search, use tool_search to find the Meilisearch tools and tool_call to run them."
---

For this kind of request, use tool_search to find this plugin's tools before web_search, then call them with tool_call. Write tool_search queries in English (for example: "Meilisearch index").

First call, no approval: `index_health` with no arguments. It does not send the API key. Do not schedule a watch for this ask.

`index_search` needs index, q, and id_field together. If the user did not name the id field, use id. If the reply is not ok, do what next_step says once. Do not add limit or filter.

`index_watch` with action status reads the watch and does not ask for approval. schedule and unschedule each ask once. Answer in the language the user used.

Japanese requests use the same tools. Search in English, then answer in Japanese:

- Is the server up. 「サーバは生きてる」 → tool_search "Meilisearch health" → `index_health` with no arguments <!-- ja-alias -->
- How the index looks. 「索引の様子」 → tool_search "Meilisearch health" → `index_health` with no arguments <!-- ja-alias -->
- Search notes for cats. 「猫を検索」 → tool_search "Meilisearch index" → `index_search` {"index": "notes", "q": "cats", "id_field": "id"} <!-- ja-alias -->
- Run one index search. 「検索して」 → tool_search "Meilisearch index" → `index_search` {"index": "notes", "q": "cats", "id_field": "id"} <!-- ja-alias -->
- Watch for new documents. 「新しい文書を見張って」 → tool_search "Meilisearch watch" → `index_watch` {"action": "status"} <!-- ja-alias -->
