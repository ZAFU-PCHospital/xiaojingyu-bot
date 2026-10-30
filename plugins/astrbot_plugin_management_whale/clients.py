import json
import time
from pathlib import Path

import aiohttp

from .core import REPO


class RemoteError(Exception):
    def __init__(self, status=0):
        self.status = status


class Services:
    def __init__(self, config):
        self.config = config
        self.token = None
        self.token_until = 0

    def _private(self):
        path = self.config.get('private_config_path', '')
        if not path or not Path(path).is_file():
            return {}
        return json.loads(Path(path).read_text())

    def ready(self, kind):
        c = self._private()
        if kind == 'issue':
            return all(c.get(x) for x in ('github_app_id', 'github_installation_id', 'github_private_key_path'))
        return bool(c.get('website_token') and c.get('website_writes_enabled') is True)

    async def request(self, method, url, *, headers=None, params=None, body=None):
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=25)) as session:
            async with session.request(method, url, headers=headers, params=params, json=body, allow_redirects=False) as r:
                if not 200 <= r.status < 300:
                    raise RemoteError(r.status)
                chunks = []
                size = 0
                async for chunk in r.content.iter_chunked(16384):
                    size += len(chunk)
                    if size > 1024 * 1024:
                        raise RemoteError()
                    chunks.append(chunk)
                data = b''.join(chunks)
                try:
                    return json.loads(data)
                except (ValueError, UnicodeError):
                    raise RemoteError() from None

    async def github_headers(self, required=False):
        headers = {'Accept': 'application/vnd.github+json', 'X-GitHub-Api-Version': '2026-03-10', 'User-Agent': 'pc-hospital-management-whale'}
        if not self.ready('issue'):
            if required:
                raise ValueError('GitHub App尚未配置，草稿保留，未执行创建。')
            return headers
        if self.token_until < time.time() + 120:
            import jwt
            c = self._private()
            key = Path(c['github_private_key_path']).read_text()
            now = int(time.time())
            signed = jwt.encode({'iat': now - 60, 'exp': now + 540, 'iss': str(c['github_app_id'])}, key, algorithm='RS256')
            result = await self.request('POST', f"https://api.github.com/app/installations/{int(c['github_installation_id'])}/access_tokens",
                headers={**headers, 'Authorization': 'Bearer ' + signed},
                body={'repositories': ['zafu-pchospital-web'], 'permissions': {'issues': 'write', 'metadata': 'read'}})
            self.token = result['token']
            self.token_until = time.time() + 3000
        return {**headers, 'Authorization': 'Bearer ' + self.token}

    async def search_issues(self, query):
        # Search terms cannot remove the fixed repo/is:issue qualifiers.
        import re
        words = re.findall(r'[\w\u4e00-\u9fff-]+', query)[:16]
        q = f'repo:{REPO} is:issue ' + ' '.join(words)
        result = await self.request('GET', 'https://api.github.com/search/issues', headers=await self.github_headers(), params={'q': q, 'per_page': 6})
        return [{'number': x['number'], 'title': x['title'], 'state': x['state'], 'url': x['html_url']} for x in result.get('items', [])]

    async def activities(self):
        r = await self.request('GET', 'https://pczafu.cn/api/v1/repair-activities')
        data = r.get('data')
        rows = data if isinstance(data, list) else (data or {}).get('items', [])
        return [{k: row[k] for k in ('id', 'title', 'capacity', 'activityAt', 'signupOpensAt', 'signupClosesAt', 'status', 'registeredCount', 'remaining') if k in row} for row in rows[:30]]

    def website_headers(self, origin, actor):
        c = self._private()
        if not self.ready('activity_create'):
            raise ValueError('官网机器写接口尚未启用，草稿保留，未执行。')
        return {'Authorization': 'Bearer ' + c['website_token'], 'X-Management-Origin': origin, 'X-Management-Actor': actor}

    async def editable_activity(self, activity_id, origin, actor):
        # Narrow machine route: no registrant records, no admin Cookie.
        from .core import UUID_RE
        if not UUID_RE.fullmatch(activity_id):
            raise ValueError('活动ID格式不正确。')
        r = await self.request('GET', f'https://pczafu.cn/api/v1/bot/repair-activities/{activity_id}', headers=self.website_headers(origin, actor))
        return r['data']

    async def execute(self, d):
        p = d['payload']
        if d['kind'] == 'issue':
            result = await self.request('POST', f'https://api.github.com/repos/{REPO}/issues', headers=await self.github_headers(True),
                body={**p, 'body': p['body'] + f"\n\n<!-- management-whale:{d['operation_id']} -->"})
            return {'url': result['html_url'], 'number': result['number']}
        headers = {**self.website_headers(d['origin'], d['confirmed_by']), 'Idempotency-Key': d['operation_id']}
        if d['kind'] == 'activity_create':
            from .announcements import ACTIVITY_FIELDS
            r = await self.request('POST', 'https://pczafu.cn/api/v1/bot/repair-activities', headers=headers,
                                   body={k: p[k] for k in ACTIVITY_FIELDS})
        else:
            headers['If-Match'] = p['expected_version']
            r = await self.request('PATCH', f"https://pczafu.cn/api/v1/bot/repair-activities/{p['activity_id']}", headers=headers,
                body={k: p[k] for k in ('title', 'capacity') if k in p})
        return {'url': 'https://pczafu.cn/repair-activities/' + r['data']['id'], 'id': r['data']['id'],
                'activity': {k: r['data'][k] for k in ('title', 'capacity', 'activityAt', 'signupOpensAt', 'signupClosesAt') if k in r['data']}}

    async def reconcile(self, d):
        if d['kind'] == 'issue':
            headers = await self.github_headers(True)
            marker = f"<!-- management-whale:{d['operation_id']} -->"
            # GET list avoids depending on asynchronous GitHub search indexing.
            for page in range(1, 4):
                rows = await self.request('GET', f'https://api.github.com/repos/{REPO}/issues', headers=headers,
                    params={'state': 'all', 'sort': 'created', 'direction': 'desc', 'per_page': 100, 'page': page})
                matches = [x for x in rows if 'pull_request' not in x and marker in (x.get('body') or '')]
                if matches:
                    if len(matches) != 1:
                        return None
                    return {'url': matches[0]['html_url'], 'number': matches[0]['number']}
                if len(rows) < 100:
                    break
            return None  # Not found is not proof of non-execution; never auto-POST again.
        r = await self.request('GET', 'https://pczafu.cn/api/v1/bot/operations/' + d['operation_id'], headers=self.website_headers(d['origin'], d['confirmed_by']))
        if r.get('data', {}).get('state') == 'succeeded':
            return r['data']['result']
        return None
