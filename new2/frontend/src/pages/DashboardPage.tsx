import { useEffect, useState } from 'react'
import ArrowForwardIcon from '@mui/icons-material/ArrowForward'
import RefreshIcon from '@mui/icons-material/Refresh'
import type { CurrentUser, DashboardResponse, SummaryStats } from '../types'
import { formatDate, Notice, PageHeading, send, StatusTag } from '../App'

const metrics: { key: keyof SummaryStats; label: string; tone: string }[] = [
  { key: 'open_work', label: 'Open work', tone: 'teal' },
  { key: 'unassigned_work', label: 'Unassigned', tone: 'coral' },
  { key: 'sla_breaches', label: 'SLA breaches', tone: 'red' },
  { key: 'locked_cases', label: 'Locked cases', tone: 'blue' },
  { key: 'unavailable_staff', label: 'Unavailable staff', tone: 'gold' },
  { key: 'pending_approvals', label: 'Pending approvals', tone: 'green' },
]

export function DashboardPage({ user, onNavigate }: { user: CurrentUser; onNavigate: (section: string) => void }) {
  const [dashboard, setDashboard] = useState<DashboardResponse | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)

  async function load() {
    setLoading(true)
    try {
      setDashboard(await send<DashboardResponse>('get', '/stats/dashboard'))
      setError(null)
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : 'Dashboard data is unavailable.')
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => { void load() }, [])

  return <>
    <PageHeading eyebrow="LIVE OPERATIONS" title="Good work starts with a clear picture." description={`Your control room for ${user.departments.length ? user.departments.join(', ') : 'BP Securities operations'}.`} actions={<button className="button button-secondary" onClick={() => void load()} disabled={loading}><RefreshIcon fontSize="small" />Refresh</button>} />
    {error && <Notice tone="danger">{error}</Notice>}
    {loading && !dashboard ? <div className="loading-line">Loading operational picture…</div> : dashboard && <>
      <section className="metric-grid" aria-label="Operational summary">
        {metrics.map((metric) => <button key={metric.key} className={`metric-card metric-${metric.tone}`} onClick={() => onNavigate(metric.key === 'pending_approvals' || metric.key === 'unavailable_staff' ? 'availability' : 'workbench')}><span>{metric.label}</span><strong>{dashboard.summary[metric.key]}</strong><small>View details <ArrowForwardIcon fontSize="inherit" /></small></button>)}
      </section>
      <div className="dashboard-grid">
        <section className="surface workload-panel">
          <div className="surface-heading"><div><div className="eyebrow">CAPACITY</div><h2>Queue workload</h2></div><button className="text-button" onClick={() => onNavigate('queues')}>All queues <ArrowForwardIcon fontSize="inherit" /></button></div>
          {dashboard.work_by_queue.length ? <div className="queue-workload-list">{dashboard.work_by_queue.map((queue) => <div className="queue-workload-row" key={queue.queue_code}><div className="queue-title"><strong>{queue.queue_name}</strong><span>{queue.queue_code} <i>·</i> {queue.source_app}</span></div><div className="capacity-track" aria-label={`${queue.capacity_utilisation_pct}% capacity used`}><span style={{ width: `${Math.min(queue.capacity_utilisation_pct, 100)}%` }} /></div><div className="queue-numbers"><strong>{queue.open_count}</strong><span>open</span></div><div className="queue-numbers"><strong>{queue.available_members}/{queue.eligible_members}</strong><span>available</span></div><div className="queue-numbers queue-overdue"><strong>{queue.overdue_count}</strong><span>overdue</span></div></div>)}</div> : <div className="empty-inline">No queue workload to show yet.</div>}
        </section>
        <section className="surface alerts-panel">
          <div className="surface-heading"><div><div className="eyebrow">NEEDS ATTENTION</div><h2>Operational alerts</h2></div><span className="count-pill">{dashboard.alerts.length}</span></div>
          {dashboard.alerts.length ? <div className="alert-list">{dashboard.alerts.slice(0, 6).map((alert) => <button className="alert-row" key={alert.key} onClick={() => onNavigate(alert.link_path.includes('availability') ? 'availability' : 'workbench')}><span className={`alert-mark alert-${alert.severity.toLowerCase()}`} /><span className="alert-copy"><strong>{alert.title}</strong><small>{alert.detail}</small></span><span className="alert-count">{alert.count}</span></button>)}</div> : <div className="empty-inline">No active alerts. Keep an eye on the board.</div>}
        </section>
      </div>
      <div className="dashboard-grid dashboard-grid-bottom">
        <section className="surface">
          <div className="surface-heading"><div><div className="eyebrow">SERVICE LEVEL</div><h2>SLA position</h2></div><button className="text-button" onClick={() => onNavigate('workbench')}>Open workbench <ArrowForwardIcon fontSize="inherit" /></button></div>
          <div className="sla-overview"><div className="sla-score"><strong>{dashboard.sla.breach_rate_pct.toFixed(1)}%</strong><span>breach rate</span></div><div className="sla-states">{[['On track', dashboard.sla.on_track, 'track'], ['Due soon', dashboard.sla.due_soon, 'soon'], ['Breached', dashboard.sla.breached, 'breach']].map(([label, count, tone]) => <div className="sla-state" key={String(label)}><span className={`sla-dot ${tone}`} /><span>{label}</span><strong>{count}</strong></div>)}</div></div>
          <div className="ageing-strip">{dashboard.ageing.buckets.map((bucket, index) => <div className="ageing-bucket" key={bucket.label}><span>{bucket.label}</span><strong>{bucket.count}</strong><i style={{ height: `${Math.max(6, Math.min(42, bucket.count / Math.max(dashboard.ageing.total, 1) * 84))}px` }} className={`ageing-bar ageing-${index}`} /></div>)}</div>
        </section>
        <section className="surface integration-summary">
          <div className="surface-heading"><div><div className="eyebrow">SOURCE HEALTH</div><h2>Integrations</h2></div><button className="text-button" onClick={() => onNavigate('integrations')}>Health details <ArrowForwardIcon fontSize="inherit" /></button></div>
          {dashboard.integration_health.length ? dashboard.integration_health.map((item) => <div className="integration-row" key={item.source_app}><span className={`health-indicator health-${item.status.toLowerCase()}`} /><span><strong>{item.source_app}</strong><small>Last check {formatDate(item.last_sync_at)}</small></span><StatusTag>{item.status}</StatusTag></div>) : <Notice tone="info">No integration probes have been recorded yet. LOS and LMS remain isolated until configured and verified.</Notice>}
          <div className="dashboard-stamp">Updated {formatDate(dashboard.generated_at)}</div>
        </section>
      </div>
    </>}
  </>
}