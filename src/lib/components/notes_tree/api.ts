import { WEBUI_API_BASE_URL } from '$lib/constants';

export type NodeKind = 'folder' | 'note' | 'daily_note' | 'task_draft';
export type PartKind = 'section' | 'paragraph' | 'list' | 'item' | 'fence' | 'table' | 'quote' | 'rule';
export type ListStyle = 'unordered' | 'ordered';
export type TargetKind = 'project_idea' | 'project_task' | 'imported_note';

export interface TreeNoteStatus {
	enabled: boolean;
	promote_enabled: boolean;
}

export interface BasisConflictResponse {
	error: 'basis_conflict';
	node_id: string;
	expected_basis_sha256: string | null;
	current_basis_sha256: string;
	part_id: string | null;
	expected_part_basis_sha256: string | null;
	current_part_basis_sha256: string | null;
	current_updated_at: number;
}

export interface BreadcrumbItem {
	id: string;
	title: string;
	node_kind: NodeKind;
}

export interface ExternalRefItem {
	id: string;
	target_kind: TargetKind;
	external_id: string;
	created_at: number;
}

export interface PartItem {
	id: string;
	node_id: string;
	parent_part_id: string | null;
	position: number;
	part_kind: PartKind;
	heading_level: number | null;
	title: string | null;
	content_markdown: string;
	list_style: ListStyle | null;
	language: string | null;
	checked: boolean | null;
	updated_at: number;
	part_basis_sha256: string;
	part_path: string;
}

export interface NodeSummary {
	id: string;
	parent_id: string | null;
	sort_order: number;
	title: string;
	node_kind: NodeKind;
	note_date: string | null;
	archived_at: number | null;
	created_at: number;
	updated_at: number;
	tags: string[];
	child_count: number;
}

export interface TreeNodeItem {
	node: NodeSummary;
	children: TreeNodeItem[];
}

export interface TreeResponse {
	items: TreeNodeItem[];
	total_nodes: number;
}

export interface CalendarDayEntry {
	note_date: string;
	node: NodeSummary;
	children: TreeNodeItem[];
}

export interface CalendarMonthGroup {
	month: number;
	month_label: string;
	days: CalendarDayEntry[];
}

export interface CalendarYearGroup {
	year: number;
	months: CalendarMonthGroup[];
}

export interface CalendarResponse {
	years: CalendarYearGroup[];
	total_days: number;
}

export interface SearchHit {
	node: NodeSummary;
	snippet: string;
	matched_part_id: string | null;
	matched_part_path: string | null;
	breadcrumbs: BreadcrumbItem[];
}

export interface SearchResponse {
	items: SearchHit[];
	total: number;
}

export interface ReadNodeResponse {
	id: string;
	parent_id: string | null;
	sort_order: number;
	title: string;
	node_kind: NodeKind;
	note_date: string | null;
	archived_at: number | null;
	created_at: number;
	updated_at: number;
	basis_sha256: string;
	rendered_markdown: string;
	tags: string[];
	breadcrumbs: BreadcrumbItem[];
	parts: PartItem[];
	external_refs: ExternalRefItem[];
	children: NodeSummary[];
}

export interface DailyResponse {
	created: boolean;
	note_date: string;
	node: ReadNodeResponse;
}

export interface PartMutationResponse {
	node: ReadNodeResponse;
	part: PartItem;
}

export interface PartDeleteResponse {
	deleted_part_ids: string[];
	node: ReadNodeResponse;
}

export interface PromoteResponse {
	ref: ExternalRefItem;
	target_url: string | null;
	status: string;
}

export interface ImportLegacyItemResult {
	legacy_note_id: string;
	title: string;
	status: 'imported' | 'skipped_existing' | 'would_import' | 'failed';
	node_id: string | null;
	page_count: number;
	error: string | null;
}

export interface ImportLegacyResponse {
	dry_run: boolean;
	imported_count: number;
	skipped_count: number;
	failed_count: number;
	items: ImportLegacyItemResult[];
}

export class TreeNoteApiError extends Error {
	status: number;
	code: string;
	conflict: BasisConflictResponse | null;

