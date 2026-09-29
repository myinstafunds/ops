/**
 * Wire types mirroring the FastAPI schemas.
 *
 * These are hand written rather than generated so that the compiler will
 * catch a contract change at the point of use. When the API changes, fix the
 * type here first, then the screens - never by widening to `any`.
 */

export type WorkStatus = 'OPEN' | 'ASSIGNED' | 'IN_PROGRESS' | 'ON_HOLD' | 'CLOSED' | 'CANCELLED'
export type Priority = 'LOW' | 'MEDIUM' | 'HIGH' | 'URGENT'
export type SlaStatus = 'NOT_STARTED' | 'ON_TRACK' | 'DUE_SOON' | 'BREACHED' | 'COMPLETED'
export type SourceApp = 'OPS' | 'LOS' | 'LMS'
export type OrgStatus = 'ACTIVE' | 'INACTIVE'
export type OperationalStatus = 'ACTIVE' | 'INACTIVE' | 'SUSPENDED'
export type AvailabilityType = 'LEAVE' | 'ABSENT' | 'BREAK' | 'TRAINING' | 'OFFLINE'
export type AvailabilityRequestStatus = 'REQUESTED' | 'APPROVED' | 'REJECTED' | 'CANCELLED'
export type Channel = 'CALL' | 'SMS' | 'WHATSAPP' | 'EMAIL' | 'RCS'
export type Direction = 'INBOUND' | 'OUTBOUND'
export type CommunicationStatus =
  | 'PENDING'
  | 'SENT'
  | 'DELIVERED'
  | 'FAILED'
  | 'CONNECTED'
  | 'NO_RESPONSE'

/** Every permission the backend recognises, mirroring `app/security/permissions.py`. */
export type Permission =
  | 'OPS_DASHBOARD_READ'
  | 'OPS_USERS_READ'
  | 'OPS_USERS_WRITE'
  | 'OPS_ROLES_READ'
  | 'OPS_DEPARTMENTS_READ'
  | 'OPS_DEPARTMENTS_WRITE'
  | 'OPS_QUEUES_READ'
  | 'OPS_QUEUES_WRITE'
  | 'OPS_CASES_READ'
  | 'OPS_CASES_CREATE'
  | 'OPS_CASES_ASSIGN'
  | 'OPS_CASES_LOCK'
  | 'OPS_CASES_TRANSFER'
  | 'OPS_CASES_COMMENT'
  | 'OPS_CASES_CLOSE'
  | 'OPS_AVAILABILITY_READ'
  | 'OPS_AVAILABILITY_WRITE'
  | 'OPS_AVAILABILITY_APPROVE'
  | 'OPS_COMMUNICATION_READ'
  | 'OPS_COMMUNICATION_WRITE'
  | 'OPS_AUDIT_READ'
  | 'OPS_REPORTS_READ'
  | 'OPS_EXPORT_REQUEST'
  | 'OPS_INTEGRATION_READ'
  | 'OPS_SETTINGS_WRITE'

/* -------------------------------------------------------------------------- */
/* envelopes                                                                  */
/* -------------------------------------------------------------------------- */

export interface PageMeta {
  page: number
  page_size: number
  total: number
  total_pages: number
  has_next: boolean
  has_previous: boolean
}

export interface PageResponse<T> {
  data: T[]
  meta: PageMeta
  trace_id: string | null
}

export interface ErrorBody {
  code: string
  message: string
  details: Record<string, unknown>
}

export interface ErrorResponse {
  error: ErrorBody
  trace_id: string | null
}

/* -------------------------------------------------------------------------- */
/* auth                                                                       */
/* -------------------------------------------------------------------------- */

export interface LoginResponse {
  access_token: string | null
  refresh_token: string | null
  token_type: string
  expires_in: number | null
  mfa_required: boolean
  mfa_token: string | null
  user: CurrentUser | null
}

export interface CurrentUser {
  id: string
  username: string
  display_name: string
  email: string | null
  tenant_id: string
  status: OperationalStatus
  is_available: boolean
  capacity: number
  bre_user_id: string | null
  bre_roles: string[]
  roles: string[]
  permissions: Permission[]
  departments: string[]
  queues: string[]
}

/* -------------------------------------------------------------------------- */
/* work items                                                                 */
/* -------------------------------------------------------------------------- */

export interface LockInfo {
  is_locked: boolean
  is_locked_by_me: boolean
  locked_by_user_id: string | null
  locked_by_name: string | null
  locked_at: string | null
  locked_until: string | null
  seconds_remaining: number | null
  is_expired: boolean
  last_heartbeat_at: string | null
}

