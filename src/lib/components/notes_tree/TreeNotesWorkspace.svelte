<script lang="ts">
	import { getContext, onMount } from 'svelte';
	import { goto } from '$app/navigation';
	import { toast } from 'svelte-sonner';
	import { mobile, showSidebar } from '$lib/stores';
	import {
		archiveTreeNote,
		createTreeNote,
		deleteExternalRef,
		deleteTreeNote,
		fetchTreeNotesStatus,
		getCalendar,
		getTree,
		getTreeNote,
		importLegacyNotes,
		moveTreeNote,
		openToday,
		putNodeTags,
		reorderTreeNodes,
		searchTreeNotes,
		TreeNoteApiError,
		updateTreeNote,
		type CalendarResponse,
		type ReadNodeResponse,
		type SearchHit,
		type TreeNodeItem
	} from './api';
	import { openTreeNoteTabs } from './store';
	import TreeNodeRow from './TreeNodeRow.svelte';
	import CalendarTree from './CalendarTree.svelte';
	import PartOutlineEditor from './PartOutlineEditor.svelte';
	import PromoteModal from './PromoteModal.svelte';
	import ConfirmDialog from '$lib/components/common/ConfirmDialog.svelte';
	import Modal from '$lib/components/common/Modal.svelte';
	import Spinner from '$lib/components/common/Spinner.svelte';
	import SidebarIcon from '$lib/components/icons/Sidebar.svelte';
	import Calendar from '$lib/components/icons/Calendar.svelte';
	import Plus from '$lib/components/icons/Plus.svelte';
	import Search from '$lib/components/icons/Search.svelte';
	import XMark from '$lib/components/icons/XMark.svelte';
	import ArchiveBox from '$lib/components/icons/ArchiveBox.svelte';
	import GarbageBin from '$lib/components/icons/GarbageBin.svelte';
	import ArrowUpTray from '$lib/components/icons/ArrowUpTray.svelte';
	import LightBulb from '$lib/components/icons/LightBulb.svelte';

	const i18n = getContext<typeof import('$lib/i18n').default>('i18n');

	export let initialNodeId: string | null = null;

	let loading = true;
	let treeItems: TreeNodeItem[] = [];
	let calendarData: CalendarResponse = { years: [], total_days: 0 };
	let promoteEnabled = false;
	let includeArchived = false;

	let expandedIds = new Set<string>();
	let activeNodeId: string | null = null;
	let lastRouteNodeId: string | null = null;
	let highlightPartId: string | null = null;

	let searchQuery = '';
	let tagFilter = '';
	let searchHits: SearchHit[] = [];
	let searching = false;
	let searchTimer: ReturnType<typeof setTimeout> | null = null;

	let editingTitle = '';
	let newTagInput = '';

	let showCreateModal = false;
	let createTitle = '';
	let createKind: 'folder' | 'note' | 'task_draft' = 'note';
	let createParentId: string | null = null;

	let showMoveModal = false;
	let moveTargetParentId = '';

	let showDeleteConfirm = false;
	let deleteRecursive = false;

	let showPromoteModal = false;
	let importingLegacy = false;

	$: activeNode = $openTreeNoteTabs.find((t) => t.id === activeNodeId) ?? null;
	$: if (activeNode) {
		editingTitle = activeNode.title;
	}
	$: rootSiblingIds = treeItems.map((item) => item.node.id);
	$: flatNodeOptions = flattenNodes(treeItems);

	function flattenNodes(items: TreeNodeItem[], prefix = ''): Array<{ id: string; label: string }> {
		const out: Array<{ id: string; label: string }> = [];
		for (const item of items) {
			const label = prefix ? `${prefix} / ${item.node.title}` : item.node.title;
			out.push({ id: item.node.id, label });
			out.push(...flattenNodes(item.children, label));
		}
		return out;
	}

	async function refreshLists() {
		const token = localStorage.token ?? '';
		const [treeRes, calRes] = await Promise.all([
			getTree(token, { includeArchived }),
			getCalendar(token)
		]);
		treeItems = treeRes.items;
		calendarData = calRes;
	}

	async function openNode(nodeId: string, partId: string | null = null, updateRoute = true) {
		const token = localStorage.token ?? '';
		try {
			const loaded = await getTreeNote(token, nodeId);
			openTreeNoteTabs.update((tabs) => {
				const existingIdx = tabs.findIndex((t) => t.id === loaded.id);
				if (existingIdx >= 0) {
					return tabs.map((t, i) => (i === existingIdx ? loaded : t));
				}
				return [...tabs, loaded];
			});
			activeNodeId = loaded.id;
			highlightPartId = partId;
			for (const crumb of loaded.breadcrumbs) {
				if (crumb.id !== loaded.id) expandedIds.add(crumb.id);
			}
			expandedIds = new Set(expandedIds);
			if (updateRoute) {
				await goto(`/tree-notes/${loaded.id}`, { replaceState: false, noScroll: true });
			}
		} catch (err) {
			toast.error(err instanceof Error ? err.message : $i18n.t('Failed to open note'));
		}
	}

	function handleNodeUpdated(updated: ReadNodeResponse) {
		openTreeNoteTabs.update((tabs) => tabs.map((t) => (t.id === updated.id ? updated : t)));
		void refreshLists();
	}

	async function handleReloadActive() {
		if (!activeNodeId) return;
		await openNode(activeNodeId, null, false);
		await refreshLists();
	}

	function closeTab(nodeId: string) {
		const prevTabs = $openTreeNoteTabs;
		const idx = prevTabs.findIndex((t) => t.id === nodeId);
		const nextTabs = prevTabs.filter((t) => t.id !== nodeId);
		openTreeNoteTabs.set(nextTabs);
		if (activeNodeId === nodeId) {
			const nextTab = nextTabs[Math.max(0, idx - 1)] ?? null;
			activeNodeId = nextTab ? nextTab.id : null;
			void goto(activeNodeId ? `/tree-notes/${activeNodeId}` : '/tree-notes', { replaceState: true });
		}
	}

	function toggleExpand(id: string) {
		const next = new Set(expandedIds);
		if (next.has(id)) next.delete(id);
		else next.add(id);
		expandedIds = next;
	}

	async function handleOpenToday() {
		try {
			const tz = Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC';
			const res = await openToday(localStorage.token ?? '', tz);
			await refreshLists();
			await openNode(res.node.id);
		} catch (err) {
			toast.error(err instanceof Error ? err.message : $i18n.t('Failed to open today note'));
		}
	}

	function openCreateDialog(parentId: string | null, kind: 'folder' | 'note' | 'task_draft' = 'note') {
		createParentId = parentId;
		createKind = kind;
		createTitle = '';
		showCreateModal = true;
	}

	async function handleCreateSubmit() {
		if (!createTitle.trim()) return;
		try {
			const created = await createTreeNote(localStorage.token ?? '', {
				title: createTitle.trim(),
				node_kind: createKind,
				parent_id: createParentId
			});
			showCreateModal = false;
			if (createParentId) {
				expandedIds.add(createParentId);
				expandedIds = new Set(expandedIds);
			}
			await refreshLists();
			await openNode(created.id);
		} catch (err) {
			toast.error(err instanceof Error ? err.message : $i18n.t('Failed to create node'));
		}
	}

	async function handleSaveTitle() {
		if (!activeNode) return;
		const trimmed = editingTitle.trim();
		if (!trimmed || trimmed === activeNode.title) {
			editingTitle = activeNode.title;
			return;
		}
		try {
			const updated = await updateTreeNote(localStorage.token ?? '', activeNode.id, {
				expected_basis_sha256: activeNode.basis_sha256,
				title: trimmed
			});
			handleNodeUpdated(updated);
		} catch (err) {
			toast.error(err instanceof Error ? err.message : $i18n.t('Failed to rename'));
			editingTitle = activeNode.title;
		}
	}

	async function handleKindChange(event: Event) {
		if (!activeNode || activeNode.node_kind === 'daily_note') return;
		const target = event.currentTarget as HTMLSelectElement;
		const nextKind = target.value as 'folder' | 'note' | 'task_draft';
		if (nextKind === activeNode.node_kind) return;
		try {
			const updated = await updateTreeNote(localStorage.token ?? '', activeNode.id, {
				expected_basis_sha256: activeNode.basis_sha256,
				node_kind: nextKind
			});
			handleNodeUpdated(updated);
		} catch (err) {
			toast.error(err instanceof Error ? err.message : $i18n.t('Failed to change kind'));
		}
	}

	async function handleReorderSibling(parentId: string | null, orderedIds: string[], expectedIds: string[]) {
		try {
			await reorderTreeNodes(localStorage.token ?? '', {
				parent_id: parentId,
				ordered_node_ids: orderedIds,
				expected_sibling_ids: expectedIds
			});
			await refreshLists();
		} catch (err) {
			toast.error(err instanceof Error ? err.message : $i18n.t('Failed to reorder'));
		}
	}

	async function handleMoveSubmit() {
		if (!activeNode) return;
		try {
			const updated = await moveTreeNote(localStorage.token ?? '', activeNode.id, {
				expected_basis_sha256: activeNode.basis_sha256,
				new_parent_id: moveTargetParentId || null
			});
			showMoveModal = false;
			handleNodeUpdated(updated);
			toast.success($i18n.t('Node moved'));
		} catch (err) {
			toast.error(err instanceof Error ? err.message : $i18n.t('Failed to move node'));
		}
	}

	async function handleToggleArchive() {
		if (!activeNode) return;
		try {
			const updated = await archiveTreeNote(localStorage.token ?? '', activeNode.id, {
				expected_basis_sha256: activeNode.basis_sha256,
				archived: activeNode.archived_at === null
			});
			handleNodeUpdated(updated);
			toast.success(updated.archived_at ? $i18n.t('Archived') : $i18n.t('Unarchived'));
		} catch (err) {
			toast.error(err instanceof Error ? err.message : $i18n.t('Failed to update archive state'));
		}
	}

	async function handleConfirmDelete() {
		if (!activeNode) return;
		const targetId = activeNode.id;
		try {
			const res = await deleteTreeNote(
				localStorage.token ?? '',
				targetId,
				activeNode.basis_sha256,
				deleteRecursive || activeNode.children.length > 0
			);
			const deletedSet = new Set(res.deleted_node_ids);
			let nextActiveId: string | null = activeNodeId;
			openTreeNoteTabs.update((tabs) => {
				const remaining = tabs.filter((t) => !deletedSet.has(t.id));
				if (nextActiveId && deletedSet.has(nextActiveId)) {
					nextActiveId = remaining[0]?.id ?? null;
				}
				return remaining;
			});
			activeNodeId = nextActiveId;
			await refreshLists();
			await goto(activeNodeId ? `/tree-notes/${activeNodeId}` : '/tree-notes', { replaceState: true });
			toast.success($i18n.t('Deleted'));
		} catch (err) {
			if (err instanceof TreeNoteApiError && err.code === 'children_exist') {
				deleteRecursive = true;
				showDeleteConfirm = true;
			}
			toast.error(err instanceof Error ? err.message : $i18n.t('Delete failed'));
		}
	}

	async function handleAddTag() {
		if (!activeNode || !newTagInput.trim()) return;
		const slug = newTagInput.trim().toLowerCase();
		if (activeNode.tags.includes(slug)) {
			newTagInput = '';
			return;
		}
		try {
			const updatedId = activeNode.id;
			const res = await putNodeTags(localStorage.token ?? '', updatedId, [...activeNode.tags, slug]);
			newTagInput = '';
			openTreeNoteTabs.update((tabs) =>
				tabs.map((t) => (t.id === updatedId ? { ...t, tags: res.tag_slugs } : t))
			);
			await refreshLists();
		} catch (err) {
			toast.error(err instanceof Error ? err.message : $i18n.t('Invalid tag'));
		}
	}

	async function handleRemoveTag(slug: string) {
		if (!activeNode) return;
		try {
			const updatedId = activeNode.id;
			const res = await putNodeTags(
				localStorage.token ?? '',
				updatedId,
				activeNode.tags.filter((t) => t !== slug)
			);
			openTreeNoteTabs.update((tabs) =>
				tabs.map((t) => (t.id === updatedId ? { ...t, tags: res.tag_slugs } : t))
			);
			await refreshLists();
		} catch (err) {
			toast.error(err instanceof Error ? err.message : $i18n.t('Failed to remove tag'));
		}
	}

	async function handleDeleteRef(refId: string) {
		if (!activeNode) return;
		try {
			await deleteExternalRef(localStorage.token ?? '', activeNode.id, refId);
			await handleReloadActive();
			toast.success($i18n.t('Reference removed'));
		} catch (err) {
			toast.error(err instanceof Error ? err.message : $i18n.t('Failed to remove reference'));
		}
	}

	async function handleImportLegacy() {
		if (importingLegacy) return;
		importingLegacy = true;
		try {
			const res = await importLegacyNotes(localStorage.token ?? '', { dry_run: false });
			toast.success(
				$i18n.t('Imported {{imported}}, skipped {{skipped}}, failed {{failed}}', {
					imported: res.imported_count,
					skipped: res.skipped_count,
					failed: res.failed_count
				})
			);
			await refreshLists();
		} catch (err) {
			toast.error(err instanceof Error ? err.message : $i18n.t('Legacy import failed'));
		} finally {
			importingLegacy = false;
		}
	}

	function scheduleSearch() {
		if (searchTimer) clearTimeout(searchTimer);
		if (!searchQuery.trim() && !tagFilter.trim()) {
			searchHits = [];
			return;
		}
		searchTimer = setTimeout(async () => {
			searching = true;
			try {
				const res = await searchTreeNotes(localStorage.token ?? '', {
					query: searchQuery.trim(),
					tag: tagFilter.trim() || null,
					includeArchived,
					limit: 30
				});
				searchHits = res.items;
			} catch (err) {
				toast.error(err instanceof Error ? err.message : $i18n.t('Search failed'));
			} finally {
				searching = false;
			}
		}, 220);
	}

	$: if (!loading && initialNodeId !== lastRouteNodeId) {
		lastRouteNodeId = initialNodeId;
		if (initialNodeId && initialNodeId !== activeNodeId) {
			void openNode(initialNodeId, null, false);
		} else if (!initialNodeId) {
			activeNodeId = null;
		}
	}

	onMount(async () => {
		try {
			const status = await fetchTreeNotesStatus();
			promoteEnabled = status.promote_enabled;
			if (!status.enabled) {
				await goto('/notes', { replaceState: true });
				return;
			}
			await refreshLists();
			lastRouteNodeId = initialNodeId;
			if (initialNodeId) {
				await openNode(initialNodeId, null, false);
			}
		} catch (err) {
			toast.error(err instanceof Error ? err.message : $i18n.t('Failed to load workspace'));
		} finally {
			loading = false;
		}
	});
