import { useEffect, useState, type FormEvent, type ReactNode } from 'react'
import DashboardIcon from '@mui/icons-material/DashboardOutlined'
import WorkIcon from '@mui/icons-material/WorkOutlineOutlined'
import PeopleIcon from '@mui/icons-material/PeopleOutlined'
import ApartmentIcon from '@mui/icons-material/ApartmentOutlined'
import QueueIcon from '@mui/icons-material/AccountTreeOutlined'
import EventAvailableIcon from '@mui/icons-material/EventAvailableOutlined'
import ForumIcon from '@mui/icons-material/ForumOutlined'
import FactCheckIcon from '@mui/icons-material/FactCheckOutlined'
import HubIcon from '@mui/icons-material/HubOutlined'
import LogoutIcon from '@mui/icons-material/Logout'
import MenuIcon from '@mui/icons-material/Menu'
import { ApiError, list, send, setUnauthorizedHandler, tokenStore } from './lib/api'
import type { CurrentUser, LoginResponse, Permission } from './types'
import { DashboardPage } from './pages/DashboardPage'
import { WorkbenchPage } from './pages/WorkbenchPage'
import { DirectoryPage } from './pages/DirectoryPage'
import { AvailabilityPage } from './pages/AvailabilityPage'
import { CommunicationsPage } from './pages/CommunicationsPage'
import { OperationsPage } from './pages/OperationsPage'
import './App.css'

type Section = 'dashboard' | 'workbench' | 'users' | 'departments' | 'queues' | 'availability' | 'communications' | 'audit' | 'integrations'

const sections: { id: Section; label: string; permission: Permission; icon: typeof DashboardIcon }[] = [
  { id: 'dashboard', label: 'Overview', permission: 'OPS_DASHBOARD_READ', icon: DashboardIcon },
  { id: 'workbench', label: 'Workbench', permission: 'OPS_CASES_READ', icon: WorkIcon },
  { id: 'users', label: 'People', permission: 'OPS_USERS_READ', icon: PeopleIcon },
  { id: 'departments', label: 'Departments', permission: 'OPS_DEPARTMENTS_READ', icon: ApartmentIcon },
  { id: 'queues', label: 'Queues', permission: 'OPS_QUEUES_READ', icon: QueueIcon },
  { id: 'availability', label: 'Availability', permission: 'OPS_AVAILABILITY_READ', icon: EventAvailableIcon },
  { id: 'communications', label: 'Communications', permission: 'OPS_COMMUNICATION_READ', icon: ForumIcon },
  { id: 'audit', label: 'Audit trail', permission: 'OPS_AUDIT_READ', icon: FactCheckIcon },
  { id: 'integrations', label: 'Integrations', permission: 'OPS_INTEGRATION_READ', icon: HubIcon },
]