export interface WorkItemSummary {
  id: string
  source_app: SourceApp
  source_key: string | null
  external_ref: string | null
  deep_link: string | null
  title: string
  customer_name: string | null
  customer_mobile_masked: string | null
  queue_code: string
  department_code: string | null
  status: WorkStatus
  priority: Priority
  sla_status: SlaStatus
  assigned_user_id: string | null
  assigned_user_name: string | null
  due_at: string | null
  created_at: string
  updated_at: string
  closed_at: string | null
  last_activity_at: string | null
  lock: LockInfo
  age_minutes: number | null
  is_overdue: boolean
}

export interface WorkItemDetail extends WorkItemSummary {
  assigned_at: string | null
  assignment_is_manual: boolean
  allocation_explanation: string | null
  sla_breached_at: string | null
  due_at_overridden: boolean
  sla_policy_id: string | null
  closure_outcome: string | null
  closure_notes: string | null
  payload: Record<string, unknown>
  open_comment_count: number
  last_synced_at: string | null
  source_fields: Record<string, unknown>
}

export interface AutoAssignResponse {
  work_item: WorkItemDetail
  assignee_user_id: string
  assignee_name: string
  explanation: string
  explanation_factors: string[]
  candidates_considered: number
}

export interface ManualAssignResponse {
  work_item: WorkItemDetail
  explanation: string
}

export interface HeartbeatResponse {
  work_item_id: string
  locked_until: string
  seconds_remaining: number
  lock_count: number
  last_heartbeat_at: string
}

export interface CandidateItem {
  user_id: string
  display_name: string
  username: string
  current_workload: number
  capacity: number
  priority_weight: number
  specialisation_match: boolean
  is_available: boolean
  daily_allocations_today: number
  eligible: boolean
  ineligible_reasons: string[]
}

export interface EligibilityResponse {
  queue_code: string
  candidates: CandidateItem[]
  eligible_count: number
  checked_at: string
}

export interface WorkItemEventItem {
  id: string
  event_type: string
  actor_user_id: string | null
  actor_name: string
  from_value: string | null
  to_value: string | null
  reason: string | null
  metadata: Record<string, unknown>
  created_at: string
}

export interface CommentItem {
  id: string
  work_item_id: string
  author_user_id: string | null
  author_name: string
  comment: string
  created_at: string
  updated_at: string
  edit_count: number
  is_deleted: boolean
  deleted_at: string | null
  can_edit: boolean
  can_delete: boolean
}

/* -------------------------------------------------------------------------- */
/* org                                                                        */
/* -------------------------------------------------------------------------- */

export interface UserListItem {
  id: string
  username: string
  display_name: string
  email: string | null
  status: OperationalStatus
  is_available: boolean
  capacity: number
  priority_weight: number
  tenant_id: string
  job_title: string | null
  roles: string[]
  bre_roles: string[]
  open_workload: number
  department_codes: string[]
  queue_codes: string[]
  current_availability: string | null
}

export interface UserDetail extends UserListItem {
  phone_masked: string | null
  specialisations: string[]
  daily_allocation_limit: number
  last_login_at: string | null
  created_at: string
  updated_at: string
  permissions: Permission[]
  locked_case_count: number
  available_now: boolean
  unavailable_until: string | null
}

export interface QueueSummary {
  id: string
  code: string
  name: string
  source_app: SourceApp
  status: OrgStatus
  priority: number
  max_workload_per_user: number
  open_items: number
  available_members: number
  member_count: number
}

export interface QueueDetail extends QueueSummary {
  description: string | null
  department_code: string | null
  eligible_roles: string[]
  required_specialisations: string[]
  sla_policy_id: string | null
  sla_target_minutes: number | null
  sla_warning_minutes: number | null
  sync_interval_minutes: number | null
  created_at: string
  updated_at: string
}

export interface DepartmentSummary {
  id: string
  code: string
  name: string
  source_app: SourceApp
  status: OrgStatus
  member_count: number
}

export interface DepartmentDetail extends DepartmentSummary {
  description: string | null
  created_at: string
  updated_at: string
}

export interface QueueMembershipItem {
  id: string
  queue_id: string
  queue_code: string
  queue_name: string
  user_id: string
  user_name: string
  username: string
  is_active: boolean
  capacity: number | null
  priority_weight: number
  daily_max_allocations: number | null
  specialisation: string | null
  shift_start: string | null
  shift_end: string | null
  open_workload: number
  available_now: boolean
}

