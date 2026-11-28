"""Fake secrets in the shapes a helper's result can carry. Each is (name, text with the secret in it, the secret part).

The masked copy the ledger keeps (forced redaction) and the copy a live event carries (``security.redact_secrets``
honoured, partial mask such as ``Bearer abcdef...2345``) differ for some of them; the wake text must not.
None is a real credential.
"""

SHAPES = [
    ("openai-key", "found key sk-proj-abcdefghij1234567890ABCDEFGH in .env", "sk-proj-abcdefghij1234567890ABCDEFGH"),
    ("github-token", "token ghp_0123456789abcdefghijklmnopqrstuv012345 works", "ghp_0123456789abcdefghijklmnopqrstuv012345"),
    ("aws-key", "key AKIAIOSFODNN7EXAMPLE was used", "AKIAIOSFODNN7EXAMPLE"),
    # Built in two parts: GitHub's push protection refuses a whole Slack token literal, even a fake one.
    ("slack-token", "bot " + "xox" + "b-123456789012-1234567890123-abcdefghijklmnopqrstuvwx posted",
     "xox" + "b-123456789012-1234567890123-abcdefghijklmnopqrstuvwx"),
    ("jwt", "jwt eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkpvaG4ifQ.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c ok",
     "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkpvaG4ifQ.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"),
    ("url-credentials", "cloned https://deploy:hunter2hunter2Xy@git.example.com/repo.git", "hunter2hunter2Xy"),
    ("password-colon", "password: hunter2hunter2Xy was in the file", "hunter2hunter2Xy"),
    ("authorization-bearer", "sent Authorization: Bearer abcdef0123456789abcdef0123456789012345 to the api", "abcdef0123456789abcdef0123456789012345"),
    ("bearer-short", "used Bearer abcdef0123456789abcdef0123456789012345 once", "abcdef0123456789abcdef0123456789012345"),
    ("authorization-token", "sent Authorization: token abcdef0123456789abcdef0123456789012345 once", "abcdef0123456789abcdef0123456789012345"),
    ("x-api-key", "sent x-api-key: abcdef0123456789abcdef0123456789012345 once", "abcdef0123456789abcdef0123456789012345"),
    ("api-key-equals", "config api_key=abcdef0123456789abcdef0123456789012345 was set", "abcdef0123456789abcdef0123456789012345"),
    ("token-equals", "config token=abcdef0123456789abcdef0123456789012345 was set", "abcdef0123456789abcdef0123456789012345"),
    ("env-export", "ran export OPENAI_API_KEY=abcdef0123456789abcdef0123456789012345 first", "abcdef0123456789abcdef0123456789012345"),
    ("curl-header", "ran curl -H 'Authorization: Bearer abcdef0123456789abcdef0123456789012345' https://api.example.com",
     "abcdef0123456789abcdef0123456789012345"),
    ("json-access-token", 'got {"access_token": "abcdef0123456789abcdef0123456789012345", "expires": 3600}',
     "abcdef0123456789abcdef0123456789012345"),
]


def summary_with_every_shape() -> str:
    return " ; ".join(f"{name}: {text}" for name, text, _ in SHAPES)


def secrets_in(text: str) -> set[str]:
    """Names of the shapes whose secret part is in *text*."""
    return {name for name, _text, secret in SHAPES if secret in text}


def kept_by_the_masking_itself() -> set[str]:
    """Shapes the redactor does not mask at all, even forced (a plain ``password:`` or ``token=`` line). They reach the
    model today in every path, so they are not a difference between the live and the replayed wake."""
    from tools.delegation_redaction import redact_child_result
    return secrets_in(redact_child_result({"summary": summary_with_every_shape()})["summary"])
