#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 The Linux Foundation

"""Diagnostic script to test GitHub organization access and GraphQL queries.

This script helps debug why an organization might return 0 repositories.
It tests:
1. Token validity and scopes
2. Organization access
3. GraphQL queries
4. Repository enumeration
"""

import asyncio
import json
import os
import sys
from typing import Any

import httpx


async def test_token_validity(token: str) -> tuple[bool, dict[str, Any]]:
    """Test if token is valid and get user info."""
    print("\n" + "=" * 80)
    print("1. Testing Token Validity")
    print("=" * 80)

    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            # Test with REST API
            response = await client.get(
                "https://api.github.com/user", headers=headers
            )
            response.raise_for_status()

            user_data = response.json()
            scopes = response.headers.get("X-OAuth-Scopes", "")

            scope_list = [s.strip() for s in scopes.split(",") if s.strip()]

            print("✓ Token is VALID")
            print(f"  User: {user_data.get('login')}")
            print(f"  Name: {user_data.get('name')}")
            print(f"  Type: {user_data.get('type')}")
            # Avoid logging the raw scope string, which is derived from the
            # authenticated request; report only the non-sensitive count.
            print(f"  Scopes granted: {len(scope_list)}")

            # Check required scopes
            has_repo = "repo" in scope_list
            has_public_repo = "public_repo" in scope_list
            has_read_org = "read:org" in scope_list

            print("\n  Scope Check:")
            print(f"    repo: {'✓' if has_repo else '✗'}")
            print(f"    public_repo: {'✓' if has_public_repo else '✗'}")
            print(f"    read:org: {'✓' if has_read_org else '✗'}")

            if not (has_repo or has_public_repo):
                print(
                    "\n  ⚠️  WARNING: Token lacks 'repo' or 'public_repo' scope"
                )

            if not has_read_org:
                print("  ⚠️  WARNING: Token lacks 'read:org' scope")

            return True, user_data

        except httpx.HTTPStatusError as e:
            print(f"✗ Token is INVALID: {e.response.status_code}")
            print(f"  Response: {e.response.text}")
            return False, {}
        except Exception as e:
            print(f"✗ Error testing token: {e}")
            return False, {}


async def test_org_access_rest(
    token: str, org: str
) -> tuple[bool, dict[str, Any]]:
    """Test organization access via REST API."""
    print("\n" + "=" * 80)
    print(f"2. Testing Organization Access (REST API): {org}")
    print("=" * 80)

    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            response = await client.get(
                f"https://api.github.com/orgs/{org}", headers=headers
            )
            response.raise_for_status()

            org_data = response.json()
            print("✓ Organization FOUND via REST API")
            print(f"  Name: {org_data.get('name')}")
            print(f"  Login: {org_data.get('login')}")
            print(f"  Public Repos: {org_data.get('public_repos')}")
            print(
                f"  Total Private Repos: {org_data.get('total_private_repos')}"
            )

            return True, org_data

        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                print("✗ Organization NOT FOUND (404)")
                print("  This could mean:")
                print("    1. Organization name is incorrect")
                print("    2. Token user doesn't have access to this org")
                print("    3. Organization doesn't exist")
            else:
                print(f"✗ HTTP Error: {e.response.status_code}")
                print(f"  Response: {e.response.text}")
            return False, {}
        except Exception as e:
            print(f"✗ Error: {e}")
            return False, {}


async def test_org_graphql_viewer(token: str) -> bool:
    """Test GraphQL with viewer query."""
    print("\n" + "=" * 80)
    print("3. Testing GraphQL API (Viewer Query)")
    print("=" * 80)

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    query = """
    query {
      viewer {
        login
        name
        organizations(first: 10) {
          totalCount
          nodes {
            login
            name
          }
        }
      }
    }
    """

    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            response = await client.post(
                "https://api.github.com/graphql",
                headers=headers,
                json={"query": query},
            )
            response.raise_for_status()

            result = response.json()

            if "errors" in result:
                print("✗ GraphQL Errors:")
                for error in result["errors"]:
                    print(f"  - {error.get('message')}")
                return False

            data = result.get("data", {})
            viewer = data.get("viewer", {})
            orgs = viewer.get("organizations", {})

            print("✓ GraphQL API working")
            print(f"  User: {viewer.get('login')}")
            print(f"  Total Orgs: {orgs.get('totalCount', 0)}")

            if orgs.get("nodes"):
                print("\n  Accessible Organizations:")
                for org in orgs.get("nodes", []):
                    print(f"    - {org.get('login')} ({org.get('name')})")

            return True

        except Exception as e:
            print(f"✗ Error: {e}")
            return False