function App() {
  const [user, setUser] = useState<CurrentUser | null>(null)
  const [checking, setChecking] = useState(true)
  const [section, setSection] = useState<Section>('dashboard')
  const [navOpen, setNavOpen] = useState(false)
  const [apiNotice, setApiNotice] = useState<string | null>(null)

  useEffect(() => {
    setUnauthorizedHandler(() => {
      tokenStore.clear()
      setUser(null)
    })
    if (!tokenStore.access) {
      setChecking(false)
      return
    }
    void send<CurrentUser>('get', '/auth/me')
      .then((current) => setUser(current))
      .catch((error: unknown) => {
        tokenStore.clear()
        if (error instanceof ApiError && error.status !== 401) setApiNotice(error.message)
      })
      .finally(() => setChecking(false))
  }, [])

  async function signIn(username: string, password: string, tenantId: string, mfaToken?: string, code?: string) {
    const response = mfaToken
      ? await send<LoginResponse>('post', '/auth/mfa/verify', { mfa_token: mfaToken, code })
      : await send<LoginResponse>('post', '/auth/login', { username, password, tenant_id: tenantId })
    if (response.mfa_required && response.mfa_token) return { mfaToken: response.mfa_token }
    if (!response.access_token) throw new Error('The identity service did not return an access token.')
    tokenStore.set(response.access_token, response.refresh_token)
    const current = await send<CurrentUser>('get', '/auth/me')
    setUser(current)
    setApiNotice(null)
    return { mfaToken: null }
  }

  async function signOut() {
    const refreshToken = tokenStore.refresh
    try {
      await send('post', '/auth/logout', { refresh_token: refreshToken })
    } catch {
      // Local session removal still completes if the API is unavailable.
    }
    tokenStore.clear()
    setUser(null)
  }

  if (checking) return <div className="boot-screen"><span className="brand-mark">OC</span><span>Connecting to Ops Control Center</span></div>
  if (!user) return <LoginScreen onSignIn={signIn} notice={apiNotice} />

  const visibleSections = sections.filter((item) => user.permissions.includes(item.permission))
  const activeSection = visibleSections.some((item) => item.id === section) ? section : visibleSections[0]?.id
  const current = visibleSections.find((item) => item.id === activeSection)

  return (
    <div className="app-frame">
      <aside className={`sidebar ${navOpen ? 'sidebar-open' : ''}`}>
        <div className="brand-lockup"><span className="brand-mark">OC</span><span><strong>Ops Control</strong><small>BP Securities · {user.tenant_id}</small></span></div>
        <div className="nav-caption">WORKSPACE</div>
        <nav aria-label="Main navigation">
          {visibleSections.map((item) => {
            const Icon = item.icon
            return <button key={item.id} className={`nav-item ${activeSection === item.id ? 'selected' : ''}`} onClick={() => { setSection(item.id); setNavOpen(false) }}><Icon fontSize="small" /><span>{item.label}</span></button>
          })}
        </nav>
        <div className="sidebar-foot"><span className="health-dot" /> API session active</div>
      </aside>
      {navOpen && <button className="mobile-scrim" aria-label="Close navigation" onClick={() => setNavOpen(false)} />}
      <main className="main-area">
        <header className="topbar">
          <button className="icon-button mobile-menu" title="Open navigation" onClick={() => setNavOpen(true)}><MenuIcon /></button>
          <div className="crumb"><span>Operations</span><span className="crumb-divider">/</span><strong>{current?.label ?? 'Workspace'}</strong></div>
          <div className="topbar-actions"><span className="tenant-tag">{user.tenant_id}</span><span className="user-chip"><span className="avatar">{user.display_name.slice(0, 1).toUpperCase()}</span><span className="user-name">{user.display_name}</span></span><button className="icon-button" title="Sign out" onClick={() => void signOut()}><LogoutIcon /></button></div>
        </header>
        <div className="content-area">
          {activeSection === 'dashboard' && <DashboardPage user={user} onNavigate={(next) => setSection(next as Section)} />}
          {activeSection === 'workbench' && <WorkbenchPage user={user} />}
          {['users', 'departments', 'queues'].includes(activeSection ?? '') && <DirectoryPage section={activeSection as 'users' | 'departments' | 'queues'} user={user} />}
          {activeSection === 'availability' && <AvailabilityPage user={user} />}
          {activeSection === 'communications' && <CommunicationsPage user={user} />}
          {['audit', 'integrations'].includes(activeSection ?? '') && <OperationsPage section={activeSection as 'audit' | 'integrations'} user={user} />}
          {!activeSection && <div className="empty-state"><h2>No workspace access</h2><p>Your account does not have any Ops module permissions.</p></div>}
        </div>
        <footer className="app-footer">Ops Control Center <span>·</span> Operational workspace</footer>
      </main>
    </div>
  )
}

export default App

function LoginScreen({ onSignIn, notice }: { onSignIn: (username: string, password: string, tenantId: string, mfaToken?: string, code?: string) => Promise<{ mfaToken: string | null }>; notice: string | null }) {
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [tenantId, setTenantId] = useState('BPSEC')
  const [mfaToken, setMfaToken] = useState<string | null>(null)
  const [code, setCode] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  async function submit(event: FormEvent) {
    event.preventDefault()
    setBusy(true)
    setError(null)
    try {
      const result = await onSignIn(username, password, tenantId, mfaToken ?? undefined, code || undefined)
      setMfaToken(result.mfaToken)
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : 'Sign-in failed.')
    } finally {
      setBusy(false)
    }
  }

  return <div className="login-layout"><section className="login-aside"><div className="brand-lockup brand-lockup-light"><span className="brand-mark">OC</span><span><strong>Ops Control</strong><small>BP Securities</small></span></div><div className="login-message"><div className="eyebrow">OPERATIONS WORKSPACE</div><h1>Clarity for the work that moves us forward.</h1><p>Workforce, availability and case ownership in one control plane.</p></div><div className="login-rail"><span>BRE identity</span><i /><span>Operational control</span><i /><span>Auditable actions</span></div></section><section className="login-panel"><form className="login-form" onSubmit={(event) => void submit(event)}><div className="eyebrow">SECURE SIGN IN</div><h2>{mfaToken ? 'Verify your identity' : 'Welcome back'}</h2><p className="muted">{mfaToken ? 'Enter the verification code from your identity provider.' : 'Use your BRE Identity credentials to continue.'}</p>{notice && <Notice tone="warning">{notice}</Notice>}{error && <Notice tone="danger">{error}</Notice>}{!mfaToken ? <><label className="field"><span>Tenant</span><input value={tenantId} onChange={(event) => setTenantId(event.target.value)} autoComplete="organization" required /></label><label className="field"><span>Username</span><input value={username} onChange={(event) => setUsername(event.target.value)} autoComplete="username" required /></label><label className="field"><span>Password</span><input type="password" value={password} onChange={(event) => setPassword(event.target.value)} autoComplete="current-password" required /></label></> : <label className="field"><span>Verification code</span><input value={code} onChange={(event) => setCode(event.target.value)} autoComplete="one-time-code" inputMode="numeric" required /></label>}<button className="button button-primary button-wide" disabled={busy}>{busy ? 'Connecting…' : mfaToken ? 'Verify and continue' : 'Continue'}<span aria-hidden="true">→</span></button><div className="login-security"><span className="health-dot" /> Authentication handled by BRE Identity</div></form></section></div>
}