</script>

<div class="flex h-full w-full min-h-0 bg-white dark:bg-gray-900">
	<aside class="w-72 shrink-0 border-r border-gray-100 dark:border-gray-850 flex flex-col min-h-0 bg-gray-50/40 dark:bg-gray-950/40">
		<div class="p-3 border-b border-gray-100 dark:border-gray-850 space-y-2.5">
			<div class="flex items-center justify-between gap-2">
				<div class="flex items-center gap-1.5">
					{#if !$showSidebar || $mobile}
						<button
							type="button"
							class="p-1.5 rounded-lg hover:bg-gray-100 dark:hover:bg-gray-850"
							on:click={() => showSidebar.set(!$showSidebar)}
							aria-label={$i18n.t('Toggle sidebar')}
						>
							<SidebarIcon className="size-4" />
						</button>
					{/if}
					<h1 class="text-sm font-semibold text-gray-900 dark:text-gray-100">{$i18n.t('Notes Tree')}</h1>
				</div>
				<button
					type="button"
					class="inline-flex items-center gap-1 px-2.5 py-1 rounded-xl text-xs font-medium bg-gray-900 dark:bg-white text-white dark:text-gray-900 hover:opacity-90"
					on:click={handleOpenToday}
				>
					<Calendar className="size-3.5" strokeWidth="1.8" />
					<span>{$i18n.t('Today')}</span>
				</button>
			</div>

			<div class="flex items-center gap-1.5">
				<div class="relative flex-1">
					<Search className="size-3.5 text-gray-400 absolute left-2.5 top-2" />
					<input
						type="text"
						bind:value={searchQuery}
						on:input={scheduleSearch}
						placeholder={$i18n.t('Search notes...')}
						class="w-full rounded-xl border border-gray-200 dark:border-gray-800 bg-white dark:bg-gray-900 pl-8 pr-2.5 py-1 text-xs outline-none"
					/>
				</div>
				<input
					type="text"
					bind:value={tagFilter}
					on:input={scheduleSearch}
					placeholder={$i18n.t('#tag')}
					class="w-20 rounded-xl border border-gray-200 dark:border-gray-800 bg-white dark:bg-gray-900 px-2 py-1 text-xs outline-none"
				/>
			</div>

			<div class="flex items-center justify-between text-[11px] text-gray-500">
				<div class="flex items-center gap-1">
					<button
						type="button"
						class="inline-flex items-center gap-0.5 px-2 py-0.5 rounded-lg bg-white dark:bg-gray-900 border border-gray-200 dark:border-gray-800 hover:bg-gray-100 dark:hover:bg-gray-850"
						on:click={() => openCreateDialog(null, 'note')}
					>
						<Plus className="size-3" strokeWidth="2" />
						<span>{$i18n.t('Note')}</span>
					</button>
					<button
						type="button"
						class="inline-flex items-center gap-0.5 px-2 py-0.5 rounded-lg bg-white dark:bg-gray-900 border border-gray-200 dark:border-gray-800 hover:bg-gray-100 dark:hover:bg-gray-850"
						on:click={() => openCreateDialog(null, 'folder')}
					>
						<Plus className="size-3" strokeWidth="2" />
						<span>{$i18n.t('Folder')}</span>
					</button>
					<button
						type="button"
						class="inline-flex items-center gap-0.5 px-2 py-0.5 rounded-lg bg-white dark:bg-gray-900 border border-gray-200 dark:border-gray-800 hover:bg-gray-100 dark:hover:bg-gray-850"
						on:click={() => openCreateDialog(null, 'task_draft')}
					>
						<Plus className="size-3" strokeWidth="2" />
						<span>{$i18n.t('Task')}</span>
					</button>
				</div>
				<label class="flex items-center gap-1 cursor-pointer">
					<input
						type="checkbox"
						bind:checked={includeArchived}
						on:change={() => {
							void refreshLists();
							scheduleSearch();
						}}
						class="rounded border-gray-300"
					/>
					<span>{$i18n.t('Archived')}</span>
				</label>
			</div>
		</div>

		<div class="flex-1 overflow-y-auto p-2 space-y-4">
			{#if loading}
				<div class="py-8 flex justify-center"><Spinner className="size-5" /></div>
			{:else if searchQuery.trim() || tagFilter.trim()}
				<div>
					<div class="px-2 py-1 text-[11px] font-semibold uppercase tracking-wide text-gray-400">
						{searching ? $i18n.t('Searching...') : $i18n.t('Search Results ({{count}})', { count: searchHits.length })}
					</div>
					{#if searchHits.length === 0 && !searching}
						<div class="px-2 py-2 text-xs text-gray-500">{$i18n.t('No matches found.')}</div>
					{:else}
						<div class="space-y-1">
							{#each searchHits as hit (hit.node.id)}
								<button
									type="button"
									class="w-full text-left rounded-lg px-2 py-1.5 hover:bg-gray-100 dark:hover:bg-gray-850 space-y-0.5"
									on:click={() => openNode(hit.node.id, hit.matched_part_id)}
								>
									<div class="text-xs font-medium text-gray-900 dark:text-gray-100 truncate">{hit.node.title}</div>
									<div class="text-[10px] text-gray-400 truncate">
										{hit.breadcrumbs.map((b) => b.title).join(' / ')}
									</div>
									{#if hit.snippet}
										<div class="text-[11px] text-gray-500 line-clamp-2">{hit.snippet}</div>
									{/if}
								</button>
							{/each}
						</div>
					{/if}
				</div>
			{:else}
				<div>
					<div class="px-2 py-1 text-[11px] font-semibold uppercase tracking-wide text-gray-400">
						{$i18n.t('Daily Calendar')}
					</div>
					<CalendarTree
						years={calendarData.years}
						{activeNodeId}
						expandedNodeIds={expandedIds}
						onToggleNodeExpand={toggleExpand}
						onSelectNode={(id) => openNode(id)}
						onCreateChild={(pid) => openCreateDialog(pid, 'note')}
						onReorderSibling={handleReorderSibling}
					/>
				</div>

				<div>
					<div class="px-2 py-1 text-[11px] font-semibold uppercase tracking-wide text-gray-400">
						{$i18n.t('Workspace')}
					</div>
					{#if treeItems.length === 0}
						<div class="px-2 py-2 text-xs text-gray-500">{$i18n.t('Create a note or import legacy notes.')}</div>
					{:else}
						<div class="space-y-0.5">
							{#each treeItems as item, idx (item.node.id)}
								<TreeNodeRow
									{item}
									depth={0}
									{activeNodeId}
									siblingIds={rootSiblingIds}
									index={idx}
									{expandedIds}
									onToggleExpand={toggleExpand}
									onSelect={(id) => openNode(id)}
									onCreateChild={(pid) => openCreateDialog(pid, 'note')}
									onReorderSibling={handleReorderSibling}
								/>
							{/each}
						</div>
					{/if}
				</div>
			{/if}
		</div>

		<div class="p-2.5 border-t border-gray-100 dark:border-gray-850 flex items-center justify-between gap-2 text-xs">
			<button
				type="button"
				disabled={importingLegacy}
				class="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-lg bg-white dark:bg-gray-900 border border-gray-200 dark:border-gray-800 hover:bg-gray-100 dark:hover:bg-gray-850 text-gray-700 dark:text-gray-300 disabled:opacity-50"
				on:click={handleImportLegacy}
			>
				<ArrowUpTray className="size-3.5" strokeWidth="1.7" />
				<span>{importingLegacy ? $i18n.t('Importing...') : $i18n.t('Import Legacy Notes')}</span>
			</button>
			<a
				href="/notes"
				class="text-[11px] text-gray-500 hover:text-gray-800 dark:hover:text-gray-200 underline"
			>
				{$i18n.t('Legacy')}
			</a>
		</div>
	</aside>

	<main class="flex-1 flex flex-col min-w-0 min-h-0">
		{#if $openTreeNoteTabs.length > 0}
			<div class="flex items-center gap-1 px-2 pt-2 border-b border-gray-100 dark:border-gray-850 overflow-x-auto">
				{#each $openTreeNoteTabs as tab (tab.id)}
					<div
						class="flex items-center gap-1.5 px-3 py-1.5 rounded-t-xl text-xs border-b-2 transition cursor-pointer max-w-48 {tab.id ===
						activeNodeId
							? 'border-gray-900 dark:border-white bg-gray-50 dark:bg-gray-850 font-medium text-gray-950 dark:text-white'
							: 'border-transparent text-gray-500 hover:text-gray-800 dark:hover:text-gray-200'}"
					>
						<button type="button" class="truncate text-left" on:click={() => openNode(tab.id)}>
							{tab.title}
						</button>
						<button
							type="button"
							class="text-gray-400 hover:text-gray-700 dark:hover:text-gray-200"
							aria-label={$i18n.t('Close tab')}
							on:click|stopPropagation={() => closeTab(tab.id)}
						>
							<XMark className="size-3" strokeWidth="2" />
						</button>
					</div>
				{/each}
			</div>
		{/if}

		{#if activeNode}
			<div class="px-4 py-3 border-b border-gray-100 dark:border-gray-850 space-y-2">
				<div class="flex flex-wrap items-center gap-1 text-[11px] text-gray-400">
					{#each activeNode.breadcrumbs as crumb, idx (crumb.id)}
						{#if idx > 0}<span>/</span>{/if}
						<button
							type="button"
							class="hover:text-gray-800 dark:hover:text-gray-200"
							on:click={() => openNode(crumb.id)}
						>
							{crumb.title}
						</button>
					{/each}
					{#if activeNode.note_date}
						<span class="ml-2 px-1.5 py-0.5 rounded bg-blue-50 dark:bg-blue-950/40 text-blue-600 dark:text-blue-300 font-mono">
							{activeNode.note_date}
						</span>
					{/if}
				</div>

				<div class="flex flex-wrap items-center justify-between gap-2">
					<input
						type="text"
						bind:value={editingTitle}
						readonly={activeNode.archived_at !== null}
						on:blur={handleSaveTitle}
						on:keydown={(e) => e.key === 'Enter' && handleSaveTitle()}
						class="flex-1 min-w-48 text-lg font-semibold bg-transparent text-gray-950 dark:text-white outline-none"
					/>

					<div class="flex flex-wrap items-center gap-1.5 text-xs">
						{#if activeNode.node_kind === 'daily_note'}
							<span class="px-2 py-1 rounded-lg bg-gray-100 dark:bg-gray-850 text-gray-600 dark:text-gray-300">
								daily_note
							</span>
						{:else}
							<select
								value={activeNode.node_kind}
								disabled={activeNode.archived_at !== null}
								on:change={handleKindChange}
								class="rounded-lg border border-gray-200 dark:border-gray-800 bg-transparent px-2 py-1"
							>
								<option value="note">note</option>
								<option value="folder">folder</option>
								<option value="task_draft">task_draft</option>
							</select>
							<button
								type="button"
								disabled={activeNode.archived_at !== null}
								class="px-2.5 py-1 rounded-lg border border-gray-200 dark:border-gray-800 hover:bg-gray-50 dark:hover:bg-gray-850 disabled:opacity-40"
								on:click={() => {
									moveTargetParentId = activeNode?.parent_id ?? '';
									showMoveModal = true;
								}}
							>
								{$i18n.t('Move')}
							</button>
						{/if}

						{#if promoteEnabled && activeNode.archived_at === null}
							<button
								type="button"
								class="inline-flex items-center gap-1 px-2.5 py-1 rounded-lg bg-blue-600 text-white hover:bg-blue-700"
								on:click={() => (showPromoteModal = true)}
							>
								<LightBulb className="size-3.5" strokeWidth="1.8" />
								<span>{$i18n.t('Promote')}</span>
							</button>
						{/if}

						<button
							type="button"
							class="inline-flex items-center gap-1 px-2.5 py-1 rounded-lg border border-gray-200 dark:border-gray-800 hover:bg-gray-50 dark:hover:bg-gray-850"
							on:click={handleToggleArchive}
						>
							<ArchiveBox className="size-3.5" strokeWidth="1.7" />
							<span>{activeNode.archived_at ? $i18n.t('Unarchive') : $i18n.t('Archive')}</span>
						</button>

						<button
							type="button"
							class="inline-flex items-center gap-1 px-2 py-1 rounded-lg border border-red-200 dark:border-red-900/50 text-red-600 dark:text-red-400 hover:bg-red-50 dark:hover:bg-red-950/30"
							on:click={() => {
								deleteRecursive = activeNode.children.length > 0;
								showDeleteConfirm = true;
							}}
						>
							<GarbageBin className="size-3.5" strokeWidth="1.7" />
						</button>
					</div>
				</div>

				<div class="flex flex-wrap items-center gap-1.5 text-xs">
					{#each activeNode.tags as slug (slug)}
						<span class="inline-flex items-center gap-1 px-2 py-0.5 rounded-full bg-gray-100 dark:bg-gray-850 text-gray-700 dark:text-gray-300">
							<span>#{slug}</span>
							<button
								type="button"
								class="text-gray-400 hover:text-gray-800 dark:hover:text-gray-100"
								on:click={() => handleRemoveTag(slug)}
							>
								<XMark className="size-2.5" strokeWidth="2" />
							</button>
						</span>
					{/each}
					<input
						type="text"
						bind:value={newTagInput}
						on:keydown={(e) => e.key === 'Enter' && handleAddTag()}
						placeholder={$i18n.t('+ add tag')}
						class="w-24 bg-transparent text-xs text-gray-600 dark:text-gray-300 outline-none border-b border-transparent focus:border-gray-300"
					/>
					{#each activeNode.external_refs as ref (ref.id)}
						<span class="inline-flex items-center gap-1 px-2 py-0.5 rounded-full bg-emerald-50 dark:bg-emerald-950/40 text-emerald-700 dark:text-emerald-300 text-[11px]">
							<span>{ref.target_kind}: {ref.external_id}</span>
							{#if ref.target_kind !== 'imported_note'}
								<button
									type="button"
									class="hover:text-red-500"
									on:click={() => handleDeleteRef(ref.id)}
								>
									<XMark className="size-2.5" strokeWidth="2" />
								</button>
							{/if}
						</span>
					{/each}
				</div>
			</div>

			<div class="flex-1 min-h-0">
				<PartOutlineEditor
					node={activeNode}
					readonly={activeNode.archived_at !== null}
					{highlightPartId}
					onNodeUpdated={handleNodeUpdated}
					onReloadNode={handleReloadActive}
				/>
			</div>
		{:else}
			<div class="flex-1 flex flex-col items-center justify-center p-8 text-center space-y-3 text-gray-500">
				<p class="text-sm font-medium text-gray-800 dark:text-gray-200">
					{$i18n.t('Select a note, open Today, or create a new node.')}
				</p>
				<div class="flex items-center gap-2">
					<button
						type="button"
						class="px-3.5 py-1.5 rounded-xl text-xs font-medium bg-gray-900 dark:bg-white text-white dark:text-gray-900"
						on:click={handleOpenToday}
					>
						{$i18n.t("Open Today's Daily Note")}
					</button>
					<button
						type="button"
						class="px-3.5 py-1.5 rounded-xl text-xs font-medium border border-gray-200 dark:border-gray-800"
						on:click={() => openCreateDialog(null, 'note')}
					>
						{$i18n.t('New Note')}
					</button>
				</div>
			</div>
		{/if}
	</main>
</div>

<Modal bind:show={showCreateModal} size="sm" ariaLabel={$i18n.t('Create Tree Node')}>
	<div class="p-5 space-y-3">
		<h3 class="text-base font-semibold text-gray-900 dark:text-gray-100">{$i18n.t('New Tree Node')}</h3>
		<input
			type="text"
			bind:value={createTitle}
			placeholder={$i18n.t('Title')}
			on:keydown={(e) => e.key === 'Enter' && handleCreateSubmit()}
			class="w-full rounded-xl border border-gray-200 dark:border-gray-700 bg-transparent px-3 py-2 text-sm outline-none"
		/>
		<select
			bind:value={createKind}
			class="w-full rounded-xl border border-gray-200 dark:border-gray-700 bg-transparent px-3 py-2 text-sm"
		>
			<option value="note">{$i18n.t('Note')}</option>
			<option value="folder">{$i18n.t('Folder')}</option>
			<option value="task_draft">{$i18n.t('Task Draft')}</option>
		</select>
		<div class="flex justify-end gap-2 pt-2">
			<button
				type="button"
				class="px-3 py-1.5 rounded-xl text-xs bg-gray-100 dark:bg-gray-800"
				on:click={() => (showCreateModal = false)}
			>
				{$i18n.t('Cancel')}
			</button>
			<button
				type="button"
				class="px-4 py-1.5 rounded-xl text-xs font-medium bg-gray-900 dark:bg-white text-white dark:text-gray-900"
				on:click={handleCreateSubmit}
			>
				{$i18n.t('Create')}
			</button>
		</div>
	</div>
</Modal>

<Modal bind:show={showMoveModal} size="sm" ariaLabel={$i18n.t('Move Node')}>
	<div class="p-5 space-y-3">
		<h3 class="text-base font-semibold text-gray-900 dark:text-gray-100">{$i18n.t('Move Node')}</h3>
		<select
			bind:value={moveTargetParentId}
			class="w-full rounded-xl border border-gray-200 dark:border-gray-700 bg-transparent px-3 py-2 text-sm"
		>
			<option value="">{$i18n.t('Top level (root)')}</option>
			{#each flatNodeOptions.filter((opt) => opt.id !== activeNode?.id) as opt (opt.id)}
				<option value={opt.id}>{opt.label}</option>
			{/each}
		</select>
		<div class="flex justify-end gap-2 pt-2">
			<button
				type="button"
				class="px-3 py-1.5 rounded-xl text-xs bg-gray-100 dark:bg-gray-800"
				on:click={() => (showMoveModal = false)}
			>
				{$i18n.t('Cancel')}
			</button>
			<button
				type="button"
				class="px-4 py-1.5 rounded-xl text-xs font-medium bg-gray-900 dark:bg-white text-white dark:text-gray-900"
				on:click={handleMoveSubmit}
			>
				{$i18n.t('Move')}
			</button>
		</div>
	</div>
</Modal>

<ConfirmDialog
	bind:show={showDeleteConfirm}
	title={$i18n.t('Delete node?')}
	message={deleteRecursive
		? $i18n.t('This will permanently delete this node and all of its descendant notes.')
		: $i18n.t('This will permanently delete this note.')}
	on:confirm={handleConfirmDelete}
/>

<PromoteModal bind:show={showPromoteModal} node={activeNode} onPromoted={handleReloadActive} />
