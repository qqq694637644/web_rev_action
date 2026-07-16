"""Repository-local wrapper for the packaged dead-code audit."""

from skill_temple.dead_code_audit import audit_repository, main

__all__ = ["audit_repository", "main"]


if __name__ == "__main__":  # pragma: no cover
    main()
