#!/usr/bin/env python3
"""Minimal local client: python examples/app_server_client.py /workspace 'Explain this project'."""

import argparse
import asyncio
import json
import sys
import uuid


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("workspace")
    parser.add_argument("question")
    args = parser.parse_args()
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "capslock",
        "--workspace",
        args.workspace,
        "app-server",
        "--stdio",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
    )
    sequence = 0

    async def send(method, **params):
        nonlocal sequence
        sequence += 1
        process.stdin.write(
            (
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": sequence,
                        "method": method,
                        "params": params,
                    }
                )
                + "\n"
            ).encode()
        )
        await process.stdin.drain()
        return sequence

    async def receive(identifier):
        while raw := await process.stdout.readline():
            message = json.loads(raw)
            if message.get("id") == identifier:
                if "error" in message:
                    raise RuntimeError(message["error"])
                return message["result"]
            print(json.dumps(message, ensure_ascii=False))
        raise RuntimeError("server disconnected")

    try:
        await receive(await send("initialize", protocol_version=1))
        session = await receive(await send("session/create"))
        sid = session["session_id"]
        await receive(await send("events/subscribe", session_id=sid))
        await receive(
            await send(
                "run/start",
                session_id=sid,
                request_id=uuid.uuid4().hex,
                question=args.question,
            )
        )
        while raw := await process.stdout.readline():
            message = json.loads(raw)
            print(json.dumps(message, ensure_ascii=False))
            # This example never grants an approval implicitly. Closing the pipe
            # cancels an active approval; a UI can instead send approval/answer.
            if message.get("method") == "approval/request" or message.get(
                "params", {}
            ).get("terminal"):
                break
    finally:
        process.stdin.close()
        await process.wait()


if __name__ == "__main__":
    asyncio.run(main())
