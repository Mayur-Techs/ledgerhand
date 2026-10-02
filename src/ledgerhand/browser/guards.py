"""Browser guards — enforce safety invariants in the browser layer.

Why: policy is enforced in code, not in a prompt. A disallowed navigation
or an unexpected dialog is caught here before it can cause harm.
"""
import posixpath
from urllib.parse import urlparse


class UnexpectedDialog(Exception):
    pass


class NavigationBlocked(Exception):
    pass


class PathGuard:
    """Blocks navigation to paths outside the allowlist (invariant I8).

    Validates BOTH the origin (scheme+host+port) AND the normalized path.
    A simple startswith("/bills") would pass "/bills-evil"; we use exact
    segment matching instead.

    Allowed paths are declared inclusive: "/bills" also allows "/bills/123".
    The admin path is never allowed, regardless of allowlist.
    """

    NEVER_ALLOWED = {"/admin"}  # hard-coded block regardless of allowlist

    def __init__(self, allowed_paths: tuple[str, ...], allowed_origin: str = ""):
        # allowed_origin: "http://localhost:8001" — scheme+host+port, no trailing slash
        self.allowed_paths = tuple(allowed_paths)
        self.allowed_origin = allowed_origin.rstrip("/")
        self.blocked_count = 0

    def is_allowed(self, url: str) -> bool:
        """Return True only if BOTH origin and path are allowed.

        Empty allowed_paths means paths are not checked (useful in tests
        that don't care about browser navigation).
        """
        parsed = urlparse(url)

        # Build the actual origin from the URL
        if self.allowed_origin:
            scheme = parsed.scheme
            netloc = parsed.netloc
            actual_origin = f"{scheme}://{netloc}"
            if actual_origin != self.allowed_origin:
                self.blocked_count += 1
                return False

        if not self.allowed_paths:
            # No path restriction configured
            return True

        # Normalise path (collapses // and ../ etc.)
        norm = posixpath.normpath(parsed.path or "/")

        # Hard-block admin paths
        for blocked in self.NEVER_ALLOWED:
            if norm == blocked or norm.startswith(blocked + "/"):
                self.blocked_count += 1
                return False

        # Check allowlist: exact match OR path is a child of an allowed prefix
        for allowed in self.allowed_paths:
            allowed_norm = posixpath.normpath(allowed)
            if norm == allowed_norm or norm.startswith(allowed_norm + "/"):
                return True

        self.blocked_count += 1
        return False

    def assert_allowed(self, url: str) -> None:
        """Raise NavigationBlocked if the URL is outside the allowlist."""
        if not self.is_allowed(url):
            raise NavigationBlocked(
                f"Navigation blocked: {url!r} is outside the allowed origin/paths"
            )


class DialogHandler:
    """Always resolves native confirm() dialogs.

    Why: with no listener, Playwright auto-dismisses (confirm() returns false).
    With an unresolved listener, the click hangs. We must always resolve.

    Accept ONLY if the dialog text matches the expected action AND amount.
    Otherwise dismiss and raise UnexpectedDialog.
    """

    def __init__(self):
        self._expected_text_fragment: str = ""
        self._expected_amount_paise: int = 0

    def expect(self, text_fragment: str, amount_paise: int) -> None:
        """Set what we expect the next confirm() to say."""
        self._expected_text_fragment = text_fragment
        self._expected_amount_paise = amount_paise

    def handle(self, dialog) -> None:
        """Called by Playwright on dialog. ALWAYS calls accept() or dismiss().

        The ERP dialog shows rupees: 'Post bill of Rs18450.00?'
        We match the fragment and the rupee amount using pure integer arithmetic
        (no float — money is integer paise everywhere).
        Any mismatch: dismiss first, then raise (so the click doesn't hang).
        """
        msg = dialog.message

        if self._expected_text_fragment and self._expected_text_fragment not in msg:
            dialog.dismiss()
            raise UnexpectedDialog(
                f"Dialog missing {self._expected_text_fragment!r}: got {msg!r}"
            )

        if self._expected_amount_paise > 0:
            # Pure integer arithmetic: paise → "rupees.paise"
            rupees = self._expected_amount_paise // 100
            paise_part = self._expected_amount_paise % 100
            fmt1 = f"{rupees}.{paise_part:02d}"   # e.g. "18450.00"
            fmt2 = str(rupees)                     # e.g. "18450" (no paise)
            if fmt1 not in msg and fmt2 not in msg:
                dialog.dismiss()
                raise UnexpectedDialog(
                    f"Dialog amount mismatch: expected {fmt1!r}, got {msg!r}"
                )

        dialog.accept()