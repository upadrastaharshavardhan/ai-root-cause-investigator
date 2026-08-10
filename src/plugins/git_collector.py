"""Git commit and change collector."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from src.config.settings import get_settings
from src.models.schemas import (
    ChangedFile,
    CommitInfo,
    EvidenceBundle,
    EvidenceItem,
    EvidenceType,
    InvestigationContext,
)
from src.plugins.base import BaseCollector
from src.utils.logging import get_logger

logger = get_logger(__name__)


class GitCollector(BaseCollector):
    name = "git"
    priority = 20

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        super().__init__(config)
        settings = get_settings()
        self.repo_url = (config or {}).get("repo_url") or settings.git_repo_url
        self.token = (config or {}).get("token") or (
            settings.git_token.get_secret_value() if settings.git_token else None
        )
        self.local_path = (config or {}).get("local_path") or "./data/repo_cache"
        self.max_lookback = (config or {}).get("max_lookback", 20)

    async def collect(self, context: InvestigationContext) -> EvidenceBundle:
        items: list[EvidenceItem] = []
        commits: list[CommitInfo] = []

        try:
            from git import Repo
        except ImportError:
            return EvidenceBundle(
                collector=self.name,
                items=[],
                errors=["GitPython not installed"],
            )

        path = Path(self.local_path)
        try:
            if path.exists() and (path / ".git").exists():
                repo = Repo(str(path))
                repo.remotes.origin.fetch()
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                # Shallow clone for speed
                auth_url = self.repo_url
                if self.token and "://" in self.repo_url:
                    proto, rest = self.repo_url.split("://", 1)
                    auth_url = f"{proto}://x-access-token:{self.token}@{rest}"
                repo = Repo.clone_from(auth_url, str(path), depth=50)
        except Exception as exc:  # noqa: BLE001
            logger.exception("git_clone_failed", error=str(exc))
            return EvidenceBundle(
                collector=self.name,
                items=[],
                errors=[f"Git access failed: {exc}"],
            )

        head = context.pipeline.commit_sha or "HEAD"
        try:
            commit = repo.commit(head)
        except Exception:
            commit = repo.head.commit

        lookback = 0
        current = commit
        while current and lookback < self.max_lookback:
            files: list[ChangedFile] = []
            try:
                if current.parents:
                    diffs = current.parents[0].diff(current, create_patch=True)
                    for d in diffs:
                        files.append(
                            ChangedFile(
                                path=d.b_path or d.a_path or "unknown",
                                change_type=d.change_type,
                                additions=getattr(d, "additions", 0) or 0,
                                deletions=getattr(d, "deletions", 0) or 0,
                                patch=d.diff.decode("utf-8", errors="replace")[:4000]
                                if d.diff
                                else None,
                            )
                        )
            except Exception as exc:  # noqa: BLE001
                logger.debug("diff_failed", sha=current.hexsha, error=str(exc))

            info = CommitInfo(
                sha=current.hexsha,
                short_sha=current.hexsha[:8],
                message=current.message.strip().split("\n")[0][:200],
                author=str(current.author.name),
                author_email=str(current.author.email),
                committed_at=datetime.utcfromtimestamp(current.committed_date),
                files=files,
            )
            commits.append(info)
            items.append(
                EvidenceItem(
                    type=EvidenceType.GIT_COMMIT,
                    source=self.name,
                    title=f"Commit {info.short_sha}: {info.message}",
                    content=info.model_dump(mode="json"),
                    timestamp=info.committed_at,
                    related_entities=[f.path for f in files] + [info.author],
                )
            )
            lookback += 1
            current = current.parents[0] if current.parents else None

        context.recent_commits = commits
        return EvidenceBundle(collector=self.name, items=items)
