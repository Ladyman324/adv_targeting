"""Trusted HTTP child. No model tools; input arrives only after containment."""
import json
import sys


def request(body, token):
    import httpx
    with httpx.Client(timeout=45, follow_redirects=False, trust_env=False) as client:
        with client.stream("POST", "https://api.openai.com/v1/responses", json=body,
                           headers={"Authorization": "Bearer " + token}) as response:
            if response.status_code != 200:
                return {"error": "HTTP_STATUS", "httpStatus": response.status_code}
            chunks, size = [], 0
            for chunk in response.iter_bytes():
                size += len(chunk)
                if size > 500000:
                    return {"error": "RESPONSE_TOO_LARGE"}
                chunks.append(chunk)
            return {"response": json.loads(b"".join(chunks))}


def main():
    try:
        # Parent bounds input and launches this fixed, trusted implementation.
        payload = json.loads(sys.stdin.buffer.read(250001))
        result = request(payload["body"], payload["token"])
    except Exception:
        # Never propagate exception text, headers, response bodies or tokens.
        result = {"error": "TRANSPORT_FAILED"}
    sys.stdout.write(json.dumps(result))


if __name__ == "__main__":
    main()
