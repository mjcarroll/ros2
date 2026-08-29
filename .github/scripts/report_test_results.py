"""Summarise colcon JUnit XML into GitHub annotations and a job summary."""
import glob
import os
import sys
import xml.etree.ElementTree as ET

MAX_ROWS = 60
MAX_ANNOTATIONS = 20


def suites(path):
    """Yield every <testsuite> in a file, whether or not it is wrapped."""
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError:
        return
    if root.tag == 'testsuite':
        yield root
    else:
        for s in root.iter('testsuite'):
            yield s


def main():
    tests = failures = errors = skipped = 0
    bad = []
    files = glob.glob('build/**/test_results/**/*.xml', recursive=True)
    for path in files:
        pkg = path.replace(chr(92), '/').split('/')[1]
        for s in suites(path):
            tests += int(s.get('tests', 0) or 0)
            failures += int(s.get('failures', 0) or 0)
            errors += int(s.get('errors', 0) or 0)
            skipped += int(s.get('skipped', 0) or 0)
            for case in s.iter('testcase'):
                for kind in ('failure', 'error'):
                    node = case.find(kind)
                    if node is None:
                        continue
                    msg = (node.get('message') or node.text or '').strip()
                    msg = ' '.join(msg.split())[:300]
                    name = case.get('name', '?')
                    cls = case.get('classname') or s.get('name', '')
                    bad.append((pkg, cls, name, kind, msg))

    leg = os.environ.get('LEG', 'unknown')
    ok = not bad
    head = (f'{tests} tests, {failures} failures, {errors} errors, '
            f'{skipped} skipped, across {len(files)} result files')

    out = [f'### {leg} — {"no failures" if ok else str(len(bad)) + " problems"}',
           '', head, '']
    if bad:
        out += ['| package | test | kind | message |', '|---|---|---|---|']
        for pkg, cls, name, kind, msg in bad[:MAX_ROWS]:
            cell = msg.replace('|', chr(92) + '|') or '(no message)'
            out.append(f'| `{pkg}` | `{cls}::{name}` | {kind} | {cell} |')
        if len(bad) > MAX_ROWS:
            out.append('')
            out.append(f'...and {len(bad) - MAX_ROWS} more; see the uploaded XML.')
    out.append('')

    summary = os.environ.get('GITHUB_STEP_SUMMARY')
    text = '\n'.join(out)
    if summary:
        with open(summary, 'a', encoding='utf-8') as handle:
            handle.write(text + '\n')
    else:
        print(text)

    # Native annotations, so failures surface on the run itself.
    for pkg, cls, name, kind, msg in bad[:MAX_ANNOTATIONS]:
        title = f'{leg}: {pkg} {cls}::{name}'.replace('\n', ' ')
        body = (msg or 'see uploaded test results').replace('\n', '%0A')
        print(f'::{"error" if kind == "error" else "warning"} '
              f'title={title}::{body}')
    if len(bad) > MAX_ANNOTATIONS:
        print(f'::notice::{len(bad) - MAX_ANNOTATIONS} further test problems '
              f'on {leg}; see the job summary')
    print(head)
    return 0


if __name__ == '__main__':
    sys.exit(main())
