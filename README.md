## Run

Install deps (including Granian):

```bash
uv sync
```

Apply migrations:

```bash
uv run python manage.py migrate
```

Minimal automated repro (starts Granian + driver, auto-picks a free port, deletes pass artifacts):

```bash
uv run --no-sync python scripts/repro_lockup.py
```

When it locks up, you will see `ws_recv_timeout` / `http_timeout` start incrementing and the run will end with a non-zero `lockup_count`:

```text
00:48:23 INFO status lockups=0 stats={'ws_send': 442, 'ws_recv': 442, 'http_ok': 1548}
00:48:28 INFO status lockups=0 stats={'ws_send': 488, 'ws_recv': 484, 'http_ok': 1720, 'ws_recv_timeout': 2}
00:48:42 INFO run end lockups=3 elapsed_s=57.677 stats={'ws_send': 494, 'ws_recv': 484, 'http_ok': 1767, 'ws_recv_timeout': 10, 'http_timeout': 1}
{"run_id": "004738_69526", "port": 8001, "run_dir": ".../granian_channels_lock_up/scripts/repro_artifacts/run_004738_69526_p8001", "start_error": null, "lockup_count": 3, "elapsed_seconds": 57.6767558749998, "server_returncode": -9}
```

Key files to inspect:
- `app/views.py` (DRF viewset + serializer)
- `app/consumers.py` (WebSocket consumer)
- `scripts/repro_lockup.py` (minimal repro: starts server + driver, auto-deletes pass artifacts)
- `app/urls.py` (DefaultRouter wiring)
