import json
import sys
import time

name = sys.argv[1]
print(json.dumps({"ready": True}), flush=True)
for line in sys.stdin:
    request = json.loads(line)
    if request.get("input") == ["wait"]:
        time.sleep(0.2)
    result = (
        {"vectors": [[0.1, 0.2] for _ in request["input"]], "tokens": 2}
        if name == "embeddings"
        else {
            "text": "hello",
            "duration": 1.0,
            "language": "en",
            "segments": [{"id": 0, "text": "hello", "start": 0, "end": 1}],
            "words": [{"word": "hello", "start": 0, "end": 1}],
        }
    )
    print(json.dumps({"result": result}), flush=True)