	constructor(status: number, code: string, message: string, conflict: BasisConflictResponse | null = null) {
		super(message);
		this.name = 'TreeNoteApiError';
		this.status = status;
		this.code = code;
		this.conflict = conflict;
	}
}

const BASE_PATH = `${WEBUI_API_BASE_URL}/gateway-bridge/tree-notes`;

function authHeaders(token: string, jsonBody = false): Record<string, string> {
	const headers: Record<string, string> = {};
	if (token) {
		headers.Authorization = `Bearer ${token}`;
	}
	if (jsonBody) {
		headers['Content-Type'] = 'application/json';
		headers.Accept = 'application/json';
	}
	return headers;
}

async function requestJson<T>(
	path: string,
	token: string,
	init: RequestInit = {},
	jsonBody = false
): Promise<T> {
	const response = await fetch(`${BASE_PATH}${path}`, {
		cache: 'no-store',
		credentials: 'same-origin',
		...init,
		headers: {
			...authHeaders(token, jsonBody),
			...(init.headers as Record<string, string> | undefined)
		}
	});
	if (!response.ok) {
		let body: Record<string, unknown> = {};
		try {
			body = (await response.json()) as Record<string, unknown>;
		} catch {
			body = {};
		}
		if (response.status === 409 && body.error === 'basis_conflict') {
			const conflict = body as unknown as BasisConflictResponse;
			throw new TreeNoteApiError(
				409,
				'basis_conflict',
				'Note changed concurrently since it was loaded.',
				conflict
			);
		}
		const code = typeof body.error === 'string' ? body.error : `http_${response.status}`;
		const detail =
			typeof body.detail === 'string'
				? body.detail
				: typeof body.error === 'string'
					? body.error
					: `Request failed (${response.status})`;
		throw new TreeNoteApiError(response.status, code, detail, null);
	}
	return (await response.json()) as T;
}

export async function fetchTreeNotesStatus(signal?: AbortSignal): Promise<TreeNoteStatus> {
	try {
		const token = typeof localStorage !== 'undefined' ? (localStorage.token ?? '') : '';
		const response = await fetch(`${BASE_PATH}/status`, {
			cache: 'no-store',
			credentials: 'same-origin',
			headers: token ? { Authorization: `Bearer ${token}` } : {},
			signal
		});
		if (!response.ok) {
			return { enabled: false, promote_enabled: false };
		}
		const payload = (await response.json()) as Partial<TreeNoteStatus>;
		return {
			enabled: payload.enabled === true,
			promote_enabled: payload.promote_enabled === true
		};
	} catch {
		return { enabled: false, promote_enabled: false };
	}
}

export async function getTree(
	token: string,
	options: { includeArchived?: boolean; includeDaily?: boolean; rootId?: string | null } = {}
): Promise<TreeResponse> {
	const params = new URLSearchParams();
	if (options.includeArchived) params.set('include_archived', 'true');
	if (options.includeDaily) params.set('include_daily', 'true');
	if (options.rootId) params.set('root_id', options.rootId);
	const suffix = params.toString() ? `?${params.toString()}` : '';
	return requestJson<TreeResponse>(`/tree${suffix}`, token, { method: 'GET' });
}

export async function getCalendar(
	token: string,
	options: { year?: number; month?: number } = {}
): Promise<CalendarResponse> {
	const params = new URLSearchParams();
	if (options.year) params.set('year', String(options.year));
	if (options.month) params.set('month', String(options.month));
	const suffix = params.toString() ? `?${params.toString()}` : '';
	return requestJson<CalendarResponse>(`/calendar${suffix}`, token, { method: 'GET' });
}

export async function searchTreeNotes(
	token: string,
	options: {
		query?: string;
		tag?: string | null;
		nodeKind?: NodeKind | null;
		includeArchived?: boolean;
		limit?: number;
		offset?: number;
	} = {}
): Promise<SearchResponse> {
	const params = new URLSearchParams();
	if (options.query) params.set('query', options.query);
	if (options.tag) params.set('tag', options.tag);
	if (options.nodeKind) params.set('node_kind', options.nodeKind);
	if (options.includeArchived) params.set('include_archived', 'true');
	if (options.limit !== undefined) params.set('limit', String(options.limit));
	if (options.offset !== undefined) params.set('offset', String(options.offset));
	const suffix = params.toString() ? `?${params.toString()}` : '';
	return requestJson<SearchResponse>(`/search${suffix}`, token, { method: 'GET' });
}

