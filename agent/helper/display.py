"""Cards for read results: fleet, findings, CVE impact and compliance reports.

Each builder takes a tool's result and returns the `display` block the UI
renders (ui/frontend/src/components/cards/ResultCards.tsx). CardCollector
strips it before the model sees the result, so cards cost no tokens. Builders
only reshape what the tool returned; they never add data.
"""

from datetime import datetime, timezone
from typing import Dict, List, Optional

MAX_ROWS = 12
MAX_CVE_ROWS = 8
MAX_REPORT_ROWS = 6
_ENV_ORDER = {'dev': 0, 'test': 1, 'staging': 2, 'prod': 3}


def _env_sorted(names):
    return sorted(names, key=lambda e: (_ENV_ORDER.get(e, 9), e))


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def _ago(iso: Optional[str], now: datetime) -> str:
    if not iso:
        return "time unknown"
    try:
        then = datetime.fromisoformat(iso)
    except ValueError:
        return "time unknown"
    if then.tzinfo is None:
        then = then.replace(tzinfo=timezone.utc)
    hours = (now - then).total_seconds() / 3600
    if hours < 1:
        return "just now"
    if hours < 48:
        return f"{int(hours)} h ago"
    return f"{int(hours // 24)} d ago"


def fleet_card(instances_by_env: Dict[str, List[dict]], environment: Optional[str]) -> dict:
    """Fleet by environment, or the instances of one environment."""
    total = sum(len(v) for v in instances_by_env.values())
    accounts = {i.get('account_id') for v in instances_by_env.values() for i in v}
    if environment:
        insts = instances_by_env.get(environment, [])
        rows = []
        for i in insts[:MAX_ROWS]:
            label = f"{i['name']} ({i['instance_id']})" if i.get('name') else i['instance_id']
            missing = i.get('missing_patches')
            if i.get('data_source') == 'ec2_direct':
                status, tone = "Pending sync", None
            elif missing is None:
                status, tone = "Not scanned", None
            elif missing:
                status, tone = f"{missing} missing", "warn"
            else:
                status, tone = "Up to date", "ok"
            rows.append({'label': label, 'status': status, 'tone': tone} if tone else
                        {'label': label, 'status': status})
        more = len(insts) - len(rows)
        subtitle = _plural(len(accounts), 'account') + (f" · and {more} more" if more else "")
        return {'type': 'instances', 'title': f"{environment} · {_plural(len(insts), 'instance')}",
                'subtitle': subtitle, 'rows': rows,
                'actions': [{'label': 'Vulnerabilities', 'prompt': f"Show vulnerabilities in {environment}"},
                            {'label': 'Preview patches', 'prompt': f"Preview patches in {environment}"}]}
    rows = []
    for env in _env_sorted(instances_by_env):
        insts = instances_by_env[env]
        compliant = sum(1 for i in insts if i.get('missing_patches') == 0 and i.get('data_source') != 'ec2_direct')
        pending = sum(1 for i in insts if i.get('data_source') == 'ec2_direct')
        row = {'label': env, 'count': len(insts), 'compliant': compliant}
        if pending:
            row['note'] = f"{len(insts)} · {pending} pending sync"
        rows.append(row)
    return {'type': 'fleet', 'title': f"Fleet · {_plural(total, 'instance')}",
            'subtitle': f"{_plural(len(accounts), 'account')} · up to date / total per environment",
            'rows': rows[:MAX_ROWS],
            'actions': [{'label': 'Critical vulnerabilities', 'prompt': 'Show critical vulnerabilities'},
                        {'label': 'Maintenance windows', 'prompt': 'Show maintenance windows'}]}


def vulnerability_card(result: dict) -> dict:
    """Findings grouped by CVE, top rows by severity and CVSS as the tool sorted them."""
    # CVEs per severity, as the title and the dashboard count them; findings only for older results.
    counts_in = {k.upper(): v for k, v in (result.get('cve_severity_counts') or result.get('severity_counts') or {}).items()}
    counts = {k.lower(): counts_in.get(k, 0) for k in ('CRITICAL', 'HIGH', 'MEDIUM', 'LOW')}
    by_cve: Dict[str, dict] = {}
    for f in result.get('findings') or []:
        cve = f.get('cve_id')
        if not cve:
            continue
        entry = by_cve.setdefault(cve, {'severity': f.get('severity') or 'UNKNOWN', 'cvss': f.get('cvss_score'),
                                        'resources': set(), 'fix': f.get('fix_available')})
        entry['resources'].update(f.get('affected_resources') or [])
    rows = []
    # Instances per CVE across every in-scope finding; the listed findings can stop part-way through a CVE.
    totals = result.get('cve_instance_counts') or {}
    for cve, e in list(by_cve.items())[:MAX_CVE_ROWS]:
        parts = [f"CVSS {e['cvss']}" if e['cvss'] is not None else "No CVSS",
                 _plural(max(totals.get(cve, 0), len(e['resources'])), 'instance'),
                 {"YES": "fix available", "PARTIAL": "partial fix"}.get(e['fix'], "no fix")]
        rows.append({'id': cve, 'severity': e['severity'], 'summary': ' · '.join(parts),
                     'action': {'label': 'Impact', 'prompt': f"How widespread is {cve} across the fleet?"}})
    unique = result.get('unique_cve_count') or len(by_cve)
    title = f"{_plural(unique, 'vulnerability')}" if unique else "No vulnerabilities found"
    return {'type': 'vulnerabilities', 'title': title.replace('vulnerabilitys', 'vulnerabilities'),
            'subtitle': f"{result.get('total_count', 0)} findings in scope", 'counts': counts,
            'rows': rows, 'more': max(unique - len(rows), 0)}


