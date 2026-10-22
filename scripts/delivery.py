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

ROOT = Path(__file__).resolve().parents[1]
COMPONENTS = {'frontend': 'commerce-frontend', 'catalogue': 'commerce-catalogue',
              'orders': 'commerce-orders', 'cart': 'commerce-orders'}
ALIASES = {'frontend': 'frontend', 'catalogue': 'catalogue', 'orders': 'orders',
           'cart': 'cart', 'publisher': 'orders', 'worker': 'orders'}
ENVIRONMENTS = ('dev', 'qa', 'production')
PREDICATE = 'https://narx.space/attestations/release/v1'


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


def facts_valid(fact, component=None):
    require(isinstance(fact, dict), 'release fact must be an object')
    require(set(fact) == {'component', 'image', 'digest', 'repository', 'commit', 'runId'}, 'unexpected release fact fields')
    name = fact['component']
    require(name in COMPONENTS and (component is None or name == component), 'unexpected component')
    require(fact['repository'] == 'Narx-Space-Prod/' + COMPONENTS[name], 'unexpected source repository')
    require(isinstance(fact['commit'], str) and re.fullmatch('[a-f0-9]{40}', fact['commit']), 'invalid source commit')
    require(type(fact['runId']) is int and fact['runId'] > 0, 'invalid workflow run')
    require(isinstance(fact['digest'], str) and re.fullmatch('sha256:[a-f0-9]{64}', fact['digest']), 'mutable or invalid digest')
    require(isinstance(fact['image'], str) and re.fullmatch(r'[0-9]{12}\.dkr\.ecr\.[a-z]{2}-[a-z]+-[0-9]+\.amazonaws\.com/narx/commerce-' + name, fact['image']), 'unexpected registry or image')
    account, region = os.environ.get('AWS_ACCOUNT_ID'), os.environ.get('AWS_REGION')
    if account and region:
        require(fact['image'].startswith(f'{account}.dkr.ecr.{region}.amazonaws.com/'), 'image belongs to another AWS account/region')
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
                    return
        except subprocess.CalledProcessError:
            continue
    raise ValueError('image lacks trusted, source-bound provenance and SBOM: ' + reference)


def healthy(revision, environment, state='success'):
    require(re.fullmatch('[a-f0-9]{40}', revision), 'invalid GitOps revision')
    expected = os.environ.get('NARX_DEPLOYMENT_BOT_LOGIN', '')
    require(expected.endswith('[bot]'), 'deployment observer identity is not configured')
    statuses = api(f'repos/Narx-Space-Prod/platform-gitops/commits/{revision}/statuses?per_page=100')
    # First status for the context is the latest. A later failure cannot be hidden by an older success.
    match = next((s for s in statuses if s['context'] == 'narx/deploy-' + environment), None)
    return bool(match and match['state'] == state and match.get('creator', {}).get('login') == expected)


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


def open_pr(environment, value, reason):
    validate(value)
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
        run('git', '-c', 'user.name=narx-release[bot]', '-c', 'user.email=release@users.noreply.github.com',
            'merge', '--no-edit', 'origin/main')
    else:
        run('git', 'switch', '-c', branch, 'origin/main')
    write(ROOT / path, value)
    run('git', 'add', path)
    if subprocess.run(['git', 'diff', '--cached', '--quiet'], check=False).returncode:
        run('git', '-c', 'user.name=narx-release[bot]', '-c', 'user.email=release@users.noreply.github.com',
            'commit', '-m', f'{reason}: {environment} {value["release"]["id"][:16]}')
    run('git', 'push', 'origin', branch)
    if updating:
        print('Refreshed release PR against current main:', existing[0]['number'])
        return
    lines = [f'{reason.capitalize()} **{environment}** to `{value["release"]["id"]}`.', '',
             'Promotes existing immutable images; no rebuild. All main review and CI requirements apply.', '']
    lines += [f'- {name}: `{f["image"]}@{f["digest"]}`; source `{f["repository"]}@{f["commit"]}`' for name, f in value['release']['components'].items()]
    body = ROOT / '.git/release-pr.md'; body.write_text('\n'.join(lines) + '\n')
    run('gh', 'pr', 'create', '--repo', 'Narx-Space-Prod/platform-gitops', '--base', 'main', '--head', branch,
        '--title', f'{reason}: {environment} {value["release"]["id"][:16]}', '--body-file', str(body), '--reviewer', 'Code-S-12')


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
    facts = {name: {'component': name, 'image': '123456789012.dkr.ecr.us-east-1.amazonaws.com/narx/commerce-' + name,
                    'digest': 'sha256:' + '0' * 64, 'repository': 'Narx-Space-Prod/' + repo,
                    'commit': '1' * 40, 'runId': 1} for name, repo in COMPONENTS.items()}
    write(path, bundle(facts))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('operation', choices=['validate', 'fixtures', 'reconcile', 'rollback', 'verify-candidate'])
    parser.add_argument('argument', nargs='?')
    parser.add_argument('--environment', choices=ENVIRONMENTS)
    parser.add_argument('--revision')
    args = parser.parse_args()
    if args.operation == 'fixtures':
        require(args.argument, 'provide a temporary fixture output path')
        fixtures(args.argument)
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
