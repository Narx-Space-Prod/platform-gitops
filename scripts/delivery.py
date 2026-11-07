#!/usr/bin/env python3
"""Immutable release bundles, verified intake, reviewed promotion and Git rollback.

Only Python's standard library. Never execute files from a candidate PR in the
privileged verifier. All external commands use argument arrays, never shell=True.
"""
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import time

ROOT = Path(__file__).resolve().parents[1]
COMPONENTS = {'frontend': 'commerce-frontend', 'catalogue': 'commerce-catalogue',
              'orders': 'commerce-orders', 'cart': 'commerce-orders'}
ALIASES = {'frontend': 'frontend', 'catalogue': 'catalogue', 'orders': 'orders',
           'cart': 'cart', 'publisher': 'orders', 'worker': 'orders'}
ENVIRONMENTS = ('dev', 'qa', 'production')
PREDICATE = 'https://narx.space/attestations/release/v1'
# Positive results live only for this Python invocation, never across CI runs.
_VERIFIED_IMAGES = set()


def run(*args, input=None):
    return subprocess.check_output(args, input=input, text=True).strip()


def api(path):
    return json.loads(run('gh', 'api', path))


def require(value, message):
    if not value:
        raise ValueError(message)


def read(path):
    return json.loads(Path(path).read_text())


def write(path, value):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(value, indent=2, sort_keys=True) + '\n')


def validate_review_scope(files, author, head_repository):
    automatic = {'environments/dev/release.json', 'environments/qa/release.json'}
    paths = {entry['filename'] for entry in files}
    if paths and paths <= automatic:
        require(len(files) == 1, 'automatic PR must change exactly one environment release')
        require(author.get('login') == 'narx-local-commerce-release[bot]'
                and author.get('id') == 352511280 and author.get('type') == 'Bot',
                'automatic release PR must originate from the dedicated release App')
        require(head_repository == 'Narx-Space-Prod/platform-gitops', 'automatic release branch must be internal')
        require(files[0].get('status') == 'modified' and not files[0].get('previous_filename'),
                'automatic PR may only modify existing release state')


def facts_valid(fact, component=None):
    require(isinstance(fact, dict), 'release fact must be an object')
    require(set(fact) == {'component', 'image', 'digest', 'repository', 'commit', 'runId'}, 'unexpected release fact fields')
    name = fact['component']
    require(name in COMPONENTS and (component is None or name == component), 'unexpected component')
    require(fact['repository'] == 'Narx-Space-Prod/' + COMPONENTS[name], 'unexpected source repository')
    require(isinstance(fact['commit'], str) and re.fullmatch('[a-f0-9]{40}', fact['commit']), 'invalid source commit')
    require(type(fact['runId']) is int and fact['runId'] > 0, 'invalid workflow run')
    require(isinstance(fact['digest'], str) and re.fullmatch('sha256:[a-f0-9]{64}', fact['digest']), 'mutable or invalid digest')
    require(fact['image'] == 'ghcr.io/narx-space-prod/commerce-' + name, 'unexpected registry or image')
    return fact


