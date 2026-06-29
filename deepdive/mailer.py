"""Send the rendered newsletter via Resend."""

from __future__ import annotations

import resend


def send(
    *, api_key: str, sender: str, recipient: str, subject: str, html: str
) -> str:
    """Send one email and return the Resend message id."""
    resend.api_key = api_key
    result = resend.Emails.send(
        {
            "from": sender,
            "to": [recipient],
            "subject": subject,
            "html": html,
        }
    )
    # The SDK returns a dict-like with an "id" on success.
    return result.get("id", "") if isinstance(result, dict) else str(result)
