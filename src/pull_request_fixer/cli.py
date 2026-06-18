# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 The Linux Foundation

"""Command-line interface for pull-request-fixer."""

from __future__ import annotations

import asyncio
from contextlib import suppress
import logging
import re
import sys
from typing import Any
from urllib.parse import urlparse

from dependamerge import get_default_workers
from rich.console import Console
from rich.logging import RichHandler
import typer

from ._version import __version__
from .blocked_pr_scanner import BlockedPRScanner  # noqa: TC003
from .exceptions import FileAccessError, GitHubAPIError, ResourceNotFoundError
from .git_config import GitConfigMode
from .github_client import GitHubClient
from .models import GitHubFixResult  # noqa: TC001
from .pr_scanner import PRScanner  # noqa: TC003
from .progress_tracker import ProgressTracker  # noqa: TC003

console = Console()


def version_callback(ctx: typer.Context, value: bool) -> None:
    """Print version and exit."""
    if value:
        console.print(f"🏷️  pull-request-fixer version {__version__}")
        ctx.exit()


def help_callback(ctx: typer.Context, value: bool) -> None:
    """Print version and help."""
    if value:
        console.print(f"🏷️  pull-request-fixer version {__version__}")
        console.print()
        # Print help text to the console
        console.print(ctx.get_help())
        ctx.exit()


def setup_logging(
    log_level: str = "INFO", quiet: bool = False, verbose: bool = False
) -> None:
    """Configure logging with Rich handler."""
    if quiet:
        log_level = "ERROR"
    elif verbose:
        log_level = "DEBUG"

    logging.basicConfig(
        level=log_level,
        format="%(message)s",
        handlers=[
            RichHandler(console=console, show_time=False, show_path=False)
        ],
    )

    # Silence httpx INFO logs to prevent Rich display interruption
    logging.getLogger("httpx").setLevel(logging.WARNING)


def parse_target(target: str) -> tuple[str, str]:
    """Parse target to determine if it's an organization or a specific PR URL.

    Args:
        target: Organization name, GitHub URL, or PR URL

    Returns:
        Tuple of (type, value) where:
        - type is "org" or "pr"
        - value is organization name for "org", or PR URL for "pr"

    Examples:
        parse_target("myorg") -> ("org", "myorg")
        parse_target("https://github.com/myorg") -> ("org", "myorg")
        parse_target("https://github.com/owner/repo/pull/123") -> ("pr", "https://github.com/owner/repo/pull/123")
    """
    # Remove trailing slash
    target = target.rstrip("/")

    # Check if it's a PR URL
    if "/pull/" in target or "/pulls/" in target:
        # It's a specific PR URL
        return ("pr", target)

    # Check if it's a GitHub URL by parsing it and validating the host.
    # A substring check such as ``"github.com" in target`` is unsafe because
    # a host like ``github.com.evil.example`` would also match, so parse the
    # URL and compare the hostname exactly. URLs without a scheme (e.g.
    # ``github.com/org``) are treated as network-path references for parsing.
    parse_input = target if "://" in target else f"//{target}"
    hostname = (urlparse(parse_input).hostname or "").lower()
    if hostname == "github.com" or hostname.endswith(".github.com"):
        # Extract org from the URL path: /ORG or /ORG/...
        org = urlparse(parse_input).path.lstrip("/").split("/")[0]
        if org:
            return ("org", org)

    # Not a URL, return as organization
    return ("org", target)


def extract_pr_info_from_url(pr_url: str) -> tuple[str, str, int] | None:
    """Extract owner, repo, and PR number from a PR URL.

    Args:
        pr_url: GitHub PR URL

    Returns:
        Tuple of (owner, repo, pr_number) or None if invalid

    Example:
        extract_pr_info_from_url("https://github.com/owner/repo/pull/123")
        -> ("owner", "repo", 123)
    """
    # Match pattern: https://github.com/OWNER/REPO/pull(s)/NUMBER
    match = re.match(
        r"https?://github\.com/([^/]+)/([^/]+)/pulls?/(\d+)", pr_url
    )
    if match:
        owner = match.group(1)
        repo = match.group(2)
        pr_number = int(match.group(3))
        return (owner, repo, pr_number)
    return None


# Create Typer app
app = typer.Typer(
    name="pull-request-fixer",
    help="Fix pull requests with GitHub integration",
    add_completion=False,
    rich_markup_mode="rich",
)


