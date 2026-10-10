"""Hermes tools for one search index and one saved watch. Tests are not imported."""


def _load_service():
    # Hermes loads this file as a package and sets __path__. Flat import is only
    # for a loader that left __package__ empty.
    if __package__:
        from . import service as svc
        return svc
    import service as svc
    return svc


def register(ctx) -> None:
    service = _load_service()

    def _search(args, **_kwargs):
        return service.index_search(service.live_runtime(), args or {})

    def _health(args, **_kwargs):
        return service.index_health(service.live_runtime(), args or {})

    def _watch(args, **_kwargs):
        return service.index_watch(service.live_runtime(), args or {})

    ctx.register_tool(
        name="index_search",
        toolset=service.TOOLSET,
        schema={
            "name": "index_search",
            "description": (
                "Search one index once. Returns the id, an optional short title, and estimatedTotalHits. "
                "Only the first 20 hits are returned. Does not advance seen ids, including when called from cron. Does not ask for approval."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "index": {"type": "string", "description": "Index uid. Letters, digits, hyphens, and underscores, 1 to 64 characters."},
                    "q": {"type": "string", "description": "Non-empty query, at most 400 characters. An empty query is refused before HTTP."},
                    "id_field": {"type": "string", "description": "Hit field used as the id. You name it, because displayed attributes can hide the primary key."},
                    "title_field": {"type": "string", "description": "Optional hit field copied as a title of at most 200 characters."},
                },
                "required": ["index", "q", "id_field"],
            },
        },
        handler=_search,
    )
    ctx.register_tool(
        name="index_health",
        toolset=service.TOOLSET,
        schema={
            "name": "index_health",
            "description": "GET /health on MEILI_HOST with no API key. Does not advance seen ids and does not ask for approval.",
            "parameters": {"type": "object", "properties": {}},
        },
        handler=_health,
    )
    ctx.register_tool(
        name="index_watch",
        toolset=service.TOOLSET,
        schema={
            "name": "index_watch",
            "description": (
                "Watch one saved query. schedule and unschedule ask for approval every time. "
                "check reads the saved watch. A cron run with no arguments is check. "
                "Seen ids advance only when check is a proven cron run and this plugin is not isolated."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["schedule", "unschedule", "status", "check"]},
                    "index": {"type": "string"},
                    "q": {"type": "string"},
                    "id_field": {"type": "string"},
                    "title_field": {"type": "string"},
                    "deliver": {"type": "string", "description": "origin, local, all, a known platform, or platform:chat_id."},
                },
            },
        },
        handler=_watch,
    )