def impact_card(result: dict) -> dict:
    """One CVE across environments, with SLA against the next window."""
    envs = result.get('environments') or []
    rows = []
    for e in envs:
        sla, until = e.get('sla_hours'), e.get('hours_until_window')
        window = e.get('next_window')
        if window in (None, 'none', 'error') or until is None:
            status, tone = f"SLA {sla} h · no maintenance window", 'bad'
        elif sla is not None and until > sla:
            status, tone = f"SLA {sla} h · {window} in {until:g} h, after the SLA", 'bad'
        else:
            status, tone = f"SLA {sla} h · {window} in {until:g} h", 'ok'
        rows.append({'label': f"{e['environment']} · {_plural(e.get('instance_count', 0), 'instance')}",
                     'status': status, 'tone': tone})
    order = result.get('rollout_order') or []
    first = order[0] if order else None
    return {'type': 'impact',
            'title': f"{result.get('cve_id')} · {_plural(result.get('total_affected', 0), 'instance')} "
                     f"in {_plural(len(envs), 'environment')}",
            'subtitle': f"{result.get('severity') or 'Unknown severity'} · CVSS {result.get('cvss_score')}",
            # Inspector's PARTIAL: a patch fixes some of the affected packages (2 Nov review, M20).
            'fields': [{'label': 'Fix', 'value': {'YES': 'Available', 'PARTIAL': 'Partial'}.get(result.get('fix_available'), 'None'),
                        **({} if result.get('fix_available') == 'YES' else {'tone': 'warn'})},
                       {'label': 'Rollout', 'value': ' → '.join(order)}],
            'rows': rows,
            'actions': [{'label': f'Plan for {first}', 'prompt': f"Plan a patch for {result.get('cve_id')} in {first}"}]
            if first else []}


def compliance_card(result: dict, now: Optional[datetime] = None) -> dict:
    """Recent reports with SLA outcomes; unknown is shown as not recorded, not as a breach."""
    now = now or datetime.now(timezone.utc)
    stats = result.get('statistics') or {}
    total = result.get('total_count', 0)
    if not total:
        return {'type': 'compliance', 'title': 'No patching activity recorded',
                'subtitle': result.get('date_range', ''), 'fields': [], 'rows': []}
    breaches, unknown = stats.get('sla_breaches', 0), stats.get('sla_unknown', 0)
    reports = result.get('reports') or []
    rows = []
    for r in reports[:MAX_REPORT_ROWS]:
        met = r.get('sla_met')
        outcome, tone = ("SLA met", 'ok') if met is True else ("SLA missed", 'bad') if met is False else \
            ("SLA not recorded", None)
        label = f"{r.get('environment')} · {str(r.get('decision') or '').lower()}"
        if r.get('cve_id') not in (None, '', 'unknown', 'N/A'):
            label += f" · {r['cve_id']}"
        status = f"{outcome} · {_ago(r.get('generated_at'), now)}"
        if r.get('reboot_pending'):
            status += f" · reboot pending on {r['reboot_pending']}"
        row = {'label': label, 'status': status}
        if tone:
            row['tone'] = tone
        rows.append(row)
    more = len(reports) - len(rows)
    subtitle = result.get('date_range', '') + (f" · latest {len(rows)} shown" if more > 0 else "")
    return {'type': 'compliance', 'title': f"{_plural(total, 'report')} · {_plural(breaches, 'SLA breach')}"
            .replace('breachs', 'breaches'),
            'subtitle': subtitle,
            'fields': [{'label': 'SLA met', 'value': str(total - breaches - unknown)},
                       {'label': 'SLA missed', 'value': str(breaches), **({'tone': 'bad'} if breaches else {})},
                       {'label': 'Not recorded', 'value': str(unknown)}],
            'rows': rows}