def main(
    target: str = typer.Argument(
        None,
        help="GitHub organization name/URL or PR URL (e.g., 'myorg', 'https://github.com/myorg', or 'https://github.com/owner/repo/pull/123')",
    ),
    _help: bool = typer.Option(
        False,
        "--help",
        "-h",
        callback=help_callback,
        is_eager=True,
        help="Show this message and exit",
    ),
    token: str | None = typer.Option(
        None,
        "--token",
        "-t",
        help="GitHub token (or set GITHUB_TOKEN env var)",
        envvar="GITHUB_TOKEN",
    ),
    fix_title: bool = typer.Option(
        False,
        "--fix-title",
        help="Fix PR title to match first commit message subject",
    ),
    fix_body: bool = typer.Option(
        False,
        "--fix-body",
        help="Fix PR body to match first commit message body (excluding trailers)",
    ),
    fix_files: bool = typer.Option(
        False,
        "--fix-files",
        help="Fix files in PR using regex search/replace",
    ),
    file_pattern: str | None = typer.Option(
        None,
        "--file-pattern",
        help="Regex pattern to match file paths (e.g., './action.yaml')",
    ),
    search_pattern: str | None = typer.Option(
        None,
        "--search-pattern",
        help="Regex pattern to search for in matched files",
    ),
    replacement: str | None = typer.Option(
        None,
        "--replacement",
        help="Replacement string (empty to remove lines)",
    ),
    remove_lines: bool = typer.Option(
        False,
        "--remove-lines",
        help="Remove matching lines entirely instead of replacing",
    ),
    context_start: str | None = typer.Option(
        None,
        "--context-start",
        help="Regex pattern for context start (e.g., 'inputs:')",
    ),
    context_end: str | None = typer.Option(
        None,
        "--context-end",
        help="Regex pattern for context end (e.g., 'runs:')",
    ),
    pr_content_only: bool = typer.Option(
        False,
        "--pr-content-only",
        help="Only fix files that are already modified in the PR (default: fix all matching files in repository)",
    ),
    show_diff: bool = typer.Option(
        False,
        "--show-diff",
        help="Show diff output for file changes",
    ),
    update_method: str = typer.Option(
        "git",
        "--update-method",
        help="Method to apply file fixes: 'git' (clone, amend, push) or 'api' (GitHub API) - only applies to --fix-files. If used without --fix-files, this option has no effect.",
        case_sensitive=False,
    ),
    disable_signing: bool = typer.Option(
        False,
        "--disable-signing",
        help="Disables commit signing (only applies to 'git' method with --fix-files)",
    ),
    bot_identity: bool = typer.Option(
        False,
        "--bot-identity",
        help="Use bot identity instead of user (only applies to 'git' method with --fix-files, disables commit signing)",
    ),
    include_drafts: bool = typer.Option(
        False,
        "--include-drafts",
        help="Include draft PRs in scan",
    ),
    no_blocked_only: bool = typer.Option(
        False,
        "--no-blocked-only",
        help="Process ALL PRs, not just blocked/unmergeable ones (default: only process blocked PRs)",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Preview changes without applying them",
    ),
    workers: int | None = typer.Option(
        None,
        "--workers",
        "-j",
        min=1,
        max=32,
        help="Number of parallel workers (auto-detects CPU cores if not specified)",
    ),
    verbose: bool = typer.Option(
        False,
        "--verbose",
        "-v",
        help="Enable verbose output",
    ),
    quiet: bool = typer.Option(
        False,
        "--quiet",
        "-q",
        help="Suppress all output except errors",
    ),
    log_level: str = typer.Option(
        "INFO",
        "--log-level",
        help="Set logging level",
    ),
    _version: bool = typer.Option(
        False,
        "--version",
        callback=version_callback,
        is_eager=True,
        help="Show version and exit",
    ),
) -> None:
    """
    Pull request fixer - automatically fix PR titles, bodies, and files.

    Can process either:
    - An entire organization: By default scans only blocked pull requests (use --no-blocked-only for all PRs)
    - A specific PR: Processes only that pull request

    Update Methods (for --fix-files only):
    - 'git' (default): Clone repo, amend commit, force-push (respects signing)
    - 'api': Use GitHub API to update files (shows as verified by GitHub)

    Git Identity & Signing (only applies to 'git' update method with --fix-files):
    - By default, uses your git user.name, user.email, and commit signing settings
    - --disable-signing: Use your identity but disable commit signing
    - --bot-identity: Use bot identity instead of user (disables commit signing)

    Examples:
      pull-request-fixer myorg --fix-title --fix-body
      pull-request-fixer https://github.com/myorg --fix-title --dry-run
      pull-request-fixer https://github.com/owner/repo/pull/123 --fix-title
      pull-request-fixer myorg --fix-title --workers 8 --verbose

      # Fix files with regex (API method, default):
      pull-request-fixer <PR-URL> --fix-files --file-pattern './action.yaml' \
        --search-pattern 'type:' --remove-lines --context-start 'inputs:' --context-end 'runs:'

      # Fix files with git method (uses local signing):
      pull-request-fixer <PR-URL> --fix-files --update-method git \
        --file-pattern './action.yaml' --search-pattern 'type:' --remove-lines
    """
    # If no target provided, show help
    if target is None:
        console.print("Error: Missing required argument 'TARGET'.")  # type: ignore[unreachable]
        console.print()
        console.print("Usage: pull-request-fixer [OPTIONS] TARGET")
        console.print()
        console.print("TARGET can be:")
        console.print("  - Organization name: myorg")
        console.print("  - Organization URL: https://github.com/myorg")
        console.print(
            "  - Specific PR URL: https://github.com/owner/repo/pull/123"
        )
        console.print()
        console.print("Run 'pull-request-fixer --help' for more information.")
        raise typer.Exit(1)

    setup_logging(log_level=log_level, quiet=quiet, verbose=verbose)

    # Normalize and validate update method
    normalized_update_method = update_method.lower()
    if normalized_update_method not in ["git", "api"]:
        console.print(
            f"[red]Error:[/red] Invalid update method '{update_method}' "
            f"(normalized to '{normalized_update_method}'). Use 'git' or 'api'"
        )
        raise typer.Exit(1)

    # Determine git config mode from CLI flags (only relevant for git method with --fix-files)
    if bot_identity and disable_signing:
        console.print(
            "[red]Error:[/red] Cannot use both --bot-identity and --disable-signing"
        )
        raise typer.Exit(1)

    if bot_identity:
        git_config_mode = GitConfigMode.BOT_IDENTITY
    elif disable_signing:
        git_config_mode = GitConfigMode.USER_NO_SIGN
    else:
        git_config_mode = GitConfigMode.USER_INHERIT

    # Validate that at least one fix option is enabled
    if not fix_title and not fix_body and not fix_files:
        console.print(
            "[yellow]Warning:[/yellow] No fix options specified. "
            "Use --fix-title, --fix-body, and/or --fix-files to enable fixes."
        )
        console.print()
        console.print("Available options:")
        console.print(
            "  --fix-title  Fix PR title to match first commit subject"
        )
        console.print("  --fix-body   Fix PR body to match first commit body")
        console.print("  --fix-files  Fix files using regex search/replace")
        console.print()
        console.print(
            "Example: pull-request-fixer myorg --fix-title --fix-body"
        )
        raise typer.Exit(1)

    # Validate file fixing options
    if fix_files:
        if not file_pattern:
            console.print(
                "[red]Error:[/red] --file-pattern is required when using --fix-files"
            )
            raise typer.Exit(1)
        if not search_pattern:
            console.print(
                "[red]Error:[/red] --search-pattern is required when using --fix-files"
            )
            raise typer.Exit(1)
        if not remove_lines and replacement is None:
            console.print(
                "[red]Error:[/red] Either --replacement or --remove-lines is required when using --fix-files"
            )
            raise typer.Exit(1)

    if not token:
        console.print(
            "[red]Error:[/red] GitHub token required. "
            "Provide --token or set GITHUB_TOKEN environment variable"
        )
        raise typer.Exit(1)

    # Parse target to determine if it's an org or a specific PR
    target_type, target_value = parse_target(target)

    if target_type == "pr":
        # Process single PR
        asyncio.run(
            process_single_pr(
                pr_url=target_value,
                token=token,
                fix_title=fix_title,
                fix_body=fix_body,
                fix_files=fix_files,
                file_pattern=file_pattern,
                search_pattern=search_pattern,
                replacement=replacement or "",
                remove_lines=remove_lines,
                context_start=context_start,
                context_end=context_end,
                pr_content_only=pr_content_only,
                blocked_only=not no_blocked_only,
                dry_run=dry_run,
                show_diff=show_diff,
                quiet=quiet,
                git_config_mode=git_config_mode,
                update_method=normalized_update_method,
                bot_identity=bot_identity,
                disable_signing=disable_signing,
            )
        )
    else:
        # Scan organization
        asyncio.run(
            scan_and_fix_organization(
                org=target_value,
                token=token,
                include_drafts=include_drafts,
                blocked_only=not no_blocked_only,
                fix_title=fix_title,
                fix_body=fix_body,
                fix_files=fix_files,
                file_pattern=file_pattern,
                search_pattern=search_pattern,
                replacement=replacement or "",
                remove_lines=remove_lines,
                context_start=context_start,
                context_end=context_end,
                pr_content_only=pr_content_only,
                dry_run=dry_run,
                show_diff=show_diff,
                workers=workers
                if workers is not None
                else get_default_workers(),
                quiet=quiet,
                git_config_mode=git_config_mode,
                update_method=normalized_update_method,
            )
        )


