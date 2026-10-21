"""Check that a Jenkins API token works against ci.ros2.org and/or build.ros2.org.

Reads credentials from the environment and prints only HTTP status and the identity the
server reports. It never prints or stores the token.

    JENKINS_CI_USER / JENKINS_CI_TOKEN        -> https://ci.ros2.org
    JENKINS_BUILD_USER / JENKINS_BUILD_TOKEN  -> https://build.ros2.org
"""

import base64
import json
import os
import sys
import urllib.error
import urllib.request

SERVERS = {
    'ci': ('https://ci.ros2.org', 'JENKINS_CI_USER', 'JENKINS_CI_TOKEN'),
    'build': ('https://build.ros2.org', 'JENKINS_BUILD_USER', 'JENKINS_BUILD_TOKEN'),
}


def get(url, user=None, token=None):
    request = urllib.request.Request(url)
    if user and token:
        basic = base64.b64encode(f'{user}:{token}'.encode()).decode()
        request.add_header('Authorization', f'Basic {basic}')
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as e:
        return e.code, b''
    except urllib.error.URLError as e:
        return None, str(e.reason).encode()


def check(name, base, user_var, token_var):
    user, token = os.environ.get(user_var), os.environ.get(token_var)
    if not (user and token):
        print(f'{name}: {user_var}/{token_var} not set, skipping')
        return False
    anonymous, _ = get(f'{base}/api/json?tree=mode')
    status, body = get(f'{base}/me/api/json?tree=id,fullName', user, token)
    print(f'{name}: anonymous GET /api/json -> {anonymous}')
    print(f'{name}: token GET /me/api/json -> {status}')
    if status == 200:
        me = json.loads(body)
        print(f'{name}: authenticated as id={me.get("id")!r} fullName={me.get("fullName")!r}')
    return status == 200


if __name__ == '__main__':
    results = [check(name, *spec) for name, spec in SERVERS.items()]
    sys.exit(0 if any(results) else 1)