export async function getTreeNote(token: string, nodeId: string): Promise<ReadNodeResponse> {
	return requestJson<ReadNodeResponse>(`/nodes/${encodeURIComponent(nodeId)}`, token, { method: 'GET' });
}

export async function createTreeNote(
	token: string,
	payload: {
		title: string;
		node_kind?: 'folder' | 'note' | 'task_draft';
		parent_id?: string | null;
		markdown?: string;
		tags?: string[];
	}
): Promise<ReadNodeResponse> {
	return requestJson<ReadNodeResponse>(
		'/nodes',
		token,
		{ method: 'POST', body: JSON.stringify(payload) },
		true
	);
}

export async function openDailyDate(token: string, noteDate: string): Promise<DailyResponse> {
	return requestJson<DailyResponse>(`/daily/${encodeURIComponent(noteDate)}`, token, { method: 'POST' }, true);
}

export async function openToday(token: string, tz?: string | null): Promise<DailyResponse> {
	return requestJson<DailyResponse>(
		'/daily/today',
		token,
		{ method: 'POST', body: JSON.stringify(tz ? { tz } : {}) },
		true
	);
}

export async function updateTreeNote(
	token: string,
	nodeId: string,
	payload: {
		expected_basis_sha256: string;
		title?: string;
		node_kind?: 'folder' | 'note' | 'task_draft';
	}
): Promise<ReadNodeResponse> {
	return requestJson<ReadNodeResponse>(
		`/nodes/${encodeURIComponent(nodeId)}`,
		token,
		{ method: 'PATCH', body: JSON.stringify(payload) },
		true
	);
}

export async function putNodeMarkdown(
	token: string,
	nodeId: string,
	payload: { expected_basis_sha256: string; markdown: string }
): Promise<ReadNodeResponse> {
	return requestJson<ReadNodeResponse>(
		`/nodes/${encodeURIComponent(nodeId)}/markdown`,
		token,
		{ method: 'PUT', body: JSON.stringify(payload) },
		true
	);
}

export async function moveTreeNote(
	token: string,
	nodeId: string,
	payload: {
		expected_basis_sha256: string;
		new_parent_id: string | null;
		target_position?: number | null;
	}
): Promise<ReadNodeResponse> {
	return requestJson<ReadNodeResponse>(
		`/nodes/${encodeURIComponent(nodeId)}/move`,
		token,
		{ method: 'POST', body: JSON.stringify(payload) },
		true
	);
}

export async function reorderTreeNodes(
	token: string,
	payload: {
		parent_id: string | null;
		ordered_node_ids: string[];
		expected_sibling_ids: string[];
	}
): Promise<TreeResponse> {
	return requestJson<TreeResponse>(
		'/nodes/reorder',
		token,
		{ method: 'POST', body: JSON.stringify(payload) },
		true
	);
}

export async function archiveTreeNote(
	token: string,
	nodeId: string,
	payload: { expected_basis_sha256: string; archived: boolean }
): Promise<ReadNodeResponse> {
	return requestJson<ReadNodeResponse>(
		`/nodes/${encodeURIComponent(nodeId)}/archive`,
		token,
		{ method: 'POST', body: JSON.stringify(payload) },
		true
	);
}

export async function deleteTreeNote(
	token: string,
	nodeId: string,
	expectedBasisSha256: string,
	recursive = false
): Promise<{ deleted_node_ids: string[] }> {
	return requestJson<{ deleted_node_ids: string[] }>(
		`/nodes/${encodeURIComponent(nodeId)}`,
		token,
		{
			method: 'DELETE',
			body: JSON.stringify({ expected_basis_sha256: expectedBasisSha256, recursive })
		},
		true
	);
}