async def process_single_pr(
    pr_url: str,
    token: str,
    fix_title: bool,
    fix_body: bool,
    fix_files: bool,
    file_pattern: str | None,
    search_pattern: str | None,
    replacement: str,
    remove_lines: bool,
    context_start: str | None,
    context_end: str | None,
    pr_content_only: bool,
    blocked_only: bool,
    *,
    dry_run: bool,
    show_diff: bool,
    quiet: bool,
    git_config_mode: str,
    update_method: str,
    bot_identity: bool,
    disable_signing: bool,
) -> None:
    """Process a single PR by URL.

    Args:
        pr_url: GitHub PR URL
        token: GitHub token
        fix_title: Whether to fix PR title
        fix_body: Whether to fix PR body
        fix_files: Whether to fix files using regex
        file_pattern: Regex pattern to match file paths
        search_pattern: Regex pattern to search for
        replacement: Replacement string
        remove_lines: Whether to remove matching lines
        context_start: Optional context start marker
        context_end: Optional context end marker
        blocked_only: Whether to only process blocked PRs
        dry_run: Whether to preview without applying changes
        quiet: Whether to suppress output
    """
    if not quiet:
        console.print(f"🔍 Processing PR: {pr_url}")
        fixes = []
        if fix_title:
            fixes.append("title")
        if fix_body:
            fixes.append("body")
        if fix_files:
            fixes.append("files")
        console.print(f"🔧 Will fix: {', '.join(fixes)}")
        if fix_files:
            method_desc = (
                "Git clone/amend/push"
                if update_method == "git"
                else "GitHub API"
            )
            console.print(f"📝 File update method: {method_desc}")
            if update_method == "git":
                if bot_identity:
                    console.print("🤖 Git identity: Bot (pull-request-fixer)")
                elif disable_signing:
                    console.print("👤 Git identity: User (signing disabled)")
                else:
                    console.print(
                        "👤 Git identity: User (inheriting signing config)"
                    )
        if dry_run:
            console.print("🏃 Dry run mode: no changes will be applied")
        console.print()

    try:
        async with GitHubClient(token) as client:  # type: ignore[attr-defined]
            # Check if PR is blocked if --blocked-only is specified
            if blocked_only:
                # Extract owner, repo, and PR number from URL
                pr_info = extract_pr_info_from_url(pr_url)
                if not pr_info:
                    console.print(f"[red]Error:[/red] Invalid PR URL: {pr_url}")
                    raise typer.Exit(1)

                owner, repo_name, pr_number = pr_info

                # Fetch PR data using GraphQL to check blocked status
                query = """
                query($owner: String!, $repo: String!, $number: Int!) {
                    repository(owner: $owner, name: $repo) {
                        pullRequest(number: $number) {
                            number
                            title
                            state
                            merged
                            mergeable
                            mergeStateStatus
                            commits(last: 1) {
                                nodes {
                                    commit {
                                        statusCheckRollup {
                                            contexts(first: 100) {
                                                nodes {
                                                    __typename
                                                    ... on StatusContext {
                                                        state
                                                        context
                                                    }
                                                    ... on CheckRun {
                                                        conclusion
                                                        status
                                                        name
                                                    }
                                                }
                                            }
                                        }
                                    }
                                }
                            }
                        }
                    }
                }
                """

                variables = {
                    "owner": owner,
                    "repo": repo_name,
                    "number": pr_number,
                }

                response = await client.graphql(query, variables)
                pr_data = response.get("repository", {}).get("pullRequest")

                if not pr_data:
                    console.print("[red]Error:[/red] Could not fetch PR data")
                    raise typer.Exit(1)

                # Check if PR is closed
                pr_state = pr_data.get("state", "").upper()
                if pr_state != "OPEN":
                    # Check if it was merged
                    is_merged = pr_data.get("merged", False)
                    state_display = (
                        "merged"
                        if is_merged
                        else (pr_state.lower() if pr_state else "closed")
                    )
                    console.print(
                        f"[yellow]ℹ️  Pull request #{pr_number} is {state_display} and cannot be processed[/yellow]"
                    )
                    if not quiet:
                        console.print(
                            "[dim]Tip: This tool can only process open pull requests[/dim]"
                        )
                    sys.exit(0)

                # Check if PR is blocked using dependamerge logic
                blocked_scanner = BlockedPRScanner(
                    token=token,
                    max_repo_tasks=1,
                )
                try:
                    (
                        is_blocked,
                        reasons,
                    ) = await blocked_scanner._is_pr_blocked_async(pr_data)
                    reason = "; ".join(reasons) if reasons else "Unknown"
                finally:
                    await blocked_scanner.close()

                if not is_blocked:
                    console.print(
                        "[yellow]⚠️  Error: pull request is NOT in a blocked state[/yellow]"
                    )
                    raise typer.Exit(1)

                if not quiet:
                    console.print(f"✓ PR is blocked: {reason}")
                    console.print()

            # Handle file fixing separately (uses Git operations)
            if fix_files and file_pattern and search_pattern:
                from .pr_file_fixer import PRFileFixer

                if not quiet:
                    console.print("📝 Fixing files in PR...")

                fixer = PRFileFixer(client, git_config_mode=git_config_mode)
                result: GitHubFixResult = await fixer.fix_pr_by_url(
                    pr_url,
                    file_pattern,
                    search_pattern,
                    replacement,
                    remove_lines=remove_lines,
                    context_start=context_start,
                    context_end=context_end,
                    dry_run=dry_run,
                    update_method=update_method,
                    pr_content_only=pr_content_only,
                )

                if not quiet:
                    console.print()
                    if result.success:
                        # Check if this is a "no changes" message (starts with ⏩)
                        if result.message.startswith("⏩"):
                            # No changes - use green for entire message
                            console.print(f"[green]{result.message}[/green]")
                        else:
                            # Files were modified - show message in green, details in orange
                            console.print(f"[green]✅ {result.message}[/green]")

                        # Show modified files with diffs (in orange to indicate changes)
                        for modification in result.file_modifications:
                            # Get relative path for display
                            try:
                                display_path = modification.file_path.name
                            except Exception:
                                display_path = str(modification.file_path)

                            # Use different emoji based on dry-run status
                            emoji = "📂" if dry_run else "🔀"
                            console.print(
                                f"[orange1]{emoji} {display_path}[/orange1]"
                            )

                            # Show diff if requested
                            if show_diff:
                                diff_output = modification.diff
                                if diff_output:
                                    console.print(
                                        diff_output,
                                        highlight=False,
                                        markup=False,
                                    )
                    else:
                        console.print(f"[yellow]⚠️  {result.message}[/yellow]")
                        if result.error:
                            console.print(f"   Error: {result.error}")

                # Create a PR comment if files were modified and not dry-run
                if not dry_run and result.success and result.file_modifications:
                    # Extract PR info from URL
                    pr_info = extract_pr_info_from_url(pr_url)
                    if pr_info:
                        owner, repo_name, pr_number = pr_info
                        command_args = {
                            "file_pattern": file_pattern,
                            "search_pattern": search_pattern,
                            "replacement": replacement,
                            "remove_lines": remove_lines,
                            "context_start": context_start,
                            "context_end": context_end,
                            "pr_content_only": pr_content_only,
                        }
                        await create_file_fix_comment(
                            client,
                            owner,
                            repo_name,
                            pr_number,
                            result,
                            command_args,
                        )

                return

            # Handle title/body fixing (uses GraphQL)
            # Extract PR info from URL
            pr_info = extract_pr_info_from_url(pr_url)
            if not pr_info:
                console.print(f"[red]Error:[/red] Invalid PR URL: {pr_url}")
                console.print()
                console.print(
                    "Expected format: https://github.com/owner/repo/pull/123"
                )
                raise typer.Exit(1)

            owner, repo_name, pr_number = pr_info

            # Fetch PR data
            if not quiet:
                console.print("📥 Fetching pull request metadata...")

            endpoint = f"/repos/{owner}/{repo_name}/pulls/{pr_number}"
            pr_data_response = await client._request("GET", endpoint)

            if not pr_data_response or not isinstance(pr_data_response, dict):
                console.print("[red]Error:[/red] Could not fetch PR data")
                raise typer.Exit(1)

            # Process the PR
            semaphore = asyncio.Semaphore(1)  # Single PR, no parallelism needed
            result = await process_pr(  # type: ignore[assignment]  # pyright: ignore[reportAssignmentType]
                client=client,
                owner=owner,
                repo_name=repo_name,
                pr_data=pr_data_response,
                fix_title=fix_title,
                fix_body=fix_body,
                dry_run=dry_run,
                quiet=quiet,
                semaphore=semaphore,
            )

            if not quiet:
                console.print()
                if result:
                    if dry_run:
                        console.print(
                            "[green]✅ [DRY RUN] Would fix this PR[/green]"
                        )
                    else:
                        console.print(
                            "[green]✅ Pull request updated successfully[/green]"
                        )
                else:
                    console.print(
                        "[yellow]ℹ️  No changes needed or applied[/yellow]"
                    )

    except ResourceNotFoundError as e:
        # Resource not found (404) - show friendly message without stack trace
        console.print(f"[yellow]⚠️  {e}[/yellow]")
        raise typer.Exit(1) from e
    except (FileAccessError, GitHubAPIError) as e:
        # These are expected errors - show friendly message without stack trace
        console.print(f"[red]Error:[/red] {e}")
        raise typer.Exit(1) from e
    except Exception as e:
        console.print(f"[red]Error processing PR:[/red] {e}")
        if not quiet:
            import traceback

            console.print("[dim]" + traceback.format_exc() + "[/dim]")
        raise typer.Exit(1) from e


