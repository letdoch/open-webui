<script lang="ts">
	import { getContext } from 'svelte';
	import { marked } from 'marked';
	import DOMPurify from 'dompurify';
	import { toast } from 'svelte-sonner';
	import {
		createPart,
		deletePart,
		putNodeMarkdown,
		reorderParts,
		TreeNoteApiError,
		updatePart,
		type BasisConflictResponse,
		type ListStyle,
		type PartItem,
		type PartKind,
		type ReadNodeResponse
	} from './api';
	import Plus from '$lib/components/icons/Plus.svelte';
	import GarbageBin from '$lib/components/icons/GarbageBin.svelte';
	import ArrowUpCircle from '$lib/components/icons/ArrowUpCircle.svelte';
	import ArrowPath from '$lib/components/icons/ArrowPath.svelte';

	const i18n = getContext<typeof import('$lib/i18n').default>('i18n');

	export let node: ReadNodeResponse;
	export let readonly = false;
	export let highlightPartId: string | null = null;
	export let onNodeUpdated: (updated: ReadNodeResponse) => void;
	export let onReloadNode: () => Promise<void>;

	let mode: 'markdown' | 'split' | 'parts' = 'split';
	let draftMarkdown = '';
	let syncedNodeId = '';
	let syncedBasis = '';
	let saving = false;
	let conflict: BasisConflictResponse | null = null;

	let editingPartId: string | null = null;
	let editTitle = '';
	let editHeadingLevel = 1;
	let editContent = '';
	let editLanguage = '';

	let showAddPart = false;
	let addParentPartId = '';
	let addKind: PartKind = 'paragraph';
	let addHeadingLevel = 2;
	let addTitle = '';
	let addContent = '';
	let addListStyle: ListStyle = 'unordered';
	let addLanguage = '';
	let addChecked: '' | 'true' | 'false' = '';

	$: if (node.id !== syncedNodeId || (node.basis_sha256 !== syncedBasis && !isDirty)) {
		draftMarkdown = node.rendered_markdown;
		syncedNodeId = node.id;
		syncedBasis = node.basis_sha256;
		conflict = null;
		editingPartId = null;
	}

	$: isDirty = draftMarkdown !== node.rendered_markdown;
	$: renderedHtml = DOMPurify.sanitize(String(marked.parse(draftMarkdown || '', { async: false })));
	$: partDepthMap = buildDepthMap(node.parts);
	$: siblingIdsByParent = buildSiblingMap(node.parts);
	$: containerParts = node.parts.filter((p) => ['section', 'list', 'item', 'quote'].includes(p.part_kind));

	function buildDepthMap(parts: PartItem[]): Record<string, number> {
		const byId = new Map(parts.map((p) => [p.id, p]));
		const depths: Record<string, number> = {};
		const resolve = (id: string, seen: Set<string>): number => {
			if (id in depths) return depths[id];
			const row = byId.get(id);
			if (!row || !row.parent_part_id || seen.has(id)) {
				depths[id] = 0;
				return 0;
			}
			seen.add(id);
			const val = resolve(row.parent_part_id, seen) + 1;
			depths[id] = val;
			return val;
		};
		for (const p of parts) {
			resolve(p.id, new Set());
		}
		return depths;
	}

	function buildSiblingMap(parts: PartItem[]): Record<string, string[]> {
		const map: Record<string, string[]> = {};
		for (const p of parts) {
			const key = p.parent_part_id ?? '__root__';
			if (!(key in map)) map[key] = [];
			map[key].push(p.id);
		}
		return map;
	}

	async function saveMarkdown(overrideBasis: string | null = null) {
		if (readonly || saving) return;
		saving = true;
		conflict = null;
		try {
			const updated = await putNodeMarkdown(localStorage.token ?? '', node.id, {
				expected_basis_sha256: overrideBasis ?? node.basis_sha256,
				markdown: draftMarkdown
			});
			syncedBasis = updated.basis_sha256;
			draftMarkdown = updated.rendered_markdown;
			onNodeUpdated(updated);
			toast.success($i18n.t('Saved'));
		} catch (err) {
			if (err instanceof TreeNoteApiError && err.conflict) {
				conflict = err.conflict;
				toast.error($i18n.t('Concurrent edit detected. Review conflict options.'));
			} else if (err instanceof TreeNoteApiError) {
				toast.error(err.message);
			} else {
				toast.error($i18n.t('Failed to save Markdown.'));
			}
		} finally {
			saving = false;
		}
	}

	async function handleReloadLatest() {
		conflict = null;
		await onReloadNode();
		draftMarkdown = node.rendered_markdown;
		syncedBasis = node.basis_sha256;
	}

	async function handleOverwriteWithDraft() {
		const latestBasis = conflict?.current_basis_sha256 ?? node.basis_sha256;
		await onReloadNode();
		await saveMarkdown(latestBasis);
	}

	function startEditPart(part: PartItem) {
		editingPartId = part.id;
		editTitle = part.title ?? '';
		editHeadingLevel = part.heading_level ?? 1;
		editContent = part.content_markdown;
		editLanguage = part.language ?? '';
	}

	async function saveEditedPart(part: PartItem) {
		if (readonly || saving) return;
		saving = true;
		try {
			const payload: Parameters<typeof updatePart>[3] = {
				expected_part_basis_sha256: part.part_basis_sha256
			};
			if (part.part_kind === 'section') {
				payload.title = editTitle.trim();
				payload.heading_level = editHeadingLevel;
			} else if (part.part_kind === 'fence') {
				payload.content_markdown = editContent;
				if (editLanguage.trim()) payload.language = editLanguage.trim();
				else payload.clear_language = true;
			} else if (part.part_kind !== 'list' && part.part_kind !== 'rule') {
				payload.content_markdown = editContent;
			}
			const res = await updatePart(localStorage.token ?? '', node.id, part.id, payload);
			editingPartId = null;
			onNodeUpdated(res.node);
			toast.success($i18n.t('Part updated'));
		} catch (err) {
			if (err instanceof TreeNoteApiError && err.conflict) {
				conflict = err.conflict;
				toast.error($i18n.t('Part changed concurrently.'));
			} else if (err instanceof TreeNoteApiError) {
				toast.error(err.message);
			}
		} finally {
			saving = false;
		}
	}

	async function toggleItemCheckbox(part: PartItem) {
		if (readonly || saving || part.part_kind !== 'item') return;
		saving = true;
		try {
			const nextChecked = part.checked === true ? false : true;
			const res = await updatePart(localStorage.token ?? '', node.id, part.id, {
				expected_part_basis_sha256: part.part_basis_sha256,
				checked: nextChecked
			});
			onNodeUpdated(res.node);
		} catch (err) {
			if (err instanceof TreeNoteApiError && err.conflict) {
				conflict = err.conflict;
			}
			toast.error(err instanceof Error ? err.message : $i18n.t('Failed to update item'));
		} finally {
			saving = false;
		}
	}

	async function movePartSibling(part: PartItem, delta: number) {
		if (readonly || saving) return;
		const key = part.parent_part_id ?? '__root__';
		const siblings = siblingIdsByParent[key] ?? [];
		const idx = siblings.indexOf(part.id);
		const target = idx + delta;
		if (idx < 0 || target < 0 || target >= siblings.length) return;
		const ordered = [...siblings];
		const [moved] = ordered.splice(idx, 1);
		ordered.splice(target, 0, moved);
		const parentPart = part.parent_part_id
			? node.parts.find((p) => p.id === part.parent_part_id) ?? null
			: null;
		saving = true;
		try {
			const updated = await reorderParts(localStorage.token ?? '', node.id, {
				parent_part_id: part.parent_part_id,
				ordered_part_ids: ordered,
				expected_sibling_part_ids: siblings,
				expected_basis_sha256: parentPart ? null : node.basis_sha256,
				expected_part_basis_sha256: parentPart ? parentPart.part_basis_sha256 : null
			});
			onNodeUpdated(updated);
		} catch (err) {
			if (err instanceof TreeNoteApiError && err.conflict) conflict = err.conflict;
			toast.error(err instanceof Error ? err.message : $i18n.t('Reorder failed'));
		} finally {
			saving = false;
		}
	}

	async function handleDeletePart(part: PartItem) {
		if (readonly || saving) return;
		saving = true;
		try {
			const res = await deletePart(localStorage.token ?? '', node.id, part.id, part.part_basis_sha256);
			onNodeUpdated(res.node);
			toast.success($i18n.t('Part deleted'));
		} catch (err) {
			if (err instanceof TreeNoteApiError && err.conflict) conflict = err.conflict;
			toast.error(err instanceof Error ? err.message : $i18n.t('Delete part failed'));
		} finally {
			saving = false;
		}
	}

	async function handleAddPart() {
		if (readonly || saving) return;
		const parentPart = addParentPartId ? node.parts.find((p) => p.id === addParentPartId) ?? null : null;
		saving = true;
		try {
			const res = await createPart(localStorage.token ?? '', node.id, {
				parent_part_id: parentPart ? parentPart.id : null,
				part_kind: addKind,
				heading_level: addKind === 'section' ? addHeadingLevel : null,
				title: addKind === 'section' ? addTitle.trim() : null,
				content_markdown: ['section', 'list', 'rule'].includes(addKind) ? '' : addContent,
				list_style: addKind === 'list' ? addListStyle : null,
				language: addKind === 'fence' && addLanguage.trim() ? addLanguage.trim() : null,
				checked: addKind === 'item' && addChecked !== '' ? addChecked === 'true' : null,
				expected_basis_sha256: parentPart ? null : node.basis_sha256,
				expected_part_basis_sha256: parentPart ? parentPart.part_basis_sha256 : null
			});
			showAddPart = false;
			addTitle = '';
			addContent = '';
			addLanguage = '';
			onNodeUpdated(res.node);
			toast.success($i18n.t('Part added'));
		} catch (err) {
			if (err instanceof TreeNoteApiError && err.conflict) conflict = err.conflict;
			toast.error(err instanceof Error ? err.message : $i18n.t('Failed to add part'));
		} finally {
			saving = false;
		}
	}

	function handleKeydown(event: KeyboardEvent) {
		if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 's') {
			event.preventDefault();
			void saveMarkdown();
		}
	}
