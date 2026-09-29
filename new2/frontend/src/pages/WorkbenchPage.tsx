import { useEffect, useState, type FormEvent } from 'react'
import AddIcon from '@mui/icons-material/Add'
import OpenInNewIcon from '@mui/icons-material/OpenInNew'
import RefreshIcon from '@mui/icons-material/Refresh'
import type { CommentItem, CurrentUser, QueueSummary, UserListItem, WorkItemDetail, WorkItemEventItem, WorkItemSummary } from '../types'
import { formatDate, list, Modal, Notice, PageHeading, Pager, SearchBox, send, shortRef, StatusTag, usePageData } from '../App'

type WorkAction = 'create' | 'assign' | 'transfer' | 'close' | null

export function WorkbenchPage({ user }: { user: CurrentUser }) {
  const [page, setPage] = useState(1)
  const [search, setSearch] = useState('')
  const [status, setStatus] = useState('')
  const [source, setSource] = useState('')
  const [queueCode, setQueueCode] = useState('')
  const [assignee, setAssignee] = useState('')
  const [priority, setPriority] = useState('')
  const [minimumAge, setMinimumAge] = useState('')
  const [refreshKey, setRefreshKey] = useState(0)
  const [selected, setSelected] = useState<WorkItemDetail | null>(null)
  const [events, setEvents] = useState<WorkItemEventItem[]>([])
  const [comments, setComments] = useState<CommentItem[]>([])
  const [staff, setStaff] = useState<UserListItem[]>([])
  const [queues, setQueues] = useState<QueueSummary[]>([])
  const [action, setAction] = useState<WorkAction>(null)
  const [actionItem, setActionItem] = useState<WorkItemSummary | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [comment, setComment] = useState('')
  const [commentError, setCommentError] = useState<string | null>(null)
  const [createForm, setCreateForm] = useState({ title: '', queue_code: '', source_app: 'OPS', external_ref: '', customer_name: '', customer_mobile: '', priority: 'MEDIUM', due_at: '', auto_assign: false })
  const [actionForm, setActionForm] = useState({ user_id: '', queue_code: '', reason: '', outcome: '', notes: '' })
  const [tab, setTab] = useState<'activity' | 'comments'>('activity')
  const query = {
    page,
    page_size: 20,
    search: search || undefined,
    status: status || undefined,
    source_app: source || undefined,
    queue_code: queueCode || undefined,
    assigned_to_me: assignee === 'me' || undefined,
    unassigned: assignee === 'unassigned' || undefined,
    assigned_user_id: assignee && assignee !== 'me' && assignee !== 'unassigned' ? assignee : undefined,
    priority: priority || undefined,
    age_min_hours: minimumAge ? Number(minimumAge) : undefined,
    sort: 'created_at',
    order: 'desc',
  }
  const rows = usePageData<WorkItemSummary>('/work-items', query, refreshKey)

  useEffect(() => {
    void Promise.all([
      list<UserListItem>('/users', { page: 1, page_size: 100, status: 'ACTIVE', sort: 'display_name', order: 'asc' }).then((result) => setStaff(result.data)).catch(() => {}),
      list<QueueSummary>('/queues', { page: 1, page_size: 100, status: 'ACTIVE', sort: 'name', order: 'asc' }).then((result) => setQueues(result.data)).catch(() => {}),
    ])
  }, [])

  useEffect(() => {
    if (!selected?.lock.is_locked_by_me || !user.permissions.includes('OPS_CASES_LOCK')) return
    const timer = window.setInterval(() => {
      void send('post', `/work-items/${selected.id}/heartbeat`).then(() => openDetail(selected.id)).catch(() => {})
    }, 5 * 60 * 1000)
    return () => window.clearInterval(timer)
  }, [selected?.id, selected?.lock.is_locked_by_me, user.permissions])

  async function openDetail(id: string) {
    try {
      const detail = await send<WorkItemDetail>('get', `/work-items/${id}`)
      setSelected(detail)
      const [eventPage, commentPage] = await Promise.all([
        list<WorkItemEventItem>(`/work-items/${id}/events`, { page: 1, page_size: 50 }),
        list<CommentItem>(`/work-items/${id}/comments`, { page: 1, page_size: 50 }),
      ])
      setEvents(eventPage.data)
      setComments(commentPage.data)
      setError(null)
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : 'Could not open work item.')
    }
  }

  async function mutate(path: string, body?: unknown) {
    if (!selected) return
    setBusy(true)
    setError(null)
    try {
      await send('post', `/work-items/${selected.id}/${path}`, body)
      await openDetail(selected.id)
      setRefreshKey((value) => value + 1)
      setAction(null)
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : 'Action could not be completed.')
    } finally {
      setBusy(false)
    }
  }

  async function submitAction(event: FormEvent) {
    event.preventDefault()
    if (action === 'transfer' && actionItem) {
      setBusy(true)
      try {
        await send('post', `/work-items/${actionItem.id}/transfer`, { queue_code: actionForm.queue_code || null, user_id: actionForm.user_id || null, reason: actionForm.reason })
        setAction(null)
        setRefreshKey((value) => value + 1)
        if (selected?.id === actionItem.id) await openDetail(actionItem.id)
      } catch (reason) { setError(reason instanceof Error ? reason.message : 'Transfer failed.') } finally { setBusy(false) }
      return
    }
    if (action === 'assign' && actionItem) {
      setBusy(true)
      try {
        await send('post', `/work-items/${actionItem.id}/assign`, { user_id: actionForm.user_id, reason: actionForm.reason })
        setAction(null)
        setRefreshKey((value) => value + 1)
        if (selected?.id === actionItem.id) await openDetail(actionItem.id)
      } catch (reason) { setError(reason instanceof Error ? reason.message : 'Assignment failed.') } finally { setBusy(false) }
      return
    }
    if (action === 'close' && actionItem) {
      setBusy(true)
      try {
        await send('post', `/work-items/${actionItem.id}/close`, { reason: actionForm.reason, outcome: actionForm.outcome || null, notes: actionForm.notes || null })
        setAction(null)
        setRefreshKey((value) => value + 1)
        if (selected?.id === actionItem.id) await openDetail(actionItem.id)
      } catch (reason) { setError(reason instanceof Error ? reason.message : 'Close failed.') } finally { setBusy(false) }
    }
  }

  async function createWork(event: FormEvent) {
    event.preventDefault()
    setBusy(true)
    try {
      await send('post', '/work-items', { ...createForm, due_at: createForm.due_at ? new Date(createForm.due_at).toISOString() : null })
      setAction(null)
      setCreateForm({ title: '', queue_code: '', source_app: 'OPS', external_ref: '', customer_name: '', customer_mobile: '', priority: 'MEDIUM', due_at: '', auto_assign: false })
      setPage(1)
      setRefreshKey((value) => value + 1)
      setError(null)
    } catch (reason) { setError(reason instanceof Error ? reason.message : 'Could not create work item.') } finally { setBusy(false) }
  }

  async function addComment(event: FormEvent) {
    event.preventDefault()
    if (!selected || !comment.trim()) return
    try {
      await send('post', `/work-items/${selected.id}/comments`, { comment })
      const result = await list<CommentItem>(`/work-items/${selected.id}/comments`, { page: 1, page_size: 50 })
      setComments(result.data)
      setComment('')
      setCommentError(null)
    } catch (reason) { setCommentError(reason instanceof Error ? reason.message : 'Could not add comment.') }
  }

  function startAction(next: WorkAction, item?: WorkItemSummary) {
    setAction(next)
    setActionItem(item ?? null)
    setActionForm({ user_id: '', queue_code: item?.queue_code ?? '', reason: '', outcome: '', notes: '' })
    setError(null)
  }

  const can = (permission: keyof CurrentUser['permissions'][number] | string) => user.permissions.includes(permission as CurrentUser['permissions'][number])

  return <>
    <PageHeading eyebrow="CASE OWNERSHIP" title="Workbench" description="Allocate, claim and move operational work with a complete case timeline." actions={can('OPS_CASES_CREATE') ? <button className="button button-primary" onClick={() => startAction('create')}><AddIcon fontSize="small" />New work item</button> : undefined} />
    {error && <Notice tone="danger">{error}</Notice>}
    <section className="surface table-surface">
      <div className="table-toolbar filter-toolbar">
        <label className="filter-field"><span>Queue</span><select value={queueCode} onChange={(event) => { setQueueCode(event.target.value); setPage(1) }}><option value="">All queues</option>{queues.map((queue) => <option key={queue.id} value={queue.code}>{queue.name}</option>)}</select></label>
        <label className="filter-field"><span>Assignee</span><select value={assignee} onChange={(event) => { setAssignee(event.target.value); setPage(1) }}><option value="">Anyone</option><option value="me">Assigned to me</option><option value="unassigned">Unassigned</option>{staff.map((person) => <option key={person.id} value={person.id}>{person.display_name}</option>)}</select></label>
        <label className="filter-field"><span>Priority</span><select value={priority} onChange={(event) => { setPriority(event.target.value); setPage(1) }}><option value="">All priorities</option>{['LOW', 'MEDIUM', 'HIGH', 'URGENT'].map((value) => <option key={value}>{value}</option>)}</select></label>
        <label className="filter-field"><span>Minimum age</span><select value={minimumAge} onChange={(event) => { setMinimumAge(event.target.value); setPage(1) }}><option value="">Any age</option><option value="24">24 hours</option><option value="72">3 days</option><option value="168">7 days</option></select></label>
      </div>
      <div className="table-toolbar"><SearchBox value={search} onChange={(value) => { setSearch(value); setPage(1) }} placeholder="Search title, customer or reference" /><label className="filter-field"><span>Status</span><select value={status} onChange={(event) => { setStatus(event.target.value); setPage(1) }}><option value="">All statuses</option>{['OPEN', 'ASSIGNED', 'IN_PROGRESS', 'ON_HOLD', 'CLOSED', 'CANCELLED'].map((value) => <option key={value}>{value}</option>)}</select></label><label className="filter-field"><span>Source</span><select value={source} onChange={(event) => { setSource(event.target.value); setPage(1) }}><option value="">All sources</option>{['OPS', 'LOS', 'LMS'].map((value) => <option key={value}>{value}</option>)}</select></label><button className="icon-button" title="Refresh work items" onClick={() => setRefreshKey((value) => value + 1)}><RefreshIcon /></button><span className="toolbar-total">{rows.total} records</span></div>
      <div className="table-scroll"><table><thead><tr><th>Work item</th><th>Queue</th><th>Priority / SLA</th><th>Assignee</th><th>Status</th><th>Lock</th><th /></tr></thead><tbody>{rows.data.map((item) => <tr key={item.id}><td><button className="row-link" onClick={() => void openDetail(item.id)}><strong>{item.title}</strong><span>{item.source_app} · {shortRef(item.external_ref)}{item.customer_name ? ` · ${item.customer_name}` : ''}</span></button></td><td><span className="mono-label">{item.queue_code}</span></td><td><StatusTag tone={`priority-${item.priority.toLowerCase()}`}>{item.priority}</StatusTag><div className="table-subline"><StatusTag tone={`sla-${item.sla_status.toLowerCase().replaceAll('_', '-')}`}>{item.sla_status.replaceAll('_', ' ')}</StatusTag></div></td><td>{item.assigned_user_name ?? <span className="muted">Unassigned</span>}</td><td><StatusTag>{item.status}</StatusTag>{item.is_overdue && <div className="table-subline"><StatusTag tone="danger">Overdue</StatusTag></div>}</td><td>{item.lock.is_locked ? <span className="lock-copy">{item.lock.locked_by_name}<small>{Math.max(0, Math.ceil((item.lock.seconds_remaining ?? 0) / 60))}m left</small></span> : <span className="muted">Available</span>}</td><td><button className="button button-quiet button-small" onClick={() => void openDetail(item.id)}>Open</button></td></tr>)}</tbody></table>{!rows.loading && !rows.error && rows.data.length === 0 && <div className="empty-state"><h2>No work items found</h2><p>Adjust the filters or create an Ops work item.</p></div>}{rows.loading && <div className="loading-line">Loading work items…</div>}{rows.error && <Notice tone="danger">{rows.error}</Notice>}</div>
      <Pager page={page} total={rows.total} pageSize={20} onPage={setPage} />
    </section>

    {selected && <Modal title="Work item details" onClose={() => setSelected(null)} wide><div className="case-drawer"><div className="case-main"><div className="case-title-line"><div><div className="eyebrow">{selected.source_app} · {shortRef(selected.external_ref)}</div><h3>{selected.title}</h3></div><StatusTag>{selected.status}</StatusTag></div><div className="case-meta-grid"><div><span>Queue</span><strong>{selected.queue_code}</strong></div><div><span>Assignee</span><strong>{selected.assigned_user_name ?? 'Unassigned'}</strong></div><div><span>Customer</span><strong>{selected.customer_name ?? '—'}</strong></div><div><span>Mobile</span><strong>{selected.customer_mobile_masked ?? '—'}</strong></div><div><span>Priority</span><strong>{selected.priority}</strong></div><div><span>Due</span><strong>{formatDate(selected.due_at)}</strong></div></div>{selected.lock.is_locked && <Notice tone={selected.lock.is_locked_by_me ? 'success' : 'warning'}>Locked by {selected.lock.locked_by_name} · {Math.max(0, Math.ceil((selected.lock.seconds_remaining ?? 0) / 60))} minutes remaining{selected.lock.is_locked_by_me ? '. Renewal runs while this detail is open.' : '.'}</Notice>}<div className="case-actions"><button className="button button-primary" disabled={!can('OPS_CASES_LOCK') || (selected.lock.is_locked && !selected.lock.is_locked_by_me)} title={!can('OPS_CASES_LOCK') ? 'Requires case lock permission' : undefined} onClick={() => void mutate(selected.lock.is_locked_by_me ? 'heartbeat' : 'lock')}>{selected.lock.is_locked_by_me ? 'Renew lock' : 'Claim case'}</button>{selected.lock.is_locked_by_me && <button className="button button-secondary" disabled={!can('OPS_CASES_LOCK')} onClick={() => { if (window.confirm('Release your lock on this case?')) void mutate('release', { reason: 'Released from case details' }) }}>Release lock</button>}{can('OPS_CASES_ASSIGN') && <button className="button button-secondary" onClick={() => startAction('assign', selected)}>Assign</button>}{can('OPS_CASES_TRANSFER') && <button className="button button-secondary" onClick={() => startAction('transfer', selected)}>Transfer</button>}{can('OPS_CASES_CLOSE') && selected.status !== 'CLOSED' && <button className="button button-danger" onClick={() => startAction('close', selected)}>Close case</button>}{selected.deep_link && <a className="button button-quiet" href={selected.deep_link} target="_blank" rel="noreferrer">Open source <OpenInNewIcon fontSize="small" /></a>}</div><div className="case-tabs"><button className={tab === 'activity' ? 'active' : ''} onClick={() => setTab('activity')}>Timeline <span>{events.length}</span></button><button className={tab === 'comments' ? 'active' : ''} onClick={() => setTab('comments')}>Comments <span>{comments.length}</span></button></div>{tab === 'activity' ? <div className="timeline">{events.map((event) => <article className="timeline-item" key={event.id}><span className="timeline-dot" /><div><div className="timeline-head"><strong>{event.event_type.replaceAll('_', ' ')}</strong><time>{formatDate(event.created_at)}</time></div><p>{event.actor_name}{event.reason ? ` · ${event.reason}` : ''}</p>{(event.from_value || event.to_value) && <small>{event.from_value ?? '—'} <span>→</span> {event.to_value ?? '—'}</small>}</div></article>)}{events.length === 0 && <p className="empty-inline">No case events recorded.</p>}</div> : <div className="comment-list">{comments.map((entry) => <article className="comment-item" key={entry.id}><div className="comment-meta"><strong>{entry.author_name}</strong><time>{formatDate(entry.created_at)}</time></div><p>{entry.is_deleted ? 'Comment removed' : entry.comment}</p></article>)}{comments.length === 0 && <p className="empty-inline">No comments yet.</p>}{can('OPS_CASES_COMMENT') && <form className="comment-form" onSubmit={(event) => void addComment(event)}><label className="field"><span>Add a case comment</span><textarea value={comment} onChange={(event) => setComment(event.target.value)} maxLength={5000} required /></label>{commentError && <Notice tone="danger">{commentError}</Notice>}<button className="button button-primary" disabled={!comment.trim()}>Post comment</button></form>}</div>}</div><aside className="case-side"><div className="eyebrow">RECORD</div><dl><dt>Created</dt><dd>{formatDate(selected.created_at)}</dd><dt>Updated</dt><dd>{formatDate(selected.updated_at)}</dd><dt>Last activity</dt><dd>{formatDate(selected.last_activity_at)}</dd><dt>Work age</dt><dd>{selected.age_minutes === null ? '—' : `${Math.floor(selected.age_minutes / 60)}h ${selected.age_minutes % 60}m`}</dd><dt>Allocation</dt><dd>{selected.allocation_explanation ?? 'No allocation note'}</dd><dt>Open comments</dt><dd>{selected.open_comment_count}</dd></dl></aside></div></Modal>}

    {action === 'create' && <Modal title="Create work item" onClose={() => setAction(null)}><form className="form-stack" onSubmit={(event) => void createWork(event)}><label className="field"><span>Title</span><input value={createForm.title} onChange={(event) => setCreateForm({ ...createForm, title: event.target.value })} minLength={3} maxLength={300} required /></label><div className="form-row"><label className="field"><span>Source application</span><select value={createForm.source_app} onChange={(event) => setCreateForm({ ...createForm, source_app: event.target.value })}>{['OPS', 'LOS', 'LMS'].map((value) => <option key={value}>{value}</option>)}</select></label><label className="field"><span>Queue</span><select value={createForm.queue_code} onChange={(event) => setCreateForm({ ...createForm, queue_code: event.target.value })} required><option value="">Select a queue</option>{queues.map((queue) => <option key={queue.id} value={queue.code}>{queue.name}</option>)}</select></label></div><div className="form-row"><label className="field"><span>External reference</span><input value={createForm.external_ref} onChange={(event) => setCreateForm({ ...createForm, external_ref: event.target.value })} maxLength={128} /></label><label className="field"><span>Priority</span><select value={createForm.priority} onChange={(event) => setCreateForm({ ...createForm, priority: event.target.value })}>{['LOW', 'MEDIUM', 'HIGH', 'URGENT'].map((value) => <option key={value}>{value}</option>)}</select></label></div><div className="form-row"><label className="field"><span>Customer name</span><input value={createForm.customer_name} onChange={(event) => setCreateForm({ ...createForm, customer_name: event.target.value })} maxLength={200} /></label><label className="field"><span>Customer mobile</span><input value={createForm.customer_mobile} onChange={(event) => setCreateForm({ ...createForm, customer_mobile: event.target.value })} maxLength={32} inputMode="tel" /></label></div><label className="field"><span>Due date</span><input type="datetime-local" value={createForm.due_at} onChange={(event) => setCreateForm({ ...createForm, due_at: event.target.value })} /></label><label className="check-field"><input type="checkbox" checked={createForm.auto_assign} onChange={(event) => setCreateForm({ ...createForm, auto_assign: event.target.checked })} /><span>Attempt automatic allocation after creation</span></label><Notice tone="info">This creates an Ops work item only. It does not update LOS or LMS. Customer mobile numbers are masked in responses.</Notice><div className="modal-actions"><button type="button" className="button button-secondary" onClick={() => setAction(null)}>Cancel</button><button className="button button-primary" disabled={busy}>{busy ? 'Creating…' : 'Create work item'}</button></div></form></Modal>}

    {(action === 'assign' || action === 'transfer' || action === 'close') && actionItem && <Modal title={action === 'assign' ? 'Assign work item' : action === 'transfer' ? 'Transfer work item' : 'Close work item'} onClose={() => setAction(null)}><form className="form-stack" onSubmit={(event) => void submitAction(event)}>{action === 'assign' && <label className="field"><span>Assignee</span><select value={actionForm.user_id} onChange={(event) => setActionForm({ ...actionForm, user_id: event.target.value })} required><option value="">Select a person</option>{staff.map((person) => <option key={person.id} value={person.id}>{person.display_name} · {person.open_workload} open</option>)}</select></label>}{action === 'transfer' && <><label className="field"><span>Destination queue</span><select value={actionForm.queue_code} onChange={(event) => setActionForm({ ...actionForm, queue_code: event.target.value })}><option value="">Keep current queue</option>{queues.map((queue) => <option key={queue.id} value={queue.code}>{queue.name}</option>)}</select></label><label className="field"><span>Destination person (optional)</span><select value={actionForm.user_id} onChange={(event) => setActionForm({ ...actionForm, user_id: event.target.value })}><option value="">Queue only</option>{staff.map((person) => <option key={person.id} value={person.id}>{person.display_name}</option>)}</select></label></>}{action === 'close' && <><label className="field"><span>Outcome</span><input value={actionForm.outcome} onChange={(event) => setActionForm({ ...actionForm, outcome: event.target.value })} maxLength={120} /></label><label className="field"><span>Closure notes</span><textarea value={actionForm.notes} onChange={(event) => setActionForm({ ...actionForm, notes: event.target.value })} maxLength={2000} /></label></>}<label className="field"><span>{action === 'transfer' ? 'Transfer reason (required)' : action === 'close' ? 'Closure reason (required)' : 'Assignment reason (required)'}</span><textarea value={actionForm.reason} onChange={(event) => setActionForm({ ...actionForm, reason: event.target.value })} minLength={3} maxLength={1000} required /></label><div className="modal-actions"><button type="button" className="button button-secondary" onClick={() => setAction(null)}>Cancel</button><button className={`button ${action === 'close' ? 'button-danger' : 'button-primary'}`} disabled={busy || (action === 'transfer' && !actionForm.queue_code && !actionForm.user_id)}>{busy ? 'Saving…' : action === 'assign' ? 'Assign work' : action === 'transfer' ? 'Transfer work' : 'Close case'}</button></div></form></Modal>}
  </>
}