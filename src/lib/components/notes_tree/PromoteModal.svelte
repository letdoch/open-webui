<script lang="ts">
	import { getContext } from 'svelte';
	import { toast } from 'svelte-sonner';
	import Modal from '$lib/components/common/Modal.svelte';
	import { promoteTreeNote, TreeNoteApiError, type ReadNodeResponse } from './api';

	const i18n = getContext<typeof import('$lib/i18n').default>('i18n');

	export let show = false;
	export let node: ReadNodeResponse | null = null;
	export let onPromoted: () => Promise<void>;

	let targetKind: 'project_idea' | 'project_task' = 'project_task';
	let projectId = '';
	let summary = '';
	let confirmed = false;
	let submitting = false;

	$: if (show && node) {
		targetKind = node.node_kind === 'task_draft' ? 'project_task' : 'project_idea';
		confirmed = false;
		summary = '';
	}

	async function handleSubmit() {
		if (!node || !confirmed || submitting) return;
		submitting = true;
		try {
			const idempotencyKey =
				typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function'
					? crypto.randomUUID()
					: `${node.id}-${Date.now()}`;
			const res = await promoteTreeNote(localStorage.token ?? '', node.id, {
				target_kind: targetKind,
				expected_basis_sha256: node.basis_sha256,
				confirmed: true,
				idempotency_key: idempotencyKey,
				project_id: projectId.trim() || null,
				summary: summary.trim() || null
			});
			toast.success($i18n.t('Promoted to {{kind}} ({{id}})', { kind: targetKind, id: res.ref.external_id }));
			show = false;
			await onPromoted();
		} catch (err) {
			if (err instanceof TreeNoteApiError) {
				toast.error(err.message);
			} else {
				toast.error($i18n.t('Promotion failed.'));
			}
		} finally {
			submitting = false;
		}
	}
</script>

<Modal bind:show size="sm" ariaLabel={$i18n.t('Promote Note')}>
	<div class="p-5 space-y-4">
		<div class="flex items-center justify-between">
			<h3 class="text-base font-semibold text-gray-900 dark:text-gray-100">
				{$i18n.t('Promote Note')}
			</h3>
			<button
				type="button"
				class="text-xs text-gray-500 hover:text-gray-800 dark:hover:text-gray-200"
				on:click={() => (show = false)}
			>
				{$i18n.t('Close')}
			</button>
		</div>

		{#if node}
			<div class="text-xs text-gray-500 dark:text-gray-400">
				{$i18n.t('Source')}: <span class="font-medium text-gray-800 dark:text-gray-200">{node.title}</span>
			</div>

			<div class="space-y-1.5">
				<label class="block text-xs font-medium text-gray-700 dark:text-gray-300" for="promote-target-kind">
					{$i18n.t('Target Type')}
				</label>
				<select
					id="promote-target-kind"
					bind:value={targetKind}
					class="w-full rounded-xl border border-gray-200 dark:border-gray-700 bg-transparent px-3 py-2 text-sm text-gray-900 dark:text-gray-100"
				>
					<option value="project_task">{$i18n.t('Project Task')}</option>
					<option value="project_idea">{$i18n.t('Project Idea')}</option>
				</select>
			</div>

			<div class="space-y-1.5">
				<label class="block text-xs font-medium text-gray-700 dark:text-gray-300" for="promote-project-id">
					{$i18n.t('Project ID (optional)')}
				</label>
				<input
					id="promote-project-id"
					type="text"
					bind:value={projectId}
					placeholder={$i18n.t('Optional target project ID')}
					class="w-full rounded-xl border border-gray-200 dark:border-gray-700 bg-transparent px-3 py-2 text-sm text-gray-900 dark:text-gray-100"
				/>
			</div>

			<div class="space-y-1.5">
				<label class="block text-xs font-medium text-gray-700 dark:text-gray-300" for="promote-summary">
					{$i18n.t('Summary (optional)')}
				</label>
				<textarea
					id="promote-summary"
					rows={3}
					bind:value={summary}
					placeholder={$i18n.t('Optional summary for promotion')}
					class="w-full rounded-xl border border-gray-200 dark:border-gray-700 bg-transparent px-3 py-2 text-sm text-gray-900 dark:text-gray-100"
				></textarea>
			</div>

			<label class="flex items-center gap-2 text-xs text-gray-700 dark:text-gray-300 cursor-pointer">
				<input type="checkbox" bind:checked={confirmed} class="rounded border-gray-300" />
				<span>{$i18n.t('Confirm creating an external item using my active OAuth session.')}</span>
			</label>

			<div class="flex justify-end gap-2 pt-2">
				<button
					type="button"
					class="px-3 py-1.5 rounded-xl text-xs font-medium bg-gray-100 dark:bg-gray-800 text-gray-700 dark:text-gray-300 hover:bg-gray-200 dark:hover:bg-gray-700"
					on:click={() => (show = false)}
				>
					{$i18n.t('Cancel')}
				</button>
				<button
					type="button"
					disabled={!confirmed || submitting}
					class="px-4 py-1.5 rounded-xl text-xs font-medium bg-gray-900 dark:bg-white text-white dark:text-gray-900 disabled:opacity-40"
					on:click={handleSubmit}
				>
					{submitting ? $i18n.t('Promoting...') : $i18n.t('Confirm Promote')}
				</button>
			</div>
		{/if}
	</div>
</Modal>