async def scan_and_fix_organization(
    org: str,
    token: str,
    fix_title: bool,
    fix_body: bool,
    fix_files: bool,
    file_pattern: str | None,
    search_pattern: str | None,
    replacement: str,
    remove_lines: bool,
    context_start: str | None,
    context_end: str | None,
    pr_content_only: bool,
    show_diff: bool,
    include_drafts: bool,
    blocked_only: bool,
    dry_run: bool,
    workers: int,
    quiet: bool,
    git_config_mode: str,
    update_method: str,
) -> None:
    """Scan organization for PRs needing fixes and fix them.

    Args:
        org: Organization name
        token: GitHub token
        fix_title: Whether to fix PR titles
        fix_body: Whether to fix PR bodies
        include_drafts: Whether to include draft PRs
        blocked_only: Whether to only process blocked/unmergeable PRs
        dry_run: Whether to preview without applying changes
        workers: Number of parallel workers
        quiet: Whether to suppress output
    """
    if not quiet:
        console.print(f"🔍 Scanning organization: {org}")
        fixes = []
        if fix_title:
            fixes.append("titles")
        if fix_body:
            fixes.append("bodies")
        if fix_files:
            fixes.append("files")
        console.print(f"🔧 Will fix: {', '.join(fixes)}")
        if not blocked_only:
            console.print("⚠️  Processing ALL PRs (not just blocked ones)")
        else:
            console.print(
                "🚫 Only processing blocked/unmergeable PRs (default)"
            )
        if dry_run:
            console.print("🏃 Dry run mode: no changes will be applied")

    try:
        async with GitHubClient(token) as client:  # type: ignore[attr-defined]
            # Validate token before proceeding
            try:
                is_valid, username, scopes = await client.validate_token()
                if not quiet:
                    console.print(f"✓ Token validated for user: {username}")
                    if scopes:
                        # Only check scopes if we were able to retrieve them
                        if "repo" not in scopes and "public_repo" not in scopes:
                            console.print(
                                "[yellow]⚠️  Warning: Token may not have required 'repo' scope[/yellow]"
                            )
                        if "read:org" not in scopes:
                            console.print(
                                "[yellow]⚠️  Warning: Token may not have 'read:org' scope needed for status checks[/yellow]"
                            )
                    else:
                        # GitHub Actions tokens don't report scopes via /user endpoint
                        console.print(
                            "[dim]Note: Unable to verify token scopes (expected for GitHub Actions tokens)[/dim]"
                        )
                    console.print()
            except Exception as e:
                console.print(f"[red]✗ Token validation failed: {e}[/red]")
                console.print(
                    "[yellow]Hint: Ensure GITHUB_TOKEN has 'repo' and 'read:org' scopes and access to the organization[/yellow]"
                )
                raise typer.Exit(1) from e

            # Create progress tracker for visual feedback
            progress_tracker = (
                None if quiet else ProgressTracker(org, show_pr_stats=True)
            )

            # Collect PRs to process
            prs_to_process: list[tuple[str, str, dict[str, Any]]] = []

            # Use BlockedPRScanner (dependamerge) when filtering to blocked PRs only
            # This ensures consistent blocking logic across both tools
            try:
                if blocked_only:
                    # Use dependamerge's GitHubService for blocked PR detection
                    blocked_scanner = BlockedPRScanner(
                        token=token,
                        progress_tracker=progress_tracker,
                        max_repo_tasks=workers,
                    )

                    # Note: progress_tracker.start() is called by scanner internally
                    try:
                        async for (
                            owner,
                            repo_name,
                            pr_data,
                            _unmergeable_pr,
                        ) in blocked_scanner.scan_organization_for_blocked_prs(
                            org, include_drafts=include_drafts
                        ):
                            # Store PR info
                            prs_to_process.append((owner, repo_name, pr_data))
                    finally:
                        await blocked_scanner.close()
                else:
                    # Use original PRScanner for all PRs (no blocking filter)
                    scanner = PRScanner(
                        client,
                        progress_tracker=progress_tracker,
                        max_repo_tasks=workers,
                        max_page_tasks=workers * 2,
                    )

                    # Note: progress_tracker.start() is called by scanner after counting repos
                    async for (
                        owner,
                        repo_name,
                        pr_data,
                    ) in scanner.scan_organization(
                        org, include_drafts=include_drafts, blocked_only=False
                    ):
                        # Store PR info
                        prs_to_process.append((owner, repo_name, pr_data))

            except Exception as scan_error:
                if progress_tracker:
                    progress_tracker.stop()
                console.print(
                    f"\n[yellow]⚠️  Scanning interrupted: {scan_error}[/yellow]"
                )
                console.print("[yellow]Processing PRs found so far...[/yellow]")

            # Stop progress tracker
            if progress_tracker:
                progress_tracker.stop()

            if not prs_to_process:
                if blocked_only:
                    console.print("\n[green]✅ No blocked PRs found![/green]")
                else:
                    console.print("\n[green]✅ No PRs found![/green]")
                return

            if not quiet:
                if blocked_only:
                    console.print("\n🔍 Examining blocked pull requests\n")
                else:
                    console.print("\n🔍 Examining pull requests\n")

            # Phase 4: Process PRs in parallel using semaphore for concurrency control
            semaphore = asyncio.Semaphore(workers)
            tasks = []

            # Handle file fixing separately if enabled
            if fix_files and file_pattern and search_pattern:
                from .pr_file_fixer import PRFileFixer

                fixer = PRFileFixer(client, git_config_mode=git_config_mode)

                # Inform about update method
                if not quiet:
                    method_desc = (
                        "Git clone/amend/push"
                        if update_method == "git"
                        else "GitHub API"
                    )
                    console.print(f"\n📝 File update method: {method_desc}")
                    if update_method == "git":
                        if git_config_mode == GitConfigMode.BOT_IDENTITY:
                            console.print("🤖 Git identity: Bot")
                        elif git_config_mode == GitConfigMode.USER_NO_SIGN:
                            console.print(
                                "👤 Git identity: User (signing disabled)"
                            )
                        else:
                            console.print(
                                "👤 Git identity: User (inheriting signing config)"
                            )
                    console.print()

                for owner, repo_name, pr_data in prs_to_process:
                    pr_number = pr_data.get("number", 0)
                    pr_url = f"https://github.com/{owner}/{repo_name}/pull/{pr_number}"

                    async def process_pr_files(
                        url: str, owner: str, repo_name: str, pr_number: int
                    ) -> dict[str, Any]:
                        async with semaphore:
                            result = await fixer.fix_pr_by_url(
                                url,
                                file_pattern,
                                search_pattern,
                                replacement,
                                remove_lines=remove_lines,
                                context_start=context_start,
                                context_end=context_end,
                                dry_run=dry_run,
                                update_method=update_method,
                                pr_content_only=pr_content_only,
                            )

                            pr_id = f"{owner}/{repo_name}#{pr_number}"
                            result_dict = {
                                "result": result,
                                "status": "success"
                                if result.success
                                else "failed",
                                "pr_id": pr_id,
                            }

                            # In dry-run mode, show output immediately (progressively)
                            if dry_run and not quiet:
                                # Only show output if there are actual changes or failures
                                if result.success and result.file_modifications:
                                    # Files were modified - show in green with orange details below
                                    console.print(
                                        f"[green]✅ {pr_id}: {result.message}[/green]"
                                    )

                                    # Show modified files with diffs
                                    for (
                                        modification
                                    ) in result.file_modifications:
                                        try:
                                            display_path = (
                                                modification.file_path.name
                                            )
                                        except Exception:
                                            display_path = str(
                                                modification.file_path
                                            )

                                        emoji = "🔀"
                                        console.print(
                                            f"[orange1]{emoji} {display_path}[/orange1]"
                                        )

                                        # Always show diff in dry-run mode
                                        diff_output = modification.diff
                                        if diff_output:
                                            console.print(
                                                diff_output,
                                                highlight=False,
                                                markup=False,
                                            )
                                    console.print()  # Blank line between PRs
                                elif not result.success:
                                    console.print(
                                        f"[red]❌ Failed: {pr_id}[/red]"
                                    )
                                    if result.error:
                                        console.print(
                                            f"  Error: {result.error}"
                                        )
                                    console.print()

                            # Create PR comment if files were modified and not dry-run
                            if (
                                not dry_run
                                and result.success
                                and result.file_modifications
                            ):
                                command_args = {
                                    "file_pattern": file_pattern,
                                    "search_pattern": search_pattern,
                                    "replacement": replacement,
                                    "remove_lines": remove_lines,
                                    "context_start": context_start,
                                    "context_end": context_end,
                                    "pr_content_only": pr_content_only,
                                }
                                with suppress(Exception):
                                    await create_file_fix_comment(
                                        client,
                                        owner,
                                        repo_name,
                                        pr_number,
                                        result,
                                        command_args,
                                    )

                            return result_dict

                    tasks.append(
                        asyncio.create_task(
                            process_pr_files(
                                pr_url, owner, repo_name, pr_number
                            )
                        )
                    )
            else:
                # Handle title/body fixing (GraphQL-based)
                for owner, repo_name, pr_data in prs_to_process:
                    task = process_pr(
                        client=client,
                        owner=owner,
                        repo_name=repo_name,
                        pr_data=pr_data,
                        fix_title=fix_title,
                        fix_body=fix_body,
                        dry_run=dry_run,
                        quiet=quiet,
                        semaphore=semaphore,
                    )
                    tasks.append(asyncio.create_task(task))

            # Wait for all processing to complete
            results = await asyncio.gather(*tasks, return_exceptions=True)

            # Process results and output
            # Count only PRs that have actual changes (file modifications)
            prs_with_changes = 0
            failed_count = 0

            if not quiet:
                # In non-dry-run mode, show summary at the end
                # In dry-run mode, output was already shown progressively above
                for result in results:
                    if isinstance(result, Exception):
                        failed_count += 1
                        if not dry_run:
                            console.print(
                                f"[red]❌ Failed to update pull request: {result}[/red]"
                            )
                    elif isinstance(result, dict):
                        status = result.get("status")
                        pr_id = result.get("pr_id", "unknown")

                        if status == "success":
                            # Check if this result has actual file modifications
                            fix_result = result.get("result")
                            has_changes = False

                            if fix_result and fix_result.file_modifications:
                                has_changes = True
                                prs_with_changes += 1
                            elif result.get("title") or result.get("body"):
                                # Title/body changes
                                has_changes = True
                                prs_with_changes += 1

                            # Only show detailed output in non-dry-run mode
                            # (dry-run output was already shown progressively)
                            if not dry_run and has_changes:
                                # Check if this is a file fixing result
                                if fix_result:
                                    # File fixing result
                                    # Check if this is a "no changes" message (starts with ⏩)
                                    if fix_result.message.startswith("⏩"):
                                        # No changes - use green for entire message
                                        console.print(
                                            f"[green]{pr_id}: {fix_result.message}[/green]"
                                        )
                                    else:
                                        # Files were modified - show in green
                                        console.print(
                                            f"[green]✅ {pr_id}: {fix_result.message}[/green]"
                                        )

                                    # Show modified files with diffs
                                    for (
                                        modification
                                    ) in fix_result.file_modifications:
                                        try:
                                            display_path = (
                                                modification.file_path.name
                                            )
                                        except Exception:
                                            display_path = str(
                                                modification.file_path
                                            )

                                        emoji = "🔀"
                                        console.print(
                                            f"[orange1]{emoji} {display_path}[/orange1]"
                                        )

                                        if show_diff:
                                            diff_output = modification.diff
                                            if diff_output:
                                                console.print(
                                                    diff_output,
                                                    highlight=False,
                                                    markup=False,
                                                )
                                else:
                                    # Title/body fixing result
                                    console.print(f"[green]✅ {pr_id}[/green]")

                                    # Show title/body changes if present
                                    title_info = result.get("title")
                                    if title_info:
                                        console.print(
                                            f"     Previous: {title_info['previous']}"
                                        )
                                        console.print(
                                            f"     Updated:  {title_info['updated']}"
                                        )
                        elif status == "failed":
                            failed_count += 1
                            if not dry_run:
                                error_msg = result.get("error", "Unknown error")
                                console.print(
                                    f"[red]❌ Failed to update pull request: {pr_id}[/red]"
                                )
                                if error_msg != "Unknown error":
                                    console.print(f"     Error: {error_msg}")
                        # Skip output for "no_change" status
            else:
                # Count results when quiet mode is on
                for result in results:
                    if isinstance(result, Exception):
                        failed_count += 1
                    elif isinstance(result, dict):
                        status = result.get("status")
                        if status == "success":
                            # Count only results with actual changes
                            fix_result = result.get("result")
                            if (
                                fix_result
                                and fix_result.file_modifications
                                or result.get("title")
                                or result.get("body")
                            ):
                                prs_with_changes += 1
                        elif status == "failed":
                            failed_count += 1

            # Summary
            if not quiet:
                # Build pr_type string conditionally
                pr_type_str = "blocked " if blocked_only else ""

                if dry_run:
                    # Dry run summary
                    if prs_with_changes == 0:
                        console.print(
                            f"☑️  No {pr_type_str}pull requests need fixes"
                        )
                    elif prs_with_changes == 1:
                        console.print(
                            f"☑️  Would apply fixes to 1 {pr_type_str}pull request"
                        )
                    else:
                        console.print(
                            f"☑️  Would apply fixes to {prs_with_changes} {pr_type_str}pull requests"
                        )
                else:
                    # Non-dry run summary
                    if prs_with_changes > 0:
                        console.print(
                            f"[green]✅ Updated {prs_with_changes} {pr_type_str}pull request{'s' if prs_with_changes != 1 else ''}[/green]"
                        )
                    if failed_count > 0:
                        console.print(
                            f"[red]❌ Failed updates: {failed_count}[/red]"
                        )

    except Exception as e:
        console.print(f"[red]Error scanning organization:[/red] {e}")
        if not quiet:
            import traceback

            console.print("[dim]" + traceback.format_exc() + "[/dim]")
        raise typer.Exit(1) from e


