# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 The Linux Foundation

"""Scanner for identifying pull requests across GitHub organizations.

This module provides functionality for scanning GitHub organizations to find
pull requests that need processing. It uses GraphQL for efficient querying
and supports parallel processing with bounded concurrency.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from .github_client import GitHubClient
    from .progress_tracker import ProgressTracker

from .graphql_queries import (
    ORG_REPOS_ONLY,
    ORG_REPOS_WITH_PRS,
    REPO_OPEN_PRS_PAGE,
)

# GitHub API tuning defaults - optimized for performance and rate limit compliance
# These match dependamerge's proven values
DEFAULT_PRS_PAGE_SIZE = 30  # Pull requests per GraphQL page
DEFAULT_FILES_PAGE_SIZE = 50  # Files per pull request
DEFAULT_COMMENTS_PAGE_SIZE = 10  # Comments per pull request
DEFAULT_CONTEXTS_PAGE_SIZE = 20  # Status contexts per pull request


class PRScanner:
    """Scanner for finding pull requests across GitHub organizations.

    This scanner uses GraphQL to efficiently query GitHub organizations for
    pull requests. It supports:
    - Parallel processing with bounded concurrency
    - Progress tracking for UI updates
    - Filtering by various criteria (draft status, etc.)
    - Pagination for large result sets
    """

    def __init__(
        self,
        client: GitHubClient,
        progress_tracker: ProgressTracker | None = None,
        max_repo_tasks: int = 8,
        max_page_tasks: int = 16,
    ):
        """Initialize PR scanner.

        Args:
            client: GitHub API client
            progress_tracker: Optional progress tracker for UI updates
            max_repo_tasks: Maximum concurrent repository scans
            max_page_tasks: Maximum concurrent page fetches
        """
        self.client = client
        self.progress_tracker = progress_tracker
        self.max_repo_tasks = max_repo_tasks
        self.max_page_tasks = max_page_tasks
        self.repo_semaphore = asyncio.Semaphore(max_repo_tasks)
        self.page_semaphore = asyncio.Semaphore(max_page_tasks)
        self.logger = logging.getLogger("pull_request_fixer.pr_scanner")

    async def scan_organization(
        self,
        organization: str,
        include_drafts: bool = False,
        blocked_only: bool = False,
    ) -> AsyncIterator[tuple[str, str, dict[str, Any]]]:
        """Scan an organization for pull requests.

        This method yields pull requests as they are discovered, allowing
        for streaming processing without loading all results into memory.

        Args:
            organization: Organization name to scan
            include_drafts: Whether to include draft PRs
            blocked_only: Whether to only yield blocked/unmergeable PRs

        Yields:
            Tuples of (owner, repo_name, pr_data) where pr_data is a dict
            containing the PR information from the GitHub API
        """
        self.logger.debug(f"Starting scan of organization: {organization}")

        # First pass: count repositories for progress tracking
        total_repos = await self._count_org_repositories(organization)
        if self.progress_tracker and total_repos > 0:
            self.progress_tracker.update_total_repositories(total_repos)
            # Start the progress tracker now that we have the count
            self.progress_tracker.start()

        self.logger.debug(f"Found {total_repos} repositories in {organization}")

        # If no repos found or error occurred, nothing to scan
        if total_repos == 0:
            self.logger.warning(
                f"No repositories found in organization: {organization}"
            )
            return

        # Second pass: scan repositories with bounded parallelism
        async for owner, repo_name, pr_data in self._scan_repositories(
            organization, include_drafts, blocked_only
        ):
            yield owner, repo_name, pr_data

    async def _count_org_repositories(self, organization: str) -> int:
        """Count total repositories in an organization.

        Args:
            organization: Organization name

        Returns:
            Total number of repositories
        """
        try:
            result = await self.client.graphql(
                ORG_REPOS_ONLY,
                variables={"org": organization, "reposCursor": None},
            )

            # Check if organization data exists
            org_data = result.get("organization")
            if org_data is None:
                self.logger.error(
                    f"Organization '{organization}' not found or token lacks access.\n"
                    f"  Please verify:\n"
                    f"  1. Organization name is correct (case-sensitive)\n"
                    f"  2. Token has 'read:org' scope\n"
                    f"  3. Token user has access to the organization\n"
                    f"  4. Organization exists on GitHub"
                )
                return 0

            repos = org_data.get("repositories", {})
            total: int = repos.get("totalCount", 0)
            self.logger.debug(
                f"Organization {organization} has {total} repositories"
            )
            return total
        except Exception as e:
            self.logger.error(f"Error counting repositories: {e}")
            import traceback

            self.logger.debug(f"Traceback: {traceback.format_exc()}")
            return 0

    async def _scan_repositories(
        self,
        organization: str,
        include_drafts: bool,
        blocked_only: bool,
    ) -> AsyncIterator[tuple[str, str, dict[str, Any]]]:
        """Scan repositories in an organization for PRs.

        Args:
            organization: Organization name
            include_drafts: Whether to include draft PRs
            blocked_only: Whether to only yield blocked/unmergeable PRs

        Yields:
            Tuples of (owner, repo_name, pr_data)
        """
        # Create a queue for PR results
        pr_queue: asyncio.Queue[tuple[str, str, dict[str, Any]] | None] = (
            asyncio.Queue()
        )

        async def process_repository(repo_node: dict[str, Any]) -> None:
            """Process a single repository and add PRs to queue."""
            async with self.repo_semaphore:
                repo_full_name = repo_node.get(
                    "nameWithOwner", "unknown/unknown"
                )
                if self.progress_tracker:
                    self.progress_tracker.start_repository(repo_full_name)

                try:
                    owner, repo_name = self._split_owner_repo(repo_full_name)
                    pr_count = 0
                    blocked_count = 0

                    # Fetch first page of PRs
                    (
                        first_nodes,
                        page_info,
                    ) = await self._fetch_repo_prs_first_page(owner, repo_name)

                    # Process first page PRs
                    for pr_node in first_nodes:
                        if self._should_include_pr(pr_node, include_drafts):
                            pr_count += 1
                            # Check if PR is blocked (only when filtering is enabled)
                            if blocked_only:
                                is_blocked, _ = self.is_pr_blocked(pr_node)
                                if is_blocked:
                                    blocked_count += 1
                                    await pr_queue.put(
                                        (owner, repo_name, pr_node)
                                    )
                            else:
                                # Not filtering, yield all PRs
                                await pr_queue.put((owner, repo_name, pr_node))

                    # Process additional pages if present
                    has_next = page_info.get("hasNextPage", False)
                    end_cursor = page_info.get("endCursor")

                    if has_next and end_cursor:
                        async for pr_node in self._iter_repo_open_prs_pages(
                            owner, repo_name, end_cursor
                        ):
                            if self._should_include_pr(pr_node, include_drafts):
                                pr_count += 1
                                # Check if PR is blocked (only when filtering is enabled)
                                if blocked_only:
                                    is_blocked, _ = self.is_pr_blocked(pr_node)
                                    if is_blocked:
                                        blocked_count += 1
                                        await pr_queue.put(
                                            (owner, repo_name, pr_node)
                                        )
                                else:
                                    # Not filtering, yield all PRs
                                    await pr_queue.put(
                                        (owner, repo_name, pr_node)
                                    )

                    if self.progress_tracker:
                        # Only report blocked count when filtering to blocked PRs
                        # Otherwise report 0 to avoid misleading progress display
                        if blocked_only:
                            self.progress_tracker.complete_repository(
                                blocked_count
                            )
                        else:
                            self.progress_tracker.complete_repository(0)

                    self.logger.debug(
                        f"Repository {repo_full_name}: found {pr_count} PRs ({blocked_count} blocked)"
                    )

                except Exception as e:
                    self.logger.error(
                        f"Error scanning repository {repo_full_name}: {e}"
                    )
                    if self.progress_tracker:
                        self.progress_tracker.add_error()

        async def producer() -> None:
            """Producer coroutine that scans repositories."""
            tasks: list[asyncio.Task[None]] = []
            try:
                async for (
                    repo_node
                ) in self._iter_org_repositories_with_open_prs(organization):
                    task = asyncio.create_task(process_repository(repo_node))
                    tasks.append(task)

                # Wait for all repository processing to complete
                if tasks:
                    await asyncio.gather(*tasks, return_exceptions=True)

            finally:
                # Signal completion by putting None in queue
                await pr_queue.put(None)

        # Start producer task
        producer_task = asyncio.create_task(producer())

        try:
            # Yield PRs as they become available
            while True:
                item = await pr_queue.get()
                if item is None:
                    break
                yield item
        finally:
            # Ensure the producer task completes and surface any exception it
            # raised. ``gather`` wraps the single task so the cleanup is an
            # explicit call expression: a bare ``await producer_task`` is
            # functionally equivalent but CodeQL reports it as a statement
            # with no effect.
            await asyncio.gather(producer_task)

    async def _iter_org_repositories_with_open_prs(
        self, organization: str
    ) -> AsyncIterator[dict[str, Any]]:
        """Iterate over repositories in an organization that have open PRs.

        Args:
            organization: Organization name

        Yields:
            Repository nodes with open PRs
        """
        has_next_page = True
        end_cursor = None

        while has_next_page:
            variables: dict[str, Any] = {
                "org": organization,
                "cursor": end_cursor,
                "prsPageSize": 1,  # Just need to know if there are PRs
                "contextsPageSize": 0,  # Don't need status contexts
            }

            try:
                result = await self.client.graphql(
                    ORG_REPOS_WITH_PRS, variables=variables
                )
                org_data = result.get("organization", {})
                if not org_data:
                    break

                repos = org_data.get("repositories", {})
                nodes = repos.get("nodes", [])

                # Only yield repos with open PRs
                for node in nodes:
                    prs = node.get("pullRequests", {})
                    if prs.get("totalCount", 0) > 0:
                        yield node

                page_info = repos.get("pageInfo", {})
                has_next_page = page_info.get("hasNextPage", False)
                end_cursor = page_info.get("endCursor")

            except Exception as e:
                self.logger.error(f"Error iterating repositories: {e}")
                break

    async def _fetch_repo_prs_first_page(
        self, owner: str, repo_name: str
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """Fetch first page of open PRs for a repository.

        Args:
            owner: Repository owner
            repo_name: Repository name

        Returns:
            Tuple of (PR nodes list, page info dict)
        """
        variables = {
            "owner": owner,
            "name": repo_name,
            "prsCursor": None,
            "prsPageSize": DEFAULT_PRS_PAGE_SIZE,
            "filesPageSize": DEFAULT_FILES_PAGE_SIZE,
            "commentsPageSize": DEFAULT_COMMENTS_PAGE_SIZE,
            "contextsPageSize": DEFAULT_CONTEXTS_PAGE_SIZE,
        }

        try:
            result = await self.client.graphql(
                REPO_OPEN_PRS_PAGE, variables=variables
            )
            repo_data = result.get("repository", {})
            if not repo_data:
                return [], {}

            prs = repo_data.get("pullRequests", {})
            pr_nodes = prs.get("nodes", [])
            page_info = prs.get("pageInfo", {})
            return pr_nodes, page_info

        except Exception as e:
            self.logger.error(
                f"Error fetching PRs for {owner}/{repo_name}: {e}"
            )
            return [], {}

    async def _iter_repo_open_prs_pages(
        self, owner: str, repo_name: str, after_cursor: str
    ) -> AsyncIterator[dict[str, Any]]:
        """Iterate over additional pages of open PRs for a repository.

        Args:
            owner: Repository owner
            repo_name: Repository name
            after_cursor: Cursor to start from

        Yields:
            PR nodes
        """
        has_next_page = True
        cursor = after_cursor

        while has_next_page:
            async with self.page_semaphore:
                variables = {
                    "owner": owner,
                    "name": repo_name,
                    "prsCursor": cursor,
                    "prsPageSize": DEFAULT_PRS_PAGE_SIZE,
                    "filesPageSize": DEFAULT_FILES_PAGE_SIZE,
                    "commentsPageSize": DEFAULT_COMMENTS_PAGE_SIZE,
                    "contextsPageSize": DEFAULT_CONTEXTS_PAGE_SIZE,
                }

                try:
                    result = await self.client.graphql(
                        REPO_OPEN_PRS_PAGE, variables=variables
                    )
                    repo_data = result.get("repository", {})
                    if not repo_data:
                        break

                    prs = repo_data.get("pullRequests", {})
                    pr_nodes = prs.get("nodes", [])

                    for pr_node in pr_nodes:
                        yield pr_node

                    page_info = prs.get("pageInfo", {})
                    has_next_page = page_info.get("hasNextPage", False)
                    cursor = page_info.get("endCursor")

                except Exception as e:
                    self.logger.error(
                        f"Error fetching PR page for {owner}/{repo_name}: {e}"
                    )
                    break

    def _should_include_pr(
        self, pr_node: dict[str, Any], include_drafts: bool
    ) -> bool:
        """Determine if a PR should be included in results.

        Args:
            pr_node: PR node from GraphQL response
            include_drafts: Whether to include draft PRs

        Returns:
            True if PR should be included
        """
        return include_drafts or not pr_node.get("isDraft", False)

    def _extract_failing_checks(self, pr: dict[str, Any]) -> list[str]:
        """Extract failing check names from PR statusCheckRollup.

        Args:
            pr: PR data from GraphQL with statusCheckRollup

        Returns:
            List of failing check names
        """
        failing: list[str] = []

        commits = (pr.get("commits") or {}).get("nodes", []) or []
        if not commits:
            return failing

        commit = (commits[0] or {}).get("commit") or {}
        rollup = commit.get("statusCheckRollup") or {}
        contexts = (rollup.get("contexts") or {}).get("nodes", []) or []

        for ctx in contexts:
            typ = ctx.get("__typename")
            if typ == "CheckRun":
                # Consider failure, cancelled, or timed_out as failing
                conclusion = (ctx.get("conclusion") or "").lower()
                if conclusion in (
                    "failure",
                    "cancelled",
                    "timed_out",
                    "action_required",
                ):
                    name = ctx.get("name") or ""
                    if name:
                        failing.append(name)
            elif typ == "StatusContext":
                # StatusContext uses state instead of conclusion
                state = (ctx.get("state") or "").upper()
                if state in ("FAILURE", "ERROR"):
                    context = ctx.get("context") or ""
                    if context:
                        failing.append(context)

        return failing

    def is_pr_blocked(self, pr: dict[str, Any]) -> tuple[bool, list[str]]:
        """Check if a PR is blocked from merging.

        Args:
            pr: PR data from GraphQL

        Returns:
            Tuple of (is_blocked, list of blocking reasons)
        """
        blocking_reasons: list[str] = []

        # Check mergeable state (uppercase enum from GraphQL)
        mergeable = (pr.get("mergeable") or "").upper()
        if mergeable == "CONFLICTING":
            blocking_reasons.append("Merge conflicts")

        # Check merge state status (lowercase from GraphQL)
        merge_state = (pr.get("mergeStateStatus") or "").lower()

        # Check for merge conflicts via merge_state as well
        if merge_state == "dirty" and "Merge conflicts" not in blocking_reasons:
            blocking_reasons.append("Merge conflicts")

        # Check if behind base branch
        if merge_state == "behind":
            blocking_reasons.append("Behind base branch")

        # Check if blocked by branch protection rules
        if merge_state == "blocked":
            blocking_reasons.append("Blocked by branch protection rules")

        # Check for failing status checks
        failing_checks = self._extract_failing_checks(pr)
        if failing_checks:
            blocking_reasons.append(
                f"Failing checks: {', '.join(failing_checks)}"
            )

        return len(blocking_reasons) > 0, blocking_reasons

    @staticmethod
    def _split_owner_repo(full_name: str) -> tuple[str, str]:
        """Split 'owner/repo' into separate components.

        Args:
            full_name: Repository full name (owner/repo)

        Returns:
            Tuple of (owner, repo_name)
        """
        parts = full_name.split("/", 1)
        if len(parts) != 2:
            return "unknown", "unknown"
        return parts[0], parts[1]