export interface DepartmentMembershipItem {
  id: string
  department_id: string
  department_code: string
  department_name: string
  user_id: string
  user_name: string
  username: string
  is_primary: boolean
  is_active: boolean
  created_at: string
}

/* -------------------------------------------------------------------------- */
/* availability                                                               */
/* -------------------------------------------------------------------------- */

export interface AvailabilityItem {
  id: string
  user_id: string
  user_name: string
  username: string
  availability_type: AvailabilityType
  status: AvailabilityRequestStatus
  start_at: string
  end_at: string
  reason: string | null
  notes: string | null
  created_by_name: string | null
  created_at: string
  decided_by_name: string | null
  decided_at: string | null
  decision_reason: string | null
  is_active_window: boolean
  blocks_allocation: boolean
  edit_count?: number
}

/* -------------------------------------------------------------------------- */
/* communications + audit + integrations                                      */
/* -------------------------------------------------------------------------- */

export interface CommunicationItem {
  id: string
  work_item_id: string | null
  work_item_ref: string | null
  channel: Channel
  direction: Direction
  status: CommunicationStatus
  customer_reference: string | null
  customer_name: string | null
  customer_mobile_masked: string | null
  actor_name: string | null
  subject: string | null
  body_preview: string | null
  outcome_notes: string | null
  disposition: string | null
  provider_message_id: string | null
  retry_count: number
  occurred_at: string
  delivered_at: string | null
  duration_seconds: number | null
  created_at: string
}

export interface AuditLogItem {
  id: string
  actor_user_id: string | null
  actor_name: string
  action: string
  entity_type: string
  entity_id: string | null
  before_json: Record<string, unknown> | null
  after_json: Record<string, unknown> | null
  changed_fields: string | null
  reason: string | null
  ip_address: string | null
  user_agent: string | null
  trace_id: string | null
  is_system_action: boolean
  created_at: string
}

export interface IntegrationHealthItem {
  source_app: SourceApp
  status: string
  last_success_at: string | null
  last_failure_at: string | null
  last_sync_at: string | null
  latency_ms: number | null
  error_count: number
  records_processed: number
  records_failed: number
  consecutive_failures: number
  last_error_summary: string | null
}

export interface SyncRunItem {
  id: string
  source_app: SourceApp
  status: string
  job_name: string
  queue_code: string | null
  started_at: string | null
  completed_at: string | null
  records_fetched: number
  records_created: number
  records_updated: number
  records_skipped: number
  records_failed: number
  error_summary: string | null
  trigger_type: string
  duration_seconds: number | null
}

/* -------------------------------------------------------------------------- */
/* dashboard                                                                  */
/* -------------------------------------------------------------------------- */

export interface StatusCount {
  status: string
  count: number
}

export interface QueueWorkloadRow {
  queue_code: string
  queue_name: string
  source_app: SourceApp
  open_count: number
  unassigned_count: number
  assigned_count: number
  overdue_count: number
  eligible_members: number
  available_members: number
  capacity_utilisation_pct: number
}

export interface UserWorkloadRow {
  user_id: string
  display_name: string
  username: string
  is_available: boolean
  open_workload: number
  capacity: number
  utilisation_pct: number
  locked_cases: number
  breached_cases: number
  department_codes: string[]
}

export interface AgeingBucket {
  label: string
  min_hours: number
  max_hours: number | null
  count: number
}

export interface AgeingResponse {
  buckets: AgeingBucket[]
  total: number
  generated_at: string
}

export interface SlaStats {
  not_started: number
  on_track: number
  due_soon: number
  breached: number
  completed: number
  total_open: number
  breach_rate_pct: number
  generated_at: string
}

export interface ActionAlert {
  key: string
  severity: string
  title: string
  detail: string
  count: number
  link_path: string
  filter_params: Record<string, string>
}

export interface DashboardResponse {
  summary: SummaryStats
  work_by_status: StatusCount[]
  work_by_queue: QueueWorkloadRow[]
  work_by_department: StatusCount[]
  user_workload: UserWorkloadRow[]
  ageing: AgeingResponse
  sla: SlaStats
  alerts: ActionAlert[]
  integration_health: IntegrationHealthItem[]
  generated_at: string
}

export interface SummaryStats {
  open_work: number
  assigned_work: number
  in_progress_work: number
  unassigned_work: number
  locked_cases: number
  sla_breaches: number
  due_today: number
  unavailable_staff: number
  active_queue_members: number
  pending_approvals: number
  open_queues: number
  generated_at: string
}
