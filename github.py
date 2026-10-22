"""Look up GitHub commits between two SHAs, with local caching so each range is fetched once.

    from github import compare
    compare('https://github.com/ros2/rclcpp', 'abc123...', 'def456...')

Unauthenticated calls are capped at 60/hr; set GITHUB_TOKEN (no scopes needed for public repos,
or "Public repositories (read-only)" on a fine-grained PAT) to raise that to 5000/hr.
"""

import json
import os
import re
import time
import urllib.error
import urllib.request

TOKEN = os.environ.get('GITHUB_TOKEN', '')
CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'cache', 'github')
API = 'https://api.github.com'
REPO_URL = re.compile(r'github\.com[:/]+([^/]+)/([^/]+?)(?:\.git)?/?$')


def compare(url, old, new):
    """[{sha, author, message}] for commits strictly after `old` up to and including `new`, oldest
    first, or None if url isn't github.com, the range can't be resolved, or the API call fails."""
    m = REPO_URL.search(url or '')
    if not m or old == new:
        return None
    owner, repo = m.group(1), m.group(2)
    os.makedirs(CACHE, exist_ok=True)
    path = os.path.join(CACHE, f'{owner}__{repo}__{old[:12]}__{new[:12]}.json')
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    headers = {'Accept': 'application/vnd.github+json'}
    if TOKEN:
        headers['Authorization'] = f'Bearer {TOKEN}'
    request = urllib.request.Request(
        f'{API}/repos/{owner}/{repo}/compare/{old}...{new}', headers=headers)
    data = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                data = json.load(response)
            break
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors='replace')
            if e.code == 403 and 'rate limit' in body.lower():
                print(f'  GitHub rate limited; skipping {owner}/{repo} {old[:8]}..{new[:8]}')
                return None
            if e.code in (404, 422):
                return None  # unknown range: force-pushed branch, rewritten history, etc.
            if attempt == 2:
                raise
            time.sleep(2 ** attempt)
        except urllib.error.URLError:
            if attempt == 2:
                raise
            time.sleep(2 ** attempt)
    if data is None:
        return None
    commits = [{'sha': c['sha'][:12], 'author': ((c.get('commit') or {}).get('author') or {}).get('name', '?'),
               'message': (c.get('commit') or {}).get('message', '').split('\n', 1)[0][:200]}
              for c in data.get('commits', [])]
    with open(path, 'w') as f:
        json.dump(commits, f)
    return commits