</script>

<div class="flex flex-col h-full min-h-0">
	<div class="flex flex-wrap items-center justify-between gap-2 px-4 py-2 border-b border-gray-100 dark:border-gray-850">
		<div class="inline-flex rounded-xl bg-gray-100 dark:bg-gray-900 p-0.5 text-xs">
			<button
				type="button"
				class="px-2.5 py-1 rounded-lg transition {mode === 'markdown'
					? 'bg-white dark:bg-gray-800 text-gray-900 dark:text-white shadow-xs'
					: 'text-gray-500 hover:text-gray-800 dark:hover:text-gray-200'}"
				on:click={() => (mode = 'markdown')}
			>
				{$i18n.t('Markdown')}
			</button>
			<button
				type="button"
				class="px-2.5 py-1 rounded-lg transition {mode === 'split'
					? 'bg-white dark:bg-gray-800 text-gray-900 dark:text-white shadow-xs'
					: 'text-gray-500 hover:text-gray-800 dark:hover:text-gray-200'}"
				on:click={() => (mode = 'split')}
			>
				{$i18n.t('Split')}
			</button>
			<button
				type="button"
				class="px-2.5 py-1 rounded-lg transition {mode === 'parts'
					? 'bg-white dark:bg-gray-800 text-gray-900 dark:text-white shadow-xs'
					: 'text-gray-500 hover:text-gray-800 dark:hover:text-gray-200'}"
				on:click={() => (mode = 'parts')}
			>
				{$i18n.t('Outline & Parts')} ({node.parts.length})
			</button>
		</div>

		<div class="flex items-center gap-2">
			<span class="text-[11px] font-mono text-gray-400" title={node.basis_sha256}>
				basis:{node.basis_sha256.slice(0, 8)}
			</span>
			{#if mode !== 'parts'}
				<button
					type="button"
					disabled={readonly || saving || !isDirty}
					class="px-3 py-1 rounded-xl text-xs font-medium bg-gray-900 dark:bg-white text-white dark:text-gray-900 disabled:opacity-40 transition"
					on:click={() => saveMarkdown()}
				>
					{saving ? $i18n.t('Saving...') : isDirty ? $i18n.t('Save Markdown') : $i18n.t('Saved')}
				</button>
			{:else if !readonly}
				<button
					type="button"
					class="inline-flex items-center gap-1 px-2.5 py-1 rounded-xl text-xs font-medium bg-gray-100 dark:bg-gray-850 hover:bg-gray-200 dark:hover:bg-gray-800 text-gray-800 dark:text-gray-200"
					on:click={() => (showAddPart = !showAddPart)}
				>
					<Plus className="size-3.5" strokeWidth="2" />
					<span>{$i18n.t('Add Part')}</span>
				</button>
			{/if}
		</div>
	</div>

	{#if conflict}
		<div class="mx-4 mt-3 rounded-xl border border-amber-300 dark:border-amber-700 bg-amber-50 dark:bg-amber-950/40 px-3.5 py-2.5 text-xs flex flex-wrap items-center justify-between gap-2">
			<div class="text-amber-900 dark:text-amber-200">
				<span class="font-semibold">{$i18n.t('Conflict detected:')}</span>
				{$i18n.t('This note changed in another tab or tool.')}
				<span class="font-mono ml-1">({conflict.current_basis_sha256.slice(0, 8)})</span>
			</div>
			<div class="flex items-center gap-2">
				<button
					type="button"
					class="inline-flex items-center gap-1 px-2.5 py-1 rounded-lg bg-white dark:bg-gray-900 border border-amber-300 dark:border-amber-700 text-amber-900 dark:text-amber-200 font-medium"
					on:click={handleReloadLatest}
				>
					<ArrowPath className="size-3" strokeWidth="2" />
					<span>{$i18n.t('Reload Latest')}</span>
				</button>
				{#if mode !== 'parts' && !readonly}
					<button
						type="button"
						class="px-2.5 py-1 rounded-lg bg-amber-600 text-white font-medium hover:bg-amber-700"
						on:click={handleOverwriteWithDraft}
					>
						{$i18n.t('Overwrite With My Draft')}
					</button>
				{/if}
			</div>
		</div>
	{/if}

	<div class="flex-1 min-h-0 overflow-hidden">
		{#if mode === 'markdown'}
			<textarea
				bind:value={draftMarkdown}
				readonly={readonly}
				on:keydown={handleKeydown}
				placeholder={$i18n.t('Write Markdown here...')}
				class="w-full h-full resize-none bg-transparent p-5 text-sm font-mono leading-6 text-gray-900 dark:text-gray-100 outline-none"
			></textarea>
		{:else if mode === 'split'}
			<div class="grid grid-cols-1 lg:grid-cols-2 h-full min-h-0 divide-y lg:divide-y-0 lg:divide-x divide-gray-100 dark:divide-gray-850">
				<textarea
					bind:value={draftMarkdown}
					readonly={readonly}
					on:keydown={handleKeydown}
					placeholder={$i18n.t('Write Markdown here...')}
					class="w-full h-full resize-none bg-transparent p-5 text-sm font-mono leading-6 text-gray-900 dark:text-gray-100 outline-none"
				></textarea>
				<div class="h-full overflow-y-auto p-5 prose prose-sm dark:prose-invert max-w-none">
					{#if draftMarkdown.trim()}
						{@html renderedHtml}
					{:else}
						<p class="text-gray-400 italic">{$i18n.t('Empty note')}</p>
					{/if}
				</div>
			</div>
		{:else}
			<div class="h-full overflow-y-auto p-4 space-y-2.5">
				{#if showAddPart && !readonly}
					<div class="rounded-xl border border-gray-200 dark:border-gray-800 p-3 bg-gray-50/70 dark:bg-gray-900/60 space-y-2.5 text-xs">
						<div class="grid grid-cols-1 sm:grid-cols-3 gap-2">
							<div>
								<label class="block text-gray-500 mb-1" for="add-part-kind">{$i18n.t('Kind')}</label>
								<select
									id="add-part-kind"
									bind:value={addKind}
									class="w-full rounded-lg border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-950 px-2 py-1"
								>
									<option value="section">section</option>
									<option value="paragraph">paragraph</option>
									<option value="list">list</option>
									<option value="item">item</option>
									<option value="fence">fence</option>
									<option value="table">table</option>
									<option value="quote">quote</option>
									<option value="rule">rule</option>
								</select>
							</div>
							<div>
								<label class="block text-gray-500 mb-1" for="add-part-parent">{$i18n.t('Parent Container')}</label>
								<select
									id="add-part-parent"
									bind:value={addParentPartId}
									class="w-full rounded-lg border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-950 px-2 py-1"
								>
									<option value="">{$i18n.t('Top level')}</option>
									{#each containerParts as cp (cp.id)}
										<option value={cp.id}>{cp.part_path} ({cp.part_kind})</option>
									{/each}
								</select>
							</div>
							{#if addKind === 'section'}
								<div>
									<label class="block text-gray-500 mb-1" for="add-heading-lvl">{$i18n.t('Heading Level')}</label>
									<select
										id="add-heading-lvl"
										bind:value={addHeadingLevel}
										class="w-full rounded-lg border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-950 px-2 py-1"
									>
										{#each [1, 2, 3, 4, 5, 6] as lvl}
											<option value={lvl}>H{lvl}</option>
										{/each}
									</select>
								</div>
							{:else if addKind === 'list'}
								<div>
									<label class="block text-gray-500 mb-1" for="add-list-style">{$i18n.t('List Style')}</label>
									<select
										id="add-list-style"
										bind:value={addListStyle}
										class="w-full rounded-lg border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-950 px-2 py-1"
									>
										<option value="unordered">unordered</option>
										<option value="ordered">ordered</option>
									</select>
								</div>
							{:else if addKind === 'item'}
								<div>
									<label class="block text-gray-500 mb-1" for="add-checked">{$i18n.t('Task Checkbox')}</label>
									<select
										id="add-checked"
										bind:value={addChecked}
										class="w-full rounded-lg border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-950 px-2 py-1"
									>
										<option value="">{$i18n.t('Plain item')}</option>
										<option value="false">{$i18n.t('Unchecked [ ]')}</option>
										<option value="true">{$i18n.t('Checked [x]')}</option>
									</select>
								</div>
							{:else if addKind === 'fence'}
								<div>
									<label class="block text-gray-500 mb-1" for="add-lang">{$i18n.t('Language')}</label>
									<input
										id="add-lang"
										type="text"
										bind:value={addLanguage}
										placeholder="python"
										class="w-full rounded-lg border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-950 px-2 py-1"
									/>
								</div>
							{/if}
						</div>

						{#if addKind === 'section'}
							<input
								type="text"
								bind:value={addTitle}
								placeholder={$i18n.t('Section heading title')}
								class="w-full rounded-lg border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-950 px-2.5 py-1.5"
							/>
						{:else if !['list', 'rule'].includes(addKind)}
							<textarea
								rows={3}
								bind:value={addContent}
								placeholder={$i18n.t('Part Markdown content')}
								class="w-full rounded-lg border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-950 px-2.5 py-1.5 font-mono"
							></textarea>
						{/if}

						<div class="flex justify-end gap-2">
							<button
								type="button"
								class="px-2.5 py-1 rounded-lg bg-gray-200 dark:bg-gray-800"
								on:click={() => (showAddPart = false)}
							>
								{$i18n.t('Cancel')}
							</button>
							<button
								type="button"
								class="px-3 py-1 rounded-lg bg-gray-900 dark:bg-white text-white dark:text-gray-900 font-medium"
								on:click={handleAddPart}
							>
								{$i18n.t('Insert Part')}
							</button>
						</div>
					</div>
				{/if}

				{#if node.parts.length === 0}
					<div class="text-xs text-gray-500 py-8 text-center">
						{$i18n.t('No parts yet. Switch to Markdown or click Add Part.')}
					</div>
				{:else}
					{#each node.parts as part (part.id)}
						{@const depth = partDepthMap[part.id] ?? 0}
						{@const siblings = siblingIdsByParent[part.parent_part_id ?? '__root__'] ?? []}
						{@const sibIdx = siblings.indexOf(part.id)}
						<div
							class="rounded-xl border px-3 py-2 text-xs transition {highlightPartId === part.id
								? 'border-blue-400 bg-blue-50/40 dark:bg-blue-950/20'
								: 'border-gray-100 dark:border-gray-850 hover:border-gray-200 dark:hover:border-gray-800'}"
							style="margin-left: {Math.min(depth, 6) * 16}px"
						>
							<div class="flex items-center justify-between gap-2">
								<div class="flex items-center gap-2 min-w-0">
									<span class="px-1.5 py-0.5 rounded bg-gray-100 dark:bg-gray-850 font-mono text-[10px] uppercase text-gray-600 dark:text-gray-300">
										{part.part_kind === 'section' ? `H${part.heading_level}` : part.part_kind}
									</span>
									<span class="font-mono text-[10px] text-gray-400 truncate">{part.part_path}</span>
									<span class="font-mono text-[10px] text-gray-400">#{part.part_basis_sha256.slice(0, 6)}</span>
								</div>

								{#if !readonly}
									<div class="flex items-center gap-1 shrink-0">
										{#if sibIdx > 0}
											<button
												type="button"
												class="p-1 rounded hover:bg-gray-100 dark:hover:bg-gray-800 text-gray-500"
												title={$i18n.t('Move up')}
												on:click={() => movePartSibling(part, -1)}
											>
												<ArrowUpCircle className="size-3.5" strokeWidth="1.7" />
											</button>
										{/if}
										{#if sibIdx < siblings.length - 1}
											<button
												type="button"
												class="p-1 rounded hover:bg-gray-100 dark:hover:bg-gray-800 text-gray-500 rotate-180"
												title={$i18n.t('Move down')}
												on:click={() => movePartSibling(part, 1)}
											>
												<ArrowUpCircle className="size-3.5" strokeWidth="1.7" />
											</button>
										{/if}
										{#if part.part_kind !== 'list' && part.part_kind !== 'rule'}
											<button
												type="button"
												class="px-2 py-0.5 rounded bg-gray-100 dark:bg-gray-850 hover:bg-gray-200 dark:hover:bg-gray-800"
												on:click={() => (editingPartId === part.id ? (editingPartId = null) : startEditPart(part))}
											>
												{editingPartId === part.id ? $i18n.t('Close') : $i18n.t('Edit')}
											</button>
										{/if}
										<button
											type="button"
											class="p-1 rounded hover:bg-red-50 dark:hover:bg-red-950/30 text-gray-400 hover:text-red-500"
											title={$i18n.t('Delete part')}
											on:click={() => handleDeletePart(part)}
										>
											<GarbageBin className="size-3.5" strokeWidth="1.6" />
										</button>
									</div>
								{/if}
							</div>

							{#if editingPartId === part.id}
								<div class="mt-2 space-y-2">
									{#if part.part_kind === 'section'}
										<div class="flex gap-2">
											<select
												bind:value={editHeadingLevel}
												class="rounded-lg border border-gray-200 dark:border-gray-700 bg-transparent px-2 py-1"
											>
												{#each [1, 2, 3, 4, 5, 6] as lvl}
													<option value={lvl}>H{lvl}</option>
												{/each}
											</select>
											<input
												type="text"
												bind:value={editTitle}
												class="flex-1 rounded-lg border border-gray-200 dark:border-gray-700 bg-transparent px-2.5 py-1"
											/>
										</div>
									{:else}
										{#if part.part_kind === 'fence'}
											<input
												type="text"
												bind:value={editLanguage}
												placeholder={$i18n.t('Language (optional)')}
												class="w-40 rounded-lg border border-gray-200 dark:border-gray-700 bg-transparent px-2 py-1 font-mono"
											/>
										{/if}
										<textarea
											rows={4}
											bind:value={editContent}
											class="w-full rounded-lg border border-gray-200 dark:border-gray-700 bg-transparent px-2.5 py-1.5 font-mono"
										></textarea>
									{/if}
									<div class="flex justify-end gap-2">
										<button
											type="button"
											class="px-2.5 py-1 rounded-lg bg-gray-100 dark:bg-gray-800"
											on:click={() => (editingPartId = null)}
										>
											{$i18n.t('Cancel')}
										</button>
										<button
											type="button"
											class="px-3 py-1 rounded-lg bg-gray-900 dark:bg-white text-white dark:text-gray-900 font-medium"
											on:click={() => saveEditedPart(part)}
										>
											{$i18n.t('Save Part')}
										</button>
									</div>
								</div>
							{:else}
								<div class="mt-1.5">
									{#if part.part_kind === 'section'}
										<div class="font-semibold text-sm text-gray-900 dark:text-gray-100">{part.title}</div>
									{:else if part.part_kind === 'item'}
										<div class="flex items-start gap-2">
											{#if part.checked !== null}
												<input
													type="checkbox"
													checked={part.checked === true}
													disabled={readonly}
													on:change={() => toggleItemCheckbox(part)}
													class="mt-0.5 rounded border-gray-300"
												/>
											{/if}
											<span class={part.checked === true ? 'line-through text-gray-400' : 'text-gray-800 dark:text-gray-200'}>
												{part.content_markdown || '(nested item)'}
											</span>
										</div>
									{:else if part.part_kind === 'list'}
										<span class="text-gray-400 italic">{part.list_style} list</span>
									{:else if part.part_kind === 'rule'}
										<hr class="border-gray-200 dark:border-gray-800 my-1" />
									{:else}
										<pre class="whitespace-pre-wrap font-mono text-gray-800 dark:text-gray-200">{part.content_markdown}</pre>
									{/if}
								</div>
							{/if}
						</div>
					{/each}
				{/if}
			</div>
		{/if}
	</div>
</div>
