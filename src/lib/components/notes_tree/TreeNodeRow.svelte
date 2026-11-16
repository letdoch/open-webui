<script lang="ts">
	import { getContext } from 'svelte';
	import type { TreeNodeItem } from './api';
	import ChevronDown from '$lib/components/icons/ChevronDown.svelte';
	import ChevronRight from '$lib/components/icons/ChevronRight.svelte';
	import Folder from '$lib/components/icons/Folder.svelte';
	import FolderOpen from '$lib/components/icons/FolderOpen.svelte';
	import DocumentPage from '$lib/components/icons/DocumentPage.svelte';
	import CheckBox from '$lib/components/icons/CheckBox.svelte';
	import Calendar from '$lib/components/icons/Calendar.svelte';
	import Plus from '$lib/components/icons/Plus.svelte';
	import ArrowUpCircle from '$lib/components/icons/ArrowUpCircle.svelte';
	import ArchiveBox from '$lib/components/icons/ArchiveBox.svelte';

	const i18n = getContext<typeof import('$lib/i18n').default>('i18n');

	export let item: TreeNodeItem;
	export let depth = 0;
	export let activeNodeId: string | null = null;
	export let siblingIds: string[] = [];
	export let index = 0;
	export let expandedIds: Set<string>;
	export let onToggleExpand: (id: string) => void;
	export let onSelect: (id: string) => void;
	export let onCreateChild: (parentId: string) => void;
	export let onReorderSibling: (parentId: string | null, orderedIds: string[], expectedIds: string[]) => void;

	$: hasChildren = item.children.length > 0;
	$: isExpanded = expandedIds.has(item.node.id);
	$: isActive = activeNodeId === item.node.id;
	$: isArchived = item.node.archived_at !== null;
	$: childSiblingIds = item.children.map((child) => child.node.id);

	function moveSibling(delta: number) {
		const target = index + delta;
		if (target < 0 || target >= siblingIds.length) return;
		const next = [...siblingIds];
		const [moved] = next.splice(index, 1);
		next.splice(target, 0, moved);
		onReorderSibling(item.node.parent_id, next, siblingIds);
	}
</script>

<div>
	<div
		class="group flex items-center gap-1 rounded-lg pr-1.5 py-1 text-xs transition cursor-pointer {isActive
			? 'bg-gray-100 dark:bg-gray-850 text-gray-950 dark:text-white font-medium'
			: 'text-gray-700 dark:text-gray-300 hover:bg-gray-50 dark:hover:bg-gray-900'}"
		style="padding-left: {depth * 14 + 6}px"
	>
		<button
			type="button"
			class="flex size-4 shrink-0 items-center justify-center rounded hover:bg-gray-200 dark:hover:bg-gray-800 text-gray-400"
			on:click|stopPropagation={() => onToggleExpand(item.node.id)}
			aria-label={$i18n.t('Toggle children')}
			disabled={!hasChildren && item.node.node_kind !== 'folder'}
		>
			{#if hasChildren || item.node.node_kind === 'folder'}
				{#if isExpanded}
					<ChevronDown className="size-3" strokeWidth="2" />
				{:else}
					<ChevronRight className="size-3" strokeWidth="2" />
				{/if}
			{/if}
		</button>

		<button
			type="button"
			class="flex flex-1 min-w-0 items-center gap-1.5 text-left"
			on:click={() => onSelect(item.node.id)}
		>
			<span class="shrink-0 text-gray-500 dark:text-gray-400">
				{#if item.node.node_kind === 'folder'}
					{#if isExpanded}
						<FolderOpen className="size-3.5" strokeWidth="1.6" />
					{:else}
						<Folder className="size-3.5" strokeWidth="1.6" />
					{/if}
				{:else if item.node.node_kind === 'task_draft'}
					<CheckBox className="size-3.5" strokeWidth="1.6" />
				{:else if item.node.node_kind === 'daily_note'}
					<Calendar className="size-3.5" strokeWidth="1.6" />
				{:else}
					<DocumentPage className="size-3.5" strokeWidth="1.6" />
				{/if}
			</span>
			<span class="truncate {isArchived ? 'line-through opacity-60' : ''}">{item.node.title}</span>
			{#if isArchived}
				<ArchiveBox className="size-3 shrink-0 text-amber-500" strokeWidth="1.6" />
			{/if}
		</button>

		<div class="hidden group-hover:flex items-center gap-0.5 shrink-0">
			{#if index > 0}
				<button
					type="button"
					class="p-0.5 rounded hover:bg-gray-200 dark:hover:bg-gray-800 text-gray-500"
					title={$i18n.t('Move up')}
					on:click|stopPropagation={() => moveSibling(-1)}
				>
					<ArrowUpCircle className="size-3" strokeWidth="1.8" />
				</button>
			{/if}
			{#if index < siblingIds.length - 1}
				<button
					type="button"
					class="p-0.5 rounded hover:bg-gray-200 dark:hover:bg-gray-800 text-gray-500 rotate-180"
					title={$i18n.t('Move down')}
					on:click|stopPropagation={() => moveSibling(1)}
				>
					<ArrowUpCircle className="size-3" strokeWidth="1.8" />
				</button>
			{/if}
			<button
				type="button"
				class="p-0.5 rounded hover:bg-gray-200 dark:hover:bg-gray-800 text-gray-500"
				title={$i18n.t('Add child note')}
				on:click|stopPropagation={() => onCreateChild(item.node.id)}
			>
				<Plus className="size-3" strokeWidth="2" />
			</button>
		</div>
	</div>

	{#if isExpanded && hasChildren}
		<div>
			{#each item.children as child, childIdx (child.node.id)}
				<svelte:self
					item={child}
					depth={depth + 1}
					{activeNodeId}
					siblingIds={childSiblingIds}
					index={childIdx}
					{expandedIds}
					{onToggleExpand}
					{onSelect}
					{onCreateChild}
					{onReorderSibling}
				/>
			{/each}
		</div>
	{/if}
</div>
