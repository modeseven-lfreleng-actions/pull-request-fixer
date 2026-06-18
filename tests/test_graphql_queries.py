# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 The Linux Foundation

"""Tests for GraphQL queries and GitHub client interaction."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from tenacity import RetryError

from pull_request_fixer.github_client import GitHubClient
from pull_request_fixer.graphql_queries import (
    BULK_PR_COMMITS_TEMPLATE,
    ORG_REPOS_ONLY,
    ORG_REPOS_WITH_PRS,
    PR_COMMIT_FRAGMENT,
    PR_FIRST_COMMIT,
    PR_WITH_STATUS,
    REPO_OPEN_PRS_PAGE,
)
from pull_request_fixer.pr_scanner import PRScanner


class TestGraphQLQueryStructure:
    """Test that GraphQL queries have the correct structure."""

    def test_org_repos_only_query_parameters(self):
        """Test ORG_REPOS_ONLY has correct parameters."""
        assert "$org: String!" in ORG_REPOS_ONLY
        assert "$reposCursor: String" in ORG_REPOS_ONLY
        assert "organization(login: $org)" in ORG_REPOS_ONLY
        assert "after: $reposCursor" in ORG_REPOS_ONLY

    def test_org_repos_with_prs_query_parameters(self):
        """Test ORG_REPOS_WITH_PRS has correct parameters."""
        assert "$org: String!" in ORG_REPOS_WITH_PRS
        assert "$cursor: String" in ORG_REPOS_WITH_PRS
        assert "$prsPageSize: Int!" in ORG_REPOS_WITH_PRS
        assert "$contextsPageSize: Int!" in ORG_REPOS_WITH_PRS
        assert "organization(login: $org)" in ORG_REPOS_WITH_PRS

    def test_repo_open_prs_page_query_parameters(self):
        """Test REPO_OPEN_PRS_PAGE has correct parameters."""
        assert "$owner: String!" in REPO_OPEN_PRS_PAGE
        assert "$name: String!" in REPO_OPEN_PRS_PAGE
        assert "$prsCursor: String" in REPO_OPEN_PRS_PAGE
        assert "$prsPageSize: Int!" in REPO_OPEN_PRS_PAGE
        assert "repository(owner: $owner, name: $name)" in REPO_OPEN_PRS_PAGE

    def test_pr_with_status_query_parameters(self):
        """Test PR_WITH_STATUS has correct parameters."""
        assert "$owner: String!" in PR_WITH_STATUS
        assert "$name: String!" in PR_WITH_STATUS
        assert "$number: Int!" in PR_WITH_STATUS
        assert "repository(owner: $owner, name: $name)" in PR_WITH_STATUS
        assert "pullRequest(number: $number)" in PR_WITH_STATUS

    def test_pr_first_commit_query_parameters(self):
        """Test PR_FIRST_COMMIT has correct parameters."""
        assert "$owner: String!" in PR_FIRST_COMMIT
        assert "$name: String!" in PR_FIRST_COMMIT
        assert "$number: Int!" in PR_FIRST_COMMIT
        assert "commits(first: 1)" in PR_FIRST_COMMIT

    def test_bulk_pr_commits_template_structure(self):
        """Test BULK_PR_COMMITS_TEMPLATE has correct structure."""
        assert "$owner: String!" in BULK_PR_COMMITS_TEMPLATE
        assert "$name: String!" in BULK_PR_COMMITS_TEMPLATE
        assert "{pr_queries}" in BULK_PR_COMMITS_TEMPLATE

    def test_pr_commit_fragment_structure(self):
        """Test PR_COMMIT_FRAGMENT has correct placeholders."""
        assert "pr{number}:" in PR_COMMIT_FRAGMENT
        assert "pullRequest(number: {number})" in PR_COMMIT_FRAGMENT


@pytest.mark.asyncio
class TestGitHubClientGraphQL:
    """Test GitHub client GraphQL functionality."""

    async def test_graphql_returns_data_only(self):
        """Test that graphql() method returns only the data part of response."""
        client = GitHubClient(token="test-token")

        # Mock the _graphql_request to return data
        mock_data = {
            "organization": {"repositories": {"totalCount": 5, "nodes": []}}
        }

        with patch.object(client, "_graphql_request", return_value=mock_data):
            result = await client.graphql("query { test }")

            # Should return the data directly, not wrapped in {"data": ...}
            assert "organization" in result
            assert "data" not in result
            assert result["organization"]["repositories"]["totalCount"] == 5

    async def test_graphql_handles_errors(self):
        """Test that graphql() raises exception on GraphQL errors."""
        client = GitHubClient(token="test-token")

        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client = MagicMock()
            mock_client_class.return_value.__aenter__.return_value = mock_client

            # Mock response with GraphQL errors
            mock_response = MagicMock()
            mock_response.json.return_value = {
                "errors": [{"message": "Not found"}],
                "data": None,
            }
            mock_response.raise_for_status = MagicMock()
            mock_client.post = AsyncMock(return_value=mock_response)

            # The retry logic wraps the FileAccessError in a RetryError
            with pytest.raises(RetryError) as exc_info:
                await client.graphql("query { test }")

            # Verify the underlying error message contains GraphQL errors
            # Access the last attempt's exception through the RetryError
            last_exception = exc_info.value.last_attempt.exception()
            assert "GraphQL errors" in str(last_exception)


@pytest.mark.asyncio
class TestPRScannerGraphQLUsage:
    """Test PR scanner uses GraphQL queries correctly."""

    async def test_count_org_repositories_uses_correct_params(self):
        """Test _count_org_repositories uses correct GraphQL parameters."""
        mock_client = AsyncMock(spec=GitHubClient)
        mock_client.graphql = AsyncMock(
            return_value={
                "organization": {
                    "repositories": {
                        "totalCount": 10,
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                        "nodes": [],
                    }
                }
            }
        )

        scanner = PRScanner(mock_client, progress_tracker=None)
        result = await scanner._count_org_repositories("test-org")

        # Verify correct parameters were used
        mock_client.graphql.assert_called_once()
        call_args = mock_client.graphql.call_args
        assert call_args[0][0] == ORG_REPOS_ONLY
        assert call_args[1]["variables"]["org"] == "test-org"
        assert "reposCursor" in call_args[1]["variables"]

        # Verify correct result
        assert result == 10

    async def test_count_org_repositories_handles_no_data(self):
        """Test _count_org_repositories handles missing organization data."""
        mock_client = AsyncMock(spec=GitHubClient)
        mock_client.graphql = AsyncMock(return_value={})

        scanner = PRScanner(mock_client, progress_tracker=None)
        result = await scanner._count_org_repositories("nonexistent-org")

        assert result == 0

    async def test_fetch_repo_prs_uses_correct_params(self):
        """Test _fetch_repo_prs_first_page uses correct parameters."""
        mock_client = AsyncMock(spec=GitHubClient)
        mock_client.graphql = AsyncMock(
            return_value={
                "repository": {
                    "pullRequests": {
                        "nodes": [{"number": 1, "title": "Test PR"}],
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                    }
                }
            }
        )

        scanner = PRScanner(mock_client, progress_tracker=None)
        pr_nodes, page_info = await scanner._fetch_repo_prs_first_page(
            "owner", "repo"
        )

        # Verify correct parameters
        call_args = mock_client.graphql.call_args
        assert call_args[0][0] == REPO_OPEN_PRS_PAGE
        variables = call_args[1]["variables"]
        assert variables["owner"] == "owner"
        assert variables["name"] == "repo"
        assert "prsPageSize" in variables
        assert "filesPageSize" in variables
        assert "commentsPageSize" in variables
        assert "contextsPageSize" in variables

        # Verify results
        assert len(pr_nodes) == 1
        assert pr_nodes[0]["number"] == 1


@pytest.mark.asyncio
@pytest.mark.skip(reason="bulk_check_pr_titles function not implemented")
class TestBulkPRCommitFetching:
    """Test bulk PR commit fetching functionality."""

    async def test_bulk_query_generation(self):
        """Test that bulk query is generated correctly."""
        # from pull_request_fixer.cli import bulk_check_pr_titles

        mock_client = AsyncMock(spec=GitHubClient)

        # Mock GraphQL response with multiple PRs
        mock_client.graphql = AsyncMock(
            return_value={
                "repository": {
                    "pr1": {
                        "number": 1,
                        "title": "Wrong title",
                        "commits": {
                            "nodes": [
                                {
                                    "commit": {
                                        "message": "fix: correct title\n\nBody text",
                                        "messageHeadline": "fix: correct title",
                                        "messageBody": "Body text",
                                    }
                                }
                            ]
                        },
                    },
                    "pr2": {
                        "number": 2,
                        "title": "Matching title",
                        "commits": {
                            "nodes": [
                                {
                                    "commit": {
                                        "message": "Matching title\n\nBody",
                                        "messageHeadline": "Matching title",
                                        "messageBody": "Body",
                                    }
                                }
                            ]
                        },
                    },
                }
            }
        )

        # pr_list = [
        #     {"number": 1, "title": "Wrong title"},
        #     {"number": 2, "title": "Matching title"},
        # ]
        #
        # result = await bulk_check_pr_titles(
        #     mock_client, "owner", "repo", pr_list, batch_size=10
        # )
        #
        # # Should only return PR with mismatched title
        # assert len(result) == 1
        # assert result[0][0]["number"] == 1
        # assert result[0][1] == "Wrong title"  # current title
        # assert result[0][2] == "fix: correct title"  # expected title
        #
        # # Verify GraphQL was called with aliased query
        # call_args = mock_client.graphql.call_args
        # query = call_args[0][0]
        # assert "pr1:" in query
        # assert "pr2:" in query
        # assert "pullRequest(number: 1)" in query
        # assert "pullRequest(number: 2)" in query

    async def test_bulk_query_batching(self):
        """Test that large PR lists are batched correctly."""
        # from pull_request_fixer.cli import bulk_check_pr_titles

        mock_client = AsyncMock(spec=GitHubClient)
        mock_client.graphql = AsyncMock(return_value={"repository": {}})

        # # Create 25 PRs - should result in 3 batches (10, 10, 5)
        # pr_list = [{"number": i, "title": f"PR {i}"} for i in range(1, 26)]
        #
        # await bulk_check_pr_titles(
        #     mock_client, "owner", "repo", pr_list, batch_size=10
        # )
        #
        # # Verify GraphQL was called 3 times (3 batches)
        # assert mock_client.graphql.call_count == 3


@pytest.mark.asyncio
class TestIntegrationScenarios:
    """Integration tests for complete scanning scenarios."""

    async def test_scan_organization_complete_flow(self):
        """Test complete organization scanning flow."""
        mock_client = AsyncMock(spec=GitHubClient)

        # Mock repository count
        count_response = {
            "organization": {
                "repositories": {
                    "totalCount": 2,
                    "nodes": [
                        {"nameWithOwner": "org/repo1", "isArchived": False},
                        {"nameWithOwner": "org/repo2", "isArchived": False},
                    ],
                }
            }
        }

        # Mock repositories with PRs
        repos_with_prs_response = {
            "organization": {
                "repositories": {
                    "nodes": [
                        {
                            "nameWithOwner": "org/repo1",
                            "name": "repo1",
                            "owner": {"login": "org"},
                            "pullRequests": {"totalCount": 1},
                        }
                    ],
                    "pageInfo": {"hasNextPage": False},
                }
            }
        }

        # Mock PR details
        pr_details_response = {
            "repository": {
                "pullRequests": {
                    "nodes": [
                        {
                            "number": 1,
                            "title": "Test PR",
                            "isDraft": False,
                            "body": "Test body",
                        }
                    ],
                    "pageInfo": {"hasNextPage": False},
                }
            }
        }

        mock_client.graphql = AsyncMock(
            side_effect=[
                count_response,
                repos_with_prs_response,
                pr_details_response,
            ]
        )

        scanner = PRScanner(mock_client, progress_tracker=None)

        prs = []
        async for owner, repo, pr_data in scanner.scan_organization("org"):
            prs.append((owner, repo, pr_data))

        # Verify we got the PR
        assert len(prs) == 1
        assert prs[0][0] == "org"
        assert prs[0][1] == "repo1"
        assert prs[0][2]["number"] == 1

    async def test_empty_organization(self):
        """Test scanning an organization with no repositories."""
        mock_client = AsyncMock(spec=GitHubClient)
        mock_client.graphql = AsyncMock(
            return_value={
                "organization": {"repositories": {"totalCount": 0, "nodes": []}}
            }
        )

        scanner = PRScanner(mock_client, progress_tracker=None)

        prs = []
        async for owner, repo, pr_data in scanner.scan_organization(
            "empty-org"
        ):
            prs.append((owner, repo, pr_data))

        assert len(prs) == 0

    async def test_organization_not_found(self):
        """Test handling of non-existent organization."""
        mock_client = AsyncMock(spec=GitHubClient)
        mock_client.graphql = AsyncMock(return_value={})

        scanner = PRScanner(mock_client, progress_tracker=None)

        prs = []
        async for owner, repo, pr_data in scanner.scan_organization(
            "nonexistent"
        ):
            prs.append((owner, repo, pr_data))

        assert len(prs) == 0


class TestCommitMessageParsing:
    """Test commit message parsing for title extraction."""

    def test_parse_simple_message(self):
        """Test parsing a simple commit message."""
        from pull_request_fixer.cli import parse_commit_message

        message = "fix: resolve bug\n\nThis fixes the issue."
        subject, body = parse_commit_message(message)

        assert subject == "fix: resolve bug"
        assert "This fixes the issue" in body

    def test_parse_message_with_trailers(self):
        """Test parsing message with trailers (should be removed from body)."""
        from pull_request_fixer.cli import parse_commit_message

        message = """feat: add new feature

This is the body.

Signed-off-by: User <user@example.com>
Co-authored-by: Other <other@example.com>"""

        subject, body = parse_commit_message(message)

        assert subject == "feat: add new feature"
        assert "This is the body" in body
        assert "Signed-off-by" not in body
        assert "Co-authored-by" not in body

    def test_parse_message_subject_only(self):
        """Test parsing a commit with only a subject line."""
        from pull_request_fixer.cli import parse_commit_message

        message = "chore: update dependencies"
        subject, body = parse_commit_message(message)

        assert subject == "chore: update dependencies"
        assert body == ""


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
