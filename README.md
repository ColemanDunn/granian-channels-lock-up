## Run

Install deps (including Granian):

```bash
uv sync
```

Apply migrations:

```bash
uv run python manage.py migrate
```

Serve with Granian (ASGINL):

```bash
uv run granian --interface asginl config.asgi:application
```

Run the lockup test script in another terminal:

```bash
uv run python scripts/lockup_test.py
```

You should see logs like:

```text
2026-01-30 12:42:19,844 INFO users status=200
2026-01-30 12:42:19,920 INFO websocket sent {'type': 'ping', 'count': 21}
2026-01-30 12:42:20,061 INFO users status=200
2026-01-30 12:42:20,277 INFO users status=200
2026-01-30 12:42:20,496 INFO users status=200
2026-01-30 12:42:20,712 INFO users status=200
2026-01-30 12:42:20,924 INFO websocket sent {'type': 'ping', 'count': 22}
2026-01-30 12:42:21,931 INFO websocket sent {'type': 'ping', 'count': 23}
2026-01-30 12:42:22,935 INFO websocket sent {'type': 'ping', 'count': 24}
2026-01-30 12:42:23,939 INFO websocket sent {'type': 'ping', 'count': 25}
2026-01-30 12:42:24,945 INFO websocket sent {'type': 'ping', 'count': 26}
2026-01-30 12:42:25,922 ERROR timeout after 5s; server may be locked up
2026-01-30 12:42:25,949 INFO websocket sent {'type': 'ping', 'count': 27}
```

Key files to inspect:
- `app/views.py` (DRF viewset + serializer)
- `app/consumers.py` (WebSocket consumer)
- `scripts/lockup_test.py` (repro script driving WebSocket + HTTP)
- `app/urls.py` (DefaultRouter wiring)