export function PageHeading({ eyebrow, title, description, actions }: { eyebrow?: string; title: string; description?: string; actions?: ReactNode }) {
  return <div className="page-heading"><div>{eyebrow && <div className="eyebrow">{eyebrow}</div>}<h1>{title}</h1>{description && <p>{description}</p>}</div>{actions && <div className="heading-actions">{actions}</div>}</div>
}

export function Notice({ children, tone = 'info' }: { children: ReactNode; tone?: 'info' | 'warning' | 'danger' | 'success' }) {
  return <div role="status" className={`notice notice-${tone}`}>{children}</div>
}

export function Modal({ title, onClose, children, wide = false }: { title: string; onClose: () => void; children: ReactNode; wide?: boolean }) {
  return <div className="modal-scrim" role="presentation" onMouseDown={(event) => { if (event.target === event.currentTarget) onClose() }}><section className={`modal ${wide ? 'modal-wide' : ''}`} role="dialog" aria-modal="true" aria-label={title}><header className="modal-head"><h2>{title}</h2><button className="icon-button" title="Close" onClick={onClose}>×</button></header>{children}</section></div>
}

export function Pager({ page, total, pageSize, onPage }: { page: number; total: number; pageSize: number; onPage: (page: number) => void }) {
  const pages = Math.max(1, Math.ceil(total / pageSize))
  return <div className="pager"><span>{total === 0 ? 'No records' : `${(page - 1) * pageSize + 1}–${Math.min(page * pageSize, total)} of ${total}`}</span><div><button className="button button-quiet" disabled={page <= 1} onClick={() => onPage(page - 1)}>Previous</button><span className="page-count">{page} / {pages}</span><button className="button button-quiet" disabled={page >= pages} onClick={() => onPage(page + 1)}>Next</button></div></div>
}

export function StatusTag({ children, tone }: { children: ReactNode; tone?: string }) {
  const value = tone ?? String(children).toLowerCase().replaceAll('_', '-')
  return <span className={`status-tag status-${value}`}>{children}</span>
}

export function SearchBox({ value, onChange, placeholder = 'Search records' }: { value: string; onChange: (value: string) => void; placeholder?: string }) {
  return <label className="search-box"><span className="sr-only">{placeholder}</span><span aria-hidden="true">⌕</span><input value={value} onChange={(event) => onChange(event.target.value)} placeholder={placeholder} /></label>
}

export function usePageData<T>(path: string, params: Record<string, unknown>, refreshKey = 0) {
  const [data, setData] = useState<T[]>([])
  const [total, setTotal] = useState(0)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const query = JSON.stringify(params)
  useEffect(() => {
    let alive = true
    setLoading(true)
    void list<T>(path, JSON.parse(query) as Record<string, unknown>)
      .then((response) => { if (alive) { setData(response.data); setTotal(response.meta.total); setError(null) } })
      .catch((reason: unknown) => { if (alive) setError(reason instanceof Error ? reason.message : 'Could not load records.') })
      .finally(() => { if (alive) setLoading(false) })
    return () => { alive = false }
  }, [path, query, refreshKey])
  return { data, total, loading, error, setError }
}

export function formatDate(value: string | null | undefined) {
  if (!value) return '—'
  return new Intl.DateTimeFormat(undefined, { dateStyle: 'medium', timeStyle: 'short' }).format(new Date(value))
}

export function shortRef(value: string | null | undefined) {
  return value ? value.length > 22 ? `${value.slice(0, 10)}…${value.slice(-8)}` : value : '—'
}

export { list, send, ApiError }