def release_id(components):
    return hashlib.sha256(json.dumps(components, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def bundle(components, previous=None):
    require(set(components) == set(COMPONENTS), 'a deployable release requires all four components')
    for name, fact in components.items():
        facts_valid(fact, name)
    result = {'release': {'ready': True, 'id': release_id(components), 'components': components, 'previous': previous}}
    for alias, component in ALIASES.items():
        fact = components[component]
        result[alias] = {'enabled': True, 'image': {'repository': fact['image'], 'digest': fact['digest']},
                         'podAnnotations': {'delivery.narx.space/release': result['release']['id'],
                                            'delivery.narx.space/source-commit': fact['commit']}}
    return result


def validate(value):
    require(isinstance(value, dict) and set(value) == {'release', *ALIASES}, 'unexpected release bundle fields')
    meta = value['release']
    require(isinstance(meta, dict) and set(meta) == {'ready', 'id', 'components', 'previous'}, 'unexpected release metadata')
    if meta['ready'] is False:
        require(meta == {'ready': False, 'id': '', 'components': {}, 'previous': None}, 'invalid bootstrap state')
        require(all(value[a] == {'enabled': False} for a in ALIASES), 'bootstrap cannot deploy application images')
        return False
    require(meta['ready'] is True, 'ready must be boolean')
    expected = bundle(meta['components'], meta['previous'])
    require(value == expected, 'rendered images/annotations do not match canonical release facts')
    if meta['previous'] is not None:
        previous = meta['previous']
        require(set(previous) == {'components', 'gitopsCommit', 'environment'}, 'invalid rollback evidence')
        require(previous['environment'] in ENVIRONMENTS and re.fullmatch('[a-f0-9]{40}', previous['gitopsCommit']), 'invalid previous revision')
        bundle(previous['components'])
    return True


def successful_run(fact):
    data = api(f"repos/{fact['repository']}/actions/runs/{fact['runId']}")
    return (data.get('conclusion') == 'success' and data.get('event') == 'push'
            and data.get('head_branch') == 'main' and data.get('head_sha') == fact['commit']
            and data.get('path') == '.github/workflows/ci.yml'
            and data.get('head_repository', {}).get('full_name') == fact['repository'])


def envelopes(text):
    decoder = json.JSONDecoder()
    while text.strip():
        value, end = decoder.raw_decode(text.lstrip())
        yield from value if isinstance(value, list) else [value]
        text = text.lstrip()[end:]


def verify_image(fact):
    facts_valid(fact)
    builders = read(ROOT / 'policy/builders.json')['workflowCommits']
    require(builders and all(re.fullmatch('[a-f0-9]{40}', b) for b in builders), 'reviewed builder commit is not configured')
    reference = fact['image'] + '@' + fact['digest']
    expected = {k: fact[k] for k in ('repository', 'commit', 'component', 'runId')}
    expected.update(workflow='.github/workflows/ci.yml', ref='refs/heads/main')
    verification_key = (reference, json.dumps(expected, sort_keys=True), tuple(builders))
    if verification_key in _VERIFIED_IMAGES:
        return
    for builder in builders:
        common = ['cosign', 'verify-attestation', '--certificate-identity',
                  'https://github.com/Narx-Space-Prod/platform-ci/.github/workflows/service.yml@' + builder,
                  '--certificate-oidc-issuer', 'https://token.actions.githubusercontent.com',
                  '--certificate-github-workflow-repository', fact['repository'],
                  '--certificate-github-workflow-ref', 'refs/heads/main',
                  '--certificate-github-workflow-sha', fact['commit'],
                  '--certificate-github-workflow-trigger', 'push']
        try:
            output = run(*common, '--type', PREDICATE, reference)
            for envelope in envelopes(output):
                statement = json.loads(base64.b64decode(envelope['payload'], validate=True))
                subjects = statement.get('subject', [])
                bound = any(s.get('digest', {}).get('sha256') == fact['digest'].removeprefix('sha256:') for s in subjects)
                if bound and statement.get('predicateType') == PREDICATE and statement.get('predicate') == expected:
                    run(*common, '--type', 'cyclonedx', reference)
                    _VERIFIED_IMAGES.add(verification_key)
                    return
        except subprocess.CalledProcessError:
            continue
    raise ValueError('image lacks trusted, source-bound provenance and SBOM: ' + reference)


def deployment_revisions(revision, environment):
    require(re.fullmatch('[a-f0-9]{40}', revision), 'invalid GitOps revision')
    require(environment in ENVIRONMENTS, 'unexpected deployment environment')
    # Ignore commits for other environments, but never reuse evidence across a
    # chart, runtime, release or Argo application configuration change.
    anchor = run('git', 'log', '--first-parent', '-1', '--format=%H', revision, '--',
                 'charts', 'environments/' + environment, 'applications/local/commerce.json')
    require(re.fullmatch('[a-f0-9]{40}', anchor), 'deployment configuration history is missing')
    revisions = run('git', 'rev-list', '--first-parent', revision, '^' + anchor).splitlines() + [anchor]
    # Bound remote API calls; missing evidence fails closed rather than skipping
    # newer failures to find a historical successful deployment.
    return revisions[:50]


def healthy(revision, environment, state='success'):
    require(re.fullmatch('[a-f0-9]{40}', revision), 'invalid GitOps revision')
    expected = os.environ.get('NARX_DEPLOYMENT_BOT_LOGIN', '')
    require(expected.endswith('[bot]'), 'deployment observer identity is not configured')
    for deployed_revision in deployment_revisions(revision, environment):
        statuses = api(f'repos/Narx-Space-Prod/platform-gitops/commits/{deployed_revision}/statuses?per_page=100')
        match = next((s for s in statuses if s['context'] == 'narx/deploy-' + environment), None)
        if match:
            return match['state'] == state and match.get('creator', {}).get('login') == expected
    return False


def next_bundle(current, components, revision, environment):
    if validate(current):
        require(healthy(revision, environment), 'current environment must be verified healthy before advancing')
        previous = {'components': current['release']['components'], 'gitopsCommit': revision, 'environment': environment}
    else:
        previous = None
    return bundle(components, previous)


def rollback(current, environment):
    require(validate(current), 'cannot roll back an empty release')
    previous = current['release']['previous']
    require(previous and previous['environment'] == environment, 'no previous verified release')
    require(healthy(previous['gitopsCommit'], environment), 'previous release has no trusted successful rollout evidence')
    # Do not create rollback loops or undo schema/data migrations automatically.
    return bundle(previous['components'])


def enable_release_auto_merge(environment, branch):
    require(environment in ENVIRONMENTS, 'unexpected promotion environment')
    if environment == 'production':
        return
    expected = run('git', 'rev-parse', 'HEAD')
    require(re.fullmatch('[a-f0-9]{40}', expected), 'invalid expected PR head')
    for attempt in range(5):
        # Git push can complete before GitHub updates its PR view. Never accept
        # a different head or remove the match-head guard to work around lag.
        pr = json.loads(run('gh', 'pr', 'view', branch, '--repo', 'Narx-Space-Prod/platform-gitops',
                            '--json', 'state,headRefOid,autoMergeRequest'))
        if pr['headRefOid'] == expected:
            if pr['state'] == 'MERGED' or (pr['state'] == 'OPEN' and pr.get('autoMergeRequest')):
                return
            require(pr['state'] == 'OPEN', 'release PR is not open')
            try:
                run('gh', 'pr', 'merge', branch, '--repo', 'Narx-Space-Prod/platform-gitops',
                    '--auto', '--squash', '--match-head-commit', expected)
                return
            except subprocess.CalledProcessError:
                if attempt == 4:
                    raise
        if attempt < 4:
            time.sleep(min(2 ** attempt, 4))
    raise ValueError('GitHub PR head did not reach the expected commit; no merge requested')


def open_pr(environment, value, reason):
    validate(value)
    # GitHub bot account ID plus login binds attribution to our installed App.
    identity = ['-c', 'user.name=narx-local-commerce-release[bot]',
                '-c', 'user.email=352511280+narx-local-commerce-release[bot]@users.noreply.github.com']
    branch = 'release/' + environment + '-' + value['release']['id'][:16]
    path = f'environments/{environment}/release.json'
    existing = json.loads(run('gh', 'pr', 'list', '--repo', 'Narx-Space-Prod/platform-gitops', '--state', 'all', '--head', branch, '--json', 'number,state'))
    if existing and existing[0]['state'] == 'CLOSED':
        print('Release PR was declined:', existing[0]['number'])
        return
    updating = bool(existing and existing[0]['state'] == 'OPEN')
    if updating:
        run('git', 'fetch', 'origin', branch)
        run('git', 'switch', '-c', branch, 'FETCH_HEAD')
        run('git', *identity,
            'merge', '--no-edit', 'origin/main')
    else:
        run('git', 'switch', '-c', branch, 'origin/main')
    write(ROOT / path, value)
    run('git', 'add', path)
    if subprocess.run(['git', 'diff', '--cached', '--quiet'], check=False).returncode:
        run('git', *identity,
            'commit', '-m', f'{reason}: {environment} {value["release"]["id"][:16]}')
    run('git', 'push', 'origin', branch)
    if updating:
        print('Refreshed release PR against current main:', existing[0]['number'])
        enable_release_auto_merge(environment, branch)
        return
    lines = [f'{reason.capitalize()} **{environment}** to `{value["release"]["id"]}`.', '',
             'Promotes existing immutable images; no rebuild. All required CI checks apply. Dev and QA release PRs merge automatically after trusted checks; production and platform changes require code-owner review and explicit merge.', '']
    if os.environ.get('GITHUB_REPOSITORY') == 'Narx-Space-Prod/platform-gitops' and os.environ.get('GITHUB_RUN_ID', '').isdigit():
        lines += ['[Central delivery graph](https://github.com/Narx-Space-Prod/platform-gitops/actions/runs/' + os.environ['GITHUB_RUN_ID'] + ')', '']
    lines += [f'- {name}: `{f["image"]}@{f["digest"]}`; source `{f["repository"]}@{f["commit"]}`' for name, f in value['release']['components'].items()]
    body = ROOT / '.git/release-pr.md'; body.write_text('\n'.join(lines) + '\n')
    args = ['gh', 'pr', 'create', '--repo', 'Narx-Space-Prod/platform-gitops', '--base', 'main', '--head', branch,
            '--title', f'{reason}: {environment} {value["release"]["id"][:16]}', '--body-file', str(body)]
    if environment == 'production':
        args += ['--reviewer', 'Code-S-12']
    run(*args)
    enable_release_auto_merge(environment, branch)


def released_facts():
    result, repositories = {}, {}
    for component, repository in COMPONENTS.items():
        if repository not in repositories:
            pages = json.loads(run('gh', 'api', '--paginate', '--slurp',
                                   f'repos/Narx-Space-Prod/{repository}/releases?per_page=100'))
            repositories[repository] = [record for page in pages for record in page]
        records = repositories[repository]
        for record in records:
            if record.get('draft') or record.get('prerelease') or not re.fullmatch(component + '-[a-f0-9]{40}', record['tag_name']):
                continue
            assets = [a for a in record.get('assets', []) if a['name'] == 'release.json' and a['size'] <= 16384]
            if not assets:
                continue
            fact = json.loads(run('gh', 'api', '-H', 'Accept: application/octet-stream',
                                  f'repos/Narx-Space-Prod/{repository}/releases/assets/{assets[0]["id"]}'))
            facts_valid(fact, component)
            require(record['tag_name'] == component + '-' + fact['commit'], 'release tag/source mismatch')
            if not successful_run(fact):
                continue
            if component in result:
                selected = result[component]
                comparison = api(f"repos/{fact['repository']}/compare/{selected['commit']}...{fact['commit']}")
                if comparison.get('status') != 'ahead':
                    continue
            result[component] = fact
        if component in result:
            verify_image(result[component])
    return result


def reconcile(operation, environment=None):
    run('git', 'fetch', 'origin', 'main')
    revision = run('git', 'rev-parse', 'origin/main')
    run('git', 'switch', '--detach', revision)
    states = {e: read(ROOT / f'environments/{e}/release.json') for e in ENVIRONMENTS}
    for value in states.values():
        validate(value)
    if operation == 'rollback':
        require(environment in ENVIRONMENTS, 'select a rollback environment')
        value = rollback(states[environment], environment)
        for fact in value['release']['components'].values():
            verify_image(fact)
        open_pr(environment, value, 'rollback')
        return
    facts = released_facts()
    for environment in ('qa', 'production'):
        current = states[environment]
        if validate(current) and current['release']['previous'] and healthy(revision, environment, 'failure'):
            value = rollback(current, environment)
            for fact in value['release']['components'].values():
                verify_image(fact)
            open_pr(environment, value, 'rollback')
            return
    if (set(facts) == set(COMPONENTS) and release_id(facts) != states['dev']['release']['id']
            and (not validate(states['dev']) or healthy(revision, 'dev'))):
        value = next_bundle(states['dev'], facts, revision, 'dev')
        open_pr('dev', value, 'release')
        return
    for source, target in [('dev', 'qa'), ('qa', 'production')]:
        if not validate(states[source]) or not healthy(revision, source):
            continue
        facts = states[source]['release']['components']
        if release_id(facts) == states[target]['release']['id']:
            continue
        if validate(states[target]) and not healthy(revision, target):
            print('Waiting for trusted healthy target evidence:', target)
            continue
        for fact in facts.values():
            verify_image(fact)
        value = next_bundle(states[target], facts, revision, target)
        open_pr(target, value, 'promote')
        return
    print('No eligible change: waiting for signed releases, rollout evidence or a reviewed merge.')


def verify_candidate(value, environment, revision, states):
    ready = validate(value)
    current = states[environment]
    if not ready:
        require(not validate(current), 'an active release cannot return to bootstrap state')
        return
    facts = value['release']['components']
    previous = current['release']['previous']
    is_rollback = previous and facts == previous['components']
    if is_rollback:
        require(value == rollback(current, environment), 'rollback differs from trusted previous release')
    else:
        if environment != 'dev':
            source = 'dev' if environment == 'qa' else 'qa'
            require(validate(states[source]) and facts == states[source]['release']['components'], 'promotion must use the exact upstream bundle')
            require(healthy(revision, source), 'upstream environment has no trusted successful rollout')
        require(value == next_bundle(current, facts, revision, environment), 'candidate rollback evidence is not the current healthy state')
    for fact in facts.values():
        require(successful_run(fact), 'source CI did not succeed on protected main')
        verify_image(fact)


def fixtures(path):
    facts = {name: {'component': name, 'image': 'ghcr.io/narx-space-prod/commerce-' + name,
                    'digest': 'sha256:' + '0' * 64, 'repository': 'Narx-Space-Prod/' + repo,
                    'commit': '1' * 40, 'runId': 1} for name, repo in COMPONENTS.items()}
    write(path, bundle(facts))


def stage_output(environment, value, revision, active=True):
    release = value['release']['id']
    branch = 'release/' + environment + '-' + release[:16]
    prs = json.loads(run('gh', 'pr', 'list', '--repo', 'Narx-Space-Prod/platform-gitops',
        '--state', 'all', '--head', branch, '--json', 'number,url,state'))
    pr = next((p for state in ('OPEN', 'MERGED') for p in prs if p['state'] == state), None)
    output = {'release-id': release, 'environment': environment, 'revision': revision,
              'active': str(active).lower(), 'pr-number': str(pr['number']) if pr else '',
              'pr-url': pr['url'] if pr else ''}
    if os.environ.get('GITHUB_OUTPUT'):
        with open(os.environ['GITHUB_OUTPUT'], 'a') as target:
            target.write(''.join(f'{key}={value}\n' for key, value in output.items()))
    if os.environ.get('GITHUB_STEP_SUMMARY'):
        with open(os.environ['GITHUB_STEP_SUMMARY'], 'a') as target:
            target.write(f'### {environment}: `{release[:16]}`\n\n')
            if pr:
                target.write(f"[Release PR #{pr['number']}]({pr['url']}) · {pr['state']}\n\n")
            target.write('| Component | Source CI | Commit |\n| --- | --- | --- |\n')
            for name, fact in value['release']['components'].items():
                target.write(f"| {name} | [CI run {fact['runId']}](https://github.com/{fact['repository']}/actions/runs/{fact['runId']}) | `{fact['commit'][:12]}` |\n")
    return output


def wait_for_source(timeout=120):
    """Dispatch happens at CI's last step; do not race its completed-run evidence."""
    if os.environ.get('GITHUB_EVENT_NAME') != 'repository_dispatch':
        return
    event = read(os.environ['GITHUB_EVENT_PATH'])
    source = event.get('client_payload', {})
    if not source:  # Earlier reviewed notification workflows remain compatible.
        return
    repository, revision, run_id = (source.get(k) for k in ('repository', 'commit', 'runId'))
    require(repository in {'Narx-Space-Prod/' + r for r in COMPONENTS.values()}, 'unexpected source repository')
    require(isinstance(revision, str) and re.fullmatch('[a-f0-9]{40}', revision), 'invalid source revision')
    require(type(run_id) is int and run_id > 0, 'invalid source run')
    deadline = time.monotonic() + timeout
    while True:
        data = api(f'repos/{repository}/actions/runs/{run_id}')
        require(data.get('head_sha') == revision and data.get('event') == 'push'
                and data.get('head_branch') == 'main' and data.get('path') == '.github/workflows/ci.yml'
                and data.get('head_repository', {}).get('full_name') == repository, 'source run is not protected-main CI')
        if data.get('status') == 'completed':
            require(data.get('conclusion') == 'success', 'source CI did not complete successfully')
            return
        require(time.monotonic() < deadline, 'source CI did not finish before the intake deadline')
        time.sleep(10)


def advance_stage(environment, expected=None):
    """Advance exactly one graph stage using the existing immutable-release policy."""
    require(environment in ENVIRONMENTS, 'unexpected delivery stage')
    run('git', 'fetch', 'origin', 'main')
    revision = run('git', 'rev-parse', 'origin/main')
    run('git', 'switch', '--detach', revision)
    states = {e: read(ROOT / f'environments/{e}/release.json') for e in ENVIRONMENTS}
    for value in states.values():
        validate(value)
    if environment == 'dev':
        wait_for_source()
        facts = released_facts()
        require(set(facts) == set(COMPONENTS), 'all four signed components are required')
    else:
        require(isinstance(expected, str) and re.fullmatch('[a-f0-9]{64}', expected), 'expected release ID is required')
        source = 'dev' if environment == 'qa' else 'qa'
        require(validate(states[source]) and states[source]['release']['id'] == expected, 'upstream release was superseded')
        require(healthy(revision, source), 'upstream environment lacks trusted deployment success')
        facts = states[source]['release']['components']
        for fact in facts.values():
            require(successful_run(fact), 'source CI is no longer successful')
            verify_image(fact)
    current = states[environment]
    wanted = release_id(facts)
    if current['release']['id'] != wanted:
        value = next_bundle(current, facts, revision, environment)
        open_pr(environment, value, 'release' if environment == 'dev' else 'promote')
        records = json.loads(run('gh', 'pr', 'list', '--repo', 'Narx-Space-Prod/platform-gitops',
            '--state', 'all', '--head', 'release/' + environment + '-' + wanted[:16], '--json', 'state'))
        require(any(p['state'] in ('OPEN', 'MERGED') for p in records), 'release PR was declined or was not created')
    else:
        value = current
    active = environment != 'dev' or any(
        states[e]['release']['id'] != wanted or not healthy(revision, e) for e in ENVIRONMENTS)
    return stage_output(environment, value, revision, active)


def wait_for_deployment(environment, expected, timeout=1200):
    require(environment in ENVIRONMENTS, 'unexpected deployment environment')
    require(isinstance(expected, str) and re.fullmatch('[a-f0-9]{64}', expected), 'invalid expected release ID')
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            run('git', 'fetch', 'origin', 'main')
            revision = run('git', 'rev-parse', 'origin/main')
            value = json.loads(run('git', 'show', revision + f':environments/{environment}/release.json'))
            validate(value)
            if value['release']['id'] == expected:
                require(not healthy(revision, environment, 'failure'), 'trusted deployment failure reported for ' + environment)
                if healthy(revision, environment):
                    print('Verified deployment and PostSync smoke:', environment, expected[:16])
                    return stage_output(environment, value, revision)
            print('Waiting for merged release and trusted smoke evidence:', environment, expected[:16])
        except subprocess.CalledProcessError:
            # Transport failures never count as success; retry only within this deadline.
            print('Deployment evidence temporarily unavailable; retrying.')
        time.sleep(20)
    raise ValueError('deployment evidence timed out: ' + environment)


def recover_stage(environment):
    require(environment in ('qa', 'production'), 'automatic recovery is limited to QA/production')
    run('git', 'fetch', 'origin', 'main')
    revision = run('git', 'rev-parse', 'origin/main')
    run('git', 'switch', '--detach', revision)
    current = read(ROOT / f'environments/{environment}/release.json')
    if not validate(current) or not healthy(revision, environment, 'failure'):
        print('No current trusted deployment failure; no recovery proposed.')
        return
    value = rollback(current, environment)
    for fact in value['release']['components'].values():
        verify_image(fact)
    open_pr(environment, value, 'rollback')
    stage_output(environment, value, revision)


def observe_production():
    event = read(os.environ['GITHUB_EVENT_PATH'])
    pr = event.get('pull_request', {})
    require(pr.get('merged') is True and pr.get('base', {}).get('ref') == 'main'
            and pr.get('head', {}).get('repo', {}).get('full_name') == 'Narx-Space-Prod/platform-gitops', 'not an internal merged main PR')
    number, revision = pr.get('number'), pr.get('merge_commit_sha')
    require(type(number) is int and isinstance(revision, str) and re.fullmatch('[a-f0-9]{40}', revision), 'invalid merged release PR')
    pages = json.loads(run('gh', 'api', '--paginate', '--slurp',
        f'repos/Narx-Space-Prod/platform-gitops/pulls/{number}/files?per_page=100'))
    files = [f for page in pages for f in page]
    # Other owned chart/platform changes can affect Production, but are not a
    # release continuation and must not masquerade as this release's deployment.
    require(len(files) == 1 and files[0]['filename'] == 'environments/production/release.json'
            and files[0]['status'] == 'modified' and not files[0].get('previous_filename'), 'not a Production release-only PR')
    run('git', 'fetch', 'origin', 'main')
    value = json.loads(run('git', 'show', revision + ':environments/production/release.json'))
    require(validate(value), 'Production release is not deployable')
    wait_for_deployment('production', value['release']['id'])


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('operation', choices=['validate', 'fixtures', 'reconcile', 'rollback', 'verify-candidate', 'stage', 'wait', 'recover', 'observe-production'])
    parser.add_argument('argument', nargs='?')
    parser.add_argument('--environment', choices=ENVIRONMENTS)
    parser.add_argument('--revision')
    parser.add_argument('--bundle')
    args = parser.parse_args()
    if args.operation == 'fixtures':
        require(args.argument, 'provide a temporary fixture output path')
        fixtures(args.argument)
    elif args.operation == 'stage':
        advance_stage(args.argument, args.bundle)
    elif args.operation == 'wait':
        wait_for_deployment(args.argument, args.bundle)
    elif args.operation == 'recover':
        recover_stage(args.argument)
    elif args.operation == 'observe-production':
        observe_production()
    elif args.operation == 'validate':
        for environment in ENVIRONMENTS:
            validate(read(ROOT / f'environments/{environment}/release.json'))
    elif args.operation == 'verify-candidate':
        require(args.environment and args.revision, 'candidate environment and base revision are required')
        states = {e: read(ROOT / f'environments/{e}/release.json') for e in ENVIRONMENTS}
        verify_candidate(read(args.argument), args.environment, args.revision, states)
    else:
        reconcile(args.operation, args.argument)


if __name__ == '__main__':
    main()