async def process_pr(
    client: GitHubClient,
    owner: str,
    repo_name: str,
    pr_data: dict[str, Any],
    fix_title: bool,
    fix_body: bool,
    dry_run: bool,
    quiet: bool,
    semaphore: asyncio.Semaphore,
) -> dict[str, Any]:
    """Process a single PR to fix title and/or body.

    Args:
        client: GitHub API client
        owner: Repository owner
        repo_name: Repository name
        pr_data: PR data from scanner
        fix_title: Whether to fix title
        fix_body: Whether to fix body
        dry_run: Whether this is a dry run
        quiet: Whether to suppress output
        semaphore: Semaphore for concurrency control

    Returns:
        Dict with status: 'success', 'failed', 'no_change', and optional details
    """
    async with semaphore:
        pr_number: int | None = pr_data.get("number")
        pr_title = pr_data.get("title", "")
        pr_id = f"{owner}/{repo_name}#{pr_number}"

        if pr_number is None:
            return {
                "status": "failed",
                "pr_id": pr_id,
                "error": "PR number not found",
            }

        try:
            # Get first commit info
            commit_info = await get_first_commit_info(
                client, owner, repo_name, pr_number
            )

            if not commit_info:
                return {
                    "status": "failed",
                    "pr_id": pr_id,
                    "error": "Could not retrieve commit info",
                }

            commit_subject = commit_info.get("subject", "").strip()
            commit_body = commit_info.get("body", "").strip()

            changes_needed = False
            title_result = None
            body_result = None

            # Check if title needs fixing
            if fix_title and commit_subject and commit_subject != pr_title:
                changes_needed = True
                if dry_run:
                    if not quiet:
                        console.print(f"🔄 {pr_id}")
                        console.print(f"     Current: {pr_title}")
                        console.print(f"     Fixed:   {commit_subject}")
                    title_result = {
                        "success": True,
                        "previous": pr_title,
                        "updated": commit_subject,
                    }
                else:
                    # Update PR title (silently during processing)
                    success = await update_pr_title(
                        client, owner, repo_name, pr_number, commit_subject
                    )
                    title_result = {
                        "success": success,
                        "previous": pr_title,
                        "updated": commit_subject,
                    }

            # Check if body needs fixing
            if fix_body and commit_body:
                # Get current PR body
                current_body = pr_data.get("body", "").strip()

                if commit_body != current_body:
                    changes_needed = True
                    if dry_run:
                        if not quiet:
                            console.print(f"🔄 {pr_id}")
                            console.print("   Would update body")
                            console.print(
                                f"     Length: {len(commit_body)} chars"
                            )
                        body_result = {"success": True}
                    else:
                        # Update PR body (silently during processing)
                        success = await update_pr_body(
                            client, owner, repo_name, pr_number, commit_body
                        )
                        body_result = {"success": success}

            if not changes_needed:
                return {"status": "no_change", "pr_id": pr_id}

            # Create a comment on the PR if changes were made (not in dry-run)
            if not dry_run and (title_result or body_result):
                changes_made = []
                if title_result and title_result.get("success"):
                    changes_made.append("title")
                if body_result and body_result.get("success"):
                    changes_made.append("body")

                if changes_made:
                    await create_pr_comment(
                        client, owner, repo_name, pr_number, changes_made
                    )

            # Determine overall status
            has_success = (title_result and title_result.get("success")) or (
                body_result and body_result.get("success")
            )
            has_failure = (
                title_result and not title_result.get("success")
            ) or (body_result and not body_result.get("success"))

            if has_failure:
                status = "failed"
            elif has_success:
                status = "success"
            else:
                status = "no_change"

            return {
                "status": status,
                "pr_id": pr_id,
                "title": title_result,
                "body": body_result,
            }

        except Exception as e:
            return {"status": "failed", "pr_id": pr_id, "error": str(e)}


