import json
import logging
import threading
import time

import requests
import websocket


RESET = "\033[0m"
COLORS = {
    "main": "\033[36m",
    "ws": "\033[35m",
}


class ThreadColorFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        color = COLORS.get(record.threadName, "")
        formatted = super().format(record)
        return f"{color}{formatted}{RESET if color else ''}"


handler = logging.StreamHandler()
handler.setFormatter(ThreadColorFormatter("%(asctime)s %(levelname)s %(message)s"))
logging.basicConfig(level=logging.INFO, handlers=[handler])


WS_URL = "ws://127.0.0.1:8000/ws/echo/"
HTTP_URL = "http://127.0.0.1:8000/api/users/"
HTTP_TIMEOUT_SECONDS = 5
SEND_INTERVAL_SECONDS = 1


def websocket_worker(stop_event: threading.Event) -> None:
    logging.info("websocket connecting to %s", WS_URL)
    ws = websocket.WebSocket()
    ws.connect(WS_URL, timeout=HTTP_TIMEOUT_SECONDS)
    try:
        counter = 0
        while not stop_event.is_set():
            payload = {"type": "ping", "count": counter}
            ws.send(json.dumps(payload))
            logging.info("websocket sent %s", payload)
            counter += 1
            time.sleep(SEND_INTERVAL_SECONDS)
    finally:
        logging.info("websocket closing")
        ws.close()


def main() -> None:
    stop_event = threading.Event()
    thread = threading.Thread(
        target=websocket_worker,
        name="ws",
        args=(stop_event,),
        daemon=True,
    )
    thread.start()

    try:
        while True:
            try:
                response = requests.get(HTTP_URL, timeout=HTTP_TIMEOUT_SECONDS)
                response.raise_for_status()
                logging.info("users status=%s", response.status_code)
            except requests.Timeout:
                logging.error("timeout after %ss; server may be locked up", HTTP_TIMEOUT_SECONDS)
            except requests.RequestException as exc:
                logging.error("http error: %s", exc)
            time.sleep(0.2)
    except KeyboardInterrupt:
        logging.info("shutting down")
    finally:
        stop_event.set()
        thread.join(timeout=2)


if __name__ == "__main__":
    main()