export async function createPart(
	token: string,
	nodeId: string,
	payload: {
		parent_part_id?: string | null;
		position?: number | null;
		part_kind: PartKind;
		heading_level?: number | null;
		title?: string | null;
		content_markdown?: string;
		list_style?: ListStyle | null;
		language?: string | null;
		checked?: boolean | null;
		expected_basis_sha256?: string | null;
		expected_part_basis_sha256?: string | null;
	}
): Promise<PartMutationResponse> {
	return requestJson<PartMutationResponse>(
		`/nodes/${encodeURIComponent(nodeId)}/parts`,
		token,
		{ method: 'POST', body: JSON.stringify(payload) },
		true
	);
}

export async function updatePart(
	token: string,
	nodeId: string,
	partId: string,
	payload: {
		expected_part_basis_sha256: string;
		heading_level?: number | null;
		title?: string | null;
		content_markdown?: string | null;
		list_style?: ListStyle | null;
		language?: string | null;
		clear_language?: boolean;
		checked?: boolean | null;
		clear_checked?: boolean;
	}
): Promise<PartMutationResponse> {
	return requestJson<PartMutationResponse>(
		`/nodes/${encodeURIComponent(nodeId)}/parts/${encodeURIComponent(partId)}`,
		token,
		{ method: 'PATCH', body: JSON.stringify(payload) },
		true
	);
}

export async function reorderParts(
	token: string,
	nodeId: string,
	payload: {
		parent_part_id: string | null;
		ordered_part_ids: string[];
		expected_sibling_part_ids: string[];
		expected_basis_sha256?: string | null;
		expected_part_basis_sha256?: string | null;
	}
): Promise<ReadNodeResponse> {
	return requestJson<ReadNodeResponse>(
		`/nodes/${encodeURIComponent(nodeId)}/parts/reorder`,
		token,
		{ method: 'POST', body: JSON.stringify(payload) },
		true
	);
}

export async function deletePart(
	token: string,
	nodeId: string,
	partId: string,
	expectedPartBasisSha256: string
): Promise<PartDeleteResponse> {
	return requestJson<PartDeleteResponse>(
		`/nodes/${encodeURIComponent(nodeId)}/parts/${encodeURIComponent(partId)}`,
		token,
		{
			method: 'DELETE',
			body: JSON.stringify({ expected_part_basis_sha256: expectedPartBasisSha256 })
		},
		true
	);
}

export async function putNodeTags(
	token: string,
	nodeId: string,
	tagSlugs: string[]
): Promise<{ node_id: string; tag_slugs: string[] }> {
	return requestJson<{ node_id: string; tag_slugs: string[] }>(
		`/nodes/${encodeURIComponent(nodeId)}/tags`,
		token,
		{ method: 'PUT', body: JSON.stringify({ tag_slugs: tagSlugs }) },
		true
	);
}

export async function promoteTreeNote(
	token: string,
	nodeId: string,
	payload: {
		target_kind: 'project_idea' | 'project_task';
		expected_basis_sha256: string;
		confirmed: true;
		idempotency_key: string;
		project_id?: string | null;
		summary?: string | null;
	}
): Promise<PromoteResponse> {
	return requestJson<PromoteResponse>(
		`/nodes/${encodeURIComponent(nodeId)}/promote`,
		token,
		{ method: 'POST', body: JSON.stringify(payload) },
		true
	);
}

export async function deleteExternalRef(
	token: string,
	nodeId: string,
	refId: string
): Promise<{ deleted_ref_id: string }> {
	return requestJson<{ deleted_ref_id: string }>(
		`/nodes/${encodeURIComponent(nodeId)}/external-refs/${encodeURIComponent(refId)}`,
		token,
		{ method: 'DELETE' }
	);
}

export async function importLegacyNotes(
	token: string,
	payload: { note_ids?: string[] | null; dry_run?: boolean } = {}
): Promise<ImportLegacyResponse> {
	return requestJson<ImportLegacyResponse>(
		'/import-legacy',
		token,
		{ method: 'POST', body: JSON.stringify(payload) },
		true
	);
}
