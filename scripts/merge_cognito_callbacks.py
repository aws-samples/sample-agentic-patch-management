#!/usr/bin/env python3
"""Build an UpdateUserPoolClient request that adds lowercase ALB callback URLs.

UpdateUserPoolClient resets every field it isn't given, so the request carries
the client's current settings unchanged and only adds missing URLs.

Usage:
    merge_cognito_callbacks.py <describe-client.json> <cli-skeleton.json> <alb-dns>

Prints the request JSON and exits 0, or prints nothing and exits 3 when every
URL is already present.
"""

import json
import sys


def merge(client: dict, accepted: set, alb_dns: str) -> dict:
    """Return the update request, or an empty dict when no URL is missing.

    Args:
        client: The UserPoolClient object from DescribeUserPoolClient.
        accepted: Request keys the installed AWS CLI accepts.
        alb_dns: ALB DNS name as CloudFormation reports it (mixed case).

    Returns:
        Request dict for UpdateUserPoolClient, or {} if nothing to change.
    """
    hosts = [alb_dns, alb_dns.lower()]
    want_callbacks = [f"https://{h}/oauth2/idpresponse" for h in hosts]
    want_logouts = [u for h in hosts for u in (f"https://{h}", f"https://{h}/signed-out")]

    callbacks = list(client.get("CallbackURLs", []))
    logouts = list(client.get("LogoutURLs", []))
    missing = [u for u in want_callbacks if u not in callbacks] + [u for u in want_logouts if u not in logouts]
    if not missing:
        return {}

    request = {k: v for k, v in client.items() if k in accepted}
    request["CallbackURLs"] = callbacks + [u for u in want_callbacks if u not in callbacks]
    request["LogoutURLs"] = logouts + [u for u in want_logouts if u not in logouts]
    return request


def main() -> int:
    client = json.load(open(sys.argv[1]))["UserPoolClient"]
    accepted = set(json.load(open(sys.argv[2])))
    request = merge(client, accepted, sys.argv[3])
    if not request:
        return 3
    json.dump(request, sys.stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main())
