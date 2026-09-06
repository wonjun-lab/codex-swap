"""실측 응답의 순서를 보존하면서 파이프 경계만 흔드는 자식이다.

계정 파일을 해석하지 않는다. 실행할 시나리오와 기록 경로는 테스트가 환경으로 준다.
"""

import json
import os
import select
import signal
import sys
import time
from pathlib import Path


def main() -> None:
    assert sys.argv[1:] == ["app-server"]
    scenario = json.loads(Path(os.environ["PROBE_SCENARIO"]).read_text())
    messages = iter(scenario["messages"])
    with Path(os.environ["PROBE_EVENTS"]).open("a", buffering=1) as events:

        def record(kind: str, **fields: object) -> None:
            events.write(json.dumps({"kind": kind, "at": time.monotonic(), **fields}) + "\n")

        if scenario.get("ignore_term"):
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
        record("start", pid=os.getpid())
        # TextIO 의 미리 읽기가 조기 initialized 를 숨기면 순서 검사가 무의미해진다.
        for line in sys.stdin.buffer.raw:
            request = json.loads(line)
            record("received", message=request)
            if "id" not in request:
                continue
            request_id = request["id"]
            if request_id == scenario.get("stop_at"):
                mode = scenario["mode"]
                if mode == "close":
                    os.close(1)
                if mode in ("close", "silence"):
                    time.sleep(30)
                if mode == "exit127":
                    print("401 unauthorized", file=sys.stderr, flush=True)
                    return sys.exit(127)
                return
            if request_id == 1:
                premature = bool(select.select([sys.stdin.fileno()], [], [], 0.03)[0])
                record("before_initialize_response", premature=premature)
            time.sleep(scenario.get("delays", {}).get(str(request_id), 0))
            for message in messages:
                payload = (message + "\n").encode()
                record("sent", line=message)
                if scenario.get("fragment"):
                    for offset in range(0, len(payload), 17):
                        os.write(1, payload[offset : offset + 17])
                        time.sleep(0.0001)
                else:
                    os.write(1, payload)
                try:
                    response = json.loads(message)
                except ValueError:
                    continue
                if (
                    isinstance(response, dict)
                    and type(response.get("id")) is int
                    and response["id"] == request_id
                ):
                    break
        if scenario.get("ignore_term"):
            time.sleep(30)


if __name__ == "__main__":
    main()
