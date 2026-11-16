<script lang="ts">
	import { getContext } from 'svelte';
	import type { CalendarYearGroup } from './api';
	import TreeNodeRow from './TreeNodeRow.svelte';
	import ChevronDown from '$lib/components/icons/ChevronDown.svelte';
	import ChevronRight from '$lib/components/icons/ChevronRight.svelte';
	import Calendar from '$lib/components/icons/Calendar.svelte';
	import Plus from '$lib/components/icons/Plus.svelte';

	const i18n = getContext<typeof import('$lib/i18n').default>('i18n');

	export let years: CalendarYearGroup[] = [];
	export let activeNodeId: string | null = null;
	export let expandedNodeIds: Set<string>;
	export let onToggleNodeExpand: (id: string) => void;
	export let onSelectNode: (id: string) => void;
	export let onCreateChild: (parentId: string) => void;
	export let onReorderSibling: (parentId: string | null, orderedIds: string[], expectedIds: string[]) => void;

	let collapsedYears = new Set<number>();
	let collapsedMonths = new Set<string>();

	function toggleYear(year: number) {
		const next = new Set(collapsedYears);
		if (next.has(year)) next.delete(year);
		else next.add(year);
		collapsedYears = next;
	}

	function toggleMonth(key: string) {
		const next = new Set(collapsedMonths);
		if (next.has(key)) next.delete(key);
		else next.add(key);
		collapsedMonths = next;
	}
</script>

{#if years.length === 0}
	<div class="px-3 py-2 text-xs text-gray-500 dark:text-gray-400">
		{$i18n.t('No daily notes yet.')}
	</div>
{:else}
	<div class="space-y-0.5">
		{#each years as yearGroup (yearGroup.year)}
			{@const yearCollapsed = collapsedYears.has(yearGroup.year)}
			<div>
				<button
					type="button"
					class="w-full flex items-center gap-1.5 px-2 py-1 text-xs font-semibold text-gray-600 dark:text-gray-300 hover:bg-gray-50 dark:hover:bg-gray-900 rounded-lg"
					on:click={() => toggleYear(yearGroup.year)}
				>
					{#if yearCollapsed}
						<ChevronRight className="size-3" strokeWidth="2" />
					{:else}
						<ChevronDown className="size-3" strokeWidth="2" />
					{/if}
					<span>{yearGroup.year}</span>
				</button>

				{#if !yearCollapsed}
					<div class="pl-3 space-y-0.5">
						{#each yearGroup.months as monthGroup (`${yearGroup.year}-${monthGroup.month}`)}
							{@const monthKey = `${yearGroup.year}-${monthGroup.month}`}
							{@const monthCollapsed = collapsedMonths.has(monthKey)}
							<div>
								<button
									type="button"
									class="w-full flex items-center gap-1.5 px-2 py-1 text-xs font-medium text-gray-500 dark:text-gray-400 hover:bg-gray-50 dark:hover:bg-gray-900 rounded-lg"
									on:click={() => toggleMonth(monthKey)}
								>
									{#if monthCollapsed}
										<ChevronRight className="size-3" strokeWidth="2" />
									{:else}
										<ChevronDown className="size-3" strokeWidth="2" />
									{/if}
									<span>{monthGroup.month_label}</span>
									<span class="ml-auto text-[10px] text-gray-400">{monthGroup.days.length}</span>
								</button>

								{#if !monthCollapsed}
									<div class="pl-2 space-y-0.5">
										{#each monthGroup.days as day (day.node.id)}
											{@const isExpanded = expandedNodeIds.has(day.node.id)}
											{@const dayChildIds = day.children.map((c) => c.node.id)}
											<div>
												<div
													class="group flex items-center gap-1 rounded-lg px-1.5 py-1 text-xs transition cursor-pointer {activeNodeId ===
													day.node.id
														? 'bg-gray-100 dark:bg-gray-850 text-gray-950 dark:text-white font-medium'
														: 'text-gray-700 dark:text-gray-300 hover:bg-gray-50 dark:hover:bg-gray-900'}"
												>
													<button
														type="button"
														class="flex size-4 shrink-0 items-center justify-center rounded hover:bg-gray-200 dark:hover:bg-gray-800 text-gray-400"
														on:click|stopPropagation={() => onToggleNodeExpand(day.node.id)}
														disabled={day.children.length === 0}
														aria-label={$i18n.t('Toggle children')}
													>
														{#if day.children.length > 0}
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
														on:click={() => onSelectNode(day.node.id)}
													>
														<Calendar className="size-3.5 shrink-0 text-blue-500" strokeWidth="1.6" />
														<span class="truncate">{day.node.title}</span>
													</button>

													<button
														type="button"
														class="hidden group-hover:flex p-0.5 rounded hover:bg-gray-200 dark:hover:bg-gray-800 text-gray-500"
														title={$i18n.t('Add child note')}
														on:click|stopPropagation={() => onCreateChild(day.node.id)}
													>
														<Plus className="size-3" strokeWidth="2" />
													</button>
												</div>

												{#if isExpanded && day.children.length > 0}
													<div>
														{#each day.children as child, idx (child.node.id)}
															<TreeNodeRow
																item={child}
																depth={1}
																{activeNodeId}
																siblingIds={dayChildIds}
																index={idx}
																expandedIds={expandedNodeIds}
																onToggleExpand={onToggleNodeExpand}
																onSelect={onSelectNode}
																{onCreateChild}
																{onReorderSibling}
															/>
														{/each}
													</div>
												{/if}
											</div>
										{/each}
									</div>
								{/if}
							</div>
						{/each}
					</div>
				{/if}
			</div>
		{/each}
	</div>
{/if}