async def test_org_graphql_direct(
    token: str, org: str
) -> tuple[bool, dict[str, Any]]:
    """Test organization access via GraphQL."""
    print("\n" + "=" * 80)
    print(f"4. Testing Organization Access (GraphQL): {org}")
    print("=" * 80)

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    query = """
    query($org: String!) {
      organization(login: $org) {
        login
        name
        repositories(first: 10, orderBy: { field: NAME, direction: ASC }) {
          totalCount
          nodes {
            nameWithOwner
            isArchived
            isPrivate
            visibility
          }
        }
      }
    }
    """

    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            response = await client.post(
                "https://api.github.com/graphql",
                headers=headers,
                json={"query": query, "variables": {"org": org}},
            )
            response.raise_for_status()

            result = response.json()

            if "errors" in result:
                print("✗ GraphQL Errors:")
                for error in result["errors"]:
                    print(f"  - {error.get('message')}")
                    print(f"    Type: {error.get('type')}")
                    if "path" in error:
                        print(f"    Path: {error.get('path')}")
                return False, {}

            data = result.get("data", {})
            org_data = data.get("organization")

            if org_data is None:
                print("✗ Organization data is NULL")
                print("  This means:")
                print("    1. Organization doesn't exist, OR")
                print("    2. Token doesn't have access to this organization")
                print("\n  Full response:")
                print(f"  {json.dumps(result, indent=2)}")
                return False, {}

            repos = org_data.get("repositories", {})
            total_repos = repos.get("totalCount", 0)
            repo_nodes = repos.get("nodes", [])

            print("✓ Organization FOUND via GraphQL")
            print(f"  Name: {org_data.get('name')}")
            print(f"  Login: {org_data.get('login')}")
            print(f"  Total Repositories: {total_repos}")

            if repo_nodes:
                print(f"\n  First {len(repo_nodes)} Repositories:")
                for repo in repo_nodes:
                    archived = " [ARCHIVED]" if repo.get("isArchived") else ""
                    private = (
                        " [PRIVATE]" if repo.get("isPrivate") else " [PUBLIC]"
                    )
                    visibility = repo.get("visibility", "UNKNOWN")
                    print(
                        f"    - {repo.get('nameWithOwner')}{private}{archived} ({visibility})"
                    )

            # Count non-archived repos
            non_archived = [r for r in repo_nodes if not r.get("isArchived")]
            if len(non_archived) < len(repo_nodes):
                print(
                    f"\n  Note: {len(repo_nodes) - len(non_archived)} archived repos"
                )

            return True, org_data

        except Exception as e:
            print(f"✗ Error: {e}")
            import traceback

            print(traceback.format_exc())
            return False, {}


async def test_org_repos_only_query(token: str, org: str) -> bool:
    """Test the exact ORG_REPOS_ONLY query used by the tool."""
    print("\n" + "=" * 80)
    print(f"5. Testing ORG_REPOS_ONLY Query: {org}")
    print("=" * 80)

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    # This is the EXACT query from graphql_queries.py
    query = """
query($org: String!, $reposCursor: String) {
  organization(login: $org) {
    repositories(first: 100, after: $reposCursor, orderBy: { field: NAME, direction: ASC }) {
      pageInfo {
        hasNextPage
        endCursor
      }
      nodes {
        nameWithOwner
        isArchived
      }
    }
  }
}
"""

    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            response = await client.post(
                "https://api.github.com/graphql",
                headers=headers,
                json={
                    "query": query,
                    "variables": {"org": org, "reposCursor": None},
                },
            )
            response.raise_for_status()

            result = response.json()

            print("  Full Response:")
            print(f"  {json.dumps(result, indent=2)}")

            if "errors" in result:
                print("\n✗ GraphQL Errors:")
                for error in result["errors"]:
                    print(f"  - {error.get('message')}")
                return False

            data = result.get("data", {})
            org_data = data.get("organization")

            if org_data is None:
                print("\n✗ organization field is NULL")
                return False

            repos = org_data.get("repositories", {})
            nodes = repos.get("nodes", [])
            page_info = repos.get("pageInfo", {})

            print("\n✓ Query successful")
            print(f"  Total nodes returned: {len(nodes)}")
            print(f"  Has next page: {page_info.get('hasNextPage', False)}")

            if nodes:
                print("\n  Repositories:")
                for repo in nodes[:10]:  # Show first 10
                    archived = " [ARCHIVED]" if repo.get("isArchived") else ""
                    print(f"    - {repo.get('nameWithOwner')}{archived}")

                if len(nodes) > 10:
                    print(f"    ... and {len(nodes) - 10} more")

                # Count non-archived
                non_archived = [r for r in nodes if not r.get("isArchived")]
                print(f"\n  Non-archived repos: {len(non_archived)}")
                print(f"  Archived repos: {len(nodes) - len(non_archived)}")
            else:
                print("\n  No repositories returned!")

            return True

        except Exception as e:
            print(f"✗ Error: {e}")
            import traceback

            print(traceback.format_exc())
            return False


async def main() -> None:
    """Run all diagnostic tests."""
    if len(sys.argv) < 2:
        print("Usage: python diagnose_org_access.py <organization-name>")
        print("\nExample: python diagnose_org_access.py lfreleng-actions")
        sys.exit(1)

    org = sys.argv[1]
    token = os.getenv("GITHUB_TOKEN")

    if not token:
        print("ERROR: GITHUB_TOKEN environment variable not set")
        sys.exit(1)

    print("=" * 80)
    print("GitHub Organization Access Diagnostic Tool")
    print("=" * 80)
    print(f"Organization: {org}")
    # Never log token contents (even partially); report only its length.
    print(f"Token: provided ({len(token)} characters)")

    # Run all tests
    token_valid, user_data = await test_token_validity(token)
    if not token_valid:
        print("\n❌ Token is invalid. Cannot proceed.")
        sys.exit(1)

    await test_org_graphql_viewer(token)
    await test_org_access_rest(token, org)
    graphql_ok, graphql_data = await test_org_graphql_direct(token, org)
    await test_org_repos_only_query(token, org)

    print("\n" + "=" * 80)
    print("Summary")
    print("=" * 80)

    if graphql_ok:
        print("✓ All checks passed!")
        print(f"\n  The tool should be able to access '{org}'")
        print("  If you're still seeing 0 repositories, the repos might be:")
        print("    1. All archived (tool filters these out)")
        print("    2. Have no open pull requests")
    else:
        print("✗ Organization access failed")
        print("\n  Possible solutions:")
        print("    1. Verify organization name is correct (case-sensitive)")
        print("    2. Ensure token has 'read:org' scope")
        print("    3. Confirm token user is a member of the organization")
        print(
            f"    4. Check if organization exists at: https://github.com/{org}"
        )


if __name__ == "__main__":
    asyncio.run(main())
