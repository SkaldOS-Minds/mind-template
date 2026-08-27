#!/usr/bin/env python3
"""A git credential helper that asks the Mind Hub for a fresh token.

Design section 8.4, the known friction and its handling: installation access
tokens live one hour, and a clone that outlives one is the normal case, not the
edge case. Without this file an agent hits an auth failure, has to recognise it
as an expiry rather than a permission problem, call the hub again, and rewrite
its remote. With it, git asks, the helper fetches, and nobody notices.

The thing to understand about a credential helper: git does not pass arguments
for the credential it wants. It writes `protocol=`, `host=` and `path=` lines to
stdin, then a blank line, and reads back `username=` and `password=`. That is
the whole protocol, and it is why this script reads stdin before it does
anything.

    Install once, inside a clone:

        git config credential.https://github.com.helper \\
            "$PWD/scripts/git-credential-mind-hub.py --mind <slug>"

    Configure it, either in the environment or in .mind-hub.json beside it:

        MIND_HUB_URL    https://<the hub>            (required)
        MIND_HUB_TOKEN  the hub bearer token         (required)
        MIND_SLUG       the mind, if --mind is absent

    Check it without git:

        printf 'protocol=https\\nhost=github.com\\n\\n' | \\
            scripts/git-credential-mind-hub.py --mind <slug>

Stdlib only, like every other script in a mind: a mind must run anywhere
`python3` exists, with nothing installed.

The token is cached under the user's cache directory, mode 0600, keyed by hub
and mind, and used until two minutes before it expires. The cache is a
convenience, not a store: delete it and the next call refetches. `git credential
reject` (the `erase` verb) drops it, which is what makes a revoked installation
take effect on the next fetch instead of an hour later.
"""

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

CONFIG_FILENAMES = (".mind-hub.json", ".mind-hub")
EXPIRY_MARGIN_SECONDS = 120
TIMEOUT_SECONDS = 20


def log(message):
    """Helpers must keep stdout for git. Everything else goes to stderr."""
    print(f"git-credential-mind-hub: {message}", file=sys.stderr)


# --- configuration --------------------------------------------------------


def repo_root(start):
    path = Path(start).resolve()
    for candidate in (path, *path.parents):
        if (candidate / ".git").exists():
            return candidate
    return path


def load_config(args):
    """Environment wins over the file, so a shared clone can carry a hub URL in
    git while each operator keeps their own token in their own environment."""
    config = {}
    root = repo_root(args.repo or Path.cwd())
    for name in CONFIG_FILENAMES:
        candidate = root / name
        if candidate.is_file():
            try:
                config.update(json.loads(candidate.read_text(encoding="utf-8")))
            except (OSError, ValueError) as exc:
                log(f"ignoring {candidate}: {exc}")
            break

    hub_url = os.environ.get("MIND_HUB_URL") or config.get("hub_url")
    token = os.environ.get("MIND_HUB_TOKEN") or config.get("token")
    mind = args.mind or os.environ.get("MIND_SLUG") or config.get("mind")

    if not mind:
        # A clone in mind-wiersholm is the mind wiersholm, the same rule
        # assert.py uses for the Edda header. One less thing to configure.
        name = root.name
        mind = name[5:] if name.startswith("mind-") else name

    return {"hub_url": (hub_url or "").rstrip("/"), "token": token, "mind": mind}


# --- cache ----------------------------------------------------------------


def cache_path(config):
    base = os.environ.get("XDG_CACHE_HOME")
    root = Path(base) if base else Path.home() / ".cache"
    directory = root / "skaldos-mind"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    host = config["hub_url"].replace("://", "-").replace("/", "-").replace(":", "-")
    return directory / f"{host}.{config['mind']}.json"


def read_cache(config):
    path = cache_path(config)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not data.get("token") or not data.get("username"):
        return None
    if float(data.get("expires_epoch", 0)) - EXPIRY_MARGIN_SECONDS <= time.time():
        return None
    return data


def write_cache(config, payload):
    path = cache_path(config)
    tmp = path.with_suffix(".tmp")
    # Create with the right mode from the start: a token must never exist on
    # disk world-readable, not even between two syscalls.
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(payload, handle)
    os.replace(tmp, path)


def drop_cache(config):
    try:
        cache_path(config).unlink()
    except OSError:
        pass


# --- the hub call ---------------------------------------------------------


def parse_expiry(value):
    if not value:
        return time.time() + 3600
    text = str(value).replace("Z", "+00:00")
    try:
        import datetime

        return datetime.datetime.fromisoformat(text).timestamp()
    except ValueError:
        return time.time() + 3600


def fetch_token(config):
    """POST /api/v1/minds/{mind}/workspace, exactly as any other API client."""
    url = f"{config['hub_url']}/api/v1/minds/{config['mind']}/workspace"
    body = json.dumps({"reason": "git credential helper"}).encode("utf-8")
    request = urllib.request.Request(url, data=body, method="POST")
    request.add_header("Authorization", f"Bearer {config['token']}")
    request.add_header("Content-Type", "application/json")
    request.add_header("Accept", "application/json")
    request.add_header("User-Agent", "git-credential-mind-hub/1")

    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = json.loads(exc.read().decode("utf-8"))["error"]["message"]
        except Exception:  # noqa: BLE001 - the status code is the real signal
            detail = exc.reason
        log(f"hub refused the mint ({exc.code}): {detail}")
        return None
    except urllib.error.URLError as exc:
        log(f"hub unreachable at {config['hub_url']}: {exc.reason}")
        return None

    token = payload.get("token")
    if not token:
        log("hub returned no token")
        return None
    return {
        "username": payload.get("username") or "x-access-token",
        "token": token,
        "expires_epoch": parse_expiry(payload.get("expires_at")),
        "repository": payload.get("repository"),
    }


# --- the git protocol -----------------------------------------------------


def read_request(stream):
    fields = {}
    for line in stream:
        line = line.strip()
        if not line:
            break
        key, _, value = line.partition("=")
        if key:
            fields[key] = value
    return fields


def main():
    parser = argparse.ArgumentParser(
        description="Fetch a fresh Mind Hub workspace token for git.",
        epilog="git invokes this with one of: get, store, erase.",
    )
    parser.add_argument("operation", nargs="?", default="get")
    parser.add_argument("--mind", help="the mind slug; defaults to the clone's name")
    parser.add_argument("--repo", help="path inside the clone (default: cwd)")
    args = parser.parse_args()

    config = load_config(args)

    if args.operation == "store":
        # git offers to remember a credential it just used. The hub is the
        # store, and it is a better one, so decline silently.
        read_request(sys.stdin)
        return 0

    if args.operation == "erase":
        # git only erases after a rejection, which means the token is dead.
        read_request(sys.stdin)
        drop_cache(config)
        return 0

    if args.operation != "get":
        log(f"unknown operation {args.operation!r}")
        return 0  # A helper that fails loudly just breaks git. Defer instead.

    read_request(sys.stdin)

    if not config["hub_url"] or not config["token"]:
        # Say nothing on stdout: git falls through to the next helper or to
        # prompting, which is the correct behaviour for an unconfigured helper.
        log(
            "not configured; set MIND_HUB_URL and MIND_HUB_TOKEN, or write "
            ".mind-hub.json in the clone"
        )
        return 0

    credential = read_cache(config)
    if credential is None:
        credential = fetch_token(config)
        if credential is None:
            return 0
        write_cache(config, credential)

    sys.stdout.write(f"username={credential['username']}\n")
    sys.stdout.write(f"password={credential['token']}\n")
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