async def get_first_commit_info(
    client: GitHubClient,
    owner: str,
    repo: str,
    pr_number: int,
) -> dict[str, str] | None:
    """Get the first commit's message from a PR.

    Args:
        client: GitHub API client
        owner: Repository owner
        repo: Repository name
        pr_number: PR number

    Returns:
        Dict with 'subject' and 'body' keys, or None if error
    """
    try:
        # Get commits for the PR
        endpoint = f"/repos/{owner}/{repo}/pulls/{pr_number}/commits"
        response = await client._request("GET", endpoint)

        if not response or not isinstance(response, list) or len(response) == 0:
            return None

        # Get first commit
        first_commit = response[0]
        commit_data = first_commit.get("commit", {})
        message = commit_data.get("message", "")

        # Parse commit message into subject and body
        subject, body = parse_commit_message(message)

        return {
            "subject": subject,
            "body": body,
        }

    except Exception as e:
        console.print(f"[red]Error getting commit info: {e}[/red]")
        return None


def parse_commit_message(message: str) -> tuple[str, str]:
    """Parse a commit message into subject and body.

    Removes trailers like 'Signed-off-by:', 'Co-authored-by:', etc.

    Args:
        message: Full commit message

    Returns:
        Tuple of (subject, body) where body has trailers removed
    """
    lines = message.split("\n")

    if not lines:
        return "", ""

    # First line is the subject
    subject = lines[0].strip()

    # Rest is body (skip empty line after subject if present)
    body_lines = lines[1:]

    # Skip leading empty lines
    while body_lines and not body_lines[0].strip():
        body_lines.pop(0)

    # Remove trailers from the end
    # Common trailer patterns
    trailer_patterns = [
        r"^Signed-off-by:",
        r"^Co-authored-by:",
        r"^Reviewed-by:",
        r"^Tested-by:",
        r"^Acked-by:",
        r"^Cc:",
        r"^Reported-by:",
        r"^Suggested-by:",
        r"^Fixes:",
        r"^See-also:",
        r"^Link:",
        r"^Bug:",
        r"^Change-Id:",
    ]

    # Find where trailers start (from the end)
    trailer_start_idx = len(body_lines)

    for i in range(len(body_lines) - 1, -1, -1):
        line = body_lines[i].strip()

        # Empty line before trailers is ok
        if not line:
            continue

        # Check if this line is a trailer
        is_trailer = False
        for pattern in trailer_patterns:
            if re.match(pattern, line, re.IGNORECASE):
                is_trailer = True
                break

        if is_trailer:
            # This line and everything after is a trailer
            trailer_start_idx = i
        else:
            # Found a non-trailer, non-empty line, stop looking
            break

    # Get body without trailers
    body_lines = body_lines[:trailer_start_idx]

    # Remove trailing empty lines
    while body_lines and not body_lines[-1].strip():
        body_lines.pop()

    body = "\n".join(body_lines).strip()

    return subject, body


