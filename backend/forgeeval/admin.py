"""Operator CLI for bootstrapping tenants and managing API keys.

    python -m forgeeval.admin bootstrap --org acme --project support-bot
    python -m forgeeval.admin create-key --org acme --project support-bot --scope traces:write
    python -m forgeeval.admin revoke-key <prefix>

The plaintext key is printed once and never stored.
"""

import argparse
import sys
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from forgeeval.auth import ALL_SCOPES, generate_key
from forgeeval.db.models import ApiKey, Organization, Project
from forgeeval.db.session import SessionLocal


def get_or_create_project(db: Session, org_slug: str, project_slug: str) -> Project:
    org = db.scalar(select(Organization).where(Organization.slug == org_slug))
    if org is None:
        org = Organization(name=org_slug, slug=org_slug)
        db.add(org)
        db.flush()
    project = db.scalar(
        select(Project).where(Project.org_id == org.id, Project.slug == project_slug)
    )
    if project is None:
        project = Project(org_id=org.id, name=project_slug, slug=project_slug)
        db.add(project)
        db.flush()
    return project


def create_key(db: Session, project: Project, scopes: list[str]) -> str:
    unknown = set(scopes) - set(ALL_SCOPES)
    if unknown:
        raise ValueError(f"unknown scopes: {sorted(unknown)}; valid: {ALL_SCOPES}")
    key = generate_key()
    db.add(ApiKey(project_id=project.id, key_prefix=key.prefix, key_hash=key.hash, scopes=scopes))
    db.flush()
    return key.plaintext


def revoke_key(db: Session, prefix: str) -> bool:
    key = db.scalar(select(ApiKey).where(ApiKey.key_prefix == prefix))
    if key is None:
        return False
    key.revoked_at = key.revoked_at or datetime.now(UTC)
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="forgeeval.admin")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("bootstrap", "create-key"):
        p = sub.add_parser(name)
        p.add_argument("--org", required=True)
        p.add_argument("--project", required=True)
        p.add_argument("--scope", action="append", dest="scopes", help="repeatable; default all")
    revoke = sub.add_parser("revoke-key")
    revoke.add_argument("prefix")
    args = parser.parse_args(argv)

    with SessionLocal() as db:
        if args.command == "revoke-key":
            if not revoke_key(db, args.prefix):
                print(f"No key with prefix {args.prefix}", file=sys.stderr)
                return 1
            db.commit()
            print(f"Revoked key {args.prefix}")
            return 0

        project = get_or_create_project(db, args.org, args.project)
        plaintext = create_key(db, project, args.scopes or ALL_SCOPES)
        db.commit()
        print(f"project_id: {project.id}")
        print(f"api_key:    {plaintext}")
        print("Store this key now; it cannot be shown again.", file=sys.stderr)
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
