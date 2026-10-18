"""Fresh isolated fixture instance, using the normal Redis adapter. No Docker.

    python -m services.page.fake.run

All stream data and decisions are temporary and disappear when this process stops.
"""
import argparse
import os
from pathlib import Path
import random
import subprocess
import sys
from threading import Thread


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--port", type=int, default=8501)
    args = p.parse_args()
    from fakeredis import TcpFakeServer
    import redis
    from services.page.fake import feed
    from services.casewriter.casewriter import push_fake

    root = Path(__file__).resolve().parents[3]
    server = TcpFakeServer(("127.0.0.1", 0), server_type="redis")
    server.daemon_threads = True
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    client = redis.Redis(host="127.0.0.1", port=server.server_address[1], decode_responses=True, protocol=2)
    child = None
    try:
        rings = feed.load_rings(feed.RINGS_FILE)
        client.set("page:feed_mode", "fixture")
        push_fake(feed.RINGS_FILE, client)
        feed.push_cases(client, rings)
        feed.push_scored(client, random.Random(7), 300)
        env = dict(os.environ, REDIS_HOST="127.0.0.1", REDIS_PORT=str(server.server_address[1]))
        print("Fixture simulation: http://127.0.0.1:%d — decisions are temporary." % args.port, flush=True)
        child = subprocess.Popen([sys.executable, "-m", "streamlit", "run", "services/page/app.py",
                                  "--server.address", "127.0.0.1", "--server.port", str(args.port),
                                  "--server.headless", "true", "--client.toolbarMode", "minimal",
                                  "--browser.gatherUsageStats", "false"], cwd=root, env=env)
        return child.wait()
    except KeyboardInterrupt:
        return 0
    finally:
        if child and child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
        client.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


if __name__ == "__main__":
    raise SystemExit(main())