async def update_pr_title(
    client: GitHubClient,
    owner: str,
    repo: str,
    pr_number: int,
    new_title: str,
) -> bool:
    """Update a PR's title.

    Args:
        client: GitHub API client
        owner: Repository owner
        repo: Repository name
        pr_number: PR number
        new_title: New title to set

    Returns:
        True if successful, False otherwise
    """
    try:
        endpoint = f"/repos/{owner}/{repo}/pulls/{pr_number}"
        data = {"title": new_title}

        response = await client._request("PATCH", endpoint, json=data)

        # If successful, trigger re-run of failed checks
        if response is not None:
            await rerun_failed_checks(client, owner, repo, pr_number)

        return response is not None

    except Exception as e:
        console.print(f"[red]Error updating PR title: {e}[/red]")
        return False


async def update_pr_body(
    client: GitHubClient,
    owner: str,
    repo: str,
    pr_number: int,
    new_body: str,
) -> bool:
    """Update a PR's body.

    Args:
        client: GitHub API client
        owner: Repository owner
        repo: Repository name
        pr_number: PR number
        new_body: New body to set

    Returns:
        True if successful, False otherwise
    """
    try:
        endpoint = f"/repos/{owner}/{repo}/pulls/{pr_number}"
        data = {"body": new_body}

        response = await client._request("PATCH", endpoint, json=data)

        # If successful, trigger re-run of failed checks
        if response is not None:
            await rerun_failed_checks(client, owner, repo, pr_number)

        return response is not None

    except Exception as e:
        console.print(f"[red]Error updating PR body: {e}[/red]")
        return False


async def rerun_failed_checks(
    client: GitHubClient,
    owner: str,
    repo: str,
    pr_number: int,
) -> bool:
    """Re-run failed checks on a PR after updates.

    This function attempts to trigger a re-run of failed checks by:
    1. Getting the head SHA of the PR
    2. Finding failed check runs for that SHA
    3. Re-requesting each failed check run

    Args:
        client: GitHub API client
        owner: Repository owner
        repo: Repository name
        pr_number: PR number
    """
    try:
        # Get PR to find head SHA
        pr_endpoint = f"/repos/{owner}/{repo}/pulls/{pr_number}"
        pr_data_response = await client._request("GET", pr_endpoint)

        if not pr_data_response or not isinstance(pr_data_response, dict):
            return False

        head_sha = pr_data_response.get("head", {}).get("sha")
        if not head_sha:
            return False

        # Get check runs for this commit
        checks_endpoint = f"/repos/{owner}/{repo}/commits/{head_sha}/check-runs"
        checks_data_response = await client._request("GET", checks_endpoint)

        if not checks_data_response or not isinstance(
            checks_data_response, dict
        ):
            return False

        check_runs = checks_data_response.get("check_runs", [])

        # Find failed or cancelled check runs
        failed_runs = [
            run
            for run in check_runs
            if run.get("conclusion")
            in ["failure", "cancelled", "timed_out", "action_required"]
            and run.get("status") == "completed"
        ]

        # Re-run each failed check
        for run in failed_runs:
            run_id = run.get("id")
            if run_id:
                try:
                    rerun_endpoint = (
                        f"/repos/{owner}/{repo}/check-runs/{run_id}/rerequest"
                    )
                    await client._request("POST", rerun_endpoint)
                except Exception:
                    # Silently ignore errors - not all checks support re-run
                    pass

        return True
    except Exception:
        # Silently ignore errors - re-running checks is best-effort
        return False


async def create_pr_comment(
    client: GitHubClient,
    owner: str,
    repo: str,
    pr_number: int,
    changes_made: list[str],
) -> None:
    """Create a comment on the PR summarizing the fixes applied.

    Args:
        client: GitHub API client
        owner: Repository owner
        repo: Repository name
        pr_number: PR number
        changes_made: List of changes made (e.g., ["title", "body"])
    """
    try:
        # Build the comment body
        lines = [
            "## 🛠️ Pull Request Fixer",
            "",
            "Automatically fixed pull request metadata:",
        ]

        # Add specific fixes
        if "title" in changes_made:
            lines.append("- Updated pull request title to match commit")
        if "body" in changes_made:
            lines.append(
                "- Updated pull request description to match commit body message"
            )

        lines.extend(
            [
                "",
                "---",
                "*This fix was automatically applied by "
                "[pull-request-fixer](https://github.com/lfreleng-actions/pull-request-fixer)*",
            ]
        )

        comment_body = "\n".join(lines)

        # Create the comment
        endpoint = f"/repos/{owner}/{repo}/issues/{pr_number}/comments"
        data = {"body": comment_body}

        await client._request("POST", endpoint, json=data)

    except Exception:
        # Silently ignore errors - commenting is best-effort
        pass


async def create_file_fix_comment(
    client: GitHubClient,
    owner: str,
    repo: str,
    pr_number: int,
    result: GitHubFixResult,
    command_args: dict[str, Any],
) -> None:
    """Create a comment on the PR summarizing the file fixes applied.

    Args:
        client: GitHub API client
        owner: Repository owner
        repo: Repository name
        pr_number: PR number
        result: GitHubFixResult with file modifications
        command_args: Dictionary of command-line arguments used
    """
    try:
        # Build the command invocation
        cmd_parts = [f"pull-request-fixer {owner}/{repo}"]
        cmd_parts.append("--fix-files")

        if command_args.get("file_pattern"):
            cmd_parts.append(f"--file-pattern '{command_args['file_pattern']}'")
        if command_args.get("search_pattern"):
            cmd_parts.append(
                f"--search-pattern '{command_args['search_pattern']}'"
            )
        if command_args.get("replacement"):
            cmd_parts.append(f"--replacement '{command_args['replacement']}'")
        if command_args.get("remove_lines"):
            cmd_parts.append("--remove-lines")
        if command_args.get("context_start"):
            cmd_parts.append(
                f"--context-start '{command_args['context_start']}'"
            )
        if command_args.get("context_end"):
            cmd_parts.append(f"--context-end '{command_args['context_end']}'")
        if command_args.get("pr_content_only"):
            cmd_parts.append("--pr-content-only")

        command = " \\\n  ".join(cmd_parts)

        # Count total diff lines
        total_diff_lines = sum(
            len(mod.diff.split("\n")) for mod in result.file_modifications
        )

        # Build the comment body
        lines = [
            "## 🛠️ Pull Request Fixer",
            "",
            "**Command run:**",
            "",
            "```bash",
            command,
            "```",
            "",
            "**Automatically fixed files in this pull request:**",
            "",
        ]

        # If total diff is under 40 lines, show full diffs
        if total_diff_lines <= 40:
            for modification in result.file_modifications:
                try:
                    display_path = modification.file_path.name
                except Exception:
                    display_path = str(modification.file_path)

                lines.append(f"🔀 {display_path}")
                lines.append("```diff")
                lines.append(modification.diff)
                lines.append("```")
                lines.append("")
        else:
            # Just show file list
            file_count = len(result.file_modifications)
            lines.append(f"**{file_count} file(s) changed:**")
            lines.append("")

            for modification in result.file_modifications:
                try:
                    display_path = modification.file_path.name
                except Exception:
                    display_path = str(modification.file_path)

                lines.append(f"🔀 {display_path}")

            lines.append("")

        lines.extend(
            [
                "---",
                "*This fix was automatically applied by "
                "[pull-request-fixer](https://github.com/lfreleng-actions/pull-request-fixer)*",
            ]
        )

        comment_body = "\n".join(lines)

        # Create the comment
        endpoint = f"/repos/{owner}/{repo}/issues/{pr_number}/comments"
        data = {"body": comment_body}

        await client._request("POST", endpoint, json=data)

    except Exception:
        # Silently ignore errors - commenting is best-effort
        pass


def cli() -> None:
    """CLI entry point."""
    typer.run(main)


if __name__ == "__main__":
    cli()
